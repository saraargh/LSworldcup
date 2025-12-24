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
    # Render and other hosts use environment variable PORT
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run_flask)
    t.start()

# =========================================================
# CONFIG
# =========================================================
TOKEN = os.getenv("WC_TOKEN") or os.getenv("TOKEN")
UK_TZ = pytz.timezone("Europe/London")

GITHUB_REPO = os.getenv("GITHUB_REPO", "saraargh/LSworldcup")
GITHUB_FILE_PATH = os.getenv("TOURNAMENT_JSON_PATH", "tournament_data.json")
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main")

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN") or os.getenv("WC_GITHUB_TOKEN") or os.getenv("WC_TOKEN")
HEADERS = {"Authorization": f"token {GITHUB_TOKEN}"} if GITHUB_TOKEN else {}

ALLOWED_ROLE_IDS = [
    1413545658006110401, 1404098545006546954, 1420817462290681936, 
    1404105470204969000, 1404104881098195015
]

# =========================================================
# GITHUB API HELPERS
# =========================================================
def _json_url():
    return f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_FILE_PATH}"

def load_data():
    try:
        r = requests.get(_json_url(), headers=HEADERS, params={"ref": GITHUB_BRANCH}, timeout=20)
        if r.status_code == 200:
            content = r.json()
            raw = base64.b64decode(content["content"]).decode()
            return json.loads(raw), content.get("sha")
    except Exception as e:
        print(f"Load Error: {e}")
    return {"status": "IDLE", "items": [], "suggestions": [], "leaderboard": [], "bracket": [], "winners_pool": [], "finished_matches": []}, None

def save_data(data, sha=None):
    try:
        if not sha: _, sha = load_data()
        payload = {
            "message": "Update tournament data",
            "content": base64.b64encode(json.dumps(data, indent=4).encode()).decode(),
            "branch": GITHUB_BRANCH,
            "sha": sha
        }
        requests.put(_json_url(), headers=HEADERS, data=json.dumps(payload), timeout=20)
    except Exception as e:
        print(f"Save Error: {e}")

# =========================================================
# ROUND NAME HELPER
# =========================================================
def get_round_name(count):
    if count > 16: return "Round of 32"
    if count > 8: return "Round of 16"
    if count > 4: return "Quarter-Finals"
    if count > 2: return "Semi-Finals"
    return "The Grand Final"

# =========================================================
# UI COMPONENTS
# =========================================================

class CategoryModal(ui.Modal, title="Suggest a Category"):
    category = ui.TextInput(label="Category Name", min_length=2)
    async def on_submit(self, interaction: discord.Interaction):
        data, sha = load_data()
        data.setdefault('suggestions', []).append({
            "name": self.category.value.strip(), 
            "user": interaction.user.display_name
        })
        save_data(data, sha)
        await interaction.response.send_message(f"✅ Category suggestion added: **{self.category.value}**")

class SuggestionModal(ui.Modal, title="World Cup Entry"):
    name = ui.TextInput(label="Item Name")
    desc = ui.TextInput(label="Description", style=discord.TextStyle.paragraph)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.send_message(
            f"✅ Text received for **{self.name.value}**. \n📸 **Final Step:** Please upload the image in this channel now.", 
            ephemeral=True
        )
        
        def check(m): return m.author == interaction.user and m.attachments
        try:
            msg = await interaction.client.wait_for('message', timeout=60.0, check=check)
            data, sha = load_data()
            new_item = {
                "name": self.name.value.strip(), 
                "desc": self.desc.value.strip(), 
                "image": msg.attachments[0].url, 
                "user": interaction.user.display_name
            }
            data['items'].append(new_item)
            save_data(data, sha)
            await interaction.followup.send(f"🎊 **{interaction.user.display_name}** has successfully submitted **{self.name.value}**!")
        except asyncio.TimeoutError:
            await interaction.followup.send("❌ Image timeout. Submission cancelled.", ephemeral=True)

class ItemGallery(ui.View):
    def __init__(self, items):
        super().__init__(timeout=60)
        self.items, self.index = items, 0
    def create_embed(self):
        item = self.items[self.index]
        embed = discord.Embed(title=item['name'], description=item['desc'], color=0x3498db).set_image(url=item['image'])
        embed.set_footer(text=f"Item {self.index+1}/{len(self.items)} | Added by {item['user']}")
        return embed
    @ui.button(label="⬅️", style=discord.ButtonStyle.gray)
    async def prev(self, i, b):
        self.index = (self.index - 1) % len(self.items)
        await i.response.edit_message(embed=self.create_embed())
    @ui.button(label="➡️", style=discord.ButtonStyle.gray)
    async def next(self, i, b):
        self.index = (self.index + 1) % len(self.items)
        await i.response.edit_message(embed=self.create_embed())

