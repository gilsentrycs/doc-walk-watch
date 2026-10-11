#!/usr/bin/env python3
"""Watch New Zealand DOC hut/campsite availability and alert on Telegram.

Configure trips in watch.json, then run this on a schedule (GitHub Actions
needs no server). Python 3.9+, standard library only - nothing to install.

Quick start:
    python3 doc_watch.py --list-walks              # the 10 Great Walks
    python3 doc_watch.py --list-huts "Kepler Track"
    python3 doc_watch.py --find-place "Mueller"    # non-Great-Walk huts
    python3 doc_watch.py --check                   # run once, print, send nothing
    python3 doc_watch.py --setup-telegram          # find your chat id
    python3 doc_watch.py                           # real run (sends alerts)
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
from typing import Any

API = "https://prod-nz-rdr.recreation-management.tylerapp.com/nzrdr/rdr"
SITE = "https://bookings.doc.govt.nz"

# The API returns an empty 202 unless BOTH Origin and Referer name the booking
# site. This is the single most important detail in this file.
HEADERS = {
    "Origin": SITE,
    "Referer": SITE + "/Web/",
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
}

TIMEOUT = 30
RETRIES = 4
STALE_HOURS = 6   # alert if nothing has succeeded for this long
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, "watch.json")
STATE_FILE = os.path.join(BASE_DIR, "state.json")

log = logging.getLogger("doc_watch")


class CheckError(Exception):
    """A check could not be completed.

    Raised for network failures and unrecognised responses. Never swallowed
    into "nothing available": if DOC changes the API we want a loud failure
    rather than a silent false negative that hides real vacancies.
    """


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def api(path: str, payload: dict | None = None) -> Any:
    """Call the DOC backend, retrying transient failures.

    A 202 with an empty body means "cold cache, ask again" rather than an
    error, so it is retried like any other transient failure.
    """
    url = f"{API}/{path.lstrip('/')}"
    data = json.dumps(payload).encode() if payload is not None else None
    last = None

    for attempt in range(1, RETRIES + 1):
        try:
            req = urllib.request.Request(
                url, data=data, method="POST" if data else "GET"
            )
            for k, v in HEADERS.items():
                req.add_header(k, v)
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                body = resp.read().decode("utf-8", "replace")
                if resp.status == 202 or not body.strip():
                    raise CheckError(f"empty {resp.status} (cold cache)")
                if resp.status != 200:
                    raise CheckError(f"HTTP {resp.status}")
                return json.loads(body)
        except (urllib.error.URLError, OSError, json.JSONDecodeError,
                CheckError) as exc:
            last = exc
            if attempt < RETRIES:
                delay = 2 ** attempt + random.uniform(0, 1)
                log.debug("retry %d/%d after %s (%.1fs)", attempt, RETRIES, exc, delay)
                time.sleep(delay)

    raise CheckError(f"{path} failed after {RETRIES} attempts: {last}")


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------

def daterange(start: str, end: str) -> list[str]:
    a, b = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    if b < a:
        raise ValueError(f"window end {end} is before start {start}")
    out = []
    while a <= b:
        out.append(a.isoformat())
        a += dt.timedelta(days=1)
    return out


def plus(date: str, days: int) -> str:
    return (dt.date.fromisoformat(date) + dt.timedelta(days=days)).isoformat()


def pretty(iso: str) -> str:
    d = dt.date.fromisoformat(iso)
    return f"{d.day} {d.strftime('%b')} {d.year}"


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def great_walks() -> dict[str, int]:
    """{walk name: placeId} for the ten Great Walks."""
    raw = api("search/getgreatwalkplaces/false/isCashier")
    rows = raw.get("GWPlaceData")
    if not rows:
        raise CheckError("no GWPlaceData in Great Walks list")
    return {r["Name"]: r["PlaceId"] for r in rows}


def booking_window() -> tuple[str, str]:
    raw = api("search/getgreatwalkplaces/false/isCashier")
    return raw.get("FutureBookingStartDate", "?"), raw.get("FutureBookingEndDate", "?")


def resolve_walk(name: str) -> int:
    walks = great_walks()
    if name in walks:
        return walks[name]
    loose = [w for w in walks if name.lower() in w.lower()]
    if len(loose) == 1:
        return walks[loose[0]]
    if loose:
        raise CheckError(f"walk {name!r} is ambiguous: {loose}")
    raise CheckError(f"unknown walk {name!r}. Known: {sorted(walks)}")


def find_place(term: str) -> list[dict]:
    """Search every bookable place (for huts that are not Great Walks)."""
    rows = api("fd/CityPark/CityPlaceswithAlias")
    t = term.lower()
    return [r for r in rows
            if t in (r.get("Name") or "").lower()
            or t in (r.get("DisplayName") or "").lower()]


def place_facilities(place_id: int) -> list[dict]:
    """Bookable facilities within a place, with their facility ids."""
    rows = api(f"fd/facilities/place/{place_id}")
    if isinstance(rows, dict):
        rows = rows.get("Facilities") or []
    return rows or []


# ---------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------

def greatwalk_availability(place_id: int, window: list[str]) -> dict[str, dict[str, int]]:
    """{facility name: {date: places free}} for a Great Walk.

    One request covers the whole window. This endpoint treats `nights` as days
    *after* arrival, so it returns len(window) columns for nights=len-1. We key
    off the dates it actually returns rather than trusting that arithmetic.
    """
    raw = api("search/greatwalkplacefacility", {
        "accomodation": "",
        "placeId": place_id,
        "customerClassificationId": 0,
        "arrivalDate": window[0],
        "nights": max(len(window) - 1, 1),
    })
    facs = raw.get("GreatWalkFacilityData")
    if not isinstance(facs, list) or not facs:
        raise CheckError("no GreatWalkFacilityData (API may have changed)")

    out: dict[str, dict[str, int]] = {}
    for f in facs:
        name, rows = f.get("FacilityName"), f.get("GreatWalkFacilityDateData")
        if not name or not isinstance(rows, list):
            continue
        # NOTE: IsAvailable is True here even when TotalAvailable is 0, so the
        # place count is the only trustworthy signal.
        out[name] = {
            r["ArrivalDate"][:10]: r["TotalAvailable"]
            for r in rows
            if r.get("ArrivalDate") and isinstance(r.get("TotalAvailable"), int)
        }
    return out


def facility_availability(facility_id: int, name: str,
                          window: list[str]) -> dict[str, dict[str, int]]:
    """{facility name: {date: places free}} for a standalone hut/campsite."""
    raw = api(f"search/occupancygrid/{facility_id}"
              f"/startdate/{window[0]}/nights/{len(window)}/0")
    grid = (raw.get("Facility") or {}).get("Dates")
    if not isinstance(grid, dict) or not grid:
        raise CheckError(f"no Facility.Dates for facility {facility_id}")

    per_date: dict[str, int] = {}
    for row in grid.values():
        date, total = row.get("Date"), row.get("TotalAvailable")
        if not date or not isinstance(total, int):
            continue
        # Mirror image of the Great Walk trap: this endpoint reports a FULL
        # TotalAvailable on dates that are out of season, so the season and
        # block flags have to gate the count.
        if not row.get("InSeason", False) or row.get("IsBlocked", False):
            total = 0
        per_date[date] = total
    return {name: per_date}


# ---------------------------------------------------------------------------
# Trip matching
# ---------------------------------------------------------------------------

def match_trip(trip: dict, avail: dict[str, dict[str, int]],
               party: int) -> list[dict]:
    """Every start date where the whole trip fits, consecutively.

    A trip is an ordered list of nights. Night i must be spent at one of its
    acceptable facilities on start+i, with at least `party` places free, and
    every night must land inside the window. This generalises both a single
    night at one hut and a multi-night traverse across different huts.
    """
    window = daterange(trip["window"]["start"], trip["window"]["end"])
    valid = set(window)
    nights = trip["nights"]
    matches = []

    for start in window:
        dates = [plus(start, i) for i in range(len(nights))]
        if any(d not in valid for d in dates):
            continue  # the trip would run past the end of the window

        picks, ok = [], True
        for night, date in zip(nights, dates):
            options = [(f, avail.get(f, {}).get(date)) for f in night["stay"]]
            usable = [(f, p) for f, p in options if p is not None and p >= party]
            if not usable:
                ok = False
                break
            usable.sort(key=lambda fp: -fp[1])  # roomiest first
            best, places = usable[0]
            picks.append({
                "date": date, "facility": best, "places": places,
                "also": [f for f, _ in usable[1:]],
            })
        if ok:
            matches.append({"start": start, "nights": picks})
    return matches


def verify_facilities(trip: dict, avail: dict[str, dict[str, int]]) -> None:
    """Fail loudly if the config names a facility the API does not return."""
    known = set(avail)
    wanted = {f for night in trip["nights"] for f in night["stay"]}
    missing = sorted(wanted - known)
    if missing:
        raise CheckError(
            f"trip {trip['name']!r} names unknown facilities {missing}. "
            f"Available here: {sorted(known)}"
        )


def check_trip(trip: dict, party: int) -> dict:
    window = daterange(trip["window"]["start"], trip["window"]["end"])
    kind = trip.get("type", "greatwalk")

    if kind == "greatwalk":
        place_id = trip.get("place_id") or resolve_walk(trip["walk"])
        avail = greatwalk_availability(place_id, window)
        book_url = f"{SITE}/Web/Default.aspx#!greatwalk-result"
    elif kind == "facility":
        avail = facility_availability(
            trip["facility_id"], trip["nights"][0]["stay"][0], window
        )
        book_url = (f"{SITE}/Web/Default.aspx#!park/"
                    f"{trip['place_id']}/{trip['facility_id']}")
    else:
        raise CheckError(f"trip {trip['name']!r} has unknown type {kind!r}")

    verify_facilities(trip, avail)
    wanted = {f for night in trip["nights"] for f in night["stay"]}
    return {
        "name": trip["name"],
        "avail": {f: avail[f] for f in sorted(wanted)},
        "matches": match_trip(trip, avail, party),
        "book_url": trip.get("book_url", book_url),
    }


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

def band(places: int) -> str:
    """Coarse capacity band, so trivial wobble does not re-alert."""
    if places < 3:
        return str(places)
    if places < 5:
        return "3-4"
    if places < 10:
        return "5-9"
    return "10+"


def signature(matches: list[dict]) -> list[str]:
    return sorted(
        m["start"] + ">" + ",".join(f"{n['facility']}|{band(n['places'])}"
                                    for n in m["nights"])
        for m in matches
    )


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

def tg_creds() -> tuple[str, str]:
    return (os.environ.get("TELEGRAM_BOT_TOKEN", "").strip(),
            os.environ.get("TELEGRAM_CHAT_ID", "").strip())


def redact(text: str) -> str:
    return re.sub(r"bot\d+:[A-Za-z0-9_\-]+", "bot<REDACTED>", text)


def send_telegram(text: str, dry_run: bool = False) -> bool:
    token, chat = tg_creds()
    if not token or not chat:
        log.error("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set")
        return False
    if dry_run:
        log.info("[dry-run] would send:\n%s", text)
        return True

    payload = json.dumps({"chat_id": chat, "text": text,
                          "disable_web_page_preview": False}).encode()
    for attempt in range(1, 4):
        try:
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{token}/sendMessage",
                data=payload, method="POST")
            req.add_header("Content-Type", "application/json")
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                if resp.status == 200:
                    return True
        except urllib.error.HTTPError as exc:
            # A wrong token or chat id is 401/400/404 and will never succeed, so
            # say so at once instead of making the user wait out the backoff.
            if exc.code in (400, 401, 403, 404):
                log.error("telegram rejected the request (HTTP %d). "
                          "Check TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID, and "
                          "make sure you have sent your bot a message first.",
                          exc.code)
                return False
            log.warning("telegram attempt %d failed: %s", attempt, redact(str(exc)))
        except Exception as exc:  # noqa: BLE001 - must never leak the token
            log.warning("telegram attempt %d failed: %s", attempt, redact(str(exc)))
        time.sleep(2 ** attempt)
    return False


MAX_LISTED = 6  # Telegram caps messages at 4096 chars; keep them readable too.


def format_message(result: dict, party: int) -> str:
    lines = ["\U0001f3d4 DOC AVAILABILITY", "", result["name"].upper(), ""]
    shown = result["matches"][:MAX_LISTED]
    extra = len(result["matches"]) - len(shown)
    for m in shown:
        if len(m["nights"]) > 1:
            lines.append(f"Trip starting {pretty(m['start'])}:")
        for n in m["nights"]:
            alts = f"  (or {', '.join(n['also'])})" if n["also"] else ""
            lines.append(f"  {n['facility']}: {pretty(n['date'])} "
                         f"— room for {party} ({n['places']} places){alts}")
        if len(m["nights"]) > 1:
            lines.append("")   # separate multi-night itineraries; keep singles tight
    if shown and len(shown[0]["nights"]) == 1:
        lines.append("")
    if extra:
        lines.append(f"...and {extra} more start date(s).")
        lines.append("")
    lines += ["Book now:", result["book_url"]]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Config / state
# ---------------------------------------------------------------------------

def load_config(path: str) -> dict:
    try:
        with open(path) as fh:
            cfg = json.load(fh)
    except FileNotFoundError:
        raise SystemExit(f"No config at {path}. Copy watch.example.json to watch.json.")
    except json.JSONDecodeError as exc:
        raise SystemExit(f"{path} is not valid JSON: {exc}")

    if not cfg.get("trips"):
        raise SystemExit(f"{path} has no 'trips'.")
    for t in cfg["trips"]:
        for key in ("name", "window", "nights"):
            if key not in t:
                raise SystemExit(f"trip {t.get('name', '?')!r} is missing {key!r}")
        daterange(t["window"]["start"], t["window"]["end"])  # validates order
        for night in t["nights"]:
            if not night.get("stay"):
                raise SystemExit(f"trip {t['name']!r} has a night with no 'stay' list")
    return cfg


def load_state() -> dict:
    try:
        with open(STATE_FILE) as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(state, fh, indent=1, sort_keys=True)
    os.replace(tmp, STATE_FILE)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def print_table(result: dict, party: int) -> None:
    print(f"\n  {result['name']}")
    dates = sorted({d for per in result["avail"].values() for d in per})
    width = max((len(f) for f in result["avail"]), default=10)
    # Each column is 5 wide so the counts sit under their date. "-" is a known
    # zero; "?" is a date the API did not report at all.
    print(f"    {'':{width}}  " + " ".join(f"{d[5:]:>5}" for d in dates))
    for fac, per in result["avail"].items():
        cells = []
        for d in dates:
            p = per.get(d)
            cells.append(f"{'?' if p is None else (p or '-'):>5}")
        print(f"    {fac:{width}}  " + " ".join(cells))
    if result["matches"]:
        print(f"    -> {len(result['matches'])} option(s) for {party}:")
        for m in result["matches"]:
            chain = " then ".join(f"{n['facility']} {n['date'][5:]}" for n in m["nights"])
            print(f"       {chain}")
    else:
        print(f"    -> nothing for {party}")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_list_walks() -> int:
    start, end = booking_window()
    print(f"Bookable period: {start} to {end}\n")
    for name, pid in sorted(great_walks().items()):
        print(f"  {pid:>5}  {name}")
    return 0


def cmd_list_huts(walk: str) -> int:
    pid = resolve_walk(walk)
    probe = daterange(*(lambda s: (s, plus(s, 2)))(dt.date.today().isoformat()))
    avail = greatwalk_availability(pid, probe)
    print(f"Facilities on {walk} (placeId {pid}) - use these names in 'stay':\n")
    for name in sorted(avail):
        print(f"  {name}")
    return 0


def cmd_find_place(term: str) -> int:
    hits = find_place(term)
    if not hits:
        print(f"No place matching {term!r}.")
        return 1
    for h in hits:
        pid = h["PlaceId"]
        print(f"\n  {h['Name']}  (place_id {pid})")
        try:
            for f in place_facilities(pid):
                fid = f.get("FacilityId")
                print(f"      facility_id {fid:<6} {f.get('Name')}")
        except CheckError as exc:
            print(f"      (could not list facilities: {exc})")
            print(f"      Open {SITE}/Web/Default.aspx#!park/{pid} and read the")
            print("      facility id from the URL after clicking the hut.")
    return 0


def cmd_setup_telegram() -> int:
    token, chat = tg_creds()
    if not token:
        print("Set TELEGRAM_BOT_TOKEN first (get one from @BotFather).")
        return 1
    print("Send any message to your bot, then press Enter...")
    input()
    try:
        with urllib.request.urlopen(
                f"https://api.telegram.org/bot{token}/getUpdates", timeout=TIMEOUT) as r:
            data = json.loads(r.read().decode())
    except Exception as exc:  # noqa: BLE001
        print("Could not reach Telegram:", redact(str(exc)))
        return 1
    ids = {u["message"]["chat"]["id"]: u["message"]["chat"].get("first_name", "")
           for u in data.get("result", []) if "message" in u}
    if not ids:
        print("No messages seen. Message the bot, then run this again.")
        return 1
    for cid, who in ids.items():
        print(f"  TELEGRAM_CHAT_ID={cid}   ({who})")
    if chat:
        print(f"\nCurrently configured: {chat}")
    return 0


def run(cfg: dict, args) -> int:
    party = cfg.get("party_size", 2)
    state = load_state()
    state.setdefault("trips", {})
    failures = []
    any_ok = False

    for trip in cfg["trips"]:
        if trip.get("enabled") is False:
            log.info("%s: disabled, skipped", trip["name"])
            continue
        try:
            result = check_trip(trip, party)
        except CheckError as exc:
            failures.append(trip["name"])
            log.error("%s: CHECK FAILED - %s", trip["name"], exc)
            continue

        any_ok = True
        log.info("%s: %d option(s) for %d", trip["name"], len(result["matches"]), party)
        if args.check:
            print_table(result, party)
            continue

        entry = state["trips"].setdefault(trip["name"], {})
        new_sig = signature(result["matches"])
        if new_sig and new_sig != entry.get("signature", []):
            if send_telegram(format_message(result, party), args.dry_run):
                brief = new_sig[:4] + ([f"+{len(new_sig) - 4} more"]
                                       if len(new_sig) > 4 else [])
                log.info("%s: ALERT SENT %s", trip["name"], brief)
                if not args.dry_run:
                    entry["signature"] = new_sig
        elif not new_sig:
            entry["signature"] = []
        entry["checked"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")

    # Health: a watcher that quietly stops working looks exactly like "nothing
    # available", so say so out loud after a long run of failures.
    now = dt.datetime.now(dt.timezone.utc)
    if any_ok:
        state["last_success"] = now.isoformat(timespec="seconds")
        state.pop("stale_alerted", None)
    elif not args.check:
        last = state.get("last_success")
        since = now - dt.datetime.fromisoformat(last) if last else dt.timedelta()
        alerted = state.get("stale_alerted")
        due = not alerted or (now - dt.datetime.fromisoformat(alerted)) >= dt.timedelta(hours=STALE_HOURS)
        if since >= dt.timedelta(hours=STALE_HOURS) and due:
            hours = since.total_seconds() / 3600
            if send_telegram(
                    "\u26a0\ufe0f DOC WATCHER PROBLEM\n\n"
                    f"No successful check for {hours:.0f} hours.\n"
                    f"Failing: {', '.join(failures) or 'everything'}\n\n"
                    "Availability is UNKNOWN - this is not 'nothing available'.\n"
                    "Check the workflow logs.", args.dry_run):
                state["stale_alerted"] = now.isoformat(timespec="seconds")

    if not args.check and not args.dry_run:
        save_state(state)

    if failures and not any_ok:
        log.error("every trip failed: %s", failures)
        return 2
    return 1 if failures else 0


# ---------------------------------------------------------------------------

def self_test() -> int:
    """Offline checks of the trip matcher. No network, no credentials."""
    fails = 0
    trip = {
        "name": "t", "window": {"start": "2027-02-15", "end": "2027-02-20"},
        "nights": [{"stay": ["Luxmore Hut"]}, {"stay": ["Iris Burn Campsite"]}],
    }
    zero = {d: 0 for d in daterange("2027-02-15", "2027-02-20")}

    def case(label, avail, expect, party=2):
        nonlocal fails
        got = [m["start"] for m in match_trip(trip, avail, party)]
        ok = got == expect
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
        if not ok:
            print(f"         expected {expect}, got {got}")
            fails += 1

    print("Consecutive-night matching:")
    case("both nights free -> match",
         {"Luxmore Hut": {**zero, "2027-02-16": 4},
          "Iris Burn Campsite": {**zero, "2027-02-17": 6}}, ["2027-02-16"])
    case("second night same day -> no match",
         {"Luxmore Hut": {**zero, "2027-02-16": 4},
          "Iris Burn Campsite": {**zero, "2027-02-16": 6}}, [])
    case("order reversed -> no match",
         {"Luxmore Hut": {**zero, "2027-02-17": 4},
          "Iris Burn Campsite": {**zero, "2027-02-16": 6}}, [])
    case("one place short -> no match",
         {"Luxmore Hut": {**zero, "2027-02-16": 1},
          "Iris Burn Campsite": {**zero, "2027-02-17": 6}}, [])
    case("exactly enough -> match",
         {"Luxmore Hut": {**zero, "2027-02-16": 2},
          "Iris Burn Campsite": {**zero, "2027-02-17": 2}}, ["2027-02-16"])
    case("solo walker, 1 place is enough -> match",
         {"Luxmore Hut": {**zero, "2027-02-16": 1},
          "Iris Burn Campsite": {**zero, "2027-02-17": 1}}, ["2027-02-16"], party=1)
    case("second night would fall outside window -> no match",
         {"Luxmore Hut": {**zero, "2027-02-20": 9},
          "Iris Burn Campsite": dict(zero)}, [])
    case("two separate options -> both",
         {"Luxmore Hut": {**zero, "2027-02-15": 3, "2027-02-18": 2},
          "Iris Burn Campsite": {**zero, "2027-02-16": 3, "2027-02-19": 5}},
         ["2027-02-15", "2027-02-18"])

    print("\nAlternatives within one night:")
    flex = {"name": "f", "window": {"start": "2027-02-15", "end": "2027-02-16"},
            "nights": [{"stay": ["Hut A", "Hut B"]}]}
    got = match_trip(flex, {"Hut A": {"2027-02-15": 0, "2027-02-16": 0},
                            "Hut B": {"2027-02-15": 5, "2027-02-16": 0}}, 2)
    ok = [m["nights"][0]["facility"] for m in got] == ["Hut B"]
    print(f"  [{'PASS' if ok else 'FAIL'}] falls back to the open hut")
    fails += 0 if ok else 1

    got = match_trip(flex, {"Hut A": {"2027-02-15": 3}, "Hut B": {"2027-02-15": 9}}, 2)
    ok = got and got[0]["nights"][0]["facility"] == "Hut B" and \
        got[0]["nights"][0]["also"] == ["Hut A"]
    print(f"  [{'PASS' if ok else 'FAIL'}] picks roomiest, lists the other")
    fails += 0 if ok else 1

    print("\nUnknown facility names fail loudly:")
    try:
        verify_facilities(trip, {"Luxmore Hut": {}})
        print("  [FAIL] did not raise")
        fails += 1
    except CheckError:
        print("  [PASS] raises CheckError instead of reporting 'nothing available'")

    print("\nDeduplication:")
    m = [{"start": "2027-02-16", "nights": [
        {"date": "2027-02-16", "facility": "Luxmore Hut", "places": 12, "also": []}]}]
    m2 = [{"start": "2027-02-16", "nights": [
        {"date": "2027-02-16", "facility": "Luxmore Hut", "places": 11, "also": []}]}]
    m3 = [{"start": "2027-02-16", "nights": [
        {"date": "2027-02-16", "facility": "Luxmore Hut", "places": 2, "also": []}]}]
    for label, ok in [("12 -> 11 places: no re-alert", signature(m) == signature(m2)),
                      ("12 -> 2 places: re-alert", signature(m) != signature(m3)),
                      ("no matches -> empty signature", signature([]) == [])]:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
        fails += 0 if ok else 1

    print("\n" + ("ALL TESTS PASSED" if not fails else f"{fails} TEST(S) FAILED"))
    return 1 if fails else 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default=CONFIG_FILE)
    p.add_argument("--check", action="store_true",
                   help="print an availability table, send nothing, save nothing")
    p.add_argument("--dry-run", action="store_true",
                   help="do everything except send and save")
    p.add_argument("--list-walks", action="store_true")
    p.add_argument("--list-huts", metavar="WALK")
    p.add_argument("--find-place", metavar="TERM")
    p.add_argument("--setup-telegram", action="store_true")
    p.add_argument("--test-telegram", action="store_true")
    p.add_argument("--self-test", action="store_true")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
    )

    try:
        if args.self_test:
            return self_test()
        if args.list_walks:
            return cmd_list_walks()
        if args.list_huts:
            return cmd_list_huts(args.list_huts)
        if args.find_place:
            return cmd_find_place(args.find_place)
        if args.setup_telegram:
            return cmd_setup_telegram()
        if args.test_telegram:
            ok = send_telegram(
                "\U0001f9ea TEST — DOC watcher\n\n"
                "Telegram delivery works. This is NOT a real availability alert.",
                args.dry_run)
            print("sent" if ok else "FAILED - check your token and chat id")
            return 0 if ok else 1
        return run(load_config(args.config), args)
    except CheckError as exc:
        log.error("%s", exc)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
