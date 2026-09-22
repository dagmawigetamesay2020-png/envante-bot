"""
Envantē ē+ job watcher -> Telegram alerts (Version 2)

How it works (same steps the website's own JavaScript uses):
  1. POST email + password to https://api.envante.it/api/login  -> get a token
  2. POST to https://api.envante.it/api/inventari with the token -> list of jobs
  3. Compare job ids with the ones seen before (saved in seen_jobs.json)
  4. Send a Telegram message for every new job

Usage:
  python bot.py --chat-id   -> prints your Telegram chat id
  python bot.py --show      -> logs in and prints the current jobs (test)
  python bot.py             -> runs the watcher forever
  python bot.py --once      -> one check, then exit (used by GitHub Actions)
"""

import json
import os
import sys
import time

import requests
from dotenv import load_dotenv

load_dotenv()

EMAIL = os.getenv("ENVANTE_EMAIL")
PASSWORD = os.getenv("ENVANTE_PASSWORD")
TG_TOKEN = os.getenv("TELEGRAM_TOKEN")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
INTERVAL_SECONDS = int(os.getenv("CHECK_EVERY_MINUTES", "15")) * 60
# Location filters (comma separated, not case-sensitive). Empty = no filter.
# A job must match EVERY filter that is filled in.
def _words(name):
    return [w.strip().lower() for w in os.getenv(name, "").split(",") if w.strip()]

FILTER_REGIONE = _words("FILTER_REGIONE")
FILTER_PROVINCIA = _words("FILTER_PROVINCIA")
FILTER_CITTA = _words("FILTER_CITTA")

API = "https://api.envante.it/api"
SITE = "https://job.envante.it/"
STATE_FILE = "seen_jobs.json"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
    "Accept": "application/json",
    "Origin": "https://job.envante.it",
    "Referer": "https://job.envante.it/",
}


# ---------------------------------------------------------------- Telegram

def send_telegram(text: str) -> None:
    r = requests.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage",
        data={"chat_id": TG_CHAT_ID, "text": text[:4000],
              "disable_web_page_preview": True},
        timeout=20,
    )
    r.raise_for_status()


def print_chat_id() -> None:
    data = requests.get(f"https://api.telegram.org/bot{TG_TOKEN}/getUpdates",
                        timeout=20).json()
    for update in data.get("result", []):
        chat = (update.get("message") or {}).get("chat")
        if chat:
            print("Your chat id is:", chat["id"])
            return
    print("No messages found. Send your bot 'hi' in Telegram, then run again.")


# ---------------------------------------------------------------- ē+ API

def login() -> str:
    """Send email + password, get back the token (the 'wristband')."""
    r = requests.post(f"{API}/login", headers=HEADERS,
                      data={"email": EMAIL, "password": PASSWORD}, timeout=30)
    try:
        body = r.json()
    except ValueError:
        raise RuntimeError(f"Login: unexpected reply (status {r.status_code})")
    if r.status_code != 200:
        raise RuntimeError(f"Login failed: {body.get('message', r.status_code)}")

    token = body.get("data")
    if isinstance(token, dict):                 # just in case it's wrapped
        token = token.get("token") or token.get("access_token")
    if not token:
        raise RuntimeError("Login: no token in the reply")
    return str(token)


def get_jobs(token: str) -> list[dict]:
    """Ask for all jobs, exactly like the site does with the filters on 'all'."""
    r = requests.post(
        f"{API}/inventari",
        headers={**HEADERS, "Authorization": f"Bearer {token}"},
        data={"regione": "all", "provincia": "all", "citta": "all",
              "inizio": "", "fine": ""},
        timeout=30,
    )
    if r.status_code in (401, 403):
        raise PermissionError("token expired")
    r.raise_for_status()
    return r.json()["data"]["inventari"]


def field(job: dict, *names, default="?"):
    for n in names:
        if job.get(n) not in (None, ""):
            return job[n]
    return default


def describe(job: dict) -> str:
    orario = job.get("Orario")
    if not orario:
        orario = {"0": "Mattina", "1": "Pomeriggio", "2": "Sera"}.get(
            str(job.get("fascia_oraria")), "Da definire")
    elif len(str(orario)) >= 8:                 # "08:30:00" -> "08:30"
        orario = str(orario)[:5]
    city = field(job, "Città", "Citta", "citta", "CittÃ ")
    return (f"📅 {field(job, 'Data')}  🕒 {orario}\n"
            f"📍 {city} ({field(job, 'Provincia')}), {field(job, 'Regione')}")


