import discord
from discord import app_commands
import random
import string
import os
import json
import time
import asyncio
from typing import Optional
from api import start_api_thread

TOKEN = os.environ["TOKEN"]
ANNOUNCE_CHANNEL_ID = int(os.environ.get("ANNOUNCE_CHANNEL", "0"))
OWNER_ROLE_NAME = os.environ.get("OWNER_ROLE", "Owner")

DATA_FILE = "data.json"

# In-memory giveaway stores: message_id -> giveaway dict
active_giveaways: dict[int, dict] = {}
ended_giveaways: dict[int, dict] = {}

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

def parse_duration(duration: str) -> int | None:
    """Parse duration string like '1h', '7d', '2w', '1m', 'lifetime' into seconds. Returns None for lifetime."""
    duration = duration.strip().lower()
    if duration in ("lifetime", "life", "perm", "permanent"):
        return None
    units = {"h": 3600, "d": 86400, "w": 604800, "m": 2592000}
    for suffix, mult in units.items():
        if duration.endswith(suffix):
            try:
                return int(duration[:-1]) * mult
            except ValueError:
                return 0
    return 0

def duration_label(seconds: int | None) -> str:
    if seconds is None:
        return "Lifetime"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    if seconds < 604800:
        return f"{seconds // 86400}d"
    if seconds < 2592000:
        return f"{seconds // 604800}w"
    return f"{seconds // 2592000} month(s)"

# ─────────────────────────────────────────────
#  GIVEAWAY HELPERS
# ─────────────────────────────────────────────

def build_giveaway_embed(prize: str, host: discord.Member, end_time: int, entries: list[int], ended: bool = False) -> discord.Embed:
    color = 0xAA00FF if not ended else 0x555555
    title = "🎉 GIVEAWAY 🎉" if not ended else "🎉 GIVEAWAY ENDED 🎉"
    embed = discord.Embed(title=title, description=f"**Prize:** {prize}", color=color)
    embed.add_field(name="Hosted by", value=host.mention, inline=True)
    embed.add_field(name="Entries", value=str(len(entries)), inline=True)
    if not ended:
        embed.add_field(name="Ends", value=f"<t:{end_time}:R>", inline=True)
        embed.set_footer(text="Click ✅ Join or ❌ Leave to manage your entry • Vanta.cc")
    else:
        embed.set_footer(text="Giveaway ended • Vanta.cc")
    return embed

async def end_giveaway(message_id: int):
    """Pick a winner and announce it."""
    giveaway = active_giveaways.get(message_id)
    if not giveaway or giveaway.get("ended"):
        return

    giveaway["ended"] = True

    channel = client.get_channel(giveaway["channel_id"])
    if not channel:
        return

    try:
        message = await channel.fetch_message(message_id)
    except discord.NotFound:
        return

    entries = giveaway["entries"]
    prize = giveaway["prize"]
    host_id = giveaway["host_id"]
    end_time = giveaway["end_time"]

    # Save to ended store for rerolls
    ended_giveaways[message_id] = {
        "entries": list(entries),
        "prize": prize,
    }

    # Resolve host member
    host = channel.guild.get_member(host_id)
    host_mention = host.mention if host else f"<@{host_id}>"

    # Build ended embed
    ended_embed = build_giveaway_embed(
        prize,
        host or discord.Object(id=host_id),
        end_time,
        entries,
        ended=True
    )

    if not entries:
        ended_embed.add_field(name="Winner", value="No one entered 😢", inline=False)
        await message.edit(embed=ended_embed, view=None)
        await channel.send(f"🎉 The giveaway for **{prize}** has ended but nobody entered!")
    else:
        winner_id = random.choice(entries)
        winner = channel.guild.get_member(winner_id)
        winner_mention = winner.mention if winner else f"<@{winner_id}>"
        ended_embed.add_field(name="Winner", value=winner_mention, inline=False)
        await message.edit(embed=ended_embed, view=None)
        await channel.send(
            f"🎉 Congratulations {winner_mention}! You won **{prize}**!\n"
            f"Hosted by {host_mention}."
        )

    # Remove from active giveaways
    active_giveaways.pop(message_id, None)


