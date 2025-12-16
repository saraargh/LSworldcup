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
GITHUB_FILE_PATH = "tournament_data.json"
GITHUB_BRANCH = "main"
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")

HEADERS = {"Authorization": f"token {GITHUB_TOKEN}"} if GITHUB_TOKEN else {}

ALLOWED_ROLE_IDS = [
    1413545658006110401,
    1404098545006546954,
    1420817462290681936,
    1404105470204969000,
    1404104881098195015
]

AUTO_WARN_SECONDS = 23 * 60 * 60
AUTO_LOCK_SECONDS = 24 * 60 * 60

VOTE_A = "🔴"
VOTE_B = "🔵"
ALLOWED_VOTES = {VOTE_A, VOTE_B}

STAGE_BY_COUNT = {
    32: "Round of 32",
    16: "Round of 16",
    8: "Quarter Finals",
    4: "Semi Finals",
    2: "Finals"
}

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
    "item_authors": {},
    "user_items": {},
    "cup_history": []
}

# =========================================================
# GITHUB HELPERS
# =========================================================

def _gh_url():
    return f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_FILE_PATH}"

def load_data():
    r = requests.get(_gh_url(), headers=HEADERS, params={"ref": GITHUB_BRANCH})
    if r.status_code == 200:
        content = r.json()
        raw = base64.b64decode(content["content"]).decode()
        data = json.loads(raw)
        sha = content["sha"]
        for k in DEFAULT_DATA:
            data.setdefault(k, DEFAULT_DATA[k])
        return data, sha

    sha = save_data(DEFAULT_DATA.copy())
    return DEFAULT_DATA.copy(), sha

def save_data(data, sha=None):
    payload = {
        "message": "Update World Cup data",
        "content": base64.b64encode(json.dumps(data, indent=4).encode()).decode(),
        "branch": GITHUB_BRANCH
    }
    if sha:
        payload["sha"] = sha

    r = requests.put(_gh_url(), headers=HEADERS, data=json.dumps(payload))
    if r.status_code in (200, 201):
        return r.json()["content"]["sha"]
    return sha

# =========================================================
# UTIL
# =========================================================

def user_allowed(member: discord.Member):
    return any(r.id in ALLOWED_ROLE_IDS for r in member.roles)

async def count_votes(guild, channel_id, message_id):
    channel = guild.get_channel(channel_id)
    if not channel:
        return 0, 0
    msg = await channel.fetch_message(message_id)

    a, b = set(), set()
    for reaction in msg.reactions:
        if str(reaction.emoji) not in ALLOWED_VOTES:
            async for u in reaction.users():
                if not u.bot:
                    await reaction.remove(u)
            continue

        async for u in reaction.users():
            if u.bot:
                continue
            if str(reaction.emoji) == VOTE_A:
                a.add(u.id)
            else:
                b.add(u.id)

    dupes = a & b
    a -= dupes
    b -= dupes

    return len(a), len(b)

# =========================================================
# CLIENT
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
# MATCH + LOCK LOGIC (MERGED EMBEDS)
# =========================================================

async def post_match_embed(channel, data, sha, previous=None):
    a = data["current_round"].pop(0)
    b = data["current_round"].pop(0)

    embed = discord.Embed(
        title=f"🎮 {data['round_stage']}",
        color=discord.Color.blurple()
    )

    if previous:
        embed.add_field(
            name="🏆 Previous Match",
            value=f"**{previous['winner']}** won\n"
                  f"{VOTE_A} {previous['a']} {previous['a_votes']} — "
                  f"{VOTE_B} {previous['b']} {previous['b_votes']}",
            inline=False
        )

    embed.add_field(
        name="🗳️ Current Match",
        value=f"{VOTE_A} **{a}**\n{VOTE_B} **{b}**\n\n⏰ Auto-lock in 24h",
        inline=False
    )

    msg = await channel.send(embed=embed)
    await msg.add_reaction(VOTE_A)
    await msg.add_reaction(VOTE_B)

    data["last_match"] = {
        "a": a,
        "b": b,
        "message_id": msg.id,
        "channel_id": channel.id,
        "locked": False
    }

    sha = save_data(data, sha)
    asyncio.create_task(auto_lock(channel, msg.id))
    return sha

