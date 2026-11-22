"""
RSS Release Bot for Discord.

Watches any number of RSS feeds and posts every new entry to a Discord channel
as a rich embed — title, link, cover image and timestamp — with an optional
role ping. Feeds aren't hardcoded: add as many as you want at runtime with
/rss-create, no restart needed.

Features
- Add/list/remove feeds at runtime with /rss-create, /rss-list, /rss-remove.
- Polls every configured feed on an interval you set.
- Posts new entries oldest-first, with title, link, cover image and timestamp.
- Pings a shared "update" role, if one is configured.
- Remembers what it has already posted, per feed, so restarts don't re-post.
- A first check of a feed (or a feed that just changed) is recorded quietly —
  it won't flood the channel with the whole back catalog.
- A sudden burst of "new" items is treated as the feed changing, not a wave
  of real releases: everything is marked seen and nothing is posted.
- !checkrss previews the newest entry of each feed without pinging anyone.

.env (see .env.example)
  RSS_BOT_TOKEN       Discord bot token (required)
  RSS_CHANNEL_ID      Channel to post releases in (required)
  CHECK_INTERVAL      Seconds between feed checks (default 120)
  SITE_ICON_URL       Small logo shown in the embed footer
  ROLE_UPDATE         Role ID pinged on every release (optional)

Feeds themselves are NOT set in .env — add them with /rss-create once the bot
is running. They're stored in rss_feeds.json, next to this file.

Needs: discord.py 2.0+, feedparser, requests, python-dotenv.
"""

import asyncio
import html
import json
import os
import re
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

import discord
from discord import app_commands
import feedparser
import requests
from discord.ext import tasks
from dotenv import load_dotenv

load_dotenv()

# ==================== CONFIGURATION ====================

DISCORD_TOKEN  = os.getenv("RSS_BOT_TOKEN", "YOUR_RSS_BOT_TOKEN_HERE")
CHANNEL_ID     = int(os.getenv("RSS_CHANNEL_ID", "0") or 0)
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "120"))

SITE_ICON_URL  = os.getenv("SITE_ICON_URL", "")
ROLE_UPDATE    = os.getenv("ROLE_UPDATE", "")

FEEDS_FILE        = "rss_feeds.json"          # {"name": "url", ...}, in the order they were added
CACHE_FILE        = "rss_last_entries.json"   # {"name": ["link", ...], ...}
ENTRIES_PER_CHECK = 20                                   # newest items looked at per feed per check
FLOOD_LIMIT       = int(os.getenv("FLOOD_LIMIT", "10"))  # more "new" items than this = feed changed, not real releases
SEEN_KEEP         = 3000                                 # remembered items per feed

# Whether the bot has been started by an admin with !rssstart
rss_started = False

HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# ==================== BOT ====================

intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True


class RSSBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        await self.tree.sync()
        print("✅ Slash commands synced!")


bot = RSSBot()

# ==================== STORAGE ====================


def _read_json(path, default):
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError) as e:
            print(f"⚠️ Couldn't read {path}: {e}")
    return default


def _write_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def load_feeds() -> dict:
    data = _read_json(FEEDS_FILE, {})
    return data if isinstance(data, dict) else {}


def save_feeds(data: dict) -> None:
    _write_json(FEEDS_FILE, data)


def load_cache() -> dict:
    data = _read_json(CACHE_FILE, {})
    return data if isinstance(data, dict) else {}


def save_cache(data: dict) -> None:
    _write_json(CACHE_FILE, data)

# ==================== TEXT HELPERS ====================


def truncate(text: str, limit: int = 256) -> str:
    """Truncate string to Discord embed limit, ending with '...' if cut."""
    text = text or ""
    return text if len(text) <= limit else text[: limit - 3] + "..."


def clean_text(s) -> str:
    """HTML entities and tags out, spaces tidied."""
    s = re.sub(r"<[^>]+>", "", str(s or ""))
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def norm_link(url: str) -> str:
    """Link without scheme, query, fragment or trailing slash (so the same entry == the same text)."""
    p = urlparse((url or "").strip())
    return (p.netloc.lower() + p.path.rstrip("/")).lower()


def norm_name(name: str) -> str:
    """Feed name for lookups/duplicate checks: lowercase, trimmed."""
    return re.sub(r"\s+", " ", (name or "").strip()).lower()


def role_ids(*ids) -> list:
    """Only real role IDs (skips blank/placeholder values), no repeats, order kept."""
    out = []
    for rid in ids:
        rid = str(rid or "").strip()
        if rid.isdigit() and rid not in out:
            out.append(rid)
    return out

