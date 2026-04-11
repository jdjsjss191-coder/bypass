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
    return "Vyron-" + "".join(random.choices(chars, k=15))

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
        embed.set_footer(text="Click ✅ Join or ❌ Leave to manage your entry • Vyron.cc")
    else:
        embed.set_footer(text="Giveaway ended • Vyron.cc")
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
    embed.set_footer(text="Vyron.cc")
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ─────────────────────────────────────────────
#  EXISTING COMMANDS
# ─────────────────────────────────────────────

@tree.command(name="genv2key", description="Generate a Vyron V2 key for yourself")
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
    data.setdefault("key_created", {})[key] = int(time.time())
    data["keys"].setdefault(uid, []).append(key)
    save_data(data)
    embed = discord.Embed(title="Vyron V2 Key", description=f"```{key}```", color=0x5080FF)
    embed.add_field(name="Duration", value=duration_label(secs), inline=True)
    if expiry:
        embed.add_field(name="Expires", value=f"<t:{expiry}:R>", inline=True)
    embed.set_footer(text="Vyron.cc")
    await interaction.response.send_message(embed=embed, ephemeral=True)

@tree.command(name="genv2keyto", description="Generate a Vyron V2 key and DM it to a user")
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
    data.setdefault("key_created", {})[key] = int(time.time())
    data["keys"].setdefault(uid, []).append(key)
    save_data(data)
    dm_embed = discord.Embed(title="Vyron V2 Key", description=f"```{key}```", color=0x5080FF)
    dm_embed.add_field(name="Duration", value=duration_label(secs), inline=True)
    if expiry:
        dm_embed.add_field(name="Expires", value=f"<t:{expiry}:R>", inline=True)
    dm_embed.set_footer(text="Vyron.cc")
    pub_embed = discord.Embed(
        title="🔑 Key Sent",
        description=f"{interaction.user.mention} sent a Vyron V2 key to {user.mention}.",
        color=0x5080FF
    )
    pub_embed.add_field(name="Duration", value=duration_label(secs), inline=True)
    pub_embed.set_footer(text="Vyron.cc")
    try:
        await user.send(embed=dm_embed)
        await interaction.response.send_message(embed=pub_embed)
    except discord.Forbidden:
        await interaction.response.send_message(f"Couldn't DM {user.mention} — they may have DMs disabled.")

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
            title="Vyron V2 — Temporary Key (1 Hour)",
            description=f"```{key}```",
            color=0xFFAA00
        )
        embed.add_field(name="Expires", value=f"<t:{expiry}:R>", inline=False)
        embed.set_footer(text="Vyron.cc • This key expires in 1 hour")
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
    embed.set_author(name="Message from Vyron.cc")
    embed.set_footer(text="Vyron.cc")
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
    embed.set_footer(text="Vyron.cc")
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
        title="You have been blacklisted from Vyron.cc",
        description=f"**Reason:** {reason}",
        color=0xFF2222
    )
    embed.set_footer(text="Vyron.cc")
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
    embed.set_author(name="Vyron.cc Announcement")
    embed.set_footer(text="Vyron.cc")
    await channel.send(embed=embed)
    await interaction.response.send_message("Announcement sent.", ephemeral=True)

@tree.command(name="dmall", description="DM all members in the server")
@app_commands.describe(message="The message to send to everyone")
async def dmall(interaction: discord.Interaction, message: str):
    if not has_owner_role(interaction):
        return await deny(interaction)
    await interaction.response.defer(ephemeral=True)
    embed = discord.Embed(description=message, color=0x5080FF)
    embed.set_author(name="Message from Vyron.cc")
    embed.set_footer(text="Vyron.cc")
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

# ─────────────────────────────────────────────
#  NEW COMMANDS
# ─────────────────────────────────────────────

@tree.command(name="warn", description="Warn a user with a reason")
@app_commands.describe(user="The user to warn", reason="Reason for the warning")
async def warn(interaction: discord.Interaction, user: discord.Member, reason: str):
    if not has_owner_role(interaction):
        return await deny(interaction)
    data = load_data()
    uid = str(user.id)
    data.setdefault("warnings", {}).setdefault(uid, []).append({
        "reason": reason,
        "by": str(interaction.user.id),
        "at": int(time.time())
    })
    save_data(data)
    warn_count = len(data["warnings"][uid])
    # DM the user
    dm_embed = discord.Embed(
        title="⚠️ You have been warned",
        description=f"**Reason:** {reason}",
        color=0xFFAA00
    )
    dm_embed.add_field(name="Total Warnings", value=str(warn_count), inline=True)
    dm_embed.set_footer(text="Vyron.cc")
    try:
        await user.send(embed=dm_embed)
    except discord.Forbidden:
        pass
    # Public response
    pub_embed = discord.Embed(
        title="⚠️ Warning Issued",
        description=f"{user.mention} has been warned.",
        color=0xFFAA00
    )
    pub_embed.add_field(name="Reason", value=reason, inline=False)
    pub_embed.add_field(name="Warned by", value=interaction.user.mention, inline=True)
    pub_embed.add_field(name="Total Warnings", value=str(warn_count), inline=True)
    pub_embed.set_footer(text="Vyron.cc")
    await interaction.response.send_message(embed=pub_embed)


@tree.command(name="warnings", description="Check warnings for a user")
@app_commands.describe(user="The user to check warnings for")
async def warnings(interaction: discord.Interaction, user: discord.Member):
    if not has_owner_role(interaction):
        return await deny(interaction)
    data = load_data()
    uid = str(user.id)
    warns = data.get("warnings", {}).get(uid, [])
    if not warns:
        await interaction.response.send_message(f"{user.mention} has no warnings.", ephemeral=True)
        return
    embed = discord.Embed(title=f"⚠️ Warnings for {user.display_name}", color=0xFFAA00)
    for i, w in enumerate(warns, 1):
        by = interaction.guild.get_member(int(w["by"]))
        by_str = by.mention if by else f"<@{w['by']}>"
        embed.add_field(
            name=f"Warning #{i} — <t:{w['at']}:R>",
            value=f"**Reason:** {w['reason']}\n**By:** {by_str}",
            inline=False
        )
    embed.set_footer(text="Vyron.cc")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="clearwarnings", description="Clear all warnings for a user")
