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
# KEEP ALIVE SERVER (Render 24/7)
# =========================================================
app = Flask('')

@app.route('/')
def home():
    return "The Landing Strip World Cup Bot is Online and Fully Operational!"

def run_flask():
    # Render uses the PORT environment variable
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 8080)))

def keep_alive():
    t = Thread(target=run_flask)
    t.daemon = True
    t.start()

# =========================================================
# CONFIGURATION & ENVIRONMENT
# =========================================================
TOKEN = os.getenv("WC_TOKEN")
GITHUB_REPO = os.getenv("GITHUB_REPO")
GITHUB_FILE_PATH = os.getenv("TOURNAMENT_JSON_PATH")
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
HEADERS = {"Authorization": f"token {GITHUB_TOKEN}"} if GITHUB_TOKEN else {}

# Your Specific Admin Role IDs
ALLOWED_ROLE_IDS = [
    1413545658006110401, 
    1404098545006546954, 
    1420817462290681936, 
    1404105470204969000, 
    1404104881098195015
]

# Hidden channel where images are re-uploaded for permanent hosting
STORAGE_CHANNEL_ID = 1461047591528562801

# =========================================================
# DATA PERSISTENCE (GITHUB SYNC LOGIC)
# =========================================================
def load_data():
    try:
        # Cache buster to ensure we get the absolute latest JSON from GitHub
        cb = random.randint(1, 999999)
        url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_FILE_PATH}?cb={cb}"
        r = requests.get(url, headers=HEADERS, params={"ref": GITHUB_BRANCH}, timeout=20)
        
        if r.status_code == 200:
            content = r.json()
            raw_data = base64.b64decode(content["content"]).decode()
            return json.loads(raw_data), content.get("sha")
    except Exception as e:
        print(f"CRITICAL LOAD ERROR: {e}")
        
    # Default State if GitHub fails
    return {
        "status": "IDLE",
        "title": "The Landing Strip World Cup",
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
        "message": f"Tournament Update: {datetime.datetime.now()}",
        "content": encoded,
        "branch": GITHUB_BRANCH,
        "sha": sha
    }
    
    requests.put(url, headers=HEADERS, data=json.dumps(payload), timeout=20)

def is_admin(user):
    return any(role.id in ALLOWED_ROLE_IDS for role in user.roles)

def get_round_name(count):
    if count > 16: return "Round of 32"
    if count > 8: return "Round of 16"
    if count > 4: return "Quarter-Finals"
    if count > 2: return "Semi-Finals"
    return "Grand Final"

# =========================================================
# UI SYSTEM (BUTTONS & EMBEDS)
# =========================================================

class ResetConfirmView(ui.View):
    def __init__(self):
        super().__init__(timeout=60)

    @ui.button(label="Confirm FULL Reset", style=discord.ButtonStyle.danger)
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
        await i.response.edit_message(content="🧨 **Tournament Data Cleared.** (History Preserved)", view=None)