# ==================== RSS FUNCTIONS ====================


def http_get(url: str):
    """Plain GET with a browser-like User-Agent. None on failure."""
    try:
        return requests.get(url, headers=HTTP_HEADERS, timeout=15)
    except requests.RequestException as e:
        print(f"⚠️ HTTP error fetching {url}: {e}")
        return None


def _cover_of(entry, base: str):
    """Best-effort cover image URL from an RSS entry (media thumbnail/content, then enclosures)."""
    url = None
    for key in ("media_thumbnail", "media_content"):
        val = entry.get(key)
        if isinstance(val, list) and val:
            url = val[0].get("url")
        elif isinstance(val, dict):
            url = val.get("url")
        if url:
            break
    if not url:
        for enc in entry.get("enclosures") or []:
            if str(enc.get("type", "")).startswith("image/") and enc.get("href"):
                url = enc["href"]
                break
    if url and not url.startswith("http"):
        url = urljoin(base, url)
    return url


def to_release(entry) -> dict:
    """One feed item → everything a release post needs."""
    link = (entry.get("link") or "").strip()
    title = clean_text(entry.get("title"))

    published = datetime.now(timezone.utc)
    if entry.get("published_parsed"):
        published = datetime(*entry.published_parsed[:6], tzinfo=timezone.utc)

    return {
        "link": link,
        "title": title,
        "cover": _cover_of(entry, link or ""),
        "published": published,
    }


def fetch_releases(url: str):
    """Fetch a feed → releases, newest first. None when the feed couldn't be read."""
    try:
        res = http_get(url)
        if res is None:
            return None
        if res.status_code != 200:
            print(f"⚠️ RSS error ({url}): HTTP {res.status_code}")
            return None
        feed = feedparser.parse(res.content)
        return [to_release(e) for e in feed.entries if e.get("link")]
    except Exception as e:
        print(f"⚠️ RSS error ({url}): {e}")
        return None


def seen_set(lists) -> set:
    """Saved links → one set."""
    out = set()
    for lst in lists:
        for link in lst or []:
            out.add(norm_link(link))
    return out


def remember(lst: list, releases) -> list:
    have = set(lst)
    for r in releases:
        key = norm_link(r["link"])
        if key not in have:
            lst.append(key)
            have.add(key)
    return lst[-SEEN_KEEP:]

# ==================== RELEASE POST ====================


def build_release_message(name: str, r: dict):
    """content (the ping), embed for one release."""
    ping = " ".join(f"<@&{rid}>" for rid in role_ids(ROLE_UPDATE))
    content = f"{ping} 🎉 **New Release!** 🎉".strip()

    embed = discord.Embed(
        title=truncate(r["title"]),
        url=r["link"],
        description=f"📡 **{name}**",
        color=0x5865F2,
        timestamp=r["published"],
    )
    if r.get("cover"):
        embed.set_thumbnail(url=r["cover"])
    embed.set_footer(text="RSS Release Bot", icon_url=SITE_ICON_URL or None)
    return content, embed


async def post_release(channel, name: str, r: dict, ping: bool = True):
    content, embed = build_release_message(name, r)
    mentions = discord.AllowedMentions(roles=True, users=False, everyone=False) if ping else discord.AllowedMentions.none()
    if not ping:
        content = "🧪 *Preview — nobody was pinged*\n" + content
    await channel.send(content=content, embed=embed, allowed_mentions=mentions)


async def get_channel(channel_id: int):
    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except Exception as e:
            print(f"[DEBUG] fetch_channel error ({channel_id}): {e}")
            return None
    return channel


async def check_feeds(feeds: dict, cache: dict, channel) -> dict:
    """Post what's new in each feed; returns the updated cache."""
    seen_all = seen_set(cache.values())
    for name, url in feeds.items():
        releases = await asyncio.to_thread(fetch_releases, url)
        if not releases:
            print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] ⚠️ No entries: {name} ({url})")
            continue
        current = releases[:ENTRIES_PER_CHECK]
        mine = cache.setdefault(name, [])

        if not seen_all and not mine:
            # First check of this feed: remember what's there, don't post old entries.
            cache[name] = remember(mine, current)
            print(f"📥 First check of {name}: {len(current)} entries marked as seen (not posted).")
            continue

        new = [r for r in current if norm_link(r["link"]) not in seen_all]
        if len(new) > FLOOD_LIMIT:
            cache[name] = remember(mine, current)
            print(f"⚠️ {len(new)} 'new' entries at once in {name} — looks like the feed changed. Marked seen, nothing posted.")
            continue

        for r in reversed(new):  # oldest first
            await post_release(channel, name, r)
            print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] ✅ Sent [{name}]: {r['title']}")
            await asyncio.sleep(1.5)
        cache[name] = remember(mine, current)
        seen_all |= set(cache[name])
    return cache

