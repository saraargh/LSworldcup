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
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

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

# Admin Role IDs
ALLOWED_ROLE_IDS = [
    1413545658006110401, 
    1404098545006546954, 
    1420817462290681936, 
    1404105470204969000, 
    1404104881098195015
]

# Storage Channel for re-hosting images
STORAGE_CHANNEL_ID = 1461047591528562801

# =========================================================
# DATA PERSISTENCE (GITHUB SYNC LOGIC)
# =========================================================
def load_data():
    try:
        # Prevent GitHub caching with a random number
        cache_buster = random.randint(1, 999999)
        url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_FILE_PATH}"
        params = {"ref": GITHUB_BRANCH, "cb": cache_buster}
        
        response = requests.get(url, headers=HEADERS, params=params, timeout=20)
        
        if response.status_code == 200:
            content = response.json()
            # Decode Base64 from GitHub
            decoded_bytes = base64.b64decode(content["content"])
            decoded_str = decoded_bytes.decode()
            return json.loads(decoded_str), content.get("sha")
            
    except Exception as e:
        print(f"CRITICAL LOAD ERROR: {e}")
        
    # Return default template if load fails
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
    # If SHA isn't provided, fetch it so we can overwrite
    if not sha:
        _, sha = load_data()
    
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_FILE_PATH}"
    
    # Encode JSON to Base64
    json_str = json.dumps(data, indent=4)
    encoded_bytes = base64.b64encode(json_str.encode())
    encoded_str = encoded_bytes.decode()
    
    payload = {
        "message": f"Sync: {datetime.datetime.now()}",
        "content": encoded_str,
        "branch": GITHUB_BRANCH,
        "sha": sha
    }
    
    requests.put(url, headers=HEADERS, data=json.dumps(payload), timeout=20)

def is_admin(user):
    # Check if the user has any of the 5 allowed roles
    user_role_ids = [role.id for role in user.roles]
    for role_id in ALLOWED_ROLE_IDS:
        if role_id in user_role_ids:
            return True
    return False

def get_round_name(data):
    # We now pass 'data' so we can count EVERYONE still in the tournament
    bracket_count = len(data.get('bracket', []))
    pool_count = len(data.get('winners_pool', []))
    
    # Check if a match is currently happening (adds 2 more people)
    current_match_count = 2 if data.get('current_match') else 0
    
    total_alive = bracket_count + pool_count + current_match_count

    if total_alive > 16:
        return "Round of 32"
    elif total_alive > 8:
        return "Round of 16"
    elif total_alive > 4:
        return "Quarter-Finals"
    elif total_alive > 2:
        return "Semi-Finals"
    elif total_alive == 2:
        return "Grand Final"
    else:
        return "Tournament Complete"


# =========================================================
# UI SYSTEM (PERSISTENT BUTTON LOGIC)
# =========================================================

class ResetConfirmView(ui.View):
    def __init__(self):
        super().__init__(timeout=60)

    @ui.button(label="Confirm FULL Reset", style=discord.ButtonStyle.danger, custom_id="reset_confirm_btn")
    async def confirm(self, interaction: discord.Interaction, button: ui.Button):
        data, sha = load_data()
        data["status"] = "IDLE"
        data["items"] = []
        data["suggestions"] = []
        data["bracket"] = []
        data["winners_pool"] = []
        data["finished_matches"] = []
        data["current_match"] = None
        data["current_cat"] = None
        data["final_winner"] = None
        
        save_data(data, sha)
        await interaction.response.edit_message(content="🧨 **Tournament Reset Successful.**", view=None)

class HistoryView(ui.View):
    def __init__(self, history_data=None):
        super().__init__(timeout=None)
        self.data = history_data or []
        self.page = 0

    def create_embed(self):
        start = self.page * 5
        chunk = self.data[start : start + 5]
        
        description_text = ""
        for idx, entry in enumerate(chunk):
            count = start + idx + 1
            description_text += f"{count}. 🏆 **{entry['cat'].upper()}**\n"
            description_text += f"┕ Winner: **{entry['item']}**\n"
            description_text += f"┕ Submitter: {entry['user']}\n\n"
        
        if not description_text:
            description_text = "The Hall of Fame is currently empty."
            
        embed = discord.Embed(
            title="🎖️ Hall of Fame History", 
            description=description_text, 
            color=0xf1c40f
        )
        
        total_pages = (len(self.data) - 1) // 5 + 1 if self.data else 1
        embed.set_footer(text=f"Page {self.page+1} of {total_pages}")
        return embed

    @ui.button(label="⬅️ Previous", style=discord.ButtonStyle.gray, custom_id="hist_prev")
    async def prev(self, interaction: discord.Interaction, button: ui.Button):
        data, _ = load_data()
        self.data = data.get('leaderboard', [])
        self.page = max(0, self.page - 1)
        await interaction.response.edit_message(embed=self.create_embed())

    @ui.button(label="Next ➡️", style=discord.ButtonStyle.gray, custom_id="hist_next")
    async def next(self, interaction: discord.Interaction, button: ui.Button):
        data, _ = load_data()
        self.data = data.get('leaderboard', [])
        if (self.page + 1) * 5 < len(self.data):
            self.page += 1
        await interaction.response.edit_message(embed=self.create_embed())

