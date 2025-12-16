import discord
from discord import app_commands
from discord.ext import tasks
import requests
import base64
import json
import os
import random
import asyncio
import time
import pytz
from flask import Flask
from threading import Thread

# =========================================================
# CONFIG
# =========================================================

TOKEN = os.getenv("WC_TOKEN") or os.getenv("TOKEN")

UK_TZ = pytz.timezone("Europe/London")

GITHUB_REPO = os.getenv("GITHUB_REPO", "saraargh/LSworldcup")
GITHUB_FILE_PATH = os.getenv("TOURNAMENT_JSON_PATH", "tournament_data.json")
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main")

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")  # your PAT
HEADERS = {"Authorization": f"token {GITHUB_TOKEN}"} if GITHUB_TOKEN else {}

ALLOWED_ROLE_IDS = [
    1413545658006110401,  # William/Admin
    1404098545006546954,  # serversorter
    1420817462290681936,  # kd
    1404105470204969000,  # greg
    1404104881098195015   # sazzles
]

AUTO_WARN_SECONDS = 23 * 60 * 60
AUTO_LOCK_SECONDS = 24 * 60 * 60

VOTE_A = "🔴"
VOTE_B = "🔵"

STAGE_BY_COUNT = {
    32: "Round of 32",
    16: "Round of 16",
    8:  "Quarter Finals",
    4:  "Semi Finals",
    2:  "Finals"
}

MAX_ITEMS = 32

# =========================================================
# DEFAULT DATA
# =========================================================

DEFAULT_DATA = {
    "items": [],
    "current_round": [],
    "next_round": [],
    "scores": {},
    "running": False,
    "title": "",
    "last_winner": None,
    "last_match": None,
    "finished_matches": [],
    "round_stage": "",
    "item_authors": {},  # item -> user_id (str)
    "user_items": {},    # user_id -> item
    "cup_history": []    # list of {title, winner, author_id, timestamp}
}

# =========================================================
# GITHUB HELPERS
# =========================================================

def _gh_url():
    return f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_FILE_PATH}"

def load_data():
    """
    Load JSON from GitHub at the configured branch.
    Ensures required keys exist and types are sane.
    """
    try:
        r = requests.get(_gh_url(), headers=HEADERS, params={"ref": GITHUB_BRANCH}, timeout=15)

        if r.status_code == 200:
            content = r.json()
            raw = base64.b64decode(content["content"]).decode()
            data = json.loads(raw) if raw.strip() else DEFAULT_DATA.copy()
            sha = content.get("sha")

            # ensure keys
            for k in DEFAULT_DATA:
                if k not in data:
                    data[k] = DEFAULT_DATA[k]

            # ensure types
            if not isinstance(data.get("items"), list):
                data["items"] = []
            if not isinstance(data.get("scores"), dict):
                data["scores"] = {}
            if not isinstance(data.get("item_authors"), dict):
                data["item_authors"] = {}
            if not isinstance(data.get("user_items"), dict):
                data["user_items"] = {}
            if not isinstance(data.get("cup_history"), list):
                data["cup_history"] = []

            return data, sha

        if r.status_code == 404:
            sha = save_data(DEFAULT_DATA.copy(), sha=None)
            return DEFAULT_DATA.copy(), sha

        print(f"[GitHub] load_data unexpected status: {r.status_code} {r.text}")
        return DEFAULT_DATA.copy(), None

    except Exception as e:
        print("[GitHub] load_data error:", e)
        return DEFAULT_DATA.copy(), None

def save_data(data, sha=None):
    """
    Save JSON to GitHub at the configured branch.
    """
    try:
        payload = {
            "message": "Update tournament data",
            "content": base64.b64encode(json.dumps(data, indent=4).encode()).decode(),
            "branch": GITHUB_BRANCH
        }
        if sha:
            payload["sha"] = sha

        r = requests.put(_gh_url(), headers=HEADERS, data=json.dumps(payload), timeout=15)
        if r.status_code in (200, 201):
            return r.json().get("content", {}).get("sha")

        print(f"[GitHub] save_data unexpected status: {r.status_code} {r.text}")
        return sha

    except Exception as e:
        print("[GitHub] save_data error:", e)
        return sha

# =========================================================
# UTILITIES
# =========================================================