@app_commands.describe(user="The user to clear warnings for")
async def clearwarnings(interaction: discord.Interaction, user: discord.Member):
    if not has_owner_role(interaction):
        return await deny(interaction)
    data = load_data()
    uid = str(user.id)
    data.setdefault("warnings", {}).pop(uid, None)
    save_data(data)
    await interaction.response.send_message(f"✅ Cleared all warnings for {user.mention}.", ephemeral=True)


@tree.command(name="stats", description="Show bot and server stats")
async def stats(interaction: discord.Interaction):
    if not has_owner_role(interaction):
        return await deny(interaction)
    data = load_data()
    now = int(time.time())

    total_keys = sum(len(v) for v in data.get("keys", {}).values())
    total_temp = sum(
        sum(1 for t in tkeys if t["expiry"] > now)
        for tkeys in data.get("temp_keys", {}).values()
    )
    total_blacklisted = len(data.get("blacklist", {}))
    total_warnings = sum(len(v) for v in data.get("warnings", {}).values())
    total_members = interaction.guild.member_count
    total_active_giveaways = len(active_giveaways)

    # Count expired keys
    expired = 0
    key_expiry = data.get("key_expiry", {})
    for key, exp in key_expiry.items():
        if exp is not None and now > exp:
            expired += 1

    embed = discord.Embed(title="📊 Vyron.cc Stats", color=0x5080FF)
    embed.add_field(name="Server Members", value=str(total_members), inline=True)
    embed.add_field(name="Permanent Keys Issued", value=str(total_keys), inline=True)
    embed.add_field(name="Active Temp Keys", value=str(total_temp), inline=True)
    embed.add_field(name="Expired Keys", value=str(expired), inline=True)
    embed.add_field(name="Blacklisted Users", value=str(total_blacklisted), inline=True)
    embed.add_field(name="Total Warnings", value=str(total_warnings), inline=True)
    embed.add_field(name="Active Giveaways", value=str(total_active_giveaways), inline=True)
    embed.set_footer(text="Vyron.cc")
    await interaction.response.send_message(embed=embed)


@tree.command(name="slowmode", description="Set slowmode on a channel")
@app_commands.describe(
    seconds="Slowmode delay in seconds (0 to disable)",
    channel="Channel to apply slowmode to (defaults to current channel)"
)
async def slowmode(interaction: discord.Interaction, seconds: int, channel: discord.TextChannel = None):
    if not has_owner_role(interaction):
        return await deny(interaction)
    target = channel or interaction.channel
    if seconds < 0 or seconds > 21600:
        await interaction.response.send_message("❌ Slowmode must be between 0 and 21600 seconds.", ephemeral=True)
        return
    await target.edit(slowmode_delay=seconds)
    if seconds == 0:
        await interaction.response.send_message(f"✅ Slowmode disabled in {target.mention}.")
    else:
        await interaction.response.send_message(f"✅ Slowmode set to **{seconds}s** in {target.mention}.")


@tree.command(name="unblacklist", description="Remove a user from the blacklist")
@app_commands.describe(user="The user to unblacklist")
async def unblacklist(interaction: discord.Interaction, user: discord.Member):
    if not has_owner_role(interaction):
        return await deny(interaction)
    data = load_data()
    uid = str(user.id)
    if uid not in data.get("blacklist", {}):
        await interaction.response.send_message(f"{user.mention} is not blacklisted.", ephemeral=True)
        return
    del data["blacklist"][uid]
    save_data(data)
    embed = discord.Embed(
        title="✅ You have been unblacklisted from Vyron.cc",
        description="Your access has been restored.",
        color=0x00CC66
    )
    embed.set_footer(text="Vyron.cc")
    try:
        await user.send(embed=embed)
    except discord.Forbidden:
        pass
    await interaction.response.send_message(f"✅ {user.mention} has been unblacklisted.", ephemeral=True)


@tree.command(name="keyinfo", description="Show info about a specific key")
@app_commands.describe(key="The key to look up")
async def keyinfo(interaction: discord.Interaction, key: str):
    if not has_owner_role(interaction):
        return await deny(interaction)
    data = load_data()
    key = key.strip()

    # Find which user owns this key
    owner_uid = None
    for uid, keys in data.get("keys", {}).items():
        if key in keys:
            owner_uid = uid
            break

    if not owner_uid:
        await interaction.response.send_message("❌ Key not found.", ephemeral=True)
        return

    owner = interaction.guild.get_member(int(owner_uid))
    owner_str = owner.mention if owner else f"<@{owner_uid}>"

    now = int(time.time())
    expiry = data.get("key_expiry", {}).get(key)
    created = data.get("key_created", {}).get(key)
    hwid = data.get("key_hwid", {}).get(key, "Not bound yet")
    blacklisted = owner_uid in data.get("blacklist", {})

    if expiry is None:
        expiry_str = "Lifetime"
        status = "✅ Active"
    elif now > expiry:
        expiry_str = f"<t:{expiry}:R>"
        status = "❌ Expired"
    else:
        expiry_str = f"<t:{expiry}:R>"
        status = "✅ Active"

    if blacklisted:
        status = f"🚫 Owner Blacklisted"

    embed = discord.Embed(title="🔑 Key Info", description=f"```{key}```", color=0x5080FF)
    embed.add_field(name="Owner", value=owner_str, inline=True)
    embed.add_field(name="Status", value=status, inline=True)
    embed.add_field(name="Expires", value=expiry_str, inline=True)
    embed.add_field(name="Created", value=f"<t:{created}:R>" if created else "Unknown", inline=True)
    embed.add_field(name="HWID", value=f"`{hwid}`", inline=False)
    embed.set_footer(text="Vyron.cc")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="extendkey", description="Extend the expiry of a key")