async def auto_lock(channel, message_id):
    await asyncio.sleep(AUTO_LOCK_SECONDS)
    data, sha = load_data()
    lm = data.get("last_match")
    if not lm or lm["message_id"] != message_id or lm["locked"]:
        return

    a, b = await count_votes(channel.guild, lm["channel_id"], lm["message_id"])
    lm["locked"] = True
    lm["locked_counts"] = {"a": a, "b": b}

    msg = await channel.fetch_message(message_id)
    embed = msg.embeds[0]
    embed.set_footer(text="🔒 Voting closed")
    await msg.edit(embed=embed)

    save_data(data, sha)

# =========================================================
# COMMANDS (PARTIAL — CONTINUES IN PART 2)
# =========================================================

@client.tree.command(name="addwcitem")
async def addwcitem(interaction: discord.Interaction, items: str):
    await interaction.response.defer(ephemeral=True)

    data, sha = load_data()
    is_admin = user_allowed(interaction.user)
    uid = str(interaction.user.id)

    incoming = [i.strip() for i in items.split(",") if i.strip()]

    if len(data["items"]) + len(incoming) > 32:
        return await interaction.followup.send("❌ Max 32 items.", ephemeral=True)

    if not is_admin:
        if uid in data["user_items"]:
            return await interaction.followup.send("❌ You already added an item.", ephemeral=True)
        if len(incoming) != 1:
            return await interaction.followup.send("❌ You may add only one item.", ephemeral=True)

    for it in incoming:
        if it not in data["items"]:
            data["items"].append(it)
            data["scores"][it] = 0
            data["item_authors"][it] = uid
            if not is_admin:
                data["user_items"][uid] = it

    save_data(data, sha)
    await interaction.followup.send("✅ Item(s) added.", ephemeral=True)

# ===========================
# CONTINUED COMMANDS
# ===========================

@client.tree.command(name="removewcitem")
@app_commands.describe(items="Comma-separated list (case-insensitive)")
async def removewcitem(interaction: discord.Interaction, items: str):
    # staff-only
    await interaction.response.defer(ephemeral=True)

    if not user_allowed(interaction.user):
        return await interaction.followup.send("❌ No permission.", ephemeral=True)

    data, sha = load_data()
    remove_list = [x.strip() for x in items.split(",") if x.strip()]
    removed = []

    lower_map = {i.lower(): i for i in data.get("items", [])}

    for it in remove_list:
        key = it.lower()
        if key in lower_map:
            original = lower_map[key]
            # remove from items + scores
            try:
                data["items"].remove(original)
            except ValueError:
                pass
            data.get("scores", {}).pop(original, None)

            # remove author mapping + user_items mapping if pointing at this item
            author_id = data.get("item_authors", {}).pop(original, None)
            if author_id and data.get("user_items", {}).get(str(author_id)) == original:
                data["user_items"].pop(str(author_id), None)

            removed.append(original)

    save_data(data, sha)
    if removed:
        return await interaction.followup.send(f"✅ Removed: {', '.join(removed)}", ephemeral=True)
    return await interaction.followup.send("⚠️ No items removed.", ephemeral=True)


