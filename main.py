import os
import discord
from discord.ext import commands
from flask import Flask
from threading import Thread

# Intents
intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f'✅ Logged in as {bot.user}')

@bot.command()
async def ping(ctx):
    await ctx.send("Pong!")

# Run bot
def run_bot():
    bot.run(os.getenv("DISCORD_TOKEN"))

# Web server (keeps bot alive)
app = Flask('')

@app.route('/')
def home():
    return "Bot is running!"

def run_web():
    app.run(host='0.0.0.0', port=8080)

Thread(target=run_web).start()
Thread(target=run_bot).start()