@app_commands.describe(key="The key to extend", duration="Extra time to add: e.g. 7d, 1m, 1h")
async def extendkey(interaction: discord.Interaction, key: str, duration: str):
    if not has_owner_role(interaction):
        return await deny(interaction)
    secs = parse_duration(duration)
    if not secs:
        await interaction.response.send_message("❌ Invalid duration. Use e.g. `1h`, `7d`, `1m`. Lifetime not valid here.", ephemeral=True)
        return

    data = load_data()
    key = key.strip()

    # Check key exists
    found = any(key in keys for keys in data.get("keys", {}).values())
    if not found:
        await interaction.response.send_message("❌ Key not found.", ephemeral=True)
        return

    key_expiry = data.setdefault("key_expiry", {})
    now = int(time.time())
    current_expiry = key_expiry.get(key)

    if current_expiry is None:
        # Was lifetime — set expiry from now
        new_expiry = now + secs
    elif current_expiry < now:
        # Already expired — extend from now
        new_expiry = now + secs
    else:
        # Still active — add on top
        new_expiry = current_expiry + secs

    key_expiry[key] = new_expiry
    save_data(data)

    embed = discord.Embed(title="✅ Key Extended", description=f"```{key}```", color=0x00CC66)
    embed.add_field(name="Added", value=duration_label(secs), inline=True)
    embed.add_field(name="New Expiry", value=f"<t:{new_expiry}:R>", inline=True)
    embed.set_footer(text="Vyron.cc")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="revokekey", description="Revoke and delete a key from a user")
@app_commands.describe(key="The key to revoke")
async def revokekey(interaction: discord.Interaction, key: str):
    if not has_owner_role(interaction):
        return await deny(interaction)
    data = load_data()
    key = key.strip()

    owner_uid = None
    for uid, keys in data.get("keys", {}).items():
        if key in keys:
            owner_uid = uid
            keys.remove(key)
            break

    if not owner_uid:
        await interaction.response.send_message("❌ Key not found.", ephemeral=True)
        return

    # Clean up related data
    data.get("key_expiry", {}).pop(key, None)
    data.get("key_hwid", {}).pop(key, None)
    data.get("key_created", {}).pop(key, None)
    save_data(data)

    owner = interaction.guild.get_member(int(owner_uid))
    owner_str = owner.mention if owner else f"<@{owner_uid}>"

    # Notify the user
    dm_embed = discord.Embed(
        title="🔑 Key Revoked",
        description=f"Your Vyron V2 key has been revoked.",
        color=0xFF4444
    )
    dm_embed.add_field(name="Key", value=f"```{key}```", inline=False)
    dm_embed.set_footer(text="Vyron.cc")
    try:
        if owner:
            await owner.send(embed=dm_embed)
    except discord.Forbidden:
        pass

    await interaction.response.send_message(
        f"✅ Key `{key}` revoked from {owner_str}.",
        ephemeral=True
    )


# ─────────────────────────────────────────────
#  MODERATION COMMANDS
# ─────────────────────────────────────────────

@tree.command(name="kick", description="Kick a user from the server")
@app_commands.describe(user="The user to kick", reason="Reason for the kick")
async def kick(interaction: discord.Interaction, user: discord.Member, reason: str = "No reason provided"):
    if not has_owner_role(interaction):
        return await deny(interaction)
    if user.top_role >= interaction.guild.me.top_role:
        await interaction.response.send_message("❌ I can't kick that user — their role is too high.", ephemeral=True)
        return
    dm_embed = discord.Embed(
        title="👢 You have been kicked",
        description=f"You were kicked from **{interaction.guild.name}**.",
        color=0xFF6600
    )
    dm_embed.add_field(name="Reason", value=reason, inline=False)
    dm_embed.set_footer(text="Vyron.cc")
    try:
        await user.send(embed=dm_embed)
    except discord.Forbidden:
        pass
    await user.kick(reason=reason)
    pub_embed = discord.Embed(
        title="👢 User Kicked",
        description=f"{user.mention} has been kicked.",
        color=0xFF6600
    )
    pub_embed.add_field(name="Reason", value=reason, inline=False)
    pub_embed.add_field(name="Kicked by", value=interaction.user.mention, inline=True)
    pub_embed.set_footer(text="Vyron.cc")
    await interaction.response.send_message(embed=pub_embed)


@tree.command(name="ban", description="Ban a user from the server")
@app_commands.describe(user="The user to ban", reason="Reason for the ban", delete_days="Days of messages to delete (0-7)")
async def ban(interaction: discord.Interaction, user: discord.Member, reason: str = "No reason provided", delete_days: int = 0):
    if not has_owner_role(interaction):
        return await deny(interaction)
    if user.top_role >= interaction.guild.me.top_role:
        await interaction.response.send_message("❌ I can't ban that user — their role is too high.", ephemeral=True)
        return
    delete_days = max(0, min(7, delete_days))
    dm_embed = discord.Embed(
        title="🔨 You have been banned",
        description=f"You were banned from **{interaction.guild.name}**.",
        color=0xFF0000
    )
    dm_embed.add_field(name="Reason", value=reason, inline=False)
    dm_embed.set_footer(text="Vyron.cc")
    try:
        await user.send(embed=dm_embed)
    except discord.Forbidden:
        pass
    await user.ban(reason=reason, delete_message_days=delete_days)
    pub_embed = discord.Embed(
        title="🔨 User Banned",
        description=f"{user.mention} has been banned.",
        color=0xFF0000
    )
    pub_embed.add_field(name="Reason", value=reason, inline=False)
    pub_embed.add_field(name="Banned by", value=interaction.user.mention, inline=True)
    pub_embed.set_footer(text="Vyron.cc")
    await interaction.response.send_message(embed=pub_embed)