@client.tree.command(name="listwcitems", description="List all World Cup items (paginated)")
async def listwcitems(interaction: discord.Interaction):
    # PUBLIC (only /wchelp is ephemeral)
    await interaction.response.defer(ephemeral=True)

    data, _ = load_data()
    items = data.get("items", [])

    if not items:
        return await interaction.followup.send("No items added yet.", ephemeral=False)

    pages = [items[i:i+10] for i in range(0, len(items), 10)]
    total_pages = len(pages)
    page = 0

    def make_embed(p: int):
        e = discord.Embed(
            title="📋 World Cup Items",
            description="\n".join(f"{p*10+i+1}. {v}" for i, v in enumerate(pages[p])),
            color=discord.Color.blue()
        )
        e.set_footer(text=f"Page {p+1}/{total_pages}")
        return e

    await interaction.followup.send(embed=make_embed(0), ephemeral=False)
    msg = await interaction.original_response()

    if total_pages <= 1:
        return

    await msg.add_reaction("⬅️")
    await msg.add_reaction("➡️")

    def check(reaction, user):
        return (
            user == interaction.user
            and reaction.message.id == msg.id
            and str(reaction.emoji) in ("⬅️", "➡️")
        )

    while True:
        try:
            reaction, user = await interaction.client.wait_for("reaction_add", timeout=60.0, check=check)
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


@client.tree.command(name="startwc", description="Start the World Cup (staff only, requires exactly 32 items)")
@app_commands.describe(title="World Cup title")
async def startwc(interaction: discord.Interaction, title: str):
    # SHOULD BE EPHEMERAL (you asked: started should be ephemeral)
    await interaction.response.defer(ephemeral=True)

    if not user_allowed(interaction.user):
        return await interaction.followup.send("❌ No permission.", ephemeral=True)

    data, sha = load_data()

    if data.get("running"):
        return await interaction.followup.send("❌ Already running.", ephemeral=True)

    if len(data.get("items", [])) != 32:
        return await interaction.followup.send("❌ Must have exactly 32 items.", ephemeral=True)

    data["title"] = title
    data["current_round"] = data["items"].copy()
    random.shuffle(data["current_round"])
    data["next_round"] = []
    data["finished_matches"] = []
    data["last_match"] = None
    data["last_winner"] = None
    data["running"] = True
    data["round_stage"] = STAGE_BY_COUNT.get(32, "Round of 32")

    sha = save_data(data, sha)

    await interaction.channel.send(f"@everyone The World Cup of **{title}** is starting — cast your votes! 🏆")

    # first match (no previous)
    if len(data["current_round"]) >= 2:
        await post_match_embed(interaction.channel, data, sha, previous=None)

    return await interaction.followup.send("✅ Tournament started.", ephemeral=True)