class ItemGallery(ui.View):
    def __init__(self, items=None):
        super().__init__(timeout=None)
        self.items = items or []
        self.index = 0
        self.mode = "GALLERY"

    def create_content(self):
        if not self.items:
            return discord.Embed(title="Empty", description="No items submitted yet.")
        
        if self.mode == "GALLERY":
            item = self.items[self.index]
            embed = discord.Embed(
                title=f"Entry #{self.index+1}: {item['name']}", 
                description=item.get('desc', 'No description.'), 
                color=0x3498db
            )
            embed.set_image(url=item['image'])
            # Remove the mention from the footer
            embed.set_footer(text=f"Entry {self.index + 1} of {len(self.items)}")

            # Add the mention to the bottom of the description instead
            embed.description += f"\n\n**Submitter:** {item.get('user', 'Unknown')}"

            return embed
        
        # List View mode
        list_text = ""
        for idx, x in enumerate(self.items):
            list_text += f"{idx+1}. **{x['name']}** (By: {x['user']})\n"
        
        return discord.Embed(
            title="📋 All Tournament Entries", 
            description=list_text or "No entries found.", 
            color=0x3498db
        )

    @ui.button(label="⬅️", style=discord.ButtonStyle.gray, custom_id="gal_prev")
    async def prev(self, interaction: discord.Interaction, button: ui.Button):
        data, _ = load_data()
        self.items = data.get('items', [])
        if self.items:
            self.index = (self.index - 1) % len(self.items)
        await interaction.response.edit_message(embed=self.create_content())

    @ui.button(label="➡️", style=discord.ButtonStyle.gray, custom_id="gal_next")
    async def next(self, interaction: discord.Interaction, button: ui.Button):
        data, _ = load_data()
        self.items = data.get('items', [])
        if self.items:
            self.index = (self.index + 1) % len(self.items)
        await interaction.response.edit_message(embed=self.create_content())

    @ui.button(label="Toggle View (List/Gallery)", style=discord.ButtonStyle.blurple, custom_id="gal_toggle")
    async def toggle(self, interaction: discord.Interaction, button: ui.Button):
        data, _ = load_data()
        self.items = data.get('items', [])
        if self.mode == "GALLERY":
            self.mode = "LIST"
        else:
            self.mode = "GALLERY"
        await interaction.response.edit_message(embed=self.create_content())


class MatchView(discord.ui.View):
    def __init__(self, item_a=None, item_b=None, current_item="A"):
        super().__init__(timeout=None) # Persistent: Buttons never timeout
        self.item_a = item_a
        self.item_b = item_b
        self.current_item = current_item

        # Persistent button labels
        if item_a:
            self.vote_a_button.label = f"Vote for {item_a['name']}"
        if item_b:
            self.vote_b_button.label = f"Vote for {item_b['name']}"

    def create_embed(self, data):
        # Reboot Recovery logic
        if (self.item_a is None or self.item_b is None) and data.get('current_match'):
            match = data['current_match']
            self.item_a, self.item_b = match['item_a'], match['item_b']
            self.vote_a_button.label = f"Vote for {self.item_a['name']}"
            self.vote_b_button.label = f"Vote for {self.item_b['name']}"

        votes = list(data['current_match'].get('votes', {}).values())
        cA, cB = votes.count("A"), votes.count("B")
        viewing = self.item_a if self.current_item == "A" else self.item_b
        
        embed = discord.Embed(title=f"⚔️ {self.item_a['name']} vs {self.item_b['name']}", color=0xff4757)
        
        # 1. Currently Viewing (Top)
        embed.add_field(name="\u200b", value=f"**Currently Viewing:** {viewing['name']}", inline=False)

        # 2. Description Section (Left exactly as you have it)
        desc_text = viewing.get('desc', 'No description provided.')
        embed.add_field(
            name="\u200b", 
            value=f"**Description:** {desc_text}", 
            inline=False
        )
        
        # 3. Submitter Section (Left exactly as you have it)
        submitter = viewing.get('user', 'Unknown')
        embed.add_field(
            name="\u200b",
            value=f"submitted by: {submitter}",
            inline=False
        )
        
        # 4. Image (Main center content)
        if viewing.get('image'):
            embed.set_image(url=viewing['image'])
            
        # 5. Standings (Moved to Footer to be under the image)
        embed.set_footer(text=f"🗳️ Current Standings: {self.item_a['name']} ({cA}) — {self.item_b['name']} ({cB})\nThe Landing Strip World Cup System")
        
        return embed

    @discord.ui.button(label="⬅️", style=discord.ButtonStyle.secondary, custom_id="view_a")
    async def view_a(self, interaction: discord.Interaction, button: discord.ui.Button):
        data, _ = load_data()
        self.current_item = "A"
        await interaction.response.edit_message(embed=self.create_embed(data), view=self)

    @discord.ui.button(label="➡️", style=discord.ButtonStyle.secondary, custom_id="view_b")
    async def view_b(self, interaction: discord.Interaction, button: discord.ui.Button):
        data, _ = load_data()
        self.current_item = "B"
        await interaction.response.edit_message(embed=self.create_embed(data), view=self)

    @discord.ui.button(label="Vote A", style=discord.ButtonStyle.danger, custom_id="persistent_v_a")
    async def vote_a_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.process_vote(interaction, "A")

    @discord.ui.button(label="Vote B", style=discord.ButtonStyle.primary, custom_id="persistent_v_b")
    async def vote_b_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.process_vote(interaction, "B")

    async def process_vote(self, interaction, choice):
        data, sha = load_data()
        match = data.get('current_match')
        
        # FIXED: This ensures variables aren't empty after a bot reboot
        if self.item_a is None or self.item_b is None:
            self.item_a = match['item_a']
            self.item_b = match['item_b']

        user_id = str(interaction.user.id)
        match.setdefault('votes', {})
        
        if match['votes'].get(user_id) == choice:
            return await interaction.response.send_message("⚠️ Already voted for this!", ephemeral=True)

        match['votes'][user_id] = choice
        save_data(data, sha)
        
        winner_name = self.item_a['name'] if choice == "A" else self.item_b['name']
        await interaction.response.send_message(f"✅ Voted for **{winner_name}**!", ephemeral=True)
        await interaction.message.edit(embed=self.create_embed(data))