def user_allowed(member: discord.Member):
    return any(role.id in ALLOWED_ROLE_IDS for role in member.roles)

async def _remove_invalid_reactions(msg: discord.Message):
    """
    Removes any reactions that are NOT 🔴 or 🔵 (for all non-bot users).
    Requires the bot to have Manage Messages permission.
    """
    for reaction in msg.reactions:
        emoji = str(reaction.emoji)
        if emoji in (VOTE_A, VOTE_B):
            continue
        try:
            async for u in reaction.users():
                if u.bot:
                    continue
                try:
                    await reaction.remove(u)
                except Exception:
                    pass
        except Exception:
            pass

async def count_votes_from_message(guild: discord.Guild, channel_id: int, message_id: int):
    """
    Returns: a_count, b_count, a_names(dict), b_names(dict)
    Enforces "single vote rule" by removing dupes from BOTH counts.
    Also strips invalid reactions automatically.
    """
    try:
        channel = guild.get_channel(channel_id)
        if channel is None:
            return 0, 0, {}, {}

        msg = await channel.fetch_message(message_id)

        # clean invalid reactions
        await _remove_invalid_reactions(msg)

    except Exception:
        return 0, 0, {}, {}

    a_users, b_users = set(), set()
    a_names, b_names = {}, {}

    for reaction in msg.reactions:
        emoji = str(reaction.emoji)
        if emoji not in (VOTE_A, VOTE_B):
            continue

        try:
            users = [u async for u in reaction.users()]
        except Exception:
            users = []

        for u in users:
            if u.bot:
                continue

            if emoji == VOTE_A:
                a_users.add(u.id)
                a_names[u.id] = u.display_name
            else:
                b_users.add(u.id)
                b_names[u.id] = u.display_name

    # single vote rule: remove from BOTH
    dupes = a_users & b_users
    for uid in dupes:
        a_users.discard(uid)
        b_users.discard(uid)
        a_names.pop(uid, None)
        b_names.pop(uid, None)

    return len(a_users), len(b_users), a_names, b_names

def _spaced(*lines: str) -> str:
    """
    Creates nice spacing between sections in the embed.
    """
    return "\n\n".join([ln for ln in lines if ln is not None])

def _names_block(names: dict) -> str:
    if not names:
        return "_No votes yet_"
    # keep it tidy
    return "\n".join(f"• {n}" for n in list(names.values())[:25])

# =========================================================
# DISCORD CLIENT
# =========================================================

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

class WorldCupBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync()

client = WorldCupBot()

# =========================================================
# MATCH FLOW (LOCK / AUTO-LOCK / POST / LIVE UPDATES)
# =========================================================

async def _lock_match(
    guild: discord.Guild,
    channel: discord.TextChannel,
    data,
    sha,
    reason: str,
    ping_everyone: bool,
    reply_msg: discord.Message | None
):
    """
    Locks the current match and snapshots vote counts.
    Adds a lock badge to the embed.
    """
    lm = data.get("last_match")
    if not lm or lm.get("locked"):
        return data, sha

    a_votes, b_votes, _, _ = await count_votes_from_message(
        guild, lm["channel_id"], lm["message_id"]
    )

    lm["locked"] = True
    lm["locked_at"] = int(time.time())
    lm["locked_counts"] = {"a": a_votes, "b": b_votes}
    lm["lock_reason"] = reason

    sha = save_data(data, sha)

    # Edit embed to show lock badge
    try:
        msg = await channel.fetch_message(lm["message_id"])
        if msg.embeds:
            emb = msg.embeds[0]
            new = discord.Embed(
                title=emb.title,
                description=(emb.description or "") + "\n\n🔒 **Voting closed**",
                color=emb.color if emb.color else discord.Color.dark_grey()
            )
            if emb.footer and emb.footer.text:
                new.set_footer(text=emb.footer.text)
            await msg.edit(embed=new)
    except Exception:
        pass

    # Announce lock
    try:
        ping = "@everyone " if ping_everyone else ""
        text = f"{ping}🔒 **Voting is now closed.** ({reason})"
        if reply_msg:
            await reply_msg.reply(text)
        else:
            await channel.send(text)
    except Exception:
        pass

    return data, sha

