#!/usr/bin/env python3
"""Hourly check for the shared Internship Tracker website.

Runs on GitHub Actions. Downloads the list the website is showing now, asks
LinkedIn for new internships with the same searches as the desktop tracker
(tracker.py), and writes the updated list for the website to publish.

  python3 update.py PREVIOUS_URL OUT_DIR

PREVIOUS_URL is where the live site serves its files (".../postings.json",
".../seen.json" and ".../d/NN.json" are fetched from it). A 404 means this is
the first run, so the check pages back through the last 90 days. OUT_DIR gets
postings.json (what the page shows), seen.json (ids the title filter turned
down, so later checks know they've been seen) and d/00.json to d/99.json (job
descriptions for postings from the last 30 days, split by the last two digits
of the posting id so the page loads only the one it needs).
"""
import html
import json
import os
import random
import re
import sqlite3
import sys
import tempfile
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

# tracker.py keeps its settings and database in a data folder; give it a
# throwaway one, since this script keeps its state in the published files.
os.environ.setdefault("INTERNSHIP_TRACKER_HOME", tempfile.mkdtemp())
import tracker  # noqa: E402

# Postings stay on the site for good once found. Only the ids the title filter
# turned down are forgotten after a while (LinkedIn's search reaches 90 days back).
SEEN_DAYS = 92

# Descriptions come from each posting's own page, one request per posting, so
# they're only fetched for postings from the last 30 days, a few at a time,
# newest first; a backlog is worked through over several hourly runs.
DESC_DAYS = 30
DESC_MAX = 400       # per run; the rest wait for the next hourly run
DESC_WORKERS = 3
LI_POSTING = "https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/"


def shard(pid):
    return pid[-2:] if pid[-2:].isdigit() else "00"