class HistoryView(ui.View):
    def __init__(self, history_data):
        super().__init__(timeout=None)
        self.data = history_data
        self.page = 0

    def create_embed(self):
        start = self.page * 5
        chunk = self.data[start:start+5]
        
        # This is your requested "Tree" layout
        desc = ""
        for idx, entry in enumerate(chunk):
            desc += f"{start+idx+1}. 🏆 **{entry['cat'].upper()}**\n"
            desc += f"┕ Winner: **{entry['item']}**\n"
            desc += f"┕ Submitter: {entry['user']}\n\n"
            
        embed = discord.Embed(
            title="🎖️ Hall of Fame History", 
            description=desc or "The archives are currently empty.", 
            color=0xf1c40f
        )
        
        total_pages = (len(self.data) - 1) // 5 + 1 if self.data else 1
        embed.set_footer(text=f"Page {self.page+1} of {total_pages}")
        return embed

    @ui.button(label="⬅️ Previous", style=discord.ButtonStyle.gray)
    async def prev(self, i, b):
        self.page = max(0, self.page - 1)
        await i.response.edit_message(embed=self.create_embed())

    @ui.button(label="Next ➡️", style=discord.ButtonStyle.gray)
    async def next(self, i, b):
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
            return discord.Embed(title="Empty", description="No items have been submitted yet.")
        
        if self.mode == "GALLERY":
            item = self.items[self.index]
            embed = discord.Embed(
                title=f"Entry #{self.index+1}: {item['name']}", 
                description=item.get('desc', 'No description.'), 
                color=0x3498db
            )
            embed.set_image(url=item['image'])
            embed.set_footer(text=f"Submitter: {item.get('user', 'Unknown')}")
            return embed
        
        # List View
        txt = ""
        for idx, x in enumerate(self.items):
            txt += f"{idx+1}. **{x['name']}** (By: {x['user']})\n"
        
        return discord.Embed(title="📋 All Tournament Entries", description=txt or "None", color=0x3498db)

    @ui.button(label="⬅️", style=discord.ButtonStyle.gray)
    async def prev(self, i, b):
        data, _ = load_data()
        self.items = data.get('items', [])
        self.index = (self.index - 1) % len(self.items)
        await i.response.edit_message(embed=self.create_content())

    @ui.button(label="➡️", style=discord.ButtonStyle.gray)
    async def next(self, i, b):
        data, _ = load_data()
        self.items = data.get('items', [])
        self.index = (self.index + 1) % len(self.items)
        await i.response.edit_message(embed=self.create_content())

    @ui.button(label="Toggle View (List/Gallery)", style=discord.ButtonStyle.blurple)
    async def toggle(self, i, b):
        data, _ = load_data()
        self.items = data.get('items', [])
        self.mode = "LIST" if self.mode == "GALLERY" else "GALLERY"
        await i.response.edit_message(embed=self.create_content())

class MatchView(ui.View):
    def __init__(self, item_a=None, item_b=None, round_name=None, match_num=None):
        # timeout must be None for persistence
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
            description=f"**{self.item_a['name']}** vs **{self.item_b['name']}**\n\n**Currently Viewing:** {item['name']}\n{item.get('desc', '')}", 
            color=0x3498db
        )
        embed.set_image(url=item['image'])
        embed.set_footer(text=f"Check both entries before voting! (Page {page+1}/2)")
        return embed

    # Added custom_id to fix the ValueError
    @ui.button(label="⬅️ View Entry A", style=discord.ButtonStyle.gray, custom_id="view_a_btn")
    async def prev_page(self, i: discord.Interaction, b: ui.Button):
        data, _ = load_data()
        m = data['current_match']
        self.item_a, self.item_b = m['item_a'], m['item_b']
        await i.response.edit_message(embed=self.create_embed(0))

    # Added custom_id to fix the ValueError
    @ui.button(label="View Entry B ➡️", style=discord.ButtonStyle.gray, custom_id="view_b_btn")
    async def next_page(self, i: discord.Interaction, b: ui.Button):
        data, _ = load_data()
        m = data['current_match']
        self.item_a, self.item_b = m['item_a'], m['item_b']
        await i.response.edit_message(embed=self.create_embed(1))

    @ui.button(style=discord.ButtonStyle.danger, custom_id="v_a_master", row=1)
    async def vote_a(self, i: discord.Interaction, b: ui.Button):
        data, sha = load_data()
        match = data.get("current_match")
        if not match or str(i.user.id) in match.get("votes", {}):
            return await i.response.send_message("❌ You have already voted in this match!", ephemeral=True)
        
        match["votes"][str(i.user.id)] = "A"
        save_data(data, sha)
        await i.response.send_message(f"✅ Your vote for **{match['item_a']['name']}** has been recorded.", ephemeral=True)

    @ui.button(style=discord.ButtonStyle.primary, custom_id="v_b_master", row=1)
    async def vote_b(self, i: discord.Interaction, b: ui.Button):
        data, sha = load_data()
        match = data.get("current_match")
        if not match or str(i.user.id) in match.get("votes", {}):
            return await i.response.send_message("❌ You have already voted in this match!", ephemeral=True)
        
        match["votes"][str(i.user.id)] = "B"
        save_data(data, sha)
        await i.response.send_message(f"✅ Your vote for **{match['item_b']['name']}** has been recorded.", ephemeral=True)