class EndConfirmView(ui.View):
    def __init__(self, data, sha, is_early=False):
        super().__init__(timeout=60)
        self.data = data
        self.sha = sha
        self.is_early = is_early

    @ui.button(label="⚠️ CONFIRM: END TOURNAMENT NOW", style=discord.ButtonStyle.danger)
    async def confirm_end(self, interaction: discord.Interaction, button: ui.Button):
        winner = self.data.get("final_winner")
        
        # 1. Announce Winner
        if winner:
            embed = discord.Embed(
                title="🎊 CHAMPION CROWNED 🎊", 
                description=f"# 👑 {winner['name'].upper()} 👑\n\nWinner of the **{self.data['current_cat']}** World Cup!\n**Submitted by:** {winner['user']}", 
                color=0xf1c40f
            )
            embed.set_image(url=winner['image'])
            self.data.setdefault('leaderboard', []).append({
                "item": winner['name'], 
                "cat": self.data['current_cat'], 
                "user": winner['user']
            })
            await interaction.channel.send("@everyone 🏆 **THE WORLD CUP IS COMPLETE!**", embed=embed)
        else:
            await interaction.channel.send("🛑 **TOURNAMENT ENDED MANUALLY.**")
        
        # 2. Sweep/Unpin all bot messages in the channel
        try:
            pins = await interaction.channel.pins()
            for pin in pins:
                if pin.author.id == interaction.client.user.id:
                    await pin.unpin()
        except:
            pass

        # 3. Reset Data (FIXED: REMOVED "items": [] FROM WIPE)
        self.data.update({
            "status": "IDLE", 
            "suggestions": [], 
            "bracket": [], 
            "winners_pool": [], 
            "finished_matches": [],
            "current_match": None, 
            "current_cat": None, 
            "final_winner": None
        })
        
        save_data(self.data, self.sha)
        await interaction.response.edit_message(content="✅ **Tournament state cleared and pins removed. Items have been preserved.**", view=None)

