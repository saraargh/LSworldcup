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
from io import BytesIO
from PIL import Image, ImageOps, ImageDraw, ImageFont
import re
import hashlib
from urllib.parse import urlparse

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
    1413545658006110401,  # William/Admin
    1404098545006546954,  # serversorter
    1420817462290681936,  # kd
    1404105470204969000,  # greg
    1404104881098195015   # sazzles
]

AUTO_WARN_SECONDS = 23 * 60 * 60
AUTO_LOCK_SECONDS = 24 * 60 * 60

VOTE_A = "🔴"
VOTE_B = "🔵"
ALLOWED_VOTE_EMOJIS = (VOTE_A, VOTE_B)

STAGE_BY_COUNT = {
    32: "Round of 32",
    16: "Round of 16",
    8:  "Quarter Finals",
    4:  "Semi Finals",
    2:  "Finals"
}

IMAGES_DIR = "wc_images"  # stored in repo root folder

# =========================================================
# DEFAULT DATA
# =========================================================

DEFAULT_DATA = {
    "items": [],
    "item_images": {},     # item -> github path (e.g., wc_images/<file>.png) OR https://...
    "current_round": [],
    "next_round": [],
    "scores": {},
    "running": False,
    "title": "",
    "last_winner": None,
    "last_match": None,    # includes matchup + message ids + prev_result snapshot
    "finished_matches": [],
    "round_stage": "",
    "item_authors": {},    # item -> user_id (str)
    "user_items": {},      # user_id -> item
    "cup_history": []
}

# =========================================================
# GITHUB HELPERS
# =========================================================

def _gh_url(path: str):
    return f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path}"

def _gh_params():
    return {"ref": GITHUB_BRANCH} if GITHUB_BRANCH else None

def _json_url():
    return _gh_url(GITHUB_FILE_PATH)

def _safe_filename(s: str) -> str:
    s = s.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s or "item"

def load_data():
    try:
        r = requests.get(_json_url(), headers=HEADERS, params=_gh_params(), timeout=20)
        if r.status_code == 200:
            content = r.json()
            raw = base64.b64decode(content["content"]).decode()
            data = json.loads(raw) if raw.strip() else DEFAULT_DATA.copy()
            sha = content.get("sha")

            # ensure keys
            for k in DEFAULT_DATA:
                if k not in data:
                    data[k] = DEFAULT_DATA[k]

            # ensure types
            if not isinstance(data.get("items"), list):
                data["items"] = []
            if not isinstance(data.get("item_images"), dict):
                data["item_images"] = {}
            if not isinstance(data.get("item_authors"), dict):
                data["item_authors"] = {}
            if not isinstance(data.get("user_items"), dict):
                data["user_items"] = {}
            if not isinstance(data.get("cup_history"), list):
                data["cup_history"] = []

            return data, sha

        if r.status_code == 404:
            sha = save_data(DEFAULT_DATA.copy(), None)
            return DEFAULT_DATA.copy(), sha

        print(f"[GitHub] load_data unexpected status: {r.status_code} {r.text}")
        return DEFAULT_DATA.copy(), None

    except Exception as e:
        print("[GitHub] load_data error:", e)
        return DEFAULT_DATA.copy(), None

def save_data(data, sha=None):
    try:
        payload = {
            "message": "Update tournament data",
            "content": base64.b64encode(json.dumps(data, indent=4).encode()).decode(),
            "branch": GITHUB_BRANCH
        }
        if sha:
            payload["sha"] = sha

        r = requests.put(_json_url(), headers=HEADERS, data=json.dumps(payload), timeout=20)
        if r.status_code in (200, 201):
            return r.json().get("content", {}).get("sha")

        print(f"[GitHub] save_data unexpected status: {r.status_code} {r.text}")
        return sha
    except Exception as e:
        print("[GitHub] save_data error:", e)
        return sha

def gh_get_file_bytes(path: str) -> bytes | None:
    try:
        r = requests.get(_gh_url(path), headers=HEADERS, params=_gh_params(), timeout=20)
        if r.status_code != 200:
            print(f"[GitHub] gh_get_file_bytes failed {r.status_code}: {r.text}")
            return None
        content = r.json()
        raw = base64.b64decode(content["content"])
        return raw
    except Exception as e:
        print("[GitHub] gh_get_file_bytes error:", e)
        return None

def gh_put_file_bytes(path: str, content_bytes: bytes, sha: str | None = None) -> str | None:
    try:
        payload = {
            "message": f"Add/update {path}",
            "content": base64.b64encode(content_bytes).decode(),
            "branch": GITHUB_BRANCH
        }
        if sha:
            payload["sha"] = sha

        r = requests.put(_gh_url(path), headers=HEADERS, data=json.dumps(payload), timeout=30)
        if r.status_code in (200, 201):
            return r.json().get("content", {}).get("sha")
        print(f"[GitHub] gh_put_file_bytes failed {r.status_code}: {r.text}")
        return None
    except Exception as e:
        print("[GitHub] gh_put_file_bytes error:", e)
        return None

# =========================================================
# IMAGE LOADING (NEW): supports GitHub path OR direct URL
# =========================================================

def _is_http_url(s: str) -> bool:
    if not s or not isinstance(s, str):
        return False
    try:
        u = urlparse(s)
        return u.scheme in ("http", "https") and bool(u.netloc)
    except Exception:
        return False

