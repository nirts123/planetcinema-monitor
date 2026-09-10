#!/usr/bin/env python3
"""
Planet Cinema (planetcinema.co.il) booking-window monitor.

Planet's site is a Vista Cinema storefront. Two undocumented JSON APIs matter:

- Coming-soon / now-playing feed (new movie announcements):
  https://www.planetcinema.co.il/il/data-api-service/v1/feed/10100/byName/{coming-soon|now-playing}?lang=he_IL

- Per-cinema, per-day showtime listing (what's actually bookable "now"):
  https://www.planetcinema.co.il/il/data-api-service/v1/quickbook/10100/film-events/in-cinema/{cinemaId}/at-date/{YYYY-MM-DD}?attr=&lang=he_IL
  -> body.films is a list of films with sessions that day; each film has
     "id" (film code, e.g. "7460s2r" = The Odyssey) and "attributeIds"
     (includes format tags like "imax", "4dx", "vip" when that cinema shows
     the film in that format that day).

Planet only publishes bookable sessions a week or two ahead; new dates get
added periodically. This script detects:
  1. New movies appearing in the coming-soon/now-playing feeds.
  2. The booking horizon (last date with any sessions) advancing per cinema.
  3. A watched movie (optionally: in IMAX specifically) newly becoming bookable.

The watchlist itself lives in Cloudflare Workers KV, managed instantly via
Telegram (/movies, /watch, /unwatch, /list handled by worker/worker.js, which
Telegram calls directly as a webhook). This script only *reads* the current
watchlist (via the Worker's GET /watchlist, bearer-token protected) - it does
not manage it. Run this periodically (GitHub Actions cron); it prints/pushes
only what's NEW since the last run.
"""
import json
import os
import sys
import urllib.request
from datetime import date, timedelta

SITE = "10100"
BASE = "https://www.planetcinema.co.il/il/data-api-service/v1"
HERE = os.path.dirname(__file__)
STATE_FILE = os.path.join(HERE, "state.json")

CINEMAS = {
    "1072": "פלאנט ראשון לציון",
}

HORIZON_DAYS = 21


def rtl(s):
    """Isolate an RTL (Hebrew) fragment inside an otherwise-LTR line so
    Telegram doesn't garble the bidi ordering."""
    return f"⁧{s}⁩"


def ltr(s):
    """Isolate an LTR (dates/codes/URLs) fragment inside an otherwise-RTL
    line so Telegram doesn't garble the bidi ordering."""
    return f"⁦{s}⁩"


FEED_LABELS = {
    "coming-soon": "בקרוב",
    "now-playing": "כרגע בקולנוע",
}


def display_date(iso_date):
    """'YYYY-MM-DD' (the API/URL format) -> 'DD/MM/YY' for display only."""
    y, m, d = iso_date.split("-")
    return f"{d}/{m}/{y[2:]}"


def fetch(url, headers=None):
    merged = {"User-Agent": "Mozilla/5.0"}
    merged.update(headers or {})
    req = urllib.request.Request(url, headers=merged)
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def get_feed(name):
    """code -> {featureTitle, url, dateStarted, attributes}"""
    d = fetch(f"{BASE}/feed/{SITE}/byName/{name}?lang=he_IL")
    out = {}
    for p in d["body"]["posters"]:
        code = p["url"].rstrip("/").rsplit("/", 1)[-1]
        out[code] = {
            "featureTitle": p["featureTitle"],
            "url": p["url"],
            "dateStarted": p.get("dateStarted"),
            "attributes": p.get("attributes", []),
        }
    return out


def get_day_films(cinema_id, day):
    """code -> {name, attributes}"""
    d = fetch(f"{BASE}/quickbook/{SITE}/film-events/in-cinema/{cinema_id}/at-date/{day}?attr=&lang=he_IL")
    return {f["id"]: {"name": f["name"], "attributes": f.get("attributeIds", [])} for f in d["body"]["films"]}


def load_watchlist():
    """Watchlist is owned by the Cloudflare Worker (Telegram-managed); fetch it."""
    worker_url = os.environ.get("WORKER_URL")
    sync_secret = os.environ.get("WORKER_SYNC_SECRET")
    if not worker_url or not sync_secret:
        print("! WORKER_URL/WORKER_SYNC_SECRET not set, watchlist checks skipped", file=sys.stderr)
        return {}
    try:
        raw = fetch(f"{worker_url}/watchlist", headers={"Authorization": f"Bearer {sync_secret}"})
    except Exception as e:
        print(f"! failed to fetch watchlist from worker: {e}", file=sys.stderr)
        return {}
    watchlist = {}
    for code, val in raw.items():
        if isinstance(val, str):
            watchlist[code] = {"name": val, "imax": False}
        else:
            watchlist[code] = {"name": val.get("name", code), "imax": bool(val.get("imax"))}
    return watchlist


