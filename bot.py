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
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "")


async def send_webhook(content: str = "", embeds: list = None):
    """Fire-and-forget Discord webhook. Silently ignores any errors."""
    if not WEBHOOK_URL:
        return
    try:
        import aiohttp
        payload: dict = {}
        if content:
            payload["content"] = content
        if embeds:
            payload["embeds"] = embeds
        if not payload:
            return
        async with aiohttp.ClientSession() as session:
            await session.post(
                WEBHOOK_URL,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=5),
            )
    except Exception:
        pass

DATA_FILE = "data.json"

# In-memory giveaway stores: message_id -> giveaway dict
active_giveaways: dict[int, dict] = {}
ended_giveaways: dict[int, dict] = {}

def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    return {
        "keys": {},
        "keys_internal": {},
        "blacklist": {},
        "temp_keys": {},
        "temp_keys_internal": {},
    }

def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

def gen_key():
    chars = string.ascii_letters + string.digits
    return "Vyron-" + "".join(random.choices(chars, k=15))

def gen_ext_key():
    chars = string.ascii_letters + string.digits
    return "VyronExt-" + "".join(random.choices(chars, k=15))


def gen_int_key():
    chars = string.ascii_letters + string.digits
    return "VyronInt-" + "".join(random.choices(chars, k=15))

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

@tree.command(name="genv1key", description="Generate a Vyron V1 (Script) key for yourself")
@app_commands.describe(duration="Duration: e.g. 1h, 7d, 2w, 1m, lifetime", amount="Number of keys to generate (1-10)")
async def genv1key(interaction: discord.Interaction, duration: str = "lifetime", amount: int = 1):
    if not has_owner_role(interaction):
        return await deny(interaction)
    
    if amount < 1 or amount > 10:
        await interaction.response.send_message("Amount must be between 1 and 10.", ephemeral=True)
        return
    
    secs = parse_duration(duration)
    if secs == 0:
        await interaction.response.send_message("Invalid duration. Use e.g. `1h`, `7d`, `2w`, `1m`, `lifetime`.", ephemeral=True)
        return
    
    data = load_data()
    uid = str(interaction.user.id)
    expiry = int(time.time()) + secs if secs else None
    
    generated_keys = []
    for _ in range(amount):
        key = gen_key()
        data.setdefault("key_expiry", {})[key] = expiry
        data.setdefault("key_created", {})[key] = int(time.time())
        data.setdefault("key_generated_by", {})[key] = uid
        data["keys"].setdefault(uid, []).append(key)
        generated_keys.append(key)
    
    save_data(data)
    
    # Clean embed without emojis
    embed = discord.Embed(title=f"Vyron V1 Key{'s' if amount > 1 else ''}", color=0x5865F2)
    keys_text = "\n".join(f"```{k}```" for k in generated_keys)
    embed.description = keys_text
    embed.add_field(name="Duration", value=duration_label(secs), inline=True)
    embed.add_field(name="Count", value=str(amount), inline=True)
    if expiry:
        embed.add_field(name="Expires", value=f"<t:{expiry}:R>", inline=True)
    embed.set_footer(text="Vyron.cc")
    await interaction.response.send_message(embed=embed, ephemeral=True)
    
    # Webhook notification
    for key in generated_keys:
        asyncio.create_task(send_webhook(embeds=[{
            "title": "Key Generated (V1)",
            "color": 0x5865F2,
            "fields": [
                {"name": "Generated by", "value": f"{interaction.user.mention} ({interaction.user})", "inline": True},
                {"name": "For", "value": f"{interaction.user.mention}", "inline": True},
                {"name": "Duration", "value": duration_label(secs), "inline": True},
                {"name": "Key", "value": f"```{key}```", "inline": False},
            ],
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }]))


@tree.command(name="genv2key", description="Generate a Vyron V2 key for yourself")
@app_commands.describe(duration="Duration: e.g. 1h, 7d, 2w, 1m, lifetime", amount="Number of keys to generate (1-10)")
async def genv2key(interaction: discord.Interaction, duration: str = "lifetime", amount: int = 1):
    if not has_owner_role(interaction):
        return await deny(interaction)
    
    if amount < 1 or amount > 10:
        await interaction.response.send_message("Amount must be between 1 and 10.", ephemeral=True)
        return
    
    secs = parse_duration(duration)
    if secs == 0:
        await interaction.response.send_message("Invalid duration. Use e.g. `1h`, `7d`, `2w`, `1m`, `lifetime`.", ephemeral=True)
        return
    
    data = load_data()
    uid = str(interaction.user.id)
    expiry = int(time.time()) + secs if secs else None
    
    generated_keys = []
    for _ in range(amount):
        key = gen_key()
        data.setdefault("key_expiry", {})[key] = expiry
        data.setdefault("key_created", {})[key] = int(time.time())
        data.setdefault("key_generated_by", {})[key] = uid
        data["keys"].setdefault(uid, []).append(key)
        generated_keys.append(key)
    
    save_data(data)
    
    # Clean embed without emojis
    embed = discord.Embed(title=f"Vyron V2 Key{'s' if amount > 1 else ''}", color=0x5865F2)
    keys_text = "\n".join(f"```{k}```" for k in generated_keys)
    embed.description = keys_text
    embed.add_field(name="Duration", value=duration_label(secs), inline=True)
    embed.add_field(name="Count", value=str(amount), inline=True)
    if expiry:
        embed.add_field(name="Expires", value=f"<t:{expiry}:R>", inline=True)
    embed.set_footer(text="Vyron.cc")
    await interaction.response.send_message(embed=embed, ephemeral=True)
    
    # Webhook notification
    for key in generated_keys:
        asyncio.create_task(send_webhook(embeds=[{
            "title": "Key Generated (V2)",
            "color": 0x5865F2,
            "fields": [
                {"name": "Generated by", "value": f"{interaction.user.mention} ({interaction.user})", "inline": True},
                {"name": "For", "value": f"{interaction.user.mention}", "inline": True},
                {"name": "Duration", "value": duration_label(secs), "inline": True},
                {"name": "Key", "value": f"```{key}```", "inline": False},
            ],
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }]))

@tree.command(name="genv2keyto", description="Generate a Vyron V2 key and DM it to one or more users")
@app_commands.describe(
    users="Mention one or more users e.g. @user1 @user2 @user3",
    duration="Duration: e.g. 1h, 7d, 2w, 1m, lifetime",
    amount="Number of keys per user (1-10)"
)
async def genv2keyto(interaction: discord.Interaction, users: str, duration: str = "lifetime", amount: int = 1):
    if not has_owner_role(interaction):
        return await deny(interaction)

    if amount < 1 or amount > 10:
        await interaction.response.send_message("Amount must be between 1 and 10.", ephemeral=True)
        return

    secs = parse_duration(duration)
    if secs == 0:
        await interaction.response.send_message("Invalid duration. Use e.g. `1h`, `7d`, `2w`, `1m`, `lifetime`.", ephemeral=True)
        return

    # parse all mentioned user IDs from the string
    import re
    mentioned_ids = re.findall(r"<@!?(\d+)>", users)
    if not mentioned_ids:
        await interaction.response.send_message("No valid user mentions found. Use @username format.", ephemeral=True)
        return

    # resolve members
    members = []
    for uid_str in mentioned_ids:
        member = interaction.guild.get_member(int(uid_str))
        if member:
            members.append(member)

    if not members:
        await interaction.response.send_message("Could not resolve any members from those mentions.", ephemeral=True)
        return

    await interaction.response.defer()

    data = load_data()
    expiry = int(time.time()) + secs if secs else None
    now = int(time.time())

    results = []
    for member in members:
        uid = str(member.id)
        generated_keys = []
        for _ in range(amount):
            key = gen_key()
            data.setdefault("key_expiry", {})[key] = expiry
            data.setdefault("key_created", {})[key] = now
            data.setdefault("key_generated_by", {})[key] = str(interaction.user.id)
            data["keys"].setdefault(uid, []).append(key)
            generated_keys.append(key)
        results.append((member, generated_keys))

    save_data(data)

    # DM each user and collect status
    dm_results = []
    for member, generated_keys in results:
        keys_text = "\n".join(f"```{k}```" for k in generated_keys)
        dm_embed = discord.Embed(
            title=f"Vyron V2 Key{'s' if amount > 1 else ''}",
            description=keys_text,
            color=0x5865F2
        )
        dm_embed.add_field(name="Duration", value=duration_label(secs), inline=True)
        dm_embed.add_field(name="Count", value=str(amount), inline=True)
        if expiry:
            dm_embed.add_field(name="Expires", value=f"<t:{expiry}:R>", inline=True)
        dm_embed.set_footer(text="Vyron.cc")
        try:
            await member.send(embed=dm_embed)
            dm_results.append(f"{member.mention} - Success")
        except discord.Forbidden:
            dm_results.append(f"{member.mention} - DMs closed")

        for key in generated_keys:
            asyncio.create_task(send_webhook(embeds=[{
                "title": "Key Generated (V2)",
                "color": 0x5865F2,
                "fields": [
                    {"name": "Generated by", "value": f"{interaction.user.mention} ({interaction.user})", "inline": True},
                    {"name": "For", "value": f"{member.mention} ({member})", "inline": True},
                    {"name": "Duration", "value": duration_label(secs), "inline": True},
                    {"name": "Key", "value": f"```{key}```", "inline": False},
                ],
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }]))

    total_keys = len(members) * amount
    # Clean public response - no keys shown
    pub_embed = discord.Embed(
        title=f"Keys Generated",
        description=f"Generated {amount} key{'s' if amount > 1 else ''} for {len(members)} user{'s' if len(members) > 1 else ''}",
        color=0x57F287
    )
    pub_embed.add_field(name="Generated by", value=interaction.user.mention, inline=True)
    pub_embed.add_field(name="Duration", value=duration_label(secs), inline=True)
    pub_embed.add_field(name="Total keys", value=str(total_keys), inline=True)
    pub_embed.add_field(name="Delivery Status", value="\n".join(dm_results), inline=False)
    pub_embed.set_footer(text="Vyron.cc • Keys sent via DM")

    await interaction.followup.send(embed=pub_embed)


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
    keys = data.get("keys", {}).get(uid, [])
    int_keys = data.get("keys_internal", {}).get(uid, [])
    temp_keys = [t for t in data.get("temp_keys", {}).get(uid, []) if t["expiry"] > int(time.time())]
    blacklisted = uid in data["blacklist"]
    if not keys and not int_keys and not temp_keys:
        await interaction.response.send_message(f"No keys found for {user.mention}.", ephemeral=True)
        return
    status = f"🚫 Blacklisted: {data['blacklist'][uid]}" if blacklisted else "✅ Active"
    embed = discord.Embed(title=f"Keys for {user.display_name}", color=0xFF4444 if blacklisted else 0x5080FF)
    embed.add_field(name="Status", value=status, inline=False)
    if keys:
        embed.add_field(name=f"Script Keys ({len(keys)})", value="```" + "\n".join(f"• {k}" for k in keys) + "```", inline=False)
    if int_keys:
        embed.add_field(name=f"Internal Keys ({len(int_keys)})", value="```" + "\n".join(f"• {k}" for k in int_keys) + "```", inline=False)
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
    asyncio.create_task(send_webhook(embeds=[{
        "title": "🚫 User Blacklisted",
        "color": 0xFF2222,
        "fields": [
            {"name": "User", "value": f"{user.mention} ({user})", "inline": True},
            {"name": "Blacklisted by", "value": f"{interaction.user.mention} ({interaction.user})", "inline": True},
            {"name": "Reason", "value": reason, "inline": False},
        ],
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }]))

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
    total_keys_int = sum(len(v) for v in data.get("keys_internal", {}).values())
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
    embed.add_field(name="Script Keys Issued", value=str(total_keys), inline=True)
    embed.add_field(name="Internal Keys Issued", value=str(total_keys_int), inline=True)
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
        for uid, keys in data.get("keys_internal", {}).items():
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
        found = any(key in keys for keys in data.get("keys_internal", {}).values())
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
        ki = data.setdefault("keys_internal", {})
        for uid, keys in list(ki.items()):
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


