#!/usr/bin/env python3
"""Internship tracker: watches LinkedIn and Handshake for new Finance and
Data Analytics internships and notifies you when new ones appear.

Standard library only; no pip installs needed. Settings, the postings
database and postings.html live in DATA_DIR (see _data_dir below).

  python3 tracker.py            check once, notify about anything new
  python3 tracker.py --loop     keep checking every `interval_minutes`
  python3 tracker.py --list     print the 25 most recent postings seen
  python3 tracker.py --test-notify   send a test notification

For the desktop window, run app.py instead.
"""
import argparse
import contextlib
import email
import html
import imaplib
import json
import os
import platform
import random
import re
import smtplib
import sqlite3
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.message import EmailMessage

def _data_dir():
    if os.environ.get("INTERNSHIP_TRACKER_HOME"):
        d = os.environ["INTERNSHIP_TRACKER_HOME"]
    elif platform.system() == "Darwin":
        d = os.path.expanduser("~/Library/Application Support/InternshipTracker")
    elif platform.system() == "Windows":
        d = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "InternshipTracker")
    else:
        d = os.path.expanduser("~/.internship-tracker")
    os.makedirs(d, exist_ok=True)
    return d


DATA_DIR = _data_dir()
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
DB_PATH = os.path.join(DATA_DIR, "postings.db")
REPORT_PATH = os.path.join(DATA_DIR, "postings.html")
LOCK_PATH = os.path.join(DATA_DIR, "check.lock")       # exists while a check runs
LAST_PATH = os.path.join(DATA_DIR, "last_check.txt")   # touched when a check finishes
PROGRESS = {"done": 0, "total": 0, "new": 0}           # searches finished / to run, new postings so far

DEFAULT_CONFIG = {
    "interval_minutes": 60,
    "linkedin": {
        "enabled": True,
        "location": "United States",
        # Searches run against LinkedIn's public job search, filtered to
        # internships posted in the last `posted_within_hours`.
        "searches": [
            "finance intern",
            "financial analyst intern",
            "investment banking intern",
            "data analytics intern",
            "data analyst intern",
            "business analytics intern",
            "accounting intern",
            "audit intern",
            "tax intern",
            "marketing intern",
            "sales intern",
            "business development intern",
            "consulting intern",
            "strategy intern",
            "human resources intern",
            "supply chain intern",
            "operations intern",
            "economics intern",
            "business analyst intern",
            "business intern",
        ],
        "posted_within_hours": 2160,  # 90 days
        "remote_only": False,
    },
    "handshake": {
        # Handshake needs your school login, so the tracker reads the
        # "new jobs for your saved search" emails Handshake sends you.
        "enabled": False,
        "imap_host": "imap.gmail.com",
        "email": "",
        "app_password": "",
        "mailbox": "INBOX",
        "lookback_days": 3,
    },
    # A posting is kept only if its title matches one of these (case-insensitive)...
    "title_must_match_any": [
        "financ", "invest", "banking", "equity", "accounting", "treasury",
        "wealth", "fp&a", "credit", "capital", "asset management",
        "data", "analytic", "analyst", "business intelligence", "quant",
        "accountant", "audit", "tax", "marketing", "brand", "communications",
        "public relations", "advertis", "sales", "business development", "partnerships",
        "consult", "strategy", "strategic", "human resources", "talent", "recruit",
        "people operations", "supply chain", "logistics", "procurement", "purchasing",
        "sourcing", "operations", "econom", "business", "management",
    ],
    # ...and none of these.
    "title_exclude_any": ["senior", "sr.", "director", "manager", "principal", "lead "],
    "notify": {
        "desktop": True,
        # Phone push via the free ntfy app: install it, subscribe to a topic
        # name only you know, and put that name here.
        "ntfy_topic": "",
        "email_to": "",  # sends via the Handshake IMAP account's SMTP if set
        "smtp_host": "smtp.gmail.com",
    },
}

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0 Safari/537.36")


# ---------------------------------------------------------------- config / db