async def _schedule_auto_lock(channel: discord.TextChannel, message_id: int):
    """
    Warn at 23h, lock at 24h (with @everyone).
    """
    try:
        await asyncio.sleep(AUTO_WARN_SECONDS)
        data, sha = load_data()
        lm = data.get("last_match")
        if not lm or lm.get("message_id") != message_id or lm.get("locked"):
            return

        try:
            msg = await channel.fetch_message(message_id)
            await msg.reply("@everyone ⏰ **Voting closes soon!** (auto-lock at 24h)")
        except Exception:
            await channel.send("@everyone ⏰ **Voting closes soon!** (auto-lock at 24h)")

        await asyncio.sleep(max(0, AUTO_LOCK_SECONDS - AUTO_WARN_SECONDS))
        data, sha = load_data()
        lm = data.get("last_match")
        if not lm or lm.get("message_id") != message_id or lm.get("locked"):
            return

        try:
            reply_msg = await channel.fetch_message(message_id)
        except Exception:
            reply_msg = None

        await _lock_match(
            guild=channel.guild,
            channel=channel,
            data=data,
            sha=sha,
            reason="Auto-locked after 24h",
            ping_everyone=True,
            reply_msg=reply_msg
        )

    except Exception as e:
        print("Auto-lock scheduler error:", e)

async def _build_merged_match_embed(
    data: dict,
    current_a: str,
    current_b: str,
    a_count: int,
    b_count: int,
    a_names: dict,
    b_names: dict,
    prev: dict | None,
    locked: bool
) -> discord.Embed:
    stage = data.get("round_stage", "Matchup")

    prev_block = ""
    if prev:
        prev_block = _spaced(
            "🏆 **Previous Match**",
            f"**{prev['winner']}** won",
            f"{VOTE_A} {prev['a']} — **{prev['a_votes']}**\n{VOTE_B} {prev['b']} — **{prev['b_votes']}**",
        )

    current_block = _spaced(
        "🎮 **Current Match**",
        f"{VOTE_A} **{current_a}** — **{a_count}** votes\n{_names_block(a_names)}",
        f"{VOTE_B} **{current_b}** — **{b_count}** votes\n{_names_block(b_names)}",
    )

    footer_bits = ["⏰ Auto-lock in 24h"]
    if locked:
        footer_bits.append("🔒 Locked")

    description = _spaced(
        prev_block if prev else None,
        "────────────",
        current_block,
        "────────────",
        " • ".join(footer_bits)
    )

    if locked:
        description += "\n\n🔒 **Voting closed**"

    embed = discord.Embed(
        title=f"🎮 {stage}",
        description=description,
        color=discord.Color.random()
    )
    return embed

async def post_next_match(channel: discord.TextChannel, data, sha, prev_result: dict | None = None):
    """
    Posts the next matchup and starts live reaction updating + auto-lock.
    """
    if len(data["current_round"]) < 2:
        return sha

    a = data["current_round"].pop(0)
    b = data["current_round"].pop(0)
    sha = save_data(data, sha)

    # initial empty state
    embed = await _build_merged_match_embed(
        data=data,
        current_a=a,
        current_b=b,
        a_count=0,
        b_count=0,
        a_names={},
        b_names={},
        prev=prev_result,
        locked=False
    )

    msg = await channel.send(embed=embed)
    await msg.add_reaction(VOTE_A)
    await msg.add_reaction(VOTE_B)

    data["last_match"] = {
        "a": a,
        "b": b,
        "message_id": msg.id,
        "channel_id": channel.id,
        "locked": False,
        "locked_at": None,
        "locked_counts": None,
        "lock_reason": None
    }
    sha = save_data(data, sha)

    asyncio.create_task(_schedule_auto_lock(channel, msg.id))

    # Live update loop
    def check(reaction: discord.Reaction, user: discord.User):
        return (
            not user.bot and
            reaction.message.id == msg.id
        )

    async def reaction_loop():
        while True:
            latest, _ = load_data()
            lm = latest.get("last_match")
            if not lm or lm.get("message_id") != msg.id:
                return
            if lm.get("locked"):
                # show locked embed one last time (with badge)
                try:
                    a_count = int(lm.get("locked_counts", {}).get("a", 0))
                    b_count = int(lm.get("locked_counts", {}).get("b", 0))
                    a_names = {}
                    b_names = {}
                    locked_embed = await _build_merged_match_embed(
                        data=latest,
                        current_a=a,
                        current_b=b,
                        a_count=a_count,
                        b_count=b_count,
                        a_names=a_names,
                        b_names=b_names,
                        prev=prev_result,
                        locked=True
                    )
                    await msg.edit(embed=locked_embed)
                except Exception:
                    pass
                return

            try:
                await client.wait_for("reaction_add", check=check)
            except Exception:
                continue

            try:
                # refresh from message each update
                a_count, b_count, a_names, b_names = await count_votes_from_message(
                    channel.guild, msg.channel.id, msg.id
                )

                fresh = await _build_merged_match_embed(
                    data=latest,
                    current_a=a,
                    current_b=b,
                    a_count=a_count,
                    b_count=b_count,
                    a_names=a_names,
                    b_names=b_names,
                    prev=prev_result,
                    locked=False
                )
                await msg.edit(embed=fresh)

            except Exception:
                continue

    asyncio.create_task(reaction_loop())
    return sha
    
