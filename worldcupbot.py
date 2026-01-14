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
# KEEP ALIVE SERVER (RENDER COMPLIANCE)
# =========================================================
app = Flask('')

@app.route('/')
def home():
    return "Bot is alive and running!"

def run_flask():
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 8080)))

def keep_alive():
    t = Thread(target=run_flask)
    t.start()

# =========================================================
# CONFIGURATION (MATCHED TO YOUR RENDER VARIABLES)
# =========================================================
TOKEN = os.getenv("WC_TOKEN")
GITHUB_REPO = os.getenv("GITHUB_REPO")
GITHUB_FILE_PATH = os.getenv("TOURNAMENT_JSON_PATH")
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
HEADERS = {"Authorization": f"token {GITHUB_TOKEN}"} if GITHUB_TOKEN else {}

ALLOWED_ROLE_IDS = [
    1413545658006110401, 
    1404098545006546954, 
    1420817462290681936, 
    1404105470204969000, 
    1404104881098195015
]
STORAGE_CHANNEL_ID = 1461047591528562801

# =========================================================
# DATA PERSISTENCE (GITHUB ENGINE)
# =========================================================
def load_data():
    try:
        # Cache Buster: Forces GitHub to bypass its 60s cache
        cb = random.randint(1, 999999)
        url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_FILE_PATH}?cb={cb}"
        r = requests.get(url, headers=HEADERS, params={"ref": GITHUB_BRANCH}, timeout=20)
        
        if r.status_code == 200:
            content = r.json()
            raw_data = base64.b64decode(content["content"]).decode()
            return json.loads(raw_data), content.get("sha")
            
    except Exception as e:
        print(f"Load Error: {e}")
        
    return {
        "status": "IDLE",
        "items": [],
        "suggestions": [],
        "leaderboard": [],
        "bracket": [],
        "winners_pool": [],
        "finished_matches": [],
        "current_match": None,
        "current_cat": None,
        "final_winner": None
    }, None

def save_data(data, sha=None):
    if not sha:
        _, sha = load_data()
        
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_FILE_PATH}"
    encoded = base64.b64encode(json.dumps(data, indent=4).encode()).decode()
    
    payload = {
        "message": "Tournament Data Sync",
        "content": encoded,
        "branch": GITHUB_BRANCH,
        "sha": sha
    }
    
    r = requests.put(url, headers=HEADERS, data=json.dumps(payload), timeout=20)
    return r.status_code

def get_round_name(count):
    if count > 16:
        return "Round of 32"
    if count > 8:
        return "Round of 16"
    if count > 4:
        return "Quarter-Finals"
    if count > 2:
        return "Semi-Finals"
    return "Grand Final"

# =========================================================
# UI SYSTEM - PERSISTENT VIEWS
# =========================================================

class ResetConfirmView(ui.View):
    def __init__(self):
        super().__init__(timeout=60)

    @ui.button(label="Confirm Reset", style=discord.ButtonStyle.danger)
    async def confirm(self, i: discord.Interaction, b: ui.Button):
        data, sha = load_data()
        data.update({
            "status": "IDLE",
            "title": [],
            "items": [], 
            "suggestions": [], 
            "bracket": [], 
            "winners_pool": [], 
            "finished_matches": [], 
            "current_match": None, 
            "current_cat": None, 
            "final_winner": None
        })
        save_data(data, sha)
        await i.response.edit_message(content="🧨 **Tournament has been completely reset.**", view=None)

class EndConfirmView(ui.View):
    def __init__(self, bot_instance):
        super().__init__(timeout=60)
        self.bot = bot_instance

    @ui.button(label="Force End Tournament", style=discord.ButtonStyle.danger)
    async def confirm(self, i: discord.Interaction, b: ui.Button):
        data, sha = load_data()
        data.update({
            "status": "IDLE", 
            "items": [], 
            "suggestions": [], 
            "bracket": [], 
            "winners_pool": [], 
            "finished_matches": [], 
            "current_match": None, 
            "current_cat": None, 
            "final_winner": None
        })
        save_data(data, sha)
        await i.response.edit_message(content="⚠️ **Tournament ended early by Admin override.**", view=None)