# =========================================================
# BOT CORE CLASS
# =========================================================
class WC_Bot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.all())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        # Keeps buttons working after bot restarts
        self.add_view(MatchView())
        self.add_view(HistoryView([]))
        self.add_view(ItemGallery([]))

    async def resolve_match(self, data, sha):
        match = data['current_match']
        if not match: return
        
        chan = self.get_channel(match['channel_id'])
        votes = list(match.get("votes", {}).values())
        v1, v2 = votes.count("A"), votes.count("B")
        
        if v1 > v2:
            winner = match['item_a']
        elif v2 > v1:
            winner = match['item_b']
        else:
            winner = random.choice([match['item_a'], match['item_b']])
            await chan.send("⚖️ **It's a tie!** A random winner has been selected.")

        data.setdefault('finished_matches', []).append({
            "name": f"{match['item_a']['name']} vs {match['item_b']['name']}", 
            "winner": winner['name'], 
            "score": f"{v1}-{v2}"
        })
        
        data.setdefault('winners_pool', []).append(winner)
        data['current_match'] = None
        
        await chan.send(embed=discord.Embed(
            title="Match Concluded", 
            description=f"🏆 **{winner['name']}** advances to the next round! ({v1} to {v2})", 
            color=0x2ecc71
        ).set_image(url=winner['image']))

        # Handle Round Advancement
        if not data['bracket'] and len(data['winners_pool']) > 1:
            data['bracket'] = data['winners_pool']
            data['winners_pool'] = []
            await chan.send(f"🛡️ **Round Complete!** Moving to the **{get_round_name(len(data['bracket']))}**.")
        elif not data['bracket'] and len(data['winners_pool']) == 1:
            data['final_winner'] = winner
            data['status'] = "FINISHED"
            save_data(data, sha)
            await chan.send("🏁 **The Grand Final is over!** Admin: use `/endcup` to crown the champion!")
            return

        save_data(data, sha)
        await self.post_next(chan)

    async def post_next(self, channel):
        data, sha = load_data()
        if not data['bracket']: return
        
        # Pull next two competitors
        a = data['bracket'].pop(0)
        b = data['bracket'].pop(0)
        
        round_name = get_round_name(len(data['bracket']) + 2)
        match_num = len(data['finished_matches']) + 1
        
        view = MatchView(a, b, round_name, match_num)
        await channel.send(f"@everyone ⚔️ **{round_name} - Match {match_num}** is now live!")
        msg = await channel.send(embed=view.create_embed(0), view=view)
        
        data['current_match'] = {
            "item_a": a, "item_b": b, "message_id": msg.id, 
            "channel_id": channel.id, "votes": {}
        }
        data['status'] = "MATCH_ACTIVE"
        save_data(data, sha)

bot = WC_Bot()

# =========================================================
# SLASH COMMANDS - ADMINS
# =========================================================

@bot.tree.command(name="opensuggestions", description="Phase 1: Open theme suggestions")
async def opensuggestions(i: discord.Interaction):
    if not is_admin(i.user): 
        return await i.response.send_message("❌ Admin only.", ephemeral=True)
    data, sha = load_data()
    data['status'] = "SUGGESTIONS_OPEN"
    save_data(data, sha)
    await i.response.send_message("@everyone 💡 **The World Cup is starting!** Theme suggestions are now OPEN! Use `/suggestcategory`!")

