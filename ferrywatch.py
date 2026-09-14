#!/usr/bin/env python3
"""
ferrywatch - bevakar fordonsplatser pa en Eckerolinjen-avgang och larmar via ntfy.

Inga externa beroenden. Python 3.9+.

Datakalla: https://api.eckerolinjen.se/api/v1/availability
Oppet API, ingen inloggning. Kraver headern X-Application: booking.

Statuskoder fran API:et, samma som fargerna i bokningen:
    high   -> Tillgangligt   (gron)
    low    -> Fa platser     (orange)
    empty  -> Fullbokat      (rod)

Kor:
    python3 ferrywatch.py            # en enda kontroll (for cron / GitHub Actions)
    python3 ferrywatch.py --loop     # kontinuerlig loop, var 3:e minut
    python3 ferrywatch.py --test     # skicka en testnotis och avsluta
    python3 ferrywatch.py --status   # visa nulaget utan att larma
"""

import argparse
import json
import logging
import os
import random
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python 3.8
    ZoneInfo = None


# --------------------------------------------------------------------------
# Konfiguration. Allt kan overstyras med miljovariabler.
# --------------------------------------------------------------------------

CONFIG = {
    # Resan som bevakas
    "ROUTE": os.getenv("ROUTE", "EG"),              # EG = Eckero -> Grisslehamn
    "DATE": os.getenv("DATE", "2026-09-20"),        # YYYY-MM-DD
    "TIME": os.getenv("TIME", "13:30"),             # HH:MM, avgangstid i Alandstid
    "ROUTE_LABEL": os.getenv("ROUTE_LABEL", "Eckerö → Grisslehamn"),

    # Vad som bevakas: "vehicles" (fordon), "passengers" (resenarer), "food" (mat)
    "WATCH_GROUP": os.getenv("WATCH_GROUP", "vehicles"),

    # Statusar som ska utlosa larm. "low" = fa platser, "high" = tillgangligt.
    "ALERT_ON": [s.strip() for s in os.getenv("ALERT_ON", "low,high").split(",") if s.strip()],

    # Notifiering via ntfy
    "NTFY_SERVER": os.getenv("NTFY_SERVER", "https://ntfy.sh"),
    "NTFY_TOPIC": os.getenv("NTFY_TOPIC", ""),

    # Pollning i --loop-lage
    "INTERVAL_SECONDS": int(os.getenv("INTERVAL_SECONDS", "180")),
    "JITTER_SECONDS": int(os.getenv("JITTER_SECONDS", "30")),

    # Paminn var N:e minut sa lange platsen fortfarande ar ledig
    "REMIND_MINUTES": int(os.getenv("REMIND_MINUTES", "30")),

    # Larma om bevakaren sjalv ar trasig efter N misslyckade forsok i rad
    "FAIL_STREAK_ALERT": int(os.getenv("FAIL_STREAK_ALERT", "5")),

    # Livstecken var N:e timme sa att tystnad aldrig ar tvetydig. 0 = av.
    "HEARTBEAT_HOURS": int(os.getenv("HEARTBEAT_HOURS", "24")),

    # Filer
    "STATE_FILE": os.getenv("STATE_FILE", "state.json"),
    "LOG_FILE": os.getenv("LOG_FILE", "ferrywatch.log"),
}

API_BASE = "https://api.eckerolinjen.se/api/v1"
BOOKING_URL = "https://boka.eckerolinjen.se/ticket"
USER_AGENT = "ferrywatch/1.0 (personlig platsbevakning; 1 anrop var 3:e minut)"

STATUS_SV = {
    "high": "Tillgängligt",
    "low": "Få platser kvar",
    "empty": "Fullbokat",
    "unknown": "Okänd",
}

GROUP_SV = {
    "vehicles": "Fordon",
    "passengers": "Resenärer",
    "food": "Mat",
    "bus": "Buss",
}

# API:ets statusfalt heter "vehicles"/"passengers"/"food"/"bus",
# men resurserna under dem har gruppnamnet i singular.
RESOURCE_GROUP = {
    "vehicles": "vehicle",
    "passengers": "passenger",
    "food": "food",
    "bus": "bus",
}


log = logging.getLogger("ferrywatch")


# --------------------------------------------------------------------------
# Hjalpfunktioner
# --------------------------------------------------------------------------

def setup_logging(log_file):
    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s  %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(fmt)
    log.addHandler(stream)

    if log_file:
        try:
            fh = logging.FileHandler(log_file, encoding="utf-8")
            fh.setFormatter(fmt)
            log.addHandler(fh)
        except OSError as exc:
            log.warning("Kunde inte oppna loggfilen %s: %s", log_file, exc)


