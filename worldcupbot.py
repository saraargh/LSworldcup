import discord
from discord import app_commands, ui
from discord.ext import tasks
from flask import Flask
from threading import Thread
import datetime
import random
import json
import base64
import requests
import asyncio
import os
import pytz

# =========================================================
# KEEP ALIVE SERVER (Flask)
# =========================================================
app = Flask('')

@app.route('/')
def home():
    return "Bot is alive!"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run_flask)
    t.start()

# =========================================================
# CONFIG
# =========================================================
TOKEN = os.getenv("WC_TOKEN") or os.getenv("TOKEN")
GITHUB_REPO = os.getenv("GITHUB_REPO", "saraargh/LSworldcup")
GITHUB_FILE_PATH = os.getenv("TOURNAMENT_JSON_PATH", "tournament_data.json")
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN") or os.getenv("WC_GITHUB_TOKEN") or os.getenv("WC_TOKEN")
HEADERS = {"Authorization": f"token {GITHUB_TOKEN}"} if GITHUB_TOKEN else {}

ALLOWED_ROLE_IDS = [1413545658006110401, 1404098545006546954, 1420817462290681936, 1404105470204969000, 1404104881098195015]

# =========================================================
# GITHUB API HELPERS
# =========================================================
def load_data():
    try:
        r = requests.get(f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_FILE_PATH}", headers=HEADERS, params={"ref": GITHUB_BRANCH}, timeout=20)
        if r.status_code == 200:
            content = r.json()
            raw = base64.b64decode(content["content"]).decode()
            return json.loads(raw), content.get("sha")
    except Exception as e:
        print(f"Load Error: {e}")
    return {"status": "IDLE", "items": [], "suggestions": [], "leaderboard": [], "bracket": [], "winners_pool": [], "finished_matches": [], "current_match": None, "current_cat": "World Cup"}, None

def save_data(data, sha=None):
    try:
        if not sha: _, sha = load_data()
        payload = {
            "message": "Update tournament data",
            "content": base64.b64encode(json.dumps(data, indent=4).encode()).decode(),
            "branch": GITHUB_BRANCH,
            "sha": sha
        }
        requests.put(f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_FILE_PATH}", headers=HEADERS, data=json.dumps(payload), timeout=20)
    except Exception as e:
        print(f"Save Error: {e}")

def get_round_name(count):
    if count > 16: return "Round of 32"
    if count > 8: return "Round of 16"
    if count > 4: return "Quarter-Finals"
    if count > 2: return "Semi-Finals"
    return "Grand Final"

# =========================================================
# UI COMPONENTS
# =========================================================
class VoteView(ui.View):
    def __init__(self, item_a_name, item_b_name):
        super().__init__(timeout=None)
        self.vote_a.label = f"Vote for {item_a_name}"
        self.vote_b.label = f"Vote for {item_b_name}"
        self.item_a_name = item_a_name
        self.item_b_name = item_b_name

    @ui.button(style=discord.ButtonStyle.danger, custom_id="vote_a")
    async def vote_a(self, interaction: discord.Interaction, button: ui.Button):
        data, sha = load_data()
        match = data.get("current_match")
        if not match: return await interaction.response.send_message("No active match!", ephemeral=True)
        votes = match.get("votes", {})
        if str(interaction.user.id) in votes: return await interaction.response.send_message("You have already voted!", ephemeral=True)
        votes[str(interaction.user.id)] = "A"
        match["votes"] = votes
        save_data(data, sha)
        await interaction.response.send_message(f"✅ Vote recorded for **{self.item_a_name}**!", ephemeral=True)

    @ui.button(style=discord.ButtonStyle.primary, custom_id="vote_b")
    async def vote_b(self, interaction: discord.Interaction, button: ui.Button):
        data, sha = load_data()
        match = data.get("current_match")
        if not match: return await interaction.response.send_message("No active match!", ephemeral=True)
        votes = match.get("votes", {})
        if str(interaction.user.id) in votes: return await interaction.response.send_message("You have already voted!", ephemeral=True)
        votes[str(interaction.user.id)] = "B"
        match["votes"] = votes
        save_data(data, sha)
        await interaction.response.send_message(f"✅ Vote recorded for **{self.item_b_name}**!", ephemeral=True)