@bot.tree.command(name="choosecategory", description="Phase 2: Pick a random theme from suggestions")
async def choosecategory(i: discord.Interaction):
    if not is_admin(i.user): return
    data, sha = load_data()
    if not data['suggestions']: 
        return await i.response.send_message("❌ No suggestions found.", ephemeral=True)
    
    await i.response.send_message("🎰 **Spinning the wheel of themes...**")
    await asyncio.sleep(2)
    
    pick = random.choice(data['suggestions'])
    data['current_cat'] = pick['name']
    data['suggestions'] = [] # Clear for next time
    data['status'] = "ADDING_ITEMS"
    save_data(data, sha)
    
    await i.channel.send(f"@everyone 🎉 The theme is: **{pick['name'].upper()}**!\n(Suggested by {pick['user']})\n\nSubmit your entries now with `/additem`!")

@bot.tree.command(name="removeitem", description="Admin: Remove an item by its number")
async def removeitem(i: discord.Interaction, index: int):
    if not is_admin(i.user): return
    data, sha = load_data()
    if 1 <= index <= len(data['items']):
        removed = data['items'].pop(index-1)
        save_data(data, sha)
        await i.response.send_message(f"🗑️ Removed **{removed['name']}** from the entry list.")
    else:
        await i.response.send_message("❌ Index out of range.", ephemeral=True)

@bot.tree.command(name="edititem", description="Admin: Change name or description of an entry")
async def edititem(i: discord.Interaction, index: int, new_name: str = None, new_desc: str = None):
    if not is_admin(i.user): return
    data, sha = load_data()
    if 1 <= index <= len(data['items']):
        item = data['items'][index-1]
        if new_name: item['name'] = new_name[:75]
        if new_desc: item['desc'] = new_desc
        save_data(data, sha)
        await i.response.send_message(f"📝 Updated Entry #{index}.")
    else:
        await i.response.send_message("❌ Index out of range.", ephemeral=True)

@bot.tree.command(name="startworldcup", description="Phase 3: Close submissions and start Round 1")
async def startworldcup(i: discord.Interaction):
    if not is_admin(i.user): return
    data, sha = load_data()
    if len(data['items']) != 32:
        return await i.response.send_message(f"❌ You need exactly 32 items. (Current: {len(data['items'])})", ephemeral=True)
    
    random.shuffle(data['items'])
    data['bracket'] = data['items']
    data['finished_matches'] = []
    data['winners_pool'] = []
    data['status'] = "MATCH_ACTIVE"
    save_data(data, sha)
    
    await i.response.send_message(f"🏆 **THE {data['current_cat'].upper()} WORLD CUP HAS OFFICIALLY BEGUN!**")
    await bot.post_next(i.channel)

@bot.tree.command(name="nextmatch", description="Admin: End current poll and start next match")
async def nextmatch(i: discord.Interaction):
    if not is_admin(i.user): return
    data, sha = load_data()
    if not data.get("current_match"):
        return await i.response.send_message("❌ There is no match currently active.", ephemeral=True)
    
    await i.response.send_message("⌛ Resolving match...", ephemeral=True)
    await bot.resolve_match(data, sha)

@bot.tree.command(name="endcup", description="Phase 4: Crown the winner and archive to Hall of Fame")
async def endcup(i: discord.Interaction):
    if not is_admin(i.user): return
    data, sha = load_data()
    
    if data.get("final_winner"):
        w = data["final_winner"]
        embed = discord.Embed(
            title="🎊 CHAMPION CROWNED 🎊", 
            description=f"# 👑 {w['name'].upper()} 👑\n\nWinner of the **{data['current_cat']}** World Cup!\n**Submitted by:** {w['user']}", 
            color=0xf1c40f
        )
        embed.set_image(url=w['image'])
        await i.channel.send("@everyone 🏆 **THE TOURNAMENT HAS CONCLUDED!**", embed=embed)
        
        # Add to Hall of Fame
        data.setdefault('leaderboard', []).append({
            "item": w['name'], 
            "cat": data['current_cat'], 
            "user": w['user']
        })
        
        # Reset current game state for next time
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
        await i.response.send_message("✅ Tournament archived to Hall of Fame.", ephemeral=True)
    else:
        await i.response.send_message("⚠️ The tournament isn't over yet!", ephemeral=True)

