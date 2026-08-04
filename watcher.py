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
HOUR_MIN, HOUR_MAX = 18, 23        # 18:00 <= showtime <= 23:00
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
    requests.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        json={"chat_id": TG_CHAT, "text": text,
              "disable_web_page_preview": True},
        timeout=30,
    ).raise_for_status()


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
    until = dt.date.today() + dt.timedelta(days=DAYS_AHEAD)
    dates = quickbook_get(
        session, f"{BASE}/dates/in-cinema/{CINEMA_ID}/until/{until}?attr=imax&lang={LANG}"
    )["dates"]

    events = []
    for date_str in dates:
        try:
            body = quickbook_get(
                session,
                f"{BASE}/film-events/in-cinema/{CINEMA_ID}/at-date/{date_str}?attr=&lang={LANG}",
            )
        except Exception as e:
            print(f"skip {date_str}: {e}", file=sys.stderr)
            continue
        for ev in body.get("events", []):
            if ev.get("filmId") != FILM_ID:
                continue
            if "imax" not in [a.lower() for a in ev.get("attributeIds", [])]:
                continue
            start = dt.datetime.fromisoformat(ev["eventDateTime"])
            if HOUR_MIN <= start.hour <= HOUR_MAX:
                events.append(ev)
    return events


# ---------- Seat map (tickets5) ----------

def open_ticket_session(session, event_id):
    """Follow booking link -> order page. Returns presentationId or None."""
    launch = f"https://www.planetcinema.co.il/il/booking-router/launch/{event_id}?lang=he"
    r = session.get(launch, headers={"User-Agent": UA}, timeout=30,
                    allow_redirects=True)
    m = re.search(r"/order/(\d+)", r.url)
    if not m:
        # sometimes the id is inside the final HTML
        m = re.search(r"/order/(\d+)", r.text or "")
    pid = m.group(1) if m else None

    # br page redirects to tickets5 via JS; visit the order page explicitly
    # so tickets5 issues its session cookies
    if pid:
        try:
            opr = session.get(f"{TICKETS}/order/{pid}?lang=he", timeout=30,
                              headers={"User-Agent": UA,
                                       "Accept": "text/html,application/xhtml+xml"})
            print(f"  order page -> {opr.status_code}")
        except Exception as oe:
            print(f"  order page failed: {oe}")

    # mimic the SPA: uuid cookie matching the uuid header + config bootstrap
    u = str(uuidlib.uuid4())
    session.headers["x-watcher-uuid"] = u  # stash for ticket_api
    session.cookies.set("uuid", u, domain="tickets5.planetcinema.co.il")
    session.cookies.set("lang", "iw_IL", domain="tickets5.planetcinema.co.il")
    if pid:
        for cfg_path in ("/api/config", f"/api/config?presentationId={pid}"):
            try:
                cr = session.get(f"{TICKETS}{cfg_path}", timeout=30, headers={
                    "User-Agent": UA,
                    "Accept": "application/json, text/plain, */*",
                    "Referer": f"{TICKETS}/order/{pid}?lang=he",
                    "uuid": u,
                })
                print(f"  config {cfg_path} -> {cr.status_code}")
                if cr.ok:
                    break
            except Exception as ce:
                print(f"  config {cfg_path} failed: {ce}")
    print(f"  landed: {r.url} | cookies: {sorted(c.name for c in session.cookies)}")
    return pid


def ticket_api(session, path, presentation_id):
    headers = {
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Referer": f"{TICKETS}/order/{presentation_id}?lang=he",
        "uuid": session.headers.get("x-watcher-uuid", str(uuidlib.uuid4())),
    }
    r = session.get(f"{TICKETS}{path}", headers=headers, timeout=30)
    if not r.ok:
        print(f"  {path} -> {r.status_code}: {r.text[:150]!r}")
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

    try:
        plan = ticket_api(session, f"/api/seats/seatplanV2?venueId={venue_id}&seatplanId={seatplan_id}",
                          presentation_id)
    except Exception:
        plan = ticket_api(
            session,
            f"/api/seats/seatplanV2?venueId={venue_id}&seatplanId={seatplan_id}&venueTypeId=2",
            presentation_id)
    status = ticket_api(
        session,
        f"/api/seats/seats-statusV2?presentationId={presentation_id}&venueTypeId=2&isReserved=1",
        presentation_id)

    free = set(status.get("seats", {}).keys())  # keys "area_seatKey_rowKey", listed = free

    best_run, best_row = 0, None
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
                    if run > best_run:
                        best_run, best_row = run, row.get("n")
    return best_run, best_row


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
                run, row_name = check_adjacent_seats(ts, pid)
                ok = run >= MIN_ADJACENT
                detail = f"💺 {run} צמודים בשורה {row_name} (שורה {ROW_MIN}+)" if ok else \
                         f"אין {MIN_ADJACENT} צמודים משורה {ROW_MIN} (מקס' {run})"
                print(f"{ev_id} {when}: seatmap run={run} row={row_name}")
            except Exception as e:
                # fallback: total-free estimate
                ok = est_free >= FALLBACK_MIN_SEATS
                detail = f"💺 כ-{est_free} פנויים (בדיקת שורות לא זמינה)"
                print(f"{ev_id} seatmap failed ({e}); fallback est_free={est_free}",
                      file=sys.stderr)

        prev = state.get(ev_id)
        is_new = prev is None
        became_ok = prev is not None and not prev.get("ok") and ok

        if ok and (is_new or became_ok):
            tag = "🆕 הקרנה חדשה" if is_new else "🎟️ התפנו מקומות מתאימים"
            alerts.append(f"{tag} — האודיסאה IMAX ראשל\"צ\n🗓 {when}\n{detail}\n🔗 {link}")

        state[ev_id] = {"ok": ok, "dt": ev["eventDateTime"]}

    save_state(state)

    if alerts:
        send_telegram("\n\n".join(alerts))
        print(f"sent {len(alerts)} alert(s)")
    else:
        print("no new matching events")


if __name__ == "__main__":
    main()
