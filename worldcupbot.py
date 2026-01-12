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

# =========================================================
# KEEP ALIVE & CONFIG
# =========================================================
app = Flask('')
@app.route('/')
def home(): return "Bot is alive!"
def run_flask(): app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 8080)))
def keep_alive(): Thread(target=run_flask).start()

TOKEN = os.getenv("WC_TOKEN") or os.getenv("TOKEN")
GITHUB_REPO = os.getenv("GITHUB_REPO", "saraargh/LSworldcup")
GITHUB_FILE_PATH = os.getenv("TOURNAMENT_JSON_PATH", "tournament_data.json")
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN") or os.getenv("WC_GITHUB_TOKEN") or os.getenv("WC_TOKEN")
HEADERS = {"Authorization": f"token {GITHUB_TOKEN}"} if GITHUB_TOKEN else {}
ALLOWED_ROLE_IDS = [1413545658006110401, 1404098545006546954, 1420817462290681936, 1404105470204969000, 1404104881098195015]

# =========================================================
# DATA PERSISTENCE
# =========================================================
def load_data():
    try:
        r = requests.get(f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_FILE_PATH}", headers=HEADERS, params={"ref": GITHUB_BRANCH}, timeout=20)
        if r.status_code == 200:
            content = r.json()
            raw = base64.b64decode(content["content"]).decode()
            return json.loads(raw), content.get("sha")
    except Exception: pass
    return {"status": "IDLE", "items": [], "suggestions": [], "leaderboard": [], "bracket": [], "winners_pool": [], "finished_matches": [], "current_match": None, "current_cat": None}, None

def save_data(data, sha=None):
    if not sha: _, sha = load_data()
    payload = {"message": "Sync", "content": base64.b64encode(json.dumps(data, indent=4).encode()).decode(), "branch": GITHUB_BRANCH, "sha": sha}
    requests.put(f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_FILE_PATH}", headers=HEADERS, data=json.dumps(payload), timeout=20)

def get_round_name(count):
    if count > 16: return "Round of 32"
    if count > 8: return "Round of 16"
    if count > 4: return "Quarter-Finals"
    if count > 2: return "Semi-Finals"
    return "Grand Final"

# =========================================================
# UI COMPONENTS (PERSISTENT)
# =========================================================

class ItemGallery(ui.View):
    def __init__(self, items=None):
        super().__init__(timeout=None)
        self.items = items or []
        self.index = 0
        self.mode = "GALLERY"

    def create_content(self):
        if not self.items: return discord.Embed(title="Empty", description="No items found.")
        if self.mode == "GALLERY":
            item = self.items[self.index]
            embed = discord.Embed(title=item['name'], description=item.get('desc', ''), color=0x3498db).set_image(url=item['image'])
            embed.set_footer(text=f"Entry {self.index+1}/{len(self.items)} | Added by {item.get('user', 'Unknown')}")
            return embed
        txt = "\n".join([f"{idx+1}. **{x['name']}**" for idx, x in enumerate(self.items)])
        return discord.Embed(title="📋 All Entries", description=txt, color=0x3498db)

    @ui.button(label="⬅️", style=discord.ButtonStyle.gray, custom_id="gal_prev", row=0)
    async def prev(self, i, b):
        data, _ = load_data()
        self.items = data.get('items', [])
        self.index = (self.index - 1) % len(self.items)
        await i.response.edit_message(embed=self.create_content())

    @ui.button(label="➡️", style=discord.ButtonStyle.gray, custom_id="gal_next", row=0)
    async def next(self, i, b):
        data, _ = load_data()
        self.items = data.get('items', [])
        self.index = (self.index + 1) % len(self.items)
        await i.response.edit_message(embed=self.create_content())

    @ui.button(label="Toggle View", style=discord.ButtonStyle.blurple, custom_id="gal_toggle", row=1)
    async def toggle(self, i, b):
        data, _ = load_data()
        self.items = data.get('items', [])
        self.mode = "LIST" if self.mode == "GALLERY" else "GALLERY"
        await i.response.edit_message(embed=self.create_content())