class ScoreboardView(discord.ui.View):
    def __init__(self, matches, page=0):
        super().__init__(timeout=60)
        self.matches = matches
        self.page = page
        self.items_per_page = 5
        self.update_buttons()

    def update_buttons(self):
        if not self.matches:
            self.prev_button.disabled = True
            self.next_button.disabled = True
            return

        self.prev_button.disabled = (self.page == 0)
        max_page = (len(self.matches) - 1) // self.items_per_page
        self.next_button.disabled = (self.page >= max_page)

    def create_embed(self):
        embed = discord.Embed(title="📜 Match History", color=0xf1c40f)
        
        if not self.matches:
            embed.description = "No matches have finished yet!"
            return embed

        # Calculate slice for pagination
        start = self.page * self.items_per_page
        end = start + self.items_per_page
        current_batch = self.matches[start:end]
        
        for m in current_batch:
            # Main match details
            match_name = m.get('name', 'Unknown Match')
            winner = m.get('winner', 'N/A')
            score = m.get('score', '0-0')
            
            # Use a field for the match info
            embed.add_field(
                name=f"🔹 {match_name}",
                value=f"🏆 **Winner:** {winner} ({score})",
                inline=False
            )
            
            # Add an empty field to create a physical gap (prevents crowding)
            embed.add_field(name="\u200b", value="───────────────────", inline=False)
            
        total_pages = ((len(self.matches) - 1) // self.items_per_page) + 1
        embed.set_footer(text=f"Page {self.page + 1} of {total_pages} | The Landing Strip World Cup")
        return embed

    @discord.ui.button(label="⬅️ Newer", style=discord.ButtonStyle.secondary)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page -= 1
        self.update_buttons()
        await interaction.response.edit_message(embed=self.create_embed(), view=self)

    @discord.ui.button(label="Older ➡️", style=discord.ButtonStyle.secondary)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page += 1
        self.update_buttons()
        await interaction.response.edit_message(embed=self.create_embed(), view=self)


# =========================================================
# BOT CORE CLASS
# =========================================================



class WC_Bot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.all())
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        # Registering the view here fixes the "Interaction Failed" after reboots
        self.add_view(MatchView(None, None))
        await self.tree.sync()
        print(f"✅ Synced slash commands for {self.user}")

    async def on_ready(self):
        print(f"🚀 Logged in as {self.user}")

    # === TOURNAMENT LOGIC ===
    def calculate_stats(self, data, winner, runner_up):
        history = data.get('finished_matches', [])
        
        # 1. 2nd Place
        second = runner_up['name']
        second_user = runner_up.get('user', 'Unknown')

        # 2. 3rd Place (Loser of the semi-final/previous match)
        third = "Unknown"
        third_user = "Unknown"
        if len(history) >= 1:
            last_match = history[-1]
            third = last_match.get('loser', 'Unknown')
            third_user = last_match.get('loser_user', 'Unknown')

        # 3. Vote Statistics
        tally = {}
        for m in history:
            try:
                parts = m['score'].split('-')
                w_votes = int(parts[0])
                l_votes = int(parts[1])
                tally[m['winner']] = tally.get(m['winner'], 0) + w_votes
                if 'loser' in m:
                    tally[m['loser']] = tally.get(m['loser'], 0) + l_votes
            except: pass

        if tally:
            sorted_votes = sorted(tally.items(), key=lambda x: x[1], reverse=True)
            most_voted_item, most_votes = sorted_votes[0]
            least_voted_item, least_votes = sorted_votes[-1]
        else:
            most_voted_item, most_votes = "None", 0
            least_voted_item, least_votes = "None", 0

        return {
            "second": second, "second_user": second_user,
            "third": third, "third_user": third_user,
            "most_voted": most_voted_item, "most_votes": most_votes,
            "least_voted": least_voted_item, "least_votes": least_votes
        }

    async def resolve_match(self, data, sha):
        match = data.get('current_match')
        if not match: return

        channel = self.get_channel(match['channel_id']) or await self.fetch_channel(match['channel_id'])

        # Count Votes
        votes = list(match.get('votes', {}).values())
        cA, cB = votes.count("A"), votes.count("B")
        
        winner, loser = (match['item_a'], match['item_b']) if cA >= cB else (match['item_b'], match['item_a'])
        win_score, lose_score = (cA, cB) if cA >= cB else (cB, cA)

        # Update History
        data.setdefault('finished_matches', []).append({
            "name": f"{match['item_a']['name']} vs {match['item_b']['name']}",
            "winner": winner['name'],
            "winner_user": winner.get('user', 'Unknown'),
            "loser": loser['name'],
            "loser_user": loser.get('user', 'Unknown'),
            "score": f"{win_score}-{lose_score}"
        })
        data.setdefault('winners_pool', []).append(winner)
        
        old_msg_id = match['message_id']
        data['current_match'] = None 
        save_data(data, sha)

        # Unpin old match
        try:
            old_msg = await channel.fetch_message(old_msg_id)
            await old_msg.unpin()
        except: pass

        # === GRAND FINALE CHECK ===
        is_tournament_over = not data.get('bracket') and len(data.get('winners_pool', [])) == 1

        if is_tournament_over:
            stats = self.calculate_stats(data, winner, loser)
            
            final_embed = discord.Embed(title="👑 CHAMPION CROWNED 👑", color=0xf1c40f)
            # Matches the "submitted by: @user" style
            final_embed.description = f"# 🏆 {winner['name']}\n**Submitted by:** @{winner.get('user', 'Unknown')}"
            
            if winner.get('image'):
                final_embed.set_image(url=winner['image'])

            # Special Mentions in your preferred clean style
            mentions = (
                f"🥈 **Second Place:** {stats['second']} (@{stats['second_user']})\n"
                f"🥉 **Third Place:** {stats['third']} (@{stats['third_user']})\n\n"
                f"🔥 **Most Voted Overall:** {stats['most_voted']} ({stats['most_votes']} votes)\n"
                f"❄️ **Least Voted Overall:** {stats['least_voted']} ({stats['least_votes']} votes)"
            )
            final_embed.add_field(name="🏅 Special Mentions", value=mentions, inline=False)
            final_embed.set_footer(text="The Landing Strip World Cup System")
            
            await channel.send(embed=final_embed)
            
            data['status'] = "FINISHED"
            data['final_winner'] = winner
            _, latest_sha = load_data()
            save_data(data, latest_sha)
            return

        # === REGULAR ROUND WINNER ===
        win_embed = discord.Embed(title="🏆 MATCH CONCLUDED", color=0x2ecc71)
        win_embed.description = f"### {winner['name']} has DEFEATED {loser['name']}!"
        win_embed.add_field(name="Final Score", value=f"✅ **{win_score}** — ❌ **{lose_score}**", inline=False)
        win_embed.add_field(name="Advancing", value=f"⭐ {winner['name']}", inline=True)
        
        if winner.get('image'):
            win_embed.set_thumbnail(url=winner['image'])
        
        await channel.send(embed=win_embed)

        # Small delay before next match
        await asyncio.sleep(2)
        await self.post_next(channel)

    async def post_next(self, channel):
        # Fresh data load
        data, sha = load_data()

        # If bracket is empty, move winners_pool into bracket for next round
        if not data.get('bracket') or len(data['bracket']) < 2:
            if len(data.get('winners_pool', [])) >= 2:
                data['bracket'] = list(data.get('winners_pool', []))
                data['winners_pool'] = []
                save_data(data, sha)
                await channel.send("🛡️ **Round Complete!** Advancing winners...")
                # Re-load after saving bracket shift
                data, sha = load_data()
            else: 
                return # Not enough items to continue yet

        # Take next two from bracket
        comp_a = data['bracket'].pop(0)
        comp_b = data['bracket'].pop(0)

        # Initialize current_match
        data['current_match'] = {
            "item_a": comp_a, 
            "item_b": comp_b, 
            "votes": {}, 
            "channel_id": channel.id
        }
        data['status'] = "MATCH_ACTIVE"

        # Create MatchView (Persistence handled by custom_ids inside the class)
        view = MatchView(comp_a, comp_b, current_item="A") 
        embed = view.create_embed(data)
        
        msg = await channel.send(
            content="⚔️ @everyone **THE NEXT MATCH UP IS READY, CAST YOUR VOTES BELOW!!**", 
            embed=embed, 
            view=view
        )
        
        data['current_match']['message_id'] = msg.id
        save_data(data, sha)
        
        try: 
            await msg.pin()
        except: 
            pass