@tree.command(name="unban", description="Unban a user by their ID")
@app_commands.describe(user_id="The user ID to unban", reason="Reason for the unban")
async def unban(interaction: discord.Interaction, user_id: str, reason: str = "No reason provided"):
    if not has_owner_role(interaction):
        return await deny(interaction)
    try:
        uid = int(user_id)
    except ValueError:
        await interaction.response.send_message("❌ Invalid user ID.", ephemeral=True)
        return
    try:
        await interaction.guild.unban(discord.Object(id=uid), reason=reason)
        await interaction.response.send_message(f"✅ User `{user_id}` has been unbanned.", ephemeral=True)
    except discord.NotFound:
        await interaction.response.send_message("❌ That user is not banned.", ephemeral=True)


@tree.command(name="mute", description="Timeout (mute) a user for a duration")
@app_commands.describe(user="The user to mute", duration="Duration: e.g. 10m, 1h, 7d (max 28d)", reason="Reason for the mute")
async def mute(interaction: discord.Interaction, user: discord.Member, duration: str, reason: str = "No reason provided"):
    if not has_owner_role(interaction):
        return await deny(interaction)
    if user.top_role >= interaction.guild.me.top_role:
        await interaction.response.send_message("❌ I can't mute that user — their role is too high.", ephemeral=True)
        return
    secs = parse_duration(duration)
    if not secs:
        await interaction.response.send_message("❌ Invalid duration. Use e.g. `10m`, `1h`, `7d`. Max is 28 days.", ephemeral=True)
        return
    secs = min(secs, 28 * 86400)  # Discord max timeout is 28 days
    import datetime
    until = discord.utils.utcnow() + datetime.timedelta(seconds=secs)
    await user.timeout(until, reason=reason)
    dm_embed = discord.Embed(
        title="🔇 You have been muted",
        description=f"You were timed out in **{interaction.guild.name}**.",
        color=0xAAAAAA
    )
    dm_embed.add_field(name="Duration", value=duration_label(secs), inline=True)
    dm_embed.add_field(name="Reason", value=reason, inline=False)
    dm_embed.set_footer(text="Vyron.cc")
    try:
        await user.send(embed=dm_embed)
    except discord.Forbidden:
        pass
    pub_embed = discord.Embed(
        title="🔇 User Muted",
        description=f"{user.mention} has been timed out.",
        color=0xAAAAAA
    )
    pub_embed.add_field(name="Duration", value=duration_label(secs), inline=True)
    pub_embed.add_field(name="Expires", value=f"<t:{int(until.timestamp())}:R>", inline=True)
    pub_embed.add_field(name="Reason", value=reason, inline=False)
    pub_embed.add_field(name="Muted by", value=interaction.user.mention, inline=True)
    pub_embed.set_footer(text="Vyron.cc")
    await interaction.response.send_message(embed=pub_embed)


@tree.command(name="unmute", description="Remove a timeout from a user")
@app_commands.describe(user="The user to unmute")
async def unmute(interaction: discord.Interaction, user: discord.Member):
    if not has_owner_role(interaction):
        return await deny(interaction)
    await user.timeout(None)
    await interaction.response.send_message(f"✅ {user.mention} has been unmuted.")


@tree.command(name="lock", description="Lock a channel so only staff can send messages")
@app_commands.describe(channel="Channel to lock (defaults to current channel)", reason="Reason for locking")
async def lock(interaction: discord.Interaction, channel: discord.TextChannel = None, reason: str = "No reason provided"):
    if not has_owner_role(interaction):
        return await deny(interaction)
    target = channel or interaction.channel
    overwrite = target.overwrites_for(interaction.guild.default_role)
    overwrite.send_messages = False
    await target.set_permissions(interaction.guild.default_role, overwrite=overwrite, reason=reason)
    embed = discord.Embed(
        title="🔒 Channel Locked",
        description=f"{target.mention} has been locked.",
        color=0xFF4444
    )
    embed.add_field(name="Reason", value=reason, inline=False)
    embed.add_field(name="Locked by", value=interaction.user.mention, inline=True)
    embed.set_footer(text="Vyron.cc")
    await interaction.response.send_message(embed=embed)


@tree.command(name="unlock", description="Unlock a channel")
@app_commands.describe(channel="Channel to unlock (defaults to current channel)")
async def unlock(interaction: discord.Interaction, channel: discord.TextChannel = None):
    if not has_owner_role(interaction):
        return await deny(interaction)
    target = channel or interaction.channel
    overwrite = target.overwrites_for(interaction.guild.default_role)
    overwrite.send_messages = None  # Reset to default
    await target.set_permissions(interaction.guild.default_role, overwrite=overwrite)
    embed = discord.Embed(
        title="🔓 Channel Unlocked",
        description=f"{target.mention} has been unlocked.",
        color=0x00CC66
    )
    embed.add_field(name="Unlocked by", value=interaction.user.mention, inline=True)
    embed.set_footer(text="Vyron.cc")
    await interaction.response.send_message(embed=embed)


