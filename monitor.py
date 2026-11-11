#!/usr/bin/env python3
"""Verbier Opening Session ticket monitor.

Polls the public availability endpoint that the official Verbier 4Vallees shop
page itself uses, and sends a push notification the moment CHF 5 Opening
Session tickets come back in stock. It never logs in, never adds to a basket
and never buys anything: you book by hand.

Standard library only (Python 3.9+).
"""
from __future__ import annotations

import csv
import json
import os
import random
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

ZURICH = ZoneInfo("Europe/Zurich")
BASE = "https://verbier4vallees.ch"
DATES_PATH = "/en/shop/product/available-dates-times"

PRODUCTS = {
    "410650": {
        "name": "Opening Session (ticket only)",
        "url": f"{BASE}/en/online-shop/activities/opening-session_activity_410650",
    },
    "426820": {
        "name": "Opening Session + St. Nicholas Express (with train)",
        "url": f"{BASE}/en/online-shop/activities/opening-session-st.nicholas-express_activity_426820",
    },
}

LOG_FILE = Path(os.environ.get("EVENT_LOG", "data/events.csv"))
LOG_FIELDS = ["utc", "zurich", "product_id", "product", "available", "max", "event"]


def env(name: str, default: str = "") -> str:
    value = os.environ.get(name, "")
    return value.strip() if value.strip() else default


@dataclass
class Config:
    products: list[str]
    event_date: str
    min_tickets: int
    poll_seconds: int
    hot_poll_seconds: int
    hot_window: tuple[str, str]
    weekend_poll_seconds: int
    active_hours: tuple[str, str]
    end_date: str
    run_minutes: int
    once: bool
    heartbeat: bool

    @classmethod
    def from_env(cls) -> "Config":
        def window(raw: str) -> tuple[str, str]:
            a, b = raw.split("-")
            return a.strip(), b.strip()

        return cls(
            products=[p.strip() for p in env("PRODUCTS", "410650,426820").split(",") if p.strip()],
            event_date=env("EVENT_DATE", "2026-12-05"),
            min_tickets=int(env("MIN_TICKETS", "1")),
            poll_seconds=int(env("POLL_SECONDS", "60")),
            hot_poll_seconds=int(env("HOT_POLL_SECONDS", "20")),
            hot_window=window(env("HOT_WINDOW", "07:30-10:00")),
            weekend_poll_seconds=int(env("WEEKEND_POLL_SECONDS", "180")),
            active_hours=window(env("ACTIVE_HOURS", "06:00-23:30")),
            end_date=env("END_DATE", "2026-12-04"),
            run_minutes=int(env("RUN_MINUTES", "330")),
            once=env("ONCE", "false").lower() == "true",
            heartbeat=env("HEARTBEAT", "true").lower() == "true",
        )


# ---------------------------------------------------------------- time rules

def _hm(s: str) -> tuple[int, int]:
    h, m = s.split(":")
    return int(h), int(m)


def in_window(now: datetime, window: tuple[str, str]) -> bool:
    local = now.astimezone(ZURICH)
    cur = (local.hour, local.minute)
    return _hm(window[0]) <= cur < _hm(window[1])


def is_weekday(now: datetime) -> bool:
    return now.astimezone(ZURICH).weekday() < 5


def within_campaign(now: datetime, cfg: Config) -> bool:
    return now.astimezone(ZURICH).date() <= date.fromisoformat(cfg.end_date)


def poll_interval(now: datetime, cfg: Config) -> int:
    if not is_weekday(now):
        return cfg.weekend_poll_seconds
    if in_window(now, cfg.hot_window):
        return cfg.hot_poll_seconds
    return cfg.poll_seconds


# ------------------------------------------------------------------ parsing

class SiteError(Exception):
    pass


class AccessDenied(SiteError):
    pass


class BackOff(SiteError):
    def __init__(self, seconds: int):
        super().__init__(f"site asked us to back off for {seconds}s")
        self.seconds = seconds