def matches_filter(job: dict) -> bool:
    checks = [
        (FILTER_REGIONE, field(job, "Regione", default="")),
        (FILTER_PROVINCIA, field(job, "Provincia", default="")),
        (FILTER_CITTA, field(job, "Città", "Citta", "citta", "CittÃ ", default="")),
    ]
    for wanted, value in checks:
        if wanted and not any(w in str(value).lower() for w in wanted):
            return False
    return True


# ---------------------------------------------------------------- Memory

def load_seen():
    if not os.path.exists(STATE_FILE):
        return None
    with open(STATE_FILE, encoding="utf-8") as f:
        return set(json.load(f))


def save_seen(ids) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(sorted(ids), f)


# ---------------------------------------------------------------- Main loop

class Watcher:
    def __init__(self):
        self.token = None

    def jobs(self) -> list[dict]:
        if self.token is None:
            self.token = login()
        try:
            return get_jobs(self.token)
        except PermissionError:                 # wristband expired: log in again
            self.token = login()
            return get_jobs(self.token)

    def check_once(self) -> None:
        jobs = [j for j in self.jobs() if matches_filter(j)]
        ids = {str(j.get("id")) for j in jobs}
        seen = load_seen()

        if seen is None:                        # first run
            save_seen(ids)
            send_telegram(f"✅ Watcher started. {len(jobs)} jobs listed right now. "
                          f"I'll message you when a new one appears.\n{SITE}")
            return

        new = [j for j in jobs if str(j.get("id")) not in seen]
        for job in new[:20]:
            send_telegram(f"🆕 New Envantē job!\n\n{describe(job)}\n\n{SITE}")
        if len(new) > 20:
            send_telegram(f"...and {len(new) - 20} more new jobs. {SITE}")
        save_seen(seen | ids)


ERROR_FILE = "last_error.txt"


def run_once(watcher: "Watcher") -> None:
    """One check. Errors are reported on Telegram only when they change,
    so a broken site doesn't send you a message every 15 minutes."""
    previous = open(ERROR_FILE, encoding="utf-8").read() if os.path.exists(ERROR_FILE) else ""
    try:
        watcher.check_once()
        current = ""
        print("checked OK")
    except Exception as e:
        current = str(e)
        print("Error:", current)
        if current != previous:
            try:
                send_telegram(f"⚠️ Watcher problem: {current}")
            except Exception:
                pass
    if current == "" and previous:
        send_telegram("✅ Watcher is working again.")
    with open(ERROR_FILE, "w", encoding="utf-8") as f:
        f.write(current)


def main() -> None:
    missing = [k for k, v in {"ENVANTE_EMAIL": EMAIL, "ENVANTE_PASSWORD": PASSWORD,
                              "TELEGRAM_TOKEN": TG_TOKEN}.items() if not v]
    if missing:
        sys.exit(f"Missing in .env: {', '.join(missing)}")

    if "--chat-id" in sys.argv:
        print_chat_id()
        return

    watcher = Watcher()

    if "--show" in sys.argv:
        jobs = watcher.jobs()
        print(f"Logged in OK. {len(jobs)} jobs found.\n")
        mine = [j for j in jobs if matches_filter(j)]
        print(f"{len(mine)} of them match your filters:\n")
        for job in mine:
            print(describe(job), "\n")
        if jobs:
            print("Fields in one job:", list(jobs[0].keys()))
        return

    if not TG_CHAT_ID:
        sys.exit("Missing TELEGRAM_CHAT_ID in .env (run: python bot.py --chat-id)")

    if "--once" in sys.argv:
        run_once(watcher)
        return

    last_error = None
    while True:
        try:
            watcher.check_once()
            last_error = None
            print(time.strftime("%H:%M"), "checked OK")
        except Exception as e:
            print("Error:", e)
            watcher.token = None
            if str(e) != last_error:            # don't spam the same error
                try:
                    send_telegram(f"⚠️ Watcher problem: {e}")
                except Exception:
                    pass
                last_error = str(e)
        time.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