class MatchView(ui.View):
    def __init__(self, item_a=None, item_b=None, round_name=None, match_num=None):
        super().__init__(timeout=None)
        self.item_a, self.item_b = item_a, item_b
        self.round_name, self.match_num = round_name, match_num
        
        if item_a and item_b:
            self.vote_a.label = f"Vote for {item_a['name']}"
            self.vote_b.label = f"Vote for {item_b['name']}"

    def create_embed(self, page=0):
        item = self.item_a if page == 0 else self.item_b
        embed = discord.Embed(
            title=f"Match {self.match_num}: {self.round_name}",
            description=f"**{self.item_a['name']}** vs **{self.item_b['name']}**\n\n**Currently Viewing:** {item['name']}\n{item.get('desc', '')}",
            color=0x3498db
        ).set_image(url=item['image'])
        embed.set_footer(text=f"Entry {page+1} of 2 | Use arrows to compare entries")
        return embed

    @ui.button(label="⬅️ Previous Entry", style=discord.ButtonStyle.gray, custom_id="match_prev", row=0)
    async def prev_page(self, i: discord.Interaction, b: ui.Button):
        data, _ = load_data()
        m = data['current_match']
        if not m or i.message.id != m.get('message_id'):
            return await i.response.send_message("❌ This match post is inactive. View the current match.", ephemeral=True)
        self.item_a, self.item_b = m['item_a'], m['item_b']
        self.match_num, self.round_name = len(data['finished_matches'])+1, get_round_name(len(data['bracket'])+2)
        await i.response.edit_message(embed=self.create_embed(0))

    @ui.button(label="Next Entry ➡️", style=discord.ButtonStyle.gray, custom_id="match_next", row=0)
    async def next_page(self, i: discord.Interaction, b: ui.Button):
        data, _ = load_data()
        m = data['current_match']
        if not m or i.message.id != m.get('message_id'):
            return await i.response.send_message("❌ This match post is inactive. View the current match.", ephemeral=True)
        self.item_a, self.item_b = m['item_a'], m['item_b']
        self.match_num, self.round_name = len(data['finished_matches'])+1, get_round_name(len(data['bracket'])+2)
        await i.response.edit_message(embed=self.create_embed(1))

    @ui.button(style=discord.ButtonStyle.danger, custom_id="vote_a", row=1)
    async def vote_a(self, i: discord.Interaction, b: ui.Button):
        data, sha = load_data()
        match = data.get("current_match")
        # SECURITY CHECK: Is this message the active one?
        if not match or i.message.id != match.get("message_id"):
            return await i.response.send_message("❌ This match has already ended. Please vote in the latest match post!", ephemeral=True)
        
        if str(i.user.id) in match.get("votes", {}):
            return await i.response.send_message("You've already voted!", ephemeral=True)
        
        match["votes"][str(i.user.id)] = "A"
        save_data(data, sha)
        await i.response.send_message(f"✅ Voted for {match['item_a']['name']}!", ephemeral=True)

    @ui.button(style=discord.ButtonStyle.primary, custom_id="vote_b", row=1)
    async def vote_b(self, i: discord.Interaction, b: ui.Button):
        data, sha = load_data()
        match = data.get("current_match")
        # SECURITY CHECK: Is this message the active one?
        if not match or i.message.id != match.get("message_id"):
            return await i.response.send_message("❌ This match has already ended. Please vote in the latest match post!", ephemeral=True)
            
        if str(i.user.id) in match.get("votes", {}):
            return await i.response.send_message("You've already voted!", ephemeral=True)
            
        match["votes"][str(i.user.id)] = "B"
        save_data(data, sha)
        await i.response.send_message(f"✅ Voted for {match['item_b']['name']}!", ephemeral=True)