class HistoryView(ui.View):
    def __init__(self, history_data):
        super().__init__(timeout=None)
        self.data = history_data
        self.page = 0

    def create_embed(self):
        # Changed to 5 entries per page
        start = self.page * 5
        chunk = self.data[start:start+5]
        desc = ""
        
        for idx, entry in enumerate(chunk):
            # Layout: Bold Category first, then Item and User
            desc += (
                f"{start+idx+1}. 🏆 **{entry['cat'].upper()}**\n"
                f"└ Winner: **{entry['item']}**\n"
                f"└ Submitted by: {entry['user']}\n\n"
            )
            
        embed = discord.Embed(
            title="🎖️ Hall of Fame History", 
            description=desc or "The archives are currently empty.", 
            color=0xf1c40f
        )
        
        # Adjust total pages calculation for 5 per page
        total_pages = (len(self.data) - 1) // 5 + 1 if self.data else 1
        embed.set_footer(text=f"Page {self.page+1} of {total_pages}")
        return embed

    @ui.button(label="⬅️ Previous", style=discord.ButtonStyle.gray, custom_id="hist_prev")
    async def prev(self, i, b):
        self.page = max(0, self.page - 1)
        await i.response.edit_message(embed=self.create_embed())

    @ui.button(label="Next ➡️", style=discord.ButtonStyle.gray, custom_id="hist_next")
    async def next(self, i, b):
        # Adjusting page limit check for 5 per page
        if (self.page + 1) * 5 < len(self.data):
            self.page += 1
        await i.response.edit_message(embed=self.create_embed())


class ItemGallery(ui.View):
    def __init__(self, items=None):
        super().__init__(timeout=None)
        self.items = items or []
        self.index = 0
        self.mode = "GALLERY"

    def create_content(self):
        if not self.items:
            return discord.Embed(title="Empty", description="No items found.")
        
        if self.mode == "GALLERY":
            item = self.items[self.index]
            embed = discord.Embed(
                title=item['name'], 
                description=item.get('desc', ''), 
                color=0x3498db
            ).set_image(url=item['image'])
            embed.set_footer(text=f"Entry {self.index+1}/{len(self.items)} | Added by {item.get('user', 'Unknown')}")
            return embed
        
        txt = "\n".join([f"{idx+1}. **{x['name']}**" for idx, x in enumerate(self.items)])
        return discord.Embed(title="📋 Entry List", description=txt or "None", color=0x3498db)

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
        self.item_a = item_a
        self.item_b = item_b
        self.round_name = round_name
        self.match_num = match_num
        if item_a and item_b:
            self.vote_a.label = f"Vote: {item_a['name']}"
            self.vote_b.label = f"Vote: {item_b['name']}"

    def create_embed(self, page=0):
        item = self.item_a if page == 0 else self.item_b
        embed = discord.Embed(
            title=f"Match {self.match_num}: {self.round_name}", 
            description=f"**{self.item_a['name']}** vs **{self.item_b['name']}**\n\n**Viewing:** {item['name']}\n{item.get('desc', '')}", 
            color=0x3498db
        ).set_image(url=item['image'])
        embed.set_footer(text=f"Viewing {page+1}/2 | Compare both before voting!")
        return embed

    @ui.button(label="⬅️ View Previous", style=discord.ButtonStyle.gray, custom_id="match_prev", row=0)
    async def prev_page(self, i: discord.Interaction, b: ui.Button):
        data, _ = load_data()
        m = data['current_match']
        self.item_a = m['item_a']
        self.item_b = m['item_b']
        self.match_num = len(data['finished_matches']) + 1
        self.round_name = get_round_name(len(data['bracket']) + 2)
        await i.response.edit_message(embed=self.create_embed(0))

    @ui.button(label="View Next ➡️", style=discord.ButtonStyle.gray, custom_id="match_next", row=0)
    async def next_page(self, i: discord.Interaction, b: ui.Button):
        data, _ = load_data()
        m = data['current_match']
        self.item_a = m['item_a']
        self.item_b = m['item_b']
        self.match_num = len(data['finished_matches']) + 1
        self.round_name = get_round_name(len(data['bracket']) + 2)
        await i.response.edit_message(embed=self.create_embed(1))

    @ui.button(style=discord.ButtonStyle.danger, custom_id="vote_a", row=1)
    async def vote_a(self, i: discord.Interaction, b: ui.Button):
        data, sha = load_data()
        match = data.get("current_match")
        if not match or str(i.user.id) in match.get("votes", {}):
            return await i.response.send_message("You have already voted for this match!", ephemeral=True)
        match["votes"][str(i.user.id)] = "A"
        save_data(data, sha)
        await i.response.send_message(f"✅ Your vote for **{match['item_a']['name']}** has been recorded!", ephemeral=True)

    @ui.button(style=discord.ButtonStyle.primary, custom_id="vote_b", row=1)
    async def vote_b(self, i: discord.Interaction, b: ui.Button):
        data, sha = load_data()
        match = data.get("current_match")
        if not match or str(i.user.id) in match.get("votes", {}):
            return await i.response.send_message("You have already voted for this match!", ephemeral=True)
        match["votes"][str(i.user.id)] = "B"
        save_data(data, sha)
        await i.response.send_message(f"✅ Your vote for **{match['item_b']['name']}** has been recorded!", ephemeral=True)

