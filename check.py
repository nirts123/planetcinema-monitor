#!/usr/bin/env python3
"""
Planet Cinema (planetcinema.co.il) booking-window monitor.

Planet's site is a Vista Cinema storefront. Two undocumented JSON APIs matter:

- Coming-soon / now-playing feed (movie catalog, used for /movies search):
  https://www.planetcinema.co.il/il/data-api-service/v1/feed/10100/byName/{coming-soon|now-playing}?lang=he_IL

- Per-cinema, per-day showtime listing (what's actually bookable "now"):
  https://www.planetcinema.co.il/il/data-api-service/v1/quickbook/10100/film-events/in-cinema/{cinemaId}/at-date/{YYYY-MM-DD}?attr=&lang=he_IL
  -> body.films is a list of films with sessions that day; each film has
     "id" (film code, e.g. "7460s2r" = The Odyssey) and "attributeIds"
     (includes format tags like "imax", "4dx", "vip" when that cinema shows
     the film in that format that day).

Planet only publishes bookable sessions a week or two ahead; new dates get
added periodically (this site: roughly weekly). This script detects:
  1. New movies appearing in the coming-soon/now-playing feeds.
  2. The booking horizon (last date with any sessions) advancing per cinema.
  3. A watched movie (optionally: in IMAX specifically) newly becoming bookable.

The watchlist (watchlist.json) is managed entirely via Telegram commands:
  /movies [search text]        browse/search the catalog, numbered for /watch
  /watch <number|code> [imax]  add a film from the last /movies list (or by
                                code/url directly); add "imax" to only alert
                                on IMAX screenings of that film
  /unwatch <code>               remove a film
  /list                         show current watchlist

State (feed/horizon baselines, Telegram update offset, last /movies listing)
is kept in state.json; run this periodically (cron/GitHub Actions) and it
prints/pushes only what's NEW since the last run.
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
}

HORIZON_DAYS = 21
MOVIES_LIST_LIMIT = 25


def fetch(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
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


def load_json(path, default):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return default


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_state():
    return load_json(STATE_FILE, {"feeds": {}, "horizon": {}, "watch_seen": {}, "telegram_offset": 0, "last_movie_list": []})


def load_watchlist():
    """Normalizes old plain-string entries ({code: "name"}) to the current
    {code: {"name": ..., "imax": bool}} shape."""
    raw = load_json(WATCHLIST_FILE, {"7460s2r": {"name": "The Odyssey / האודיסאה", "imax": False}})
    watchlist = {}
    for code, val in raw.items():
        if isinstance(val, str):
            watchlist[code] = {"name": val, "imax": False}
        else:
            watchlist[code] = {"name": val.get("name", code), "imax": bool(val.get("imax"))}
    return watchlist


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


def search_catalog(all_feed_entries, query):
    if not query:
        results = list(all_feed_entries)
    else:
        q = query.lower()
        results = [
            e for e in all_feed_entries
            if q in e["featureTitle"].lower() or q in e["url"].lower()
        ]
    # De-dupe by code (same film can appear in both feeds).
    seen = set()
    deduped = []
    for e in results:
        if e["code"] in seen:
            continue
        seen.add(e["code"])
        deduped.append(e)
    return deduped


def format_movie_list(entries):
    lines = []
    for i, e in enumerate(entries, start=1):
        tag = " [IMAX]" if "imax" in e.get("attributes", []) else ""
        lines.append(f"{i}. {e['featureTitle']}{tag}")
    return "\n".join(lines)


def resolve_watch_target(arg, last_movie_list, all_feed_entries):
    """arg like '3', '3 imax', '7460s2r', '7460s2r imax', or a full URL
    (optionally followed by 'imax'). Returns (code, name, imax_only) or None."""
    tokens = arg.strip().split()
    if not tokens:
        return None
    imax_only = tokens[-1].lower() == "imax"
    if imax_only:
        tokens = tokens[:-1]
    if not tokens:
        return None
    ref = " ".join(tokens)

    if ref.isdigit() and last_movie_list:
        idx = int(ref) - 1
        if 0 <= idx < len(last_movie_list):
            entry = last_movie_list[idx]
            return entry["code"], entry["featureTitle"], imax_only
        return None

    code = extract_film_code(ref)
    for e in all_feed_entries:
        if e["code"] == code:
            return code, e["featureTitle"], imax_only
    return code, code, imax_only


def process_telegram_commands(state, watchlist, all_feed_entries):
    """Poll Telegram for /movies, /watch, /unwatch, /list commands."""
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

        if cmd == "/movies":
            results = search_catalog(all_feed_entries, arg)[:MOVIES_LIST_LIMIT]
            state["last_movie_list"] = [{"code": e["code"], "featureTitle": e["featureTitle"]} for e in results]
            if results:
                body = format_movie_list(results)
                send_telegram(f"{body}\n\nReply e.g. /watch 3  or  /watch 3 imax", reply_to=msg_id)
            else:
                send_telegram("No matching movies.", reply_to=msg_id)

        elif cmd == "/watch" and arg:
            resolved = resolve_watch_target(arg, state.get("last_movie_list", []), all_feed_entries)
            if not resolved:
                send_telegram("Couldn't resolve that. Try /movies <search> first, then /watch <number>.", reply_to=msg_id)
            else:
                code, name, imax_only = resolved
                watchlist[code] = {"name": name, "imax": imax_only}
                changed = True
                suffix = " (IMAX only)" if imax_only else ""
                send_telegram(f"Watching: {name}{suffix} ({code})", reply_to=msg_id)

        elif cmd == "/unwatch" and arg:
            code = extract_film_code(arg.split()[0])
            if code in watchlist:
                name = watchlist.pop(code)["name"]
                changed = True
                send_telegram(f"Stopped watching: {name} ({code})", reply_to=msg_id)
            else:
                send_telegram(f"Not on watchlist: {code}", reply_to=msg_id)

        elif cmd == "/list":
            if watchlist:
                lines = []
                for code, meta in watchlist.items():
                    suffix = " [IMAX only]" if meta.get("imax") else ""
                    lines.append(f"- {meta['name']}{suffix} ({code})")
                send_telegram("Watchlist:\n" + "\n".join(lines), reply_to=msg_id)
            else:
                send_telegram("Watchlist is empty.", reply_to=msg_id)

    return changed


def main():
    state = load_state()
    watchlist = load_watchlist()
    new_events = []

    # 1. New movies in coming-soon / now-playing feeds; also the catalog used
    #    by /movies and to resolve film titles for newly /watch-ed codes.
    all_feed_entries = []
    for feed_name in ("coming-soon", "now-playing"):
        try:
            current = get_feed(feed_name)
        except Exception as e:
            print(f"! failed to fetch {feed_name}: {e}", file=sys.stderr)
            continue
        prev = state["feeds"].get(feed_name, {})
        for code, p in current.items():
            all_feed_entries.append({"code": code, "featureTitle": p["featureTitle"], "url": p["url"], "attributes": p["attributes"]})
            if code not in prev:
                new_events.append(f"[{feed_name}] NEW: {p['featureTitle']} -> {p['url']} (dateStarted={p.get('dateStarted')})")
        state["feeds"][feed_name] = {code: {"featureTitle": p["featureTitle"], "url": p["url"]} for code, p in current.items()}

    # 2. Pick up /movies, /watch, /unwatch, /list commands sent to the bot.
    watchlist_changed = process_telegram_commands(state, watchlist, all_feed_entries)
    if watchlist_changed:
        save_json(WATCHLIST_FILE, watchlist)

    # 3. Booking horizon per cinema + watchlist detection (respecting per-film IMAX-only flag)
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
            new_events.append(f"[{cinema_name}] booking horizon now open through {last_open} (was {prev_horizon})")
        state["horizon"][cinema_id] = last_open

        for code, meta in watchlist.items():
            key = f"{cinema_id}:{code}"
            prev_days = set(state["watch_seen"].get(key, []))
            newly = sorted(d for d in watch_hits[code] if d not in prev_days)
            if newly:
                suffix = " [IMAX]" if meta.get("imax") else ""
                new_events.append(f"[{cinema_name}] {meta['name']}{suffix}: newly bookable on {', '.join(newly)}")
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
