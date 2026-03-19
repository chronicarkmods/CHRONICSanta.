import os
import asyncio
from gamercon import RCON
import discord

# --- ENVIRONMENT VARIABLES ---
TOKEN = os.getenv("DISCORD_TOKEN")
servers = RCON_SERVERS.split(",") if RCON_SERVERS else []  # Example: IP1:PORT1,IP2:PORT2,...,IP9:PORT9
ADMIN_PASS = os.getenv("RCON_PASSWORD")       # Admin password for all servers
DISCORD_CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID"))  # Channel where logs will go

# --- Discord bot setup ---
intents = discord.Intents.default()
intents.messages = True
bot = discord.Client(intents=intents)

# --- Async RCON connections ---
async def connect_rcon(server_list, password):
    connections = []
    for s in server_list:
        ip_port = s.split(":")
        if len(ip_port) != 2:
            print(f"Invalid server entry: {s}")
            continue
        ip, port = ip_port
        rcon = RCON(ip.strip(), int(port.strip()), password=password)
        try:
            await rcon.connect()  # Async connect
            print(f"Connected to {ip}:{port}")
            connections.append(rcon)
        except Exception as e:
            print(f"Failed to connect {ip}:{port} → {e}")
    return connections

# --- Async log monitoring ---
async def monitor_logs(connections):
    while True:
        for rcon in connections:
            try:
                # Replace 'GetChat' with the correct ASA command for tribe/chat logs
                logs = await rcon.command("GetChat")  
                if logs:
                    channel = bot.get_channel(DISCORD_CHANNEL_ID)
                    if channel:
                        for line in logs.split("\n"):
                            if line.strip():
                                await channel.send(f"[{rcon.host}] {line}")
            except Exception as e:
                print(f"Error fetching logs from {rcon.host}: {e}")
        await asyncio.sleep(5)  # Poll every 5 seconds

# --- Bot startup ---
@bot.event
async def on_ready():
    print(f"Bot online: {bot.user}")
    server_list = RCON_SERVERS.split(",")
    connections = await connect_rcon(server_list, ADMIN_PASS)
    if connections:
        asyncio.create_task(monitor_logs(connections))
    else:
        print("No RCON connections available. Check your servers and password.")

# --- Run bot ---
bot.run(TOKEN)