# =========================================================
# BOT CORE
# =========================================================
class WC_Bot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.all())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        self.auto_checker.start()

    @tasks.loop(minutes=5)
    async def auto_checker(self):
        data, sha = load_data()
        if data.get("status") == "MATCH_ACTIVE" and data.get("current_match"):
            if datetime.datetime.now().timestamp() > data['current_match']['end_at']:
                await self.resolve_match(data, sha)

    async def resolve_match(self, data, sha):
        match = data['current_match']
        chan = self.get_channel(match['channel_id'])
        votes = match.get("votes", {})
        v1 = list(votes.values()).count("A")
        v2 = list(votes.values()).count("B")
        
        # Determine winner
        if v1 > v2: winner = match['item_a']
        elif v2 > v1: winner = match['item_b']
        else: winner = random.choice([match['item_a'], match['item_b']])

        data.setdefault('finished_matches', []).append({"name": f"{match['item_a']['name']} vs {match['item_b']['name']}", "winner": winner['name']})
        data.setdefault('winners_pool', []).append(winner)
        data['current_match'] = None

        embed = discord.Embed(title=f"🏆 Winner: {winner['name']}", description=f"{winner.get('desc', '')}\n\nFinal Score: {v1} to {v2}", color=0x2ecc71)
        embed.set_image(url=winner['image'])
        await chan.send(embed=embed)

        if not data['bracket'] and len(data['winners_pool']) > 1:
            data['bracket'], data['winners_pool'] = data['winners_pool'], []
            await chan.send(f"🛡️ **Round Complete! Moving to {get_round_name(len(data['bracket']))}.**")
            save_data(data, sha)
            await self.post_next(chan)
        elif not data['bracket'] and len(data['winners_pool']) == 1:
            await chan.send(f"🎊 **CHAMPION: {winner['name']}!**")
            data.setdefault('leaderboard', []).append({"user": winner['user'], "item": winner['name']})
            data['status'] = "FINISHED"
            save_data(data, sha)
        else:
            save_data(data, sha)
            await self.post_next(chan)

    async def post_next(self, channel):
        data, sha = load_data()
        if len(data['bracket']) < 2: return
        a, b = data['bracket'].pop(0), data['bracket'].pop(0)
        
        match_num = len(data.get('finished_matches', [])) + 1
        round_title = get_round_name(len(data['bracket']) + 2)

        await channel.send(f"It's time for Match {match_num} of the {round_title}!\n\n**{a['name']} vs {b['name']}**")
        
        await channel.send(f"**{a['name']}**\n{a.get('desc', '')}")
        await channel.send(a['image'])

        await channel.send(f"**{b['name']}**\n{b.get('desc', '')}")
        await channel.send(b['image'])

        view = VoteView(a['name'], b['name'])
        poll_msg = await channel.send("Cast your vote! (Only one choice allowed):", view=view)
        
        data['current_match'] = {
            "item_a": a, "item_b": b, "message_id": poll_msg.id, 
            "channel_id": channel.id, "end_at": datetime.datetime.now().timestamp() + 60, "votes": {}
        }
        data['status'] = "MATCH_ACTIVE"
        save_data(data, sha)

bot = WC_Bot()

@bot.tree.command(name="choosecategory")
async def choosecategory(interaction: discord.Interaction):
    if not any(r.id in ALLOWED_ROLE_IDS for r in interaction.user.roles): return
    data, sha = load_data()
    if not data['suggestions']: return await interaction.response.send_message("No suggestions available.")
    pick = random.choice(data['suggestions'])
    data['current_cat'] = pick['name']
    data['title'] = pick['name']
    save_data(data, sha)
    await interaction.response.send_message(f"Selected Category: **{pick['name']}**")

@bot.tree.command(name="startworldcup")
async def startworldcup(interaction: discord.Interaction):
    if not any(r.id in ALLOWED_ROLE_IDS for r in interaction.user.roles): return
    data, sha = load_data()
    if not data.get('current_cat'): return await interaction.response.send_message("Pick a category first with /choosecategory!")
    random.shuffle(data['items'])
    data['bracket'] = data['items']
    data['finished_matches'], data['winners_pool'] = [], []
    save_data(data, sha)
    await interaction.response.send_message(f"🏆 **THE {data['current_cat'].upper()} WORLD CUP BEGINS!**")
    await bot.post_next(interaction.channel)

@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"Logged in as {bot.user}")

if __name__ == "__main__":
    keep_alive()
    bot.run(TOKEN)