def get_json(base, name):
    """A file the live site serves; {} if it isn't there. Any other failure
    stops the run so a hiccup can't wipe the list."""
    try:
        with urllib.request.urlopen(f"{base.rstrip('/')}/{name}", timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
        return {}


def get_previous(base):
    """(postings, seen ids, first-run time, descriptions) the site serves now; empty on the first run."""
    posts, seen = get_json(base, "postings.json"), get_json(base, "seen.json")
    descs = get_json(base, "descriptions.json").get("descriptions", {})  # the single file used before d/
    for n in range(100):
        descs.update(get_json(base, f"d/{n:02d}.json"))
    return ({p["id"]: p for p in posts.get("postings", [])}, seen.get("seen", {}),
            posts.get("started_at"), descs)


def description_text(page):
    """The job description from a LinkedIn guest posting page, as plain text
    with line breaks and bullets kept. "" if the page has none."""
    m = (re.search(r'<div class="show-more-less-html__markup[^"]*"[^>]*>', page)
         or re.search(r'<div class="description__text[^"]*"[^>]*>', page))
    if not m:
        return ""
    # The text can hold its own <div>s, so read up to the "Show more" button
    # or the end of the section rather than the first </div>.
    rest = page[m.end():]
    ends = [i for i in (rest.find("<button"), rest.find("</section>")) if i >= 0]
    t = rest[:min(ends)] if ends else rest[:20000]
    t = re.sub(r"<br\s*/?>", "\n", t, flags=re.I)
    t = re.sub(r"<li(?:\s[^>]*)?>", "\n• ", t, flags=re.I)
    t = re.sub(r"</(p|div|ul|ol|h\d)>", "\n\n", t, flags=re.I)
    t = re.sub(r"<(?:p|div|ul|ol|h\d)(?:\s[^>]*)?>", "\n", t, flags=re.I)
    t = html.unescape(re.sub(r"<[^>]+>", "", t))
    lines = [re.sub(r"[ \t\xa0]+", " ", l).strip() for l in t.split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def fetch_description(pid):
    try:
        page = tracker.fetch(LI_POSTING + pid.split(":", 1)[1])
    except Exception as e:  # taken down, or LinkedIn said no; try again next run
        print(f"  description {pid}: {e}")
        return pid, ""
    time.sleep(random.uniform(0.5, 1.2))  # be polite
    return pid, description_text(page)


def main(base, out_dir):
    cfg = tracker.load_config()  # the defaults: all business fields, United States, 90 days
    old, seen, started, descs = get_previous(base)
    first_run = not old
    now = datetime.now(timezone.utc)
    stamp = now.isoformat(timespec="seconds")
    # Everything the first run finds was already posted; the page treats only
    # postings found after it as new.
    started = started or stamp

    # check_linkedin stops paging a search once it reaches postings it knows,
    # and reads what it knows from a database; hand it one with these ids.
    db = sqlite3.connect(":memory:")
    db.execute("CREATE TABLE postings (id TEXT)")
    db.execute("CREATE TABLE skipped (id TEXT)")
    db.executemany("INSERT INTO postings VALUES (?)", [(i,) for i in old])
    db.executemany("INSERT INTO skipped VALUES (?)", [(i,) for i in seen])

    print("First run: fetching the last 90 days..." if first_run else f"Checking LinkedIn ({len(old)} postings known)...")
    found = tracker.check_linkedin(cfg, None if first_run else db)
    print(f"{len(found)} postings returned by LinkedIn.")

    posts = dict(old)
    for j in found:
        if j["id"] in posts:
            p = posts[j["id"]]
            p["keywords"] = tracker._merge_keywords(p.get("keywords"), j.get("keywords") or [])
            continue
        if not tracker.title_ok(j["title"], cfg):
            seen.setdefault(j["id"], now.date().isoformat())
            continue
        posts[j["id"]] = {
            "id": j["id"], "title": j["title"], "company": j["company"], "location": j["location"],
            "source": j["source"], "url": j["url"], "posted": j["posted"], "found": stamp,
            "field": tracker.category(j["title"]), "place": tracker.place(j["location"]),
            "keywords": "; ".join(j.get("keywords") or []),
        }

    cutoff = (now - timedelta(days=SEEN_DAYS)).date().isoformat()
    seen = {i: d for i, d in seen.items() if d >= cutoff}
    added = sum(p["found"] == stamp for p in posts.values())

    # Descriptions for postings from the last day that don't have one yet.
    since = (now - timedelta(days=DESC_DAYS)).date().isoformat()
    day = lambda p: p.get("posted") or p["found"][:10]
    missing = sorted((i for i, p in posts.items() if i.startswith("linkedin:") and i not in descs
                      and day(p) >= since), key=lambda i: day(posts[i]), reverse=True)
    want = missing[:DESC_MAX]
    if want:
        print(f"Fetching {len(want)} of {len(missing)} missing description(s)...")
        with ThreadPoolExecutor(max_workers=DESC_WORKERS) as pool:
            got = {pid: text for pid, text in pool.map(fetch_description, want) if text}
        descs.update(got)
        print(f"  {len(got)} saved.")
    descs = {i: d for i, d in descs.items() if i in posts}
    for i, p in posts.items():
        p["d"] = 1 if i in descs else 0

    error = None
    if not found and old:
        error = "LinkedIn didn't return anything on the last check"
        print(f"Warning: {error}. Keeping the current list.")

    os.makedirs(out_dir, exist_ok=True)
    rows = sorted(posts.values(), key=lambda p: (p.get("posted") or "", p["found"]), reverse=True)
    with open(os.path.join(out_dir, "postings.json"), "w") as f:
        json.dump({"checked_at": stamp, "started_at": started, "added": added, "error": error,
                   "fields": tracker.FIELD_NAMES, "postings": rows}, f, separators=(",", ":"))
    with open(os.path.join(out_dir, "seen.json"), "w") as f:
        json.dump({"seen": seen}, f, separators=(",", ":"))
    os.makedirs(os.path.join(out_dir, "d"), exist_ok=True)
    shards = {f"{n:02d}": {} for n in range(100)}
    for i, d in descs.items():
        shards[shard(i)][i] = d
    for n, part in shards.items():
        with open(os.path.join(out_dir, "d", f"{n}.json"), "w") as f:
            json.dump(part, f, separators=(",", ":"))
    print(f"{added} new posting(s) added; {len(rows)} on the site.")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])
