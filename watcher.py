#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Yes Planet IMAX watcher — "The Odyssey" @ Rishon LeZion (cinema 1072)
Alerts on Telegram when a NEW screening appears between 18:00-23:00
with at least MIN_SEATS free seats.
State is kept in seen_events.json (committed back by GitHub Actions).
"""
import json
import os
import sys
import datetime as dt
import urllib.request

# ---- Config ----
TENANT = "10100"
CINEMA_ID = "1072"                 # Rishon LeZion
FILM_ID = "7460s2r"                # The Odyssey
BASE = f"https://www.planetcinema.co.il/il/data-api-service/v1/quickbook/{TENANT}"
LANG = "he_IL"
DAYS_AHEAD = 30                    # how far ahead to scan
HOUR_MIN, HOUR_MAX = 18, 23        # 18:00 <= showtime <= 23:00
MIN_SEATS = 4
STATE_FILE = "seen_events.json"

TG_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TG_CHAT = os.environ["TELEGRAM_CHAT_ID"]

HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}


def get_json(url):
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def send_telegram(text):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    data = json.dumps({
        "chat_id": TG_CHAT,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=30)


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1, sort_keys=True)


def main():
    state = load_state()
    today = dt.date.today()

    # 1) which dates have screenings for this cinema
    until = today + dt.timedelta(days=DAYS_AHEAD)
    dates_url = f"{BASE}/dates/in-cinema/{CINEMA_ID}/until/{until}?attr=imax&lang={LANG}"
    try:
        dates = get_json(dates_url)["body"]["dates"]
    except Exception as e:
        print(f"dates fetch failed: {e}", file=sys.stderr)
        sys.exit(1)

    alerts = []
    for date_str in dates:
        url = f"{BASE}/film-events/in-cinema/{CINEMA_ID}/at-date/{date_str}?attr=&lang={LANG}"
        try:
            body = get_json(url)["body"]
        except Exception as e:
            print(f"skip {date_str}: {e}", file=sys.stderr)
            continue

        for ev in body.get("events", []):
            if ev.get("filmId") != FILM_ID:
                continue
            attrs = [a.lower() for a in ev.get("attributeIds", [])]
            if "imax" not in attrs:
                continue

            start = dt.datetime.fromisoformat(ev["eventDateTime"])
            if not (HOUR_MIN <= start.hour <= HOUR_MAX):
                continue

            ev_id = ev["id"]
            sold_out = ev.get("soldOut", False)
            ratio = ev.get("availabilityRatio", 0) or 0
            # seat count estimate: IMAX Rishon ~ 400 seats; ratio resolution ~1 seat
            est_free = round(ratio * ev.get("auditoriumTinyName_seatCount", 0) or ratio * 400)
            ok = (not sold_out) and est_free >= MIN_SEATS

            prev = state.get(ev_id)
            is_new = prev is None
            became_ok = prev is not None and not prev.get("ok") and ok

            if ok and (is_new or became_ok):
                link = ev.get("bookingLink") or ev.get("bookingRouterLaunchLink") or \
                    f"https://www.planetcinema.co.il/il/booking-router/launch/{ev_id}?lang=he"
                when = start.strftime("%d/%m/%Y %H:%M")
                tag = "🆕 הקרנה חדשה" if is_new else "🎟️ התפנו מקומות"
                alerts.append(
                    f"{tag} — האודיסאה IMAX ראשל\"צ\n"
                    f"🗓 {when}\n"
                    f"💺 כ-{est_free} מושבים פנויים\n"
                    f"🔗 {link}"
                )

            state[ev_id] = {"ok": ok, "dt": ev["eventDateTime"], "free": est_free}

    save_state(state)

    if alerts:
        send_telegram("\n\n".join(alerts))
        print(f"sent {len(alerts)} alert(s)")
    else:
        print("no new matching events")


if __name__ == "__main__":
    main()