# =========================================================
# COMMANDS
# =========================================================

@client.tree.command(name="addwcitem", description="Add item(s) to the World Cup")
@app_commands.describe(items="Comma-separated list")
async def addwcitem(interaction: discord.Interaction, items: str):
    await interaction.response.defer(ephemeral=True)

    data, sha = load_data()
    staff = user_allowed(interaction.user)
    uid = str(interaction.user.id)

    # Non-staff: only ONE item total ever
    if not staff and uid in data.get("user_items", {}):
        return await interaction.followup.send("You can only add **one** item to the World Cup.", ephemeral=True)

    # Split
    items_in = [x.strip() for x in items.split(",") if x.strip()]
    if not items_in:
        return await interaction.followup.send("Give me at least one item.", ephemeral=True)

    # Non-staff: must submit exactly one at a time
    if not staff and len(items_in) != 1:
        return await interaction.followup.send("You can only add **one** item at a time.", ephemeral=True)

    # Enforce hard cap (32 total)
    if len(data.get("items", [])) >= MAX_ITEMS:
        return await interaction.followup.send("❌ Item list is already full (32/32).", ephemeral=True)

    added = []
    for it in items_in:
        # stop if we hit cap mid-loop
        if len(data["items"]) >= MAX_ITEMS:
            break

        if it not in data["items"]:
            data["items"].append(it)
            data["scores"].setdefault(it, 0)
            data.setdefault("item_authors", {})
            data.setdefault("user_items", {})
            data["item_authors"][it] = uid
            if not staff:
                data["user_items"][uid] = it
            added.append(it)

    sha = save_data(data, sha)

    if added:
        return await interaction.followup.send(f"✅ Added: {', '.join(added)}", ephemeral=True)

    return await interaction.followup.send("⚠️ Nothing added (duplicates or list is full).", ephemeral=True)


@client.tree.command(name="removewcitem", description="Remove item(s) (staff only, case-insensitive)")
@app_commands.describe(items="Comma-separated list")
async def removewcitem(interaction: discord.Interaction, items: str):
    await interaction.response.defer(ephemeral=True)

    if not user_allowed(interaction.user):
        return await interaction.followup.send("❌ No permission.", ephemeral=True)

    data, sha = load_data()
    removed = []

    lower_map = {i.lower(): i for i in data["items"]}
    for it in [x.strip() for x in items.split(",") if x.strip()]:
        key = it.lower()
        if key in lower_map:
            original = lower_map[key]
            data["items"].remove(original)
            data["scores"].pop(original, None)

            author_id = data.get("item_authors", {}).pop(original, None)
            if author_id and data.get("user_items", {}).get(str(author_id)) == original:
                data["user_items"].pop(str(author_id), None)

            removed.append(original)

    save_data(data, sha)

    if removed:
        return await interaction.followup.send(f"✅ Removed: {', '.join(removed)}", ephemeral=True)
    return await interaction.followup.send("⚠️ Nothing removed.", ephemeral=True)


