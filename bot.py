import discord
from discord import app_commands
import random
import string
import os
import json

TOKEN = os.environ["TOKEN"]

# store keys per user: { user_id: [key1, key2, ...] }
# store blacklist: { user_id: reason }
DATA_FILE = "data.json"

def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    return {"keys": {}, "blacklist": {}}

def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

def gen_key():
    chars = string.ascii_letters + string.digits
    return "Vanta-" + "".join(random.choices(chars, k=15))

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

@tree.command(name="genv2key", description="Generate a Vanta V2 key for yourself")
async def genv2key(interaction: discord.Interaction):
    key = gen_key()
    data = load_data()
    uid = str(interaction.user.id)
    data["keys"].setdefault(uid, []).append(key)
    save_data(data)
    embed = discord.Embed(title="Vanta V2 Key", description=f"```{key}```", color=0x5080FF)
    embed.set_footer(text="Vanta.cc")
    await interaction.response.send_message(embed=embed, ephemeral=True)

@tree.command(name="genv2keyto", description="Generate a Vanta V2 key and DM it to a user")
@app_commands.describe(user="The user to send the key to")
async def genv2keyto(interaction: discord.Interaction, user: discord.Member):
    key = gen_key()
    data = load_data()
    uid = str(user.id)
    data["keys"].setdefault(uid, []).append(key)
    save_data(data)
    embed = discord.Embed(title="Vanta V2 Key", description=f"```{key}```", color=0x5080FF)
    embed.set_footer(text="Vanta.cc")
    try:
        await user.send(embed=embed)
        await interaction.response.send_message(f"Key sent to {user.mention} via DM.", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message(f"Couldn't DM {user.mention} — they may have DMs disabled.", ephemeral=True)

@tree.command(name="sendmessageto", description="Send a custom DM to a user")
@app_commands.describe(user="The user to message", message="The message to send")
async def sendmessageto(interaction: discord.Interaction, user: discord.Member, message: str):
    embed = discord.Embed(description=message, color=0x5080FF)
    embed.set_author(name="Message from Vanta.cc")
    embed.set_footer(text="Vanta.cc")
    try:
        await user.send(embed=embed)
        await interaction.response.send_message(f"Message sent to {user.mention}.", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message(f"Couldn't DM {user.mention} — they may have DMs disabled.", ephemeral=True)

@tree.command(name="checkkeys", description="Check all keys sent to a user")
@app_commands.describe(user="The user to check")
async def checkkeys(interaction: discord.Interaction, user: discord.Member):
    data = load_data()
    uid = str(user.id)
    keys = data["keys"].get(uid, [])
    blacklisted = uid in data["blacklist"]
    if not keys:
        await interaction.response.send_message(f"No keys found for {user.mention}.", ephemeral=True)
        return
    key_list = "\n".join(f"• {k}" for k in keys)
    status = f"🚫 Blacklisted: {data['blacklist'][uid]}" if blacklisted else "✅ Active"
    embed = discord.Embed(title=f"Keys for {user.display_name}", color=0xFF4444 if blacklisted else 0x5080FF)
    embed.add_field(name="Status", value=status, inline=False)
    embed.add_field(name=f"Keys ({len(keys)})", value=f"```{key_list}```", inline=False)
    embed.set_footer(text="Vanta.cc")
    await interaction.response.send_message(embed=embed, ephemeral=True)

@tree.command(name="blacklist", description="Blacklist a user and DM them the reason")
@app_commands.describe(user="The user to blacklist", reason="Reason for blacklist")
async def blacklist(interaction: discord.Interaction, user: discord.Member, reason: str):
    data = load_data()
    uid = str(user.id)
    data["blacklist"][uid] = reason
    save_data(data)
    embed = discord.Embed(
        title="You have been blacklisted from Vanta.cc",
        description=f"**Reason:** {reason}",
        color=0xFF2222
    )
    embed.set_footer(text="Vanta.cc")
    try:
        await user.send(embed=embed)
        await interaction.response.send_message(f"{user.mention} has been blacklisted. Reason: {reason}", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message(f"{user.mention} blacklisted but couldn't DM them (DMs disabled). Reason: {reason}", ephemeral=True)

@client.event
async def on_ready():
    await tree.sync()
    print(f"Logged in as {client.user}")

client.run(TOKEN)