@tree.command(name="userinfo", description="Show detailed info about a user")
@app_commands.describe(user="The user to look up")
async def userinfo(interaction: discord.Interaction, user: discord.Member = None):
    if not has_owner_role(interaction):
        return await deny(interaction)
    user = user or interaction.user
    data = load_data()
    uid = str(user.id)
    now = int(time.time())

    perm_keys = data.get("keys", {}).get(uid, [])
    temp_keys = [t for t in data.get("temp_keys", {}).get(uid, []) if t["expiry"] > now]
    warns = data.get("warnings", {}).get(uid, [])
    blacklisted = uid in data.get("blacklist", {})
    blacklist_reason = data["blacklist"].get(uid, "") if blacklisted else ""

    roles = [r.mention for r in reversed(user.roles) if r.name != "@everyone"]
    roles_str = " ".join(roles) if roles else "None"

    status_str = "🚫 Blacklisted" if blacklisted else "✅ Active"

    embed = discord.Embed(
        title=f"👤 {user.display_name}",
        color=0xFF4444 if blacklisted else 0x5080FF
    )
    embed.set_thumbnail(url=user.display_avatar.url)
    embed.add_field(name="Username", value=str(user), inline=True)
    embed.add_field(name="ID", value=str(user.id), inline=True)
    embed.add_field(name="Status", value=status_str, inline=True)
    embed.add_field(name="Joined Server", value=f"<t:{int(user.joined_at.timestamp())}:R>", inline=True)
    embed.add_field(name="Account Created", value=f"<t:{int(user.created_at.timestamp())}:R>", inline=True)
    embed.add_field(name="Permanent Keys", value=str(len(perm_keys)), inline=True)
    embed.add_field(name="Active Temp Keys", value=str(len(temp_keys)), inline=True)
    embed.add_field(name="Warnings", value=str(len(warns)), inline=True)
    if blacklisted:
        embed.add_field(name="Blacklist Reason", value=blacklist_reason, inline=False)
    if roles:
        embed.add_field(name=f"Roles ({len(roles)})", value=roles_str[:1024], inline=False)
    embed.set_footer(text="Vyron.cc")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="wipekeys", description="Delete all keys for a user")
@app_commands.describe(user="The user whose keys to wipe")
async def wipekeys(interaction: discord.Interaction, user: discord.Member):
    if not has_owner_role(interaction):
        return await deny(interaction)
    data = load_data()
    uid = str(user.id)
    keys = data.get("keys", {}).get(uid, [])
    if not keys:
        await interaction.response.send_message(f"{user.mention} has no keys to wipe.", ephemeral=True)
        return
    # Clean up all related data for each key
    for key in keys:
        data.get("key_expiry", {}).pop(key, None)
        data.get("key_hwid", {}).pop(key, None)
        data.get("key_created", {}).pop(key, None)
    count = len(keys)
    data["keys"][uid] = []
    save_data(data)
    dm_embed = discord.Embed(
        title="🔑 Keys Wiped",
        description=f"All your Vyron V2 keys have been revoked.",
        color=0xFF4444
    )
    dm_embed.set_footer(text="Vyron.cc")
    try:
        await user.send(embed=dm_embed)
    except discord.Forbidden:
        pass
    await interaction.response.send_message(
        f"✅ Wiped **{count}** key(s) from {user.mention}.",
        ephemeral=True
    )


@tree.command(name="customkey", description="Create a fully custom key and assign it to a user")
@app_commands.describe(
    user="The user to assign the key to",
    key="The custom key string (e.g. Vyron-MyCustomKey123)",
    duration="Duration: e.g. 1h, 7d, 2w, 1m, lifetime"
)
async def customkey(interaction: discord.Interaction, user: discord.Member, key: str, duration: str = "lifetime"):
    if not has_owner_role(interaction):
        return await deny(interaction)

    key = key.strip()

    if not key:
        await interaction.response.send_message("❌ Key cannot be empty.", ephemeral=True)
        return

    if len(key) < 4:
        await interaction.response.send_message("❌ Key must be at least 4 characters.", ephemeral=True)
        return

    if len(key) > 64:
        await interaction.response.send_message("❌ Key must be 64 characters or less.", ephemeral=True)
        return

    secs = parse_duration(duration)
    if secs == 0:
        await interaction.response.send_message("❌ Invalid duration. Use e.g. `1h`, `7d`, `2w`, `1m`, `lifetime`.", ephemeral=True)
        return

    data = load_data()

    # Check if key already exists anywhere
    all_existing = set()
    for keys in data.get("keys", {}).values():
        all_existing.update(keys)
    for tkeys in data.get("temp_keys", {}).values():
        for t in tkeys:
            all_existing.add(t["key"])

    if key in all_existing:
        await interaction.response.send_message("❌ That key already exists. Choose a different one.", ephemeral=True)
        return

    uid = str(user.id)
    expiry = int(time.time()) + secs if secs else None
    data.setdefault("key_expiry", {})[key] = expiry
    data.setdefault("key_created", {})[key] = int(time.time())
    data["keys"].setdefault(uid, []).append(key)
    save_data(data)

    # DM the key to the user
    dm_embed = discord.Embed(title="🔑 Vyron V2 Key", description=f"```{key}```", color=0x5080FF)
    dm_embed.add_field(name="Duration", value=duration_label(secs), inline=True)
    if expiry:
        dm_embed.add_field(name="Expires", value=f"<t:{expiry}:R>", inline=True)
    dm_embed.set_footer(text="Vyron.cc")

    # Public response
    pub_embed = discord.Embed(
        title="🔑 Custom Key Created",
        description=f"{interaction.user.mention} created a custom key for {user.mention}.",
        color=0x5080FF
    )
    pub_embed.add_field(name="Duration", value=duration_label(secs), inline=True)
    if expiry:
        pub_embed.add_field(name="Expires", value=f"<t:{expiry}:R>", inline=True)
    pub_embed.set_footer(text="Vyron.cc")

    try:
        await user.send(embed=dm_embed)
        pub_embed.description += "\n✅ Key sent via DM."
    except discord.Forbidden:
        pub_embed.description += "\n⚠️ Couldn't DM the user — they may have DMs disabled."

    await interaction.response.send_message(embed=pub_embed)


# ─────────────────────────────────────────────
#  KEY PANEL
# ─────────────────────────────────────────────

class KeyPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🔑 Redeem Key", style=discord.ButtonStyle.success, custom_id="panel_redeem")
    async def redeem_key(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(RedeemKeyModal())

    @discord.ui.button(label="📋 Get Script", style=discord.ButtonStyle.primary, custom_id="panel_getscript")
    async def get_script(self, interaction: discord.Interaction, button: discord.ui.Button):
        data = load_data()
        uid = str(interaction.user.id)
        now = int(time.time())

        # Find a valid redeemed key for this user
        redeemed = data.get("redeemed_keys", {}).get(uid)
        if not redeemed:
            await interaction.response.send_message("❌ You haven't redeemed a key yet. Click **Redeem Key** first.", ephemeral=True)
            return

        key = redeemed["key"]
        # Check key is still valid
        expiry = data.get("key_expiry", {}).get(key)
        if expiry is not None and now > expiry:
            await interaction.response.send_message("❌ Your key has expired. Please contact staff.", ephemeral=True)
            return

        # Check not blacklisted
        if uid in data.get("blacklist", {}):
            await interaction.response.send_message("❌ You are blacklisted.", ephemeral=True)
            return

        embed = discord.Embed(
            title="📋 Your Script Key",
            description=f"Place this above your loader script:",
            color=0x5080FF
        )
        embed.add_field(name="Key", value=f"```{key}```", inline=False)
        if expiry:
            embed.add_field(name="Expires", value=f"<t:{expiry}:R>", inline=True)
        else:
            embed.add_field(name="Expires", value="Lifetime", inline=True)
        embed.set_footer(text="Vyron.cc • Keep this key private")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="⚙️ Reset HWID", style=discord.ButtonStyle.secondary, custom_id="panel_resethwid")
    async def reset_hwid(self, interaction: discord.Interaction, button: discord.ui.Button):
        data = load_data()
        uid = str(interaction.user.id)
        now = int(time.time())

        redeemed = data.get("redeemed_keys", {}).get(uid)
        if not redeemed:
            await interaction.response.send_message("❌ You haven't redeemed a key yet.", ephemeral=True)
            return

        key = redeemed["key"]

        # 24hr cooldown check
        cooldowns = data.setdefault("hwid_reset_cooldown", {})
        last_reset = cooldowns.get(uid, 0)
        cooldown_secs = 86400  # 24 hours
        if now - last_reset < cooldown_secs:
            next_reset = last_reset + cooldown_secs
            await interaction.response.send_message(
                f"❌ You can only reset your HWID once every 24 hours.\nNext reset available: <t:{next_reset}:R>",
                ephemeral=True
            )
            return

        # Do the reset
        key_hwid = data.setdefault("key_hwid", {})
        if key in key_hwid:
            del key_hwid[key]
        cooldowns[uid] = now
        save_data(data)

        await interaction.response.send_message(
            "✅ Your HWID has been reset. The next time you use your key it will bind to your new device.",
            ephemeral=True
        )

    @discord.ui.button(label="📊 Get Stats", style=discord.ButtonStyle.secondary, custom_id="panel_stats")
    async def get_stats(self, interaction: discord.Interaction, button: discord.ui.Button):
        data = load_data()
        uid = str(interaction.user.id)
        now = int(time.time())

        redeemed = data.get("redeemed_keys", {}).get(uid)
        if not redeemed:
            await interaction.response.send_message("❌ You haven't redeemed a key yet.", ephemeral=True)
            return

        key = redeemed["key"]
        expiry = data.get("key_expiry", {}).get(key)
        created = data.get("key_created", {}).get(key)
        hwid = data.get("key_hwid", {}).get(key, "Not bound yet")
        redeemed_at = redeemed.get("redeemed_at")

        if expiry is None:
            expiry_str = "Lifetime"
            status = "✅ Active"
        elif now > expiry:
            expiry_str = f"<t:{expiry}:R>"
            status = "❌ Expired"
        else:
            expiry_str = f"<t:{expiry}:R>"
            status = "✅ Active"

        # HWID reset cooldown
        last_reset = data.get("hwid_reset_cooldown", {}).get(uid, 0)
        if last_reset and now - last_reset < 86400:
            next_reset = last_reset + 86400
            hwid_reset_str = f"<t:{next_reset}:R>"
        else:
            hwid_reset_str = "Available now"

        embed = discord.Embed(title="📊 Your Key Stats", color=0x5080FF)
        embed.add_field(name="Key", value=f"```{key}```", inline=False)
        embed.add_field(name="Status", value=status, inline=True)
        embed.add_field(name="Expires", value=expiry_str, inline=True)
        embed.add_field(name="Redeemed", value=f"<t:{redeemed_at}:R>" if redeemed_at else "Unknown", inline=True)
        embed.add_field(name="HWID Bound", value="Yes" if hwid != "Not bound yet" else "No", inline=True)
        embed.add_field(name="Next HWID Reset", value=hwid_reset_str, inline=True)
        embed.set_footer(text="Vyron.cc")
        await interaction.response.send_message(embed=embed, ephemeral=True)


