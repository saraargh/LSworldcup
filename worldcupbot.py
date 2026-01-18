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
            description_text += f"{count}. <:cutecup:1462480543449874442> **{entry['cat'].upper()}**\n"
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


class MatchView(ui.View):
    def __init__(self, item_a, item_b, round_name=None, match_num=None):
        super().__init__(timeout=None)
        self.item_a = item_a
        self.item_b = item_b
        self.round_name = round_name or "Active Match"
        self.match_num = match_num or "1"
        
        # Set button labels based on item names
        if self.item_a and self.item_b:
            self.vote_a.label = f"Vote for {self.item_a['name']}"
            self.vote_b.label = f"Vote for {self.item_b['name']}"

    def create_embed(self, page, match_data=None):
        # 1. Use passed data if available, otherwise fetch from GitHub
        if match_data:
            match = match_data
        else:
            data, _ = load_data()
            match = data.get("current_match", {})
        
        # 2. Extract votes safely (defaults to empty if match is None)
        votes = match.get("votes", {}) if match else {}
        count_a = list(votes.values()).count("A")
        count_b = list(votes.values()).count("B")

        # 3. Build the embed using the items stored in 'self'
        item = self.item_a if page == 0 else self.item_b
        color = 0xff4757 if page == 0 else 0x2e86de
        
        embed = discord.Embed(
            title=f"<:crossedswords:1462484325151801487> {self.round_name} - Match {self.match_num}: {self.item_a['name']} vs {self.item_b['name']}",
            description=f"**Viewing:** {item['name']}\n\n**Description:** {item.get('desc', 'No description.')}\n\n**Submitter:** {item.get('user', 'Unknown')}",
            color=color
        )
        embed.add_field(
            name="\n\n📊 Live Standings", 
            value=f"**{self.item_a['name']}:** {count_a} votes\n**{self.item_b['name']}:** {count_b} votes", 
            inline=False
        )
        embed.set_image(url=item['image'])
        embed.set_footer(text=f"Total Votes Cast: {len(votes)}")
        return embed


    @ui.button(emoji="<:left:1462297168382656732>", style=discord.ButtonStyle.gray, custom_id="v_a_nav", row=0)
    async def view_a(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.edit_message(embed=self.create_embed(0))

    @ui.button(emoji="<:right:1462297211659358444>", style=discord.ButtonStyle.gray, custom_id="v_b_nav", row=0)
    async def view_b(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.edit_message(embed=self.create_embed(1))

    @ui.button(label="Show Voter List", style=discord.ButtonStyle.success, custom_id="view_voters", row=0)
    async def view_voters(self, interaction: discord.Interaction, button: ui.Button):
        data, _ = load_data()
        match = data.get("current_match", {})
        votes = match.get("votes", {})
        
        if not votes:
            return await interaction.response.send_message("No votes have been cast yet!", ephemeral=True)
        
        list_a = [f"<@{uid}>" for uid, choice in votes.items() if choice == "A"]
        list_b = [f"<@{uid}>" for uid, choice in votes.items() if choice == "B"]
        
        embed = discord.Embed(title="📊 Current Voter Breakdown", color=0x95a5a6)
        embed.add_field(name=f"Votes for {self.item_a['name']}", value="\n".join(list_a) if list_a else "None", inline=True)
        embed.add_field(name=f"Votes for {self.item_b['name']}", value="\n".join(list_b) if list_b else "None", inline=True)
        
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @ui.button(style=discord.ButtonStyle.danger, custom_id="vote_a_match", row=1)
    async def vote_a(self, interaction: discord.Interaction, button: ui.Button):
        data, sha = load_data()
        match = data.get("current_match")
        if not match: 
            return await interaction.response.send_message("❌ Match not active.", ephemeral=True)
        
        user_id = str(interaction.user.id)
        existing_votes = match.get("votes", {})
        
        # Check if already voted for A
        if existing_votes.get(user_id) == "A":
            return await interaction.response.send_message(f"⚠️ You already voted for **{self.item_a['name']}**!", ephemeral=True)
        
        # Save vote
        existing_votes[user_id] = "A"
        match["votes"] = existing_votes
        save_data(data, sha)
        
        await interaction.response.send_message(f"✅ Voted for **{self.item_a['name']}**!", ephemeral=True)
        
        # Smart Refresh: Keep user on current page (A or B)
        current_page = 0
        if interaction.message.embeds and self.item_b['name'] in interaction.message.embeds[0].description:
            current_page = 1
        await interaction.message.edit(embed=self.create_embed(current_page))

    @ui.button(style=discord.ButtonStyle.primary, custom_id="vote_b_match", row=1)
    async def vote_b(self, interaction: discord.Interaction, button: ui.Button):
        data, sha = load_data()
        match = data.get("current_match")
        if not match: 
            return await interaction.response.send_message("❌ Match not active.", ephemeral=True)
        
        user_id = str(interaction.user.id)
        existing_votes = match.get("votes", {})
        
        # Check if already voted for B
        if existing_votes.get(user_id) == "B":
            return await interaction.response.send_message(f"⚠️ You already voted for **{self.item_b['name']}**!", ephemeral=True)
        
        # Save vote
        existing_votes[user_id] = "B"
        match["votes"] = existing_votes
        save_data(data, sha)
        
        await interaction.response.send_message(f"✅ Voted for **{self.item_b['name']}**!", ephemeral=True)
        
        # Smart Refresh: Keep user on current page
        current_page = 1
        if interaction.message.embeds and self.item_a['name'] in interaction.message.embeds[0].description:
            current_page = 0
        await interaction.message.edit(embed=self.create_embed(current_page))


class EndConfirmView(ui.View):
    def __init__(self, data, sha, is_early=False):
        super().__init__(timeout=60)
        self.data = data
        self.sha = sha
        self.is_early = is_early
        
        winner = self.data.get("final_winner")
        if winner:
            self.confirm_end.style = discord.ButtonStyle.success
            self.confirm_end.label = f"<:cutecup:1462480543449874442> CROWN {winner['name'].upper()}"
        else:
            self.confirm_end.style = discord.ButtonStyle.danger
            self.confirm_end.label = "⚠️ CONFIRM: END TOURNAMENT EARLY"

    @ui.button(label="CONFIRM END", style=discord.ButtonStyle.secondary)
    async def confirm_end(self, interaction: discord.Interaction, button: ui.Button):
        winner = self.data.get("final_winner")
        
        if winner:
            history = self.data.get('finished_matches', [])
            stats = {} # { "Movie Name": {"votes": X, "user": "Name"} }

            for m in history:
                votes = m.get('votes', {})
                # Process Item A
                na = m['item_a']['name']
                stats.setdefault(na, {"votes": 0, "user": m['item_a'].get('user', 'Unknown')})
                stats[na]["votes"] += list(votes.values()).count("A")
                # Process Item B
                nb = m['item_b']['name']
                stats.setdefault(nb, {"votes": 0, "user": m['item_b'].get('user', 'Unknown')})
                stats[nb]["votes"] += list(votes.values()).count("B")

            if stats:
                sorted_stats = sorted(stats.items(), key=lambda x: x[1]['votes'], reverse=True)
                
                # Logic for Most/Least Voted with Tie Handling
                max_v = max(s['votes'] for s in stats.values())
                min_v = min(s['votes'] for s in stats.values())
                
                most_list = [f"{n} (by {s['user']})" for n, s in stats.items() if s['votes'] == max_v]
                least_list = [f"{n} (by {s['user']})" for n, s in stats.items() if s['votes'] == min_v]

                # Podium items
                second_text = "N/A"
                if len(sorted_stats) > 1:
                    n2, d2 = sorted_stats[1]
                    second_text = f"{n2} (by {d2['user']})"
                
                third_text = "N/A"
                if len(sorted_stats) > 2:
                    n3, d3 = sorted_stats[2]
                    third_text = f"{n3} (by {d3['user']})"

                # --- BUILD THE SPECIAL MENTIONS STRING ---
                mentions = (
                    f"🥈 **Second Place:** {second_text}\n"
                    f"🥉 **Third Place:** {third_text}\n\n"
                    f"<:spongebob:1462295496637812817> **Most Votes Received:** {', '.join(most_list)} — **{max_v} votes**\n"
                    f"<:patrick:1462296137921728533> **Least Votes Received:** {', '.join(least_list)} — **{min_v} votes**"
                )

                embed = discord.Embed(
                    title="🎊 CHAMPION CROWNED 🎊", 
                    description=f"# 👑 {winner['name'].upper()} <:winner:1462297763260923946>\n\nWinner of the **{self.data['current_cat']}** World Cup!\n**Submitted by:** {winner['user']}", 
                    color=0xf1c40f
                )
                embed.add_field(name="SPECIAL MENTIONS 🗯️✨", value=mentions, inline=False)
                embed.set_image(url=winner['image'])
            
            # Save to leaderboard
            self.data.setdefault('leaderboard', []).append({
                "item": winner['name'], 
                "cat": self.data['current_cat'], 
                "user": winner['user']
            })
            await interaction.channel.send("@everyone <:cutecup:1462480543449874442> **TOURNAMENT COMPLETE!**", embed=embed)
        else:
            await interaction.channel.send("🛑 **TOURNAMENT ENDED MANUALLY.**")
        
        # 2. Sweep Pins
        try:
            pins = await interaction.channel.pins()
            for pin in pins:
                if pin.author.id == interaction.client.user.id:
                    await pin.unpin()
        except: pass

        # 3. Reset Data
        self.data.update({
            "status": "IDLE", "suggestions": [], "bracket": [], "winners_pool": [], 
            "finished_matches": [], "current_match": None, "current_cat": None, "final_winner": None
        })
        
        save_data(self.data, self.sha)
        await interaction.response.edit_message(content="✅ **Tournament wiped, pins cleared, and bot reset.**", view=None)





class ScoreboardView(ui.View):
    def __init__(self, matches=None):
        super().__init__(timeout=None)
        self.matches = list(reversed(matches or []))
        self.page = 0
        self.view_mode = "HISTORY" 

    def create_embed(self, data):
        embed = discord.Embed(color=0x3498db)
        
        if self.view_mode == "HISTORY":
            start = self.page * 5
            chunk = self.matches[start:start+5]
            embed.title = "📊 Match History"
            
            description_text = "*Newest results at the top*\n\n"
            if not self.matches:
                description_text += "No matches have finished yet."
            else:
                for m in chunk:
                    description_text += (
                        f"🔹 **{m['name']}**\n"
                        f"<:cutecup:1462480543449874442> **{m['winner']}** ({m.get('score', '0-0')})\n\n"
                        
            embed.description = description_text
            total_pages = (len(self.matches) - 1) // 5 + 1 if self.matches else 1
            embed.set_footer(text=f"Page {self.page + 1} of {total_pages}")

        else:
            # --- SURVIVOR ONLY MODE ---
            embed.title = "🟢 Remaining Survivors"
            
            survivors = []
            curr = data.get('current_match')
            if curr:
                survivors.append(f"{curr['item_a']['name']} ({curr['item_a']['user']})")
                survivors.append(f"{curr['item_b']['name']} ({curr['item_b']['user']})")
            for item in data.get('bracket', []):
                survivors.append(f"{item['name']} ({item['user']})")
            for item in data.get('winners_pool', []):
                survivors.append(f"{item['name']} ({item['user']})")

            survivor_list = "\n".join([f"🟢 {s}" for s in survivors]) if survivors else "No survivors."

            embed.description = (
                f"**{len(survivors)} entries still in the running:**\n\n"
                f"{survivor_list}"
            )
            embed.set_footer(text="The path to the Grand Final")

        return embed

    @ui.button(emoji="<:left:1462297168382656732>", style=discord.ButtonStyle.gray, custom_id="sb_nav_newer")
    async def prev(self, interaction: discord.Interaction, button: ui.Button):
        data, _ = load_data()
        self.view_mode = "HISTORY"
        self.page = max(0, self.page - 1)
        await interaction.response.edit_message(embed=self.create_embed(data), view=self)

    @ui.button(emoji="<:right:1462297211659358444>", style=discord.ButtonStyle.gray, custom_id="sb_nav_older")
    async def next(self, interaction: discord.Interaction, button: ui.Button):
        data, _ = load_data()
        self.view_mode = "HISTORY"
        if (self.page + 1) * 5 < len(self.matches):
            self.page += 1
        await interaction.response.edit_message(embed=self.create_embed(data), view=self)

    @ui.button(label="🟢 Survivors", style=discord.ButtonStyle.success, custom_id="sb_nav_toggle")
    async def toggle_survivors(self, interaction: discord.Interaction, button: ui.Button):
        data, _ = load_data()
        if self.view_mode == "HISTORY":
            self.view_mode = "SURVIVORS"
            button.label = "📜 View History"
        else:
            self.view_mode = "HISTORY"
            button.label = "🟢 Survivors"
            
        await interaction.response.edit_message(embed=self.create_embed(data), view=self)


# =========================================================
# BOT CORE CLASS
# =========================================================
class WC_Bot(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents.all())
        self.tree = app_commands.CommandTree(self)
        self.processing_lock = asyncio.Lock()

    async def setup_hook(self):
        data, _ = load_data()
        match = data.get("current_match")

        # If a match is active in your JSON, we pass the real items
        if match and "item_a" in match:
            self.add_view(MatchView(
                match['item_a'], 
                match['item_b'], 
                match.get('round_name', 'Grand Final'), # Default if missing from JSON
                match.get('match_num', '1')             # Default if missing from JSON
            ))
        else:
            # Only use None if the tournament hasn't started
            self.add_view(MatchView(None, None, None, None))
            
        self.add_view(ScoreboardView())
        self.add_view(ItemGallery())
        self.add_view(HistoryView())
        
        await self.tree.sync()
        print(f"✅ Synced and successfully loaded match from JSON.")

    # --- THE ENGINE: POST NEXT MATCH ---

    async def post_next(self, channel):
        data, sha = load_data()
        
        # 1. Bracket logic
        if not data.get('bracket') or len(data['bracket']) < 2:
            if len(data.get('winners_pool', [])) >= 2:
                old_round = get_round_name(data)
                data['bracket'] = list(data['winners_pool'])
                data['winners_pool'] = []
                new_round = get_round_name(data)
                await channel.send(f"🛡️ **{old_round} Complete!** Advancing survivors to the **{new_round}**...")
            elif len(data.get('winners_pool', [])) == 1:
                data['final_winner'] = data['winners_pool'][0]
                data['status'] = "FINISHED"
                data['current_match'] = None
                save_data(data, sha)
                winner = data['final_winner']
                embed = discord.Embed(
                    title="🏁 TOURNAMENT COMPLETE 🏁",
                    description=f"The bracket has concluded! Use **/endcup** to crown **{winner['name']}** and archive the results.",
                    color=0xf1c40f
                )
                return await channel.send(embed=embed)
            else:
                data['status'] = "FINISHED"
                save_data(data, sha)
                return await channel.send("🎊 **Tournament Complete!**")

        # 2. Pop competitors
        comp_a = data['bracket'].pop(0)
        comp_b = data['bracket'].pop(0)
        
        # 3. Calculate round info
        round_name = get_round_name(data)
        match_num = len(data.get('finished_matches', [])) + 1
        
        # 4. Create the View & Message (Back to original simple call)
        view = MatchView(comp_a, comp_b, round_name, match_num) 
        msg = await channel.send(
            content=f"<:crossedswords:1462484325151801487> **{round_name}** is now LIVE!", 
            embed=view.create_embed(0), 
            view=view
        )
        
        # 5. Save state
        data['current_match'] = {
            "item_a": comp_a, 
            "item_b": comp_b, 
            "votes": {}, 
            "message_id": msg.id, 
            "channel_id": channel.id,
            "round_name": round_name,
            "match_num": match_num
        }
        data['status'] = "MATCH_ACTIVE"
        save_data(data, sha)
        
        try:
            await msg.pin()
        except:
            pass





    # --- THE ENGINE: RESOLVE MATCH ---
    async def resolve_match(self, data, sha):
        async with self.processing_lock:
            match = data.get('current_match')
            if not match:
                return

            channel = self.get_channel(match['channel_id'])
            if not channel:
                channel = await self.fetch_channel(match['channel_id'])

            # 1. Count Votes
            votes = list(match.get('votes', {}).values())
            count_a = votes.count("A")
            count_b = votes.count("B")

            # 2. Determine Winner & Loser
            if count_a >= count_b:
                winner, loser = match['item_a'], match['item_b']
                winning_score, losing_score = count_a, count_b
            else:
                winner, loser = match['item_b'], match['item_a']
                winning_score, losing_score = count_b, count_a

            # 3. Archive Results for Scoreboard
            data.setdefault('finished_matches', []).append({
                "name": f"{match['item_a']['name']} vs {match['item_b']['name']}",
                "winner": winner['name'],
                "winner_user": winner.get('user', 'Unknown'),
                "loser_name": loser['name'],
                "score": f"{count_a}-{count_b}"
            })

            # 4. Move Winner to next round pool
            data.setdefault('winners_pool', []).append(winner)
            
            # 5. Clean up current match state
            old_msg_id = match['message_id']
            data['current_match'] = None
            save_data(data, sha)

            # 6. Unpin old match (Keeps the channel clean)
            try:
                old_msg = await channel.fetch_message(old_msg_id)
                await old_msg.unpin()
            except:
                pass

            # 7. THE WINNER EMBED (Restored Hype Version)
            win_embed = discord.Embed(
                title="<:cutecup:1462480543449874442> MATCH CONCLUDED",
                description=f"### {winner['name']} has DEFEATED {loser['name']}!",
                color=0x2ecc71 # Victory Green
            )
            win_embed.add_field(name="Final Score", value=f"✅ **{winning_score}** —  ❌ **{losing_score}**", inline=False)
            win_embed.add_field(name="Advancing to Next Round", value=f"<:cutestar:1462482027273129994> {winner['name']}", inline=True)
            win_embed.add_field(name="Submitted by", value=f"<:submitter:1462482380211097828> {winner.get('user', 'Unknown')}", inline=True)
            
            # Show the winning entry's image in the announcement
            if winner.get('image'):
                win_embed.set_thumbnail(url=winner['image'])
            
            win_embed.set_footer(text=f"The tournament continues... | Total Votes: {len(votes)}")

            await channel.send(embed=win_embed)
            
            # 8. Pause for effect, then post the next match
            await asyncio.sleep(4) 
            await self.post_next(channel)


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
        f"@everyone 🎉 The theme for this tournament is: **{selected['name'].upper()}**!\n"
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
    # 1. DEFER IMMEDIATELY - Do not put anything above this!
    try:
        await interaction.response.defer(ephemeral=False)
    except:
        return # Interaction already failed

    if not is_admin(interaction.user):
        return await interaction.followup.send("❌ Admin only.", ephemeral=True)
    
    # 2. Now it's safe to do the slow GitHub work
    data, sha = load_data()
    
    item_count = len(data.get('items', []))
    if item_count != 32:
        return await interaction.followup.send(f"❌ You need exactly 32 items to start. (Current: {item_count})")

    # 3. Prepare items
    shuffled = list(data['items'])
    random.shuffle(shuffled)
    comp_a = shuffled.pop(0)
    comp_b = shuffled.pop(0)

    # 4. Save tournament state
    data['status'] = "MATCH_ACTIVE"
    data['bracket'] = shuffled
    data['winners_pool'] = []
    data['finished_matches'] = []
    data['current_match'] = {
        "item_a": comp_a, 
        "item_b": comp_b, 
        "votes": {}, 
        "round_name": "Round of 32",
        "match_num": 1,
        "channel_id": interaction.channel_id
    }
    save_data(data, sha)

    # 5. Create view and send message
    # MatchView.create_embed now has the safety check we added earlier
    view = MatchView(comp_a, comp_b, "Round of 32", 1)
    
    await interaction.followup.send(f"<:cutecup:1462480543449874442> **THE {data['current_cat'].upper()} WORLD CUP HAS BEGUN!**")
    
    msg = await interaction.channel.send(
        content="<:crossedswords:1462484325151801487> **Round of 32** is now LIVE!", 
        embed=view.create_embed(0), 
        view=view
    )
    
    # 6. Update message ID and pin
    data, sha = load_data()
    data['current_match']['message_id'] = msg.id
    save_data(data, sha)
    
    try:
        await msg.pin()
    except:
        pass





@bot.tree.command(name="nextmatch", description="Force the next match to start")
@app_commands.checks.has_permissions(administrator=True)
async def nextmatch(interaction: discord.Interaction):
    # 1. DO THIS FIRST. No code, no loading, nothing should be above this.
    try:
        await interaction.response.defer(ephemeral=True)
    except:
        pass # If it already deferred, ignore

    # 2. NOW do the slow work
    data, sha = load_data()
    
    if not data.get('current_match'):
        return await interaction.followup.send("❌ No match is currently active.")

    # 3. Resolve and move on
    # Using followup.send because we already deferred
    await interaction.followup.send("🔄 Closing votes and starting next match...")
    await bot.resolve_match(data, sha)



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
        msg = f"<:cutecup:1462480543449874442> **Winner Detected: {winner['name']}**\nClick below to archive this result and reset for the next cup."
    
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

    embed = discord.Embed(title="<:cutecup:1462480543449874442> World Cup Bot Help", description=desc, color=0x3498db)
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
        title=f"<:crossedswords:1462484325151801487> {data.get('current_cat', 'Tournament').upper()} Live Bracket", 
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

@bot.tree.command(name="scoreboard", description="View match history and tournament survivors")
async def scoreboard(interaction: discord.Interaction):
    await interaction.response.defer()
    data, _ = load_data()
    finished = data.get('finished_matches', [])
    
    view = ScoreboardView(finished)
    await interaction.followup.send(embed=view.create_embed(data), view=view)



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
    # 1. Defer first to avoid timeout errors
    await interaction.response.defer()
    
    data, _ = load_data()
    status_mode = data.get('status', 'IDLE')
    embed = discord.Embed(title="<:cutecup:1462480543449874442> World Cup Dashboard", color=0x3498db)
    
    if status_mode == "IDLE":
        embed.description = "The bot is currently **Idle**. Waiting for an admin to open suggestions."
        
    elif status_mode == "SUGGESTIONS_OPEN":
        count = len(data.get('suggestions', []))
        embed.description = (
            "💡 **Theme Suggestions**\n"
            "We are currently collecting themes! Use `/suggestcategory` to join in.\n\n"
            f"**Total Suggestions:** {count}"
        )
        
    elif status_mode == "ADDING_ITEMS":
        count = len(data.get('items', []))
        # Progress bar for item submissions (0 to 32)
        filled = int((count / 32) * 10)
        bar = "🟩" * filled + "⬜" * (10 - filled)
        embed.description = (
            f"📦 **Submissions**\nTheme: **{data['current_cat'].upper()}**\n"
            f"Submit entries with `/additem`!\n\n"
            f"**Entries:** {count}/32\n`{bar}`"
        )
        
    elif status_mode == "MATCH_ACTIVE":
        match = data.get('current_match')
        if match:
            vote_count = len(match.get('votes', {}))
            round_name = get_round_name(data)
            
            # Match Progress Logic (31 total matches for 32 items)
            current_num = len(data.get('finished_matches', [])) + 1
            total_matches = 31 
            
            # Progress bar for the tournament (0 to 31)
            pct = int((current_num / total_matches) * 10)
            bar = "🟩" * pct + "⬜" * (10 - pct)
            
            # Time Calculation
            time_info = ""
            start_str = match.get("start_time")
            if start_str:
                start_time = datetime.datetime.fromisoformat(start_str)
                end_time = start_time + datetime.timedelta(hours=24)
                remaining = end_time - datetime.datetime.now()
                
                if remaining.total_seconds() > 0:
                    hours, remainder = divmod(int(remaining.total_seconds()), 3600)
                    minutes, _ = divmod(remainder, 60)
                    time_info = f"\n⏳ **Time Remaining:** {hours}h {minutes}m"
                else:
                    time_info = "\n✅ **Match time complete!** Admins can close this now."

            embed.description = (
                f"<:crossedswords:1462484325151801487> **Matches Live**\n"
                f"Theme: **{data['current_cat'].upper()}**\n\n"
                f"**Current Round:** {round_name}\n"
                f"**Match Progress:** {current_num} of {total_matches}\n"
                f"`{bar}`\n\n"
                f"**Active Match:** {match['item_a']['name']} vs {match['item_b']['name']}\n"
                f"📊 **Total Votes cast so far:** {vote_count}"
                f"{time_info}"
            )
        else:
            embed.description = "🔄 **Processing...** Moving to the next match."

    elif status_mode == "FINISHED":
        winner = data.get('final_winner')
        embed.description = (
            f"🏁 **Tournament Complete**\n"
            f"The champion of **{data['current_cat']}** is **{winner['name']}**!\n\n"
            "Waiting for admins to archive and reset."
        )
        if winner:
            embed.set_thumbnail(url=winner['image'])

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