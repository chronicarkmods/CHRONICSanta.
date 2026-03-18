# main.py
import discord
from discord.ext import commands, tasks
import asyncio
import os
import ftplib
import json
import re
from dotenv import load_dotenv
from datetime import datetime

# -------------------------
# LOAD ENV VARIABLES
# -------------------------
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
FTP_HOST = os.getenv("FTP_HOST")
FTP_USER = os.getenv("FTP_USER")
FTP_PASS = os.getenv("FTP_PASS")
FTP_PATH = os.getenv("FTP_PATH")

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
# FETCH LOG FILES FROM FTP
# -------------------------
def fetch_logs():
    ftp = ftplib.FTP(FTP_HOST)
    ftp.login(FTP_USER, FTP_PASS)
    ftp.cwd(FTP_PATH)

    files = ftp.nlst()
    log_files = [f for f in files if f.lower().endswith(".log")]

    log_data = {}
    for log_file in log_files:
        lines = []
        ftp.retrlines(f"RETR {log_file}", lines.append)
        log_data[log_file] = lines

    ftp.quit()
    return log_data

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

    # prevent duplicate tribe
    if tribe_name in data["tribes"]:
        await ctx.send(f"⚠️ Tribe {tribe_name} is already registered!")
        return

    # create role
    role = await guild.create_role(name=tribe_name)

    # give role to user
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
# LOG MONITORING
# -------------------------
async def monitor_logs():
    await bot.wait_until_ready()
    data = load_data()
    guild = bot.guilds[0]

    while True:
        try:
            all_logs = fetch_logs()

            for log_file, lines in all_logs.items():
                last_line = data["maps"].get(log_file, 0)
                new_lines = lines[last_line:]

                for line in new_lines:
                    event = parse_event(line)
                    if not event:
                        continue

                    tribe = extract_tribe(line)
                    if not tribe or tribe not in data["tribes"]:
                        continue

                    # skip paused tribes
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

                data["maps"][log_file] = len(lines)

            save_data(data)

        except Exception as e:
            print("Error:", e)

        await asyncio.sleep(30)

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