# =========================================================
# BOT CORE
# =========================================================
class WC_Bot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.all())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        self.auto_checker.start()

    @tasks.loop(minutes=5) # FOR TESTING: Change to seconds=30
    async def auto_checker(self):
        data, sha = load_data()
        if data.get("status") == "MATCH_ACTIVE" and data.get("current_match"):
            if datetime.datetime.now().timestamp() > data['current_match']['end_at']:
                await self.resolve_match(data, sha)

    async def resolve_match(self, data, sha):
        match = data['current_match']
        chan = self.get_channel(match['channel_id'])
        msg = await chan.fetch_message(match['message_id'])
        v1 = next((r.count - 1 for r in msg.reactions if str(r.emoji) == "🔴"), 0)
        v2 = next((r.count - 1 for r in msg.reactions if str(r.emoji) == "🔵"), 0)
        
        winner = match['item_a'] if v1 > v2 else match['item_b'] if v2 > v1 else random.choice([match['item_a'], match['item_b']])
        data.setdefault('finished_matches', []).append({"name": f"{match['item_a']['name']} vs {match['item_b']['name']}", "winner": winner['name']})
        data.setdefault('winners_pool', []).append(winner)
        data['current_match'] = None

        embed = discord.Embed(title=f"🏆 Winner: {winner['name']}", color=0x2ecc71).set_image(url=winner['image'])
        await chan.send(embed=embed)

        if not data['bracket'] and len(data['winners_pool']) > 1:
            data['bracket'], data['winners_pool'] = data['winners_pool'], []
            await chan.send(f"🛡️ **Round Complete! Moving to {get_round_name(len(data['bracket']))}.**")
            save_data(data, sha)
            await self.post_next(chan)
        elif not data['bracket'] and len(data['winners_pool']) == 1:
            await chan.send(f"🎊 **THE FINAL CHAMPION IS {winner['name']}!**")
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
        
        await channel.send(f"⚔️ **{get_round_name(len(data['bracket'])+2)}:** {a['name']} vs {b['name']}")
        for x in [a,b]:
            await channel.send(embed=discord.Embed(title=x['name']).set_image(url=x['image']))
        
        poll = await channel.send("Vote: 🔴 or 🔵")
        await poll.add_reaction("🔴"); await poll.add_reaction("🔵")
        
        # FOR TESTING: Change 86400 (24h) to 120 (2 mins)
        data['current_match'] = {"item_a": a, "item_b": b, "message_id": poll.id, "channel_id": channel.id, "end_at": datetime.datetime.now().timestamp() + 86400}
        data['status'] = "MATCH_ACTIVE"
        save_data(data, sha)

bot = WC_Bot()

# =========================================================
# COMMANDS
# =========================================================

@bot.tree.command(name="categoryrequest")
async def categoryrequest(interaction: discord.Interaction):
    if not any(r.id in ALLOWED_ROLE_IDS for r in interaction.user.roles): return
    view = ui.View(timeout=None)
    btn = ui.Button(label="Suggest Category", style=discord.ButtonStyle.green)
    btn.callback = lambda i: i.response.send_modal(CategoryModal())
    view.add_item(btn)
    await interaction.response.send_message("Open for category suggestions!", view=view)

@bot.tree.command(name="choosecategory")
async def choosecategory(interaction: discord.Interaction):
    if not any(r.id in ALLOWED_ROLE_IDS for r in interaction.user.roles): return
    data, sha = load_data()
    if not data['suggestions']: return await interaction.response.send_message("No suggestions found.")
    pick = random.choice(data['suggestions'])
    data['current_cat'] = pick['name']
    save_data(data, sha)
    view = ui.View()
    btn = ui.Button(label="Submit Entry", style=discord.ButtonStyle.primary)
    btn.callback = lambda i: i.response.send_modal(SuggestionModal())
    view.add_item(btn)
    await interaction.response.send_message(f"Selected Category: **{pick['name']}**", view=view)

@bot.tree.command(name="listitems")
async def listitems(interaction: discord.Interaction):
    data, _ = load_data()
    if not data.get('items'): return await interaction.response.send_message("The list is empty.")
    await interaction.response.send_message(f"Total Entries: {len(data['items'])}/32", view=ItemGallery(data['items']))

@bot.tree.command(name="removeitem")
async def removeitem(interaction: discord.Interaction, name: str):
    if not any(r.id in ALLOWED_ROLE_IDS for r in interaction.user.roles): return
    data, sha = load_data()
    data['items'] = [i for i in data['items'] if i['name'].lower() != name.lower()]
    save_data(data, sha)
    await interaction.response.send_message(f"Removed **{name}**.")

@bot.tree.command(name="scoreboard")
async def scoreboard(interaction: discord.Interaction):
    data, _ = load_data()
    embed = discord.Embed(title="Tournament Scoreboard", color=0x3498db)
    curr = data.get('current_match')
    embed.add_field(name="Current Match", value=f"{curr['item_a']['name']} vs {curr['item_b']['name']}" if curr else "None")
    history = "\n".join([f"✅ {m['name']} -> {m['winner']}" for m in data.get('finished_matches', [])[-5:]])
    embed.add_field(name="Recent Results", value=history or "None", inline=False)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="leaderboard")
async def leaderboard(interaction: discord.Interaction):
    data, _ = load_data()
    board = data.get('leaderboard', [])
    if not board: return await interaction.response.send_message("No hall of fame yet!")
    desc = "\n".join([f"🏆 {e['user']} ({e['item']})" for e in board])
    await interaction.response.send_message(embed=discord.Embed(title="Leaderboard", description=desc))

@bot.tree.command(name="startworldcup")
async def startworldcup(interaction: discord.Interaction, title: str):
    if not any(r.id in ALLOWED_ROLE_IDS for r in interaction.user.roles): return
    data, sha = load_data()
    random.shuffle(data['items'])
    data['bracket'], data['title'], data['finished_matches'], data['winners_pool'] = data['items'], title, [], []
    save_data(data, sha)
    await interaction.response.send_message(f"🏆 Starting: {title}")
    await bot.post_next(interaction.channel)

@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"Bot is live as {bot.user}")

# =========================================================
# STARTING THE BOT
# =========================================================
if __name__ == "__main__":
    keep_alive() 
    bot.run(TOKEN)