# --- CLASS ENDS HERE ---
bot = WC_Bot()
# =========================================================
# SLASH COMMANDS - ADMINS
# =========================================================

@bot.tree.command(name="opensuggestions", description="Phase 1: Open theme suggestions for users")
async def opensuggestions(interaction: discord.Interaction):
    if not is_admin(interaction.user): 
        return await interaction.response.send_message("❌ Admin only command.", ephemeral=True)
        
    data, sha = load_data()
    data['status'] = "SUGGESTIONS_OPEN"
    save_data(data, sha)
    await interaction.response.send_message("@everyone 💡 **The World Cup is starting!** Theme suggestions are now OPEN! Use `/suggestcategory`!")

@bot.tree.command(name="choosecategory", description="Phase 2: Pick a random theme from user suggestions")
async def choosecategory(interaction: discord.Interaction):
    if not is_admin(interaction.user):
        return await interaction.response.send_message("❌ Admin only.", ephemeral=True)
        
    # Safety Defer
    try:
        await interaction.response.defer(ephemeral=False)
    except:
        pass

    data, sha = load_data()
    if not data.get('suggestions'): 
        return await interaction.followup.send("❌ No suggestions were found in the database.", ephemeral=True)
    
    # We use followup because we deferred
    await interaction.followup.send("🎰 **Selecting a random category...**")
    
    selected = random.choice(data['suggestions'])
    data['current_cat'] = selected['name']
    data['suggestions'] = [] # Clear for next cycle
    data['status'] = "ADDING_ITEMS"
    
    save_data(data, sha)
    
    announcement = (
        f"@everyone 🎉 The theme for the next World Cup is: **{selected['name'].upper()}**!\n"
        f"(Theme suggested by {selected['user']})\n\n"
        "Submit your entries now using `/additem`!"
    )
    await interaction.channel.send(announcement)

@bot.tree.command(name="removeitem", description="Admin: Remove an item by its list number")
async def removeitem(interaction: discord.Interaction, index: int):
    if not is_admin(interaction.user):
        return await interaction.response.send_message("❌ Admin only.", ephemeral=True)
        
    data, sha = load_data()
    if 1 <= index <= len(data['items']):
        removed_item = data['items'].pop(index - 1)
        save_data(data, sha)
        await interaction.response.send_message(f"🗑️ Successfully removed **{removed_item['name']}**.")
    else:
        await interaction.response.send_message("❌ Invalid index number.", ephemeral=True)

@bot.tree.command(name="edititem", description="Admin: Edit the name, description, or image of an entry")
async def edititem(interaction: discord.Interaction, index: int, new_name: str = None, new_desc: str = None, new_image: discord.Attachment = None):
    if not is_admin(interaction.user):
        return await interaction.response.send_message("❌ Admin only.", ephemeral=True)
        
    data, sha = load_data()
    if 1 <= index <= len(data['items']):
        target = data['items'][index - 1]
        
        if new_name:
            target['name'] = new_name[:75]
        if new_desc:
            target['desc'] = new_desc
        if new_image:
            # Re-upload the new image to the storage channel
            storage_channel = bot.get_channel(STORAGE_CHANNEL_ID)
            stored_msg = await storage_channel.send(file=await new_image.to_file())
            target['image'] = stored_msg.attachments[0].url
            
        save_data(data, sha)
        await interaction.response.send_message(f"📝 Entry #{index} has been updated.")
    else:
        await interaction.response.send_message("❌ Invalid index number.", ephemeral=True)

