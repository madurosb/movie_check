#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Yes Planet IMAX watcher v2 — "The Odyssey" @ Rishon LeZion (cinema 1072)

Pipeline per screening (18:00-23:00, IMAX only):
  1. Main quickbook API -> new/changed events
  2. Booking link redirect -> presentationId on tickets5.planetcinema.co.il
  3. Seat APIs (seatplanV2 + seats-statusV2) -> real seat map
  4. Check: >= MIN_ADJACENT adjacent free seats in row ROW_MIN or higher
  5. Telegram alert

If step 2-4 fails (e.g. Cloudflare blocks the runner), falls back to the
availabilityRatio estimate so an alert is never missed.
"""
import json
import os
import re
import sys
import uuid as uuidlib
import datetime as dt

import requests

# ---- Config ----
TENANT = "10100"
CINEMA_ID = "1072"                 # Rishon LeZion
FILM_ID = "7460s2r"                # The Odyssey
BASE = f"https://www.planetcinema.co.il/il/data-api-service/v1/quickbook/{TENANT}"
LANG = "he_IL"
TICKETS = "https://tickets5.planetcinema.co.il"
DAYS_AHEAD = 30
# Allowed start-time window per weekday (start_time, latest_start_time).
# Mon-Thu + Sun: evening only. Friday: all day (day off).
# Saturday: all day but nothing starting after 23:00 (work next morning).
WINDOWS = {
    0: (dt.time(17, 59), dt.time(23, 0)),   # Monday
    1: (dt.time(17, 59), dt.time(23, 0)),   # Tuesday
    2: (dt.time(17, 59), dt.time(23, 0)),   # Wednesday
    3: (dt.time(17, 59), dt.time(23, 0)),   # Thursday
    4: (dt.time(0, 0), dt.time(23, 59)),    # Friday — all day
    5: (dt.time(0, 0), dt.time(23, 0)),     # Saturday — all day until 23:00
    6: (dt.time(17, 59), dt.time(23, 0)),   # Sunday
}


def in_window(start):
    lo, hi = WINDOWS[start.weekday()]
    return lo <= start.time() <= hi

ROW_MIN = 6                        # count adjacency only from this row number up
MIN_ADJACENT = 4                   # need this many adjacent free seats
FALLBACK_MIN_SEATS = 4             # fallback threshold when seat map unavailable
STATE_FILE = "seen_events.json"

TG_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TG_CHAT = os.environ["TELEGRAM_CHAT_ID"]

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36")


# ---------- Telegram ----------

def send_telegram(text):
    """Send to every chat id. Returns True if at least one delivery succeeded."""
    ok_any = False
    for chat in TG_CHAT.split(","):
        chat = chat.strip()
        if not chat:
            continue
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
                json={"chat_id": chat, "text": text,
                      "disable_web_page_preview": True},
                timeout=30,
            )
            if r.ok:
                ok_any = True
                print(f"  telegram -> {chat}: OK")
            else:
                print(f"  telegram -> {chat}: FAILED {r.status_code} {r.text[:200]}",
                      file=sys.stderr)
        except Exception as e:
            print(f"  telegram -> {chat}: EXCEPTION {e}", file=sys.stderr)
    return ok_any


# ---------- State ----------

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1, sort_keys=True)


# ---------- Main quickbook API ----------

def quickbook_get(session, url):
    r = session.get(url, headers={"User-Agent": UA, "Accept": "application/json"},
                    timeout=30)
    r.raise_for_status()
    return r.json()["body"]


def collect_candidate_events(session):
    """All Odyssey IMAX events at 18:00-23:00 in the next DAYS_AHEAD days."""
    # scan every calendar day directly — the /dates endpoint returns a
    # filtered subset and was hiding screenings from us
    today = dt.date.today()
    dates = [str(today + dt.timedelta(days=i)) for i in range(DAYS_AHEAD + 1)]

    events = []
    for date_str in dates:
        try:
            body = quickbook_get(
                session,
                f"{BASE}/film-events/in-cinema/{CINEMA_ID}/at-date/{date_str}?attr=&lang={LANG}",
            )
        except Exception:
            continue  # no screenings that day
        for ev in body.get("events", []):
            if ev.get("filmId") != FILM_ID:
                continue
            if "imax" not in [a.lower() for a in ev.get("attributeIds", [])]:
                continue
            start = dt.datetime.fromisoformat(ev["eventDateTime"])
            ok_time = in_window(start)
            print(f"  found {ev['id']} {start:%a %d/%m %H:%M} "
                  f"{'IN window' if ok_time else 'outside window'}")
            if ok_time:
                events.append(ev)
    print(f"total in window: {len(events)}")
    return events


# ---------- Seat map (tickets5) ----------

def open_ticket_session(session, event_id):
    """Follow booking link -> extract presentationId."""
    launch = f"https://www.planetcinema.co.il/il/booking-router/launch/{event_id}?lang=he"
    r = session.get(launch, headers={"User-Agent": UA}, timeout=30,
                    allow_redirects=True)
    m = re.search(r"/order/(\d+)", r.url) or re.search(r"/order/(\d+)", r.text or "")
    return m.group(1) if m else None


def ticket_api(session, path, presentation_id, method="GET"):
    headers = {
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Origin": TICKETS,
        "Referer": f"{TICKETS}/order/{presentation_id}?lang=he",
        "uuid": str(uuidlib.uuid4()),
    }
    if method == "POST":
        r = session.post(f"{TICKETS}{path}", headers=headers, json={}, timeout=30)
    else:
        r = session.get(f"{TICKETS}{path}", headers=headers, timeout=30)
    if not r.ok:
        print(f"  {method} {path} -> {r.status_code}: {r.text[:120]!r}")
    r.raise_for_status()
    return r.json()



def find_key(obj, names):
    """Recursively find first value whose key (lowercased) is in names."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.lower() in names and v not in (None, "", 0):
                return v
        for v in obj.values():
            r = find_key(v, names)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = find_key(v, names)
            if r is not None:
                return r
    return None