def save_config(cfg):
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


def load_config():
    if not os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "w") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)
    # Fill in any keys added in newer versions.
    for k, v in DEFAULT_CONFIG.items():
        if isinstance(v, dict):
            cfg.setdefault(k, {})
            for k2, v2 in v.items():
                cfg[k].setdefault(k2, v2)
        else:
            cfg.setdefault(k, v)
    return cfg


def open_db():
    db = sqlite3.connect(DB_PATH)
    db.execute("""CREATE TABLE IF NOT EXISTS postings (
        id TEXT PRIMARY KEY, source TEXT, title TEXT, company TEXT,
        location TEXT, url TEXT, posted TEXT, first_seen TEXT)""")
    cols = [r[1] for r in db.execute("PRAGMA table_info(postings)")]
    if "status" not in cols:  # new | seen | applied | hidden
        db.execute("ALTER TABLE postings ADD COLUMN status TEXT DEFAULT 'new'")
        db.commit()
    if "keywords" not in cols:  # the LinkedIn searches that turned this posting up, "; "-separated
        db.execute("ALTER TABLE postings ADD COLUMN keywords TEXT DEFAULT ''")
        db.commit()
    # Postings the title filter turned down, so a check knows it has seen them before.
    db.execute("CREATE TABLE IF NOT EXISTS skipped (id TEXT PRIMARY KEY)")
    db.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT)")
    return db


def _merge_keywords(old, new):
    out = [k for k in (old or "").split("; ") if k]
    for k in new:
        if k and k not in out:
            out.append(k)
    return "; ".join(out)


# ---------------------------------------------------------------- keyword box

def parse_query(q):
    """Split what's typed in the Keywords box into (must have, must not have).
    Words can come in any order; "quoted words" stay together; -word excludes."""
    want, skip = [], []
    for minus, phrase, word in re.findall(r'(-?)(?:"([^"]*)"|(\S+))', (q or "").lower()):
        term = (phrase or word).strip('"').strip()
        if term:
            (skip if minus else want).append(term)
    return want, skip


def query_match(parsed, text):
    want, skip = parsed
    t = text.lower()
    return all(w in t for w in want) and not any(w in t for w in skip)


# Field for each posting, decided by its title. The first field whose words
# match wins, so more specific fields come first.
FIELDS = [
    ("Data Analytics", ("data", "analytic", "business intelligence", "quant", "sql", "statistic")),
    ("Accounting", ("accounting", "accountant", "audit", "tax ", "tax,", "tax-", "taxation", "cpa")),
    ("Finance", ("financ", "invest", "banking", "banker", "equity", "treasury", "wealth", "fp&a",
                 "credit", "capital", "asset management", "trading", "underwriting", "actuar",
                 "mergers", "m&a", "valuation", "venture", "restructuring", "summer analyst")),
    ("Marketing", ("marketing", "brand", "communications", "public relations", "advertis",
                   "social media", "content", "seo", "digital media")),
    ("Sales & Business Development", ("sales", "business development", "partnerships", "account executive")),
    ("Consulting & Strategy", ("consult", "strategy", "strategic", "advisory")),
    ("Human Resources", ("human resources", "hr ", "hr intern", "people operations", "talent", "recruit")),
    ("Supply Chain & Operations", ("supply chain", "logistics", "procurement", "purchasing", "sourcing",
                                   "operations", "inventory")),
    ("Economics", ("econom",)),
]
GENERAL_FIELD = "Business (general)"
FIELD_NAMES = [name for name, _ in FIELDS] + [GENERAL_FIELD]


def category(title):
    t = f"{title.lower()} "
    for name, words in FIELDS:
        if any(w in t for w in words):
            return name
    return GENERAL_FIELD


STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California",
    "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia",
    "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois",
    "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana",
    "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota",
    "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada",
    "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York",
    "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon",
    "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia",
    "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}
