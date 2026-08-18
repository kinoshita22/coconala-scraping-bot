# Coconala new-job → Discord / Slack notifier

Watches two [Coconala job-request](https://coconala.com/requests) categories and posts
every new job to **a separate channel per category**, on Discord and/or Slack.

| Watched page | Destinations |
| --- | --- |
| https://coconala.com/requests/categories/22 | `DISCORD_WEBHOOK_CATEGORY_22` / `SLACK_WEBHOOK_CATEGORY_22` |
| https://coconala.com/requests/categories/11 | `DISCORD_WEBHOOK_CATEGORY_11` / `SLACK_WEBHOOK_CATEGORY_11` |

Both services can run at once, or either one alone — a destination is simply skipped
when its webhook URL is empty. Each destination keeps **its own delivery state**, so
adding Slack later does not re-post old jobs to Discord, and an outage on one service
never blocks the other.

Each message contains: **category (linked) / title (linked to the job) / budget /
client name, avatar and profile link / job URL**, plus proposal count, deadline and
posting time.

## Setup

```bash
cd coconala-bot-python
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# put your two webhook URLs in .env
```

Create the webhooks under:

- **Discord** — Channel settings → Integrations → Webhooks → New Webhook, one per channel.
- **Slack** — <https://api.slack.com/apps> → your app → Incoming Webhooks → Add New
  Webhook to Workspace, then pick the channel. A Slack incoming webhook is bound to one
  channel, so create one per category.

No bot token is required for either service.

## Running

```bash
python main.py
```

- **On the first run, existing postings are recorded as "seen" and not sent**, so the
  channel is not flooded at startup. Set `NOTIFY_ON_FIRST_RUN=true` to send them anyway.
- Seen request IDs live in `state.json`, so a restart never re-posts the same job.
- Stop with `Ctrl+C` (SIGTERM also shuts down cleanly).

### Verifying

```bash
python check_parser.py       # scraping only, nothing is sent anywhere
python send_test.py          # really posts the newest job to every channel
python send_test.py slack    # only Slack
python send_test.py discord  # only Discord
```

## Nothing arrives in Discord?

First check whether there is actually anything new. Coconala gets only a handful of new
postings per day, and with `NOTIFY_ON_FIRST_RUN=false` the jobs that already existed at
startup are never sent. **Silence until someone submits a new job is the expected
behaviour.**

- Right after startup you should see one line per destination, e.g.
  `category_22 -> connected to Discord webhook 'xxx' (channel_id=...)` and
  `category_22 -> Slack webhook configured`. A Discord line reporting HTTP 404 means the
  webhook was deleted in Discord and must be recreated. (Slack incoming webhooks cannot
  be verified without posting, so they are only reported as configured.)
- Every 10 minutes the bot logs `still watching (nothing new): ... newest=...`.
  That line means it is polling normally.
- To see a real message, run `python send_test.py`
  (posts the newest job to every channel; `state.json` is not modified).
- To replay the current listing through the normal path, delete `state.json` and start
  with `NOTIFY_ON_FIRST_RUN=true` (up to `MAX_NOTIFY_PER_CYCLE` per channel).
- Make sure only **one** instance is running — two bots mean duplicate messages and a
  race on `state.json`.

## How it works

Coconala's listing pages are server-rendered by Nuxt, and the whole request list is
embedded in `window.__NUXT__` as structured data (request id, title, budget, client,
avatar path, timestamps). The bot rebuilds that payload instead of scraping the DOM, so
it is fairly resilient to CSS class changes and can read the **client avatar URL**,
which never appears in the listing HTML. If the payload format changes, it falls back
automatically to parsing the rendered HTML cards (avatars are unavailable in that mode).

## Settings (.env)

| Variable | Default | Meaning |
| --- | --- | --- |
| `DISCORD_WEBHOOK_CATEGORY_22` | (empty) | Discord webhook for the category 22 channel |
| `DISCORD_WEBHOOK_CATEGORY_11` | (empty) | Discord webhook for the category 11 channel |
| `SLACK_WEBHOOK_CATEGORY_22` | (empty) | Slack webhook for the category 22 channel |
| `SLACK_WEBHOOK_CATEGORY_11` | (empty) | Slack webhook for the category 11 channel |
| `POLL_INTERVAL` | `3` | Polling interval in seconds |
| `NOTIFY_ON_FIRST_RUN` | `false` | Send existing postings on the first run |
| `MAX_NOTIFY_PER_CYCLE` | `10` | Max messages per cycle (safety valve) |
| `INCLUDE_DESCRIPTION` | `false` | Include an excerpt of the job description |
| `DESCRIPTION_MAX_CHARS` | `300` | Length of that excerpt |
| `STATE_FILE` | `state.json` | Where seen request IDs are stored |
| `STATE_MAX_IDS` | `2000` | How many IDs to keep per feed |
| `DISCORD_MIN_INTERVAL` | `1.2` | Minimum gap between posts to one Discord webhook (s) |
| `SLACK_MIN_INTERVAL` | `1.2` | Minimum gap between posts to one Slack webhook (s) |
| `HTTP_TIMEOUT` | `20` | HTTP timeout in seconds |
| `LOG_LEVEL` | `INFO` | Log level |

## Running as a service (systemd)

`/etc/systemd/system/coconala-bot.service`:

```ini
[Unit]
Description=Coconala new request -> Discord bot
After=network-online.target

[Service]
Type=simple
User=hades
WorkingDirectory=/home/hades/Music/mywork/coconala-bot-python
ExecStart=/home/hades/Music/mywork/coconala-bot-python/.venv/bin/python main.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload && sudo systemctl enable --now coconala-bot
journalctl -u coconala-bot -f
```

## Notes

- **Bandwidth**: a listing page is about 150 KB gzipped. At a 3 second interval across
  two URLs that is roughly **6 MB per minute, or ~8 GB per day**. For 24/7 operation,
  raising `POLL_INTERVAL` to 10-30 seconds is recommended — given how rarely new jobs
  are posted, you will not miss anything.
- Polling this aggressively puts load on the site and can get you blocked (HTTP 403).
  Check Coconala's terms of service and `robots.txt`, and run it at your own risk.
  Repeated failures make the bot back off automatically.
- A job whose delivery failed is not marked as seen, so it is retried on the next cycle.
