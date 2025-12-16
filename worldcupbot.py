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
from typing import Optional, Dict, Any, Tuple

# =========================================================
# CONFIG
# =========================================================

# Discord token (Render: you said yours is WC_TOKEN)
TOKEN = os.getenv("WC_TOKEN") or os.getenv("TOKEN")

UK_TZ = pytz.timezone("Europe/London")

# GitHub storage
GITHUB_REPO = os.getenv("GITHUB_REPO", "saraargh/LSworldcup")
GITHUB_FILE_PATH = os.getenv("TOURNAMENT_JSON_PATH", "tournament_data.json")
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main")

# GitHub token (classic PAT works with "token", fine-grained works too)
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN") or os.getenv("WC_GITHUB_TOKEN") or os.getenv("WC_TOKEN")

HEADERS = {
    "Accept": "application/vnd.github+json",
    "User-Agent": "LSWorldCupBot/1.0"
}
if GITHUB_TOKEN:
    HEADERS["Authorization"] = f"token {GITHUB_TOKEN}"

# Roles allowed to run staff-only commands
ALLOWED_ROLE_IDS = [
    1413545658006110401,  # William/Admin
    1404098545006546954,  # serversorter
    1420817462290681936,  # kd
    1404105470204969000,  # greg
    1404104881098195015   # sazzles
]

# ------------------- Auto Lock Timers -------------------
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

# =========================================================
# DEFAULT DATA
# =========================================================

DEFAULT_DATA: Dict[str, Any] = {
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

    # track who added what + enforce 1 per user (non-admin)
    "item_authors": {},   # item -> user_id (str)
    "user_items": {},     # user_id -> item

    # persistent history (DO NOT wipe on reset)
    "cup_history": []     # list of {title, winner, author_id, timestamp}
}

# =========================================================
# GITHUB HELPERS
# =========================================================

def _gh_url() -> str:
    return f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_FILE_PATH}"

def _gh_params() -> Optional[dict]:
    return {"ref": GITHUB_BRANCH} if GITHUB_BRANCH else None

def load_data() -> Tuple[Dict[str, Any], Optional[str]]:
    """
    Loads JSON from GitHub. Ensures required keys exist.
    """
    try:
        r = requests.get(_gh_url(), headers=HEADERS, params=_gh_params(), timeout=15)

        if r.status_code == 200:
            content = r.json()
            raw = base64.b64decode(content["content"]).decode("utf-8", errors="replace")
            data = json.loads(raw) if raw.strip() else DEFAULT_DATA.copy()
            sha = content.get("sha")

            # ensure keys exist
            for k in DEFAULT_DATA:
                if k not in data:
                    data[k] = DEFAULT_DATA[k]

            # ensure types
            if not isinstance(data.get("item_authors"), dict):
                data["item_authors"] = {}
            if not isinstance(data.get("user_items"), dict):
                data["user_items"] = {}
            if not isinstance(data.get("cup_history"), list):
                data["cup_history"] = []

            if not isinstance(data.get("items"), list):
                data["items"] = []
            if not isinstance(data.get("current_round"), list):
                data["current_round"] = []
            if not isinstance(data.get("next_round"), list):
                data["next_round"] = []
            if not isinstance(data.get("scores"), dict):
                data["scores"] = {}

            return data, sha

        if r.status_code == 404:
            sha = save_data(DEFAULT_DATA.copy(), sha=None)
            return DEFAULT_DATA.copy(), sha

        print(f"[GitHub] load_data unexpected status: {r.status_code} {r.text}")
        return DEFAULT_DATA.copy(), None

    except Exception as e:
        print("[GitHub] load_data error:", e)
        return DEFAULT_DATA.copy(), None