# =========================================================
# THE BOT CLASS
# =========================================================
class WC_Bot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.all())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        # Persistent views survive bot restarts
        self.add_view(MatchView())
        self.add_view(HistoryView([]))
        self.add_view(ItemGallery([]))

    async def resolve_match(self, data, sha):
        match = data['current_match']
        if not match: 
            return
        
        chan = self.get_channel(match['channel_id'])
        votes = list(match.get("votes", {}).values())
        v1, v2 = votes.count("A"), votes.count("B")
        
        if v1 > v2:
            winner = match['item_a']
        elif v2 > v1:
            winner = match['item_b']
        else:
            winner = random.choice([match['item_a'], match['item_b']])
        
        data.setdefault('finished_matches', []).append({
            "name": f"{match['item_a']['name']} vs {match['item_b']['name']}", 
            "winner": winner['name'], 
            "score": f"{v1}-{v2}"
        })
        data.setdefault('winners_pool', []).append(winner)
        data['current_match'] = None
        
        embed = discord.Embed(title="Match Result", description=f"**{winner['name']}** advances to the next round! ({v1}-{v2})", color=0x2ecc71)
        embed.set_image(url=winner['image'])
        await chan.send(embed=embed)
        
        # Round Management Logic
        if not data['bracket'] and len(data['winners_pool']) > 1:
            data['bracket'] = data['winners_pool']
            data['winners_pool'] = []
            next_r = get_round_name(len(data['bracket']))
            await chan.send(f"🛡️ **All matches in this round are complete. Moving to {next_r}!**")
        elif not data['bracket'] and len(data['winners_pool']) == 1:
            data['final_winner'] = winner
            data['status'] = "FINISHED"
            save_data(data, sha)
            await chan.send("🏁 **THE GRAND FINAL IS OVER!** Admin, please use `/endcup` to archive the results!")
            return
        
        save_data(data, sha)
        await self.post_next(chan)

    async def post_next(self, channel):
        data, sha = load_data()
        if not data['bracket']:
            return
        
        a = data['bracket'].pop(0)
        b = data['bracket'].pop(0)
        
        round_n = get_round_name(len(data['bracket']) + 2)
        match_n = len(data['finished_matches']) + 1
        
        view = MatchView(a, b, round_n, match_n)
        await channel.send(f"@everyone ⚔️ **{round_n} - Match {match_n} is now LIVE!**")
        msg = await channel.send(embed=view.create_embed(0), view=view)
        
        data['current_match'] = {
            "item_a": a, 
            "item_b": b, 
            "message_id": msg.id, 
            "channel_id": channel.id, 
            "votes": {}
        }
        data['status'] = "MATCH_ACTIVE"
        save_data(data, sha)

bot = WC_Bot()

# =========================================================
# SLASH COMMANDS
# =========================================================

