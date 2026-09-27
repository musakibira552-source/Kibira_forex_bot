"""
FOREX NEWS ALERT TELEGRAM BOT
For:
EUR/CAD
GBP/CAD
EUR/CHF
AUD/JPY
AUD/CHF
AUD/NZD
USD/CAD
USD/JPY
USD/CHF
GBP/USD

Alerts:
- High-impact news only
- USD, CAD, EUR, GBP, CHF, AUD, JPY, NZD
- 30 minutes before
- 5 minutes before
- Uganda time: Africa/Kampala

Runs continuously as a long-lived process (checks every 60 seconds).
Designed to be deployed on Railway as a Worker service.

IMPORTANT:
Set these Railway environment variables:

TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
NEWS_API_URL=(optional - defaults to a free ForexFactory calendar feed)
NEWS_API_KEY=(optional - only needed if you switch to a paid provider)
"""

import os
import json
import time
import hashlib
from datetime import datetime, timezone

import requests
from zoneinfo import ZoneInfo


# ============================================================
# CONFIGURATION
# ============================================================

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Free, no-key-required ForexFactory weekly calendar feed.
# Returns upcoming high/medium/low impact events with country, date, impact,
# forecast, previous. Swap this for a paid provider by setting NEWS_API_URL.
DEFAULT_NEWS_API_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"

NEWS_API_URL = os.getenv("NEWS_API_URL", DEFAULT_NEWS_API_URL)
NEWS_API_KEY = os.getenv("NEWS_API_KEY")

CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "300"))

UGANDA_TZ = ZoneInfo("Africa/Kampala")

WATCHLIST = {
    "EUR/CAD",
    "GBP/CAD",
    "EUR/CHF",
    "AUD/JPY",
    "AUD/CHF",
    "AUD/NZD",
    "USD/CAD",
    "USD/JPY",
    "USD/CHF",
    "GBP/USD",
}

WATCHED_CURRENCIES = {
    "USD",
    "CAD",
    "EUR",
    "GBP",
    "CHF",
    "AUD",
    "JPY",
    "NZD",
}

ALERT_WINDOWS = {
    30: "30 MINUTES",
    5: "5 MINUTES",
}

STATE_FILE = os.getenv("STATE_FILE", "news_alert_state.json")


# ============================================================
# VALIDATION
# ============================================================

def validate_config():
    missing = []
    if not BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not CHAT_ID:
        missing.append("TELEGRAM_CHAT_ID")
    if not NEWS_API_URL:
        missing.append("NEWS_API_URL")
    if missing:
        raise RuntimeError("Missing Railway variables: " + ", ".join(missing))


# ============================================================
# PAIR MAPPING
# ============================================================

def get_affected_pairs(currency):
    affected = []
    for pair in WATCHLIST:
        base, quote = pair.split("/")
        if currency == base or currency == quote:
            affected.append(pair)
    return sorted(affected)


# ============================================================
# NEWS FETCHER
# ============================================================

def fetch_news():
    headers = {
        "Accept": "application/json",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Referer": "https://www.forexfactory.com/",
    }
    if NEWS_API_KEY:
        headers["Authorization"] = f"Bearer {NEWS_API_KEY}"

    response = requests.get(NEWS_API_URL, headers=headers, timeout=20)

    if response.status_code == 429:
        print("Rate limited by news provider — will retry next cycle.")
        return []

    response.raise_for_status()
    return response.json()


# ============================================================
# NORMALIZE NEWS DATA
# ============================================================

def normalize_events(data):
    """
    Different calendar providers use different field names.
    The rest of the program expects:
        currency, impact, title, event_time, forecast, previous
    """
    if isinstance(data, dict):
        events = data.get("events") or data.get("data") or data.get("results") or []
    elif isinstance(data, list):
        events = data
    else:
        events = []

    normalized = []

    for event in events:
        currency = (
            event.get("currency")
            or event.get("country")
            or event.get("currency_code")
        )
        impact = (
            event.get("impact")
            or event.get("impact_level")
            or event.get("importance")
            or ""
        )
        title = event.get("title") or event.get("event") or event.get("name") or "Economic Event"
        event_time = (
            event.get("datetime")
            or event.get("date")
            or event.get("time")
            or event.get("event_time")
        )
        forecast = event.get("forecast") or event.get("estimate") or "N/A"
        previous = event.get("previous") or event.get("prior") or "N/A"

        normalized.append({
            "currency": str(currency).upper() if currency else "",
            "impact": str(impact).lower(),
            "title": title,
            "event_time": event_time,
            "forecast": forecast,
            "previous": previous,
        })

    return normalized