# ==================== RSS MONITORING TASK ====================


@tasks.loop(seconds=CHECK_INTERVAL)
async def monitor_rss():
    try:
        feeds = load_feeds()
        if not feeds:
            return
        channel = await get_channel(CHANNEL_ID)
        if not channel:
            print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] ❌ Channel not found!")
            return
        cache = await check_feeds(feeds, load_cache(), channel)
        save_cache(cache)
        print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] 🔍 RSS check complete")
    except Exception as e:
        import traceback
        print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] ❌ Error: {e}")
        print(traceback.format_exc())


@monitor_rss.before_loop
async def _wait_ready():
    await bot.wait_until_ready()

# ==================== SLASH COMMANDS ====================


def is_admin(interaction: discord.Interaction) -> bool:
    perms = getattr(interaction.user, "guild_permissions", None)
    return bool(perms and perms.administrator)


@bot.tree.command(name="rss-create", description="Add a new RSS feed for the bot to watch")
@app_commands.describe(name="A short name for this feed (shown in posts)", url="The feed's URL")
@app_commands.default_permissions(administrator=True)
async def rss_create(interaction: discord.Interaction, name: str, url: str):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ Administrator permission required!", ephemeral=True)
        return

    name = name.strip()
    url = url.strip()
    if not name or not url:
        await interaction.response.send_message("❌ Both a name and a URL are required.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    feeds = load_feeds()
    if any(norm_name(n) == norm_name(name) for n in feeds):
        await interaction.followup.send(f"⚠️ A feed named **{name}** already exists. Use `/rss-remove` first if you want to replace it.", ephemeral=True)
        return

    releases = await asyncio.to_thread(fetch_releases, url)
    if releases is None:
        await interaction.followup.send("❌ Couldn't read that URL as an RSS feed. Double check it and try again.", ephemeral=True)
        return

    feeds[name] = url
    save_feeds(feeds)

    # Mark its current entries as seen so the first check doesn't flood the channel.
    cache = load_cache()
    cache[name] = remember(cache.get(name, []), releases[:ENTRIES_PER_CHECK])
    save_cache(cache)

    await interaction.followup.send(
        f"✅ Added feed **{name}**\n`{url}`\n\nFound {len(releases)} current entries (marked as seen — only new ones from now on will post)."
        + ("" if rss_started else "\n\n⚠️ RSS monitoring isn't running yet — an admin needs to run `!rssstart`."),
        ephemeral=True,
    )


@bot.tree.command(name="rss-list", description="Show every feed the bot is watching")
async def rss_list(interaction: discord.Interaction):
    feeds = load_feeds()
    if not feeds:
        await interaction.response.send_message("No feeds configured yet. Use `/rss-create` to add one.", ephemeral=True)
        return
    lines = [f"**{name}**\n`{url}`" for name, url in feeds.items()]
    embed = discord.Embed(title=f"📡 Configured Feeds ({len(feeds)})", description="\n\n".join(lines), color=0x5865F2)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="rss-remove", description="Stop watching a feed")
@app_commands.describe(name="The feed's name, as shown in /rss-list")
@app_commands.default_permissions(administrator=True)
async def rss_remove(interaction: discord.Interaction, name: str):
    if not is_admin(interaction):
        await interaction.response.send_message("❌ Administrator permission required!", ephemeral=True)
        return

    feeds = load_feeds()
    match = next((n for n in feeds if norm_name(n) == norm_name(name)), None)
    if not match:
        await interaction.response.send_message(f"❌ No feed named **{name}**. Check `/rss-list` for the exact name.", ephemeral=True)
        return

    del feeds[match]
    save_feeds(feeds)

    cache = load_cache()
    if match in cache:
        del cache[match]
        save_cache(cache)

    await interaction.response.send_message(f"🗑️ Removed feed **{match}**.", ephemeral=True)


@rss_remove.autocomplete("name")
async def rss_remove_autocomplete(interaction: discord.Interaction, current: str):
    """Suggest existing feed names while typing /rss-remove."""
    q = norm_name(current)
    names = [n for n in load_feeds() if q in norm_name(n)]
    return [app_commands.Choice(name=n, value=n) for n in names[:25]]

# ==================== TEXT COMMANDS ====================


def is_admin_msg(message) -> bool:
    return bool(getattr(message.author, "guild_permissions", None) and message.author.guild_permissions.administrator)