@bot.tree.command(name="additem")
async def additem(i: discord.Interaction, name: str, description: str, image: discord.Attachment):
    if not any(r.id in ALLOWED_ROLE_IDS for r in i.user.roles): 
        return
        
    data, sha = load_data()
    if len(data['items']) >= 32:
        return await i.response.send_message("❌ **Bracket Full!** You cannot add more than 32 items.", ephemeral=True)
    
    await i.response.defer()
    storage_chan = bot.get_channel(STORAGE_CHANNEL_ID)
    stored_msg = await storage_chan.send(content=f"Upload: {name} by {i.user.name}", file=await image.to_file())
    
    data['items'].append({
        "name": name[:75], 
        "desc": description, 
        "image": stored_msg.attachments[0].url, 
        "user": i.user.name
    })
    save_data(data, sha)
    remaining = 32 - len(data['items'])
    await i.followup.send(f"✅ **{i.user.name}** has added **{name}** to the entry pool! ({len(data['items'])}/32 - {remaining} left)")

@bot.tree.command(name="edititem")
async def edititem(i: discord.Interaction, target_name: str, new_name: str = None, new_desc: str = None, new_image: discord.Attachment = None):
    if not any(r.id in ALLOWED_ROLE_IDS for r in i.user.roles): 
        return
        
    await i.response.defer()
    data, sha = load_data()
    item = next((x for x in data['items'] if x['name'].lower() == target_name.lower()), None)
    
    if not item:
        return await i.followup.send(f"❌ Could not find an item named '{target_name}'.")
    
    changes = []
    if new_name:
        item['name'] = new_name[:75]
        changes.append(f"Name changed to **{new_name}**")
    if new_desc:
        item['desc'] = new_desc
        changes.append("Description updated")
    if new_image:
        storage_chan = bot.get_channel(STORAGE_CHANNEL_ID)
        stored_msg = await storage_chan.send(content=f"Edit: {item['name']}", file=await new_image.to_file())
        item['image'] = stored_msg.attachments[0].url
        changes.append("Image updated")
    
    if not changes:
        return await i.followup.send("⚠️ You didn't specify any changes!")
        
    save_data(data, sha)
    log = "\n".join([f"• {c}" for c in changes])
    await i.followup.send(f"🛠️ **{i.user.name}** has updated **{target_name}**:\n{log}")

@bot.tree.command(name="removeitem")
async def removeitem(i: discord.Interaction, name: str):
    if not any(r.id in ALLOWED_ROLE_IDS for r in i.user.roles): 
        return
        
    data, sha = load_data()
    original_count = len(data['items'])
    data['items'] = [x for x in data['items'] if x['name'].lower() != name.lower()]
    
    if len(data['items']) == original_count:
        return await i.response.send_message(f"❌ '{name}' not found.", ephemeral=True)
    
    save_data(data, sha)
    await i.response.send_message(f"🗑️ **{i.user.name}** removed **{name}** from the tournament. ({len(data['items'])}/32 items remaining)")

@bot.tree.command(name="startworldcup")
async def startworldcup(i: discord.Interaction):
    if not any(r.id in ALLOWED_ROLE_IDS for r in i.user.roles): 
        return
        
    data, sha = load_data()
    count = len(data['items'])
    
    if count != 32:
        return await i.response.send_message(f"❌ **Error:** You have **{count}/32** items. You must have exactly 32 items to start the tournament.", ephemeral=True)
    
    random.shuffle(data['items'])
    data['bracket'] = data['items']
    data['finished_matches'] = []
    data['winners_pool'] = []
    save_data(data, sha)
    
    await i.response.send_message(f"🏆 **THE {data['current_cat'].upper()} WORLD CUP HAS OFFICIALLY BEGUN!**")
    await bot.post_next(i.channel)

@bot.tree.command(name="endcup")
async def endcup(i: discord.Interaction):
    if not any(r.id in ALLOWED_ROLE_IDS for r in i.user.roles): 
        return
        
    data, sha = load_data()
    
    if data.get("final_winner"):
        w = data["final_winner"]
        embed = discord.Embed(
            title="🎊 TOURNAMENT CHAMPION 🎊", 
            description=f"# 👑 {w['name'].upper()} 👑\nWinner of the **{data['current_cat']}** Cup!\nSubmitted by: **{w['user']}**", 
            color=0xf1c40f
        )
        embed.set_image(url=w['image'])
        await i.channel.send("@everyone 🏆 **WE HAVE A WINNER!**", embed=embed)
        
        data.setdefault('leaderboard', []).append({
            "item": w['name'], 
            "cat": data['current_cat'], 
            "user": w['user']
        })
        
        # Reset current cup but keep leaderboard
        data.update({
            "status": "IDLE", 
            "items": [], 
            "suggestions": [], 
            "bracket": [], 
            "winners_pool": [], 
            "finished_matches": [], 
            "current_match": None, 
            "current_cat": None, 
            "final_winner": None
        })
        save_data(data, sha)
        await i.response.send_message("Tournament finalized and archived.", ephemeral=True)
    else:
        await i.response.send_message(
            "⚠️ **Warning:** The Grand Final has not been reached. Do you want to force-end the cup early?", 
            view=EndConfirmView(bot), 
            ephemeral=True
        )