def load_json(path, default):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return default


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_state():
    return load_json(STATE_FILE, {"feeds": {}, "horizon": {}, "watch_seen": {}})


def send_telegram(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("(no TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID set, skipping push)", file=sys.stderr)
        return
    import urllib.parse

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for i in range(0, len(text), 3500):
        chunk = text[i:i + 3500]
        data = urllib.parse.urlencode({"chat_id": chat_id, "text": chunk}).encode()
        try:
            urllib.request.urlopen(urllib.request.Request(url, data=data, method="POST"), timeout=15).read()
        except Exception as e:
            print(f"! telegram send failed: {e}", file=sys.stderr)


def main():
    state = load_state()
    watchlist = load_watchlist()
    new_events = []
    film_urls = {}

    # 1. New movies in coming-soon / now-playing feeds
    for feed_name in ("coming-soon", "now-playing"):
        try:
            current = get_feed(feed_name)
        except Exception as e:
            print(f"! failed to fetch {feed_name}: {e}", file=sys.stderr)
            continue
        prev = state["feeds"].get(feed_name, {})
        for code, p in current.items():
            film_urls[code] = p["url"]
            if code not in prev:
                started = p.get("dateStarted")
                started_str = f" (יציאה: {ltr(display_date(started[:10]))})" if started else ""
                new_events.append(f"סרט חדש [{FEED_LABELS.get(feed_name, feed_name)}]: {p['featureTitle']}{started_str}\n{ltr(p['url'])}")
        state["feeds"][feed_name] = {code: {"featureTitle": p["featureTitle"], "url": p["url"]} for code, p in current.items()}

    # 2. Booking horizon per cinema + watchlist detection (respecting per-film IMAX-only flag)
    today = date.today()
    for cinema_id, cinema_name in CINEMAS.items():
        last_open = None
        watch_hits = {code: set() for code in watchlist}
        for i in range(HORIZON_DAYS):
            day = (today + timedelta(days=i)).isoformat()
            try:
                films = get_day_films(cinema_id, day)
            except Exception as e:
                print(f"! failed {cinema_id} {day}: {e}", file=sys.stderr)
                continue
            if films:
                last_open = day
            for code, meta in watchlist.items():
                film = films.get(code)
                if not film:
                    continue
                if meta.get("imax") and "imax" not in film["attributes"]:
                    continue
                watch_hits[code].add(day)

        prev_horizon = state["horizon"].get(cinema_id)
        if last_open and last_open != prev_horizon:
            was = ltr(display_date(prev_horizon)) if prev_horizon else "אף פעם"
            new_events.append(f"{rtl(cinema_name)}: אפשר להזמין עד {ltr(display_date(last_open))} (היה עד {was})")
        state["horizon"][cinema_id] = last_open

        for code, meta in watchlist.items():
            key = f"{cinema_id}:{code}"
            prev_days = set(state["watch_seen"].get(key, []))
            newly = sorted(d for d in watch_hits[code] if d not in prev_days)
            if newly:
                imax_only = meta.get("imax")
                lines = [f"{rtl(meta['name'])} {ltr(f'({code})')}"]
                dates_label = "תאריכי IMAX חדשים" if imax_only else "אפשר להזמין (ראשון לציון) מתאריכים חדשים"
                lines.append(f"{dates_label}: {ltr(', '.join(display_date(d) for d in newly))}")
                url = film_urls.get(code)
                if url:
                    link = f"{url}#/buy-tickets-by-film?for-movie={code}&in-cinema={cinema_id}&at={newly[0]}&view-mode=list"
                    lines.append(f"לך תזמין אח\n{ltr(link)}")
                new_events.append("\n".join(lines))
            state["watch_seen"][key] = sorted(watch_hits[code])

    save_json(STATE_FILE, state)

    if new_events:
        message = "\n".join(new_events)
        print(message)
        send_telegram(message)
    else:
        print("No changes since last check.")


if __name__ == "__main__":
    main()