# =========================================================
# BOT CORE
# =========================================================
class WC_Bot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.all())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        self.add_view(MatchView()) 
        self.add_view(ItemGallery())
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
        v = list(match.get("votes", {}).values())
        v1, v2 = v.count("A"), v.count("B")
        winner = match['item_a'] if v1 > v2 else (match['item_b'] if v2 > v1 else random.choice([match['item_a'], match['item_b']]))
        data.setdefault('finished_matches', []).append({"name": f"{match['item_a']['name']} vs {match['item_b']['name']}", "winner": winner['name'], "score": f"{v1}-{v2}"})
        data.setdefault('winners_pool', []).append(winner)
        data['current_match'] = None
        embed = discord.Embed(title=f"Match Result", description=f"**{winner['name']}** advances! ({v1} - {v2})", color=0x2ecc71).set_image(url=winner['image'])
        await chan.send(embed=embed)
        if not data['bracket'] and len(data['winners_pool']) > 1:
            data['bracket'], data['winners_pool'] = data['winners_pool'], []
            await chan.send(f"🛡️ **Round Over. Moving to {get_round_name(len(data['bracket']))}**")
        elif not data['bracket'] and len(data['winners_pool']) == 1:
            data.setdefault('leaderboard', []).append({"user": winner['user'], "item": winner['name'], "cat": data['current_cat']})
            data['status'] = "FINISHED"
            await chan.send(f"🎊 **TOURNAMENT CHAMPION: {winner['name']}!**")
            save_data(data, sha); return
        save_data(data, sha)
        await self.post_next(chan)

    async def post_next(self, channel):
        data, sha = load_data()
        if not data['bracket']: return
        a, b = data['bracket'].pop(0), data['bracket'].pop(0)
        round_n = get_round_name(len(data['bracket']) + 2)
        match_n = len(data['finished_matches']) + 1
        view = MatchView(a, b, round_n, match_n)
        msg = await channel.send(embed=view.create_embed(0), view=view)
        data['current_match'] = {"item_a": a, "item_b": b, "message_id": msg.id, "channel_id": channel.id, "end_at": datetime.datetime.now().timestamp() + 86400, "votes": {}}
        data['status'] = "MATCH_ACTIVE"
        save_data(data, sha)

bot = WC_Bot()

# --- COMMANDS ---

@bot.tree.command(name="opensuggestions")
async def opensuggestions(i: discord.Interaction):
    if not any(r.id in ALLOWED_ROLE_IDS for r in i.user.roles): return
    await i.response.send_message(embed=discord.Embed(title="💡 Suggestions Open!", description="Theme suggestions are open! Use `/suggestcategory`.", color=0xf1c40f))

@bot.tree.command(name="listcategories")
async def listcategories(i: discord.Interaction):
    data, _ = load_data()
    if not data['suggestions']: return await i.response.send_message("No suggestions.", ephemeral=True)
    list_txt = "\n".join([f"• **{s['name']}** (by {s['user']})" for s in data['suggestions']])
    await i.response.send_message(embed=discord.Embed(title="📋 Suggested Categories", description=list_txt, color=0x3498db))

@bot.tree.command(name="choosecategory")
async def choosecategory(i: discord.Interaction):
    if not any(r.id in ALLOWED_ROLE_IDS for r in i.user.roles): return
    data, sha = load_data()
    if data.get("status") == "MATCH_ACTIVE": return await i.response.send_message("Tournament active!", ephemeral=True)
    if not data['suggestions']: return await i.response.send_message("No suggestions.")
    await i.response.send_message("🎰 **Picking a theme...**")
    await asyncio.sleep(2)
    pick = random.choice(data['suggestions'])
    data['current_cat'], data['suggestions'] = pick['name'], []
    save_data(data, sha)
    await i.edit_original_response(content=f"🎉 Category: **{pick['name'].upper()}**!\nUse `/additem` to enter!")

@bot.tree.command(name="additem")
async def additem(i: discord.Interaction, name: str, description: str, image_url: str):
    data, sha = load_data()
    if data.get("status") == "MATCH_ACTIVE": return await i.response.send_message("Locked during tournament!", ephemeral=True)
    if not data.get("current_cat"): return await i.response.send_message("Pick category first!", ephemeral=True)
    if len(name) > 75: return await i.response.send_message(f"❌ Name too long ({len(name)}/75 chars).", ephemeral=True)
    data['items'].append({"name": name, "desc": description, "image": image_url, "user": i.user.name})
    save_data(data, sha)
    await i.response.send_message(f"✅ Added **{name}**!")

