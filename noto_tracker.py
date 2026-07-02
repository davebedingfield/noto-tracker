#!/usr/bin/env python3
"""
noto_tracker.py — GitHub Actions edition, v2 (official USGA feed)

Texts your Google Fi phone after every hole David Noto completes at the
2026 U.S. Senior Open, sourced directly from USGA's own live scoring API
(the same one that powers championships.usga.org/ussenioropen/2026/scoring.html).

Env vars (set as GitHub repo secrets):
  GMAIL_ADDRESS, GMAIL_APP_PASSWORD, FI_NUMBER
Optional:
  USGA_PLAYER_ID   (default "55982" = David Noto)
  PLAYER_LAST_NAME (default "Noto", used only for text messages / fallback lookup)
  POLL_SECONDS         (default 120)
  MAX_RUNTIME_SECONDS  (default 20700 = 5h45m, under GitHub's 6h job cap)

Behavior:
  * First successful poll is a silent baseline -> one "tracker live" text,
    then per-hole texts from that point on (no catch-up spam on restart).
  * Exits 0 when the player's round shows finished ("F"/"F*"), or at MAX_RUNTIME.
  * Auto-tracks whichever round is currently active (the feed's own "round"
    field advances Thu -> Sun automatically; no code changes needed all week).
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
USGA_PLAYER_ID   = os.environ.get("USGA_PLAYER_ID", "55982")
PLAYER_LAST_NAME = os.environ.get("PLAYER_LAST_NAME", "Noto")
GMAIL_ADDRESS    = os.environ["GMAIL_ADDRESS"]
GMAIL_APP_PASS   = os.environ["GMAIL_APP_PASSWORD"]
FI_NUMBER        = os.environ["FI_NUMBER"]
POLL_SECONDS     = int(os.environ.get("POLL_SECONDS", "120"))
MAX_RUNTIME      = int(os.environ.get("MAX_RUNTIME_SECONDS", "20700"))
# ----------------------------------------------------------

LEADERBOARD_URL = (
    "https://ace-api.usga.org/scoring/v1/leaderboard.json"
    "?championship=usso&championship-year=2026"
)
SMS_ADDRESS = f"{FI_NUMBER}@msg.fi.google.com"

HOLE_LABELS = {
    -3: "ALBATROSS!!", -2: "EAGLE!", -1: "Birdie",
     0: "Par", 1: "Bogey", 2: "Double bogey", 3: "Triple bogey",
}


def label_for(delta: int) -> str:
    return HOLE_LABELS.get(delta, f"{delta:+d} on the hole")


def fmt_signed(n: int) -> str:
    return "E" if n == 0 else f"{n:+d}"


def fetch_leaderboard() -> dict:
    req = urllib.request.Request(
        LEADERBOARD_URL, headers={"User-Agent": "Mozilla/5.0 (hole-tracker)"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def find_player(data: dict, player_id: str, last_name: str):
    """Match by USGA identifier first (exact), fall back to last name."""
    standings = data.get("standings", [])
    for entry in standings:
        p = entry.get("player", {})
        if str(p.get("identifier", "")).lstrip("0") == str(player_id).lstrip("0"):
            return entry
    target = last_name.lower()
    for entry in standings:
        if entry.get("player", {}).get("lastName", "").lower() == target:
            return entry
    return None


def extract_status(data: dict, entry: dict):
    """Return (round, thru, today_rel_to_par, tourney_to_par_text, finished)."""
    rnd = data.get("round", 1)
    holes = entry.get("holesThrough", {})
    thru = holes.get("value")
    disp = str(holes.get("displayValue", "")).upper()
    finished = disp.startswith("F")

    today = entry.get("toParToday", {}).get("value")
    total_display = entry.get("toPar", {}).get("displayValue", "?")

    try:
        thru = int(thru)
    except (TypeError, ValueError):
        thru = 18 if finished else 0

    return rnd, thru, today, total_display, finished


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
    print(f"Tracking {PLAYER_LAST_NAME} (id {USGA_PLAYER_ID}) via USGA feed; "
          f"texting {FI_NUMBER}@msg.fi... poll={POLL_SECONDS}s, "
          f"max runtime={MAX_RUNTIME}s", flush=True)

    baselined = False
    state = {}

    while time.time() - start < MAX_RUNTIME:
        try:
            data = fetch_leaderboard()
            entry = find_player(data, USGA_PLAYER_ID, PLAYER_LAST_NAME)

            if entry is None:
                print(time.strftime("%H:%M"), "- player not found in feed "
                      "(may not have teed off, or missed cut). Waiting...", flush=True)
            else:
                rnd, thru, today, total, finished = extract_status(data, entry)

                if not baselined:
                    if finished:
                        print("Round already final at startup; nothing to do.", flush=True)
                        return
                    state = {"round": rnd, "thru": thru or 0, "today": today or 0}
                    baselined = True
                    send_text(f"Tracker live: {PLAYER_LAST_NAME} R{rnd}, "
                              f"thru {thru}, {fmt_signed(today or 0)} today, "
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
                            body = (f"{PLAYER_LAST_NAME} R{rnd} hole {hole}: "
                                    f"{label_for(delta_total)}. Thru {thru}, "
                                    f"{fmt_signed(today)} today, {total} total.")
                        else:
                            body = (f"{PLAYER_LAST_NAME} R{rnd}: holes "
                                    f"{holes_done[0]}-{holes_done[-1]} done "
                                    f"({delta_total:+d} over that stretch). "
                                    f"Thru {thru}, {fmt_signed(today)} today, "
                                    f"{total} total.")
                        send_text(body)
                        state.update({"round": rnd, "thru": thru, "today": today})

                    if finished:
                        send_text(f"{PLAYER_LAST_NAME} has FINISHED round {rnd}: "
                                  f"{fmt_signed(today or 0)} today, {total} total.")
                        print("Round final. Exiting.", flush=True)
                        return

                    print(time.strftime("%H:%M"),
                          f"- thru {thru}, today {today}, total {total}", flush=True)

        except KeyError:
            raise
        except Exception as e:
            print(time.strftime("%H:%M"), f"- hiccup, will retry: {e}", flush=True)

        time.sleep(POLL_SECONDS)

    print("Max runtime reached; a later scheduled run will pick up. Exiting.", flush=True)


if __name__ == "__main__":
    main()