def parse_availability(data: object, product_id: str, event_date: str) -> tuple[int, int]:
    """Return (available, max) for the event date, or raise SiteError if the
    response does not look exactly like what the shop normally returns."""
    if not isinstance(data, dict) or data.get("success") is not True:
        raise SiteError("unexpected response (success != true)")
    rows = data.get("availabilityByDate")
    if not isinstance(rows, dict):
        raise SiteError("unexpected response (no availabilityByDate)")
    row = rows.get(f"{product_id}-{event_date}")
    if row is None:
        row = next((r for r in rows.values() if isinstance(r, dict) and r.get("date") == event_date), None)
    if not isinstance(row, dict):
        raise SiteError(f"event date {event_date} missing from response (dates: {sorted(rows)})")
    available, maximum = row.get("available"), row.get("max")
    if not isinstance(available, int) or isinstance(available, bool) or available < 0:
        raise SiteError(f"unexpected 'available' value: {available!r}")
    if not isinstance(maximum, int) or isinstance(maximum, bool):
        maximum = -1
    return available, maximum


# ------------------------------------------------------------------- HTTP

def user_agent() -> str:
    repo = env("GITHUB_REPOSITORY")
    contact = f"+https://github.com/{repo}" if repo else "personal use"
    return f"VerbierTicketMonitor/1.0 ({contact}; personal availability alert, no purchasing)"


