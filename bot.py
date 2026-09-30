"""
FOREX NEWS + PRICE ALERT TELEGRAM BOT

News alerts for:
EUR/CAD, GBP/CAD, EUR/CHF, AUD/JPY, AUD/CHF, AUD/NZD,
USD/CAD, USD/JPY, USD/CHF, GBP/USD

News alert rules:
- High-impact news only
- USD, CAD, EUR, GBP, CHF, AUD, JPY, NZD
- 30 minutes before / 5 minutes before
- Uganda time: Africa/Kampala

Price alerts:
- Message the bot on Telegram with a pair and a target price, e.g.:
      AUDUSD 153.654
  or:
      /AUDUSD, 153.654
- Works for ANY pair (not just the watchlist above), as long as Twelve Data
  supports it.
- The bot checks the live price periodically. If your target is above the
  current price, it alerts when price rises to/above it. If your target is
  below the current price, it alerts when price falls to/below it.
- One-shot: each alert fires once, then is removed.

Runs continuously as a long-lived process. Designed to be deployed on
Railway as a Worker service.

IMPORTANT:
Set these Railway environment variables:

TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
TWELVE_DATA_API_KEY=your_twelvedata_api_key
NEWS_API_URL=(optional - defaults to a free ForexFactory calendar feed)
NEWS_API_KEY=(optional - only needed if you switch to a paid provider)
"""

import os
import re
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

TWELVE_DATA_API_KEY = os.getenv("TWELVE_DATA_API_KEY")
TWELVE_DATA_BASE_URL = "https://api.twelvedata.com/price"

NEWS_CHECK_INTERVAL_SECONDS = int(os.getenv("CHECK_INTERVAL_SECONDS", "300"))
PRICE_CHECK_INTERVAL_SECONDS = int(os.getenv("PRICE_CHECK_INTERVAL_SECONDS", "60"))
LOOP_SLEEP_SECONDS = int(os.getenv("LOOP_SLEEP_SECONDS", "15"))

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
PRICE_ALERTS_FILE = os.getenv("PRICE_ALERTS_FILE", "price_alerts_state.json")
TELEGRAM_OFFSET_FILE = os.getenv("TELEGRAM_OFFSET_FILE", "telegram_offset.json")


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

    if not TWELVE_DATA_API_KEY:
        print(
            "WARNING: TWELVE_DATA_API_KEY not set — price alerts (e.g. 'AUDUSD "
            "153.654') will be accepted but cannot be checked until it's added."
        )


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


