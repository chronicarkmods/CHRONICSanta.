import os
import re
import json
import sqlite3
import hashlib
import asyncio
from collections import defaultdict, deque
from datetime import datetime, timezone

import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv
from mcrcon import MCRcon


# =========================
# ENV / CONFIG
# =========================
load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
GUILD_ID = int(os.getenv("GUILD_ID", "0"))
COMMAND_PREFIX = os.getenv("COMMAND_PREFIX", "!")
ADMIN_ROLE_NAME = os.getenv("ADMIN_ROLE_NAME", "Admin").strip()
TRIBE_TEXT_CATEGORY_ID = int(os.getenv("TRIBE_TEXT_CATEGORY_ID", "0"))
TRIBE_VOICE_CATEGORY_ID = int(os.getenv("TRIBE_VOICE_CATEGORY_ID", "0"))
LOG_POLL_SECONDS = int(os.getenv("LOG_POLL_SECONDS", "20"))
DATABASE_FILE = os.getenv("DATABASE_FILE", "bot_data.db").strip()

if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN is missing.")
if not GUILD_ID:
    raise RuntimeError("GUILD_ID is missing.")
if not TRIBE_TEXT_CATEGORY_ID:
    raise RuntimeError("TRIBE_TEXT_CATEGORY_ID is missing.")
if not TRIBE_VOICE_CATEGORY_ID:
    raise RuntimeError("TRIBE_VOICE_CATEGORY_ID is missing.")

with open("servers.json", "r", encoding="utf-8") as f:
    SERVERS = json.load(f)

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix=COMMAND_PREFIX, intents=intents, help_command=None)

recent_hashes = defaultdict(lambda: deque(maxlen=1500))
startup_complete = False


# =========================
# DATABASE
# =========================
def db():
    conn = sqlite3.connect(DATABASE_FILE)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS tribes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tribe_name TEXT UNIQUE NOT NULL,
            role_id INTEGER NOT NULL,
            text_channel_id INTEGER NOT NULL,
            voice_channel_id INTEGER NOT NULL,
            paid INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS tribe_members (
            tribe_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            UNIQUE(tribe_id, user_id)
        )
    """)

    conn.commit()
    conn.close()


def get_tribe_by_name(tribe_name: str):
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM tribes WHERE LOWER(tribe_name)=LOWER(?)", (tribe_name,))
    row = cur.fetchone()
    conn.close()
    return row


def get_all_tribes():
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT * FROM tribes ORDER BY tribe_name COLLATE NOCASE")
    rows = cur.fetchall()
    conn.close()
    return rows


def create_tribe_record(tribe_name: str, role_id: int, text_channel_id: int, voice_channel_id: int):
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO tribes (tribe_name, role_id, text_channel_id, voice_channel_id, paid, created_at)
        VALUES (?, ?, ?, ?, 0, ?)
    """, (
        tribe_name,
        role_id,
        text_channel_id,
        voice_channel_id,
        datetime.now(timezone.utc).isoformat()
    ))
    conn.commit()
    conn.close()


def set_tribe_paid(tribe_name: str, paid: int):
    conn = db()
    cur = conn.cursor()
    cur.execute("UPDATE tribes SET paid=? WHERE LOWER(tribe_name)=LOWER(?)", (paid, tribe_name))
    conn.commit()
    updated = cur.rowcount
    conn.close()
    return updated > 0


def add_member_to_tribe(tribe_name: str, user_id: int):
    tribe = get_tribe_by_name(tribe_name)
    if not tribe:
        return False, "Tribe not found."

    conn = db()
    cur = conn.cursor()
    try:
        cur.execute("INSERT OR IGNORE INTO tribe_members (tribe_id, user_id) VALUES (?, ?)", (tribe["id"], user_id))
        conn.commit()
    finally:
        conn.close()
    return True, "Member added."


def remove_member_from_tribe(tribe_name: str, user_id: int):
    tribe = get_tribe_by_name(tribe_name)
    if not tribe:
        return False, "Tribe not found."

    conn = db()
    cur = conn.cursor()
    cur.execute("DELETE FROM tribe_members WHERE tribe_id=? AND user_id=?", (tribe["id"], user_id))
    conn.commit()
    conn.close()
    return True, "Member removed."


# =========================
# HELPERS
# =========================
def is_admin_member(member: discord.Member) -> bool:
    if member.guild_permissions.administrator or member.guild_permissions.manage_guild:
        return True
    return any(role.name == ADMIN_ROLE_NAME for role in member.roles)