# Metro areas LinkedIn names without a state ("Greater Boston", "Denver Metropolitan Area").
METROS = {
    "new york": "NY", "boston": "MA", "chicago": "IL", "los angeles": "CA", "san francisco": "CA",
    "bay area": "CA", "san diego": "CA", "seattle": "WA", "denver": "CO", "dallas": "TX",
    "houston": "TX", "austin": "TX", "atlanta": "GA", "miami": "FL", "philadelphia": "PA",
    "pittsburgh": "PA", "washington dc": "DC", "minneapolis": "MN", "milwaukee": "WI",
    "hartford": "CT", "detroit": "MI", "phoenix": "AZ", "charlotte": "NC", "nashville": "TN",
    "st. louis": "MO", "kansas city": "MO", "salt lake": "UT", "portland": "OR", "columbus": "OH",
    "cleveland": "OH", "cincinnati": "OH", "indianapolis": "IN", "raleigh": "NC", "tampa": "FL",
    "orlando": "FL", "baltimore": "MD", "richmond": "VA", "las vegas": "NV", "omaha": "NE",
}


def place(loc):
    """Which state a posting's location is in, as a dropdown label."""
    l = (loc or "").strip()
    low = l.lower()
    if "remote" in low:
        return "Remote"
    last = l.rsplit(",", 1)[-1].strip()
    if last.upper() in STATES and len(last) == 2:
        return STATES[last.upper()]
    for name in sorted(STATES.values(), key=len, reverse=True):
        if name.lower() in low and not (name == "Washington" and "dc" in low.split()):
            return name
    for metro, code in METROS.items():
        if metro in low:
            return STATES[code]
    return "Nationwide / other"


# ---------------------------------------------------------------- filtering

def title_ok(title, cfg):
    t = title.lower()
    if any(x in t for x in cfg["title_exclude_any"]):
        return False
    return any(x in t for x in cfg["title_must_match_any"])


# ---------------------------------------------------------------- LinkedIn

LI_SEARCH = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"


def fetch(url, retries=5):
    for attempt in range(retries):
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries - 1:  # asked to slow down
                time.sleep(6 * (attempt + 1) + random.uniform(0, 4))
                continue
            if e.code == 400:  # past the last page
                return ""
            raise
        except (urllib.error.URLError, TimeoutError):  # a dropped connection; try again
            if attempt == retries - 1:
                raise
            time.sleep(2 * (attempt + 1))
    return ""