@bot.event
async def on_message(message):
    global rss_started

    if message.author == bot.user:
        return

    content_lower = message.content.lower()

    # ── !rssstart ────────────────────────────────────────────────────────────
    if content_lower == "!rssstart":
        if not is_admin_msg(message):
            await message.channel.send("❌ Admin only!")
            return
        if rss_started:
            await message.channel.send("⚠️ RSS monitoring is already running!")
            return
        if not load_feeds():
            await message.channel.send("❌ No feeds configured yet. Use `/rss-create` to add one first.")
            return

        rss_started = True
        await bot.wait_until_ready()
        if monitor_rss.is_running():
            monitor_rss.cancel()
            await asyncio.sleep(1)
        monitor_rss.start()
        feeds = "\n".join(f"📡 **{n}**: `{u}`" for n, u in load_feeds().items())
        await message.channel.send(
            "🚀 **RSS monitoring started!**\n"
            f"{feeds}\n"
            f"⏱️ Checking every `{CHECK_INTERVAL}` seconds."
        )

    # ── !rssstop ─────────────────────────────────────────────────────────────
    elif content_lower == "!rssstop":
        if not is_admin_msg(message):
            await message.channel.send("❌ Admin only!")
            return
        if not rss_started:
            await message.channel.send("⚠️ RSS monitoring is not running.")
            return
        rss_started = False
        monitor_rss.cancel()
        await message.channel.send("🛑 **RSS monitoring stopped.**")

    # ── !checkrss ────────────────────────────────────────────────────────────
    elif content_lower == "!checkrss":
        # Shows the newest entry of each feed the way it would be posted — without pinging anyone.
        try:
            feeds = load_feeds()
            if not feeds:
                await message.channel.send("❌ No feeds configured yet. Use `/rss-create` to add one first.")
                return
            sent_any = False
            for name, url in feeds.items():
                releases = await asyncio.to_thread(fetch_releases, url)
                if releases:
                    await post_release(message.channel, name, releases[0], ping=False)
                    sent_any = True
            if not sent_any:
                await message.channel.send("❌ No entries found in the feed(s).")
        except Exception as e:
            await message.channel.send(f"❌ Error: {e}")

    # ── !botinfo ─────────────────────────────────────────────────────────────
    elif content_lower == "!botinfo":
        feeds = load_feeds()
        info_embed = discord.Embed(
            title="ℹ️ RSS Bot Information",
            description="RSS release monitor",
            color=0x3498db,
        )
        if feeds:
            for n, u in feeds.items():
                info_embed.add_field(name=f"📡 {n}", value=u, inline=False)
        else:
            info_embed.add_field(name="📡 Feeds", value="None yet — use `/rss-create`", inline=False)
        info_embed.add_field(name="⏱️ Check Interval", value=f"{CHECK_INTERVAL}s", inline=True)
        info_embed.add_field(
            name="🚀 Status",
            value="🟢 Running" if rss_started else "🔴 Stopped — use `!rssstart`",
            inline=True,
        )
        info_embed.add_field(
            name="Commands",
            value="`!rssstart` `!rssstop` `!checkrss` `!botinfo` • `/rss-create` `/rss-list` `/rss-remove`",
            inline=False,
        )
        info_embed.set_footer(text=f"Bot: {bot.user.name}")
        await message.channel.send(embed=info_embed)

# ==================== BOT EVENTS ====================


@bot.event
async def on_ready():
    print("=" * 60)
    print(f"🤖 RSS Bot ready! Logged in as {bot.user}")
    feeds = load_feeds()
    if feeds:
        for n, u in feeds.items():
            print(f"📡 {n}: {u}")
    else:
        print("📡 No feeds configured yet — use /rss-create")
    print(f"💬 Channel ID: {CHANNEL_ID}")
    print(f"⏱️  Check interval: {CHECK_INTERVAL} seconds")
    print("📝 Text: !rssstart, !rssstop, !checkrss, !botinfo")
    print("📝 Slash: /rss-create, /rss-list, /rss-remove")
    print("⚠️  RSS monitoring is PAUSED — an admin must type !rssstart to begin.")
    print("=" * 60)

# ==================== RUN ====================

if __name__ == "__main__":
    if DISCORD_TOKEN == "YOUR_RSS_BOT_TOKEN_HERE" or not DISCORD_TOKEN:
        print("❌ ERROR: RSS_BOT_TOKEN not set in .env!")
        raise SystemExit(1)
    if not CHANNEL_ID:
        print("❌ ERROR: RSS_CHANNEL_ID not set in .env!")
        raise SystemExit(1)
    print("Starting RSS Release Bot...")
    bot.run(DISCORD_TOKEN)