@client.tree.command(name="listwcitems", description="List World Cup items (public, paginated)")
async def listwcitems(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=False)

    data, _ = load_data()
    items = data.get("items", [])
    if not items:
        return await interaction.followup.send("No items added yet.", ephemeral=False)

    pages = [items[i:i+10] for i in range(0, len(items), 10)]
    page = 0

    def make_embed(p: int):
        e = discord.Embed(
            title="📋 World Cup Items",
            description="\n".join(f"{i+1+p*10}. {v}" for i, v in enumerate(pages[p])),
            color=discord.Color.blue()
        )
        e.set_footer(text=f"Page {p+1}/{len(pages)} • {len(items)}/{MAX_ITEMS} items")
        return e

    await interaction.followup.send(embed=make_embed(0), ephemeral=False)
    msg = await interaction.original_response()

    if len(pages) == 1:
        return

    await msg.add_reaction("⬅️")
    await msg.add_reaction("➡️")

    def check(r: discord.Reaction, u: discord.User):
        return (u == interaction.user and r.message.id == msg.id and str(r.emoji) in ("⬅️", "➡️"))

    while True:
        try:
            r, u = await client.wait_for("reaction_add", timeout=60.0, check=check)
            if str(r.emoji) == "➡️" and page < len(pages) - 1:
                page += 1
            elif str(r.emoji) == "⬅️" and page > 0:
                page -= 1

            await msg.edit(embed=make_embed(page))
            try:
                await msg.remove_reaction(r.emoji, u)
            except Exception:
                pass
        except asyncio.TimeoutError:
            break


