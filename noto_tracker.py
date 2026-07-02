#!/usr/bin/env python3
"""
noto_tracker.py — GitHub Actions edition
Texts your Google Fi phone after every hole David Noto completes
at the 2026 U.S. Senior Open, via Gmail -> msg.fi.google.com.

Config comes from environment variables (set as GitHub repo secrets):
  GMAIL_ADDRESS, GMAIL_APP_PASSWORD, FI_NUMBER
Optional env vars:
  PLAYER_LAST_NAME (default "Noto"), POLL_SECONDS (default 120),
  MAX_RUNTIME_SECONDS (default 20700 = 5h45m, safely under GitHub's 6h job cap)

Behavior designed for stateless cloud runs:
  * On startup, the FIRST successful poll is a silent baseline — you get one
    "tracker live" status text, then per-hole texts from that point on.
    (So a restarted/late-starting job never spams catch-up messages.)
  * Exits 0 when the player's round is final, or at MAX_RUNTIME.
  * If the player is already finished when the job starts, exits quietly.
"""

import json
import os
import smtplib
import ssl
import sys
import time
import urllib.request
from email.message import EmailMessage

# ---------------- config from environment ----------------
PLAYER_LAST_NAME = os.environ.get("PLAYER_LAST_NAME", "Noto")
GMAIL_ADDRESS    = os.environ["GMAIL_ADDRESS"]
GMAIL_APP_PASS   = os.environ["GMAIL_APP_PASSWORD"]
FI_NUMBER        = os.environ["FI_NUMBER"]
POLL_SECONDS     = int(os.environ.get("POLL_SECONDS", "120"))
MAX_RUNTIME      = int(os.environ.get("MAX_RUNTIME_SECONDS", "20700"))
# ----------------------------------------------------------

SCOREBOARD_URL = (
    "https://site.api.espn.com/apis/site/v2/sports/golf/champions-tour/scoreboard"
)
SMS_ADDRESS = f"{FI_NUMBER}@msg.fi.google.com"

HOLE_LABELS = {
    -3: "ALBATROSS!!", -2: "EAGLE!", -1: "Birdie",
     0: "Par", 1: "Bogey", 2: "Double bogey", 3: "Triple bogey",
}


def label_for(delta: int) -> str:
    return HOLE_LABELS.get(delta, f"{delta:+d} on the hole")


def fmt_today(today: int) -> str:
    return "E" if today == 0 else f"{today:+d}"


def fetch_scoreboard() -> dict:
    req = urllib.request.Request(
        SCOREBOARD_URL, headers={"User-Agent": "Mozilla/5.0 (hole-tracker)"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def parse_rel_to_par(text):
    if text is None:
        return None
    s = str(text).strip()
    if s in ("E", "e"):
        return 0
    try:
        return int(s)
    except ValueError:
        return None


def find_player(data: dict, last_name: str):
    target = last_name.lower()
    for event in data.get("events", []):
        for comp in event.get("competitions", []):
            for player in comp.get("competitors", []):
                name = (
                    player.get("athlete", {}).get("displayName", "")
                    or player.get("athlete", {}).get("fullName", "")
                )
                if target in name.lower():
                    return event.get("name", "U.S. Senior Open"), player
    return None, None


def extract_status(player: dict):
    status = player.get("status", {}) or {}
    thru = status.get("thru")
    period = status.get("period") or 1
    state_desc = (status.get("type", {}) or {}).get("name", "")

    linescores = player.get("linescores", []) or []
    today = None
    if linescores:
        current = linescores[-1]
        today = parse_rel_to_par(current.get("displayValue") or current.get("value"))

    total = player.get("score")
    if isinstance(total, dict):
        total = total.get("displayValue")
    total = str(total) if total is not None else "?"

    finished = "final" in state_desc.lower() or str(thru).upper() in ("F", "18")
    try:
        thru = int(thru)
    except (TypeError, ValueError):
        thru = 18 if finished else None

    return period, thru, today, total, finished


def per_hole_detail(player: dict, round_index: int, hole_number: int):
    try:
        rnd = player["linescores"][round_index]
        for h in rnd.get("linescores", []):
            if int(h.get("period", 0)) == hole_number:
                return int(h.get("value")), int(h.get("par", 0)) or None
    except (KeyError, IndexError, TypeError, ValueError):
        pass
    return None, None


def send_text(body: str):
    msg = EmailMessage()
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = SMS_ADDRESS
    msg["Subject"] = ""
    msg.set_content(body[:450])
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx) as server:
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASS.replace(" ", ""))
        server.send_message(msg)
    print(f"  -> TEXT SENT: {body}", flush=True)