def departure_deadline(cfg):
    """Avgangstiden som ett tidszonsmedvetet datetime.

    Eckerolinjens avgangstider fran Eckero anges i alandsk tid (UTC+3 pa sommaren).
    """
    naive = datetime.strptime(f"{cfg['DATE']} {cfg['TIME']}", "%Y-%m-%d %H:%M")
    if ZoneInfo is not None:
        try:
            return naive.replace(tzinfo=ZoneInfo("Europe/Mariehamn"))
        except Exception:
            pass
    # Fallback: anta UTC+3
    return naive.replace(tzinfo=timezone(timedelta(hours=3)))


def now_utc():
    return datetime.now(timezone.utc)


def load_state(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_state(path, state):
    tmp = f"{path}.tmp"
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError as exc:
        log.error("Kunde inte skriva state till %s: %s", path, exc)


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------

class ApiError(Exception):
    """Natverksfel eller HTTP-fel. Overgaende, vi forsoker igen."""


class SchemaError(Exception):
    """Svaret gick inte att tolka. Sajten har troligen byggts om."""


def http_get_json(url, timeout=20):
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "Accept-Language": "sv-SE",
            "X-Application": "booking",
            "User-Agent": USER_AGENT,
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace")
    return json.loads(raw)


def fetch_availability(cfg, attempts=3):
    """Hamtar tillganglighet for datumet. Tre forsok med exponentiell backoff."""
    url = f"{API_BASE}/availability?from_date={cfg['DATE']}&routes%5B%5D={cfg['ROUTE']}"
    last = None
    for attempt in range(1, attempts + 1):
        try:
            payload = http_get_json(url)
            if not isinstance(payload, dict) or not payload.get("success"):
                raise SchemaError(f"API svarade utan success: {str(payload)[:200]}")
            return payload
        except SchemaError:
            raise
        except (urllib.error.URLError, urllib.error.HTTPError, ValueError, OSError) as exc:
            last = exc
            if attempt < attempts:
                backoff = 2 ** attempt
                log.warning("Forsok %d/%d misslyckades (%s). Vantar %ds.",
                            attempt, attempts, exc, backoff)
                time.sleep(backoff)
    raise ApiError(f"Alla {attempts} forsok misslyckades: {last}")


def parse_departure(payload, cfg):
    """Plockar ut den bevakade avgangen och returnerar en sammanfattning.

    Kastar SchemaError om svaret inte ser ut som vi forvantar oss, sa att
    en ombyggd sajt ger ett larm i stallet for tyst "inget nytt".
    """
    try:
        departures = payload["data"]["availability"]
    except (KeyError, TypeError):
        raise SchemaError("Hittade inte data.availability i svaret")

    if not isinstance(departures, list) or not departures:
        raise SchemaError("data.availability ar tom eller inte en lista")

    match = None
    for dep in departures:
        if not isinstance(dep, dict):
            continue
        if dep.get("date") == cfg["DATE"] and dep.get("time") == cfg["TIME"] \
                and dep.get("route") == cfg["ROUTE"]:
            match = dep
            break

    if match is None:
        seen = ", ".join(
            f"{d.get('time')}" for d in departures if isinstance(d, dict)
        )
        raise SchemaError(
            f"Avgangen {cfg['DATE']} {cfg['TIME']} {cfg['ROUTE']} saknas i svaret. "
            f"Avgangar som fanns: {seen or 'inga'}"
        )

    group = cfg["WATCH_GROUP"]
    status_block = match.get("status")
    if not isinstance(status_block, dict) or group not in status_block:
        raise SchemaError(f"status.{group} saknas i svaret for avgangen")

    status = status_block.get(group)
    if status not in ("high", "low", "empty"):
        raise SchemaError(f"Okand statuskod '{status}' for {group}")

    # Den auktoritativa signalen per resurs ar faltet "available".
    #
    # "capacity" duger INTE som larmsignal. Det ar en delad pott (ser ut att
    # vara lopmeter dack) som galler hela fordonsutrymmet, inte antal bilar.
    # Verifierat mot skarp data 2026-09-14: 18:30-avgangen rapporterade
    # capacity 2.99 medan varenda bilkategori hade available=false - potten
    # rackte for en MC, inte for en personbil. Ett larm pa capacity >= 1 hade
    # darfor gett falsklarm mitt i natten.
    #
    # Gruppen "vehicle" ar bil. Tvahjulingar ligger separat i
    # "vehicle_two_wheel" och ska inte rakna som en bilplats.
    res_group = RESOURCE_GROUP.get(group, group)
    resources = [
        r for r in match.get("resources", [])
        if isinstance(r, dict) and r.get("group") == res_group and not r.get("is_locked")
    ]
    if not resources:
        raise SchemaError(f"Inga resurser i gruppen '{res_group}' for avgangen")

    bookable = [r for r in resources if r.get("available") is True]
    capacities = [r.get("capacity", 0) or 0 for r in resources]

    return {
        "status": status,
        "bookable": bool(bookable),
        "bookable_codes": [r.get("resource_code") for r in bookable],
        "max_capacity": round(float(max(capacities)) if capacities else 0.0, 2),
        "from_price": (match.get("from_prices") or {}).get("vehicle"),
        "departure_id": match.get("id"),
        "all_statuses": status_block,
    }


# --------------------------------------------------------------------------
# Notifiering (ntfy)
# --------------------------------------------------------------------------

# ntfy:s JSON-API kraver prioritet som heltal 1-5. Namnen ("urgent" osv)
# fungerar bara i X-Priority-headern, inte i JSON-kroppen - skickar man strangen
# svarar ntfy 400 och notisen forsvinner tyst.
NTFY_PRIORITY = {"min": 1, "low": 2, "default": 3, "high": 4, "urgent": 5, "max": 5}


def notify(cfg, title, message, priority="default", tags=None, click=None):
    """Publicerar till ntfy via JSON-endpointen, som ar UTF-8-saker."""
    topic = cfg["NTFY_TOPIC"]
    if not topic:
        log.error("NTFY_TOPIC ar inte satt. Notisen skickades INTE: %s / %s", title, message)
        return False

    body = {
        "topic": topic,
        "title": title,
        "message": message,
        "priority": NTFY_PRIORITY.get(priority, 3),
        "tags": tags or [],
    }
    if click:
        body["click"] = click

    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        cfg["NTFY_SERVER"].rstrip("/") + "/",
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        method="POST",
    )

    for attempt in range(1, 4):
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                resp.read()
            log.info("Notis skickad: %s", title)
            return True
        except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
            log.warning("Notis misslyckades (forsok %d/3): %s", attempt, exc)
            if attempt < 3:
                time.sleep(2 ** attempt)
    log.error("Kunde inte skicka notis: %s", title)
    return False