def http_get(url: str, accept: str = "application/json", timeout: int = 20) -> tuple[bytes, dict]:
    req = urllib.request.Request(url, headers={
        "User-Agent": user_agent(),
        "Accept": accept,
        "Cache-Control": "no-cache",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read(), {k.lower(): v for k, v in r.headers.items()}
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise AccessDenied(f"HTTP {e.code} from {url}") from e
        if e.code in (429, 503):
            raw = e.headers.get("Retry-After", "")
            seconds = int(raw) if raw.isdigit() else 600
            raise BackOff(max(seconds, 300)) from e
        raise SiteError(f"HTTP {e.code} from {url}") from e
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as e:
        raise SiteError(f"network error: {e}") from e


def robots_allows(path: str) -> bool:
    body, _ = http_get(f"{BASE}/robots.txt", accept="text/plain")
    rp = urllib.robotparser.RobotFileParser()
    rp.parse(body.decode("utf-8", "replace").splitlines())
    return rp.can_fetch(user_agent(), f"{BASE}{path}")


def fetch_availability(product_id: str, event_date: str) -> tuple[int, int]:
    url = f"{BASE}{DATES_PATH}?" + urllib.parse.urlencode({"activity_id": product_id})
    body, headers = http_get(url)
    if "json" not in headers.get("content-type", ""):
        raise SiteError(f"non-JSON response ({headers.get('content-type')}) - possible bot challenge")
    try:
        data = json.loads(body)
    except json.JSONDecodeError as e:
        raise SiteError("invalid JSON") from e
    return parse_availability(data, product_id, event_date)


# ----------------------------------------------------------- notifications

def notify(title: str, message: str, *, urgent: bool, click: str = "", tags: list[str] | None = None) -> None:
    """Send to every channel configured. Never raises."""
    sent = False
    topic = env("NTFY_TOPIC")
    if topic:
        payload = {
            "topic": topic,
            "title": title,
            "message": message,
            "priority": 5 if urgent else 2,
            "tags": tags or [],
        }
        if click:
            payload["click"] = click
            payload["actions"] = [{"action": "view", "label": "Open shop", "url": click, "clear": True}]
        if urgent and env("NTFY_EMAIL"):
            payload["email"] = env("NTFY_EMAIL")
        headers = {"Content-Type": "application/json", "User-Agent": "VerbierTicketMonitor/1.0"}
        if env("NTFY_TOKEN"):
            headers["Authorization"] = f"Bearer {env('NTFY_TOKEN')}"
        sent |= _post(env("NTFY_SERVER", "https://ntfy.sh").rstrip("/"), payload, headers, "ntfy")

    bot, chat = env("TELEGRAM_BOT_TOKEN"), env("TELEGRAM_CHAT_ID")
    if bot and chat:
        text = f"{title}\n\n{message}" + (f"\n\n{click}" if click else "")
        sent |= _post(f"https://api.telegram.org/bot{bot}/sendMessage",
                      {"chat_id": chat, "text": text, "disable_notification": not urgent},
                      {"Content-Type": "application/json"}, "telegram")

    if not sent:
        print(f"[notify:none-configured-or-failed] {title} | {message}", flush=True)


def _post(url: str, payload: dict, headers: dict, label: str) -> bool:
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=15) as r:
                if 200 <= r.status < 300:
                    print(f"[notify:{label}] sent", flush=True)
                    return True
        except Exception as e:  # noqa: BLE001 - notification must never crash the monitor
            print(f"[notify:{label}] attempt {attempt + 1} failed: {e}", flush=True)
        time.sleep(2 * (attempt + 1))
    return False


# -------------------------------------------------------------- event log

def log_event(product_id: str, available: int, maximum: int, event: str, now: datetime) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    new = not LOG_FILE.exists()
    with LOG_FILE.open("a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=LOG_FIELDS)
        if new:
            w.writeheader()
        w.writerow({
            "utc": now.astimezone(timezone.utc).isoformat(timespec="seconds"),
            "zurich": now.astimezone(ZURICH).isoformat(timespec="seconds"),
            "product_id": product_id,
            "product": PRODUCTS.get(product_id, {}).get("name", product_id),
            "available": available,
            "max": maximum,
            "event": event,
        })


def commit_log(reason: str) -> None:
    """Commit and push the event log (only inside GitHub Actions)."""
    if env("COMMIT_EVENTS", "false").lower() != "true" or not LOG_FILE.exists():
        return
    def git(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *args], capture_output=True, text=True)
    git("add", str(LOG_FILE))
    if git("diff", "--cached", "--quiet").returncode == 0:
        return
    git("-c", "user.name=ticket-monitor[bot]", "-c", "user.email=ticket-monitor@users.noreply.github.com",
        "commit", "-m", f"Log: {reason} [skip ci]")
    for _ in range(3):
        git("pull", "--rebase", "--autostash", "-q")
        if git("push", "-q").returncode == 0:
            print(f"[log] pushed: {reason}", flush=True)
            return
        time.sleep(3)
    print("[log] push failed (log kept locally for next attempt)", flush=True)


# ------------------------------------------------------------ core logic

@dataclass
class ProductState:
    available: int | None = None
    maximum: int | None = None
    in_stock_since: datetime | None = None


def evaluate(prev: ProductState, available: int, maximum: int, min_tickets: int) -> str | None:
    """Decide what happened between two observations.

    Returns one of: 'release' (stock appeared), 'restock' (more released while
    already in stock), 'sold_out', 'change' (any other change), or None.
    """
    was_in = prev.available is not None and prev.available >= min_tickets
    now_in = available >= min_tickets
    if now_in and not was_in:
        return "release"
    if was_in and not now_in:
        return "sold_out"
    if now_in and prev.maximum is not None and maximum > prev.maximum:
        return "restock"
    if prev.available != available or prev.maximum != maximum:
        return "change" if prev.available is not None else "baseline"
    return None


def handle(pid: str, state: ProductState, available: int, maximum: int, cfg: Config, now: datetime) -> str | None:
    event = evaluate(state, available, maximum, cfg.min_tickets)
    info = PRODUCTS.get(pid, {"name": pid, "url": f"{BASE}/en/online-shop"})
    local = now.astimezone(ZURICH).strftime("%H:%M:%S")
    released = ""
    if state.maximum is not None and maximum > state.maximum:
        released = f" ({maximum - state.maximum} newly released)"

    if event in ("release", "restock"):
        state.in_stock_since = state.in_stock_since or now
        verb = "AVAILABLE NOW" if event == "release" else "MORE RELEASED"
        notify(
            f"CHF 5 Verbier tickets {verb}",
            f"{available} x {info['name']} for {cfg.event_date}{released}.\n"
            f"Seen at {local} Swiss time. Book immediately - drops sell out fast. "
            f"Remember: St Nicholas costume required on the day.",
            urgent=True, click=info["url"], tags=["rotating_light", "ski"],
        )
    elif event == "sold_out":
        lasted = ""
        if state.in_stock_since:
            lasted = f" after ~{int((now - state.in_stock_since).total_seconds() // 60)} min"
        state.in_stock_since = None
        notify(f"Sold out again: {info['name']}",
               f"Back to 0 at {local} Swiss time{lasted}. Total released so far: {maximum}.",
               urgent=False, click=info["url"], tags=["x"])

    if event:
        log_event(pid, available, maximum, event, now)
        print(f"[{local}] {pid} available={available} max={maximum} event={event}", flush=True)
    state.available, state.maximum = available, maximum
    return event


# --------------------------------------------------------- GitHub chaining

def gh_api(method: str, path: str, body: dict | None = None) -> dict | None:
    token, repo = env("GITHUB_TOKEN"), env("GITHUB_REPOSITORY")
    if not token or not repo:
        return None
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        method=method,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "VerbierTicketMonitor/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read()
            return json.loads(raw) if raw else {}
    except Exception as e:  # noqa: BLE001
        print(f"[github] {method} {path} failed: {e}", flush=True)
        return None


def older_run_active() -> bool:
    """True if an older run of this workflow is still going (so we should not double-poll)."""
    my_id = int(env("GITHUB_RUN_ID", "0") or 0)
    if not my_id:
        return False
    workflow = env("WORKFLOW_FILE", "monitor.yml")
    # A dispatched run is usually a hand-off: give the predecessor ~5 min to
    # finish committing. A cron safety-net run just checks once and leaves.
    attempts = 10 if env("GITHUB_EVENT_NAME") == "workflow_dispatch" else 1
    for i in range(attempts):
        busy = False
        for status in ("in_progress", "queued"):
            data = gh_api("GET", f"/actions/workflows/{workflow}/runs?status={status}&per_page=50") or {}
            if any(r.get("id", 0) < my_id for r in data.get("workflow_runs", [])):
                busy = True
        if not busy:
            return False
        if i < attempts - 1:
            time.sleep(30)
    return True


def dispatch_next_run() -> None:
    workflow = env("WORKFLOW_FILE", "monitor.yml")
    ref = env("GITHUB_REF_NAME", "main")
    if gh_api("POST", f"/actions/workflows/{workflow}/dispatches", {"ref": ref}) is not None:
        print("[github] dispatched follow-up run", flush=True)


# -------------------------------------------------------------------- main

def main() -> int:
    cfg = Config.from_env()
    start = datetime.now(timezone.utc)

    if not within_campaign(start, cfg):
        print(f"Campaign over (END_DATE={cfg.end_date}). Disable the workflow.")
        return 0
    if not cfg.once and not in_window(start, cfg.active_hours):
        print(f"Outside active hours {cfg.active_hours} Europe/Zurich; exiting.")
        return 0
    if not cfg.once and older_run_active():
        print("Another monitor run is already active; exiting to avoid duplicate polling.")
        return 0

    if not robots_allows(DATES_PATH):
        notify("Verbier monitor stopped", "robots.txt now disallows the availability endpoint. "
               "Monitoring halted out of respect for the site.", urgent=True)
        return 2

    stop = {"flag": False}
    signal.signal(signal.SIGTERM, lambda *_: stop.update(flag=True))

    deadline = start + timedelta(minutes=cfg.run_minutes)
    states = {pid: ProductState() for pid in cfg.products}
    consecutive_errors, error_alerted, first_cycle = 0, False, True

    while not stop["flag"]:
        now = datetime.now(timezone.utc)
        try:
            for pid in cfg.products:
                available, maximum = fetch_availability(pid, cfg.event_date)
                event = handle(pid, states[pid], available, maximum, cfg, now)
                if event == "sold_out":
                    commit_log(f"{pid} sold out")
            if consecutive_errors and error_alerted:
                notify("Verbier monitor recovered", "Availability checks are working again.", urgent=False)
            consecutive_errors, error_alerted = 0, False
        except AccessDenied as e:
            notify("Verbier monitor stopped", f"The site refused access ({e}). Not retrying or bypassing; "
                   "check the site and restart manually.", urgent=True)
            commit_log("halted")
            return 3
        except BackOff as e:
            print(f"[backoff] {e}", flush=True)
            time.sleep(e.seconds)
            continue
        except SiteError as e:
            consecutive_errors += 1
            print(f"[error {consecutive_errors}] {e}", flush=True)
            if consecutive_errors >= 8 and not error_alerted:
                notify("Verbier monitor having problems",
                       f"{consecutive_errors} failed checks in a row: {e}. You may miss a drop - check manually.",
                       urgent=False, click=PRODUCTS.get(cfg.products[0], {}).get("url", BASE))
                error_alerted = True

        if first_cycle:
            first_cycle = False
            if cfg.heartbeat and consecutive_errors == 0:
                summary = ", ".join(f"{PRODUCTS.get(p, {}).get('name', p)}: {s.available}/{s.maximum}"
                                    for p, s in states.items())
                notify("Verbier monitor running", f"Watching every {poll_interval(now, cfg)}s. Now: {summary}",
                       urgent=False, tags=["eyes"])

        if cfg.once:
            break
        now = datetime.now(timezone.utc)
        if now >= deadline or not in_window(now, cfg.active_hours) or not within_campaign(now, cfg):
            break
        time.sleep(poll_interval(now, cfg) + random.uniform(0, 3))

    commit_log("end of run")
    now = datetime.now(timezone.utc)
    if not cfg.once and not stop["flag"] and in_window(now, cfg.active_hours) and within_campaign(now, cfg):
        dispatch_next_run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