@bot.tree.command(name="startworldcup", description="Phase 3: Close entries and begin the matches")
async def startworldcup(interaction: discord.Interaction):
    if not is_admin(interaction.user):
        return await interaction.response.send_message("❌ Admin only.", ephemeral=True)
    
    await interaction.response.defer(ephemeral=False)
        
    data, sha = load_data()
    item_count = len(data['items'])
    
    if item_count != 32:
        return await interaction.followup.send(f"❌ You need exactly 32 items to start. (Current: {item_count})")
    
    random.shuffle(data['items'])
    data['bracket'] = list(data['items'])
    data['finished_matches'] = []
    data['winners_pool'] = []
    data['status'] = "MATCH_ACTIVE"
    data['current_match'] = None # Ensure clean start
    
    save_data(data, sha)
    
    await interaction.followup.send(f"🏆 **THE {data['current_cat'].upper()} WORLD CUP HAS BEGUN!**")
    
    # We call post_next from the bot instance
    await bot.post_next(interaction.channel)


@bot.tree.command(name="nextmatch", description="Force the next match to start")
@app_commands.checks.has_permissions(administrator=True)
async def nextmatch(interaction: discord.Interaction):
    # Use ephemeral=True so it's private
    await interaction.response.defer(ephemeral=True)
    
    try:
        data, sha = load_data()
        
        # Check if tournament is finished
        if data.get('status') == "FINISHED":
            await interaction.followup.send("🏁 Tournament is over. Use /endcup.")
            return

        if data.get('current_match'):
            # This finishes the current match
            await bot.resolve_match(data, sha)
            await interaction.followup.send("✅ Match resolved. Next one is starting...")
        else:
            # This starts a match if none is active
            await bot.post_next(interaction.channel)
            await interaction.followup.send("🚀 Next match posted!")
            
    except Exception as e:
        # This will tell you EXACTLY why it is failing instead of just "thinking"
        await interaction.followup.send(f"❌ Error: {e}")






@bot.tree.command(name="resetcup", description="Admin: EMERGENCY WIPE of current tournament")
async def resetcup(interaction: discord.Interaction):
    if not is_admin(interaction.user):
        return await interaction.response.send_message("❌ Admin only.", ephemeral=True)
    
    confirm_view = ResetConfirmView()
    await interaction.response.send_message("⚠️ **Are you sure you want to delete all current tournament progress?**", view=confirm_view, ephemeral=True)

@bot.tree.command(name="endcup", description="Phase 4: Crown winner and move to Hall of Fame")
async def endcup(interaction: discord.Interaction):
    if not is_admin(interaction.user):
        return await interaction.response.send_message("❌ Admin only.", ephemeral=True)
        
    await interaction.response.defer(ephemeral=True)
    data, sha = load_data()
    winner = data.get("final_winner")
    
    # If the cup isn't finished, we set a warning flag
    is_early = winner is None
    
    if is_early:
        msg = "⚠️ **WARNING:** The tournament has not finished yet! Clicking below will delete all current progress and reset the bot without crowning a winner."
    else:
        msg = f"🏆 **Winner Detected: {winner['name']}**\nClick below to archive this result and reset for the next cup."
    
    view = EndConfirmView(data, sha, is_early=is_early)
    await interaction.followup.send(content=msg, view=view, ephemeral=True)


# =========================================================
# SLASH COMMANDS - USERS
# =========================================================

@bot.tree.command(name="help", description="Guide on how to use the World Cup bot")
async def help(interaction: discord.Interaction):
    is_admin_user = is_admin(interaction.user)
    
    desc = "**User Commands:**\n"
    desc += "• `/suggestcategory`: Suggest a theme for the next cup.\n"
    desc += "• `/listcategories`: See all current theme suggestions.\n"
    desc += "• `/additem`: Submit your entry (Name, Desc, Image).\n"
    desc += "• `/matchups`: View the live bracket and upcoming pairs.\n"
    desc += "• `/scoreboard`: See results of finished matches.\n"
    desc += "• `/listitems`: View all entries in the current cup.\n"
    desc += "• `/cuphistory`: View the Hall of Fame archive.\n"
    
    if is_admin_user:
        desc += "\n**Admin Commands:**\n"
        desc += "• `/opensuggestions`: Allow theme suggestions.\n"
        desc += "• `/choosecategory`: Randomly pick the cup theme.\n"
        desc += "• `/startworldcup`: Start the bracket (Requires 32 items).\n"
        desc += "• `/nextmatch`: End the current poll and post the next.\n"
        desc += "• `/edititem`: Fix an entry's name/description.\n"
        desc += "• `/removeitem`: Delete an entry from the list.\n"
        desc += "• `/endcup`: Finish the cup and save winner to history.\n"
        desc += "• `/resetcup`: Emergency wipe of the current cup."

    embed = discord.Embed(title="🏆 World Cup Bot Help", description=desc, color=0x3498db)
    await interaction.response.send_message(embed=embed, ephemeral=True)