def admin_only():
    async def predicate(ctx):
        return is_admin_member(ctx.author)
    return commands.check(predicate)


def chunk_text(text: str, size: int = 1800):
    text = text or ""
    for i in range(0, len(text), size):
        yield text[i:i + size]


async def safe_send(channel: discord.TextChannel, content: str):
    for chunk in chunk_text(content, 1800):
        await channel.send(chunk)


def normalize_lines(raw: str):
    if not raw:
        return []

    raw = raw.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.strip() for line in raw.split("\n") if line.strip()]
    return lines


def line_hash(server_name: str, line: str) -> str:
    return hashlib.sha256(f"{server_name}|{line}".encode("utf-8")).hexdigest()


def looks_like_raid(line: str) -> bool:
    raid_keywords = [
        "destroyed",
        "killed",
        "was killed",
        "your",
        "demolish",
        "damaged",
        "attacked",
        "c4",
        "rocket",
        "tek rifle",
        "auto turret",
        "heavy turret",
        "plant species",
        "enemy",
        "claimed",
        "stole",
        "explosive",
        "soaked",
        "generator",
        "vault",
        "bed",
        "foundation",
        "deathwall",
    ]
    lower = line.lower()
    return any(word in lower for word in raid_keywords)


def build_alert_prefix(tribe_name: str, server_name: str, raid: bool):
    if raid:
        return f"🚨 **RAID ALERT** • **{tribe_name}** • `{server_name}`"
    return f"📜 **Tribe Log Update** • **{tribe_name}** • `{server_name}`"


def run_rcon_command_sync(server: dict, command: str) -> str:
    with MCRcon(server["host"], server["password"], port=int(server["port"])) as mcr:
        response = mcr.command(command)
        return response or ""


async def run_rcon_command(server: dict, command: str) -> str:
    return await asyncio.to_thread(run_rcon_command_sync, server, command)


async def fetch_server_logs(server: dict) -> list[str]:
    command = server.get("log_command", "GetGameLog")
    try:
        raw = await run_rcon_command(server, command)
        return normalize_lines(raw)
    except Exception as e:
        return [f"[BOT_RCON_ERROR] {server['name']}: {e}"]


async def get_guild() -> discord.Guild:
    guild = bot.get_guild(GUILD_ID)
    if guild is None:
        guild = await bot.fetch_guild(GUILD_ID)
    return guild


async def get_text_category(guild: discord.Guild) -> discord.CategoryChannel:
    channel = guild.get_channel(TRIBE_TEXT_CATEGORY_ID)
    if not isinstance(channel, discord.CategoryChannel):
        raise RuntimeError("TRIBE_TEXT_CATEGORY_ID is not a valid category.")
    return channel


async def get_voice_category(guild: discord.Guild) -> discord.CategoryChannel:
    channel = guild.get_channel(TRIBE_VOICE_CATEGORY_ID)
    if not isinstance(channel, discord.CategoryChannel):
        raise RuntimeError("TRIBE_VOICE_CATEGORY_ID is not a valid category.")
    return channel


async def add_member_permissions(guild: discord.Guild, tribe_row, member: discord.Member):
    role = guild.get_role(int(tribe_row["role_id"]))
    text_channel = guild.get_channel(int(tribe_row["text_channel_id"]))
    voice_channel = guild.get_channel(int(tribe_row["voice_channel_id"]))

    if role:
        await member.add_roles(role, reason="Added to tribe")
    if isinstance(text_channel, discord.TextChannel):
        await text_channel.set_permissions(member, read_messages=True, send_messages=True, view_channel=True)
    if isinstance(voice_channel, discord.VoiceChannel):
        await voice_channel.set_permissions(member, view_channel=True, connect=True, speak=True)


async def remove_member_permissions(guild: discord.Guild, tribe_row, member: discord.Member):
    role = guild.get_role(int(tribe_row["role_id"]))
    text_channel = guild.get_channel(int(tribe_row["text_channel_id"]))
    voice_channel = guild.get_channel(int(tribe_row["voice_channel_id"]))

    if role and role in member.roles:
        await member.remove_roles(role, reason="Removed from tribe")
    if isinstance(text_channel, discord.TextChannel):
        await text_channel.set_permissions(member, overwrite=None)
    if isinstance(voice_channel, discord.VoiceChannel):
        await voice_channel.set_permissions(member, overwrite=None)


