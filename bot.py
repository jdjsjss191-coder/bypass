import discord
from discord import app_commands
import random
import string
import os

TOKEN = os.environ["TOKEN"]

intents = discord.Intents.default()
client = discord.Client(intents=intents)
tree = app_commands.CommandTree(client)

def gen_key():
    chars = string.ascii_letters + string.digits
    return "Vanta-" + "".join(random.choices(chars, k=15))

@tree.command(name="genv2key", description="Generate a Vanta V2 key for yourself")
async def genv2key(interaction: discord.Interaction):
    key = gen_key()
    embed = discord.Embed(
        title="Vanta V2 Key",
        description=f"```{key}```",
        color=0x5080FF
    )
    embed.set_footer(text="Vanta.cc")
    await interaction.response.send_message(embed=embed, ephemeral=True)

@tree.command(name="genv2keyto", description="Generate a Vanta V2 key and DM it to a user")
@app_commands.describe(user="The user to send the key to")
async def genv2keyto(interaction: discord.Interaction, user: discord.Member):
    key = gen_key()
    embed = discord.Embed(
        title="Vanta V2 Key",
        description=f"```{key}```",
        color=0x5080FF
    )
    embed.set_footer(text="Vanta.cc")
    try:
        await user.send(embed=embed)
        await interaction.response.send_message(
            f"Key sent to {user.mention} via DM.", ephemeral=True
        )
    except discord.Forbidden:
        await interaction.response.send_message(
            f"Couldn't DM {user.mention} — they may have DMs disabled.", ephemeral=True
        )

@client.event
async def on_ready():
    await tree.sync()
    print(f"Logged in as {client.user}")

client.run(TOKEN)