def check_adjacent_seats(session, presentation_id):
    """
    Returns (best_run, row_name) — the longest run of adjacent free seats
    in rows >= ROW_MIN (wheelchair spots excluded), or (0, None).
    Raises on any API failure (caller falls back).
    """
    pres = ticket_api(session, f"/api/presentations/{presentation_id}?referralMiniSiteId=0",
                      presentation_id)
    venue_id = find_key(pres, {"venueid"})
    seatplan_id = find_key(pres, {"seatplanid"}) or 1
    if not venue_id:
        raise RuntimeError(f"venueId not found; pres keys: {list(pres)[:20]}")

    plan = ticket_api(session,
                      f"/api/seats/seatplanV2?venueId={venue_id}&seatplanId={seatplan_id}",
                      presentation_id, method="POST")
    status = ticket_api(
        session,
        f"/api/seats/seats-statusV2?presentationId={presentation_id}&venueTypeId=2&isReserved=1",
        presentation_id)

    free = set(status.get("seats", {}).keys())  # keys "area_seatKey_rowKey", listed = free

    best_run, best_row = 0, None
    free_in_rows = 0
    for area_key, area in plan.get("S", {}).items():
        for group in area.get("G", {}).values():
            for row_key, row in group.get("R", {}).items():
                try:
                    row_num = int(row.get("n", "0"))
                except ValueError:
                    continue
                if row_num < ROW_MIN:
                    continue
                # seat keys are consecutive integers when physically adjacent
                seat_keys = sorted(
                    int(k) for k, s in row.get("S", {}).items()
                    if not s.get("hc")  # skip wheelchair spots
                )
                run = 0
                prev = None
                for sk in seat_keys:
                    is_free = f"{area_key}_{sk}_{row_key}" in free
                    contiguous = prev is not None and sk == prev + 1
                    run = (run + 1) if (is_free and (run == 0 or contiguous)) else (1 if is_free else 0)
                    prev = sk
                    if is_free:
                        free_in_rows += 1
                    if run > best_run:
                        best_run, best_row = run, row.get("n")
    return best_run, best_row, len(free), free_in_rows


# ---------- Main ----------