class RedeemKeyModal(discord.ui.Modal, title="Redeem Your Key"):
    key_input = discord.ui.TextInput(
        label="Enter your key",
        placeholder="Vyron-XXXXXXXXXXXXXXX",
        min_length=4,
        max_length=64,
        required=True
    )

    async def on_submit(self, interaction: discord.Interaction):
        key = self.key_input.value.strip()
        data = load_data()
        uid = str(interaction.user.id)
        now = int(time.time())

        # Collect all valid permanent keys
        all_perm_keys = set()
        for keys in data.get("keys", {}).values():
            all_perm_keys.update(keys)

        # Collect valid temp keys
        all_temp_keys = set()
        for tkeys in data.get("temp_keys", {}).values():
            for t in tkeys:
                if t["expiry"] > now:
                    all_temp_keys.add(t["key"])

        all_valid = all_perm_keys | all_temp_keys

        if key not in all_valid:
            await interaction.response.send_message("❌ Invalid or expired key.", ephemeral=True)
            return

        # Check expiry
        expiry = data.get("key_expiry", {}).get(key)
        if expiry is not None and now > expiry:
            await interaction.response.send_message("❌ That key has expired.", ephemeral=True)
            return

        # Check if key is already redeemed by someone else
        redeemed_keys = data.setdefault("redeemed_keys", {})
        for existing_uid, r in redeemed_keys.items():
            if r["key"] == key and existing_uid != uid:
                await interaction.response.send_message("❌ That key has already been redeemed by another user.", ephemeral=True)
                return

        # Check blacklist
        if uid in data.get("blacklist", {}):
            await interaction.response.send_message("❌ You are blacklisted.", ephemeral=True)
            return

        # Redeem it
        redeemed_keys[uid] = {
            "key": key,
            "redeemed_at": now
        }
        save_data(data)

        expiry_str = f"<t:{expiry}:R>" if expiry else "Lifetime"
        embed = discord.Embed(
            title="✅ Key Redeemed!",
            description="Your key has been successfully redeemed. Click **Get Script** to retrieve it anytime.",
            color=0x00CC66
        )
        embed.add_field(name="Key", value=f"```{key}```", inline=False)
        embed.add_field(name="Expires", value=expiry_str, inline=True)
        embed.set_footer(text="Vyron.cc")
        await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="addpanel", description="Post the Vyron Key Panel in this channel")
async def addpanel(interaction: discord.Interaction):
    if not has_owner_role(interaction):
        return await deny(interaction)

    embed = discord.Embed(
        title="Vyron Key Panel",
        description=(
            "If you're a buyer, click on the buttons below to redeem your key, "
            "get the script, or reset your HWID.\n\n"
            "Make sure to keep your `script_key` above the loader or it will not work."
        ),
        color=0x5080FF
    )
    embed.set_footer(text=f"Sent by {interaction.user} • Vyron.cc")

    view = KeyPanelView()
    await interaction.channel.send(embed=embed, view=view)
    await interaction.response.send_message("✅ Panel posted.", ephemeral=True)


# ─────────────────────────────────────────────
#  TICKET SYSTEM
# ─────────────────────────────────────────────

class TicketSelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(TicketSelect())


class TicketSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(
                label="Support",
                description="Get help with an issue",
                emoji="🎧",
                value="support"
            ),
            discord.SelectOption(
                label="Buy Another Key",
                description="Purchase an additional key",
                emoji="🛒",
                value="buy"
            ),
        ]
        super().__init__(
            placeholder="📩  Open a ticket — make a selection",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="ticket_select"
        )

    async def callback(self, interaction: discord.Interaction):
        guild = interaction.guild
        uid = str(interaction.user.id)
        ticket_type = self.values[0]
        data = load_data()

        # Check if user already has an open ticket
        open_tickets = data.setdefault("open_tickets", {})
        if uid in open_tickets:
            existing = guild.get_channel(open_tickets[uid])
            if existing:
                await interaction.response.send_message(
                    f"❌ You already have an open ticket: {existing.mention}",
                    ephemeral=True
                )
                return
            else:
                # Channel was deleted manually, clean up
                del open_tickets[uid]

        # Find the Owner role
        owner_role = discord.utils.get(guild.roles, name=OWNER_ROLE_NAME)

        # Build permission overwrites — private to user + Owner role only
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True
            ),
            guild.me: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                manage_channels=True,
                read_message_history=True
            ),
        }
        if owner_role:
            overwrites[owner_role] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True
            )

        # Create ticket channel
        label = "support" if ticket_type == "support" else "buy-key"
        channel_name = f"ticket-{label}-{interaction.user.name}".lower().replace(" ", "-")[:100]

        # Try to find or create a Tickets category
        category = discord.utils.get(guild.categories, name="Tickets")
        ticket_channel = await guild.create_text_channel(
            name=channel_name,
            overwrites=overwrites,
            category=category,
            reason=f"Ticket opened by {interaction.user}"
        )

        # Save open ticket
        open_tickets[uid] = ticket_channel.id
        save_data(data)

        # Build the welcome embed inside the ticket
        if ticket_type == "support":
            title = "🎧 Support Ticket"
            description = (
                f"Hey {interaction.user.mention}, thanks for reaching out!\n\n"
                "Please describe your issue in detail and a staff member will be with you shortly."
            )
            color = 0x5080FF
        else:
            title = "🛒 Purchase Ticket"
            description = (
                f"Hey {interaction.user.mention}, thanks for your interest!\n\n"
                "Let us know what you'd like to purchase and a staff member will assist you shortly."
            )
            color = 0x00CC66

        ticket_embed = discord.Embed(title=title, description=description, color=color)
        ticket_embed.add_field(name="Opened by", value=interaction.user.mention, inline=True)
        ticket_embed.add_field(name="Type", value="Support" if ticket_type == "support" else "Buy Another Key", inline=True)
        ticket_embed.set_footer(text="Vyron.cc • Click Close Ticket when done")

        close_view = CloseTicketView(uid)
        owner_ping = owner_role.mention if owner_role else ""
        await ticket_channel.send(
            content=f"{interaction.user.mention} {owner_ping}",
            embed=ticket_embed,
            view=close_view
        )

        await interaction.response.send_message(
            f"✅ Your ticket has been created: {ticket_channel.mention}",
            ephemeral=True
        )


