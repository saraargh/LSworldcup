import discord
from discord import app_commands
import requests
import base64
import json
import os

# ================= CONFIG =================

TOKEN = os.getenv("WC_TOKEN")          # Discord bot token
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")   # SAME PAT for now (as you confirmed)
GITHUB_REPO = "saraargh/LSworldcup"
GITHUB_FILE_PATH = "tournament_data.json"

HEADERS = {
    "Authorization": f"token {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json"
}

# ================= GITHUB HELPERS =================

def gh_url():
    return f"https://api.github.com/repos/{GITHUB_REPO}/contents/{GITHUB_FILE_PATH}"

def load_data():
    r = requests.get(gh_url(), headers=HEADERS, timeout=10)

    print("GITHUB GET STATUS:", r.status_code)

    if r.status_code != 200:
        print("GITHUB GET FAILED:", r.text)
        return None, None

    payload = r.json()
    raw = base64.b64decode(payload["content"]).decode()
    data = json.loads(raw)
    sha = payload["sha"]

    print("GITHUB LOAD OK — items:", len(data.get("items", [])))
    return data, sha


def save_data(data, sha):
    payload = {
        "message": "Test update from WC test bot",
        "content": base64.b64encode(
            json.dumps(data, indent=4).encode()
        ).decode(),
        "sha": sha
    }

    r = requests.put(
        gh_url(),
        headers=HEADERS,
        json=payload,
        timeout=10
    )

    print("GITHUB PUT STATUS:", r.status_code)

    if r.status_code not in (200, 201):
        print("GITHUB PUT FAILED:", r.text)
        return False

    return True


# ================= DISCORD BOT =================

intents = discord.Intents.default()

class TestBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync()
        print("Slash commands synced")


bot = TestBot()

# ================= COMMANDS =================

@bot.tree.command(name="wc_test_list", description="List items from GitHub JSON")
async def wc_test_list(interaction: discord.Interaction):
    data, _ = load_data()
    if data is None:
        return await interaction.response.send_message(
            "❌ Failed to load GitHub data (check logs)",
            ephemeral=True
        )

    items = data.get("items", [])
    if not items:
        return await interaction.response.send_message(
            "⚠️ No items found in JSON",
            ephemeral=True
        )

    text = "\n".join(f"• {i}" for i in items[:20])
    await interaction.response.send_message(
        f"📋 Items (showing up to 20):\n{text}",
        ephemeral=True
    )


@bot.tree.command(name="wc_test_add", description="Add ONE test item to GitHub JSON")
@app_commands.describe(name="Item name")
async def wc_test_add(interaction: discord.Interaction, name: str):
    data, sha = load_data()
    if data is None:
        return await interaction.response.send_message(
            "❌ Failed to load GitHub data (check logs)",
            ephemeral=True
        )

    if name in data.get("items", []):
        return await interaction.response.send_message(
            "⚠️ Item already exists",
            ephemeral=True
        )

    data.setdefault("items", []).append(name)

    ok = save_data(data, sha)
    if not ok:
        return await interaction.response.send_message(
            "❌ Failed to write to GitHub (check logs)",
            ephemeral=True
        )

    await interaction.response.send_message(
        f"✅ Added **{name}** to GitHub JSON",
        ephemeral=True
    )


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} ({bot.user.id})")

bot.run(TOKEN)