# =========================
# BOT EVENTS
# =========================
@bot.event
async def on_ready():
    global startup_complete

    print("=" * 60)
    print(f"Logged in as: {bot.user} ({bot.user.id})")
    print(f"Guild ID: {GUILD_ID}")
    print(f"Loaded servers: {len(SERVERS)}")
    print(f"Poll interval: {LOG_POLL_SECONDS}s")
    print("=" * 60)

    if not startup_complete:
        poll_logs.start()
        startup_complete = True


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CheckFailure):
        await ctx.send("❌ You do not have permission to use that command.")
        return

    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.send("❌ Missing argument.")
        return

    if isinstance(error, commands.CommandNotFound):
        return

    await ctx.send(f"❌ Error: {error}")


# =========================
# COMMANDS
# =========================
@bot.command(name="help")
async def help_command(ctx):
    msg = f"""
**ChronicArk Santa Commands**

`{COMMAND_PREFIX}registertribe Tribe Name`
Create private tribe text + voice channels and tribe role.

`{COMMAND_PREFIX}approvetribe Tribe Name`
Mark tribe as paid/active.

`{COMMAND_PREFIX}unapprovetribe Tribe Name`
Disable paid status.

`{COMMAND_PREFIX}addtribemember Tribe Name @user`
Add a user to a tribe role/channel.

`{COMMAND_PREFIX}removetribemember Tribe Name @user`
Remove a user from tribe role/channel.

`{COMMAND_PREFIX}listtribes`
Show all tribes and paid status.

`{COMMAND_PREFIX}sendrcon ServerName command here`
Run a raw RCON command on one server.

`{COMMAND_PREFIX}testalert Tribe Name message here`
Send a test alert to a tribe's private text channel.
"""
    await ctx.send(msg)


@bot.command()
@admin_only()
async def registertribe(ctx, *, tribe_name: str):
    guild = ctx.guild
    if guild is None:
        await ctx.send("❌ Must be used inside your server.")
        return

    existing = get_tribe_by_name(tribe_name)
    if existing:
        await ctx.send("❌ That tribe already exists.")
        return

    text_category = await get_text_category(guild)
    voice_category = await get_voice_category(guild)

    role = await guild.create_role(name=tribe_name, mentionable=True, reason="New tribe registration")

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        role: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, connect=True, speak=True),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True, manage_roles=True, connect=True, speak=True),
    }

    safe_name = re.sub(r"[^a-zA-Z0-9-]+", "-", tribe_name.lower()).strip("-")
    text_channel = await guild.create_text_channel(
        name=f"{safe_name}-logs",
        category=text_category,
        overwrites=overwrites,
        topic=f"Private tribe logs for {tribe_name}",
        reason="New tribe registration",
    )

    voice_channel = await guild.create_voice_channel(
        name=f"{tribe_name} Voice",
        category=voice_category,
        overwrites=overwrites,
        reason="New tribe registration",
    )

    create_tribe_record(
        tribe_name=tribe_name,
        role_id=role.id,
        text_channel_id=text_channel.id,
        voice_channel_id=voice_channel.id,
    )

    await text_channel.send(
        f"✅ Welcome **{tribe_name}**.\n"
        f"This private channel will receive tribe logs and raid alerts once the tribe is approved."
    )

    await ctx.send(
        f"✅ Tribe created: **{tribe_name}**\n"
        f"Role: {role.mention}\n"
        f"Text: {text_channel.mention}\n"
        f"Voice: **{voice_channel.name}**\n"
        f"Status: **UNPAID / NOT ACTIVE YET**"
    )


@bot.command()
@admin_only()
async def approvetribe(ctx, *, tribe_name: str):
    ok = set_tribe_paid(tribe_name, 1)
    if not ok:
        await ctx.send("❌ Tribe not found.")
        return

    tribe = get_tribe_by_name(tribe_name)
    guild = ctx.guild
    channel = guild.get_channel(int(tribe["text_channel_id"]))
    if isinstance(channel, discord.TextChannel):
        await channel.send("✅ Your tribe subscription is now **ACTIVE**. Logs and raid alerts are live.")

    await ctx.send(f"✅ Tribe **{tribe_name}** approved and marked paid.")


@bot.command()
@admin_only()
async def unapprovetribe(ctx, *, tribe_name: str):
    ok = set_tribe_paid(tribe_name, 0)
    if not ok:
        await ctx.send("❌ Tribe not found.")
        return

    tribe = get_tribe_by_name(tribe_name)
    guild = ctx.guild
    channel = guild.get_channel(int(tribe["text_channel_id"]))
    if isinstance(channel, discord.TextChannel):
        await channel.send("⚠️ Your tribe subscription has been set to **INACTIVE**.")

    await ctx.send(f"⚠️ Tribe **{tribe_name}** set inactive.")