def main():
    start = time.time()
    print(f"Tracking {PLAYER_LAST_NAME}; texting {FI_NUMBER}@msg.fi... "
          f"poll={POLL_SECONDS}s, max runtime={MAX_RUNTIME}s", flush=True)

    baselined = False
    state = {}

    while time.time() - start < MAX_RUNTIME:
        try:
            data = fetch_scoreboard()
            event_name, player = find_player(data, PLAYER_LAST_NAME)

            if player is None:
                print(time.strftime("%H:%M"), "- player not on scoreboard "
                      "(round not posted yet, or missed cut). Waiting...", flush=True)
            else:
                rnd, thru, today, total, finished = extract_status(player)

                if not baselined:
                    if finished:
                        print("Round already final at startup; nothing to do.", flush=True)
                        return
                    state = {"round": rnd, "thru": thru or 0, "today": today or 0}
                    baselined = True
                    shown = thru if thru is not None else 0
                    send_text(f"Tracker live: {PLAYER_LAST_NAME} R{rnd}, "
                              f"thru {shown}, {fmt_today(today or 0)} today, "
                              f"{total} total. Per-hole texts from here on.")
                else:
                    if state.get("round") != rnd:
                        state = {"round": rnd, "thru": 0, "today": 0}

                    prev_thru = state.get("thru") or 0
                    prev_today = state.get("today") or 0

                    if thru is not None and today is not None and thru > prev_thru:
                        delta_total = today - prev_today
                        holes_done = list(range(prev_thru + 1, thru + 1))

                        if len(holes_done) == 1:
                            hole = holes_done[0]
                            strokes, par = per_hole_detail(player, rnd - 1, hole)
                            if strokes and par:
                                body = (f"{PLAYER_LAST_NAME} R{rnd} hole {hole} "
                                        f"(par {par}): {strokes} — "
                                        f"{label_for(strokes - par)}. Thru {thru}, "
                                        f"{fmt_today(today)} today, {total} total.")
                            else:
                                body = (f"{PLAYER_LAST_NAME} R{rnd} hole {hole}: "
                                        f"{label_for(delta_total)}. Thru {thru}, "
                                        f"{fmt_today(today)} today, {total} total.")
                        else:
                            body = (f"{PLAYER_LAST_NAME} R{rnd}: holes "
                                    f"{holes_done[0]}-{holes_done[-1]} done "
                                    f"({delta_total:+d} over that stretch). "
                                    f"Thru {thru}, {fmt_today(today)} today, "
                                    f"{total} total.")
                        send_text(body)
                        state.update({"round": rnd, "thru": thru, "today": today})

                    if finished:
                        send_text(f"{PLAYER_LAST_NAME} has FINISHED round {rnd}: "
                                  f"{fmt_today(today or 0)} today, {total} total.")
                        print("Round final. Exiting.", flush=True)
                        return

                    shown = thru if thru is not None else "-"
                    print(time.strftime("%H:%M"),
                          f"- thru {shown}, today {today}, total {total}", flush=True)

        except KeyError:
            raise  # missing required env var — fail loudly
        except Exception as e:
            print(time.strftime("%H:%M"), f"- hiccup, will retry: {e}", flush=True)

        time.sleep(POLL_SECONDS)

    print("Max runtime reached; a later scheduled run will pick up. Exiting.", flush=True)


if __name__ == "__main__":
    main()