@client.tree.command(name="closematch", description="Lock the current match (staff only)")
async def closematch(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    if not user_allowed(interaction.user):
        return await interaction.followup.send("❌ No permission.", ephemeral=True)

    data, sha = load_data()
    lm = data.get("last_match")
    if not lm:
        return await interaction.followup.send("No active match.", ephemeral=True)

    try:
        reply_msg = await interaction.channel.fetch_message(lm["message_id"])
    except Exception:
        reply_msg = None

    await _lock_match(
        guild=interaction.guild,
        channel=interaction.channel,
        data=data,
        sha=sha,
        reason=f"Closed by {interaction.user.display_name}",
        ping_everyone=False,
        reply_msg=reply_msg
    )

    return await interaction.followup.send("🔒 Match locked.", ephemeral=True)


@client.tree.command(name="startwc", description="Start World Cup (staff only, requires 32 items)")
@app_commands.describe(title="World Cup title")
async def startwc(interaction: discord.Interaction, title: str):
    await interaction.response.defer(ephemeral=True)

    if not user_allowed(interaction.user):
        return await interaction.followup.send("❌ No permission.", ephemeral=True)

    data, sha = load_data()
    if data.get("running"):
        return await interaction.followup.send("❌ Already running.", ephemeral=True)

    if len(data.get("items", [])) != MAX_ITEMS:
        return await interaction.followup.send(f"❌ Must have exactly {MAX_ITEMS} items.", ephemeral=True)

    data.update({
        "title": title,
        "current_round": random.sample(data["items"], len(data["items"])),
        "next_round": [],
        "finished_matches": [],
        "last_match": None,
        "last_winner": None,
        "running": True,
        "round_stage": STAGE_BY_COUNT.get(32, "Round of 32")
    })

    sha = save_data(data, sha)

    await interaction.channel.send(f"@everyone World Cup **{title}** has begun 🏆")
    await post_next_match(interaction.channel, data, sha, prev_result=None)

    return await interaction.followup.send("✅ Tournament started.", ephemeral=True)


@client.tree.command(name="nextwcround", description="Process current match / advance rounds (staff only)")
async def nextwcround(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    if not user_allowed(interaction.user):
        return await interaction.followup.send("❌ No permission.", ephemeral=True)

    data, sha = load_data()
    if not data.get("running"):
        return await interaction.followup.send("❌ No active tournament.", ephemeral=True)

    guild = interaction.guild
    prev_result = None

    # 1) Process last match (even if locked)
    if data.get("last_match"):
        lm = data["last_match"]

        if lm.get("locked") and isinstance(lm.get("locked_counts"), dict):
            a_votes = int(lm["locked_counts"].get("a", 0))
            b_votes = int(lm["locked_counts"].get("b", 0))
        else:
            a_votes, b_votes, _, _ = await count_votes_from_message(
                guild, lm["channel_id"], lm["message_id"]
            )

        a = lm["a"]
        b = lm["b"]

        if a_votes > b_votes:
            winner = a
        elif b_votes > a_votes:
            winner = b
        else:
            winner = random.choice([a, b])

        prev_result = {
            "a": a,
            "b": b,
            "winner": winner,
            "a_votes": a_votes,
            "b_votes": b_votes
        }

        data["finished_matches"].append(prev_result)
        data["next_round"].append(winner)
        data["scores"][winner] = data["scores"].get(winner, 0) + 1

        data["last_match"] = None
        data["last_winner"] = winner
        sha = save_data(data, sha)

        # If we can immediately post the next match
        if len(data.get("current_round", [])) >= 2:
            await post_next_match(interaction.channel, data, sha, prev_result=prev_result)
            return await interaction.followup.send("✔ Match processed.", ephemeral=True)

        # Otherwise we need a second /nextwcround to advance stage
        return await interaction.followup.send("✔ Match processed. Run again to advance the round.", ephemeral=True)

    # 2) Promote to next round
    if not data.get("current_round") and data.get("next_round"):
        prev_stage = data.get("round_stage") or "Round"
        data["current_round"] = data["next_round"].copy()
        data["next_round"] = []

        new_len = len(data["current_round"])
        data["round_stage"] = STAGE_BY_COUNT.get(new_len, f"{new_len}-items round")
        sha = save_data(data, sha)

        await interaction.channel.send(
            f"✅ **{prev_stage} complete!** Now entering **{data['round_stage']}**."
        )

        if new_len >= 2:
            await post_next_match(interaction.channel, data, sha, prev_result=None)

        # finals reached with 1 left
        if new_len == 1:
            data["last_winner"] = data["current_round"][0]
            sha = save_data(data, sha)

        return await interaction.followup.send("🔁 Advanced to next round.", ephemeral=True)

    # 3) Nothing to do
    return await interaction.followup.send("⚠ Nothing to process.", ephemeral=True)


@client.tree.command(name="scoreboard", description="Show tournament progress (public, paginated)")
async def scoreboard(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=False)

    data, _ = load_data()
    finished = data.get("finished_matches", [])
    current = data.get("last_match")
    remaining = data.get("current_round", [])

    finished_lines = []
    for i, f in enumerate(finished):
        finished_lines.append(
            f"{i+1}. {f['a']} vs {f['b']} → **{f['winner']}** "
            f"({VOTE_A} {f['a_votes']} | {VOTE_B} {f['b_votes']})"
        )
    if not finished_lines:
        finished_lines = ["No matches played yet."]

    finished_pages = [finished_lines[i:i+10] for i in range(0, len(finished_lines), 10)]

    if current:
        locked = " 🔒" if current.get("locked") else ""
        current_line = f"{current['a']} vs {current['b']} (voting now){locked}"
    else:
        current_line = "None"

    upcoming_lines = []
    for i in range(0, len(remaining), 2):
        if i + 1 < len(remaining):
            upcoming_lines.append(f"• {remaining[i]} vs {remaining[i+1]}")
        else:
            upcoming_lines.append(f"• {remaining[i]} (auto-advance)")
    if not upcoming_lines:
        upcoming_lines = ["None"]

    upcoming_chunks = []
    chunk = []
    length = 0
    for line in upcoming_lines:
        if length + len(line) + 1 > 900:
            upcoming_chunks.append(chunk)
            chunk = []
            length = 0
        chunk.append(line)
        length += len(line) + 1
    if chunk:
        upcoming_chunks.append(chunk)

    page = 0
    total_pages = max(len(finished_pages), len(upcoming_chunks))

    def make_embed(page_index: int):
        embed = discord.Embed(title="🏆 World Cup Scoreboard", color=discord.Color.teal())
        embed.add_field(name="Tournament", value=data.get("title") or "No title", inline=False)
        embed.add_field(name="Stage", value=data.get("round_stage") or "N/A", inline=False)
        embed.add_field(name="Current Match", value=current_line, inline=False)
        embed.add_field(
            name="Finished Matches",
            value="\n".join(finished_pages[min(page_index, len(finished_pages)-1)]),
            inline=False
        )
        embed.add_field(
            name="Upcoming Matchups",
            value="\n".join(upcoming_chunks[min(page_index, len(upcoming_chunks)-1)]),
            inline=False
        )
        embed.set_footer(text=f"Page {page_index+1}/{total_pages}")
        return embed

    await interaction.followup.send(embed=make_embed(0), ephemeral=False)
    msg = await interaction.original_response()

    if total_pages > 1:
        await msg.add_reaction("⬅️")
        await msg.add_reaction("➡️")

    def check(reaction, user):
        return user == interaction.user and reaction.message.id == msg.id and str(reaction.emoji) in ("⬅️", "➡️")

    while total_pages > 1:
        try:
            reaction, user = await client.wait_for("reaction_add", timeout=60.0, check=check)
            if str(reaction.emoji) == "➡️" and page < total_pages - 1:
                page += 1
            elif str(reaction.emoji) == "⬅️" and page > 0:
                page -= 1

            await msg.edit(embed=make_embed(page))
            try:
                await msg.remove_reaction(reaction.emoji, user)
            except Exception:
                pass
        except asyncio.TimeoutError:
            break


@client.tree.command(name="resetwc", description="Reset the tournament (staff only). History is NOT deleted.")
async def resetwc(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    if not user_allowed(interaction.user):
        return await interaction.followup.send("❌ No permission.", ephemeral=True)

    data, sha = load_data()
    history = data.get("cup_history", [])

    fresh = DEFAULT_DATA.copy()
    fresh["cup_history"] = history

    save_data(fresh, sha)
    return await interaction.followup.send("🔄 Reset complete (history kept).", ephemeral=True)


@client.tree.command(name="endwc", description="Announce the winner & end the tournament (staff only) + save history")
async def endwc(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    data, sha = load_data()

    if not user_allowed(interaction.user):
        return await interaction.followup.send("❌ No permission.", ephemeral=True)

    if not data.get("running"):
        return await interaction.followup.send("❌ No active tournament.", ephemeral=True)

    winner = data.get("last_winner")
    if not winner:
        return await interaction.followup.send(
            "⚠ No winner recorded. Run `/nextwcround` until finals are done.",
            ephemeral=True
        )

    author_id = data.get("item_authors", {}).get(winner)
    added_by_text = f"<@{author_id}>" if author_id else "Unknown"

    entry = {
        "title": data.get("title") or "Untitled",
        "winner": winner,
        "author_id": author_id,
        "timestamp": int(time.time())
    }
    data.setdefault("cup_history", [])
    data["cup_history"].append(entry)

    await interaction.channel.send("@everyone We have a World Cup Winner‼️🎉🏆")

    embed = discord.Embed(
        title="🎉 World Cup Winner!",
        description=(
            f"🏆 **{winner}** wins the World Cup of **{data.get('title')}**!\n\n"
            f"✨ Added by: {added_by_text}"
        ),
        color=discord.Color.green()
    )
    embed.set_image(url="https://cdn.discordapp.com/attachments/1444274467864838207/1449046416453271633/IMG_8499.gif")
    await interaction.channel.send(embed=embed)

    data["running"] = False
    save_data(data, sha)

    return await interaction.followup.send("✔ Winner announced + saved to history.", ephemeral=True)


@client.tree.command(name="cuphistory", description="View past World Cups (public, paginated)")
async def cuphistory(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=False)

    data, _ = load_data()
    hist = data.get("cup_history", [])
    if not hist:
        return await interaction.followup.send("No history yet.", ephemeral=False)

    hist = list(reversed(hist))
    pages = [hist[i:i+5] for i in range(0, len(hist), 5)]
    page = 0
    total = len(pages)

    def make_embed(p: int):
        e = discord.Embed(title="📜 World Cup History", color=discord.Color.blurple())
        for h in pages[p]:
            title = h.get("title") or "Untitled"
            winner = h.get("winner") or "Unknown"
            author = h.get("author_id")
            author_txt = f"<@{author}>" if author else "Unknown"
            ts = h.get("timestamp")
            when = f"<t:{int(ts)}:D>" if ts else "Unknown date"
            e.add_field(
                name=title,
                value=f"🏆 **{winner}**\n✨ Added by: {author_txt}\n🕒 {when}",
                inline=False
            )
        e.set_footer(text=f"Page {p+1}/{total}")
        return e

    await interaction.followup.send(embed=make_embed(0), ephemeral=False)
    msg = await interaction.original_response()

    if total > 1:
        await msg.add_reaction("⬅️")
        await msg.add_reaction("➡️")

    def check(reaction, user):
        return user == interaction.user and reaction.message.id == msg.id and str(reaction.emoji) in ("⬅️", "➡️")

    while total > 1:
        try:
            reaction, user = await client.wait_for("reaction_add", timeout=60.0, check=check)
            if str(reaction.emoji) == "➡️" and page < total - 1:
                page += 1
            elif str(reaction.emoji) == "⬅️" and page > 0:
                page -= 1

            await msg.edit(embed=make_embed(page))
            try:
                await msg.remove_reaction(reaction.emoji, user)
            except Exception:
                pass
        except asyncio.TimeoutError:
            break


@client.tree.command(name="deletehistory", description="Delete a cup from history by title (staff only)")
@app_commands.describe(title="Exact title to delete")
async def deletehistory(interaction: discord.Interaction, title: str):
    await interaction.response.defer(ephemeral=True)

    if not user_allowed(interaction.user):
        return await interaction.followup.send("❌ No permission.", ephemeral=True)

    data, sha = load_data()
    before = len(data.get("cup_history", []))
    data["cup_history"] = [h for h in data.get("cup_history", []) if (h.get("title") or "") != title]

    if len(data["cup_history"]) == before:
        return await interaction.followup.send("⚠️ Not found.", ephemeral=True)

    save_data(data, sha)
    return await interaction.followup.send("🗑 Deleted.", ephemeral=True)


@client.tree.command(name="authorleaderboard", description="Leaderboard by who added items (public)")
async def authorleaderboard(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=False)

    data, _ = load_data()
    scores = data.get("scores", {})
    item_authors = data.get("item_authors", {})

    author_points = {}
    for item, pts in scores.items():
        aid = item_authors.get(item)
        if not aid:
            continue
        author_points[aid] = author_points.get(aid, 0) + int(pts)

    if not author_points:
        return await interaction.followup.send("No author scores yet.", ephemeral=False)

    rows = sorted(author_points.items(), key=lambda x: x[1], reverse=True)
    lines = [f"{i}. <@{aid}> — **{pts}**" for i, (aid, pts) in enumerate(rows[:25], start=1)]

    embed = discord.Embed(
        title="🏅 Author Leaderboard",
        description="\n".join(lines),
        color=discord.Color.gold()
    )
    embed.set_footer(text="Points are based on World Cup win counts (scores).")

    return await interaction.followup.send(embed=embed, ephemeral=False)


@client.tree.command(name="wchelp", description="Help menu")
async def wchelp(interaction: discord.Interaction):
    # ONLY command that is ephemeral (your rule)
    embed = discord.Embed(title="📝 World Cup Help", color=discord.Color.blue())
    embed.add_field(name="/addwcitem", value="Add items (non-staff: 1 total; staff: multiple; max 32 total)", inline=False)
    embed.add_field(name="/removewcitem", value="Remove items (staff only)", inline=False)
    embed.add_field(name="/listwcitems", value="List items (public, paginated)", inline=False)
    embed.add_field(name="/startwc", value="Start tournament (staff only, needs 32 items)", inline=False)
    embed.add_field(name="/closematch", value="Lock current match (staff only)", inline=False)
    embed.add_field(name="/nextwcround", value="Process match / advance rounds (staff only)", inline=False)
    embed.add_field(name="/scoreboard", value="View progress (public, paginated)", inline=False)
    embed.add_field(name="/resetwc", value="Reset tournament (staff only, history kept)", inline=False)
    embed.add_field(name="/endwc", value="Announce winner (staff only) + store history", inline=False)
    embed.add_field(name="/cuphistory", value="View past cups (public, paginated)", inline=False)
    embed.add_field(name="/deletehistory", value="Delete history entry (staff only)", inline=False)
    embed.add_field(name="/authorleaderboard", value="Leaderboard by item author (public)", inline=False)

    return await interaction.response.send_message(embed=embed, ephemeral=True)

# =========================================================
# FLASK KEEP-ALIVE (Render)
# =========================================================

app = Flask("")

@app.route("/")
def home():
    return "World Cup Bot is alive!"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

Thread(target=run_flask).start()

# =========================================================
# START
# =========================================================

@client.event
async def on_ready():
    print(f"Logged in as {client.user} (ID: {client.user.id})")
    print(f"[Config] Repo={GITHUB_REPO} Path={GITHUB_FILE_PATH} Branch={GITHUB_BRANCH}")
    if not GITHUB_TOKEN:
        print("[Config] WARNING: No GitHub token found (GITHUB_TOKEN).")

client.run(TOKEN)