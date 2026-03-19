# bot.py
import discord
from discord.ext import commands, tasks
import asyncio
import os
import json
import re
from dotenv import load_dotenv
from datetime import datetime
from mcrcon import MCRcon

# -------------------------
# LOAD ENV VARIABLES
# -------------------------
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
RCON_PASS = os.getenv("RCON_PASS")
RCON_SERVERS = os.getenv("RCON_SERVERS").split(",")  # format: IP:PORT,IP:PORT,...

# -------------------------
# BOT SETUP
# -------------------------
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

DATA_FILE = "data.json"

# -------------------------
# LOAD / SAVE DATA
# -------------------------
def load_data():
    if not os.path.exists(DATA_FILE):
        return {"tribes": {}, "maps": {}}
    with open(DATA_FILE, "r") as f:
        return json.load(f)

def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=4)

# -------------------------
# RCON CONNECTIONS
# -------------------------
def get_rcon_connections():
    connections = {}
    for s in RCON_SERVERS:
        ip, port = s.split(":")
        conn = MCRcon(ip, RCON_PASS, port=int(port))
        try:
            conn.connect()
            connections[s] = conn
        except Exception as e:
            print(f"Failed RCON connection {s}: {e}")
            connections[s] = None
    return connections

# -------------------------
# PARSE EVENTS
# -------------------------
def parse_event(line):
    l = line.lower()
    if "destroyed" in l:
        return "💥 Structure Destroyed"
    if "was killed" in l:
        return "☠️ Player Killed"
    if "killed" in l:
        return "🦖 Dino Killed"
    return None

def extract_tribe(line):
    match = re.search(r'Tribe\s+(.+)', line)
    if match:
        return match.group(1).strip()
    return None

# -------------------------
# UPDATE TRIBE ACTIVITY
# -------------------------
def update_activity(data, tribe):
    data["tribes"][tribe] = {"last_seen": asyncio.get_event_loop().time()}

def is_offline(data, tribe, threshold=600):
    if tribe not in data["tribes"]:
        return True
    last = data["tribes"][tribe]["last_seen"]
    return (asyncio.get_event_loop().time() - last) > threshold

# -------------------------
# REGISTER TRIBE
# -------------------------
@bot.command()
async def register(ctx, *, tribe_name):
    data = load_data()
    guild = ctx.guild

    if tribe_name in data["tribes"]:
        await ctx.send(f"⚠️ Tribe {tribe_name} is already registered!")
        return

    # create role
    role = await guild.create_role(name=tribe_name)
    await ctx.author.add_roles(role)

    # create private channel
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(read_messages=False),
        role: discord.PermissionOverwrite(read_messages=True),
        guild.me: discord.PermissionOverwrite(read_messages=True)
    }
    channel = await guild.create_text_channel(
        name=f"{tribe_name}-logs",
        overwrites=overwrites
    )

    data["tribes"][tribe_name] = {"channel_id": channel.id, "paused": False}
    save_data(data)
    await ctx.send(f"✅ Tribe {tribe_name} registered! Channel created.")

# -------------------------
# PAUSE / UNPAUSE TRIBE
# -------------------------
@bot.command()
async def pause(ctx, *, tribe_name):
    data = load_data()
    if tribe_name not in data["tribes"]:
        await ctx.send(f"⚠️ Tribe {tribe_name} not found!")
        return
    data["tribes"][tribe_name]["paused"] = True
    save_data(data)
    await ctx.send(f"⏸️ Tribe {tribe_name} is now paused. No alerts will be sent.")

@bot.command()
async def unpause(ctx, *, tribe_name):
    data = load_data()
    if tribe_name not in data["tribes"]:
        await ctx.send(f"⚠️ Tribe {tribe_name} not found!")
        return
    data["tribes"][tribe_name]["paused"] = False
    save_data(data)
    await ctx.send(f"▶️ Tribe {tribe_name} is now unpaused. Alerts will resume.")

# -------------------------
# CLEAR TEST TRIBES
# -------------------------
@bot.command()
async def clear_test(ctx):
    data = load_data()
    removed = []
    for tribe in list(data["tribes"]):
        if "test" in tribe.lower():
            del data["tribes"][tribe]
            removed.append(tribe)
    save_data(data)
    await ctx.send(f"🧹 Cleared test tribes: {', '.join(removed) if removed else 'None'}")

# -------------------------
# CHECK REGISTERED TRIBES
# -------------------------
@bot.command()
async def check(ctx):
    data = load_data()
    if not data["tribes"]:
        await ctx.send("No tribes registered.")
        return
    tribe_list = ", ".join(data["tribes"].keys())
    await ctx.send(f"Registered tribes: {tribe_list}")

# -------------------------
# RCON CONNECTION CHECK
# -------------------------
@bot.command()
async def rconcheck(ctx):
    connections = get_rcon_connections()
    success = [s for s, c in connections.items() if c is not None]
    fail = [s for s, c in connections.items() if c is None]
    await ctx.send(f"✅ Connected: {', '.join(success) if success else 'None'}\n❌ Failed: {', '.join(fail) if fail else 'None'}")

# -------------------------
# MONITOR LOGS VIA RCON
# -------------------------
async def monitor_logs():
    await bot.wait_until_ready()
    data = load_data()
    guild = bot.guilds[0]  # assumes bot is in one server

    while True:
        try:
            connections = get_rcon_connections()
            for server_name, conn in connections.items():
                if conn is None:
                    continue
                # Example command to get logs from ARK server
                try:
                    logs = conn.command("GetLogs")  # Replace with actual RCON log command if needed
                    for line in logs.splitlines():
                        event = parse_event(line)
                        if not event:
                            continue

                        tribe = extract_tribe(line)
                        if not tribe or tribe not in data["tribes"]:
                            continue
                        if data["tribes"][tribe].get("paused"):
                            continue

                        offline = is_offline(data, tribe)
                        update_activity(data, tribe)

                        channel_id = data["tribes"][tribe]["channel_id"]
                        channel = bot.get_channel(channel_id)
                        if channel:
                            embed = discord.Embed(
                                title="🚨 OFFLINE RAID" if offline else "⚔️ Raid Activity",
                                description=line,
                                color=0xff0000 if offline else 0xffaa00,
                                timestamp=datetime.utcnow()
                            )
                            embed.add_field(name="Event", value=event, inline=False)
                            await channel.send(embed=embed)
                except Exception as e:
                    print(f"RCON command failed on {server_name}: {e}")

        except Exception as e:
            print("Monitor error:", e)

        save_data(data)
        await asyncio.sleep(30)  # repeat every 30 sec

# -------------------------
# ON READY
# -------------------------
@bot.event
async def on_ready():
    print(f"Bot online: {bot.user}")
    bot.loop.create_task(monitor_logs())

# -------------------------
# RUN BOT
# -------------------------
bot.run(TOKEN)
