#!/usr/bin/env python3
"""
Planet Cinema (planetcinema.co.il) booking-window monitor.

Planet's site is a Vista Cinema storefront. Two undocumented JSON APIs matter:

- Coming-soon / now-playing feed (new movie announcements):
  https://www.planetcinema.co.il/il/data-api-service/v1/feed/10100/byName/{coming-soon|now-playing}?lang=he_IL

- Per-cinema, per-day showtime listing (what's actually bookable "now"):
  https://www.planetcinema.co.il/il/data-api-service/v1/quickbook/10100/film-events/in-cinema/{cinemaId}/at-date/{YYYY-MM-DD}?attr=&lang=he_IL
  -> body.films is a list of films with sessions that day, id = film code (e.g. "7460s2r" = The Odyssey)

Planet only publishes bookable sessions a week or two ahead; new dates get
added periodically (this site: roughly weekly). This script detects:
  1. New movies appearing in the coming-soon/now-playing feeds.
  2. The booking horizon (last date with any sessions) advancing per cinema.
  3. A watched movie newly appearing in the bookable window.

The watchlist (watchlist.json) can be edited by messaging the Telegram bot:
  /watch <film-url-or-code>   add a film (e.g. /watch https://www.planetcinema.co.il/films/dune-part-three/8105s2r)
  /unwatch <code>             remove a film
  /list                       show current watchlist

State (feed/horizon baselines, Telegram update offset) is kept in state.json;
run this periodically (cron/GitHub Actions) and it prints/pushes only what's
NEW since the last run.
"""
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import date, timedelta

SITE = "10100"
BASE = "https://www.planetcinema.co.il/il/data-api-service/v1"
HERE = os.path.dirname(__file__)
STATE_FILE = os.path.join(HERE, "state.json")
WATCHLIST_FILE = os.path.join(HERE, "watchlist.json")

CINEMAS = {
    "1072": "פלאנט ראשון לציון",
    "1025": "פלאנט אילון",
    "1074": "פלאנט באר שבע",
    "1075": "פלאנט זכרון יעקב",
    "1070": "פלאנט חיפה",
    "1073": "פלאנט ירושלים",
}

HORIZON_DAYS = 21