def save_data(data: Dict[str, Any], sha: Optional[str] = None) -> Optional[str]:
    """
    Saves JSON to GitHub. Writes to the configured branch.
    """
    try:
        payload: Dict[str, Any] = {
            "message": "Update tournament data",
            "content": base64.b64encode(json.dumps(data, indent=4).encode("utf-8")).decode("utf-8"),
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

def user_allowed(member: discord.Member, allowed_roles) -> bool:
    return any(role.id in allowed_roles for role in member.roles)

def is_staff(member: discord.Member) -> bool:
    return user_allowed(member, ALLOWED_ROLE_IDS)

async def count_votes_from_message(guild: discord.Guild, channel_id: int, message_id: int):
    try:
        channel = guild.get_channel(channel_id)
        if channel is None:
            return 0, 0, {}, {}
        msg = await channel.fetch_message(message_id)
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
            elif emoji == VOTE_B:
                b_users.add(u.id)
                b_names[u.id] = u.display_name

    # Single vote rule: if user reacted with both, remove from BOTH counts
    dupes = a_users & b_users
    for uid in dupes:
        a_users.discard(uid)
        b_users.discard(uid)
        a_names.pop(uid, None)
        b_names.pop(uid, None)

    return len(a_users), len(b_users), a_names, b_names

def build_merged_match_embed(
    stage: str,
    next_a: str,
    next_b: str,
    a_count: int = 0,
    b_count: int = 0,
    a_names: Optional[dict] = None,
    b_names: Optional[dict] = None,
    prev_result: Optional[dict] = None
) -> discord.Embed:
    """
    One embed that contains:
      - Previous match result (optional)
      - Next match voting (always)
    """
    a_names = a_names or {}
    b_names = b_names or {}

    embed = discord.Embed(
        title=f"🎮 {stage}",
        color=discord.Color.random()
    )

    if prev_result:
        winner = prev_result["winner"]
        pa = prev_result["a"]
        pb = prev_result["b"]
        pav = prev_result["a_votes"]
        pbv = prev_result["b_votes"]

        embed.add_field(
            name="Previous Match Result 🏆",
            value=(
                f"**{winner}** won!\n"
                f"{VOTE_A} {pa}: {pav}\n"
                f"{VOTE_B} {pb}: {pbv}"
            ),
            inline=False
        )

    # Next match voting block
    a_list = "\n".join([f"• {n}" for n in a_names.values()]) or "_No votes yet_"
    b_list = "\n".join([f"• {n}" for n in b_names.values()]) or "_No votes yet_"

    embed.add_field(
        name="Vote Now 🗳️",
        value=(
            f"{VOTE_A} **{next_a}** — {a_count} votes\n{a_list}\n\n"
            f"{VOTE_B} **{next_b}** — {b_count} votes\n{b_list}"
        ),
        inline=False
    )

    embed.set_footer(text="React 🔴 or 🔵 to vote. Other reactions are removed.")
    return embed

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
        # global sync
        await self.tree.sync()

client = WorldCupBot()

# =========================================================
# REACTION ENFORCEMENT (remove non-red/blue + single vote)
# =========================================================

@client.event
async def on_reaction_add(reaction: discord.Reaction, user: discord.User):
    try:
        if user.bot:
            return
        if not reaction.message.guild:
            return

        data, _ = load_data()
        lm = data.get("last_match")
        if not lm:
            return

        if reaction.message.id != lm.get("message_id"):
            return

        emoji = str(reaction.emoji)

        # Remove any reaction that isn't 🔴 or 🔵
        if emoji not in (VOTE_A, VOTE_B):
            try:
                await reaction.message.remove_reaction(reaction.emoji, user)
            except Exception:
                pass
            return

        # Enforce single vote: if user reacts with one, remove the other
        other = VOTE_B if emoji == VOTE_A else VOTE_A
        try:
            await reaction.message.remove_reaction(other, user)
        except Exception:
            pass

    except Exception:
        return

# =========================================================
# AUTO LOCK + MATCH POSTING (UNCHANGED MATCH FLOW)
# =========================================================

async def _lock_match(
    guild: discord.Guild,
    channel: discord.TextChannel,
    data: Dict[str, Any],
    sha: Optional[str],
    reason: str,
    ping_everyone: bool,
    reply_msg: Optional[discord.Message]
):
    """
    Locks last_match by snapshotting counts.
    Edits the embed to show 🔒 Voting closed.
    Replies to the matchup message.
    """
    if not data.get("last_match"):
        return data, sha

    lm = data["last_match"]
    if lm.get("locked"):
        return data, sha

    a_votes, b_votes, _, _ = await count_votes_from_message(guild, lm["channel_id"], lm["message_id"])
    lm["locked"] = True
    lm["locked_at"] = int(time.time())
    lm["locked_counts"] = {"a": a_votes, "b": b_votes}
    lm["lock_reason"] = reason

    sha = save_data(data, sha)

    try:
        msg = await channel.fetch_message(lm["message_id"])
        if msg.embeds:
            emb = msg.embeds[0]
            new = discord.Embed(
                title=emb.title or f"🎮 {data.get('round_stage','Matchup')}",
                description=(emb.description or ""),
                color=emb.color if emb.color else discord.Color.dark_grey()
            )

            # keep existing fields, but add lock note
            for f in emb.fields:
                new.add_field(name=f.name, value=f.value, inline=f.inline)

            new.add_field(name="Status", value="🔒 **Voting closed**", inline=False)

            if emb.footer and emb.footer.text:
                new.set_footer(text=emb.footer.text)
            await msg.edit(embed=new)
    except Exception as e:
        print("Lock edit failed:", e)

    try:
        ping = "@everyone " if ping_everyone else ""
        text = f"{ping}🔒 **Voting is now closed.** ({reason})"
        if reply_msg:
            await reply_msg.reply(text)
        else:
            try:
                m = await channel.fetch_message(lm["message_id"])
                await m.reply(text)
            except Exception:
                await channel.send(text)
    except Exception as e:
        print("Lock announce failed:", e)

    return data, sha

async def _schedule_auto_lock(channel: discord.TextChannel, message_id: int):
    """
    Warn at 23h, lock at 24h, only if that message is still the active last_match.
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

async def post_next_match(
    channel: discord.TextChannel,
    data: Dict[str, Any],
    sha: Optional[str],
    prev_result: Optional[dict] = None
):
    """
    Posts the next matchup embed (merged with previous result if supplied),
    adds reactions, starts auto-lock, starts reaction loop updater.
    """
    if len(data["current_round"]) < 2:
        return sha

    a = data["current_round"].pop(0)
    b = data["current_round"].pop(0)
    sha = save_data(data, sha)

    # initial embed (no votes yet)
    embed = build_merged_match_embed(
        stage=data.get("round_stage", "Matchup"),
        next_a=a,
        next_b=b,
        a_count=0,
        b_count=0,
        a_names={},
        b_names={},
        prev_result=prev_result
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

    # start 23h warn + 24h lock timers
    asyncio.create_task(_schedule_auto_lock(channel, msg.id))

    # live reaction updater (stops if locked or match changes)
    client_obj = channel.guild._state._get_client()

    def check(reaction: discord.Reaction, user: discord.User):
        return (
            user != channel.guild.me and
            reaction.message.id == msg.id and
            str(reaction.emoji) in (VOTE_A, VOTE_B)
        )

    async def reaction_loop():
        while True:
            try:
                latest, _ = load_data()
                lm = latest.get("last_match")
                if not lm or lm.get("message_id") != msg.id:
                    return
                if lm.get("locked"):
                    return

                await client_obj.wait_for("reaction_add", check=check)

                a_count, b_count, a_names, b_names = await count_votes_from_message(
                    channel.guild, msg.channel.id, msg.id
                )

                updated = build_merged_match_embed(
                    stage=latest.get("round_stage", data.get("round_stage", "Matchup")),
                    next_a=a,
                    next_b=b,
                    a_count=a_count,
                    b_count=b_count,
                    a_names=a_names,
                    b_names=b_names,
                    prev_result=prev_result
                )
                await msg.edit(embed=updated)

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
    is_admin = is_staff(interaction.user)
    uid = str(interaction.user.id)

    incoming = [x.strip() for x in items.split(",") if x.strip()]

    # enforce max 32
    if len(data["items"]) + len(incoming) > 32:
        return await interaction.followup.send("❌ World Cup can only have **32 items**.", ephemeral=True)

    # non-admin rules
    if not is_admin:
        if uid in data["user_items"]:
            return await interaction.followup.send(
                "You can only add one item to the World Cup. Don’t be greedy 😌",
                ephemeral=True
            )
        if len(incoming) != 1:
            return await interaction.followup.send(
                "You can only add one item to the World Cup. Don’t be greedy 😌",
                ephemeral=True
            )

    added = []
    for it in incoming:
        if it not in data["items"]:
            data["items"].append(it)
            data["scores"].setdefault(it, 0)
            data["item_authors"][it] = uid
            if not is_admin:
                data["user_items"][uid] = it
            added.append(it)

    sha = save_data(data, sha)

    if added:
        return await interaction.followup.send(f"✅ Added: {', '.join(added)}", ephemeral=False)
    return await interaction.followup.send("⚠️ Nothing added.", ephemeral=True)


@client.tree.command(name="removewcitem", description="Remove item(s) (admin only)")
@app_commands.describe(items="Comma-separated list")
async def removewcitem(interaction: discord.Interaction, items: str):
    await interaction.response.defer(ephemeral=True)

    if not is_staff(interaction.user):
        return await interaction.followup.send("❌ No permission.", ephemeral=True)

    data, sha = load_data()
    removed = []

    lookup = {i.lower(): i for i in data["items"]}
    for raw in [x.strip() for x in items.split(",") if x.strip()]:
        key = raw.lower()
        if key in lookup:
            item = lookup[key]
            data["items"].remove(item)
            data["scores"].pop(item, None)

            author = data["item_authors"].pop(item, None)
            if author and data["user_items"].get(author) == item:
                data["user_items"].pop(author, None)

            removed.append(item)

    save_data(data, sha)
    if removed:
        return await interaction.followup.send(f"🗑 Removed: {', '.join(removed)}", ephemeral=False)
    return await interaction.followup.send("⚠️ Nothing removed.", ephemeral=True)


@client.tree.command(name="listwcitems", description="List all World Cup items")
async def listwcitems(interaction: discord.Interaction):
    await interaction.response.defer()

    data, _ = load_data()
    items = data["items"]

    if not items:
        return await interaction.followup.send("No items added yet.")

    pages = [items[i:i+10] for i in range(0, len(items), 10)]
    page = 0

    def embed(p):
        e = discord.Embed(
            title="📋 World Cup Items",
            description="\n".join(f"{i+1+p*10}. {v}" for i, v in enumerate(pages[p])),
            color=discord.Color.blue()
        )
        e.set_footer(text=f"Page {p+1}/{len(pages)}")
        return e

    await interaction.followup.send(embed=embed(0))
    msg = await interaction.original_response()

    if len(pages) > 1:
        await msg.add_reaction("⬅️")
        await msg.add_reaction("➡️")

    def check(r, u):
        return u == interaction.user and r.message.id == msg.id

    while True:
        try:
            r, u = await interaction.client.wait_for("reaction_add", timeout=60, check=check)
            if str(r.emoji) == "➡️" and page < len(pages)-1:
                page += 1
            elif str(r.emoji) == "⬅️" and page > 0:
                page -= 1
            await msg.edit(embed=embed(page))
            await msg.remove_reaction(r.emoji, u)
        except asyncio.TimeoutError:
            break


@client.tree.command(name="closematch", description="Lock the current match (admin only)")
async def closematch(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    if not is_staff(interaction.user):
        return await interaction.followup.send("❌ No permission.", ephemeral=True)

    data, sha = load_data()
    lm = data.get("last_match")
    if not lm:
        return await interaction.followup.send("No active match.", ephemeral=True)

    try:
        msg = await interaction.channel.fetch_message(lm["message_id"])
    except Exception:
        msg = None

    await _lock_match(
        interaction.guild,
        interaction.channel,
        data,
        sha,
        f"Closed by {interaction.user.display_name}",
        False,
        msg
    )

    await interaction.followup.send("🔒 Match locked.", ephemeral=True)


@client.tree.command(name="startwc", description="Start the World Cup (admin only)")
@app_commands.describe(title="World Cup title")
async def startwc(interaction: discord.Interaction, title: str):
    await interaction.response.defer(ephemeral=True)

    if not is_staff(interaction.user):
        return await interaction.followup.send("❌ No permission.", ephemeral=True)

    data, sha = load_data()

    if data["running"]:
        return await interaction.followup.send("❌ Already running.", ephemeral=True)
    if len(data["items"]) != 32:
        return await interaction.followup.send("❌ You must have exactly **32 items**.", ephemeral=True)

    data.update({
        "title": title,
        "current_round": random.sample(data["items"], 32),
        "next_round": [],
        "finished_matches": [],
        "last_match": None,
        "last_winner": None,
        "running": True,
        "round_stage": STAGE_BY_COUNT[32]
    })

    sha = save_data(data, sha)
    await interaction.channel.send(f"@everyone **{title}** World Cup has begun! 🏆")
    await post_next_match(interaction.channel, data, sha)

    await interaction.followup.send("✅ Tournament started.", ephemeral=True)


@client.tree.command(name="nextwcround", description="Process the current match (admin only)")
async def nextwcround(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    if not is_staff(interaction.user):
        return await interaction.followup.send("❌ No permission.", ephemeral=True)

    data, sha = load_data()
    if not data["running"]:
        return await interaction.followup.send("❌ No active tournament.", ephemeral=True)

    lm = data.get("last_match")
    if not lm:
        return await interaction.followup.send("⚠ Nothing to process.", ephemeral=True)

    if lm.get("locked") and lm.get("locked_counts"):
        a_votes = lm["locked_counts"]["a"]
        b_votes = lm["locked_counts"]["b"]
    else:
        a_votes, b_votes, _, _ = await count_votes_from_message(
            interaction.guild, lm["channel_id"], lm["message_id"]
        )

    a, b = lm["a"], lm["b"]
    winner = a if a_votes > b_votes else b if b_votes > a_votes else random.choice([a, b])

    result = {
        "a": a,
        "b": b,
        "winner": winner,
        "a_votes": a_votes,
        "b_votes": b_votes
    }

    data["finished_matches"].append(result)
    data["next_round"].append(winner)
    data["scores"][winner] = data["scores"].get(winner, 0) + 1
    data["last_match"] = None
    data["last_winner"] = winner

    sha = save_data(data, sha)

    if not data["current_round"]:
        data["current_round"] = data["next_round"].copy()
        data["next_round"] = []
        data["round_stage"] = STAGE_BY_COUNT.get(len(data["current_round"]), "Round")
        sha = save_data(data, sha)

    await post_next_match(interaction.channel, data, sha, prev_result=result)
    await interaction.followup.send("✔ Match processed.", ephemeral=True)


@client.tree.command(name="scoreboard", description="View tournament progress")
async def scoreboard(interaction: discord.Interaction):
    await interaction.response.defer()

    data, _ = load_data()

    embed = discord.Embed(
        title="🏆 World Cup Scoreboard",
        color=discord.Color.teal()
    )
    embed.add_field(name="Tournament", value=data["title"] or "N/A", inline=False)
    embed.add_field(name="Stage", value=data["round_stage"] or "N/A", inline=False)

    finished = data["finished_matches"]
    if finished:
        lines = [
            f"{i+1}. {m['a']} vs {m['b']} → **{m['winner']}**"
            for i, m in enumerate(finished[-10:])
        ]
        embed.add_field(name="Recent Matches", value="\n".join(lines), inline=False)
    else:
        embed.add_field(name="Recent Matches", value="None yet.", inline=False)

    await interaction.followup.send(embed=embed)


@client.tree.command(name="resetwc", description="Reset the tournament (history kept)")
async def resetwc(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    if not is_staff(interaction.user):
        return await interaction.followup.send("❌ No permission.", ephemeral=True)

    data, sha = load_data()
    history = data["cup_history"]

    fresh = DEFAULT_DATA.copy()
    fresh["cup_history"] = history

    save_data(fresh, sha)
    await interaction.followup.send("🔄 Tournament reset (history preserved).", ephemeral=True)


@client.tree.command(name="endwc", description="End the World Cup and announce winner")
async def endwc(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    if not is_staff(interaction.user):
        return await interaction.followup.send("❌ No permission.", ephemeral=True)

    data, sha = load_data()
    winner = data.get("last_winner")
    if not winner:
        return await interaction.followup.send("❌ No winner yet.", ephemeral=True)

    author = data["item_authors"].get(winner)
    data["cup_history"].append({
        "title": data["title"],
        "winner": winner,
        "author_id": author,
        "timestamp": int(time.time())
    })

    data["running"] = False
    save_data(data, sha)

    embed = discord.Embed(
        title="🏆 WORLD CUP WINNER",
        description=f"**{winner}**\n✨ Added by: <@{author}>" if author else f"**{winner}**",
        color=discord.Color.green()
    )

    await interaction.channel.send("@everyone WE HAVE A WINNER 🎉")
    await interaction.channel.send(embed=embed)
    await interaction.followup.send("✔ World Cup ended.", ephemeral=True)


@client.tree.command(name="cuphistory", description="View past World Cups")
async def cuphistory(interaction: discord.Interaction):
    await interaction.response.defer()

    data, _ = load_data()
    hist = list(reversed(data["cup_history"]))

    if not hist:
        return await interaction.followup.send("No history yet.")

    embed = discord.Embed(title="📜 World Cup History", color=discord.Color.blurple())
    for h in hist[:10]:
        embed.add_field(
            name=h["title"],
            value=f"🏆 {h['winner']} — <@{h['author_id']}>",
            inline=False
        )

    await interaction.followup.send(embed=embed)


@client.tree.command(name="authorleaderboard", description="Leaderboard by item author")
async def authorleaderboard(interaction: discord.Interaction):
    await interaction.response.defer()

    data, _ = load_data()
    scores = {}

    for item, pts in data["scores"].items():
        aid = data["item_authors"].get(item)
        if aid:
            scores[aid] = scores.get(aid, 0) + pts

    if not scores:
        return await interaction.followup.send("No scores yet.")

    rows = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    embed = discord.Embed(title="🏅 Author Leaderboard", color=discord.Color.gold())

    for i, (aid, pts) in enumerate(rows[:25], 1):
        embed.add_field(name=f"{i}. <@{aid}>", value=f"{pts} points", inline=False)

    await interaction.followup.send(embed=embed)


@client.tree.command(name="wchelp", description="World Cup help")
async def wchelp(interaction: discord.Interaction):
    embed = discord.Embed(title="📝 World Cup Help", color=discord.Color.blue())
    embed.add_field(name="/addwcitem", value="Add items (1 per user, admins unlimited)", inline=False)
    embed.add_field(name="/removewcitem", value="Remove items (admin only)", inline=False)
    embed.add_field(name="/listwcitems", value="List all items", inline=False)
    embed.add_field(name="/startwc", value="Start tournament", inline=False)
    embed.add_field(name="/closematch", value="Lock current match", inline=False)
    embed.add_field(name="/nextwcround", value="Process match / round", inline=False)
    embed.add_field(name="/scoreboard", value="View progress", inline=False)
    embed.add_field(name="/resetwc", value="Reset tournament", inline=False)
    embed.add_field(name="/endwc", value="End tournament", inline=False)
    embed.add_field(name="/cuphistory", value="View past cups", inline=False)
    embed.add_field(name="/authorleaderboard", value="Leaderboard by author", inline=False)

    await interaction.response.send_message(embed=embed, ephemeral=True)

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

client.run(TOKEN)