def main():
    state = load_state()
    qb = requests.Session()

    try:
        events = collect_candidate_events(qb)
    except Exception as e:
        print(f"quickbook failed: {e}", file=sys.stderr)
        sys.exit(1)

    alerts = []
    for ev in events:
        ev_id = ev["id"]
        start = dt.datetime.fromisoformat(ev["eventDateTime"])
        when = start.strftime("%d/%m/%Y %H:%M")
        sold_out = ev.get("soldOut", False)
        ratio = ev.get("availabilityRatio", 0) or 0
        est_free = round(ratio * 400)
        link = (ev.get("bookingLink") or
                f"https://www.planetcinema.co.il/il/booking-router/launch/{ev_id}?lang=he"
                ).replace("/api/order/", "/order/")

        # --- decide "ok" ---
        ok, detail = False, ""
        if not sold_out and est_free >= 1:
            ts = requests.Session()  # fresh anonymous session per event
            try:
                pid = open_ticket_session(ts, ev_id)
                if not pid:
                    raise RuntimeError("no presentationId in redirect")
                run, row_name, free_total, free_rows = check_adjacent_seats(ts, pid)
                ok = run >= MIN_ADJACENT
                if ok:
                    detail = (f"💺 {run} צמודים בשורה {row_name} | {free_rows} פנויים משורה {ROW_MIN}+ | {free_total} באולם\n"
                              f"💺 {run} adjacent in row {row_name} | {free_rows} free from row {ROW_MIN}+ | {free_total} in hall")
                else:
                    detail = (f"💺 אין {MIN_ADJACENT} צמודים (מקס' {run}) | {free_rows} פנויים משורה {ROW_MIN}+ | {free_total} באולם\n"
                              f"💺 no {MIN_ADJACENT} adjacent (max {run}) | {free_rows} free from row {ROW_MIN}+ | {free_total} in hall")
                print(f"{ev_id} {when}: run={run} row={row_name} "
                      f"free_rows={free_rows} free_total={free_total}")
            except Exception as e:
                # fallback: total-free estimate
                ok = est_free >= FALLBACK_MIN_SEATS
                detail = (f"💺 כ-{est_free} פנויים (בדיקת שורות לא זמינה)\n"
                          f"💺 ~{est_free} free (row check unavailable)")
                print(f"{ev_id} seatmap failed ({e}); fallback est_free={est_free}",
                      file=sys.stderr)

        prev = state.get(ev_id)
        is_new = prev is None
        became_ok = prev is not None and not prev.get("ok") and ok

        if is_new or (became_ok and ok):
            if is_new and ok:
                tag = "🔥 הקרנה חדשה — יש מקומות! / New screening — seats available!"
            elif is_new:
                tag = "🆕 הקרנה חדשה נפתחה (בלי 4 צמודים כרגע) / New screening opened (no 4 adjacent yet)"
            else:
                tag = "🎟️ התפנו מקומות מתאימים / Matching seats freed up"
            alerts.append(f"{tag}\nהאודיסאה IMAX ראשל\"צ / The Odyssey IMAX Rishon LeZion"
                          f"\n🗓 {when}\n{detail}\n🔗 {link}")

        state[ev_id] = {"ok": ok, "dt": ev["eventDateTime"]}

    # daily heartbeat: one status message per day, first run after 06:00 UTC (09:00 IL)
    now = dt.datetime.now(dt.timezone.utc)
    today = str(now.date())
    if not alerts and now.hour >= 6 and state.get("_heartbeat") != today:
        n_ev = len(events)
        alerts.append(f"💓 המערכת פעילה — נבדקו {n_ev} הקרנות, אין שינוי מתאים\n"
                      f"💓 Watcher alive — {n_ev} screenings checked, no matching change")
        state["_heartbeat"] = today
    elif alerts:
        state["_heartbeat"] = today

    if alerts:
        delivered = True
        # telegram caps messages at 4096 chars - send in small batches
        for i in range(0, len(alerts), 4):
            chunk = "\n\n".join(alerts[i:i + 4])
            if not send_telegram(chunk):
                delivered = False
        if delivered:
            print(f"delivered {len(alerts)} alert(s)")
            save_state(state)
        else:
            print("DELIVERY FAILED - state not saved, will retry next run",
                  file=sys.stderr)
            sys.exit(1)
    else:
        print("no new matching events")
        save_state(state)


if __name__ == "__main__":
    main()
