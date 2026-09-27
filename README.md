# Forex News Alert Bot — Railway Deployment

## 1. Create your Telegram bot
1. In Telegram, message **@BotFather** → `/newbot` → follow prompts.
2. Copy the **bot token** it gives you (`TELEGRAM_BOT_TOKEN`).
3. Send your new bot any message, then visit:
   `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates`
   Find `"chat":{"id": ...}` — that number is your `TELEGRAM_CHAT_ID`.

## 2. Push this folder to GitHub
Create a new repo and push these files:
- `bot.py`
- `requirements.txt`
- `Procfile`

## 3. Deploy on Railway
1. Go to railway.app → **New Project** → **Deploy from GitHub repo** → select your repo.
2. Railway will detect Python and the `Procfile` and deploy a **worker** service
   (no public URL needed — this bot doesn't serve web traffic, it just runs a loop).
3. Go to your service → **Variables** tab → add:
   - `TELEGRAM_BOT_TOKEN` = your bot token
   - `TELEGRAM_CHAT_ID` = your chat id
   - (optional) `NEWS_API_URL` — only set this if you want a different calendar
     provider than the default free ForexFactory feed
   - (optional) `CHECK_INTERVAL_SECONDS` = `60` (default)
4. Deploy. Check the **Logs** tab — you should see:
   `Starting Forex News Alert Bot...` then periodic `Received N events`.

## 4. Notes on the news source
The bot defaults to a free, no-key ForexFactory weekly calendar feed
(`nfs.faireconomy.media/ff_calendar_thisweek.json`). It returns High/Medium/Low
impact economic events tagged by currency (USD, EUR, GBP, etc.) — exactly what
this bot filters on. No signup needed, but it's an unofficial public endpoint,
so if it ever goes down, swap `NEWS_API_URL` for a paid calendar provider
(e.g. Trading Economics, FCS API) — the `normalize_events()` function already
handles multiple common field-name formats.

## 5. State persistence caveat
The bot writes a small `news_alert_state.json` file to avoid duplicate alerts.
On Railway's default ephemeral filesystem, this resets on every redeploy —
meaning you could get one duplicate alert right after a redeploy, but nothing
worse. If you want it to survive redeploys, add a Railway Volume mounted at
the working directory.

## 6. Testing before going live
Temporarily lower the alert windows in `bot.py` (e.g. change `29 <= minutes <= 31`
to a wider range) or just watch the logs during a known high-impact release
(e.g. US CPI, NFP, FOMC) to confirm alerts fire correctly.