@client.tree.command(name="closematch", description="Lock the current match (staff only)")
async def closematch(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    if not user_allowed(interaction.user):
        return await interaction.followup.send("❌ No permission.", ephemeral=True)

    data, sha = load_data()
    lm = data.get("last_match")
    if not lm:
        return await interaction.followup.send("⚠️ No active match to close.", ephemeral=True)

    if lm.get("locked"):
        return await interaction.followup.send("🔒 Match is already locked.", ephemeral=True)

    # snapshot current votes
    try:
        a_votes, b_votes = await count_votes(interaction.guild, lm["channel_id"], lm["message_id"])
    except Exception:
        a_votes, b_votes = 0, 0

    lm["locked"] = True
    lm["locked_counts"] = {"a": a_votes, "b": b_votes}
    save_data(data, sha)

    # show lock badge on the matchup embed itself
    try:
        msg = await interaction.channel.fetch_message(lm["message_id"])
        if msg.embeds:
            emb = msg.embeds[0]
            emb.set_footer(text="🔒 Voting closed")
            await msg.edit(embed=emb)
        # reply ping is OFF for manual close (unchanged behaviour from your baseline)
        await msg.reply(f"🔒 **Voting is now closed.** (Closed by {interaction.user.display_name})")
    except Exception:
        pass

    return await interaction.followup.send("🔒 Match locked.", ephemeral=True)


@client.tree.command(name="nextwcround", description="Process the current match → move on (staff only)")
async def nextwcround(interaction: discord.Interaction):
    # SHOULD BE EPHEMERAL (you asked: match processed should be ephemeral)
    await interaction.response.defer(ephemeral=True)

    if not user_allowed(interaction.user):
        return await interaction.followup.send("❌ No permission.", ephemeral=True)

    data, sha = load_data()
    if not data.get("running"):
        return await interaction.followup.send("❌ No active tournament.", ephemeral=True)

    # FINAL PROTECTION (unchanged intention)
    if (
        data.get("round_stage") == "Finals"
        and not data.get("last_match")
        and not data.get("current_round")
        and data.get("last_winner") is not None
    ):
        return await interaction.followup.send(
            f"❌ No more rounds left.\nUse `/endwc` to announce the winner of **{data.get('title','')}**.",
            ephemeral=True
        )

    # PROCESS LAST MATCH
    if data.get("last_match"):
        lm = data["last_match"]

        # IMPORTANT FIX: after lock, still allow nextwcround (use snapshot)
        if lm.get("locked") and isinstance(lm.get("locked_counts"), dict):
            a_votes = int(lm["locked_counts"].get("a", 0))
            b_votes = int(lm["locked_counts"].get("b", 0))
        else:
            a_votes, b_votes = await count_votes(interaction.guild, lm["channel_id"], lm["message_id"])

        a = lm["a"]
        b = lm["b"]

        # pick winner (unchanged)
        if a_votes > b_votes:
            winner = a
        elif b_votes > a_votes:
            winner = b
        else:
            winner = random.choice([a, b])

        # record finished match
        data["finished_matches"].append({
            "a": a,
            "b": b,
            "winner": winner,
            "a_votes": a_votes,
            "b_votes": b_votes
        })

        # advance
        data["next_round"].append(winner)
        data["scores"][winner] = int(data.get("scores", {}).get(winner, 0)) + 1
        data["last_match"] = None
        data["last_winner"] = winner
        sha = save_data(data, sha)

        # is this the FINAL match?
        is_final_match = (data.get("round_stage") == "Finals") and len(data.get("current_round", [])) == 0
        if is_final_match:
            return await interaction.followup.send(
                "✔ Final match processed.\n❌ No more matches left.\nUse `/endwc` to announce the winner.",
                ephemeral=True
            )

        # merged embed: previous result + new matchup (ONE embed, not two)
        if len(data.get("current_round", [])) >= 2:
            await post_match_embed(
                interaction.channel,
                data,
                sha,
                previous={"a": a, "b": b, "winner": winner, "a_votes": a_votes, "b_votes": b_votes}
            )

        return await interaction.followup.send("✔ Match processed.", ephemeral=True)

    # PROMOTE TO NEXT ROUND (double-run behaviour preserved)
    if not data.get("current_round") and data.get("next_round"):
        prev_stage = data.get("round_stage", "Round")

        data["current_round"] = data["next_round"].copy()
        data["next_round"] = []

        new_len = len(data["current_round"])
        data["round_stage"] = STAGE_BY_COUNT.get(new_len, f"{new_len}-items round")
        sha = save_data(data, sha)

        embed = discord.Embed(
            title=f"✅ {prev_stage} complete!",
            description=f"Now entering **{data['round_stage']}**.",
            color=discord.Color.purple()
        )
        await interaction.channel.send(embed=embed)

        # post next matchup (no previous here, because this is round transition)
        if new_len >= 2:
            await post_match_embed(interaction.channel, data, sha, previous=None)

        return await interaction.followup.send("🔁 Next round posted.", ephemeral=True)

    return await interaction.followup.send("⚠ Nothing to process.", ephemeral=True)


@client.tree.command(name="scoreboard", description="Show finished matches + current match + upcoming")
async def scoreboard(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

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

    # chunk upcoming
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

    def make_embed(p: int):
        e = discord.Embed(title="🏆 World Cup Scoreboard", color=discord.Color.teal())
        e.add_field(name="Tournament", value=data.get("title") or "No title", inline=False)
        e.add_field(name="Stage", value=data.get("round_stage") or "N/A", inline=False)
        e.add_field(name="Current Match", value=current_line, inline=False)
        e.add_field(
            name="Finished Matches",
            value="\n".join(finished_pages[min(p, len(finished_pages)-1)]),
            inline=False
        )
        e.add_field(
            name="Upcoming Matchups",
            value="\n".join(upcoming_chunks[min(p, len(upcoming_chunks)-1)]),
            inline=False
        )
        e.set_footer(text=f"Page {p+1}/{total_pages}")
        return e

    await interaction.followup.send(embed=make_embed(0), ephemeral=False)
    msg = await interaction.original_response()

    if total_pages > 1:
        await msg.add_reaction("⬅️")
        await msg.add_reaction("➡️")

    def check(reaction, user):
        return (
            user == interaction.user
            and reaction.message.id == msg.id
            and str(reaction.emoji) in ("⬅️", "➡️")
        )

    while total_pages > 1:
        try:
            reaction, user = await interaction.client.wait_for("reaction_add", timeout=60.0, check=check)
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


@client.tree.command(name="endwc", description="Announce winner & end tournament (staff only) + save history")
async def endwc(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    data, sha = load_data()

    if not user_allowed(interaction.user):
        return await interaction.followup.send("❌ No permission.", ephemeral=True)

    if not data.get("running"):
        return await interaction.followup.send("❌ No active tournament.", ephemeral=True)

    winner = data.get("last_winner")
    if not winner:
        return await interaction.followup.send("⚠ No winner recorded. Run `/nextwcround` for the final match.", ephemeral=True)

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
    await interaction.response.defer(ephemeral=True)

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
        return (
            user == interaction.user
            and reaction.message.id == msg.id
            and str(reaction.emoji) in ("⬅️", "➡️")
        )

    while total > 1:
        try:
            reaction, user = await interaction.client.wait_for("reaction_add", timeout=60.0, check=check)
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


@client.tree.command(name="deletehistory", description="Delete a single cup from history by title (staff only)")
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
    await interaction.response.defer(ephemeral=True)

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

    lines = []
    for i, (aid, pts) in enumerate(rows[:25], start=1):
        lines.append(f"{i}. <@{aid}> — **{pts}**")

    embed = discord.Embed(
        title="🏅 Author Leaderboard",
        description="\n".join(lines),
        color=discord.Color.gold()
    )
    embed.set_footer(text="Points are based on win counts (scores).")

    return await interaction.followup.send(embed=embed, ephemeral=False)


@client.tree.command(name="wchelp", description="Help menu")
async def wchelp(interaction: discord.Interaction):
    # ONLY COMMAND THAT SHOULD BE EPHEMERAL
    await interaction.response.defer(ephemeral=True)

    embed = discord.Embed(title="📝 World Cup Help", color=discord.Color.blue())
    embed.add_field(name="/addwcitem", value="Add items (everyone can add 1; staff can add more). Max 32 total.", inline=False)
    embed.add_field(name="/removewcitem", value="Remove items (staff only)", inline=False)
    embed.add_field(name="/listwcitems", value="List items (paginated)", inline=False)
    embed.add_field(name="/startwc", value="Start tournament (staff only)", inline=False)
    embed.add_field(name="/closematch", value="Lock current match (staff only)", inline=False)
    embed.add_field(name="/nextwcround", value="Process match / round (staff only) — supports locked matches", inline=False)
    embed.add_field(name="/scoreboard", value="View progress (everyone)", inline=False)
    embed.add_field(name="/resetwc", value="Reset tournament (staff only, history kept)", inline=False)
    embed.add_field(name="/endwc", value="Announce winner (staff only) + store history", inline=False)
    embed.add_field(name="/cuphistory", value="View past cups (everyone)", inline=False)
    embed.add_field(name="/deletehistory", value="Delete history entry by title (staff only)", inline=False)
    embed.add_field(name="/authorleaderboard", value="Leaderboard by item author (everyone)", inline=False)

    return await interaction.followup.send(embed=embed, ephemeral=True)


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
    if not TOKEN:
        print("[Config] ERROR: No Discord token found (WC_TOKEN or TOKEN).")
    if not GITHUB_TOKEN:
        print("[Config] ERROR: No GitHub token found in env (GITHUB_TOKEN).")

client.run(TOKEN)