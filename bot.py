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

@tree.command(name="genv2key", description="Generate a Vanta V2 key")
async def genv2key(interaction: discord.Interaction):
    key = gen_key()
    embed = discord.Embed(
        title="Vanta V2 Key",
        description=f"```{key}```",
        color=0x5080FF
    )
    embed.set_footer(text="Vanta.cc")
    await interaction.response.send_message(embed=embed, ephemeral=True)

@client.event
async def on_ready():
    await tree.sync()
    print(f"Logged in as {client.user}")

client.run(TOKEN)