@bot.tree.command(name="suggestcategory", description="Submit a theme idea")
async def suggestcategory(interaction: discord.Interaction, name: str):
    # 1. Defer immediately to give GitHub time to respond
    await interaction.response.defer(ephemeral=True)
    
    data, sha = load_data()
    user_mention = f"<@{interaction.user.id}>"
    clean_name = name.strip().lower()
    
    if data['status'] != "SUGGESTIONS_OPEN":
        return await interaction.followup.send("❌ Suggestions are closed.", ephemeral=True)
    
    # Check for duplicates
    for s in data['suggestions']:
        if s['name'].lower() == clean_name:
            return await interaction.followup.send(f"❌ '{name}' has already been suggested!", ephemeral=True)
    
    # Check if user already suggested something (Admins bypass)
    if not is_admin(interaction.user):
        if any(s['user'] == user_mention for s in data['suggestions']):
            return await interaction.followup.send("❌ You've already submitted a theme!", ephemeral=True)
    
    # Save the data
    data.setdefault('suggestions', []).append({
        "name": name[:100], 
        "user": user_mention
    })
    save_data(data, sha)
    
    # 2. Use followup.send instead of response.send_message
    await interaction.followup.send(f"💡 Logged suggestion: **{name}**", ephemeral=False)

@bot.tree.command(name="additem", description="Submit an item for the bracket")
async def additem(interaction: discord.Interaction, name: str, description: str, image: discord.Attachment):
    # Try to defer, but catch the error if Discord is being slow
    try:
        await interaction.response.defer(ephemeral=False)
    except discord.errors.NotFound:
        print("Interaction timed out before defer could finish. Attempting to proceed...")
    
    data, sha = load_data()
    user_mention = f"<@{interaction.user.id}>"
    clean_name = name.strip().lower()

    if data['status'] != "ADDING_ITEMS":
        return await interaction.followup.send("❌ Submissions are closed.", ephemeral=True)
    
    # Duplicate check
    for item in data['items']:
        if item['name'].lower() == clean_name:
            return await interaction.followup.send(f"❌ '{name}' is already in!", ephemeral=True)

    if len(data['items']) >= 32:
        return await interaction.followup.send("❌ Bracket full!", ephemeral=True)
    
    # Upload Logic
    storage_channel = bot.get_channel(STORAGE_CHANNEL_ID)
    try:
        attachment_file = await image.to_file()
        storage_msg = await storage_channel.send(file=attachment_file)
        image_url = storage_msg.attachments[0].url
    except Exception as e:
        return await interaction.followup.send("❌ Image upload failed.", ephemeral=True)

    # Save Data
    short_name = name[:75]
    data['items'].append({
        "name": short_name, 
        "desc": description, 
        "image": image_url, 
        "user": user_mention
    })
    
    save_data(data, sha)
    
    # Final check: If the interaction is truly dead, this will log but at least data is saved
    try:
        await interaction.followup.send(f"✅ **{short_name}** added! ({len(data['items'])}/32)")
    except Exception as e:
        print(f"Could not send confirmation message: {e}")



@bot.tree.command(name="matchups", description="View the current bracket and upcoming matches")
async def matchups(interaction: discord.Interaction):
    # Defer first!
    await interaction.response.defer()
    
    data, _ = load_data()
    
    if data['status'] not in ["MATCH_ACTIVE", "FINISHED"]:
        return await interaction.followup.send("❌ No active bracket to display.", ephemeral=True)
    
    bracket_text = ""
    current = data.get('current_match')
    if current:
        bracket_text += "🔴 **CURRENT MATCH:**\n"
        bracket_text += f"{current['item_a']['name']} vs {current['item_b']['name']}\n\n"
    
    if data.get('bracket'):
        bracket_text += "🕒 **UPCOMING MATCHES:**\n"
        temp_list = list(data['bracket'])
        while len(temp_list) >= 2:
            a = temp_list.pop(0)
            b = temp_list.pop(0)
            bracket_text += f"• {a['name']} vs {b['name']}\n"
    
    if data.get('winners_pool'):
        bracket_text += "\n🛡️ **WAITING FOR NEXT ROUND:**\n"
        names = [w['name'] for w in data['winners_pool']]
        bracket_text += ", ".join(names)
        
    embed = discord.Embed(
        title=f"⚔️ {data.get('current_cat', 'Tournament').upper()} Live Bracket", 
        description=bracket_text or "Tournament in transition...", 
        color=0x3498db
    )
    # Use followup since we deferred
    await interaction.followup.send(embed=embed)


@bot.tree.command(name="listitems", description="Gallery of all entries")
async def listitems(interaction: discord.Interaction):
    # 1. Defer immediately because load_data() can be slow
    await interaction.response.defer()
    
    # 2. Fetch the data
    data, _ = load_data()
    
    # 3. Setup the view
    gallery_view = ItemGallery(data['items'])
    
    # 4. Use followup since we deferred
    await interaction.followup.send(embed=gallery_view.create_content(), view=gallery_view)

@bot.tree.command(name="scoreboard", description="View tournament history")
async def scoreboard(interaction: discord.Interaction):
    await interaction.response.defer() 
    data, _ = load_data()
    # Get matches and reverse them so newest are first
    matches = list(reversed(data.get('finished_matches', [])))
    
    view = ScoreboardView(matches)
    await interaction.followup.send(embed=view.create_embed(), view=view)




@bot.tree.command(name="cuphistory", description="Hall of Fame")
async def cuphistory(interaction: discord.Interaction):
    await interaction.response.defer()
    data, _ = load_data()
    leaderboard = data.get('leaderboard', [])
    
    if not leaderboard:
        return await interaction.followup.send("No Hall of Fame records found.")
    
    history_view = HistoryView(leaderboard)
    await interaction.followup.send(embed=history_view.create_embed(), view=history_view)