def load_json_file(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r") as file:
            return json.load(file)
    except Exception:
        return default


def save_json_file(path, data):
    with open(path, "w") as file:
        json.dump(data, file)


def load_state():
    return load_json_file(STATE_FILE, {})


def save_state(state):
    save_json_file(STATE_FILE, state)


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
# TELEGRAM COMMAND POLLING (for price alerts)
# ============================================================

def get_telegram_updates(offset):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
    params = {"timeout": 10}
    if offset is not None:
        params["offset"] = offset

    response = requests.get(url, params=params, timeout=20)
    response.raise_for_status()
    data = response.json()
    return data.get("result", [])


# Matches things like:
#   AUDUSD 153.654
#   /AUDUSD, 153.654
#   AUD/USD 153.654
#   audusd 0.6543
COMMAND_PATTERN = re.compile(
    r"^/?\s*([A-Za-z]{3})\s*/?\s*([A-Za-z]{3})\s*[,:]?\s+([0-9]*\.?[0-9]+)\s*$"
)


def parse_price_command(text):
    """Returns (pair, target_price) or None if the text isn't a price alert command."""
    if not text:
        return None

    match = COMMAND_PATTERN.match(text.strip())
    if not match:
        return None

    base, quote, price_str = match.groups()
    pair = f"{base.upper()}/{quote.upper()}"

    try:
        target_price = float(price_str)
    except ValueError:
        return None

    return pair, target_price


def handle_telegram_updates():
    offset_data = load_json_file(TELEGRAM_OFFSET_FILE, {"offset": None})
    offset = offset_data.get("offset")

    try:
        updates = get_telegram_updates(offset)
    except Exception as error:
        print("Telegram polling error:", error)
        return

    if not updates:
        return

    price_alerts = load_json_file(PRICE_ALERTS_FILE, [])

    for update in updates:
        offset = update["update_id"] + 1

        message = update.get("message") or update.get("edited_message")
        if not message:
            continue

        text = message.get("text", "")
        parsed = parse_price_command(text)

        if parsed is None:
            if text.strip():
                send_telegram(
                    "Didn't recognize that. To set a price alert, send:\n"
                    "PAIR PRICE\n"
                    "e.g. AUDUSD 0.6543"
                )
            continue

        pair, target_price = parsed
        current_price = fetch_price(pair)

        if current_price is None:
            send_telegram(
                f"Couldn't fetch a live price for {pair} — check the pair is "
                f"correct and supported by Twelve Data, then try again."
            )
            continue

        direction = "above" if target_price >= current_price else "below"

        alert = {
            "id": hashlib.sha256(
                f"{pair}{target_price}{message['date']}".encode()
            ).hexdigest(),
            "pair": pair,
            "target_price": target_price,
            "direction": direction,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        price_alerts.append(alert)

        arrow = "≥" if direction == "above" else "≤"
        send_telegram(
            f"✅ Price alert set: {pair} {arrow} {target_price}\n"
            f"(current price: {current_price})\n"
            f"I'll message you once when it hits."
        )
        print(f"New price alert: {pair} {direction} {target_price}")

    save_json_file(PRICE_ALERTS_FILE, price_alerts)
    save_json_file(TELEGRAM_OFFSET_FILE, {"offset": offset})


# ============================================================
# LIVE PRICE FETCHING (Twelve Data)
# ============================================================

def fetch_price(pair):
    """Fetch a single live price. Returns float or None on failure."""
    prices = fetch_prices([pair])
    return prices.get(pair)


def fetch_prices(pairs):
    """Fetch live prices for multiple pairs in one request. Returns {pair: price}."""
    if not pairs:
        return {}

    if not TWELVE_DATA_API_KEY:
        print("Skipping price check — TWELVE_DATA_API_KEY not set.")
        return {}

    symbol_param = ",".join(pairs)

    try:
        response = requests.get(
            TWELVE_DATA_BASE_URL,
            params={"symbol": symbol_param, "apikey": TWELVE_DATA_API_KEY},
            timeout=20,
        )
        response.raise_for_status()
        data = response.json()
    except Exception as error:
        print("Twelve Data fetch error:", error)
        return {}

    results = {}

    if len(pairs) == 1:
        # Single-symbol responses come back as {"price": "..."} directly.
        price = data.get("price")
        if price is not None:
            try:
                results[pairs[0]] = float(price)
            except (TypeError, ValueError):
                pass
        elif data.get("code"):
            print(f"Twelve Data error for {pairs[0]}: {data.get('message')}")
    else:
        # Multi-symbol responses are keyed by symbol.
        for pair in pairs:
            entry = data.get(pair)
            if isinstance(entry, dict) and entry.get("price") is not None:
                try:
                    results[pair] = float(entry["price"])
                except (TypeError, ValueError):
                    pass

    return results


# ============================================================
# PRICE ALERT CHECKING
# ============================================================

def check_price_alerts():
    price_alerts = load_json_file(PRICE_ALERTS_FILE, [])

    if not price_alerts:
        return

    pairs = sorted({alert["pair"] for alert in price_alerts})
    current_prices = fetch_prices(pairs)

    if not current_prices:
        return

    remaining_alerts = []

    for alert in price_alerts:
        pair = alert["pair"]
        target = alert["target_price"]
        direction = alert["direction"]

        current_price = current_prices.get(pair)

        if current_price is None:
            remaining_alerts.append(alert)
            continue

        triggered = (
            (direction == "above" and current_price >= target)
            or (direction == "below" and current_price <= target)
        )

        if triggered:
            verb = "risen to/above" if direction == "above" else "fallen to/below"
            message = (
                f"🔔 PRICE ALERT\n\n"
                f"💱 {pair} has {verb} {target}\n"
                f"📊 Current price: {current_price}"
            )
            send_telegram(message)
            print(f"Price alert triggered: {pair} {direction} {target}")
        else:
            remaining_alerts.append(alert)

    save_json_file(PRICE_ALERTS_FILE, remaining_alerts)


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
    print("Starting Forex News + Price Alert Bot...")
    validate_config()
    print(
        f"News check every {NEWS_CHECK_INTERVAL_SECONDS}s. "
        f"Price check every {PRICE_CHECK_INTERVAL_SECONDS}s. "
        f"Watchlist: {sorted(WATCHLIST)}"
    )

    last_news_check = 0.0
    last_price_check = 0.0

    while True:
        handle_telegram_updates()

        now = time.monotonic()

        if now - last_price_check >= PRICE_CHECK_INTERVAL_SECONDS:
            check_price_alerts()
            last_price_check = now

        if now - last_news_check >= NEWS_CHECK_INTERVAL_SECONDS:
            run_check()
            last_news_check = now

        time.sleep(LOOP_SLEEP_SECONDS)


if __name__ == "__main__":
    main()
