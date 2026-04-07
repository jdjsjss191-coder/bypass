import discord
from discord import app_commands
import random
import string
import os
import json
import time

TOKEN = os.environ["TOKEN"]
ANNOUNCE_CHANNEL_ID = int(os.environ.get("ANNOUNCE_CHANNEL", "0"))
OWNER_ROLE_NAME = os.environ.get("OWNER_ROLE", "Owner")

DATA_FILE = "data.json"

def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    return {"keys": {}, "blacklist": {}, "temp_keys": {}}

def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

def gen_key():
    chars = string.ascii_letters + string.digits
    return "Vanta-" + "".join(random.choices(chars, k=15))

def has_owner_role(interaction: discord.Interaction) -> bool:
    return any(r.name == OWNER_ROLE_NAME for r in interaction.user.roles)

async def deny(interaction: discord.Interaction):
    await interaction.response.send_message("❌ You need the **Owner** role to use this command.", ephemeral=True)

intents = discord.Intents.default()
intents.members = True
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

@tree.command(name="genv2key", description="Generate a Vanta V2 key for yourself")
async def genv2key(interaction: discord.Interaction):
    if not has_owner_role(interaction):
        return await deny(interaction)
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
    if not has_owner_role(interaction):
        return await deny(interaction)
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

@tree.command(name="keyall", description="Send a 1-hour temporary key to every member via DM")
async def keyall(interaction: discord.Interaction):
    if not has_owner_role(interaction):
        return await deny(interaction)
    await interaction.response.defer(ephemeral=True)
    data = load_data()
    expiry = int(time.time()) + 3600  # 1 hour from now
    sent = 0
    failed = 0
    for member in interaction.guild.members:
        if member.bot:
            continue
        key = gen_key()
        uid = str(member.id)
        data.setdefault("temp_keys", {})
        data["temp_keys"].setdefault(uid, []).append({"key": key, "expiry": expiry})
        embed = discord.Embed(
            title="Vanta V2 — Temporary Key (1 Hour)",
            description=f"```{key}```",
            color=0xFFAA00
        )
        embed.add_field(name="Expires", value=f"<t:{expiry}:R>", inline=False)
        embed.set_footer(text="Vanta.cc • This key expires in 1 hour")
        try:
            await member.send(embed=embed)
            sent += 1
        except discord.Forbidden:
            failed += 1
    save_data(data)
    await interaction.followup.send(f"Temp keys sent to {sent} members. {failed} couldn't be reached.", ephemeral=True)

@tree.command(name="sendmessageto", description="Send a custom DM to a user")
@app_commands.describe(user="The user to message", message="The message to send")
async def sendmessageto(interaction: discord.Interaction, user: discord.Member, message: str):
    if not has_owner_role(interaction):
        return await deny(interaction)
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
    if not has_owner_role(interaction):
        return await deny(interaction)
    data = load_data()
    uid = str(user.id)
    keys = data["keys"].get(uid, [])
    temp_keys = [t for t in data.get("temp_keys", {}).get(uid, []) if t["expiry"] > int(time.time())]
    blacklisted = uid in data["blacklist"]
    if not keys and not temp_keys:
        await interaction.response.send_message(f"No keys found for {user.mention}.", ephemeral=True)
        return
    status = f"🚫 Blacklisted: {data['blacklist'][uid]}" if blacklisted else "✅ Active"
    embed = discord.Embed(title=f"Keys for {user.display_name}", color=0xFF4444 if blacklisted else 0x5080FF)
    embed.add_field(name="Status", value=status, inline=False)
    if keys:
        embed.add_field(name=f"Permanent Keys ({len(keys)})", value="```" + "\n".join(f"• {k}" for k in keys) + "```", inline=False)
    if temp_keys:
        tlist = "\n".join(f"• {t['key']} (expires <t:{t['expiry']}:R>)" for t in temp_keys)
        embed.add_field(name=f"Active Temp Keys ({len(temp_keys)})", value=tlist, inline=False)
    embed.set_footer(text="Vanta.cc")
    await interaction.response.send_message(embed=embed, ephemeral=True)

@tree.command(name="blacklist", description="Blacklist a user and DM them the reason")
@app_commands.describe(user="The user to blacklist", reason="Reason for blacklist")
async def blacklist(interaction: discord.Interaction, user: discord.Member, reason: str):
    if not has_owner_role(interaction):
        return await deny(interaction)
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
        await interaction.response.send_message(f"{user.mention} blacklisted. Reason: {reason}", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message(f"{user.mention} blacklisted but couldn't DM them. Reason: {reason}", ephemeral=True)

@tree.command(name="announce", description="Send an announcement to the announcement channel")
@app_commands.describe(message="The announcement message")
async def announce(interaction: discord.Interaction, message: str):
    if not has_owner_role(interaction):
        return await deny(interaction)
    if ANNOUNCE_CHANNEL_ID == 0:
        await interaction.response.send_message("No announcement channel set. Add `ANNOUNCE_CHANNEL` env variable.", ephemeral=True)
        return
    channel = interaction.guild.get_channel(ANNOUNCE_CHANNEL_ID)
    if not channel:
        await interaction.response.send_message("Announcement channel not found.", ephemeral=True)
        return
    embed = discord.Embed(description=message, color=0x5080FF)
    embed.set_author(name="Vanta.cc Announcement")
    embed.set_footer(text="Vanta.cc")
    await channel.send(embed=embed)
    await interaction.response.send_message("Announcement sent.", ephemeral=True)

@tree.command(name="dmall", description="DM all members in the server")
@app_commands.describe(message="The message to send to everyone")
async def dmall(interaction: discord.Interaction, message: str):
    if not has_owner_role(interaction):
        return await deny(interaction)
    await interaction.response.defer(ephemeral=True)
    embed = discord.Embed(description=message, color=0x5080FF)
    embed.set_author(name="Message from Vanta.cc")
    embed.set_footer(text="Vanta.cc")
    sent = 0
    failed = 0
    for member in interaction.guild.members:
        if member.bot:
            continue
        try:
            await member.send(embed=embed)
            sent += 1
        except discord.Forbidden:
            failed += 1
    await interaction.followup.send(f"DM sent to {sent} members. {failed} couldn't be reached.", ephemeral=True)

@client.event
async def on_ready():
    await tree.sync()
    print(f"Logged in as {client.user}")

client.run(TOKEN)