@bot.tree.command(name="listcategories", description="See all suggested themes so far")
async def listcategories(interaction: discord.Interaction):
    await interaction.response.defer()
    
    data, _ = load_data()
    if not data.get('suggestions'):
        return await interaction.followup.send("No suggestions yet! Use `/suggestcategory` to add one.", ephemeral=True)
    
    txt = ""
    for idx, s in enumerate(data['suggestions']):
        txt += f"{idx+1}. **{s['name']}** (Suggested by {s['user']})\n"
        
    embed = discord.Embed(title="💡 Theme Suggestions", description=txt, color=0x3498db)
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="currentvotes", description="See who has voted in the active match")
async def currentvotes(interaction: discord.Interaction):
    # Defer since we are pulling from GitHub and processing mentions
    await interaction.response.defer(ephemeral=False)
    
    data, _ = load_data()
    match = data.get("current_match")
    
    if not match:
        return await interaction.followup.send("❌ There is no active match right now.")
    
    votes = match.get("votes", {})
    
    if not votes:
        return await interaction.followup.send(f"🗳️ **{match['item_a']['name']} vs {match['item_b']['name']}**\nNo votes have been cast yet!")

    # Separate voters into two lists
    list_a = []
    list_b = []
    
    for user_id, choice in votes.items():
        mention = f"<@{user_id}>"
        if choice == "A":
            list_a.append(mention)
        else:
            list_b.append(mention)

    # Format the lists for the embed
    str_a = "\n".join(list_a) if list_a else "_No votes_"
    str_b = "\n".join(list_b) if list_b else "_No votes_"
    
    embed = discord.Embed(
        title="🗳️ Current Vote Breakdown",
        description=f"**{match['item_a']['name']}** vs **{match['item_b']['name']}**",
        color=0x3498db
    )
    
    embed.add_field(name=f"🔵 {match['item_a']['name']} ({len(list_a)})", value=str_a, inline=True)
    embed.add_field(name=f"🔴 {match['item_b']['name']} ({len(list_b)})", value=str_b, inline=True)
    
    embed.set_footer(text=f"Total Votes: {len(votes)}")
    
    await interaction.followup.send(embed=embed)

@bot.tree.command(name="status", description="Check tournament progress and time remaining")
async def status(interaction: discord.Interaction):
    await interaction.response.defer()
    data, _ = load_data()
    status_mode = data.get('status', 'IDLE')
    embed = discord.Embed(title="🏆 World Cup Dashboard", color=0x3498db)
    
    if status_mode == "IDLE":
        embed.description = "The bot is currently **Idle**. Waiting for an admin to open suggestions."
        
    elif status_mode == "SUGGESTIONS_OPEN":
        count = len(data.get('suggestions', []))
        embed.description = (
            f"💡 **Theme Suggestions Open**\n\n"
            f"Total Suggestions: **{count}**\n"
            f"Use `/suggestcategory`!"
        )
        
    elif status_mode == "ADDING_ITEMS":
        count = len(data.get('items', []))
        filled = int((count / 32) * 10)
        bar = "🟩" * filled + "⬜" * (10 - filled)
        embed.description = (
            f"📦 **Submissions: {data.get('current_cat', 'Tournament').upper()}**\n\n"
            f"Entries: {count}/32\n"
            f"`{bar}`"
        )
        
    elif status_mode == "MATCH_ACTIVE":
        match = data.get('current_match')
        if match:
            round_name = get_round_name(data)
            
            # Timer Logic
            time_info = ""
            start_str = match.get("start_time")
            if start_str:
                start_dt = datetime.datetime.fromisoformat(start_str)
                end_dt = start_dt + datetime.timedelta(hours=24)
                unix_end = int(end_dt.timestamp())
                time_info = f"\n⏳ **Match ends:** <t:{unix_end}:R>"

            embed.description = (
                f"⚔️ **Current Stage:** {round_name}\n\n"
                f"**Match:** {match['item_a']['name']} vs {match['item_b']['name']}\n"
                f"📊 **Votes cast:** {len(match.get('votes', {}))}"
                f"{time_info}"
            )
        else:
            embed.description = "🔄 **Processing...** Next match loading."

    elif status_mode == "FINISHED":
        winner = data.get('final_winner')
        if winner:
            embed.description = (
                f"🏁 **Tournament Complete!**\n\n"
                f"The champion is **{winner['name']}**!\n\n"
                "⚠️ **Admin:** Use `/endcup` to crown the winner and reset."
            )
            embed.set_thumbnail(url=winner['image'])
        else:
            embed.description = "Tournament finished, but no winner was recorded."

    embed.set_footer(text="The Landing Strip World Cup System")
    await interaction.followup.send(embed=embed)


# =========================================================
# ON READY
# =========================================================
@bot.event
async def on_ready():
    # Sync commands to Discord
    await bot.tree.sync()
    print(f"✅ Successfully logged in as {bot.user}")
    print(f"✅ Commands Synced to Server.")

if __name__ == "__main__":
    keep_alive()
    bot.run(TOKEN)