def load_image_bytes(image_ref: str) -> bytes | None:
    """
    image_ref can be:
      - "wc_images/foo.png" (GitHub repo file)
      - "https://..." direct URL
    """
    if not image_ref or not isinstance(image_ref, str):
        return None

    if _is_http_url(image_ref):
        try:
            r = requests.get(image_ref, timeout=25, headers={"User-Agent": "WorldCupBot/1.0"})
            if r.status_code != 200:
                print(f"[IMG] URL fetch failed {r.status_code}: {image_ref}")
                return None
            return r.content
        except Exception as e:
            print("[IMG] URL fetch error:", e)
            return None

    # otherwise assume repo path
    return gh_get_file_bytes(image_ref)

# =========================================================
# UTILITIES
# =========================================================

def user_allowed(member: discord.Member, allowed_roles):
    return any(role.id in allowed_roles for role in member.roles)

async def count_votes_from_message(guild, channel_id, message_id):
    try:
        channel = guild.get_channel(channel_id)
        if channel is None:
            return 0, 0, {}, {}
        msg = await channel.fetch_message(message_id)
    except Exception:
        return 0, 0, {}, {}

    a_users, b_users = set(), set()
    a_names, b_names = {}, {}

    for reaction in msg.reactions:
        emoji = str(reaction.emoji)
        if emoji not in ALLOWED_VOTE_EMOJIS:
            continue

        try:
            users = [u async for u in reaction.users()]
        except Exception:
            users = []

        for u in users:
            if u.bot:
                continue
            if emoji == VOTE_A:
                a_users.add(u.id)
                a_names[u.id] = u.display_name
            elif emoji == VOTE_B:
                b_users.add(u.id)
                b_names[u.id] = u.display_name

    # if they voted both, count as zero
    dupes = a_users & b_users
    for uid in dupes:
        a_users.discard(uid)
        b_users.discard(uid)
        a_names.pop(uid, None)
        b_names.pop(uid, None)

    return len(a_users), len(b_users), a_names, b_names

def _separator():
    # extra spacing so sections don’t look crushed
    return "\n\n\n━━━━━━━━━━━━━━━━━━\n\n\n"

def _format_voter_list(names: dict[int, str]) -> str:
    if not names:
        return "_No votes yet_"
    return "\n".join([f"• {n}" for n in names.values()])

def _make_status_line(is_locked: bool) -> str:
    return "🔒 **Voting closed**" if is_locked else "⏰ Auto-lock in 24h"

def _try_font(size: int = 36):
    try:
        return ImageFont.truetype("DejaVuSans.ttf", size=size)
    except Exception:
        return ImageFont.load_default()