@bot.command()
@admin_only()
async def addtribemember(ctx, tribe_name: str, member: discord.Member):
    tribe = get_tribe_by_name(tribe_name)
    if not tribe:
        await ctx.send("❌ Tribe not found.")
        return

    ok, message = add_member_to_tribe(tribe_name, member.id)
    if not ok:
        await ctx.send(f"❌ {message}")
        return

    await add_member_permissions(ctx.guild, tribe, member)
    await ctx.send(f"✅ Added {member.mention} to **{tribe_name}**.")


@bot.command()
@admin_only()
async def removetribemember(ctx, tribe_name: str, member: discord.Member):
    tribe = get_tribe_by_name(tribe_name)
    if not tribe:
        await ctx.send("❌ Tribe not found.")
        return

    ok, message = remove_member_from_tribe(tribe_name, member.id)
    if not ok:
        await ctx.send(f"❌ {message}")
        return

    await remove_member_permissions(ctx.guild, tribe, member)
    await ctx.send(f"✅ Removed {member.mention} from **{tribe_name}**.")


@bot.command()
@admin_only()
async def listtribes(ctx):
    tribes = get_all_tribes()
    if not tribes:
        await ctx.send("No tribes registered yet.")
        return

    lines = []
    for tribe in tribes:
        status = "ACTIVE" if int(tribe["paid"]) == 1 else "INACTIVE"
        lines.append(f"• **{tribe['tribe_name']}** — {status}")

    await safe_send(ctx.channel, "\n".join(lines))


@bot.command()
@admin_only()
async def sendrcon(ctx, server_name: str, *, command_text: str):
    server = next((s for s in SERVERS if s["name"].lower() == server_name.lower()), None)
    if not server:
        await ctx.send("❌ Server not found in servers.json.")
        return

    try:
        result = await run_rcon_command(server, command_text)
        if not result.strip():
            result = "(empty response)"
        await safe_send(ctx.channel, f"```{result[:6000]}```")
    except Exception as e:
        await ctx.send(f"❌ RCON error: {e}")


@bot.command()
@admin_only()
async def testalert(ctx, tribe_name: str, *, message: str):
    tribe = get_tribe_by_name(tribe_name)
    if not tribe:
        await ctx.send("❌ Tribe not found.")
        return

    channel = ctx.guild.get_channel(int(tribe["text_channel_id"]))
    role = ctx.guild.get_role(int(tribe["role_id"]))
    if not isinstance(channel, discord.TextChannel):
        await ctx.send("❌ Tribe text channel not found.")
        return

    ping = role.mention if role else tribe_name
    await channel.send(f"🚨 TEST ALERT {ping}\n{message}")
    await ctx.send("✅ Test alert sent.")


# =========================
# LOG POLLING
# =========================
@tasks.loop(seconds=LOG_POLL_SECONDS)
async def poll_logs():
    guild = await get_guild()
    tribes = get_all_tribes()
    active_tribes = [t for t in tribes if int(t["paid"]) == 1]

    if not active_tribes:
        return

    for server in SERVERS:
        try:
            lines = await fetch_server_logs(server)
        except Exception as e:
            print(f"[poll_logs] fetch failed for {server['name']}: {e}")
            continue

        new_lines = []
        for line in lines:
            hashed = line_hash(server["name"], line)
            if hashed in recent_hashes[server["name"]]:
                continue
            recent_hashes[server["name"]].append(hashed)
            new_lines.append(line)

        if not new_lines:
            continue

        for line in new_lines:
            line_lower = line.lower()

            for tribe in active_tribes:
                tribe_name = tribe["tribe_name"]
                if tribe_name.lower() not in line_lower:
                    continue

                channel = guild.get_channel(int(tribe["text_channel_id"]))
                role = guild.get_role(int(tribe["role_id"]))

                if not isinstance(channel, discord.TextChannel):
                    continue

                raid = looks_like_raid(line)
                prefix = build_alert_prefix(tribe_name, server["name"], raid)

                if raid and role:
                    message = f"{prefix}\n{role.mention}\n```{line}```"
                else:
                    message = f"{prefix}\n```{line}```"

                await safe_send(channel, message)


@poll_logs.before_loop
async def before_poll_logs():
    await bot.wait_until_ready()


# =========================
# STARTUP
# =========================
def main():
    init_db()
    print("Starting ChronicArk Santa...")
    bot.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()