# ============================================================
# HIGH-IMPACT FILTER
# ============================================================

def is_high_impact(event):
    currency = event["currency"]
    impact = event["impact"]

    if currency not in WATCHED_CURRENCIES:
        return False

    high_impact_values = {"high", "red", "3", "high impact"}
    if impact not in high_impact_values:
        return False

    return True


# ============================================================
# TIME HANDLING
# ============================================================

def parse_event_time(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def minutes_until(event_time):
    now = datetime.now(timezone.utc)
    return (event_time - now).total_seconds() / 60


# ============================================================
# EVENT ID + DEDUPE STATE
# ============================================================

def event_id(event):
    raw = event["currency"] + event["title"] + str(event["event_time"])
    return hashlib.sha256(raw.encode()).hexdigest()


def load_state():
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r") as file:
            return json.load(file)
    except Exception:
        return {}


def save_state(state):
    with open(STATE_FILE, "w") as file:
        json.dump(state, file)


def already_sent(state, event_id_value, minutes):
    return f"{event_id_value}:{minutes}" in state


def mark_sent(state, event_id_value, minutes):
    state[f"{event_id_value}:{minutes}"] = datetime.now(timezone.utc).isoformat()


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message}
    response = requests.post(url, json=payload, timeout=20)
    response.raise_for_status()


# ============================================================
# MESSAGE FORMAT
# ============================================================

def build_alert(event, event_time, minutes):
    currency = event["currency"]
    pairs = get_affected_pairs(currency)
    pair_text = "\n".join(f"• {pair}" for pair in pairs)

    local_time = event_time.astimezone(UGANDA_TZ)
    time_string = local_time.strftime("%I:%M %p")

    header = "🔴 HIGH IMPACT NEWS" if minutes == 30 else "🚨 5 MINUTES TO NEWS"

    message = f"""
{header}

💱 {currency} — {event["title"]}

⏰ {time_string} EAT

⏳ {ALERT_WINDOWS[minutes]}

📊 Forecast:
{event["forecast"]}

📌 Previous:
{event["previous"]}

📈 YOUR WATCHLIST

{pair_text}

⚠️ VOLATILITY WARNING

Your trading sequence:

Liquidity Sweep
↓
Displacement
↓
FVG
↓
Retracement
↓
Confirmation
↓
Entry

News is NOT an entry signal.
"""
    return message.strip()


# ============================================================
# PROCESS EVENTS
# ============================================================

def process_events(events):
    state = load_state()

    for event in events:
        try:
            if not is_high_impact(event):
                continue

            affected = get_affected_pairs(event["currency"])
            if not affected:
                continue

            event_time = parse_event_time(event["event_time"])
            if not event_time:
                continue

            minutes = minutes_until(event_time)
            event_id_value = event_id(event)

            if 25 <= minutes <= 35:
                alert_minutes = 30
                if not already_sent(state, event_id_value, alert_minutes):
                    message = build_alert(event, event_time, alert_minutes)
                    send_telegram(message)
                    mark_sent(state, event_id_value, alert_minutes)
                    print("Sent 30-minute alert:", event["title"])

            elif 2 <= minutes <= 9:
                alert_minutes = 5
                if not already_sent(state, event_id_value, alert_minutes):
                    message = build_alert(event, event_time, alert_minutes)
                    send_telegram(message)
                    mark_sent(state, event_id_value, alert_minutes)
                    print("Sent 5-minute alert:", event["title"])

        except Exception as error:
            print("Event processing error:", error)

    save_state(state)


# ============================================================
# ONE CHECK CYCLE
# ============================================================

def run_check():
    try:
        raw_data = fetch_news()
        events = normalize_events(raw_data)
        print(f"[{datetime.now(timezone.utc).isoformat()}] Received {len(events)} events")
        process_events(events)
    except Exception as error:
        print("NEWS SYSTEM ERROR:", error)


# ============================================================
# MAIN LOOP
# ============================================================

def main():
    print("Starting Forex News Alert Bot...")
    validate_config()
    print(f"Checking every {CHECK_INTERVAL_SECONDS} seconds. Watchlist: {sorted(WATCHLIST)}")

    while True:
        run_check()
        time.sleep(CHECK_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