class GiveawayView(discord.ui.View):
    """Persistent view with Join / Leave buttons."""

    def __init__(self, message_id: int):
        # timeout=None so the view lives until the bot restarts
        super().__init__(timeout=None)
        self.message_id = message_id

    @discord.ui.button(label="✅ Join", style=discord.ButtonStyle.success, custom_id="giveaway_join")
    async def join(self, interaction: discord.Interaction, button: discord.ui.Button):
        giveaway = active_giveaways.get(self.message_id)
        if not giveaway or giveaway.get("ended"):
            await interaction.response.send_message("This giveaway has already ended.", ephemeral=True)
            return

        uid = interaction.user.id
        if uid in giveaway["entries"]:
            await interaction.response.send_message("You're already entered in this giveaway!", ephemeral=True)
            return

        giveaway["entries"].append(uid)

        # Update embed entry count
        host = interaction.guild.get_member(giveaway["host_id"])
        embed = build_giveaway_embed(
            giveaway["prize"],
            host or discord.Object(id=giveaway["host_id"]),
            giveaway["end_time"],
            giveaway["entries"]
        )
        await interaction.response.edit_message(embed=embed, view=self)
        # Confirm to user
        await interaction.followup.send("✅ You've entered the giveaway! Good luck!", ephemeral=True)

    @discord.ui.button(label="❌ Leave", style=discord.ButtonStyle.danger, custom_id="giveaway_leave")
    async def leave(self, interaction: discord.Interaction, button: discord.ui.Button):
        giveaway = active_giveaways.get(self.message_id)
        if not giveaway or giveaway.get("ended"):
            await interaction.response.send_message("This giveaway has already ended.", ephemeral=True)
            return

        uid = interaction.user.id
        if uid not in giveaway["entries"]:
            await interaction.response.send_message("You're not in this giveaway.", ephemeral=True)
            return

        giveaway["entries"].remove(uid)

        host = interaction.guild.get_member(giveaway["host_id"])
        embed = build_giveaway_embed(
            giveaway["prize"],
            host or discord.Object(id=giveaway["host_id"]),
            giveaway["end_time"],
            giveaway["entries"]
        )
        await interaction.response.edit_message(embed=embed, view=self)
        await interaction.followup.send("❌ You've left the giveaway.", ephemeral=True)


# ─────────────────────────────────────────────
#  GIVEAWAY COMMANDS
# ─────────────────────────────────────────────

@tree.command(name="giveaway", description="Start a giveaway in this channel")
@app_commands.describe(
    duration="How long the giveaway runs: e.g. 1h, 30m, 7d",
    prize="What you're giving away"
)
async def giveaway(interaction: discord.Interaction, duration: str, prize: str):
    if not has_owner_role(interaction):
        return await deny(interaction)

    secs = parse_duration(duration)
    if not secs:
        await interaction.response.send_message(
            "❌ Invalid duration. Use e.g. `30m`, `1h`, `7d`. Lifetime is not valid for giveaways.",
            ephemeral=True
        )
        return

    end_time = int(time.time()) + secs

    # Send the giveaway embed first so we get a message ID
    await interaction.response.defer(ephemeral=True)

    embed = build_giveaway_embed(prize, interaction.user, end_time, [])
    # Placeholder view — we'll replace it once we have the message ID
    placeholder_view = discord.ui.View(timeout=None)
    msg = await interaction.channel.send(embed=embed, view=placeholder_view)

    # Now build the real view tied to this message ID
    view = GiveawayView(message_id=msg.id)
    await msg.edit(view=view)

    # Store giveaway state
    active_giveaways[msg.id] = {
        "channel_id": interaction.channel.id,
        "host_id": interaction.user.id,
        "prize": prize,
        "end_time": end_time,
        "entries": [],
        "ended": False,
    }

    await interaction.followup.send(
        f"✅ Giveaway started! It will end <t:{end_time}:R>.",
        ephemeral=True
    )

    # Schedule the end
    async def _wait_and_end():
        await asyncio.sleep(secs)
        await end_giveaway(msg.id)

    asyncio.create_task(_wait_and_end())