@bot.tree.command(name="listitems")
async def listitems(i: discord.Interaction):
    data, _ = load_data()
    if not data.get('items'): return await i.response.send_message("No entries.")
    view = ItemGallery(data['items'])
    await i.response.send_message(embed=view.create_content(), view=view)

@bot.tree.command(name="startworldcup")
async def startworldcup(i: discord.Interaction):
    if not any(r.id in ALLOWED_ROLE_IDS for r in i.user.roles): return
    data, sha = load_data()
    if not data.get('current_cat'): return await i.response.send_message("Pick category!")
    if len(data['items']) < 2: return await i.response.send_message("Need 2+ items!")
    random.shuffle(data['items'])
    data['bracket'], data['finished_matches'], data['winners_pool'] = data['items'], [], []
    save_data(data, sha)
    await i.response.send_message(f"🏆 **THE {data['current_cat'].upper()} WORLD CUP BEGINS!**")
    await bot.post_next(i.channel)

@bot.tree.command(name="nextmatch")
async def nextmatch(i: discord.Interaction):
    if not any(r.id in ALLOWED_ROLE_IDS for r in i.user.roles): return
    data, sha = load_data()
    if data.get("status") != "MATCH_ACTIVE": return await i.response.send_message("No match active.", ephemeral=True)
    await i.response.send_message("Advancing...", ephemeral=True)
    await bot.resolve_match(data, sha)

@bot.tree.command(name="endcup")
async def endcup(i: discord.Interaction):
    if not any(r.id in ALLOWED_ROLE_IDS for r in i.user.roles): return
    data, sha = load_data()
    data['status'], data['current_match'], data['bracket'], data['finished_matches'] = "IDLE", None, [], []
    save_data(data, sha)
    await i.response.send_message("🛑 Tournament ended.")

@bot.tree.command(name="resetcup")
async def resetcup(i: discord.Interaction):
    if not any(r.id in ALLOWED_ROLE_IDS for r in i.user.roles): return
    data, sha = load_data()
    new_data = {"status": "IDLE", "items": [], "suggestions": [], "leaderboard": data.get('leaderboard', []), "bracket": [], "winners_pool": [], "finished_matches": [], "current_match": None, "current_cat": None}
    save_data(new_data, sha)
    await i.response.send_message("🧨 **Reset!** Items, suggestions, and theme wiped.")

@bot.tree.command(name="suggestcategory")
async def suggestcategory(i: discord.Interaction, name: str):
    data, sha = load_data()
    if data.get("status") == "MATCH_ACTIVE": return await i.response.send_message("Locked!", ephemeral=True)
    data.setdefault('suggestions', []).append({"name": name, "user": i.user.name})
    save_data(data, sha)
    await i.response.send_message(f"💡 Suggestion: **{name}**", ephemeral=True)

@bot.tree.command(name="scoreboard")
async def scoreboard(i: discord.Interaction):
    data, _ = load_data()
    embed = discord.Embed(title=f"📊 Scoreboard: {data.get('current_cat', 'None')}", color=0x3498db)
    prev = "\n".join([f"✅ {m['name']} ({m['winner']})" for m in data.get('finished_matches', [])[-5:]])
    embed.add_field(name="Recent", value=prev or "None", inline=False)
    curr = data.get('current_match')
    embed.add_field(name="Ongoing", value=f"🔥 {curr['item_a']['name']} vs {curr['item_b']['name']}" if curr else "None", inline=False)
    await i.response.send_message(embed=embed)

@bot.tree.command(name="leaderboard")
async def leaderboard(i: discord.Interaction):
    data, _ = load_data()
    lb = "\n".join([f"👑 **{x['item']}** ({x['cat']})" for x in data.get('leaderboard', [])])
    await i.response.send_message(f"🏆 **Hall of Fame**\n{lb or 'None'}")

@bot.event
async def on_ready():
    await bot.tree.sync()
    print("Online.")

if __name__ == "__main__":
    keep_alive()
    bot.run(TOKEN)