def fetch(url, data=None, method=None):
    req = urllib.request.Request(url, data=data, method=method, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def get_feed(name):
    d = fetch(f"{BASE}/feed/{SITE}/byName/{name}?lang=he_IL")
    return {p["url"].rstrip("/").rsplit("/", 1)[-1]: p for p in d["body"]["posters"]}


def get_day_films(cinema_id, day):
    d = fetch(f"{BASE}/quickbook/{SITE}/film-events/in-cinema/{cinema_id}/at-date/{day}?attr=&lang=he_IL")
    return {f["id"]: f["name"] for f in d["body"]["films"]}


def load_json(path, default):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return default


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_state():
    return load_json(STATE_FILE, {"feeds": {}, "horizon": {}, "watch_seen": {}, "telegram_offset": 0})


def load_watchlist():
    return load_json(WATCHLIST_FILE, {"7460s2r": "The Odyssey / האודיסאה"})


def send_telegram(text, reply_to=None):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("(no TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID set, skipping push)", file=sys.stderr)
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    # Telegram messages cap at 4096 chars; chunk if needed.
    for i in range(0, len(text), 3500):
        chunk = text[i:i + 3500]
        params = {"chat_id": chat_id, "text": chunk}
        if reply_to:
            params["reply_to_message_id"] = reply_to
        data = urllib.parse.urlencode(params).encode()
        try:
            urllib.request.urlopen(urllib.request.Request(url, data=data, method="POST"), timeout=15).read()
        except Exception as e:
            print(f"! telegram send failed: {e}", file=sys.stderr)


def extract_film_code(arg):
    arg = arg.strip()
    # https://www.planetcinema.co.il/films/<slug>/<code> -> <code>
    m = re.search(r"planetcinema\.co\.il/films/[^/]+/([\w-]+)", arg)
    if m:
        return m.group(1).lower()
    return arg.lower()


def resolve_film_name(code, all_feed_entries):
    for entry in all_feed_entries:
        if entry.get("code") == code:
            return entry["featureTitle"]
    return code


def process_telegram_commands(state, watchlist, all_feed_entries):
    """Poll Telegram for /watch, /unwatch, /list commands and mutate watchlist in place."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return False

    offset = state.get("telegram_offset", 0)
    url = f"https://api.telegram.org/bot{token}/getUpdates?offset={offset}&timeout=0"
    try:
        updates = fetch(url)
    except Exception as e:
        print(f"! telegram getUpdates failed: {e}", file=sys.stderr)
        return False

    changed = False
    for update in updates.get("result", []):
        state["telegram_offset"] = update["update_id"] + 1
        msg = update.get("message") or {}
        text = (msg.get("text") or "").strip()
        msg_chat_id = str(msg.get("chat", {}).get("id", ""))
        msg_id = msg.get("message_id")

        # Only accept commands from the configured chat, ignore everything else.
        if msg_chat_id != str(chat_id) or not text.startswith("/"):
            continue

        parts = text.split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""

        if cmd == "/watch" and arg:
            code = extract_film_code(arg)
            name = resolve_film_name(code, all_feed_entries)
            watchlist[code] = name
            changed = True
            send_telegram(f"Watching: {name} ({code})", reply_to=msg_id)
        elif cmd == "/unwatch" and arg:
            code = extract_film_code(arg)
            if code in watchlist:
                name = watchlist.pop(code)
                changed = True
                send_telegram(f"Stopped watching: {name} ({code})", reply_to=msg_id)
            else:
                send_telegram(f"Not on watchlist: {code}", reply_to=msg_id)
        elif cmd == "/list":
            if watchlist:
                lines = [f"- {name} ({code})" for code, name in watchlist.items()]
                send_telegram("Watchlist:\n" + "\n".join(lines), reply_to=msg_id)
            else:
                send_telegram("Watchlist is empty.", reply_to=msg_id)

    return changed


def main():
    state = load_state()
    watchlist = load_watchlist()
    new_events = []

    # 1. New movies in coming-soon / now-playing feeds (also used to resolve
    #    film titles for newly /watch-ed codes).
    all_feed_entries = []
    for feed_name in ("coming-soon", "now-playing"):
        try:
            current = get_feed(feed_name)
        except Exception as e:
            print(f"! failed to fetch {feed_name}: {e}", file=sys.stderr)
            continue
        prev = state["feeds"].get(feed_name, {})
        for code, p in current.items():
            all_feed_entries.append({"code": code, "featureTitle": p["featureTitle"]})
            if code not in prev:
                new_events.append(f"[{feed_name}] NEW: {p['featureTitle']} -> {p['url']} (dateStarted={p.get('dateStarted')})")
        state["feeds"][feed_name] = {code: {"featureTitle": p["featureTitle"], "url": p["url"]} for code, p in current.items()}

    # 2. Pick up /watch, /unwatch, /list commands sent to the bot.
    watchlist_changed = process_telegram_commands(state, watchlist, all_feed_entries)
    if watchlist_changed:
        save_json(WATCHLIST_FILE, watchlist)

    # 3. Booking horizon per cinema + watchlist detection
    today = date.today()
    for cinema_id, cinema_name in CINEMAS.items():
        last_open = None
        watch_hits = set()
        for i in range(HORIZON_DAYS):
            day = (today + timedelta(days=i)).isoformat()
            try:
                films = get_day_films(cinema_id, day)
            except Exception as e:
                print(f"! failed {cinema_id} {day}: {e}", file=sys.stderr)
                continue
            if films:
                last_open = day
            for code in watchlist:
                if code in films:
                    watch_hits.add(day)

        prev_horizon = state["horizon"].get(cinema_id)
        if last_open and last_open != prev_horizon:
            new_events.append(f"[{cinema_name}] booking horizon now open through {last_open} (was {prev_horizon})")
        state["horizon"][cinema_id] = last_open

        for code, label in watchlist.items():
            key = f"{cinema_id}:{code}"
            prev_days = set(state["watch_seen"].get(key, []))
            newly = sorted(d for d in watch_hits if d not in prev_days)
            if newly:
                new_events.append(f"[{cinema_name}] {label}: newly bookable on {', '.join(newly)}")
            state["watch_seen"][key] = sorted(watch_hits)

    save_json(STATE_FILE, state)

    if new_events:
        message = "\n".join(new_events)
        print(message)
        send_telegram(message)
    else:
        print("No changes since last check.")


if __name__ == "__main__":
    main()