@bot.tree.command(name="cuphistory")
async def cuphistory(i: discord.Interaction):
    await i.response.defer()
    data, _ = load_data()
    history_list = data.get('leaderboard', [])
    
    if not history_list:
        return await i.followup.send("📜 The Hall of Fame is currently empty in the database.")
    
    view = HistoryView(history_list)
    await i.followup.send(embed=view.create_embed(), view=view)

@bot.tree.command(name="opensuggestions")
async def opensuggestions(i: discord.Interaction):
    if not any(r.id in ALLOWED_ROLE_IDS for r in i.user.roles): return
    await i.response.send_message("@everyone 💡 **Theme suggestions are now OPEN!** Use `/suggestcategory` to submit yours!")

@bot.tree.command(name="suggestcategory")
async def suggestcategory(i: discord.Interaction, name: str):
    data, sha = load_data()
    data.setdefault('suggestions', []).append({"name": name, "user": i.user.name})
    save_data(data, sha)
    await i.response.send_message(f"💡 Suggestion logged: **{name}**", ephemeral=True)

@bot.tree.command(name="choosecategory")
async def choosecategory(i: discord.Interaction):
    if not any(r.id in ALLOWED_ROLE_IDS for r in i.user.roles): return
    data, sha = load_data()
    if not data['suggestions']:
        return await i.response.send_message("No suggestions available to pick from.")
    
    await i.response.send_message("🎰 **Rerolling the theme slot machine...**")
    await asyncio.sleep(2.5)
    
    pick = random.choice(data['suggestions'])
    data['current_cat'] = pick['name']
    data['suggestions'] = []
    save_data(data, sha)
    await i.channel.send(f"@everyone 🎉 The official theme is: **{pick['name'].upper()}**! Start submitting 32 entries with `/additem`!")

@bot.tree.command(name="nextmatch")
async def nextmatch(i: discord.Interaction):
    if not any(r.id in ALLOWED_ROLE_IDS for r in i.user.roles): return
    data, sha = load_data()
    if not data.get("current_match"):
        return await i.response.send_message("There is no active match to resolve.")
    
    await i.response.send_message("Closing the polls and calculating results...", ephemeral=True)
    await bot.resolve_match(data, sha)

@bot.tree.command(name="scoreboard")
async def scoreboard(i: discord.Interaction):
    data, _ = load_data()
    embed = discord.Embed(title="📊 Tournament Live Scoreboard", color=0x3498db)
    
    results = ""
    for m in data.get('finished_matches', [])[-5:]:
        results += f"✅ {m['name']} (Winner: {m['winner']})\n"
        
    embed.add_field(name="Recent Results", value=results or "No matches finished yet.", inline=False)
    
    curr = data.get('current_match')
    if curr:
        embed.add_field(name="Current Match", value=f"🔥 **{curr['item_a']['name']}** vs **{curr['item_b']['name']}**", inline=False)
    
    await i.response.send_message(embed=embed)

@bot.tree.command(name="listitems")
async def listitems(i: discord.Interaction):
    data, _ = load_data()
    view = ItemGallery(data['items'])
    await i.response.send_message(embed=view.create_content(), view=view)

@bot.tree.command(name="resetcup")
async def resetcup(i: discord.Interaction):
    if not any(r.id in ALLOWED_ROLE_IDS for r in i.user.roles): return
    await i.response.send_message("⚠️ **DANGER:** This will wipe all current tournament progress. Are you sure?", view=ResetConfirmView(), ephemeral=True)

@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print("------")

if __name__ == "__main__":
    keep_alive()
    bot.run(TOKEN)
