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

print("🔥 THIS IS WORLDCUPBOT.PY 🔥")

TOKEN = os.getenv("WC_TOKEN")
GITHUB_REPO = os.getenv("GITHUB_REPO", "saraargh/LSworldcup")
GITHUB_FILE_PATH = "tournament_data.json"
GITHUB_TOKEN = os.getenv("WC_TOKEN")
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
    8:  "Quarter Finals",
    4:  "Semi Finals",
    2:  "Finals"
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
    try:
        r = requests.get(_gh_url(), headers=HEADERS, timeout=10)
        if r.status_code == 200:
            content = r.json()
            raw = base64.b64decode(content["content"]).decode()
            data = json.loads(raw) if raw.strip() else DEFAULT_DATA.copy()
            sha = content.get("sha")

            for k in DEFAULT_DATA:
                if k not in data:
                    data[k] = DEFAULT_DATA[k]

            return data, sha
    except Exception as e:
        print("LOAD ERROR:", e)

    sha = save_data(DEFAULT_DATA.copy())
    return DEFAULT_DATA.copy(), sha

def save_data(data, sha=None):
    payload = {
        "message": "Update World Cup data",
        "content": base64.b64encode(json.dumps(data, indent=4).encode()).decode()
    }
    if sha:
        payload["sha"] = sha

    r = requests.put(_gh_url(), headers=HEADERS, data=json.dumps(payload))
    if r.status_code in (200, 201):
        return r.json()["content"]["sha"]
    return sha

# =========================================================
# UTILITIES
# =========================================================

def user_allowed(member: discord.Member):
    return any(role.id in ALLOWED_ROLE_IDS for role in member.roles)

async def count_votes_from_message(guild, channel_id, message_id):
    try:
        channel = guild.get_channel(channel_id)
        msg = await channel.fetch_message(message_id)
    except:
        return 0, 0, {}, {}

    a_users, b_users = set(), set()
    a_names, b_names = {}, {}

    for reaction in msg.reactions:
        if str(reaction.emoji) not in (VOTE_A, VOTE_B):
            continue
        async for u in reaction.users():
            if u.bot:
                continue
            if str(reaction.emoji) == VOTE_A:
                a_users.add(u.id)
                a_names[u.id] = u.display_name
            else:
                b_users.add(u.id)
                b_names[u.id] = u.display_name

    dupes = a_users & b_users
    for uid in dupes:
        a_users.discard(uid)
        b_users.discard(uid)
        a_names.pop(uid, None)
        b_names.pop(uid, None)

    return len(a_users), len(b_users), a_names, b_names

# =========================================================
# DISCORD CLIENT (CRITICAL FIX)
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
        print("✅ SLASH COMMANDS SYNCED")
        print("📌 COMMANDS:", [c.name for c in self.tree.get_commands()])

client = WorldCupBot()

# =========================================================
# AUTO LOCK + MATCH FLOW (UNCHANGED)
# =========================================================

async def _lock_match(guild, channel, data, sha, reason, ping, reply_msg):
    lm = data.get("last_match")
    if not lm or lm.get("locked"):
        return data, sha

    a_votes, b_votes, _, _ = await count_votes_from_message(
        guild, lm["channel_id"], lm["message_id"]
    )

    lm.update({
        "locked": True,
        "locked_at": int(time.time()),
        "locked_counts": {"a": a_votes, "b": b_votes},
        "lock_reason": reason
    })

    sha = save_data(data, sha)

    try:
        msg = await channel.fetch_message(lm["message_id"])
        emb = msg.embeds[0]
        await msg.edit(embed=discord.Embed(
            title=emb.title,
            description=(emb.description or "") + "\n\n🔒 **Voting closed**",
            color=emb.color
        ))
    except:
        pass

    try:
        text = f"{'@everyone ' if ping else ''}🔒 **Voting closed** ({reason})"
        if reply_msg:
            await reply_msg.reply(text)
        else:
            await channel.send(text)
    except:
        pass

    return data, sha

async def _schedule_auto_lock(channel, message_id):
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
    if not lm or lm["message_id"] != message_id or lm["locked"]:
        return

    try:
        reply_msg = await channel.fetch_message(message_id)
    except:
        reply_msg = None

    await _lock_match(channel.guild, channel, data, sha, "Auto-locked after 24h", True, reply_msg)

# =========================================================
# COMMANDS (ALL REGISTERED CORRECTLY)
# =========================================================

@client.tree.command(name="ping")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("🏓 Pong!")

@client.tree.command(name="wchelp")
async def wchelp(interaction: discord.Interaction):
    embed = discord.Embed(title="📝 World Cup Commands", color=discord.Color.blue())
    embed.add_field(name="/addwcitem", value="Add item (1 per user)", inline=False)
    embed.add_field(name="/listwcitems", value="List items", inline=False)
    embed.add_field(name="/startwc", value="Start tournament", inline=False)
    embed.add_field(name="/nextwcround", value="Advance match", inline=False)
    embed.add_field(name="/closematch", value="Lock voting", inline=False)
    embed.add_field(name="/scoreboard", value="View progress", inline=False)
    embed.add_field(name="/endwc", value="Announce winner", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)

# =========================================================
# KEEP ALIVE (RENDER)
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
# READY + START
# =========================================================

@client.event
async def on_ready():
    print(f"🚀 Logged in as {client.user} ({client.user.id})")

client.run(TOKEN)