@tree.command(name="giveawayend", description="End a giveaway early and pick a winner now")
@app_commands.describe(message_id="The message ID of the giveaway to end")
async def giveawayend(interaction: discord.Interaction, message_id: str):
    if not has_owner_role(interaction):
        return await deny(interaction)

    try:
        mid = int(message_id)
    except ValueError:
        await interaction.response.send_message("❌ Invalid message ID.", ephemeral=True)
        return

    if mid not in active_giveaways:
        await interaction.response.send_message("❌ No active giveaway found with that message ID.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)
    await end_giveaway(mid)
    await interaction.followup.send("✅ Giveaway ended and winner picked.", ephemeral=True)


@tree.command(name="giveawayreroll", description="Reroll a winner for an ended giveaway")
@app_commands.describe(message_id="The message ID of the ended giveaway")
async def giveawayreroll(interaction: discord.Interaction, message_id: str):
    if not has_owner_role(interaction):
        return await deny(interaction)

    # We keep a separate ended store for rerolls
    try:
        mid = int(message_id)
    except ValueError:
        await interaction.response.send_message("❌ Invalid message ID.", ephemeral=True)
        return

    if mid not in ended_giveaways:
        await interaction.response.send_message(
            "❌ No ended giveaway found with that message ID. Reroll only works on recently ended giveaways.",
            ephemeral=True
        )
        return

    giveaway = ended_giveaways[mid]
    entries = giveaway["entries"]
    prize = giveaway["prize"]

    if not entries:
        await interaction.response.send_message("❌ No entries to reroll from.", ephemeral=True)
        return

    winner_id = random.choice(entries)
    winner = interaction.guild.get_member(winner_id)
    winner_mention = winner.mention if winner else f"<@{winner_id}>"

    await interaction.response.send_message(
        f"🎉 Reroll! The new winner of **{prize}** is {winner_mention}! Congratulations!"
    )


@tree.command(name="giveawaylist", description="List all active giveaways in this server")
async def giveawaylist(interaction: discord.Interaction):
    if not has_owner_role(interaction):
        return await deny(interaction)

    if not active_giveaways:
        await interaction.response.send_message("No active giveaways right now.", ephemeral=True)
        return

    embed = discord.Embed(title="Active Giveaways", color=0xAA00FF)
    for mid, g in active_giveaways.items():
        channel = client.get_channel(g["channel_id"])
        ch_mention = channel.mention if channel else f"<#{g['channel_id']}>"
        embed.add_field(
            name=g["prize"],
            value=f"Channel: {ch_mention}\nEntries: {len(g['entries'])}\nEnds: <t:{g['end_time']}:R>\nMessage ID: `{mid}`",
            inline=False
        )
    embed.set_footer(text="Vanta.cc")
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ─────────────────────────────────────────────
#  EXISTING COMMANDS
# ─────────────────────────────────────────────

@tree.command(name="genv2key", description="Generate a Vanta V2 key for yourself")
@app_commands.describe(duration="Duration: e.g. 1h, 7d, 2w, 1m, lifetime")
async def genv2key(interaction: discord.Interaction, duration: str = "lifetime"):
    if not has_owner_role(interaction):
        return await deny(interaction)
    secs = parse_duration(duration)
    if secs == 0:
        await interaction.response.send_message("❌ Invalid duration. Use e.g. `1h`, `7d`, `2w`, `1m`, `lifetime`.", ephemeral=True)
        return
    key = gen_key()
    data = load_data()
    uid = str(interaction.user.id)
    expiry = int(time.time()) + secs if secs else None
    data.setdefault("key_expiry", {})[key] = expiry
    data["keys"].setdefault(uid, []).append(key)
    save_data(data)
    embed = discord.Embed(title="Vanta V2 Key", description=f"```{key}```", color=0x5080FF)
    embed.add_field(name="Duration", value=duration_label(secs), inline=True)
    if expiry:
        embed.add_field(name="Expires", value=f"<t:{expiry}:R>", inline=True)
    embed.set_footer(text="Vanta.cc")
    await interaction.response.send_message(embed=embed, ephemeral=True)

@tree.command(name="genv2keyto", description="Generate a Vanta V2 key and DM it to a user")
@app_commands.describe(user="The user to send the key to", duration="Duration: e.g. 1h, 7d, 2w, 1m, lifetime")
async def genv2keyto(interaction: discord.Interaction, user: discord.Member, duration: str = "lifetime"):
    if not has_owner_role(interaction):
        return await deny(interaction)
    secs = parse_duration(duration)
    if secs == 0:
        await interaction.response.send_message("❌ Invalid duration. Use e.g. `1h`, `7d`, `2w`, `1m`, `lifetime`.", ephemeral=True)
        return
    key = gen_key()
    data = load_data()
    uid = str(user.id)
    expiry = int(time.time()) + secs if secs else None
    data.setdefault("key_expiry", {})[key] = expiry
    data["keys"].setdefault(uid, []).append(key)
    save_data(data)
    embed = discord.Embed(title="Vanta V2 Key", description=f"```{key}```", color=0x5080FF)
    embed.add_field(name="Duration", value=duration_label(secs), inline=True)
    if expiry:
        embed.add_field(name="Expires", value=f"<t:{expiry}:R>", inline=True)
    embed.set_footer(text="Vanta.cc")
    try:
        await user.send(embed=embed)
        await interaction.response.send_message(f"Key sent to {user.mention} via DM. Duration: {duration_label(secs)}", ephemeral=True)
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

@tree.command(name="resethwid", description="Reset the HWID binding for a key")
@app_commands.describe(key="The key to reset HWID for")
async def resethwid(interaction: discord.Interaction, key: str):
    if not has_owner_role(interaction):
        return await deny(interaction)
    data = load_data()
    key_hwid = data.get("key_hwid", {})
    if key not in key_hwid:
        await interaction.response.send_message(f"No HWID bound to that key.", ephemeral=True)
        return
    del key_hwid[key]
    data["key_hwid"] = key_hwid
    save_data(data)
    await interaction.response.send_message(f"HWID reset for `{key}`. Next use will bind a new HWID.", ephemeral=True)

@client.event
async def on_ready():
    start_api_thread()
    await tree.sync()
    print(f"Logged in as {client.user}")

client.run(TOKEN)
