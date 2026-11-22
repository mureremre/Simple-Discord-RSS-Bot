# RSS Release Bot

A small Discord bot that watches RSS feeds and posts every new entry to a
channel as a rich embed, title, link, cover image, and an optional role
ping. Feeds aren't hardcoded in config: add as many as you want at runtime
with a slash command, no restart needed.

## Features

- Add/list/remove feeds at runtime with `/rss-create`, `/rss-list`, `/rss-remove`.
- Polls every configured feed on an interval you set.
- Posts new entries oldest-first, with title, link, cover image and timestamp.
- Pings a shared "update" role, if one is configured.
- Remembers what it has already posted, per feed, so restarts don't re-post.
- A feed's first check is recorded quietly, it won't flood the channel with
  its whole back catalog.
- A sudden burst of "new" items is treated as the feed changing, not a wave
  of real releases: everything is marked seen and nothing is posted.
- `!checkrss` previews the newest entry of each feed without pinging anyone.

## Setup

1. Create a Discord application and bot at the
   [Discord Developer Portal](https://discord.com/developers/applications),
   copy its token, and invite it to your server with permission to send
   messages and embeds, and to use application (slash) commands.
2. Clone this repo and install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

3. Copy `.env.example` to `.env` and fill it in:

   ```bash
   cp .env.example .env
   ```

   At minimum you need `RSS_BOT_TOKEN` and `RSS_CHANNEL_ID`.

4. Run the bot:

   ```bash
   python bot.py
   ```

5. In Discord, add a feed with `/rss-create`, then start monitoring with
   `!rssstart`.

## Commands

### Slash commands

| Command        | Who        | What it does                                   |
|----------------|------------|-------------------------------------------------|
| `/rss-create`  | Admin only | Adds a new feed (give it a name and a URL)      |
| `/rss-list`    | Anyone     | Shows every feed currently configured           |
| `/rss-remove`  | Admin only | Stops watching a feed (name autocompletes)      |

### Text commands

| Command       | Who        | What it does                                              |
|---------------|------------|--------------------------------------------------------------|
| `!rssstart`   | Admin only | Starts the polling loop                                      |
| `!rssstop`    | Admin only | Stops the polling loop                                        |
| `!checkrss`   | Anyone     | Previews the newest entry of each feed, pings no one           |
| `!botinfo`    | Anyone     | Shows the bot's current feeds, interval and status             |

## Configuration

See `.env.example` for every supported option. The important ones:

- `RSS_CHANNEL_ID` — the channel releases get posted in.
- `ROLE_UPDATE` — a Discord role ID to ping on every release. Leave blank to
  ping no one.
- `CHECK_INTERVAL` — seconds between feed checks (default `120`).
- `FLOOD_LIMIT` — safety valve: if a check finds more "new" items than this
  at once for one feed, the bot assumes that feed was reset rather than that
  this many things released simultaneously, and posts nothing (default `10`).

Feeds themselves are **not** set in `.env` — add them with `/rss-create`
once the bot is running.

## Notes

The bot keeps two small JSON files next to `bot.py`, created the first time
they're needed:

- `rss_feeds.json` — the feeds you've added with `/rss-create`.
- `rss_last_entries.json` — what it has already posted, per feed.

Both are in `.gitignore` since they're runtime state, not source code. Back
them up if you want to preserve your feed list across a fresh deploy.

## License

MIT — see [LICENSE](LICENSE).