def build_composite_image_bytes(img_a: bytes, img_b: bytes, label_a: str, label_b: str) -> bytes:
    im_a = Image.open(BytesIO(img_a)).convert("RGB")
    im_b = Image.open(BytesIO(img_b)).convert("RGB")

    target_h = 720

    def resize_to_h(im: Image.Image, h: int):
        w = int(im.width * (h / im.height))
        return im.resize((w, h), Image.LANCZOS)

    im_a = resize_to_h(im_a, target_h)
    im_b = resize_to_h(im_b, target_h)

    min_w = min(im_a.width, im_b.width, 720)

    def center_crop(im: Image.Image, w: int, h: int):
        left = max(0, (im.width - w) // 2)
        top = max(0, (im.height - h) // 2)
        return im.crop((left, top, left + w, top + h))

    im_a = center_crop(im_a, min_w, target_h)
    im_b = center_crop(im_b, min_w, target_h)

    border = 16
    im_a = ImageOps.expand(im_a, border=border, fill=(220, 20, 60))
    im_b = ImageOps.expand(im_b, border=border, fill=(30, 144, 255))

    gap = 12
    out_w = im_a.width + gap + im_b.width
    out_h = max(im_a.height, im_b.height)
    out = Image.new("RGB", (out_w, out_h), (20, 20, 20))
    out.paste(im_a, (0, 0))
    out.paste(im_b, (im_a.width + gap, 0))

    draw = ImageDraw.Draw(out)
    font = _try_font(34)

    pad = 18
    box_h = 92
    y0 = out_h - box_h
    draw.rectangle((0, y0, out_w, out_h), fill=(0, 0, 0))

    left_text = f"{VOTE_A} {label_a}"
    right_text = f"{VOTE_B} {label_b}"

    draw.text((pad, y0 + 22), left_text, font=font, fill=(255, 255, 255))
    rt_w = draw.textlength(right_text, font=font)
    draw.text((out_w - rt_w - pad, y0 + 22), right_text, font=font, fill=(255, 255, 255))

    buf = BytesIO()
    out.save(buf, format="PNG", optimize=True)
    return buf.getvalue()

# =========================================================
# DISCORD CLIENT
# =========================================================

intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.reactions = True

class WorldCupBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self._vote_update_tasks = {}  # message_id -> task

    async def setup_hook(self):
        await self.tree.sync()

client = WorldCupBot()

# =========================================================
# REACTION CLEANUP + VOTE REFRESH
# =========================================================

async def _refresh_vote_embed_for_message(guild: discord.Guild, channel_id: int, message_id: int):
    data, _ = load_data()
    lm = data.get("last_match") or {}
    if not lm:
        return
    if lm.get("locked"):
        return
    if lm.get("channel_id") != channel_id or lm.get("message_id") != message_id:
        return

    try:
        channel = guild.get_channel(channel_id)
        if not channel:
            return
        msg = await channel.fetch_message(message_id)
    except Exception:
        return

    a = lm.get("a")
    b = lm.get("b")
    if not a or not b:
        return

    a_count, b_count, a_names, b_names = await count_votes_from_message(guild, channel_id, message_id)
    prev = lm.get("prev_result")

    emb2 = build_match_embed(
        stage=data.get("round_stage", "Matchup"),
        a=a,
        b=b,
        a_count=a_count,
        b_count=b_count,
        a_names=a_names,
        b_names=b_names,
        locked=False,
        prev_result=prev
    )
    if msg.embeds and msg.embeds[0].image and msg.embeds[0].image.url:
        emb2.set_image(url=msg.embeds[0].image.url)

    try:
        await msg.edit(embed=emb2)
    except Exception:
        pass

@client.event
async def on_reaction_add(reaction: discord.Reaction, user: discord.User):
    if user.bot:
        return

    try:
        data, _ = load_data()
        lm = data.get("last_match") or {}
        if not lm:
            return

        if reaction.message.id != lm.get("message_id"):
            return

        emoji = str(reaction.emoji)
        if emoji not in ALLOWED_VOTE_EMOJIS:
            try:
                await reaction.remove(user)
            except Exception:
                pass
            return

        # refresh votes (debounced-ish)
        await _refresh_vote_embed_for_message(
            guild=reaction.message.guild,
            channel_id=reaction.message.channel.id,
            message_id=reaction.message.id
        )

    except Exception:
        return

@client.event
async def on_reaction_remove(reaction: discord.Reaction, user: discord.User):
    if user.bot:
        return

    try:
        data, _ = load_data()
        lm = data.get("last_match") or {}
        if not lm:
            return
        if reaction.message.id != lm.get("message_id"):
            return
        if str(reaction.emoji) not in ALLOWED_VOTE_EMOJIS:
            return

        await _refresh_vote_embed_for_message(
            guild=reaction.message.guild,
            channel_id=reaction.message.channel.id,
            message_id=reaction.message.id
        )
    except Exception:
        return

# =========================================================
# AUTO LOCK + MATCH POSTING (single embed, single composite image)
# =========================================================

async def _lock_match(guild: discord.Guild, channel: discord.TextChannel, data, sha, reason: str, ping_everyone: bool, reply_msg: discord.Message | None):
    lm = data.get("last_match")
    if not lm or lm.get("locked"):
        return data, sha

    a_votes, b_votes, a_names, b_names = await count_votes_from_message(guild, lm["channel_id"], lm["message_id"])

    lm["locked"] = True
    lm["locked_at"] = int(time.time())
    lm["locked_counts"] = {"a": a_votes, "b": b_votes}
    lm["lock_reason"] = reason

    sha = save_data(data, sha)

    try:
        msg = await channel.fetch_message(lm["message_id"])
        if msg.embeds:
            emb = msg.embeds[0]
            desc = emb.description or ""
            if "🔒 **Voting closed**" not in desc:
                desc = desc + _separator() + "🔒 **Voting closed**"
            new = discord.Embed(title=emb.title, description=desc, color=emb.color)
            if emb.image and emb.image.url:
                new.set_image(url=emb.image.url)
            await msg.edit(embed=new)
    except Exception as e:
        print("Lock edit failed:", e)

    try:
        ping = "@everyone " if ping_everyone else ""
        text = f"{ping}🔒 **Voting is now closed.** ({reason})"
        if reply_msg:
            await reply_msg.reply(text)
        else:
            await channel.send(text)
    except Exception as e:
        print("Lock announce failed:", e)

    return data, sha

async def _schedule_auto_lock(channel: discord.TextChannel, message_id: int):
    try:
        await asyncio.sleep(AUTO_WARN_SECONDS)
        data, sha = load_data()
        lm = data.get("last_match")
        if not lm or lm.get("message_id") != message_id or lm.get("locked"):
            return

        try:
            msg = await channel.fetch_message(message_id)
            await msg.reply("@everyone ⏰ **Voting closes soon!** (auto-lock at 24h)")
        except Exception:
            await channel.send("@everyone ⏰ **Voting closes soon!** (auto-lock at 24h)")

        await asyncio.sleep(max(0, AUTO_LOCK_SECONDS - AUTO_WARN_SECONDS))
        data, sha = load_data()
        lm = data.get("last_match")
        if not lm or lm.get("message_id") != message_id or lm.get("locked"):
            return

        try:
            reply_msg = await channel.fetch_message(message_id)
        except Exception:
            reply_msg = None

        await _lock_match(
            guild=channel.guild,
            channel=channel,
            data=data,
            sha=sha,
            reason="Auto-locked after 24h",
            ping_everyone=True,
            reply_msg=reply_msg
        )
    except Exception as e:
        print("Auto-lock scheduler error:", e)

def build_match_embed(stage: str, a: str, b: str, a_count: int, b_count: int, a_names: dict, b_names: dict,
                      locked: bool, prev_result: dict | None):
    title = f"🎮 {stage}" if stage else "🎮 Matchup"
    parts = []

    if prev_result:
        winner = prev_result.get("winner")
        pa = prev_result.get("a")
        pb = prev_result.get("b")
        av = prev_result.get("a_votes", 0)
        bv = prev_result.get("b_votes", 0)

        prev_block = (
            f"🏆 **Previous Match**\n"
            f"**{winner}** won\n"
            f"{VOTE_A} {pa} — **{av}**   {VOTE_B} {pb} — **{bv}**"
        )
        parts.append(prev_block)

    current_block = (
        f"📦 **Current Match**\n"
        f"{VOTE_A} **{a}** — **{a_count}** votes\n"
        f"{_format_voter_list(a_names)}\n\n"
        f"{VOTE_B} **{b}** — **{b_count}** votes\n"
        f"{_format_voter_list(b_names)}"
    )
    parts.append(current_block)
    parts.append(_make_status_line(locked))

    description = _separator().join(parts)
    emb = discord.Embed(title=title, description=description, color=discord.Color.random())
    return emb

async def post_next_match(channel: discord.TextChannel, data, sha, prev_result: dict | None = None):
    if len(data["current_round"]) < 2:
        return sha

    a = data["current_round"].pop(0)
    b = data["current_round"].pop(0)
    sha = save_data(data, sha)

    img_ref_a = data.get("item_images", {}).get(a)
    img_ref_b = data.get("item_images", {}).get(b)

    if not img_ref_a or not img_ref_b:
        await channel.send(
            f"⚠️ Missing images for matchup:\n- {a}: {img_ref_a}\n- {b}: {img_ref_b}\n"
            f"Fix by re-adding with an upload OR a URL."
        )
        return sha

    bytes_a = load_image_bytes(img_ref_a)
    bytes_b = load_image_bytes(img_ref_b)

    if not bytes_a or not bytes_b:
        await channel.send(
            "⚠️ Could not load matchup images.\n"
            f"- {a}: {img_ref_a}\n- {b}: {img_ref_b}\n"
            "If these are GitHub paths, check token perms + paths.\n"
            "If these are URLs, check they are public and direct."
        )
        return sha

    composite = build_composite_image_bytes(bytes_a, bytes_b, a, b)

    emb = build_match_embed(
        stage=data.get("round_stage", "Matchup"),
        a=a,
        b=b,
        a_count=0,
        b_count=0,
        a_names={},
        b_names={},
        locked=False,
        prev_result=prev_result
    )

    file = discord.File(BytesIO(composite), filename="matchup.png")
    emb.set_image(url="attachment://matchup.png")

    msg = await channel.send(embed=emb, file=file)
    await msg.add_reaction(VOTE_A)
    await msg.add_reaction(VOTE_B)

    data["last_match"] = {
        "a": a,
        "b": b,
        "message_id": msg.id,
        "channel_id": channel.id,
        "locked": False,
        "locked_at": None,
        "locked_counts": None,
        "lock_reason": None,
        "prev_result": prev_result
    }
    sha = save_data(data, sha)

    asyncio.create_task(_schedule_auto_lock(channel, msg.id))
    return sha

# =========================================================
# COMMANDS
# =========================================================

@client.tree.command(name="ping", description="Check the bot is alive")
async def ping(interaction: discord.Interaction):
    await interaction.response.send_message("🏓 Pong!", ephemeral=True)

@client.tree.command(name="addwcitem", description="Add ONE item to the World Cup (upload OR image URL)")
@app_commands.describe(
    item="Item name",
    image="Upload an image (optional if using image_url)",
    image_url="Direct image URL (optional if uploading image)"
)
async def addwcitem(
    interaction: discord.Interaction,
    item: str,
    image: discord.Attachment | None = None,
    image_url: str | None = None
):
    # Public outcome, but defer non-ephemeral so slow GitHub/URL doesn't break the interaction
    await interaction.response.defer(thinking=True)

    data, sha = load_data()
    is_admin = user_allowed(interaction.user, ALLOWED_ROLE_IDS)
    uid = str(interaction.user.id)

    item = item.strip()
    if not item:
        return await interaction.followup.send("⚠️ Item name can’t be empty.", ephemeral=True)

    # hard cap 32 always
    if item not in data.get("items", []) and len(data.get("items", [])) >= 32:
        return await interaction.followup.send("❌ Already at **32** items. Remove one before adding more.", ephemeral=True)

    # non-admin: only 1 item total
    if not is_admin and uid in data.get("user_items", {}):
        return await interaction.followup.send("You can only add **one** item to the World Cup.", ephemeral=True)

    if item in data.get("items", []):
        return await interaction.followup.send("⚠️ That item already exists.", ephemeral=True)

    # Must supply either upload or URL
    if (image is None) and (not image_url):
        return await interaction.followup.send("❌ You must upload an image OR provide an image URL.", ephemeral=True)

    # If URL is supplied, validate format + store URL directly
    stored_image_ref: str | None = None

    if image_url:
        image_url = image_url.strip()
        if not _is_http_url(image_url):
            return await interaction.followup.send("❌ That image_url isn’t a valid http(s) URL.", ephemeral=True)

        # test fetch now so you don’t start a tournament with dead links
        test_bytes = load_image_bytes(image_url)
        if not test_bytes:
            return await interaction.followup.send("❌ I couldn’t download that URL. Use a public direct image link.", ephemeral=True)

        # store URL directly in JSON
        stored_image_ref = image_url

    # If an upload is supplied, store it to GitHub (existing behaviour)
    if image is not None:
        if not (image.content_type or "").startswith("image/"):
            return await interaction.followup.send("❌ That upload isn’t an image. Please upload a PNG/JPG.", ephemeral=True)

        try:
            img_bytes = await image.read()
        except Exception:
            return await interaction.followup.send("❌ I couldn’t read that image upload. Try again.", ephemeral=True)

        safe = _safe_filename(item)
        short = hashlib.sha1(f"{item}|{uid}|{time.time()}".encode()).hexdigest()[:10]
        img_path = f"{IMAGES_DIR}/{safe}_{short}.png"

        # normalize to PNG
        try:
            im = Image.open(BytesIO(img_bytes)).convert("RGB")
            out = BytesIO()
            im.save(out, format="PNG", optimize=True)
            img_bytes = out.getvalue()
        except Exception:
            pass

        img_sha = gh_put_file_bytes(img_path, img_bytes, sha=None)
        if not img_sha:
            return await interaction.followup.send("❌ Failed to store the uploaded image to GitHub. Check PAT scopes + repo access.", ephemeral=True)

        stored_image_ref = img_path

    # Final check
    if not stored_image_ref:
        return await interaction.followup.send("❌ No image reference could be stored. Try again.", ephemeral=True)

    # store item
    data.setdefault("items", [])
    data.setdefault("scores", {})
    data.setdefault("item_images", {})
    data.setdefault("item_authors", {})
    data.setdefault("user_items", {})

    data["items"].append(item)
    data["scores"].setdefault(item, 0)
    data["item_images"][item] = stored_image_ref
    data["item_authors"][item] = uid
    if not is_admin:
        data["user_items"][uid] = item

    sha = save_data(data, sha)

    await interaction.followup.send(f"✅ Added: **{item}**", ephemeral=False)

@client.tree.command(name="removewcitem", description="Remove item(s) (staff only, case-insensitive)")
@app_commands.describe(items="Comma-separated list")
async def removewcitem(interaction: discord.Interaction, items: str):
    await interaction.response.defer(thinking=True, ephemeral=True)

    if not user_allowed(interaction.user, ALLOWED_ROLE_IDS):
        return await interaction.followup.send("❌ No permission.", ephemeral=True)

    data, sha = load_data()
    removed = []

    lower_map = {i.lower(): i for i in data.get("items", [])}

    for raw in [x.strip() for x in items.split(",") if x.strip()]:
        key = raw.lower()
        if key in lower_map:
            original = lower_map[key]
            data["items"].remove(original)
            data.get("scores", {}).pop(original, None)

            author_id = data.get("item_authors", {}).pop(original, None)
            data.get("item_images", {}).pop(original, None)
            if author_id and data.get("user_items", {}).get(str(author_id)) == original:
                data["user_items"].pop(str(author_id), None)

            removed.append(original)

    sha = save_data(data, sha)

    if removed:
        return await interaction.followup.send(f"✅ Removed: {', '.join(removed)}", ephemeral=True)
    return await interaction.followup.send("⚠️ Nothing removed.", ephemeral=True)

@client.tree.command(name="listwcitems", description="List all items (paginated, public)")
async def listwcitems(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)  # NOT EPHEMERAL

    data, _ = load_data()
    items = data.get("items", [])

    if not items:
        return await interaction.followup.send("No items added yet.", ephemeral=False)

    pages = [items[i:i+10] for i in range(0, len(items), 10)]
    total_pages = len(pages)
    page = 0

    def make_embed(p: int):
        e = discord.Embed(
            title="📋 World Cup Items",
            description="\n".join(f"{(p*10)+i+1}. {v}" for i, v in enumerate(pages[p])),
            color=discord.Color.blue()
        )
        e.set_footer(text=f"Page {p+1}/{total_pages}")
        return e

    msg = await interaction.followup.send(embed=make_embed(0), wait=True, ephemeral=False)

    if total_pages > 1:
        await msg.add_reaction("⬅️")
        await msg.add_reaction("➡️")

    def check(r, u):
        return u == interaction.user and r.message.id == msg.id and str(r.emoji) in ("⬅️", "➡️")

    while total_pages > 1:
        try:
            r, u = await interaction.client.wait_for("reaction_add", timeout=60, check=check)
            if str(r.emoji) == "➡️" and page < total_pages - 1:
                page += 1
            elif str(r.emoji) == "⬅️" and page > 0:
                page -= 1
            await msg.edit(embed=make_embed(page))
            try:
                await msg.remove_reaction(r.emoji, u)
            except Exception:
                pass
        except asyncio.TimeoutError:
            break

@client.tree.command(name="closematch", description="Lock the current match (staff only)")
async def closematch(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True, ephemeral=True)

    if not user_allowed(interaction.user, ALLOWED_ROLE_IDS):
        return await interaction.followup.send("❌ No permission.", ephemeral=True)

    data, sha = load_data()
    lm = data.get("last_match")
    if not lm:
        return await interaction.followup.send("⚠️ No active match.", ephemeral=True)

    try:
        reply_msg = await interaction.channel.fetch_message(lm["message_id"])
    except Exception:
        reply_msg = None

    await _lock_match(
        guild=interaction.guild,
        channel=interaction.channel,
        data=data,
        sha=sha,
        reason=f"Closed by {interaction.user.display_name}",
        ping_everyone=False,
        reply_msg=reply_msg
    )

    return await interaction.followup.send("🔒 Match locked.", ephemeral=True)

@client.tree.command(name="startwc", description="Start the World Cup (staff only, requires 32 items)")
@app_commands.describe(title="World Cup title")
async def startwc(interaction: discord.Interaction, title: str):
    await interaction.response.defer(thinking=True, ephemeral=True)

    if not user_allowed(interaction.user, ALLOWED_ROLE_IDS):
        return await interaction.followup.send("❌ No permission.", ephemeral=True)

    data, sha = load_data()

    if data.get("running"):
        return await interaction.followup.send("❌ Already running.", ephemeral=True)

    if len(data.get("items", [])) != 32:
        return await interaction.followup.send("❌ Must have **exactly 32** items to start.", ephemeral=True)

    # Accept BOTH GitHub paths and URLs — just ensure it exists and can be fetched
    missing_imgs = []
    for it in data["items"]:
        ref = data.get("item_images", {}).get(it)
        if not ref:
            missing_imgs.append(it)
            continue
        if load_image_bytes(ref) is None:
            missing_imgs.append(it)

    if missing_imgs:
        return await interaction.followup.send(
            "❌ These items have missing/broken images:\n" +
            "\n".join([f"• {x}" for x in missing_imgs[:15]]) +
            ("\n…and more" if len(missing_imgs) > 15 else ""),
            ephemeral=True
        )

    data["title"] = title
    data["current_round"] = data["items"].copy()
    random.shuffle(data["current_round"])
    data["next_round"] = []
    data["finished_matches"] = []
    data["last_match"] = None
    data["last_winner"] = None
    data["running"] = True
    data["round_stage"] = STAGE_BY_COUNT.get(32, "Round of 32")

    sha = save_data(data, sha)

    await interaction.channel.send(f"@everyone The World Cup of **{title}** is starting — cast your votes! 🏆")
    await post_next_match(interaction.channel, data, sha, prev_result=None)

    return await interaction.followup.send("✅ Tournament started.", ephemeral=True)

@client.tree.command(name="nextwcround", description="Process the current match and/or advance rounds (staff only)")
async def nextwcround(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True, ephemeral=True)

    if not user_allowed(interaction.user, ALLOWED_ROLE_IDS):
        return await interaction.followup.send("❌ No permission.", ephemeral=True)

    data, sha = load_data()
    if not data.get("running"):
        return await interaction.followup.send("❌ No active tournament.", ephemeral=True)

    guild = interaction.guild

    if data.get("last_match"):
        lm = data["last_match"]

        if lm.get("locked") and isinstance(lm.get("locked_counts"), dict):
            a_votes = int(lm["locked_counts"].get("a", 0))
            b_votes = int(lm["locked_counts"].get("b", 0))
        else:
            a_votes, b_votes, _, _ = await count_votes_from_message(
                guild, lm["channel_id"], lm["message_id"]
            )

        a = lm["a"]
        b = lm["b"]

        if a_votes > b_votes:
            winner = a
        elif b_votes > a_votes:
            winner = b
        else:
            winner = random.choice([a, b])

        prev_result = {"a": a, "b": b, "winner": winner, "a_votes": a_votes, "b_votes": b_votes}

        data["finished_matches"].append(prev_result)
        data["next_round"].append(winner)
        data["scores"][winner] = data["scores"].get(winner, 0) + 1
        data["last_match"] = None
        data["last_winner"] = winner
        sha = save_data(data, sha)

        if data.get("round_stage") == "Finals" and not data.get("current_round"):
            return await interaction.followup.send("✔ Final match processed.\nUse `/endwc` to announce the winner.", ephemeral=True)

        await interaction.channel.send(
            f"@everyone The next fixture in the World Cup of **{data.get('title','')}** is ready — vote below! 🗳️"
        )

        if len(data["current_round"]) >= 2:
            await post_next_match(interaction.channel, data, sha, prev_result=prev_result)
            return await interaction.followup.send("✔ Match processed.", ephemeral=True)

        return await interaction.followup.send("✔ Match processed. Run again to advance rounds.", ephemeral=True)

    if not data.get("current_round") and data.get("next_round"):
        prev_stage = data.get("round_stage", "Round")

        data["current_round"] = data["next_round"].copy()
        data["next_round"] = []

        new_len = len(data["current_round"])
        data["round_stage"] = STAGE_BY_COUNT.get(new_len, f"{new_len}-items round")
        sha = save_data(data, sha)

        embed = discord.Embed(
            title=f"✅ {prev_stage} complete!",
            description=f"Now entering **{data['round_stage']}**.",
            color=discord.Color.purple()
        )
        await interaction.channel.send(embed=embed)

        if new_len >= 2:
            await post_next_match(interaction.channel, data, sha, prev_result=None)
            return await interaction.followup.send("🔁 Next round posted.", ephemeral=True)

        return await interaction.followup.send("⚠️ Not enough items to continue.", ephemeral=True)

    return await interaction.followup.send("⚠️ Nothing to process.", ephemeral=True)

@client.tree.command(name="scoreboard", description="Show tournament progress (public)")
async def scoreboard(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)  # NOT EPHEMERAL

    data, _ = load_data()

    finished = data.get("finished_matches", [])
    current = data.get("last_match")
    remaining = data.get("current_round", [])

    finished_lines = []
    for i, f in enumerate(finished):
        finished_lines.append(
            f"{i+1}. {f['a']} vs {f['b']} → **{f['winner']}** "
            f"({VOTE_A} {f['a_votes']} | {VOTE_B} {f['b_votes']})"
        )
    if not finished_lines:
        finished_lines = ["No matches played yet."]

    finished_pages = [finished_lines[i:i+10] for i in range(0, len(finished_lines), 10)]

    if current:
        locked = " 🔒" if current.get("locked") else ""
        current_line = f"{current['a']} vs {current['b']} (voting now){locked}"
    else:
        current_line = "None"

    upcoming_lines = []
    for i in range(0, len(remaining), 2):
        if i + 1 < len(remaining):
            upcoming_lines.append(f"• {remaining[i]} vs {remaining[i+1]}")
        else:
            upcoming_lines.append(f"• {remaining[i]} (auto-advance)")
    if not upcoming_lines:
        upcoming_lines = ["None"]

    upcoming_chunks = []
    chunk = []
    length = 0
    for line in upcoming_lines:
        if length + len(line) + 1 > 900:
            upcoming_chunks.append(chunk)
            chunk = []
            length = 0
        chunk.append(line)
        length += len(line) + 1
    if chunk:
        upcoming_chunks.append(chunk)

    page = 0
    total_pages = max(len(finished_pages), len(upcoming_chunks))

    def make_embed(p: int):
        emb = discord.Embed(title="🏆 World Cup Scoreboard", color=discord.Color.teal())
        emb.add_field(name="Tournament", value=data.get("title") or "No title", inline=False)
        emb.add_field(name="Stage", value=data.get("round_stage") or "N/A", inline=False)
        emb.add_field(name="Current Match", value=current_line, inline=False)
        emb.add_field(
            name="Finished Matches",
            value="\n".join(finished_pages[min(p, len(finished_pages)-1)]),
            inline=False
        )
        emb.add_field(
            name="Upcoming Matchups",
            value="\n".join(upcoming_chunks[min(p, len(upcoming_chunks)-1)]),
            inline=False
        )
        emb.set_footer(text=f"Page {p+1}/{total_pages}")
        return emb

    msg = await interaction.followup.send(embed=make_embed(0), wait=True, ephemeral=False)

    if total_pages > 1:
        await msg.add_reaction("⬅️")
        await msg.add_reaction("➡️")

    def check(r, u):
        return u == interaction.user and r.message.id == msg.id and str(r.emoji) in ("⬅️", "➡️")

    while total_pages > 1:
        try:
            r, u = await interaction.client.wait_for("reaction_add", timeout=60, check=check)
            if str(r.emoji) == "➡️" and page < total_pages - 1:
                page += 1
            elif str(r.emoji) == "⬅️" and page > 0:
                page -= 1
            await msg.edit(embed=make_embed(page))
            try:
                await msg.remove_reaction(r.emoji, u)
            except Exception:
                pass
        except asyncio.TimeoutError:
            break

@client.tree.command(name="resetwc", description="Reset the tournament (staff only). Past cup history is kept.")
async def resetwc(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True, ephemeral=True)

    if not user_allowed(interaction.user, ALLOWED_ROLE_IDS):
        return await interaction.followup.send("❌ No permission.", ephemeral=True)

    data, sha = load_data()

    history = data.get("cup_history", [])
    fresh = DEFAULT_DATA.copy()
    fresh["cup_history"] = history

    save_data(fresh, sha)

    return await interaction.followup.send(
        "🔄 Reset complete.\n"
        "• All items deleted\n"
        "• All votes cleared\n"
        "• Tournament stopped\n"
        "• History preserved",
        ephemeral=True
    )

@client.tree.command(name="endwc", description="Announce the winner & end the tournament (staff only) + save history")
async def endwc(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True, ephemeral=True)

    data, sha = load_data()

    if not user_allowed(interaction.user, ALLOWED_ROLE_IDS):
        return await interaction.followup.send("❌ No permission.", ephemeral=True)

    if not data.get("running"):
        return await interaction.followup.send("❌ No active tournament.", ephemeral=True)

    winner = data.get("last_winner")
    if not winner:
        return await interaction.followup.send("⚠️ No winner recorded yet.", ephemeral=True)

    author_id = data.get("item_authors", {}).get(winner)
    added_by_text = f"<@{author_id}>" if author_id else "Unknown"

    entry = {"title": data.get("title") or "Untitled", "winner": winner, "author_id": author_id, "timestamp": int(time.time())}
    data.setdefault("cup_history", [])
    data["cup_history"].append(entry)

    await interaction.channel.send("@everyone We have a World Cup Winner‼️🎉🏆")

    embed = discord.Embed(
        title="🎉 World Cup Winner!",
        description=(f"🏆 **{winner}** wins the World Cup of **{data.get('title')}**!\n\n✨ Added by: {added_by_text}"),
        color=discord.Color.green()
    )
    embed.set_image(url="https://cdn.discordapp.com/attachments/1444274467864838207/1449046416453271633/IMG_8499.gif")
    await interaction.channel.send(embed=embed)

    data["running"] = False
    save_data(data, sha)

    return await interaction.followup.send("✔ Winner announced + saved to history.", ephemeral=True)

@client.tree.command(name="cuphistory", description="View past World Cups (public, paginated)")
async def cuphistory(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)  # NOT EPHEMERAL

    data, _ = load_data()
    hist = data.get("cup_history", [])
    if not hist:
        return await interaction.followup.send("No history yet.", ephemeral=False)

    hist = list(reversed(hist))
    pages = [hist[i:i+5] for i in range(0, len(hist), 5)]
    page = 0
    total = len(pages)

    def make_embed(p: int):
        e = discord.Embed(title="📜 World Cup History", color=discord.Color.blurple())
        for h in pages[p]:
            title = h.get("title") or "Untitled"
            winner = h.get("winner") or "Unknown"
            author = h.get("author_id")
            author_txt = f"<@{author}>" if author else "Unknown"
            ts = h.get("timestamp")
            when = f"<t:{int(ts)}:D>" if ts else "Unknown date"
            e.add_field(name=f"{title}", value=f"🏆 **{winner}**\n✨ Added by: {author_txt}\n🕒 {when}", inline=False)
        e.set_footer(text=f"Page {p+1}/{total}")
        return e

    msg = await interaction.followup.send(embed=make_embed(0), wait=True, ephemeral=False)

    if total > 1:
        await msg.add_reaction("⬅️")
        await msg.add_reaction("➡️")

    def check(r, u):
        return u == interaction.user and r.message.id == msg.id and str(r.emoji) in ("⬅️", "➡️")

    while total > 1:
        try:
            r, u = await interaction.client.wait_for("reaction_add", timeout=60, check=check)
            if str(r.emoji) == "➡️" and page < total - 1:
                page += 1
            elif str(r.emoji) == "⬅️" and page > 0:
                page -= 1
            await msg.edit(embed=make_embed(page))
            try:
                await msg.remove_reaction(r.emoji, u)
            except Exception:
                pass
        except asyncio.TimeoutError:
            break

@client.tree.command(name="deletehistory", description="Delete a cup from history by exact title (staff only)")
@app_commands.describe(title="Exact title to delete")
async def deletehistory(interaction: discord.Interaction, title: str):
    await interaction.response.defer(thinking=True, ephemeral=True)

    if not user_allowed(interaction.user, ALLOWED_ROLE_IDS):
        return await interaction.followup.send("❌ No permission.", ephemeral=True)

    data, sha = load_data()
    before = len(data.get("cup_history", []))

    data["cup_history"] = [h for h in data.get("cup_history", []) if (h.get("title") or "") != title]

    if len(data["cup_history"]) == before:
        return await interaction.followup.send("⚠️ Not found.", ephemeral=True)

    save_data(data, sha)
    return await interaction.followup.send("🗑 Deleted.", ephemeral=True)

@client.tree.command(name="authorleaderboard", description="Leaderboard by who added items (public)")
async def authorleaderboard(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)  # NOT EPHEMERAL

    data, _ = load_data()
    scores = data.get("scores", {})
    item_authors = data.get("item_authors", {})

    author_points = {}
    for item, pts in scores.items():
        aid = item_authors.get(item)
        if not aid:
            continue
        author_points[aid] = author_points.get(aid, 0) + int(pts)

    if not author_points:
        return await interaction.followup.send("No author scores yet.", ephemeral=False)

    rows = sorted(author_points.items(), key=lambda x: x[1], reverse=True)
    lines = [f"{i}. <@{aid}> — **{pts}**" for i, (aid, pts) in enumerate(rows[:25], start=1)]

    embed = discord.Embed(title="🏅 Author Leaderboard", description="\n".join(lines), color=discord.Color.gold())
    embed.set_footer(text="Points are based on World Cup win counts (scores).")

    return await interaction.followup.send(embed=embed, ephemeral=False)

@client.tree.command(name="wchelp", description="Help menu")
async def wchelp(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True, ephemeral=True)

    embed = discord.Embed(title="📝 World Cup Help", color=discord.Color.blue())
    embed.add_field(name="/addwcitem", value="Add 1 item with an upload OR an image_url. Non-staff can only add one item total.", inline=False)
    embed.add_field(name="/removewcitem", value="Remove item(s) (staff only)", inline=False)
    embed.add_field(name="/listwcitems", value="List items (public, paginated)", inline=False)
    embed.add_field(name="/startwc", value="Start tournament (staff only, needs exactly 32 items)", inline=False)
    embed.add_field(name="/closematch", value="Lock current match (staff only)", inline=False)
    embed.add_field(name="/nextwcround", value="Process match / advance rounds (staff only). Run twice between rounds.", inline=False)
    embed.add_field(name="/scoreboard", value="View progress (public)", inline=False)
    embed.add_field(name="/resetwc", value="Reset tournament (staff only) — items deleted, history kept", inline=False)
    embed.add_field(name="/endwc", value="Announce final winner (staff only) + save history", inline=False)
    embed.add_field(name="/cuphistory", value="View past cups (public)", inline=False)
    embed.add_field(name="/deletehistory", value="Delete history entry by title (staff only)", inline=False)
    embed.add_field(name="/authorleaderboard", value="Leaderboard by item author (public)", inline=False)

    return await interaction.followup.send(embed=embed, ephemeral=True)

# =========================================================
# OPTIONAL: SCHEDULED TASKS LOOP (kept minimal)
# =========================================================

@tasks.loop(minutes=1)
async def scheduled_tasks():
    now = discord.utils.utcnow().astimezone(UK_TZ)
    _ = now

# =========================================================
# FLASK KEEP-ALIVE (Render)
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
# START
# =========================================================

@client.event
async def on_ready():
    print(f"Logged in as {client.user} (ID: {client.user.id})")
    print(f"[Config] Repo={GITHUB_REPO} Path={GITHUB_FILE_PATH} Branch={GITHUB_BRANCH}")
    if not GITHUB_TOKEN:
        print("[Config] WARNING: No GitHub token found in env (GITHUB_TOKEN/WC_GITHUB_TOKEN/WC_TOKEN).")
    if not scheduled_tasks.is_running():
        scheduled_tasks.start()

client.run(TOKEN)