class CloseTicketView(discord.ui.View):
    def __init__(self, owner_uid: str = None):
        super().__init__(timeout=None)
        self.owner_uid = owner_uid

    @discord.ui.button(label="🔒 Close Ticket", style=discord.ButtonStyle.danger, custom_id="ticket_close")
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        # Only the ticket owner or Owner role can close
        is_owner_role = has_owner_role(interaction)
        data = load_data()
        open_tickets = data.get("open_tickets", {})

        # Find who owns this ticket
        ticket_owner_uid = None
        for uid, cid in open_tickets.items():
            if cid == interaction.channel.id:
                ticket_owner_uid = uid
                break

        if not is_owner_role and str(interaction.user.id) != ticket_owner_uid:
            await interaction.response.send_message("❌ Only the ticket owner or staff can close this ticket.", ephemeral=True)
            return

        embed = discord.Embed(
            title="🔒 Ticket Closing",
            description="This ticket will be deleted in 5 seconds.",
            color=0xFF4444
        )
        embed.set_footer(text="Vyron.cc")
        await interaction.response.send_message(embed=embed)

        # Clean up open_tickets record
        if ticket_owner_uid and ticket_owner_uid in open_tickets:
            del open_tickets[ticket_owner_uid]
            save_data(data)

        await asyncio.sleep(5)
        await interaction.channel.delete(reason="Ticket closed")


@tree.command(name="addticketsys", description="Post the Vyron ticket system panel in this channel")
async def addticketsys(interaction: discord.Interaction):
    if not has_owner_role(interaction):
        return await deny(interaction)

    embed = discord.Embed(
        title="🎫 Vyron Support",
        description=(
            "Need help or want to purchase a key?\n\n"
            "Select an option from the dropdown below to open a ticket.\n"
            "Our staff will assist you as soon as possible."
        ),
        color=0x5080FF
    )
    embed.add_field(name="🎧 Support", value="Get help with an issue", inline=True)
    embed.add_field(name="🛒 Buy Another Key", value="Purchase an additional key", inline=True)
    embed.set_footer(text="Vyron.cc • Tickets are private between you and staff")

    view = TicketSelectView()
    await interaction.channel.send(embed=embed, view=view)
    await interaction.response.send_message("✅ Ticket panel posted.", ephemeral=True)


@tree.command(name="closeticket", description="Close the current ticket channel")
async def closeticket(interaction: discord.Interaction):
    if not has_owner_role(interaction):
        # Check if they own this ticket
        data = load_data()
        uid = str(interaction.user.id)
        open_tickets = data.get("open_tickets", {})
        if open_tickets.get(uid) != interaction.channel.id:
            await interaction.response.send_message("❌ This is not your ticket.", ephemeral=True)
            return

    data = load_data()
    open_tickets = data.get("open_tickets", {})
    ticket_owner_uid = None
    for uid, cid in open_tickets.items():
        if cid == interaction.channel.id:
            ticket_owner_uid = uid
            break

    embed = discord.Embed(
        title="🔒 Ticket Closing",
        description="This ticket will be deleted in 5 seconds.",
        color=0xFF4444
    )
    embed.set_footer(text="Vyron.cc")
    await interaction.response.send_message(embed=embed)

    if ticket_owner_uid and ticket_owner_uid in open_tickets:
        del open_tickets[ticket_owner_uid]
        save_data(data)

    await asyncio.sleep(5)
    await interaction.channel.delete(reason="Ticket closed")


@tree.command(name="checkexecutions", description="Check execution stats for all generated keys")
async def checkexecutions(interaction: discord.Interaction):
    if not has_owner_role(interaction):
        return await deny(interaction)

    data = load_data()
    now = int(time.time())

    executions = data.get("key_executions", {})
    last_exec = data.get("key_last_exec", {})
    all_keys = {}

    # Gather all permanent keys with their owner
    for uid, keys in data.get("keys", {}).items():
        for key in keys:
            all_keys[key] = uid

    if not all_keys:
        await interaction.response.send_message("No keys have been generated yet.", ephemeral=True)
        return

    total_generated = len(all_keys)
    total_executions = sum(executions.get(k, 0) for k in all_keys)
    keys_never_used = sum(1 for k in all_keys if executions.get(k, 0) == 0)
    keys_used = total_generated - keys_never_used

    # Build per-key breakdown — sort by execution count descending
    sorted_keys = sorted(all_keys.items(), key=lambda x: executions.get(x[0], 0), reverse=True)

    embed = discord.Embed(title="📊 Key Execution Stats", color=0x5080FF)
    embed.add_field(name="Total Keys Generated", value=str(total_generated), inline=True)
    embed.add_field(name="Total Executions", value=str(total_executions), inline=True)
    embed.add_field(name="Keys Ever Used", value=str(keys_used), inline=True)
    embed.add_field(name="Keys Never Used", value=str(keys_never_used), inline=True)

    # Show top 10 most executed keys
    lines = []
    for key, uid in sorted_keys[:10]:
        count = executions.get(key, 0)
        member = interaction.guild.get_member(int(uid))
        owner_str = member.display_name if member else f"<@{uid}>"
        expiry = data.get("key_expiry", {}).get(key)
        if expiry is None:
            expiry_str = "Lifetime"
        elif now > expiry:
            expiry_str = "Expired"
        else:
            # time left
            secs_left = expiry - now
            expiry_str = duration_label(secs_left)
        last = last_exec.get(key)
        last_str = f"<t:{last}:R>" if last else "Never"
        lines.append(f"`{key[:24]}{'...' if len(key) > 24 else ''}`\n👤 {owner_str} • 🔁 {count} exec • ⏳ {expiry_str} • Last: {last_str}")

    if lines:
        embed.add_field(
            name=f"Top Keys by Executions (showing {min(10, len(sorted_keys))}/{total_generated})",
            value="\n\n".join(lines),
            inline=False
        )

    embed.set_footer(text="Vyron.cc")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@client.event
async def on_ready():
    start_api_thread()
    # Re-register persistent views so buttons work after restart
    client.add_view(KeyPanelView())
    client.add_view(TicketSelectView())
    client.add_view(CloseTicketView())
    await tree.sync()
    print(f"Logged in as {client.user}")

client.run(TOKEN)