@tree.command(name="renamekey", description="Rename an existing key to a new value")
@app_commands.describe(
    old_key="The current key to rename",
    new_key="The new key value"
)
async def renamekey(interaction: discord.Interaction, old_key: str, new_key: str):
    if not has_owner_role(interaction):
        return await deny(interaction)

    old_key = old_key.strip()
    new_key = new_key.strip()

    if not new_key or len(new_key) < 4:
        await interaction.response.send_message("❌ New key must be at least 4 characters.", ephemeral=True)
        return
    if len(new_key) > 64:
        await interaction.response.send_message("❌ New key must be 64 characters or less.", ephemeral=True)
        return

    data = load_data()

    # Check new key doesn't already exist
    all_existing = set()
    for keys in data.get("keys", {}).values():
        all_existing.update(keys)
    for keys in data.get("keys_internal", {}).values():
        all_existing.update(keys)
    if new_key in all_existing:
        await interaction.response.send_message("❌ That new key already exists.", ephemeral=True)
        return

    # Find and replace in keys
    owner_uid = None
    pool = None
    for uid, keys in data.get("keys", {}).items():
        if old_key in keys:
            owner_uid = uid
            pool = "keys"
            keys.remove(old_key)
            keys.append(new_key)
            break
    if not owner_uid:
        for uid, keys in data.get("keys_internal", {}).items():
            if old_key in keys:
                owner_uid = uid
                pool = "keys_internal"
                keys.remove(old_key)
                keys.append(new_key)
                break

    if not owner_uid:
        await interaction.response.send_message("❌ Key not found.", ephemeral=True)
        return

    # Migrate all metadata from old key to new key
    for field in ("key_expiry", "key_hwid", "key_created", "key_generated_by",
                  "key_executions", "key_last_exec", "key_roblox_info"):
        d = data.get(field, {})
        if old_key in d:
            d[new_key] = d.pop(old_key)

    save_data(data)

    owner = interaction.guild.get_member(int(owner_uid))
    owner_str = owner.mention if owner else f"<@{owner_uid}>"

    # DM the owner
    try:
        if owner:
            dm = discord.Embed(title="🔑 Key Renamed", color=0xFF9900)
            dm.add_field(name="Old Key", value=f"```{old_key}```", inline=False)
            dm.add_field(name="New Key", value=f"```{new_key}```", inline=False)
            dm.set_footer(text="Vyron.cc")
            await owner.send(embed=dm)
    except discord.Forbidden:
        pass

    embed = discord.Embed(title="🔑 Key Renamed", color=0xFF9900)
    embed.add_field(name="Old Key", value=f"```{old_key}```", inline=False)
    embed.add_field(name="New Key", value=f"```{new_key}```", inline=False)
    embed.add_field(name="Owner", value=owner_str, inline=True)
    embed.set_footer(text="Vyron.cc")
    await interaction.response.send_message(embed=embed, ephemeral=True)
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
    asyncio.create_task(send_webhook(embeds=[{
        "title": "👢 User Kicked",
        "color": 0xFF6600,
        "fields": [
            {"name": "User", "value": f"{user.mention} ({user})", "inline": True},
            {"name": "Kicked by", "value": f"{interaction.user.mention} ({interaction.user})", "inline": True},
            {"name": "Reason", "value": reason, "inline": False},
        ],
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }]))


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
    int_keys_ct = len(data.get("keys_internal", {}).get(uid, []))
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
    embed.add_field(name="Script Keys", value=str(len(perm_keys)), inline=True)
    embed.add_field(name="Internal Keys", value=str(int_keys_ct), inline=True)
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
    keys = list(data.get("keys", {}).get(uid, []))
    int_keys = list(data.get("keys_internal", {}).get(uid, []))
    if not keys and not int_keys:
        await interaction.response.send_message(f"{user.mention} has no keys to wipe.", ephemeral=True)
        return
    # Clean up all related data for each key
    for key in keys + int_keys:
        data.get("key_expiry", {}).pop(key, None)
        data.get("key_hwid", {}).pop(key, None)
        data.get("key_created", {}).pop(key, None)
    count = len(keys) + len(int_keys)
    data.setdefault("keys", {})[uid] = []
    data.setdefault("keys_internal", {})[uid] = []
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
    duration="Duration: e.g. 1h, 7d, 2w, 1m, lifetime",
    amount="Number of keys to generate with this prefix (1-10, appends _1 _2 etc if >1)"
)
async def customkey(interaction: discord.Interaction, user: discord.Member, key: str, duration: str = "lifetime", amount: int = 1):
    if not has_owner_role(interaction):
        return await deny(interaction)

    key = key.strip()
    amount = max(1, min(10, amount))

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
    all_existing = set()
    for keys in data.get("keys", {}).values():
        all_existing.update(keys)
    for keys in data.get("keys_internal", {}).values():
        all_existing.update(keys)
    for tkeys in data.get("temp_keys", {}).values():
        for t in tkeys:
            all_existing.add(t["key"])

    # Build list of keys to create
    keys_to_create = []
    if amount == 1:
        if key in all_existing:
            await interaction.response.send_message("❌ That key already exists. Choose a different one.", ephemeral=True)
            return
        keys_to_create.append(key)
    else:
        for i in range(1, amount + 1):
            candidate = f"{key}_{i}"
            if candidate in all_existing:
                await interaction.response.send_message(f"❌ Key `{candidate}` already exists. Choose a different prefix.", ephemeral=True)
                return
            keys_to_create.append(candidate)

    uid = str(user.id)
    expiry = int(time.time()) + secs if secs else None
    for k in keys_to_create:
        data.setdefault("key_expiry", {})[k] = expiry
        data.setdefault("key_created", {})[k] = int(time.time())
        data.setdefault("key_generated_by", {})[k] = str(interaction.user.id)
        data["keys"].setdefault(uid, []).append(k)
    save_data(data)

    keys_text = "\n".join(f"```{k}```" for k in keys_to_create)
    dm_embed = discord.Embed(title=f"🔑 Vyron V2 Key{'s' if amount > 1 else ''}", description=keys_text, color=0x5080FF)
    dm_embed.add_field(name="Duration", value=duration_label(secs), inline=True)
    dm_embed.add_field(name="Count", value=str(amount), inline=True)
    if expiry:
        dm_embed.add_field(name="Expires", value=f"<t:{expiry}:R>", inline=True)
    dm_embed.set_footer(text="Vyron.cc")

    pub_embed = discord.Embed(
        title=f"🔑 Custom Key{'s' if amount > 1 else ''} Created",
        description=f"{interaction.user.mention} created {amount} custom key{'s' if amount > 1 else ''} for {user.mention}.",
        color=0x5080FF
    )
    pub_embed.add_field(name="Duration", value=duration_label(secs), inline=True)
    pub_embed.add_field(name="Count", value=str(amount), inline=True)
    if expiry:
        pub_embed.add_field(name="Expires", value=f"<t:{expiry}:R>", inline=True)
    pub_embed.set_footer(text="Vyron.cc")

    try:
        await user.send(embed=dm_embed)
        pub_embed.description += "\n✅ Key(s) sent via DM."
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

        # Simple script code
        script_code = f"shared.VyronNew = {{\n    ['Key'] = '{key}',\n}}\n\nloadstring(game:HttpGet('https://bypass-production-954a.up.railway.app/source'))()"

        embed = discord.Embed(
            title="🎯 Vyron Script",
            description="Copy and paste this into your executor:",
            color=0x5080FF
        )
        
        embed.add_field(
            name="📋 Script Code",
            value=f"```lua\n{script_code}```",
            inline=False
        )
        
        embed.set_footer(text="Vyron.cc")
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
        for keys in data.get("keys_internal", {}).values():
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
        title="Vyron Script Panel",
        description=(
            "Welcome to Vyron. Use the buttons below to manage your key and access the script.\n\n"
            "**Available Actions:**\n"
            "• Redeem your key and bind it to your device\n"
            "• Download the latest script version\n"
            "• Reset your HWID if you need to switch devices\n"
            "• Check your key status and expiration\n"
            "• View your execution history\n\n"
            "**Important:** Keep your key above the loader in your script."
        ),
        color=0x5865F2
    )
    embed.set_footer(text=f"Vyron.cc • Panel created by {interaction.user}")

    view = ImprovedKeyPanelView()
    await interaction.channel.send(embed=embed, view=view)
    await interaction.response.send_message("Panel posted successfully.", ephemeral=True)


class ImprovedKeyPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Redeem Key", style=discord.ButtonStyle.success, custom_id="panel_redeem", row=0)
    async def redeem_key(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(RedeemKeyModal())

    @discord.ui.button(label="Get Script", style=discord.ButtonStyle.primary, custom_id="panel_script", row=0)
    async def get_script(self, interaction: discord.Interaction, button: discord.ui.Button):
        data = load_data()
        uid = str(interaction.user.id)
        
        # Find user's key
        user_key = None
        
        # Check permanent keys
        if uid in data.get("keys", {}):
            keys = data["keys"][uid]
            if keys:
                user_key = keys[0]  # Get first key
        
        # Check internal keys if no external key found
        if not user_key and uid in data.get("keys_internal", {}):
            keys = data["keys_internal"][uid]
            if keys:
                user_key = keys[0]  # Get first key
        
        # Check temp keys if no permanent key found
        if not user_key:
            for temp_keys in [data.get("temp_keys", {}), data.get("temp_keys_internal", {})]:
                if uid in temp_keys:
                    for temp_key in temp_keys[uid]:
                        if temp_key.get("expiry", 0) > int(time.time()):
                            user_key = temp_key["key"]
                            break
                    if user_key:
                        break
        
        if not user_key:
            embed = discord.Embed(
                title="❌ No Key Found",
                description="You don't have any active keys. Please redeem a key first.",
                color=0xFF5555
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return
        
        # Simple script code
        script_code = f"shared.VyronNew = {{\n    ['Key'] = '{user_key}',\n}}\n\nloadstring(game:HttpGet('https://bypass-production-954a.up.railway.app/source'))()"
        
        embed = discord.Embed(
            title="🎯 Vyron Script",
            description="Copy and paste this into your executor:",
            color=0x5865F2
        )
        
        embed.add_field(
            name="📋 Script Code",
            value=f"```lua\n{script_code}```",
            inline=False
        )
        
        embed.set_footer(text="Vyron.cc")
        
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="Reset HWID", style=discord.ButtonStyle.danger, custom_id="panel_hwid", row=0)
    async def reset_hwid(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ResetHWIDModal())

    @discord.ui.button(label="Check Key Status", style=discord.ButtonStyle.secondary, custom_id="panel_status", row=1)
    async def check_status(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(CheckKeyStatusModal())

    @discord.ui.button(label="Execution History", style=discord.ButtonStyle.secondary, custom_id="panel_history", row=1)
    async def exec_history(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ExecutionHistoryModal())


class RedeemKeyModal(discord.ui.Modal, title="Redeem Your Key"):
    key_input = discord.ui.TextInput(
        label="Enter your key",
        placeholder="Vyron-XXXXXXXXXXXXXXX",
        required=True,
        max_length=64
    )

    async def on_submit(self, interaction: discord.Interaction):
        key = self.key_input.value.strip()
        data = load_data()
        
        # Check if key exists
        key_found = False
        for uid, keys in list(data.get("keys", {}).items()) + list(data.get("keys_internal", {}).items()):
            if key in keys:
                key_found = True
                break
        
        if not key_found:
            await interaction.response.send_message("Invalid key. Please check and try again.", ephemeral=True)
            return
        
        # Check if blacklisted
        uid = str(interaction.user.id)
        if uid in data.get("blacklist", {}):
            reason = data["blacklist"][uid]
            await interaction.response.send_message(f"Your account is blacklisted. Reason: {reason}", ephemeral=True)
            return
        
        # Check expiry
        key_expiry = data.get("key_expiry", {}).get(key)
        if key_expiry and int(time.time()) > key_expiry:
            await interaction.response.send_message("This key has expired.", ephemeral=True)
            return
        
        # Success
        expiry_str = f"<t:{key_expiry}:R>" if key_expiry else "Lifetime"
        embed = discord.Embed(
            title="Key Redeemed Successfully",
            description=f"Your key is valid and ready to use.",
            color=0x57F287
        )
        embed.add_field(name="Key", value=f"```{key}```", inline=False)
        embed.add_field(name="Expires", value=expiry_str, inline=True)
        embed.add_field(name="Status", value="Active", inline=True)
        embed.set_footer(text="Vyron.cc")
        await interaction.response.send_message(embed=embed, ephemeral=True)


class ResetHWIDModal(discord.ui.Modal, title="Reset HWID"):
    key_input = discord.ui.TextInput(
        label="Enter your key",
        placeholder="Vyron-XXXXXXXXXXXXXXX",
        required=True,
        max_length=64
    )

    async def on_submit(self, interaction: discord.Interaction):
        key = self.key_input.value.strip()
        data = load_data()
        
        key_hwid = data.get("key_hwid", {})
        if key not in key_hwid:
            await interaction.response.send_message("No HWID bound to this key.", ephemeral=True)
            return
        
        del key_hwid[key]
        data["key_hwid"] = key_hwid
        save_data(data)
        
        embed = discord.Embed(
            title="HWID Reset Successfully",
            description="Your HWID has been reset. Next execution will bind a new device.",
            color=0x57F287
        )
        embed.set_footer(text="Vyron.cc")
        await interaction.response.send_message(embed=embed, ephemeral=True)


class CheckKeyStatusModal(discord.ui.Modal, title="Check Key Status"):
    key_input = discord.ui.TextInput(
        label="Enter your key",
        placeholder="Vyron-XXXXXXXXXXXXXXX",
        required=True,
        max_length=64
    )

    async def on_submit(self, interaction: discord.Interaction):
        key = self.key_input.value.strip()
        data = load_data()
        
        # Check if key exists
        key_found = False
        owner_uid = None
        for uid, keys in list(data.get("keys", {}).items()) + list(data.get("keys_internal", {}).items()):
            if key in keys:
                key_found = True
                owner_uid = uid
                break
        
        if not key_found:
            await interaction.response.send_message("Key not found.", ephemeral=True)
            return
        
        # Get key info
        key_expiry = data.get("key_expiry", {}).get(key)
        key_created = data.get("key_created", {}).get(key)
        key_executions = data.get("key_executions", {}).get(key, 0)
        key_last_exec = data.get("key_last_exec", {}).get(key)
        key_hwid = data.get("key_hwid", {}).get(key, "Not bound")
        
        now = int(time.time())
        if key_expiry:
            if now > key_expiry:
                status = "Expired"
                expiry_str = f"<t:{key_expiry}:R>"
            else:
                status = "Active"
                expiry_str = f"<t:{key_expiry}:R>"
        else:
            status = "Active (Lifetime)"
            expiry_str = "Never"
        
        embed = discord.Embed(
            title="Key Status",
            color=0x5865F2 if status.startswith("Active") else 0xED4245
        )
        embed.add_field(name="Status", value=status, inline=True)
        embed.add_field(name="Expires", value=expiry_str, inline=True)
        embed.add_field(name="Executions", value=str(key_executions), inline=True)
        
        if key_last_exec:
            embed.add_field(name="Last Used", value=f"<t:{key_last_exec}:R>", inline=True)
        
        if key_created:
            embed.add_field(name="Created", value=f"<t:{key_created}:R>", inline=True)
        
        embed.add_field(name="HWID", value=f"```{key_hwid[:16]}...```" if len(str(key_hwid)) > 16 else f"```{key_hwid}```", inline=False)
        embed.set_footer(text="Vyron.cc")
        await interaction.response.send_message(embed=embed, ephemeral=True)


class ExecutionHistoryModal(discord.ui.Modal, title="Execution History"):
    key_input = discord.ui.TextInput(
        label="Enter your key",
        placeholder="Vyron-XXXXXXXXXXXXXXX",
        required=True,
        max_length=64
    )

    async def on_submit(self, interaction: discord.Interaction):
        key = self.key_input.value.strip()
        data = load_data()
        
        key_executions = data.get("key_executions", {}).get(key, 0)
        key_last_exec = data.get("key_last_exec", {}).get(key)
        key_created = data.get("key_created", {}).get(key)
        
        if key_executions == 0:
            await interaction.response.send_message("No execution history for this key.", ephemeral=True)
            return
        
        embed = discord.Embed(
            title="Execution History",
            description=f"Total executions: **{key_executions}**",
            color=0x5865F2
        )
        
        if key_created:
            embed.add_field(name="Key Created", value=f"<t:{key_created}:F>", inline=False)
        
        if key_last_exec:
            embed.add_field(name="Last Execution", value=f"<t:{key_last_exec}:F>", inline=False)
            
            # Calculate average executions per day
            if key_created:
                days = max(1, (int(time.time()) - key_created) // 86400)
                avg = key_executions / days
                embed.add_field(name="Average per Day", value=f"{avg:.1f}", inline=True)
        
        embed.set_footer(text="Vyron.cc")
        await interaction.response.send_message(embed=embed, ephemeral=True)


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
    for uid, keys in data.get("keys_internal", {}).items():
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


# ─────────────────────────────────────────────
#  KEY EXPIRY DM SYSTEM
# ─────────────────────────────────────────────

async def expiry_check_loop():
    """Runs every hour. DMs users whose keys have expired if dm_check is on."""
    await client.wait_until_ready()
    while not client.is_closed():
        try:
            data = load_data()
            # Only run if dm_check is enabled (default on)
            if data.get("dm_check_enabled", True):
                now = int(time.time())
                key_expiry = data.get("key_expiry", {})
                notified = data.setdefault("expiry_notified", set() if False else [])
                notified_set = set(notified)
                changed = False

                for uid, keys in list(data.get("keys", {}).items()) + list(data.get("keys_internal", {}).items()):
                    for key in keys:
                        expiry = key_expiry.get(key)
                        if expiry is None:
                            continue  # lifetime key
                        if now >= expiry and key not in notified_set:
                            # Key just expired — DM the user
                            # Find the user in any guild
                            user = None
                            for guild in client.guilds:
                                member = guild.get_member(int(uid))
                                if member:
                                    user = member
                                    break
                            if user:
                                try:
                                    embed = discord.Embed(
                                        title="⏰ Your Key Has Expired",
                                        description=(
                                            "Your **Vyron V2** key has expired!\n\n"
                                            "Buy a new one to get back in action. 🔑"
                                        ),
                                        color=0xFF4444
                                    )
                                    embed.add_field(name="Expired Key", value=f"```{key}```", inline=False)
                                    embed.set_footer(text="Vyron.cc")
                                    await user.send(embed=embed)
                                except (discord.Forbidden, discord.HTTPException):
                                    pass
                            notified_set.add(key)
                            changed = True

                if changed:
                    data["expiry_notified"] = list(notified_set)
                    save_data(data)

        except Exception as e:
            print(f"[expiry_check_loop error] {e}")

        await asyncio.sleep(3600)  # check every hour


@tree.command(name="dmcheck", description="Toggle expired key DM notifications on or off")
@app_commands.describe(state="on or off")
@app_commands.choices(state=[
    app_commands.Choice(name="on", value="on"),
    app_commands.Choice(name="off", value="off"),
])
async def dmcheck(interaction: discord.Interaction, state: str):
    if not has_owner_role(interaction):
        return await deny(interaction)
    data = load_data()
    enabled = state == "on"
    data["dm_check_enabled"] = enabled
    save_data(data)
    status = "✅ **ON** — users will be DM'd when their key expires." if enabled else "🔕 **OFF** — expiry DMs are disabled."
    embed = discord.Embed(
        title="⏰ Expiry DM Notifications",
        description=status,
        color=0x00CC66 if enabled else 0xAAAAAA
    )
    embed.set_footer(text="Vyron.cc")
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ─────────────────────────────────────────────
#  ACTIVE SESSIONS
# ─────────────────────────────────────────────

import urllib.request
import urllib.parse

API_BASE = os.environ.get("API_BASE", "http://localhost:8080")

class KickModal(discord.ui.Modal, title="Kick User"):
    reason_input = discord.ui.TextInput(
        label="Reason",
        placeholder="Enter kick reason...",
        required=True,
        max_length=200
    )

    def __init__(self, key: str):
        super().__init__()
        self.key = key

    async def on_submit(self, interaction: discord.Interaction):
        reason = self.reason_input.value.strip()
        api_secret = os.environ.get("API_SECRET", "vyron_secret")

        try:
            payload = json.dumps({
                "key": self.key,
                "reason": reason,
                "secret": api_secret
            }).encode("utf-8")
            req = urllib.request.Request(
                f"{API_BASE}/kick",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                result = json.loads(resp.read())

            if result.get("success"):
                await interaction.response.send_message(
                    f"✅ Kick queued for key `{self.key[:20]}...`\nReason: **{reason}**\nThey will be kicked within 5 seconds.",
                    ephemeral=True
                )
            else:
                await interaction.response.send_message(
                    f"❌ Failed to kick: {result.get('reason', 'Unknown error')}",
                    ephemeral=True
                )
        except Exception as e:
            await interaction.response.send_message(
                f"❌ Error contacting API: `{e}`",
                ephemeral=True
            )


class SessionActionModal(discord.ui.Modal):
    """Modal for kick/notify actions on a session"""
    def __init__(self, action_type: str, key: str, title: str):
        super().__init__(title=title)
        self.action_type = action_type
        self.key = key
        
        if action_type == "kick":
            self.message_input = discord.ui.TextInput(
                label="Kick Reason",
                placeholder="Enter reason for kick...",
                required=True,
                max_length=200,
                style=discord.TextStyle.paragraph
            )
        else:  # notify
            self.message_input = discord.ui.TextInput(
                label="Notification Message",
                placeholder="Enter message to send...",
                required=True,
                max_length=300,
                style=discord.TextStyle.paragraph
            )
        
        self.add_item(self.message_input)
    
    async def on_submit(self, interaction: discord.Interaction):
        message = self.message_input.value.strip()
        api_secret = os.environ.get("API_SECRET", "vyron_secret")
        
        try:
            if self.action_type == "kick":
                payload = json.dumps({
                    "key": self.key,
                    "reason": message,
                    "secret": api_secret
                }).encode("utf-8")
                endpoint = f"{API_BASE}/kick"
            else:  # notify
                payload = json.dumps({
                    "key": self.key,
                    "message": message,
                    "secret": api_secret
                }).encode("utf-8")
                endpoint = f"{API_BASE}/notify"
            
            req = urllib.request.Request(
                endpoint,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                result = json.loads(resp.read())
            
            if result.get("success"):
                action_name = "Kick" if self.action_type == "kick" else "Notification"
                await interaction.response.send_message(
                    f"✅ {action_name} queued for key `{self.key[:20]}...`\n"
                    f"Message: **{message}**\n"
                    f"Will be delivered within 5 seconds.",
                    ephemeral=True
                )
            else:
                await interaction.response.send_message(
                    f"❌ Failed: {result.get('reason', 'Unknown error')}",
                    ephemeral=True
                )
        except Exception as e:
            await interaction.response.send_message(
                f"❌ Error contacting API: `{e}`",
                ephemeral=True
            )


class SessionActionSelect(discord.ui.Select):
    """Dropdown to select a session and action"""
    def __init__(self, sessions: list):
        self.sessions_data = sessions
        
        options = []
        for i, s in enumerate(sessions[:25], 1):  # Discord limit: 25 options
            key = s.get("key", "")
            owner_uid = s.get("owner_uid", "")
            roblox_info = s.get("roblox_info", {})
            roblox_name = roblox_info.get("name", "Unknown")
            
            label = f"#{i} {roblox_name[:20]}"
            description = f"Key: {key[:30]}..."
            
            options.append(discord.SelectOption(
                label=label,
                description=description,
                value=str(i-1)  # index
            ))
        
        super().__init__(
            placeholder="Select a session to manage...",
            options=options,
            min_values=1,
            max_values=1
        )
    
    async def callback(self, interaction: discord.Interaction):
        if not has_owner_role(interaction):
            await interaction.response.send_message("❌ You need the **Owner** role.", ephemeral=True)
            return
        
        idx = int(self.values[0])
        session = self.sessions_data[idx]
        key = session.get("key", "")
        
        # Show action buttons
        view = SessionActionButtons(key)
        await interaction.response.send_message(
            f"Selected session: `{key[:30]}...`\nChoose an action:",
            view=view,
            ephemeral=True
        )


class SessionActionButtons(discord.ui.View):
    """Action buttons for a selected session"""
    def __init__(self, key: str):
        super().__init__(timeout=60)
        self.key = key
    
    @discord.ui.button(label="🚫 Kick", style=discord.ButtonStyle.danger)
    async def kick_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(
            SessionActionModal("kick", self.key, "Kick User")
        )
    
    @discord.ui.button(label="📨 Send Notification", style=discord.ButtonStyle.primary)
    async def notify_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(
            SessionActionModal("notify", self.key, "Send Notification")
        )


class SessionsView(discord.ui.View):
    """View with dropdown for session management"""
    def __init__(self, sessions: list):
        super().__init__(timeout=120)
        if sessions:
            self.add_item(SessionActionSelect(sessions))


@tree.command(name="notifyuser", description="Send an in-game notification to a user running the script")
@app_commands.describe(
    key="The key of the user to notify",
    message="The message to show them in-game",
    sound_id="Optional Roblox sound asset ID to play with the notification"
)
async def notifyuser(interaction: discord.Interaction, key: str, message: str, sound_id: str = ""):
    if not has_owner_role(interaction):
        return await deny(interaction)

    api_secret = os.environ.get("API_SECRET", "vyron_secret")

    try:
        payload = json.dumps({
            "key": key.strip(),
            "message": message.strip(),
            "sound_id": sound_id.strip(),
            "discord_username": str(interaction.user),
            "secret": api_secret
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{API_BASE}/notify",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            result = json.loads(resp.read())

        if result.get("success"):
            embed = discord.Embed(
                title="Notification Queued",
                description=f"Your message will appear in-game within 5 seconds.",
                color=0x5865F2
            )
            embed.add_field(name="Key", value=f"`{key[:24]}{'...' if len(key) > 24 else ''}`", inline=True)
            embed.add_field(name="Message", value=message, inline=False)
            if sound_id:
                embed.add_field(name="Sound ID", value=f"`{sound_id}`", inline=True)
            embed.set_footer(text="Vyron.cc")
            await interaction.response.send_message(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(
                f"Failed: {result.get('reason', 'Unknown error')}", ephemeral=True
            )
    except Exception as e:
        await interaction.response.send_message(f"Error contacting API: `{e}`", ephemeral=True)


@tree.command(name="broadcastingame", description="Send a notification to ALL users currently running the script")
@app_commands.describe(
    message="The message to broadcast to everyone in-game",
    sound_id="Optional Roblox sound asset ID to play with the notification"
)
async def broadcastingame(interaction: discord.Interaction, message: str, sound_id: str = ""):
    if not has_owner_role(interaction):
        return await deny(interaction)

    await interaction.response.defer(ephemeral=True)

    # Fetch active sessions
    try:
        req = urllib.request.Request(f"{API_BASE}/sessions", method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            sessions = json.loads(resp.read())
    except Exception as e:
        await interaction.followup.send(f"❌ Could not reach API: `{e}`", ephemeral=True)
        return

    if not sessions:
        await interaction.followup.send("No active sessions to broadcast to.", ephemeral=True)
        return

    api_secret = os.environ.get("API_SECRET", "vyron_secret")
    sent = 0
    failed = 0

    for s in sessions:
        key = s.get("key", "")
        if not key:
            continue
        try:
            payload = json.dumps({
                "key": key,
                "message": message.strip(),
                "sound_id": sound_id.strip(),
                "secret": api_secret
            }).encode("utf-8")
            req = urllib.request.Request(
                f"{API_BASE}/notify",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                result = json.loads(resp.read())
            if result.get("success"):
                sent += 1
            else:
                failed += 1
        except Exception:
            failed += 1

    embed = discord.Embed(
        title="📡 Broadcast Sent",
        description=f"Message delivered to **{sent}** active session(s).",
        color=0x00CC66 if sent > 0 else 0xFF4444
    )
    embed.add_field(name="Message", value=message, inline=False)
    if sound_id:
        embed.add_field(name="Sound ID", value=f"`{sound_id}`", inline=True)
    if failed:
        embed.add_field(name="Failed", value=str(failed), inline=True)
    embed.set_footer(text="Vyron.cc")
    await interaction.followup.send(embed=embed, ephemeral=True)


@tree.command(name="notifyspam", description="Spam a notification to a user in-game multiple times")
@app_commands.describe(
    key="The key of the user to notify",
    message="The message to spam",
    times="How many times to send it (max 20)",
    delay="Seconds between each notification (min 0.5)",
    sound_id="Optional Roblox sound asset ID to play with each notification"
)
async def notifyspam(interaction: discord.Interaction, key: str, message: str, times: int, delay: float = 1.0, sound_id: str = ""):
    if not has_owner_role(interaction):
        return await deny(interaction)

    times = max(1, min(20, times))
    delay = max(0.5, delay)

    await interaction.response.defer(ephemeral=True)

    api_secret = os.environ.get("API_SECRET", "vyron_secret")
    sent = 0
    failed = 0

    for i in range(times):
        try:
            payload = json.dumps({
                "key": key.strip(),
                "message": message.strip(),
                "sound_id": sound_id.strip(),
                "secret": api_secret
            }).encode("utf-8")
            req = urllib.request.Request(
                f"{API_BASE}/notify",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                result = json.loads(resp.read())
            if result.get("success"):
                sent += 1
            else:
                failed += 1
        except Exception:
            failed += 1

        if i < times - 1:
            await asyncio.sleep(delay)

    embed = discord.Embed(
        title="📨 Notify Spam Complete",
        description=f"Sent **{sent}/{times}** notifications to `{key[:24]}{'...' if len(key) > 24 else ''}`.",
        color=0x5080FF if sent == times else 0xFF9900
    )
    embed.add_field(name="Message", value=message, inline=False)
    embed.add_field(name="Times", value=str(times), inline=True)
    embed.add_field(name="Delay", value=f"{delay}s", inline=True)
    if sound_id:
        embed.add_field(name="Sound ID", value=f"`{sound_id}`", inline=True)
    if failed:
        embed.add_field(name="Failed", value=str(failed), inline=True)
    embed.set_footer(text="Vyron.cc")
    await interaction.followup.send(embed=embed, ephemeral=True)


# ─────────────────────────────────────────────
#  MUSIC PANEL
# ─────────────────────────────────────────────

def _send_music_cmd(key: str, action: str, sound_id: str = "", loop: bool = False) -> bool:
    """Helper: queue a music command via the API. Returns True on success."""
    api_secret = os.environ.get("API_SECRET", "vyron_secret")
    import urllib.request as _ur
    try:
        payload = json.dumps({
            "key": key,
            "action": action,
            "sound_id": sound_id,
            "loop": loop,
            "secret": api_secret,
        }).encode("utf-8")
        req = _ur.Request(
            f"{API_BASE}/music",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with _ur.urlopen(req, timeout=5) as resp:
            result = json.loads(resp.read())
        return result.get("success", False)
    except Exception:
        return False


async def _fetch_sessions() -> list:
    """Fetch active sessions from the API."""
    import urllib.request as _ur
    try:
        req = _ur.Request(f"{API_BASE}/sessions", method="GET")
        with _ur.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())
    except Exception:
        return []


class MusicPlayModal(discord.ui.Modal, title="Play Music"):
    sound_input = discord.ui.TextInput(
        label="Sound Asset ID",
        placeholder="e.g. 1837843615",
        required=True,
        max_length=20,
    )
    key_input = discord.ui.TextInput(
        label="Key (leave blank to broadcast to all)",
        placeholder="Vyron-XXXXXXXXXXXXXXX  or  leave empty",
        required=False,
        max_length=64,
    )

    def __init__(self, loop: bool = False):
        super().__init__()
        self.loop = loop

    async def on_submit(self, interaction: discord.Interaction):
        sound_id = self.sound_input.value.strip()
        key_val  = self.key_input.value.strip()

        if not sound_id.isdigit():
            await interaction.response.send_message("❌ Sound ID must be a number.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        if key_val:
            # Single key
            ok = _send_music_cmd(key_val, "play", sound_id, self.loop)
            embed = discord.Embed(
                title="🎵 Music Queued" if ok else "❌ Failed",
                color=0x5080FF if ok else 0xFF4444,
            )
            embed.add_field(name="Key", value=f"`{key_val[:24]}{'...' if len(key_val) > 24 else ''}`", inline=True)
            embed.add_field(name="Sound ID", value=f"`{sound_id}`", inline=True)
            embed.add_field(name="Loop", value="✅ Yes" if self.loop else "❌ No", inline=True)
            embed.set_footer(text="Vyron.cc • Will play within 5 seconds")
        else:
            # Broadcast to all active sessions
            sessions = await _fetch_sessions()
            if not sessions:
                await interaction.followup.send("No active sessions to broadcast to.", ephemeral=True)
                return
            sent = sum(1 for s in sessions if _send_music_cmd(s["key"], "play", sound_id, self.loop))
            embed = discord.Embed(
                title="📡 Music Broadcast",
                description=f"Queued for **{sent}/{len(sessions)}** active session(s).",
                color=0x5080FF,
            )
            embed.add_field(name="Sound ID", value=f"`{sound_id}`", inline=True)
            embed.add_field(name="Loop", value="✅ Yes" if self.loop else "❌ No", inline=True)
            embed.set_footer(text="Vyron.cc • Will play within 5 seconds")

        await interaction.followup.send(embed=embed, ephemeral=True)


class MusicStopModal(discord.ui.Modal, title="Stop Music"):
    key_input = discord.ui.TextInput(
        label="Key (leave blank to stop for everyone)",
        placeholder="Vyron-XXXXXXXXXXXXXXX  or  leave empty",
        required=False,
        max_length=64,
    )

    async def on_submit(self, interaction: discord.Interaction):
        key_val = self.key_input.value.strip()
        await interaction.response.defer(ephemeral=True)

        if key_val:
            ok = _send_music_cmd(key_val, "stop")
            embed = discord.Embed(
                title="⏹ Music Stopped" if ok else "❌ Failed",
                color=0xAAAAAA if ok else 0xFF4444,
            )
            embed.add_field(name="Key", value=f"`{key_val[:24]}{'...' if len(key_val) > 24 else ''}`", inline=True)
        else:
            sessions = await _fetch_sessions()
            if not sessions:
                await interaction.followup.send("No active sessions.", ephemeral=True)
                return
            stopped = sum(1 for s in sessions if _send_music_cmd(s["key"], "stop"))
            embed = discord.Embed(
                title="⏹ Music Stopped for All",
                description=f"Stop queued for **{stopped}/{len(sessions)}** session(s).",
                color=0xAAAAAA,
            )
        embed.set_footer(text="Vyron.cc")
        await interaction.followup.send(embed=embed, ephemeral=True)


class MusicPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="▶ Play (No Loop)", style=discord.ButtonStyle.success, custom_id="music_play_once", row=0)
    async def play_once(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not has_owner_role(interaction):
            await interaction.response.send_message("❌ You need the **Owner** role.", ephemeral=True)
            return
        await interaction.response.send_modal(MusicPlayModal(loop=False))

    @discord.ui.button(label="🔁 Play (Loop)", style=discord.ButtonStyle.primary, custom_id="music_play_loop", row=0)
    async def play_loop(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not has_owner_role(interaction):
            await interaction.response.send_message("❌ You need the **Owner** role.", ephemeral=True)
            return
        await interaction.response.send_modal(MusicPlayModal(loop=True))

    @discord.ui.button(label="⏹ Stop Music", style=discord.ButtonStyle.danger, custom_id="music_stop", row=0)
    async def stop_music(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not has_owner_role(interaction):
            await interaction.response.send_message("❌ You need the **Owner** role.", ephemeral=True)
            return
        await interaction.response.send_modal(MusicStopModal())


@tree.command(name="addmusicpanel", description="Post the Vyron Music Control panel in this channel")
async def addmusicpanel(interaction: discord.Interaction):
    if not has_owner_role(interaction):
        return await deny(interaction)

    embed = discord.Embed(
        title="🎵 Vyron Music Control",
        description=(
            "Control in-game music for users running the script.\n\n"
            "**▶ Play (No Loop)** — play a sound once for a specific key or broadcast to all\n"
            "**🔁 Play (Loop)** — play a sound on repeat for a specific key or broadcast to all\n"
            "**⏹ Stop Music** — stop music for a specific key or stop for everyone\n\n"
            "Leave the key field **blank** to target all active sessions."
        ),
        color=0x5080FF,
    )
    embed.set_footer(text="Vyron.cc • Staff only")

    view = MusicPanelView()
    await interaction.channel.send(embed=embed, view=view)
    await interaction.response.send_message("✅ Music panel posted.", ephemeral=True)


# ─────────────────────────────────────────────
#  MEMBER MUSIC PANEL
#  Any member can use /membermusic to control music on their own key(s) only.
# ─────────────────────────────────────────────

def _get_user_keys(uid: str) -> list[str]:
    """Return all active (non-expired) keys belonging to a Discord user."""
    data = load_data()
    now  = int(time.time())
    keys = []
    for k in data.get("keys", {}).get(uid, []):
        expiry = data.get("key_expiry", {}).get(k)
        if expiry is None or expiry > now:
            keys.append(k)
    for k in data.get("keys_internal", {}).get(uid, []):
        expiry = data.get("key_expiry", {}).get(k)
        if expiry is None or expiry > now:
            keys.append(k)
    return keys


class MemberMusicPlayModal(discord.ui.Modal, title="Play Music (Your Key)"):
    sound_input = discord.ui.TextInput(
        label="Sound Asset ID",
        placeholder="e.g. 1837843615",
        required=True,
        max_length=20,
    )

    def __init__(self, uid: str, loop: bool = False):
        super().__init__()
        self.uid  = uid
        self.loop = loop

    async def on_submit(self, interaction: discord.Interaction):
        sound_id = self.sound_input.value.strip()
        if not sound_id.isdigit():
            await interaction.response.send_message("❌ Sound ID must be a number.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        keys = _get_user_keys(self.uid)
        if not keys:
            await interaction.followup.send("❌ You don't have any active keys.", ephemeral=True)
            return

        sent = sum(1 for k in keys if _send_music_cmd(k, "play", sound_id, self.loop))
        embed = discord.Embed(
            title="🎵 Music Queued" if sent else "❌ Failed",
            description=f"Queued for **{sent}/{len(keys)}** of your key(s).",
            color=0x5080FF if sent else 0xFF4444,
        )
        embed.add_field(name="Sound ID", value=f"`{sound_id}`", inline=True)
        embed.add_field(name="Loop", value="✅ Yes" if self.loop else "❌ No", inline=True)
        embed.set_footer(text="Vyron.cc • Will play within 5 seconds")
        await interaction.followup.send(embed=embed, ephemeral=True)


class MemberMusicPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="▶ Play (No Loop)", style=discord.ButtonStyle.success, custom_id="member_music_play_once", row=0)
    async def play_once(self, interaction: discord.Interaction, button: discord.ui.Button):
        uid = str(interaction.user.id)
        if not _get_user_keys(uid):
            await interaction.response.send_message("❌ You don't have any active keys.", ephemeral=True)
            return
        await interaction.response.send_modal(MemberMusicPlayModal(uid=uid, loop=False))

    @discord.ui.button(label="🔁 Play (Loop)", style=discord.ButtonStyle.primary, custom_id="member_music_play_loop", row=0)
    async def play_loop(self, interaction: discord.Interaction, button: discord.ui.Button):
        uid = str(interaction.user.id)
        if not _get_user_keys(uid):
            await interaction.response.send_message("❌ You don't have any active keys.", ephemeral=True)
            return
        await interaction.response.send_modal(MemberMusicPlayModal(uid=uid, loop=True))

    @discord.ui.button(label="⏹ Stop Music", style=discord.ButtonStyle.danger, custom_id="member_music_stop", row=0)
    async def stop_music(self, interaction: discord.Interaction, button: discord.ui.Button):
        uid  = str(interaction.user.id)
        keys = _get_user_keys(uid)
        if not keys:
            await interaction.response.send_message("❌ You don't have any active keys.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        stopped = sum(1 for k in keys if _send_music_cmd(k, "stop"))
        embed = discord.Embed(
            title="⏹ Music Stopped",
            description=f"Stop queued for **{stopped}/{len(keys)}** of your key(s).",
            color=0xAAAAAA,
        )
        embed.set_footer(text="Vyron.cc")
        await interaction.followup.send(embed=embed, ephemeral=True)


@tree.command(name="membermusic", description="Post the member music panel in this channel (Owner only)")
async def membermusic(interaction: discord.Interaction):
    if not has_owner_role(interaction):
        return await deny(interaction)

    embed = discord.Embed(
        title="🎵 Member Music Control",
        description=(
            "Play music in-game on your own key(s).\n\n"
            "**▶ Play (No Loop)** — play a sound once\n"
            "**🔁 Play (Loop)** — play a sound on repeat\n"
            "**⏹ Stop Music** — stop your music\n\n"
            "Buttons only affect keys registered to **your** Discord account."
        ),
        color=0x5080FF,
    )
    embed.set_footer(text="Vyron.cc • Only affects your own keys")

    view = MemberMusicPanelView()
    await interaction.channel.send(embed=embed, view=view)
    await interaction.response.send_message("✅ Member music panel posted.", ephemeral=True)


# ─────────────────────────────────────────────
#  IN-GAME PLAYER COMMANDS
# ─────────────────────────────────────────────

def _send_kick(key: str, reason: str) -> bool:
    api_secret = os.environ.get("API_SECRET", "vyron_secret")
    import urllib.request as _ur
    try:
        payload = json.dumps({"key": key, "reason": reason, "secret": api_secret}).encode()
        req = _ur.Request(f"{API_BASE}/kick", data=payload,
                          headers={"Content-Type": "application/json"}, method="POST")
        with _ur.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read()).get("success", False)
    except Exception:
        return False

def _send_notify(key: str, message: str) -> bool:
    api_secret = os.environ.get("API_SECRET", "vyron_secret")
    import urllib.request as _ur
    try:
        payload = json.dumps({"key": key, "message": message, "secret": api_secret}).encode()
        req = _ur.Request(f"{API_BASE}/notify", data=payload,
                          headers={"Content-Type": "application/json"}, method="POST")
        with _ur.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read()).get("success", False)
    except Exception:
        return False


@tree.command(name="senttogame", description="Force a user to join a Roblox game by their key and place ID")
@app_commands.describe(
    key="The key of the user to teleport",
    place_id="The Roblox place ID to send them to"
)
async def senttogame(interaction: discord.Interaction, key: str, place_id: str):
    # Double lock: must have Owner role AND be v9pv
    if not has_owner_role(interaction):
        return await deny(interaction)
    if str(interaction.user).lower().split("#")[0] != "v9pv":
        await interaction.response.send_message("You do not have permission to use this command.", ephemeral=True)
        return

    key = key.strip()
    place_id = place_id.strip()

    if not place_id.isdigit():
        await interaction.response.send_message("Invalid place ID. Must be a number.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    api_secret = os.environ.get("API_SECRET", "vyron_secret")

    try:
        payload = json.dumps({
            "key": key,
            "place_id": place_id,
            "job_id": "",  # empty = join any available server
            "secret": api_secret
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{API_BASE}/teleport",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            result = json.loads(resp.read())
    except Exception as e:
        await interaction.followup.send(f"Error contacting API: `{e}`", ephemeral=True)
        return

    if result.get("success"):
        embed = discord.Embed(
            title="Teleport Queued",
            description="User will be sent to the game within 5 seconds.",
            color=0x57F287
        )
        embed.add_field(name="Key", value=f"`{key[:24]}{'...' if len(key) > 24 else ''}`", inline=True)
        embed.add_field(name="Place ID", value=f"`{place_id}`", inline=True)
        embed.set_footer(text="Vyron.cc")
        await interaction.followup.send(embed=embed, ephemeral=True)
    else:
        await interaction.followup.send(f"Failed: {result.get('reason', 'Unknown error')}", ephemeral=True)


@tree.command(name="kickall", description="Kick everyone currently in-game with a fixed update message")
async def kickall(interaction: discord.Interaction):
    if not has_owner_role(interaction):
        return await deny(interaction)
    await interaction.response.defer(ephemeral=True)
    sessions = await _fetch_sessions()
    if not sessions:
        await interaction.followup.send("No active sessions to kick.", ephemeral=True)
        return
    reason = "Vyron Updated Please Rejoin From XIM."
    kicked = sum(1 for s in sessions if _send_kick(s["key"], reason))
    embed = discord.Embed(
        title="👢 Kicked All Sessions",
        description=f"Kicked **{kicked}/{len(sessions)}** active session(s).",
        color=0xFF6600,
    )
    embed.add_field(name="Reason", value=reason, inline=False)
    embed.set_footer(text="Vyron.cc")
    await interaction.followup.send(embed=embed, ephemeral=True)
    asyncio.create_task(send_webhook(embeds=[{
        "title": "👢 Kick All Fired",
        "color": 0xFF6600,
        "fields": [
            {"name": "By", "value": f"{interaction.user.mention} ({interaction.user})", "inline": True},
            {"name": "Sessions kicked", "value": str(kicked), "inline": True},
            {"name": "Reason", "value": reason, "inline": False},
        ],
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }]))


@tree.command(name="rejoinplayer", description="Force a player to rejoin the exact server they are in by their key")
@app_commands.describe(key="The player's key", reason="Optional message shown to them before rejoin")
async def rejoinplayer(interaction: discord.Interaction, key: str, reason: str = "Please rejoin the game."):
    if not has_owner_role(interaction):
        return await deny(interaction)
    await interaction.response.defer(ephemeral=True)

    dashboard_password = os.environ.get("DASHBOARD_PASSWORD", "vyron_admin")
    api_secret         = os.environ.get("API_SECRET", "vyron_secret")

    # 1. Get their current server location
    try:
        req = urllib.request.Request(
            f"{API_BASE}/location/{urllib.parse.quote(key.strip())}",
            method="GET",
            headers={"X-Admin-Password": dashboard_password},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            loc = json.loads(resp.read())
    except Exception as e:
        await interaction.followup.send(f"❌ Could not reach API: `{e}`", ephemeral=True)
        return

    if not loc.get("online"):
        await interaction.followup.send("❌ That key is not in an active session right now.", ephemeral=True)
        return

    place_id = loc.get("place_id", "")
    job_id   = loc.get("job_id", "")

    if not place_id or not job_id:
        await interaction.followup.send("❌ Could not retrieve server location for that key.", ephemeral=True)
        return

    # 2. Notify them
    _send_notify(key.strip(), reason)

    # 3. Kick them — the script will teleport them back via the stored place/job
    # Queue a teleport to the same server first, then kick
    try:
        payload = json.dumps({
            "key": key.strip(),
            "place_id": place_id,
            "job_id": job_id,
            "secret": api_secret,
        }).encode()
        req = urllib.request.Request(
            f"{API_BASE}/teleport",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            tp_result = json.loads(resp.read())
    except Exception as e:
        await interaction.followup.send(f"❌ Teleport queue failed: `{e}`", ephemeral=True)
        return

    ok = tp_result.get("success", False)
    embed = discord.Embed(
        title="🔄 Rejoin Queued" if ok else "❌ Failed",
        color=0x00CC66 if ok else 0xFF4444,
    )
    embed.add_field(name="Key", value=f"`{key.strip()[:24]}{'…' if len(key) > 24 else ''}`", inline=True)
    embed.add_field(name="Place ID", value=f"`{place_id}`", inline=True)
    embed.add_field(name="Job ID", value=f"`{job_id[:16]}…`", inline=True)
    embed.add_field(name="Message", value=reason, inline=False)
    embed.set_footer(text="Vyron.cc • Will teleport to same server within 5 seconds")
    await interaction.followup.send(embed=embed, ephemeral=True)


@tree.command(name="activesessions", description="View all users currently running the script")
async def activesessions(interaction: discord.Interaction):
    if not has_owner_role(interaction):
        return await deny(interaction)

    await interaction.response.defer(ephemeral=True)

    try:
        req = urllib.request.Request(f"{API_BASE}/sessions", method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            sessions = json.loads(resp.read())
    except Exception as e:
        await interaction.followup.send(f"❌ Could not reach API: `{e}`", ephemeral=True)
        return

    if not sessions:
        embed = discord.Embed(
            title="📡 Active Sessions",
            description="```\nNo active sessions right now.\n```",
            color=0x2b2d31,
        )
        embed.set_footer(text="Vyron.cc • 0 users online")
        await interaction.followup.send(embed=embed, ephemeral=True)
        return

    now = int(time.time())

    embed = discord.Embed(
        title="� Active Sessions",
        color=0x5080FF,
    )

    # header bar
    embed.description = (
        f"```ansi\n\u001b[1;32m● {len(sessions)} user(s) online\u001b[0m\n```"
    )

    for i, s in enumerate(sessions[:10], 1):
        owner_uid = s.get("owner_uid")
        if owner_uid:
            member = interaction.guild.get_member(int(owner_uid))
            owner_str = member.display_name if member else f"User {owner_uid}"
            mention   = member.mention if member else f"<@{owner_uid}>"
        else:
            owner_str = "Unknown"
            mention   = "Unknown"

        ago = now - s.get("last_seen", now)
        if ago < 60:
            seen_str = f"{ago}s ago"
        elif ago < 3600:
            seen_str = f"{ago // 60}m ago"
        else:
            seen_str = f"{ago // 3600}h ago"

        expiry = s.get("expiry", "?")
        key_short = s["key"][:22] + "…"

        place_id = s.get("place_id", "")
        job_id   = s.get("job_id", "")
        server_line = f"🌐 `{place_id}` / `{job_id[:12]}…`" if place_id and job_id else "🌐 Location unknown"

        embed.add_field(
            name=f"{'🟢' if ago < 15 else '🟡' if ago < 40 else '🔴'} #{i}  {owner_str}",
            value=(
                f"👤 {mention}\n"
                f"🔑 `{key_short}`\n"
                f"⏳ Expires: **{expiry}**\n"
                f"🕐 Last ping: **{seen_str}**\n"
                f"{server_line}"
            ),
            inline=True,
        )

    embed.set_footer(text=f"Vyron.cc • {len(sessions)} session(s) • refreshed just now")
    embed.timestamp = discord.utils.utcnow()

    view = SessionsView(sessions)
    await interaction.followup.send(embed=embed, view=view, ephemeral=True)


@tree.command(name="joinuserkey", description="Teleport yourself to another user's Roblox server using their key")
@app_commands.describe(
    my_key="Your own Vyron key (you must be in-game with the script running)",
    target_key="The key of the user whose server you want to join"
)
async def joinuserkey(interaction: discord.Interaction, my_key: str, target_key: str):
    if not has_owner_role(interaction):
        return await deny(interaction)

    my_key     = my_key.strip()
    target_key = target_key.strip()

    if my_key == target_key:
        await interaction.response.send_message("❌ Your key and the target key can't be the same.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    api_secret        = os.environ.get("API_SECRET", "vyron_secret")
    dashboard_password = os.environ.get("DASHBOARD_PASSWORD", "vyron_admin")

    # ── 1. Look up the target's current location ──────────────────────────────
    try:
        req = urllib.request.Request(
            f"{API_BASE}/location/{urllib.parse.quote(target_key)}",
            method="GET",
            headers={"X-Admin-Password": dashboard_password},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            loc = json.loads(resp.read())
    except Exception as e:
        await interaction.followup.send(f"❌ Could not reach API: `{e}`", ephemeral=True)
        return

    if not loc.get("online"):
        await interaction.followup.send(
            f"❌ Target key is not in an active session right now.\n"
            f"They need to be in-game with the script running.",
            ephemeral=True
        )
        return

    place_id = loc.get("place_id", "")
    job_id   = loc.get("job_id", "")

    if not place_id or not job_id:
        await interaction.followup.send(
            "❌ Target's location data is incomplete. They may be on an older script version.",
            ephemeral=True
        )
        return

    # ── 2. Check your own key is in an active session ─────────────────────────
    try:
        req = urllib.request.Request(
            f"{API_BASE}/location/{urllib.parse.quote(my_key)}",
            method="GET",
            headers={"X-Admin-Password": dashboard_password},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            my_loc = json.loads(resp.read())
    except Exception as e:
        await interaction.followup.send(f"❌ Could not reach API: `{e}`", ephemeral=True)
        return

    if not my_loc.get("online"):
        await interaction.followup.send(
            "❌ Your key is not in an active session. You need to be in-game with the script running first.",
            ephemeral=True
        )
        return

    # ── 3. Queue the teleport for your key ────────────────────────────────────
    try:
        payload = json.dumps({
            "key":      my_key,
            "place_id": place_id,
            "job_id":   job_id,
            "secret":   api_secret,
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{API_BASE}/teleport",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            result = json.loads(resp.read())
    except Exception as e:
        await interaction.followup.send(f"❌ Error queuing teleport: `{e}`", ephemeral=True)
        return

    if not result.get("success"):
        await interaction.followup.send(
            f"❌ Failed to queue teleport: {result.get('reason', 'Unknown error')}",
            ephemeral=True
        )
        return

    embed = discord.Embed(
        title="🚀 Teleport Queued",
        description="Your script will teleport you within **5 seconds**.",
        color=0x00CC66,
    )
    embed.add_field(name="Target Key", value=f"`{target_key[:24]}{'...' if len(target_key) > 24 else ''}`", inline=True)
    embed.add_field(name="Place ID", value=f"`{place_id}`", inline=True)
    embed.add_field(name="Server", value=f"`{job_id[:20]}...`", inline=False)
    embed.set_footer(text="Vyron.cc • Server must be public for this to work")
    await interaction.followup.send(embed=embed, ephemeral=True)


@tree.command(name="transferkey", description="Transfer a key to another user")
@app_commands.describe(
    key="The key to transfer",
    new_user="The user to transfer the key to",
    mode="Move (remove from old user) or Copy (both users keep it)"
)
@app_commands.choices(mode=[
    app_commands.Choice(name="Move — only new user gets it", value="move"),
    app_commands.Choice(name="Copy — both users keep it", value="copy"),
])
async def transferkey(interaction: discord.Interaction, key: str, new_user: discord.Member, mode: str):
    if not has_owner_role(interaction):
        return await deny(interaction)

    key = key.strip()
    data = load_data()

    # Find current owner
    old_uid = None
    pool = "keys"
    for uid, keys in data.get("keys", {}).items():
        if key in keys:
            old_uid = uid
            break
    if not old_uid:
        for uid, keys in data.get("keys_internal", {}).items():
            if key in keys:
                old_uid = uid
                pool = "keys_internal"
                break

    if not old_uid:
        await interaction.response.send_message("❌ Key not found.", ephemeral=True)
        return

    new_uid = str(new_user.id)

    # Check new user doesn't already have it
    if key in data.get("keys", {}).get(new_uid, []) or key in data.get("keys_internal", {}).get(new_uid, []):
        await interaction.response.send_message(
            f"❌ {new_user.mention} already has that key.", ephemeral=True
        )
        return

    old_member = interaction.guild.get_member(int(old_uid))
    old_str = old_member.mention if old_member else f"<@{old_uid}>"

    if mode == "move":
        # Remove from old user
        pk = data.setdefault(pool, {})
        pk[old_uid].remove(key)
        pk.setdefault(new_uid, []).append(key)
        save_data(data)

        # DM old user
        try:
            if old_member:
                dm = discord.Embed(
                    title="🔑 Key Transferred",
                    description=f"Your key has been transferred to another user.",
                    color=0xFF9900
                )
                dm.add_field(name="Key", value=f"```{key}```", inline=False)
                dm.set_footer(text="Vyron.cc")
                await old_member.send(embed=dm)
        except discord.Forbidden:
            pass

        # DM new user
        try:
            expiry = data.get("key_expiry", {}).get(key)
            dm2 = discord.Embed(
                title="🔑 Key Received",
                description=f"A Vyron V2 key has been transferred to you.",
                color=0x00CC66
            )
            dm2.add_field(name="Key", value=f"```{key}```", inline=False)
            dm2.add_field(name="Expires", value=f"<t:{expiry}:R>" if expiry else "Lifetime", inline=True)
            dm2.set_footer(text="Vyron.cc")
            await new_user.send(embed=dm2)
        except discord.Forbidden:
            pass

        embed = discord.Embed(
            title="🔑 Key Moved",
            description=f"Key moved from {old_str} → {new_user.mention}.\n{old_str} no longer has access.",
            color=0xFF9900
        )

    else:  # copy
        # Keep on old user, add to new user
        data.setdefault(pool, {}).setdefault(new_uid, []).append(key)
        save_data(data)

        # DM new user
        try:
            expiry = data.get("key_expiry", {}).get(key)
            dm2 = discord.Embed(
                title="🔑 Key Received",
                description=f"A Vyron V2 key has been shared with you.",
                color=0x00CC66
            )
            dm2.add_field(name="Key", value=f"```{key}```", inline=False)
            dm2.add_field(name="Expires", value=f"<t:{expiry}:R>" if expiry else "Lifetime", inline=True)
            dm2.set_footer(text="Vyron.cc")
            await new_user.send(embed=dm2)
        except discord.Forbidden:
            pass

        embed = discord.Embed(
            title="🔑 Key Copied",
            description=f"Key copied to {new_user.mention}.\n{old_str} still has access too.",
            color=0x5080FF
        )

    embed.add_field(name="Key", value=f"```{key}```", inline=False)
    embed.add_field(name="Mode", value="Move" if mode == "move" else "Copy", inline=True)
    embed.set_footer(text="Vyron.cc")
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ─────────────────────────────────────────────
#  INTERNAL KEY COMMANDS (C++ loader / edition=int — uses keys_internal, prefix VyronInt-)
# ─────────────────────────────────────────────

@tree.command(name="genintkey", description="Generate a Vyron Internal key for yourself (C++ loader)")
@app_commands.describe(duration="Duration: e.g. 1h, 7d, 2w, 1m, lifetime")
async def genintkey(interaction: discord.Interaction, duration: str = "lifetime"):
    if not has_owner_role(interaction):
        return await deny(interaction)
    secs = parse_duration(duration)
    if secs == 0:
        await interaction.response.send_message("❌ Invalid duration.", ephemeral=True)
        return
    key = gen_int_key()
    data = load_data()
    uid = str(interaction.user.id)
    expiry = int(time.time()) + secs if secs else None
    data.setdefault("key_expiry", {})[key] = expiry
    data.setdefault("key_created", {})[key] = int(time.time())
    data.setdefault("key_generated_by", {})[key] = uid
    data.setdefault("keys_internal", {}).setdefault(uid, []).append(key)
    save_data(data)
    embed = discord.Embed(title="Vyron Internal Key", description=f"```{key}```", color=0x00AAFF)
    embed.add_field(name="Duration", value=duration_label(secs), inline=True)
    if expiry:
        embed.add_field(name="Expires", value=f"<t:{expiry}:R>", inline=True)
    embed.set_footer(text="Vyron.cc • Internal (loader)")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="genintkeyto", description="Generate a Vyron Internal key and DM it to a user")
@app_commands.describe(user="The user to send the key to", duration="Duration: e.g. 1h, 7d, 2w, 1m, lifetime")
async def genintkeyto(interaction: discord.Interaction, user: discord.Member, duration: str = "lifetime"):
    if not has_owner_role(interaction):
        return await deny(interaction)
    secs = parse_duration(duration)
    if secs == 0:
        await interaction.response.send_message("❌ Invalid duration.", ephemeral=True)
        return
    key = gen_int_key()
    data = load_data()
    uid = str(user.id)
    expiry = int(time.time()) + secs if secs else None
    data.setdefault("key_expiry", {})[key] = expiry
    data.setdefault("key_created", {})[key] = int(time.time())
    data.setdefault("key_generated_by", {})[key] = str(interaction.user.id)
    data.setdefault("keys_internal", {}).setdefault(uid, []).append(key)
    save_data(data)
    dm_embed = discord.Embed(title="Vyron Internal Key", description=f"```{key}```", color=0x00AAFF)
    dm_embed.add_field(name="Duration", value=duration_label(secs), inline=True)
    if expiry:
        dm_embed.add_field(name="Expires", value=f"<t:{expiry}:R>", inline=True)
    dm_embed.set_footer(text="Vyron.cc • Internal (loader)")
    pub_embed = discord.Embed(
        title="Internal Key Sent",
        description=f"{interaction.user.mention} sent a Vyron Internal key to {user.mention}.",
        color=0x00AAFF
    )
    pub_embed.add_field(name="Duration", value=duration_label(secs), inline=True)
    pub_embed.set_footer(text="Vyron.cc • Internal")
    try:
        await user.send(embed=dm_embed)
        await interaction.response.send_message(embed=pub_embed)
    except discord.Forbidden:
        await interaction.response.send_message(f"Couldn't DM {user.mention} — they may have DMs disabled.")


@tree.command(name="addinternalpanel", description="Post the Vyron Internal key panel in this channel")
async def addinternalpanel(interaction: discord.Interaction):
    if not has_owner_role(interaction):
        return await deny(interaction)

    embed = discord.Embed(
        title="Vyron Internal Panel",
        description=(
            "If you have a Vyron Internal key (C++ loader), use the buttons below.\n\n"
            "Your key is bound to your **HWID** on first use.\n"
            "Use **Reset HWID** if you changed your PC (24h cooldown)."
        ),
        color=0x00AAFF
    )
    embed.set_footer(text=f"Sent by {interaction.user} • Vyron.cc Internal")

    view = InternalPanelView()
    await interaction.channel.send(embed=embed, view=view)
    await interaction.response.send_message("✅ Internal panel posted.", ephemeral=True)


class InternalPanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Redeem Key", style=discord.ButtonStyle.success, custom_id="int_panel_redeem")
    async def redeem_key(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(InternalRedeemModal())

    @discord.ui.button(label="Reset HWID", style=discord.ButtonStyle.secondary, custom_id="int_panel_resethwid")
    async def reset_hwid(self, interaction: discord.Interaction, button: discord.ui.Button):
        data = load_data()
        uid = str(interaction.user.id)
        now = int(time.time())

        redeemed = data.get("int_redeemed_keys", {}).get(uid)
        if not redeemed:
            await interaction.response.send_message("❌ You haven't redeemed an internal key yet.", ephemeral=True)
            return

        key = redeemed["key"]
        cooldowns = data.setdefault("hwid_reset_cooldown", {})
        last_reset = cooldowns.get(uid + "_int", 0)
        if now - last_reset < 86400:
            next_reset = last_reset + 86400
            await interaction.response.send_message(
                f"❌ HWID reset available <t:{next_reset}:R>.", ephemeral=True)
            return

        key_hwid = data.setdefault("key_hwid", {})
        key_hwid.pop(key, None)
        cooldowns[uid + "_int"] = now
        save_data(data)
        await interaction.response.send_message(
            "✅ HWID reset. Next launch will bind to your new machine.", ephemeral=True)

    @discord.ui.button(label="Key Info", style=discord.ButtonStyle.primary, custom_id="int_panel_info")
    async def key_info(self, interaction: discord.Interaction, button: discord.ui.Button):
        data = load_data()
        uid = str(interaction.user.id)
        now = int(time.time())

        redeemed = data.get("int_redeemed_keys", {}).get(uid)
        if not redeemed:
            await interaction.response.send_message("❌ You haven't redeemed an internal key yet.", ephemeral=True)
            return

        key = redeemed["key"]
        expiry = data.get("key_expiry", {}).get(key)
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

        last_reset = data.get("hwid_reset_cooldown", {}).get(uid + "_int", 0)
        hwid_reset_str = f"<t:{last_reset + 86400}:R>" if last_reset and now - last_reset < 86400 else "Available now"

        embed = discord.Embed(title="Vyron Internal Key Info", color=0x00AAFF)
        embed.add_field(name="Key", value=f"```{key}```", inline=False)
        embed.add_field(name="Status", value=status, inline=True)
        embed.add_field(name="Expires", value=expiry_str, inline=True)
        embed.add_field(name="Redeemed", value=f"<t:{redeemed_at}:R>" if redeemed_at else "Unknown", inline=True)
        embed.add_field(name="HWID Bound", value="Yes" if hwid != "Not bound yet" else "No", inline=True)
        embed.add_field(name="Next HWID Reset", value=hwid_reset_str, inline=True)
        embed.set_footer(text="Vyron.cc • Internal")
        await interaction.response.send_message(embed=embed, ephemeral=True)


class InternalRedeemModal(discord.ui.Modal, title="Redeem Internal Key"):
    key_input = discord.ui.TextInput(
        label="Enter your internal key",
        placeholder="VyronInt-XXXXXXXXXXXXXXX",
        min_length=4,
        max_length=64,
        required=True
    )

    async def on_submit(self, interaction: discord.Interaction):
        key = self.key_input.value.strip()
        data = load_data()
        uid = str(interaction.user.id)
        now = int(time.time())

        if not key.startswith("VyronInt-"):
            await interaction.response.send_message(
                "❌ Internal keys start with `VyronInt-`.",
                ephemeral=True)
            return

        all_perm_keys = set()
        for keys in data.get("keys_internal", {}).values():
            all_perm_keys.update(keys)

        if key not in all_perm_keys:
            await interaction.response.send_message("❌ Invalid key.", ephemeral=True)
            return

        expiry = data.get("key_expiry", {}).get(key)
        if expiry is not None and now > expiry:
            await interaction.response.send_message("❌ That key has expired.", ephemeral=True)
            return

        int_redeemed = data.setdefault("int_redeemed_keys", {})
        for existing_uid, r in int_redeemed.items():
            if r["key"] == key and existing_uid != uid:
                await interaction.response.send_message(
                    "❌ That key has already been redeemed by another user.", ephemeral=True)
                return

        if uid in data.get("blacklist", {}):
            await interaction.response.send_message("❌ You are blacklisted.", ephemeral=True)
            return

        int_redeemed[uid] = {"key": key, "redeemed_at": now}
        save_data(data)

        expiry_str = f"<t:{expiry}:R>" if expiry else "Lifetime"
        embed = discord.Embed(
            title="✅ Internal Key Redeemed",
            description="Your key is ready. Run the loader and enter it when prompted.",
            color=0x00CC66
        )
        embed.add_field(name="Key", value=f"```{key}```", inline=False)
        embed.add_field(name="Expires", value=expiry_str, inline=True)
        embed.set_footer(text="Vyron.cc • Internal")
        await interaction.response.send_message(embed=embed, ephemeral=True)


# ─────────────────────────────────────────────
#  /lookup COMMAND
# ─────────────────────────────────────────────

async def _fetch_roblox_avatar(roblox_id: str) -> Optional[str]:
    """Fetch the Roblox headshot thumbnail URL for a user ID. Returns None on failure."""
    try:
        import aiohttp
        url = (
            f"https://thumbnails.roblox.com/v1/users/avatar-headshot"
            f"?userIds={roblox_id}&size=150x150&format=Png"
        )
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    payload = await resp.json()
                    return payload.get("data", [{}])[0].get("imageUrl")
    except Exception:
        pass
    return None


async def _lookup_key_data(key: str) -> Optional[dict]:
    """Call the local /lookup API and return the parsed JSON, or None on failure."""
    try:
        import aiohttp
        dashboard_pw = os.environ.get("DASHBOARD_PASSWORD", "vyron_admin")
        api_base = os.environ.get("API_BASE", "http://localhost:8080")
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{api_base}/lookup",
                params={"key": key},
                headers={"X-Admin-Password": dashboard_pw},
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
    except Exception:
        pass
    return None


def _build_lookup_embed(info: dict, guild: discord.Guild) -> discord.Embed:
    """Build the rich lookup embed from a /lookup API response."""
    now = int(time.time())

    # Colour logic
    if info.get("blacklisted"):
        color = 0xFF4444
    elif info.get("active"):
        color = 0x00CC66
    else:
        color = 0x5080FF

    embed = discord.Embed(title="🔍 Key Lookup", color=color)

    # Key (monospace)
    embed.add_field(name="Key", value=f"`{info['key']}`", inline=False)

    # Type badge
    type_map = {"external": "External", "internal": "Internal", "temp": "Temp"}
    embed.add_field(name="Type", value=type_map.get(info.get("type", ""), info.get("type", "Unknown")), inline=True)

    # Owner
    owner_uid = info.get("owner_uid")
    if owner_uid:
        member = guild.get_member(int(owner_uid))
        if member:
            owner_val = f"{member.mention} — {member.display_name} (@{member.name})"
        else:
            owner_val = f"<@{owner_uid}>"
    else:
        owner_val = "Unknown"
    embed.add_field(name="Owner", value=owner_val, inline=True)

    # Generated by
    gen_by = info.get("generated_by")
    if gen_by:
        gen_member = guild.get_member(int(gen_by))
        gen_val = gen_member.mention if gen_member else f"<@{gen_by}>"
    else:
        gen_val = "N/A"
    embed.add_field(name="Generated By", value=gen_val, inline=True)

    # Created
    created = info.get("created")
    embed.add_field(name="Created", value=f"<t:{created}:R>" if created else "Unknown", inline=True)

    # Expiry
    expiry_str = info.get("expiry", "Unknown")
    expiry_ts = info.get("expiry_ts")
    if expiry_ts and expiry_str not in ("Lifetime", "Expired"):
        expiry_val = f"{expiry_str} (<t:{expiry_ts}:R>)"
    else:
        expiry_val = expiry_str
    embed.add_field(name="Expiry", value=expiry_val, inline=True)

    # HWID
    hwid = info.get("hwid") or "Not bound yet"
    embed.add_field(name="HWID", value=f"`{hwid}`", inline=False)

    # Roblox user
    roblox_name = info.get("roblox_name", "")
    roblox_id = info.get("roblox_id", "")
    embed.add_field(
        name="Roblox User",
        value=roblox_name if roblox_name else "Not executed yet",
        inline=True,
    )

    # Roblox profile link
    if roblox_id:
        embed.add_field(
            name="Roblox Profile",
            value=f"[View Profile](https://www.roblox.com/users/{roblox_id}/profile)",
            inline=True,
        )

    # Last executed
    last_exec = info.get("last_exec")
    embed.add_field(
        name="Last Executed",
        value=f"<t:{last_exec}:R>" if last_exec else "Never",
        inline=True,
    )

    # Active status
    embed.add_field(
        name="Currently Active",
        value="🟢 Online" if info.get("active") else "⚫ Offline",
        inline=True,
    )

    # Executions
    embed.add_field(name="Executions", value=str(info.get("executions", 0)), inline=True)

    # Blacklisted
    if info.get("blacklisted"):
        embed.add_field(name="Blacklisted", value="🚫 Yes", inline=True)

    embed.set_footer(text="Vyron.cc")
    return embed


@tree.command(name="lookup", description="Look up a key or all keys for a user")
@app_commands.describe(
    key="The key to look up",
    user="The Discord user whose keys to look up"
)
async def lookup(interaction: discord.Interaction, key: str = None, user: discord.Member = None):
    if not has_owner_role(interaction):
        return await deny(interaction)

    if not key and not user:
        await interaction.response.send_message(
            "❌ Provide at least one of `key` or `user`.", ephemeral=True
        )
        return

    await interaction.response.defer(ephemeral=True)

    if key:
        # Single key lookup
        info = await _lookup_key_data(key.strip())
        if not info or "error" in info:
            await interaction.followup.send(
                f"❌ Key not found or API error: `{info.get('error', 'Unknown') if info else 'No response'}`",
                ephemeral=True,
            )
            return

        embed = _build_lookup_embed(info, interaction.guild)

        # Fetch Roblox avatar thumbnail
        roblox_id = info.get("roblox_id", "")
        if roblox_id:
            avatar_url = await _fetch_roblox_avatar(roblox_id)
            if avatar_url:
                embed.set_thumbnail(url=avatar_url)

        await interaction.followup.send(embed=embed, ephemeral=True)

    else:
        # User lookup — find all keys for this user
        data = load_data()
        uid = str(user.id)
        all_keys = list(data.get("keys", {}).get(uid, []))
        all_keys += list(data.get("keys_internal", {}).get(uid, []))
        # Add temp keys
        for t in data.get("temp_keys", {}).get(uid, []):
            all_keys.append(t["key"])

        if not all_keys:
            await interaction.followup.send(
                f"No keys found for {user.mention}.", ephemeral=True
            )
            return

        embeds = []
        for k in all_keys[:10]:  # cap at 10 to avoid hitting Discord limits
            info = await _lookup_key_data(k)
            if not info or "error" in info:
                continue
            embed = _build_lookup_embed(info, interaction.guild)
            roblox_id = info.get("roblox_id", "")
            if roblox_id:
                avatar_url = await _fetch_roblox_avatar(roblox_id)
                if avatar_url:
                    embed.set_thumbnail(url=avatar_url)
            embeds.append(embed)

        if not embeds:
            await interaction.followup.send(
                f"Could not retrieve key data for {user.mention}.", ephemeral=True
            )
            return

        # Discord allows up to 10 embeds per message
        await interaction.followup.send(embeds=embeds[:10], ephemeral=True)


@client.event
async def on_ready():
    start_api_thread()
    # Re-register persistent views so buttons work after restart
    client.add_view(KeyPanelView())
    client.add_view(TicketSelectView())
    client.add_view(CloseTicketView())
    client.add_view(InternalPanelView())
    client.add_view(MusicPanelView())
    client.add_view(MemberMusicPanelView())
    client.add_view(ImprovedKeyPanelView())
    # Start background expiry check loop
    asyncio.create_task(expiry_check_loop())
    # Start tamper alert polling loop
    asyncio.create_task(tamper_alert_loop())
    # Sync commands to all guilds instantly (guild sync is immediate, global takes ~1hr)
    for guild in client.guilds:
        try:
            await tree.sync(guild=guild)
            print(f"Synced commands to guild: {guild.name} ({guild.id})")
        except Exception as e:
            print(f"Failed to sync to guild {guild.name}: {e}")
    # Also do global sync for any new servers
    await tree.sync()
    print(f"Logged in as {client.user} — commands synced to {len(client.guilds)} guild(s)")

TAMPER_CHANNEL_ID = 1492419723634409533
API_BASE = os.environ.get("API_BASE", "http://localhost:8080")
DASHBOARD_PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "vyron_admin")

async def tamper_alert_loop():
    """Polls the API every 10 seconds for new tamper reports and posts alerts to the tamper channel."""
    await client.wait_until_ready()
    import aiohttp
    while not client.is_closed():
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{API_BASE}/tamper/pending",
                    headers={"X-Admin-Password": DASHBOARD_PASSWORD},
                    timeout=aiohttp.ClientTimeout(total=8)
                ) as resp:
                    if resp.status == 200:
                        reports = await resp.json()
                        for report in reports:
                            await send_tamper_alert(report)
        except Exception:
            pass
        await asyncio.sleep(10)

async def send_tamper_alert(report: dict):
    """Sends a tamper detection embed to the tamper channel."""
    channel = client.get_channel(TAMPER_CHANNEL_ID)
    if not channel:
        return

    key          = report.get("key", "unknown")
    roblox_user  = report.get("roblox_user", "unknown")
    tamper_type  = report.get("tamper_type", "unknown")
    detected_at  = report.get("at", 0)

    # Look up which Discord user owns this key
    data = load_data()
    owner_uid = None
    for uid, keys in data.get("keys", {}).items():
        if key in keys:
            owner_uid = uid
            break
    if owner_uid is None:
        for uid, keys in data.get("keys_internal", {}).items():
            if key in keys:
                owner_uid = uid
                break

    discord_mention = f"<@{owner_uid}>" if owner_uid else "Unknown"

    # Try to get their Discord username
    discord_username = "Unknown"
    if owner_uid:
        guild = channel.guild
        member = guild.get_member(int(owner_uid)) if guild else None
        if member:
            discord_username = f"{member.name} ({member.display_name})"

    embed = discord.Embed(
        title="🚨 Tamper Detected",
        color=0xFF2222,
        timestamp=discord.utils.utcnow()
    )
    embed.add_field(name="Roblox Username", value=roblox_user, inline=True)
    embed.add_field(name="Discord User", value=f"{discord_mention}\n{discord_username}", inline=True)
    embed.add_field(name="Key Used", value=f"```{key}```", inline=False)
    embed.add_field(name="Tamper Type", value=tamper_type, inline=True)
    embed.add_field(name="Detected At", value=f"<t:{detected_at}:F>", inline=True)
    embed.set_footer(text="Vyron.cc • Anti-Tamper System")

    await channel.send(embed=embed)


# ─────────────────────────────────────────────
#  KICK IN-GAME
# ─────────────────────────────────────────────

@tree.command(name="kickingame", description="Kick a specific key from in-game with a custom reason")
@app_commands.describe(key="The player's key", reason="Reason shown to them on kick")
async def kickingame(interaction: discord.Interaction, key: str, reason: str = "You have been kicked by staff."):
    if not has_owner_role(interaction):
        return await deny(interaction)
    await interaction.response.defer(ephemeral=True)

    ok = _send_kick(key.strip(), reason.strip())
    embed = discord.Embed(
        title="👢 In-Game Kick Queued" if ok else "❌ Kick Failed",
        color=0xFF6600 if ok else 0xFF4444,
    )
    embed.add_field(name="Key", value=f"`{key.strip()[:24]}{'…' if len(key) > 24 else ''}`", inline=True)
    embed.add_field(name="Reason", value=reason, inline=False)
    if ok:
        embed.set_footer(text="Vyron.cc • Will kick within 5 seconds")
    await interaction.followup.send(embed=embed, ephemeral=True)

    if ok:
        asyncio.create_task(send_webhook(embeds=[{
            "title": "👢 In-Game Kick",
            "color": 0xFF6600,
            "fields": [
                {"name": "By", "value": f"{interaction.user.mention} ({interaction.user})", "inline": True},
                {"name": "Key", "value": f"`{key.strip()}`", "inline": True},
                {"name": "Reason", "value": reason, "inline": False},
            ],
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }]))


# ─────────────────────────────────────────────
#  TELEPORT PLAYER
# ─────────────────────────────────────────────

def _get_location(key: str) -> dict | None:
    """Fetch the current place_id + job_id for a key. Returns None on failure."""
    dashboard_password = os.environ.get("DASHBOARD_PASSWORD", "vyron_admin")
    import urllib.request as _ur, urllib.parse as _up
    try:
        req = _ur.Request(
            f"{API_BASE}/location/{_up.quote(key.strip())}",
            method="GET",
            headers={"X-Admin-Password": dashboard_password},
        )
        with _ur.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read())
    except Exception:
        return None

def _send_teleport(key: str, place_id: str, job_id: str) -> bool:
    api_secret = os.environ.get("API_SECRET", "vyron_secret")
    import urllib.request as _ur
    try:
        payload = json.dumps({
            "key": key, "place_id": place_id,
            "job_id": job_id, "secret": api_secret,
        }).encode()
        req = _ur.Request(f"{API_BASE}/teleport", data=payload,
                          headers={"Content-Type": "application/json"}, method="POST")
        with _ur.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read()).get("success", False)
    except Exception:
        return False


@tree.command(name="teleportplayer", description="Teleport a player to you, or teleport yourself to them")
@app_commands.describe(
    target_key="The key of the player to move",
    direction="'tome' = bring them to you  |  'tothem' = send yourself to them",
    your_key="Your own key (required when direction is 'tothem')",
)
@app_commands.choices(direction=[
    app_commands.Choice(name="Bring them to me (tome)", value="tome"),
    app_commands.Choice(name="Send me to them (tothem)", value="tothem"),
])
async def teleportplayer(
    interaction: discord.Interaction,
    target_key: str,
    direction: app_commands.Choice[str],
    your_key: str = "",
):
    if not has_owner_role(interaction):
        return await deny(interaction)
    await interaction.response.defer(ephemeral=True)

    target_key = target_key.strip()
    your_key   = your_key.strip()

    # Get target's location
    target_loc = _get_location(target_key)
    if not target_loc or not target_loc.get("online"):
        await interaction.followup.send("❌ Target key is not in an active session.", ephemeral=True)
        return

    target_place = target_loc.get("place_id", "")
    target_job   = target_loc.get("job_id", "")

    if direction.value == "tothem":
        # Send YOUR key to their server
        if not your_key:
            await interaction.followup.send("❌ You must provide your own key when using 'tothem'.", ephemeral=True)
            return
        ok = _send_teleport(your_key, target_place, target_job)
        label = "You → Them"
        moved_key = your_key
        dest_key  = target_key
    else:
        # Bring THEM to your server — need your location
        if not your_key:
            await interaction.followup.send("❌ You must provide your own key so we know your server.", ephemeral=True)
            return
        your_loc = _get_location(your_key)
        if not your_loc or not your_loc.get("online"):
            await interaction.followup.send("❌ Your key is not in an active session.", ephemeral=True)
            return
        your_place = your_loc.get("place_id", "")
        your_job   = your_loc.get("job_id", "")
        ok = _send_teleport(target_key, your_place, your_job)
        label = "Them → You"
        moved_key = target_key
        dest_key  = your_key

    embed = discord.Embed(
        title=f"🌀 Teleport Queued ({label})" if ok else "❌ Teleport Failed",
        color=0x00CC66 if ok else 0xFF4444,
    )
    embed.add_field(name="Moving Key", value=f"`{moved_key[:24]}{'…' if len(moved_key) > 24 else ''}`", inline=True)
    embed.add_field(name="Destination Key", value=f"`{dest_key[:24]}{'…' if len(dest_key) > 24 else ''}`", inline=True)
    embed.add_field(name="Direction", value=label, inline=True)
    if ok:
        embed.set_footer(text="Vyron.cc • Will teleport within 5 seconds")
    await interaction.followup.send(embed=embed, ephemeral=True)


# ─────────────────────────────────────────────
#  FREEZE PLAYER  (notification spam loop)
# ─────────────────────────────────────────────

# ─────────────────────────────────────────────
#  FREEZE PLAYER
# ─────────────────────────────────────────────

# Track which keys we've frozen so unfreeze knows
_frozen_by_bot: set = set()

def _send_freeze(key: str) -> bool:
    api_secret = os.environ.get("API_SECRET", "vyron_secret")
    import urllib.request as _ur
    try:
        payload = json.dumps({"key": key, "secret": api_secret}).encode()
        req = _ur.Request(f"{API_BASE}/freeze", data=payload,
                          headers={"Content-Type": "application/json"}, method="POST")
        with _ur.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read()).get("success", False)
    except Exception:
        return False

def _send_unfreeze(key: str) -> bool:
    api_secret = os.environ.get("API_SECRET", "vyron_secret")
    import urllib.request as _ur
    try:
        payload = json.dumps({"key": key, "secret": api_secret}).encode()
        req = _ur.Request(f"{API_BASE}/unfreeze", data=payload,
                          headers={"Content-Type": "application/json"}, method="POST")
        with _ur.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read()).get("success", False)
    except Exception:
        return False


@tree.command(name="freezeplayer", description="Freeze a player in-game — sets their walkspeed to 1 until you unfreeze them")
@app_commands.describe(key="The player's key")
async def freezeplayer(interaction: discord.Interaction, key: str):
    if not has_owner_role(interaction):
        return await deny(interaction)

    key = key.strip()

    if key in _frozen_by_bot:
        await interaction.response.send_message("❌ That key is already frozen. Use `/unfreezeplayer` to unfreeze.", ephemeral=True)
        return

    ok = _send_freeze(key)
    if ok:
        _frozen_by_bot.add(key)

    embed = discord.Embed(
        title="🧊 Player Frozen" if ok else "❌ Freeze Failed",
        description="Their walkspeed is now set to **1** and will stay that way until you unfreeze them." if ok else "Could not reach the API.",
        color=0x88CCFF if ok else 0xFF4444,
    )
    embed.add_field(name="Key", value=f"`{key[:24]}{'…' if len(key) > 24 else ''}`", inline=True)
    if ok:
        embed.set_footer(text="Vyron.cc • Use /unfreezeplayer to restore")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="unfreezeplayer", description="Unfreeze a player and restore their normal walkspeed")
@app_commands.describe(key="The player's key to unfreeze")
async def unfreezeplayer(interaction: discord.Interaction, key: str):
    if not has_owner_role(interaction):
        return await deny(interaction)

    key = key.strip()

    if key not in _frozen_by_bot:
        await interaction.response.send_message("❌ That key is not currently frozen.", ephemeral=True)
        return

    ok = _send_unfreeze(key)
    if ok:
        _frozen_by_bot.discard(key)

    embed = discord.Embed(
        title="✅ Player Unfrozen" if ok else "❌ Unfreeze Failed",
        color=0x00CC66 if ok else 0xFF4444,
    )
    embed.add_field(name="Key", value=f"`{key[:24]}{'…' if len(key) > 24 else ''}`", inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ─────────────────────────────────────────────
#  SESSION INFO
# ─────────────────────────────────────────────

@tree.command(name="sessioninfo", description="Get detailed info on a specific key's active session")
@app_commands.describe(key="The key to look up")
async def sessioninfo(interaction: discord.Interaction, key: str):
    if not has_owner_role(interaction):
        return await deny(interaction)
    await interaction.response.defer(ephemeral=True)

    key = key.strip()
    loc = _get_location(key)

    if not loc:
        await interaction.followup.send("❌ Could not reach API.", ephemeral=True)
        return

    if not loc.get("online"):
        embed = discord.Embed(
            title="📡 Session Info",
            description=f"`{key[:24]}{'…' if len(key) > 24 else ''}` is **not online** right now.",
            color=0x555555,
        )
        embed.set_footer(text="Vyron.cc")
        await interaction.followup.send(embed=embed, ephemeral=True)
        return

    data = load_data()
    # Find Discord owner
    owner_uid = None
    for uid, keys in data.get("keys", {}).items():
        if key in keys:
            owner_uid = uid
            break
    if not owner_uid:
        for uid, keys in data.get("keys_internal", {}).items():
            if key in keys:
                owner_uid = uid
                break

    member = interaction.guild.get_member(int(owner_uid)) if owner_uid else None
    owner_str = member.mention if member else (f"<@{owner_uid}>" if owner_uid else "Unknown")

    last_seen = loc.get("last_seen", 0)
    place_id  = loc.get("place_id", "N/A")
    job_id    = loc.get("job_id", "N/A")

    # Key expiry
    expiry = data.get("key_expiry", {}).get(key)
    now = int(time.time())
    if expiry is None:
        expiry_str = "Lifetime"
    elif now > expiry:
        expiry_str = "⚠️ Expired"
    else:
        secs_left = expiry - now
        if secs_left < 3600:
            expiry_str = f"{secs_left // 60}m left"
        elif secs_left < 86400:
            expiry_str = f"{secs_left // 3600}h left"
        else:
            expiry_str = f"{secs_left // 86400}d left"

    # Roblox info
    roblox_info = data.get("key_roblox_info", {}).get(key, {})
    roblox_name = roblox_info.get("name", "N/A")
    roblox_id   = roblox_info.get("id", "N/A")

    # Executions
    executions = data.get("key_executions", {}).get(key, 0)

    embed = discord.Embed(
        title="📡 Session Info",
        color=0x00CC66,
    )
    embed.add_field(name="Key", value=f"```{key}```", inline=False)
    embed.add_field(name="Discord Owner", value=owner_str, inline=True)
    embed.add_field(name="Key Expiry", value=expiry_str, inline=True)
    embed.add_field(name="Last Seen", value=f"<t:{last_seen}:R>", inline=True)
    embed.add_field(name="Roblox Username", value=roblox_name, inline=True)
    embed.add_field(name="Roblox ID", value=roblox_id, inline=True)
    embed.add_field(name="Total Executions", value=str(executions), inline=True)
    embed.add_field(name="Place ID", value=f"`{place_id}`", inline=True)
    embed.add_field(name="Job ID", value=f"`{job_id[:20]}{'…' if len(str(job_id)) > 20 else ''}`", inline=True)
    embed.add_field(
        name="Server Link",
        value=f"[Join Server](https://www.roblox.com/games/{place_id}?gameInstanceId={job_id})" if place_id != "N/A" else "N/A",
        inline=False,
    )
    embed.set_footer(text="Vyron.cc")
    await interaction.followup.send(embed=embed, ephemeral=True)


# ============================================================================
# IMPROVED ACTIVESESSIONS WITH SEARCH + NEW COMMANDS
# ============================================================================

# Note: SessionsView and related classes were already updated above

# Replace activesessions function - delete lines 2883-2965 and use this instead
# This is the improved version with search functionality

# ============================================================================
# /RENEW COMMAND - Renew a key for the same duration it was originally for
# ============================================================================

@tree.command(name="renew", description="Renew a key for the same duration it was originally created for")
@app_commands.describe(key="The key to renew")
async def renew(interaction: discord.Interaction, key: str):
    if not has_owner_role(interaction):
        return await deny(interaction)
    
    key = key.strip()
    data = load_data()
    
    # Check if key exists
    key_found = False
    for uid, keys in list(data.get("keys", {}).items()) + list(data.get("keys_internal", {}).items()):
        if key in keys:
            key_found = True
            break
    
    if not key_found:
        await interaction.response.send_message(f"❌ Key not found: `{key}`", ephemeral=True)
        return
    
    # Get key creation time and expiry
    key_created = data.get("key_created", {}).get(key)
    key_expiry = data.get("key_expiry", {}).get(key)
    
    if key_expiry is None:
        await interaction.response.send_message(
            f"❌ This key is a **lifetime** key and doesn't need renewal.",
            ephemeral=True
        )
        return
    
    if not key_created:
        await interaction.response.send_message(
            f"❌ Cannot determine original duration for this key (missing creation timestamp).",
            ephemeral=True
        )
        return
    
    # Calculate original duration
    original_duration = key_expiry - key_created
    
    # Renew: set new expiry to now + original duration
    now = int(time.time())
    new_expiry = now + original_duration
    
    data.setdefault("key_expiry", {})[key] = new_expiry
    save_data(data)
    
    # Format duration for display
    duration_str = duration_label(original_duration)
    
    embed = discord.Embed(
        title="🔄 Key Renewed",
        description=f"Key has been renewed for its original duration.",
        color=0x00CC66
    )
    embed.add_field(name="Key", value=f"```{key}```", inline=False)
    embed.add_field(name="Duration", value=duration_str, inline=True)
    embed.add_field(name="New Expiry", value=f"<t:{new_expiry}:R>", inline=True)
    embed.set_footer(text="Vyron.cc")
    
    await interaction.response.send_message(embed=embed, ephemeral=True)
    
    # Webhook notification
    asyncio.create_task(send_webhook(embeds=[{
        "title": "🔄 Key Renewed",
        "color": 0x00CC66,
        "fields": [
            {"name": "Renewed by", "value": f"{interaction.user.mention} ({interaction.user})", "inline": True},
            {"name": "Duration", "value": duration_str, "inline": True},
            {"name": "Key", "value": f"```{key}```", "inline": False},
        ],
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }]))


# ============================================================================
# /KEYUSAGE - Show execution statistics
# ============================================================================

@tree.command(name="keyusage", description="Show execution statistics and analytics for all keys")
async def keyusage(interaction: discord.Interaction):
    if not has_owner_role(interaction):
        return await deny(interaction)
    
    await interaction.response.defer(ephemeral=True)
    
    data = load_data()
    key_executions = data.get("key_executions", {})
    key_last_exec = data.get("key_last_exec", {})
    
    if not key_executions:
        await interaction.followup.send("No execution data available yet.", ephemeral=True)
        return
    
    # Sort by execution count
    sorted_keys = sorted(key_executions.items(), key=lambda x: x[1], reverse=True)
    
    total_executions = sum(key_executions.values())
    unique_keys = len(key_executions)
    
    embed = discord.Embed(
        title="📊 Key Usage Analytics",
        description=f"```ansi\n\u001b[1;32m● {total_executions} total executions\u001b[0m\n\u001b[1;32m● {unique_keys} unique keys\u001b[0m\n```",
        color=0x5080FF
    )
    
    # Top 10 most used keys
    for i, (key, count) in enumerate(sorted_keys[:10], 1):
        last_exec = key_last_exec.get(key, 0)
        if last_exec:
            last_str = f"<t:{last_exec}:R>"
        else:
            last_str = "Never"
        
        # Find owner
        owner_uid = None
        for uid, keys in list(data.get("keys", {}).items()) + list(data.get("keys_internal", {}).items()):
            if key in keys:
                owner_uid = uid
                break
        
        if owner_uid:
            member = interaction.guild.get_member(int(owner_uid))
            owner_str = member.mention if member else f"<@{owner_uid}>"
        else:
            owner_str = "Unknown"
        
        embed.add_field(
            name=f"#{i} • {count} executions",
            value=f"🔑 `{key[:24]}...`\n👤 {owner_str}\n🕐 Last: {last_str}",
            inline=False
        )
    
    embed.set_footer(text="Vyron.cc • Top 10 most executed keys")
    embed.timestamp = discord.utils.utcnow()
    
    await interaction.followup.send(embed=embed, ephemeral=True)


# ============================================================================
# /SERVERSTATS - Show which Roblox games are most popular
# ============================================================================

@tree.command(name="serverstats", description="Show which Roblox games have the most active Vyron users")
async def serverstats(interaction: discord.Interaction):
    if not has_owner_role(interaction):
        return await deny(interaction)
    
    await interaction.response.defer(ephemeral=True)
    
    try:
        req = urllib.request.Request(f"{API_BASE}/sessions", method="GET")
        with urllib.request.urlopen(req, timeout=5) as resp:
            sessions = json.loads(resp.read())
    except Exception as e:
        await interaction.followup.send(f"❌ Could not reach API: `{e}`", ephemeral=True)
        return
    
    if not sessions:
        await interaction.followup.send("No active sessions right now.", ephemeral=True)
        return
    
    # Count by place_id
    place_counts = {}
    for s in sessions:
        place_id = s.get("place_id", "")
        if place_id:
            place_counts[place_id] = place_counts.get(place_id, 0) + 1
    
    if not place_counts:
        await interaction.followup.send("No location data available for active sessions.", ephemeral=True)
        return
    
    # Sort by count
    sorted_places = sorted(place_counts.items(), key=lambda x: x[1], reverse=True)
    
    embed = discord.Embed(
        title="🌐 Server Statistics",
        description=f"```ansi\n\u001b[1;32m● {len(sessions)} total sessions\u001b[0m\n\u001b[1;32m● {len(place_counts)} unique games\u001b[0m\n```",
        color=0x5080FF
    )
    
    for i, (place_id, count) in enumerate(sorted_places[:10], 1):
        percentage = (count / len(sessions)) * 100
        embed.add_field(
            name=f"#{i} • {count} player(s) ({percentage:.1f}%)",
            value=f"🎮 Place ID: `{place_id}`",
            inline=False
        )
    
    embed.set_footer(text="Vyron.cc • Top 10 most popular games")
    embed.timestamp = discord.utils.utcnow()
    
    await interaction.followup.send(embed=embed, ephemeral=True)


# Start API server first, then try to start bot
if __name__ == "__main__":
    # Always start the API server first
    print("Starting API server...")
    start_api_thread()
    
    # Give API a moment to start
    import time
    time.sleep(2)
    
    # Try to start the bot with error handling
    try:
        print("Starting Discord bot...")
        client.run(TOKEN)
    except Exception as e:
        print(f"Bot failed to start: {e}")
        print("API server will continue running...")
        
        # Keep the API running even if bot fails
        try:
            from api import run_api
            print("Running API server directly...")
            run_api()
        except KeyboardInterrupt:
            print("Shutting down...")
        except Exception as api_error:
            print(f"API server error: {api_error}")
else:
    # If imported, just run the bot normally
    client.run(TOKEN)
