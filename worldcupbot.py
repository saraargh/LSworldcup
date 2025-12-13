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

TOKEN = os.getenv("TOKEN")
GITHUB_REPO = os.getenv("GITHUB_REPO")
GITHUB_FILE_PATH = "tournament_data.json"
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")

HEADERS = {"Authorization": f"token {GITHUB_TOKEN}"} if GITHUB_TOKEN else {}

UK_TZ = pytz.timezone("Europe/London")

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

def gh_url():
    return f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_FILE_PATH}"

def load_data():
    try:
        r = requests.get(gh_url(), headers=HEADERS, timeout=10)
        if r.status_code == 200:
            content = r.json()
            raw = base64.b64decode(content["content"]).decode()
            data = json.loads(raw)
            sha = content["sha"]
            for k in DEFAULT_DATA:
                data.setdefault(k, DEFAULT_DATA[k])
            return data, sha
    except:
        pass

    sha = save_data(DEFAULT_DATA.copy())
    return DEFAULT_DATA.copy(), sha

def save_data(data, sha=None):
    payload = {
        "message": "Update World Cup data",
        "content": base64.b64encode(json.dumps(data, indent=4).encode()).decode()
    }
    if sha:
        payload["sha"] = sha

    r = requests.put(gh_url(), headers=HEADERS, data=json.dumps(payload))
    if r.status_code in (200, 201):
        return r.json()["content"]["sha"]
    return sha

# =========================================================
# UTILITIES
# =========================================================

def is_admin(member):
    return any(r.id in ALLOWED_ROLE_IDS for r in member.roles)

async def count_votes(guild, channel_id, message_id):
    try:
        channel = guild.get_channel(channel_id)
        msg = await channel.fetch_message(message_id)
    except:
        return 0, 0

    a, b = set(), set()

    for reaction in msg.reactions:
        if str(reaction.emoji) not in (VOTE_A, VOTE_B):
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
# MATCH LOGIC
# =========================================================

async def lock_match(guild, channel, data, sha, reason):
    lm = data.get("last_match")
    if not lm or lm.get("locked"):
        return

    a, b = await count_votes(guild, lm["channel_id"], lm["message_id"])
    lm.update({
        "locked": True,
        "locked_at": int(time.time()),
        "locked_counts": {"a": a, "b": b},
        "lock_reason": reason
    })

    save_data(data, sha)

    try:
        msg = await channel.fetch_message(lm["message_id"])
        emb = msg.embeds[0]
        emb.description += "\n\n🔒 **Voting closed**"
        await msg.edit(embed=emb)
        await msg.reply(f"@everyone 🔒 **Voting closed** ({reason})")
    except:
        pass

async def auto_lock(channel, message_id):
    await asyncio.sleep(AUTO_WARN_SECONDS)
    data, sha = load_data()
    lm = data.get("last_match")
    if not lm or lm["message_id"] != message_id or lm["locked"]:
        return

    try:
        msg = await channel.fetch_message(message_id)
        await msg.reply("@everyone ⏰ Voting closes soon!")
    except:
        pass

    await asyncio.sleep(AUTO_LOCK_SECONDS - AUTO_WARN_SECONDS)
    data, sha = load_data()
    lm = data.get("last_match")
    if lm and not lm["locked"]:
        await lock_match(channel.guild, channel, data, sha, "Auto-locked after 24h")