def _text(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def parse_linkedin_cards(page):
    """Parse the HTML fragment LinkedIn's guest job search returns."""
    jobs = []
    for card in re.split(r"<li[\s>]", page)[1:]:
        m = re.search(r"urn:li:jobPosting:(\d+)", card)
        if not m:
            continue
        job_id = m.group(1)
        title = re.search(r'class="base-search-card__title"[^>]*>(.*?)</h3>', card, re.S)
        company = re.search(r'class="base-search-card__subtitle"[^>]*>(.*?)</h4>', card, re.S)
        loc = re.search(r'class="job-search-card__location"[^>]*>(.*?)</span>', card, re.S)
        posted = re.search(r'<time[^>]*datetime="([^"]+)"', card)
        jobs.append({
            "id": f"linkedin:{job_id}",
            "source": "LinkedIn",
            "title": _text(title.group(1)) if title else "",
            "company": _text(company.group(1)) if company else "",
            "location": _text(loc.group(1)) if loc else "",
            "url": f"https://www.linkedin.com/jobs/view/{job_id}/",
            "posted": posted.group(1) if posted else "",
        })
    return jobs


LI_PAGE = 10        # cards per page
LI_MAX_START = 490  # LinkedIn stops returning results after ~500 per search


LI_WORKERS = 4      # searches fetched at the same time


def _search_linkedin(li, kw, known, max_start):
    """Page through one search. With `known` (ids already saved), stop once two
    pages in a row hold nothing new (results are roughly newest first)."""
    jobs, known_streak = {}, 0
    for start in range(0, max_start + 1, LI_PAGE):
        params = {
            "keywords": kw,
            "location": li["location"],
            "f_JT": "I",  # internship
            "f_TPR": f"r{int(li['posted_within_hours']) * 3600}",
            "sortBy": "DD",  # newest first
            "start": start,
        }
        if li.get("remote_only"):
            params["f_WT"] = "2"
        try:
            page = fetch(LI_SEARCH + "?" + urllib.parse.urlencode(params))
        except Exception as e:
            print(f"  LinkedIn '{kw}': {e}")
            break
        cards = parse_linkedin_cards(page)
        for j in cards:
            jobs[j["id"]] = j
        if len(cards) < LI_PAGE:
            break
        if known is not None:
            known_streak = known_streak + 1 if all(j["id"] in known for j in cards) else 0
            if known_streak >= 2:
                break
        time.sleep(random.uniform(0.6, 1.4))  # be polite
    return list(jobs.values())


def check_linkedin(cfg, db=None, max_start=LI_MAX_START, on_search=None):
    """Fetch postings for every search, a few searches at a time. With `db`,
    each search stops paging once it reaches postings already saved; without
    it, page as deep as LinkedIn allows (used for the backfill). `on_search`
    is called with (keywords, postings) as each search finishes."""
    li = cfg["linkedin"]
    known = None if db is None else {r[0] for r in db.execute("SELECT id FROM postings UNION SELECT id FROM skipped")}
    found = {}
    with ThreadPoolExecutor(max_workers=LI_WORKERS) as pool:
        futures = {pool.submit(_search_linkedin, li, kw, known, max_start): kw for kw in li["searches"]}
        for fut in as_completed(futures):
            kw = futures[fut]
            jobs = [dict(j, keywords=[kw]) for j in fut.result()]
            if on_search:
                on_search(kw, jobs)
            for j in jobs:
                if j["id"] in found:
                    found[j["id"]]["keywords"].append(kw)
                else:
                    found[j["id"]] = dict(j)
    return list(found.values())


# ---------------------------------------------------------------- Handshake

HS_LINK = re.compile(r"https://[\w.-]*joinhandshake\.com/(?:[\w/-]*/)?(?:jobs|job-search|postings)/(\d+)[^\s\"'<>]*")


def parse_handshake_email(subject, body_html):
    """Pull job postings out of a Handshake alert email.

    Handshake alert emails list each job as a link to /jobs/<id> with the
    title as link text and the employer nearby. Tracking redirects are
    unwrapped where the real URL is in a query parameter.
    """
    body = html.unescape(body_html)
    jobs = {}
    for m in re.finditer(r'<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', body, re.S):
        href, text = m.group(1), _text(m.group(2))
        real = href
        q = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
        for v in sum(q.values(), []):
            if "joinhandshake.com" in v:
                real = v
        hm = HS_LINK.search(real)
        if not hm or not text or len(text) < 4:
            continue
        job_id = hm.group(1)
        # Employer is usually the next short text block after the title.
        rest = body[m.end():m.end() + 600]
        after = _text(rest.split("<a", 1)[0])
        company = after.split("  ")[0][:80] if after else ""
        prev = jobs.get(job_id)
        if prev and len(prev["title"]) >= len(text):
            continue
        jobs[job_id] = {
            "id": f"handshake:{job_id}",
            "source": "Handshake",
            "title": text,
            "company": company.split(" · ")[0],
            "location": "",
            "url": f"https://app.joinhandshake.com/jobs/{job_id}",
            "posted": "",
        }
    # Plain-text fallback: just links.
    if not jobs:
        for hm in HS_LINK.finditer(body):
            jobs[hm.group(1)] = {
                "id": f"handshake:{hm.group(1)}", "source": "Handshake",
                "title": subject, "company": "", "location": "",
                "url": f"https://app.joinhandshake.com/jobs/{hm.group(1)}", "posted": "",
            }
    return list(jobs.values())


def check_handshake(cfg):
    hs = cfg["handshake"]
    if not (hs["email"] and hs["app_password"]):
        print("  Handshake: add your email and app password to config.json to enable.")
        return []
    since = datetime.fromtimestamp(time.time() - hs["lookback_days"] * 86400).strftime("%d-%b-%Y")
    found = []
    with imaplib.IMAP4_SSL(hs["imap_host"]) as M:
        M.login(hs["email"], hs["app_password"])
        M.select(hs["mailbox"], readonly=True)
        _, data = M.search(None, f'(FROM "joinhandshake.com" SINCE {since})')
        for num in data[0].split():
            _, msg_data = M.fetch(num, "(RFC822)")
            msg = email.message_from_bytes(msg_data[0][1])
            subject = str(make_header(decode_header(msg.get("Subject", ""))))
            parts = [p for p in msg.walk() if p.get_content_type() in ("text/html", "text/plain")]
            parts.sort(key=lambda p: p.get_content_type() != "text/html")
            if not parts:
                continue
            payload = parts[0].get_payload(decode=True) or b""
            charset = parts[0].get_content_charset() or "utf-8"
            found += parse_handshake_email(subject, payload.decode(charset, "replace"))
    return found


# ---------------------------------------------------------------- notify

def notify(cfg, new):
    if not new:
        return
    n = cfg["notify"]
    head = f"{len(new)} new internship{'s' if len(new) != 1 else ''}"
    lines = [f"{j['title']} at {j['company']} ({j['source']})" for j in new]
    short = "\n".join(lines[:5]) + (f"\n+{len(new) - 5} more" if len(new) > 5 else "")

    if n.get("desktop"):
        desktop_notify(head, short)
    if n.get("ntfy_topic"):
        try:
            body = "\n".join(f"{l}\n{j['url']}" for l, j in zip(lines[:10], new[:10]))
            req = urllib.request.Request(
                f"https://ntfy.sh/{n['ntfy_topic']}", data=body.encode(),
                headers={"Title": head, "Tags": "briefcase", "Click": new[0]["url"]})
            urllib.request.urlopen(req, timeout=15)
        except Exception as e:
            print(f"  ntfy failed: {e}")
    if n.get("email_to") and cfg["handshake"]["email"] and cfg["handshake"]["app_password"]:
        try:
            msg = EmailMessage()
            msg["Subject"] = f"[Internship tracker] {head}"
            msg["From"] = cfg["handshake"]["email"]
            msg["To"] = n["email_to"]
            msg.set_content("\n\n".join(f"{l}\n{j['location']}\n{j['url']}" for l, j in zip(lines, new)))
            with smtplib.SMTP_SSL(n["smtp_host"]) as s:
                s.login(cfg["handshake"]["email"], cfg["handshake"]["app_password"])
                s.send_message(msg)
        except Exception as e:
            print(f"  email failed: {e}")


def desktop_notify(title, body):
    system = platform.system()
    try:
        if system == "Darwin":
            script = f'display notification {json.dumps(body)} with title {json.dumps(title)} sound name "Glass"'
            subprocess.run(["osascript", "-e", script], check=False)
        elif system == "Windows":
            ps = (
                "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] > $null;"
                "$t=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
                "$x=$t.GetElementsByTagName('text');"
                f"$x.Item(0).AppendChild($t.CreateTextNode({json.dumps(title)})) > $null;"
                f"$x.Item(1).AppendChild($t.CreateTextNode({json.dumps(body)})) > $null;"
                "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('Internship Tracker').Show([Windows.UI.Notifications.ToastNotification]::new($t))"
            )
            subprocess.run(["powershell", "-NoProfile", "-Command", ps], check=False)
        else:
            subprocess.run(["notify-send", title, body], check=False)
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------- report

def write_report(db):
    rows = db.execute("SELECT source, title, company, location, url, posted, first_seen "
                      "FROM postings ORDER BY first_seen DESC LIMIT 500").fetchall()
    trs = "\n".join(
        f"<tr><td>{html.escape(fs[:16].replace('T', ' '))}</td><td>{html.escape(src)}</td>"
        f"<td><a href=\"{html.escape(url)}\" target=_blank>{html.escape(t)}</a></td>"
        f"<td>{html.escape(c)}</td><td>{html.escape(l)}</td></tr>"
        for src, t, c, l, url, p, fs in rows)
    with open(REPORT_PATH, "w", encoding="utf-8") as f:
        f.write(f"""<!doctype html><meta charset=utf-8><title>Internships</title>
<style>body{{font:14px system-ui;margin:24px}}table{{border-collapse:collapse;width:100%}}
td,th{{padding:6px 8px;border-bottom:1px solid #ddd;text-align:left}}th{{background:#f4f4f4}}</style>
<h2>Finance &amp; Data Analytics internships ({len(rows)})</h2>
<table><tr><th>First seen</th><th>Source</th><th>Title</th><th>Company</th><th>Location</th></tr>
{trs}</table>""")


# ---------------------------------------------------------------- sharing checks

def check_in_progress():
    """True while another window (the app, the website or the command line) is checking."""
    try:
        age = time.time() - os.path.getmtime(LOCK_PATH)
        pid = int(open(LOCK_PATH).read().strip() or 0)
    except (OSError, ValueError):
        return False
    if age > 3600 or pid == os.getpid():
        return False
    if platform.system() == "Windows":
        # os.kill(pid, 0) would end the process on Windows, so ask the system instead.
        import ctypes
        k32 = ctypes.windll.kernel32
        handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        code = ctypes.c_ulong()
        alive = bool(k32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259  # STILL_ACTIVE
        k32.CloseHandle(handle)
        return alive
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        pass
    return True


def minutes_since_check():
    try:
        return (time.time() - os.path.getmtime(LAST_PATH)) / 60
    except OSError:
        return float("inf")


def auto_check_due(cfg):
    """Should a timer-driven check run now? Not if another window is already
    checking or one finished within the last half interval."""
    return not check_in_progress() and minutes_since_check() >= int(cfg["interval_minutes"]) / 2


# ---------------------------------------------------------------- main

def _store(db, j, now, status):
    """Insert a posting, or add its search keywords to the row already there.
    Returns True if it was new."""
    kws = j.get("keywords") or []
    row = db.execute("SELECT keywords FROM postings WHERE id=?", (j["id"],)).fetchone()
    if row:
        merged = _merge_keywords(row[0], kws)
        if merged != (row[0] or ""):
            db.execute("UPDATE postings SET keywords=? WHERE id=?", (merged, j["id"]))
        return False
    db.execute("INSERT INTO postings (id, source, title, company, location, url, posted, first_seen, status, keywords) "
               "VALUES (?,?,?,?,?,?,?,?,?,?)",
               (j["id"], j["source"], j["title"], j["company"], j["location"],
                j["url"], j["posted"], now, status, _merge_keywords("", kws)))
    return True


def keyword_search(cfg, db, query, pages=10):
    """One-off LinkedIn search for the Keywords box. Looks through the first
    `pages` pages of internships matching the typed words, and saves the ones
    not already in the list as seen (no notifications). Unlike the hourly
    check, titles don't have to match the business-major word list, since the
    user asked for these words specifically. Returns (found, added)."""
    want, _ = parse_query(query)
    kw = " ".join(want)
    if not kw:
        return 0, 0
    if "intern" not in kw:  # without it LinkedIn pads the results with regular jobs
        kw += " intern"
    one = dict(cfg, linkedin=dict(cfg["linkedin"], searches=[kw]))
    print(f"Searching LinkedIn for '{kw}'...")
    found = check_linkedin(one, None, max_start=(pages - 1) * LI_PAGE)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    added = 0
    for j in found:
        if any(x in j["title"].lower() for x in cfg["title_exclude_any"]):
            continue
        added += _store(db, j, now, "seen")
    db.commit()
    write_report(db)
    print(f"'{kw}': {len(found)} found on LinkedIn, {added} added to your list.")
    return len(found), added


def run_once(cfg, db, quiet_first_run=True, backfill=False):
    """Check for postings. backfill=True pages as deep as LinkedIn allows and
    records everything as already seen, without notifications."""
    first_run = db.execute("SELECT COUNT(*) FROM postings").fetchone()[0] == 0
    # Until one check has finished, saved postings don't mean a search was paged
    # to the end (the first check may have been cut short), so page all the way.
    deep = backfill or not os.path.exists(LAST_PATH)
    # Changed title words can let in postings that were turned down before, so look at everything again.
    filters = json.dumps([cfg["title_must_match_any"], cfg["title_exclude_any"]])
    old = db.execute("SELECT v FROM meta WHERE k='filters'").fetchone()
    deep = deep or (old is not None and old[0] != filters)
    now =datetime.now(timezone.utc).isoformat(timespec="seconds")
    candidates, new = [], []
    PROGRESS.update(done=0, total=len(cfg["linkedin"]["searches"]) if cfg["linkedin"]["enabled"] else 0, new=0)

    def save(jobs):
        """Save postings as they come in, so they show up while the check is still going."""
        for j in jobs:
            if not title_ok(j["title"], cfg) and j["source"] != "Handshake":
                db.execute("INSERT OR IGNORE INTO skipped VALUES (?)", (j["id"],))
                continue
            if _store(db, j, now, "seen" if backfill else "new"):
                new.append(j)
        db.commit()
        PROGRESS["new"] = len(new)

    def search_done(kw, jobs):
        save(jobs)
        PROGRESS["done"] += 1

    with open(LOCK_PATH, "w") as f:
        f.write(str(os.getpid()))
    try:
        if cfg["linkedin"]["enabled"]:
            print("Checking LinkedIn (full backfill)..." if deep else "Checking LinkedIn...")
            candidates += check_linkedin(cfg, None if deep else db, on_search=search_done)
        if cfg["handshake"]["enabled"]:
            print("Checking Handshake alert emails...")
            try:
                found = check_handshake(cfg)
                save(found)
                candidates += found
            except Exception as e:
                print(f"  Handshake: {e}")
    finally:
        with contextlib.suppress(OSError):
            os.remove(LOCK_PATH)
    with open(LAST_PATH, "w") as f:
        f.write(datetime.now().isoformat(timespec="seconds"))
    db.execute("INSERT OR REPLACE INTO meta VALUES ('filters', ?)", (filters,))
    db.commit()
    write_report(db)

    for j in new:
        print(f"  NEW  [{j['source']}] {j['title']} at {j['company']} ({j['location']})\n       {j['url']}")
    print(f"{len(new)} new posting(s); {len(candidates)} checked. Full list: {REPORT_PATH}")
    if backfill:
        pass
    elif first_run and quiet_first_run and new:
        # Don't blast dozens of alerts the very first time; just record them.
        desktop_notify("Internship tracker is set up",
                       f"Recorded {len(new)} current postings. You'll be notified about new ones.")
    else:
        notify(cfg, new)
    return new


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--loop", action="store_true", help="keep running, checking every interval_minutes")
    ap.add_argument("--list", action="store_true", help="print the most recent postings")
    ap.add_argument("--test-notify", action="store_true", help="send a test notification")
    ap.add_argument("--backfill", action="store_true",
                    help="fetch everything in the posted_within_hours window once, recorded as seen")
    args = ap.parse_args()

    cfg = load_config()
    db = open_db()
    if args.list:
        for row in db.execute("SELECT first_seen, source, title, company, url FROM postings "
                              "ORDER BY first_seen DESC LIMIT 25"):
            print(" | ".join(row))
        return
    if args.test_notify:
        notify(cfg, [{"title": "Test Finance Intern", "company": "Example Co", "source": "Test",
                      "location": "New York, NY", "url": "https://www.linkedin.com/jobs/"}])
        return
    if args.backfill:
        run_once(cfg, db, backfill=True)
        return
    while True:
        run_once(cfg, db)
        if not args.loop:
            break
        mins = cfg["interval_minutes"]
        print(f"Next check in {mins} minutes (Ctrl+C to stop).")
        time.sleep(mins * 60)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