@bot.tree.command(name="resetcup", description="Admin: Complete emergency wipe of current game")
async def resetcup(i: discord.Interaction):
    if not is_admin(i.user): return
    await i.response.send_message("⚠️ **Are you absolutely sure you want to wipe the current tournament?**", view=ResetConfirmView(), ephemeral=True)

# =========================================================
# SLASH COMMANDS - USERS
# =========================================================

@bot.tree.command(name="suggestcategory", description="Submit a theme for the next Cup")
async def suggestcategory(i: discord.Interaction, name: str):
    data, sha = load_data()
    user_mention = f"<@{i.user.id}>"
    
    if data['status'] != "SUGGESTIONS_OPEN":
        return await i.response.send_message("❌ Suggestions are currently closed.", ephemeral=True)
    
    if not is_admin(i.user) and any(s['user'] == user_mention for s in data['suggestions']):
        return await i.response.send_message("❌ You've already submitted a suggestion!", ephemeral=True)
    
    data.setdefault('suggestions', []).append({"name": name[:100], "user": user_mention})
    save_data(data, sha)
    await i.response.send_message(f"💡 Suggestion logged: **{name}**", ephemeral=True)

@bot.tree.command(name="additem", description="Submit your entry (Name, Desc, Image)")
async def additem(i: discord.Interaction, name: str, description: str, image: discord.Attachment):
    data, sha = load_data()
    user_mention = f"<@{i.user.id}>"
    
    if data['status'] != "ADDING_ITEMS":
        return await i.response.send_message("❌ Not currently accepting entries.", ephemeral=True)
    
    if len(data['items']) >= 32:
        return await i.response.send_message("❌ The bracket is full! (32/32)", ephemeral=True)
    
    if not is_admin(i.user) and any(item['user'] == user_mention for item in data['items']):
        return await i.response.send_message("❌ You have already submitted an entry for this Cup!", ephemeral=True)
    
    await i.response.defer(ephemeral=True)
    
    # Re-upload image to storage channel
    storage = bot.get_channel(STORAGE_CHANNEL_ID)
    try:
        stored = await storage.send(content=f"Tournament Upload: {name} (by {user_mention})", file=await image.to_file())
        image_url = stored.attachments[0].url
    except:
        return await i.followup.send("❌ Image upload failed. Try again.", ephemeral=True)

    # 75 CHARACTER LIMIT ENFORCED HERE
    data['items'].append({
        "name": name[:75], 
        "desc": description, 
        "image": image_url, 
        "user": user_mention
    })
    
    save_data(data, sha)
    await i.followup.send(f"✅ **{name[:75]}** added! ({len(data['items'])}/32 slots filled)")

@bot.tree.command(name="listitems", description="View all entries in the current Cup")
async def listitems(i: discord.Interaction):
    data, _ = load_data()
    view = ItemGallery(data['items'])
    await i.response.send_message(embed=view.create_content(), view=view)

@bot.tree.command(name="scoreboard", description="View match results for the current tournament")
async def scoreboard(i: discord.Interaction):
    data, _ = load_data()
    if not data['finished_matches']:
        return await i.response.send_message("No matches have been played yet.", ephemeral=True)
    
    txt = ""
    for m in data['finished_matches']:
        txt += f"🔹 {m['name']}: **{m['winner']}** ({m['score']})\n"
        
    embed = discord.Embed(title=f"📊 {data['current_cat'].upper()} Scoreboard", description=txt, color=0x3498db)
    await i.response.send_message(embed=embed)

@bot.tree.command(name="cuphistory", description="View the Hall of Fame archive")
async def cuphistory(i: discord.Interaction):
    await i.response.defer()
    data, _ = load_data()
    history_list = data.get('leaderboard', [])
    
    if not history_list:
        return await i.followup.send("No Hall of Fame history found yet.")
    
    view = HistoryView(history_list)
    await i.followup.send(embed=view.create_embed(), view=view)

# =========================================================
# ON READY
# =========================================================
@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"✅ Successfully logged in as {bot.user}")
    print(f"✅ Commands Synced.")

if __name__ == "__main__":
    keep_alive()
    bot.run(TOKEN)