async def post_match(channel, data, sha):
    a = data["current_round"].pop(0)
    b = data["current_round"].pop(0)

    embed = discord.Embed(
        title=f"🎮 {data['round_stage']}",
        description=f"{VOTE_A} {a}\n\n{VOTE_B} {b}",
        color=discord.Color.random()
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

# =========================================================
# COMMANDS
# =========================================================

@client.tree.command(name="addwcitem")
async def addwcitem(interaction: discord.Interaction, items: str):
    data, sha = load_data()
    uid = str(interaction.user.id)

    if not is_admin(interaction.user):
        if uid in data["user_items"]:
            return await interaction.response.send_message(
                "You can only add one item to the World Cup. Don’t be greedy 😌",
                ephemeral=True
            )
        if "," in items:
            return await interaction.response.send_message(
                "You can only add one item to the World Cup. Don’t be greedy 😌",
                ephemeral=True
            )

    added = []
    for item in [i.strip() for i in items.split(",") if i.strip()]:
        if item not in data["items"]:
            data["items"].append(item)
            data["scores"][item] = 0
            data["item_authors"][item] = uid
            if not is_admin(interaction.user):
                data["user_items"][uid] = item
            added.append(item)

    save_data(data, sha)
    await interaction.response.send_message(f"✅ Added: {', '.join(added)}")

@client.tree.command(name="startwc")
async def startwc(interaction: discord.Interaction, title: str):
    if not is_admin(interaction.user):
        return await interaction.response.send_message("❌ No permission.", ephemeral=True)

    data, sha = load_data()
    if len(data["items"]) != 32:
        return await interaction.response.send_message("❌ Need exactly 32 items.", ephemeral=True)

    data.update({
        "title": title,
        "running": True,
        "current_round": random.sample(data["items"], 32),
        "next_round": [],
        "finished_matches": [],
        "round_stage": STAGE_BY_COUNT[32],
        "last_match": None,
        "last_winner": None
    })

    save_data(data, sha)
    await interaction.channel.send(f"@everyone 🏆 **World Cup of {title} has started!**")
    await post_match(interaction.channel, data, sha)
    await interaction.response.send_message("✅ Started.", ephemeral=True)

@client.tree.command(name="nextwcround")
async def nextwcround(interaction: discord.Interaction):
    if not is_admin(interaction.user):
        return await interaction.response.send_message("❌ No permission.", ephemeral=True)

    data, sha = load_data()
    lm = data.get("last_match")
    if not lm:
        return await interaction.response.send_message("⚠ Nothing to process.", ephemeral=True)

    a_votes, b_votes = lm.get("locked_counts", {}).values() if lm.get("locked") else await count_votes(
        interaction.guild, lm["channel_id"], lm["message_id"]
    )

    winner = lm["a"] if a_votes >= b_votes else lm["b"]
    data["finished_matches"].append({
        "a": lm["a"],
        "b": lm["b"],
        "winner": winner
    })

    data["next_round"].append(winner)
    data["last_winner"] = winner
    data["last_match"] = None

    save_data(data, sha)

    if len(data["current_round"]) >= 2:
        await post_match(interaction.channel, data, sha)
    else:
        data["current_round"] = data["next_round"]
        data["next_round"] = []
        if len(data["current_round"]) == 1:
            await interaction.response.send_message("❌ No more rounds. Use /endwc", ephemeral=True)
            return
        data["round_stage"] = STAGE_BY_COUNT[len(data["current_round"])]
        save_data(data, sha)
        await post_match(interaction.channel, data, sha)

    await interaction.response.send_message("✔ Processed.", ephemeral=True)

@client.tree.command(name="endwc")
async def endwc(interaction: discord.Interaction):
    data, sha = load_data()
    winner = data.get("last_winner")
    if not winner:
        return await interaction.response.send_message("No winner yet.", ephemeral=True)

    author = data["item_authors"].get(winner)
    mention = f"<@{author}>" if author else "Unknown"

    data["cup_history"].append({
        "title": data["title"],
        "winner": winner,
        "author_id": author,
        "timestamp": int(time.time())
    })

    data["running"] = False
    save_data(data, sha)

    embed = discord.Embed(
        title="🏆 World Cup Winner!",
        description=f"**{winner}**\n✨ Added by {mention}",
        color=discord.Color.green()
    )

    await interaction.channel.send("@everyone 🎉 **WE HAVE A WINNER!**")
    await interaction.channel.send(embed=embed)
    await interaction.response.send_message("✔ Winner announced.", ephemeral=True)

# =========================================================
# FLASK KEEP-ALIVE
# =========================================================

app = Flask("")

@app.route("/")
def home():
    return "World Cup Bot running"

Thread(target=lambda: app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))).start()

# =========================================================
# START
# =========================================================

@client.event
async def on_ready():
    print(f"Logged in as {client.user}")

client.run(TOKEN)