# --------------------------------------------------------------------------
# Larmlogik
# --------------------------------------------------------------------------

def build_alert_text(cfg, reading):
    status_sv = STATUS_SV.get(reading["status"], reading["status"])
    group_sv = GROUP_SV.get(cfg["WATCH_GROUP"], cfg["WATCH_GROUP"])
    stamp = now_utc().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    lines = [
        f"{cfg['ROUTE_LABEL']}",
        f"{cfg['DATE']} kl {cfg['TIME']}",
        f"{group_sv}: {status_sv}",
    ]
    if reading.get("bookable_codes"):
        lines.append("Bokningsbara kategorier: " + ", ".join(reading["bookable_codes"]))
    if reading.get("from_price"):
        lines.append(f"Pris från: {reading['from_price']}")
    lines.append(f"Avläst: {stamp}")
    lines.append("")
    lines.append("Boka nu: " + BOOKING_URL)
    return "\n".join(lines)


def check_once(cfg, state, dry_run=False):
    """En kontroll. Uppdaterar state och skickar notiser vid behov."""
    deadline = departure_deadline(cfg)
    now = now_utc()

    # Avsluta sig sjalv efter avgang
    if now > deadline:
        if not state.get("finished"):
            log.info("Avgangen har passerat. Avslutar bevakningen.")
            if not dry_run:
                notify(
                    cfg,
                    "Bevakningen avslutad",
                    f"{cfg['ROUTE_LABEL']} {cfg['DATE']} kl {cfg['TIME']} har avgått. "
                    "ferrywatch slutar nu kontrollera.",
                    priority="low",
                    tags=["checkered_flag"],
                )
            state["finished"] = True
        return state, "finished"

    # Hamta och tolka
    try:
        payload = fetch_availability(cfg)
        reading = parse_departure(payload, cfg)
        failure = None
    except (ApiError, SchemaError) as exc:
        failure = exc
        reading = None

    if failure is not None:
        state["fail_streak"] = state.get("fail_streak", 0) + 1
        state["last_error"] = f"{type(failure).__name__}: {failure}"
        log.error("Kontroll misslyckades (%d i rad): %s", state["fail_streak"], failure)

        threshold = cfg["FAIL_STREAK_ALERT"]
        schema_broken = isinstance(failure, SchemaError)
        should_warn = schema_broken or state["fail_streak"] >= threshold

        if should_warn and not state.get("breakage_notified"):
            reason = ("Sajten verkar ha byggts om — svaret går inte längre att tolka."
                      if schema_broken
                      else f"{state['fail_streak']} misslyckade kontroller i rad.")
            if not dry_run:
                notify(
                    cfg,
                    "VARNING: bevakaren är trasig",
                    f"{reason}\n\nDetaljer: {state['last_error']}\n\n"
                    "Bevakningen larmar INTE om lediga platser just nu. "
                    "Kontrollera manuellt: " + BOOKING_URL,
                    priority="high",
                    tags=["warning", "wrench"],
                )
            state["breakage_notified"] = True
        return state, "error"

    # Lyckad avlasning
    if state.get("fail_streak", 0) or state.get("breakage_notified"):
        if state.get("breakage_notified") and not dry_run:
            notify(cfg, "Bevakaren fungerar igen",
                   "Kontakten med Eckerölinjens API är återställd.",
                   priority="low", tags=["white_check_mark"])
        state["fail_streak"] = 0
        state["breakage_notified"] = False
        state.pop("last_error", None)

    status = reading["status"]
    previous = state.get("status", "unknown")
    capacity = reading["max_capacity"]
    bookable = reading["bookable"]

    log.info(
        "%s %s %s | %s=%s (var: %s) | bokningsbar=%s %s | kapacitet=%s",
        cfg["DATE"], cfg["TIME"], cfg["ROUTE"],
        cfg["WATCH_GROUP"], status, previous, bookable,
        reading["bookable_codes"] or "", capacity,
    )

    state["status"] = status
    state["bookable"] = bookable
    state["max_capacity"] = capacity
    state["last_check"] = now.isoformat()

    # Tva vagar in i "ledigt", och det racker att en av dem ar sann:
    #   1. Sammanfattande status sager nagot av det vi bevakar.
    #   2. Minst en konkret kategori har available=true, aven om den
    #      sammanfattande statusen slapar efter.
    is_open = (status in cfg["ALERT_ON"]) or bookable
    was_open = bool(state.get("was_open")) if "was_open" in state \
        else (previous in cfg["ALERT_ON"])
    state["was_open"] = is_open
    outcome = "no-change"

    if is_open and not was_open:
        # Flanken vi bevakar: fullbokat -> nagot ledigt
        if not dry_run:
            notify(
                cfg,
                f"PLATS LEDIG - {cfg['TIME']} {cfg['DATE']}",
                build_alert_text(cfg, reading),
                priority="urgent",
                tags=["rotating_light", "ferry"],
                click=BOOKING_URL,
            )
        state["last_alert"] = now.isoformat()
        outcome = "opened"

    elif is_open and was_open:
        # Fortfarande ledigt. Paminn med jamna mellanrum sa att en missad
        # forsta notis inte betyder att du gar miste om platsen.
        last_alert = state.get("last_alert")
        due = True
        if last_alert:
            try:
                due = now - datetime.fromisoformat(last_alert) >= timedelta(
                    minutes=cfg["REMIND_MINUTES"])
            except ValueError:
                due = True
        if due:
            if not dry_run:
                notify(
                    cfg,
                    f"Fortfarande ledigt - {cfg['TIME']} {cfg['DATE']}",
                    build_alert_text(cfg, reading),
                    priority="high",
                    tags=["ferry"],
                    click=BOOKING_URL,
                )
            state["last_alert"] = now.isoformat()
            outcome = "reminder"
        else:
            outcome = "still-open"

    elif was_open and not is_open:
        # Nagon annan hann fore. Vard att veta, men inte ett skrikigt larm.
        if not dry_run:
            notify(
                cfg,
                "Platsen är borta igen",
                f"{cfg['ROUTE_LABEL']} {cfg['DATE']} kl {cfg['TIME']} är "
                f"{STATUS_SV.get(status, status).lower()} igen. Bevakningen fortsätter.",
                priority="low",
                tags=["x"],
            )
        state.pop("last_alert", None)
        outcome = "closed"

    # Annars: fortfarande fullbokat. Tyst.
    #
    # Har fanns tidigare ett larm pa att "capacity" passerade 1.0. Det togs
    # bort efter matning mot skarp data - se kommentaren i parse_departure.
    # Kapaciteten loggas fortfarande som kontext, men styr inga notiser.

    # Livstecken
    hb_hours = cfg["HEARTBEAT_HOURS"]
    if hb_hours > 0:
        last_hb = state.get("last_heartbeat")
        due = True
        if last_hb:
            try:
                due = now - datetime.fromisoformat(last_hb) >= timedelta(hours=hb_hours)
            except ValueError:
                due = True
        if due:
            left = deadline - now
            hours_left = int(left.total_seconds() // 3600)
            if not dry_run:
                notify(
                    cfg,
                    "Bevakningen lever",
                    f"{cfg['ROUTE_LABEL']} {cfg['DATE']} kl {cfg['TIME']}\n"
                    f"{GROUP_SV.get(cfg['WATCH_GROUP'])}: "
                    f"{STATUS_SV.get(status, status)}\n"
                    f"{hours_left} timmar kvar till avgång.",
                    priority="min",
                    tags=["heartbeat"],
                )
            state["last_heartbeat"] = now.isoformat()

    return state, outcome


# --------------------------------------------------------------------------
# Entrypoints
# --------------------------------------------------------------------------

def run_once(cfg, dry_run=False):
    state = load_state(cfg["STATE_FILE"])
    state, outcome = check_once(cfg, state, dry_run=dry_run)
    if not dry_run:
        save_state(cfg["STATE_FILE"], state)
    return outcome


def run_loop(cfg):
    log.info("Startar loop. Kontroll var %ds (+/- %ds).",
             cfg["INTERVAL_SECONDS"], cfg["JITTER_SECONDS"])
    while True:
        try:
            outcome = run_once(cfg)
        except KeyboardInterrupt:
            log.info("Avbruten av anvandaren.")
            return 0
        except Exception:  # noqa: BLE001 - loopen far aldrig do tyst
            log.exception("Ovantat fel i loopen. Fortsatter.")
            outcome = "crash"

        if outcome == "finished":
            log.info("Bevakningen ar klar. Avslutar.")
            return 0

        sleep_for = cfg["INTERVAL_SECONDS"] + random.randint(
            -cfg["JITTER_SECONDS"], cfg["JITTER_SECONDS"])
        sleep_for = max(30, sleep_for)
        try:
            time.sleep(sleep_for)
        except KeyboardInterrupt:
            log.info("Avbruten av anvandaren.")
            return 0


def show_status(cfg):
    payload = fetch_availability(cfg)
    departures = payload.get("data", {}).get("availability", [])
    print(f"\n{cfg['ROUTE_LABEL']}  {cfg['DATE']}\n")
    print(f"{'Avgång':<10}{'Resenärer':<16}{'Fordon':<18}{'Bilplats?':<12}{'Pris från'}")
    print("-" * 74)
    for dep in departures:
        st = dep.get("status", {})
        cars = [
            r for r in dep.get("resources", [])
            if r.get("group") == "vehicle" and not r.get("is_locked")
        ]
        bookable = [r.get("resource_code") for r in cars if r.get("available") is True]
        mark = "  ← bevakas" if dep.get("time") == cfg["TIME"] else ""
        print(f"{dep.get('time',''):<10}"
              f"{STATUS_SV.get(st.get('passengers'), '?'):<16}"
              f"{STATUS_SV.get(st.get('vehicles'), '?'):<18}"
              f"{('JA: ' + ','.join(bookable)) if bookable else 'nej':<12}"
              f"{(dep.get('from_prices') or {}).get('vehicle', '-')}{mark}")
    print()


def main():
    parser = argparse.ArgumentParser(description="Bevakar fordonsplatser hos Eckerolinjen.")
    parser.add_argument("--loop", action="store_true", help="Kor kontinuerligt")
    parser.add_argument("--test", action="store_true", help="Skicka en testnotis")
    parser.add_argument("--status", action="store_true", help="Visa nulaget, larma inte")
    parser.add_argument("--dry-run", action="store_true",
                        help="Kontrollera men skicka inga notiser och spara inget state")
    args = parser.parse_args()

    cfg = dict(CONFIG)
    setup_logging(cfg["LOG_FILE"])

    if args.test:
        ok = notify(
            cfg,
            "ferrywatch testnotis",
            f"Om du ser det här fungerar larmet.\n\n"
            f"Bevakar: {cfg['ROUTE_LABEL']} {cfg['DATE']} kl {cfg['TIME']}\n"
            f"Kanal: {cfg['NTFY_SERVER']}/{cfg['NTFY_TOPIC']}",
            priority="high",
            tags=["white_check_mark"],
            click=BOOKING_URL,
        )
        return 0 if ok else 1

    if args.status:
        try:
            show_status(cfg)
            return 0
        except (ApiError, SchemaError) as exc:
            print(f"Kunde inte hamta status: {exc}", file=sys.stderr)
            return 1

    if args.loop:
        return run_loop(cfg)

    outcome = run_once(cfg, dry_run=args.dry_run)
    log.info("Resultat: %s", outcome)
    return 0


if __name__ == "__main__":
    sys.exit(main())
