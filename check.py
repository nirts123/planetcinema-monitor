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
  3. A specific movie (by film code) newly appearing in the bookable window.

State is kept in state.json next to this script; run it periodically (cron/loop)
and it prints only what's NEW since the last run.
"""
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import date, timedelta

SITE = "10100"
BASE = f"https://www.planetcinema.co.il/il/data-api-service/v1"
STATE_FILE = os.path.join(os.path.dirname(__file__), "state.json")

CINEMAS = {
    "1072": "פלאנט ראשון לציון",
    "1025": "פלאנט אילון",
    "1074": "פלאנט באר שבע",
    "1075": "פלאנט זכרון יעקב",
    "1070": "פלאנט חיפה",
    "1073": "פלאנט ירושלים",
}

# Movies to specifically watch for by film code (from film page URL, e.g.
# /films/the-odyssey/7460s2r -> "7460s2r"). Add more as needed.
WATCH_FILMS = {
    "7460s2r": "The Odyssey / האודיסאה",
}

HORIZON_DAYS = 21


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def get_feed(name):
    d = fetch(f"{BASE}/feed/{SITE}/byName/{name}?lang=he_IL")
    return {p["url"].rstrip("/").rsplit("/", 1)[-1]: p for p in d["body"]["posters"]}


def get_day_films(cinema_id, day):
    d = fetch(f"{BASE}/quickbook/{SITE}/film-events/in-cinema/{cinema_id}/at-date/{day}?attr=&lang=he_IL")
    return {f["id"]: f["name"] for f in d["body"]["films"]}


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"feeds": {}, "horizon": {}, "watch_seen": {}}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def send_telegram(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("(no TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID set, skipping push)", file=sys.stderr)
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    # Telegram messages cap at 4096 chars; chunk if needed.
    for i in range(0, len(text), 3500):
        chunk = text[i:i + 3500]
        data = urllib.parse.urlencode({"chat_id": chat_id, "text": chunk}).encode()
        req = urllib.request.Request(url, data=data, method="POST")
        try:
            urllib.request.urlopen(req, timeout=15).read()
        except Exception as e:
            print(f"! telegram send failed: {e}", file=sys.stderr)


def main():
    state = load_state()
    new_events = []

    # 1. New movies in coming-soon / now-playing feeds
    for feed_name in ("coming-soon", "now-playing"):
        try:
            current = get_feed(feed_name)
        except Exception as e:
            print(f"! failed to fetch {feed_name}: {e}", file=sys.stderr)
            continue
        prev = state["feeds"].get(feed_name, {})
        for code, p in current.items():
            if code not in prev:
                new_events.append(f"[{feed_name}] NEW: {p['featureTitle']} -> {p['url']} (dateStarted={p.get('dateStarted')})")
        state["feeds"][feed_name] = {code: {"featureTitle": p["featureTitle"], "url": p["url"]} for code, p in current.items()}

    # 2. Booking horizon per cinema + watch-film detection
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
            for code in WATCH_FILMS:
                if code in films:
                    watch_hits.add(day)

        prev_horizon = state["horizon"].get(cinema_id)
        if last_open and last_open != prev_horizon:
            new_events.append(f"[{cinema_name}] booking horizon now open through {last_open} (was {prev_horizon})")
        state["horizon"][cinema_id] = last_open

        for code, label in WATCH_FILMS.items():
            key = f"{cinema_id}:{code}"
            prev_days = set(state["watch_seen"].get(key, []))
            newly = sorted(d for d in watch_hits if d not in prev_days)
            if newly:
                new_events.append(f"[{cinema_name}] {label}: newly bookable on {', '.join(newly)}")
            state["watch_seen"][key] = sorted(watch_hits)

    save_state(state)

    if new_events:
        message = "\n".join(new_events)
        print(message)
        send_telegram(message)
    else:
        print("No changes since last check.")


if __name__ == "__main__":
    main()
