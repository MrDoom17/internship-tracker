#!/usr/bin/env python3
"""Hourly check for the shared Internship Tracker website.

Runs on GitHub Actions. Downloads the list the website is showing now, asks
LinkedIn for new internships with the same searches as the desktop tracker
(tracker.py), and writes the updated list for the website to publish.

  python3 update.py PREVIOUS_URL OUT_DIR

PREVIOUS_URL is where the live site serves its files (".../postings.json" and
".../seen.json" are fetched from it). A 404 means this is the first run, so the
check pages back through the last 90 days. OUT_DIR gets postings.json (what the
page shows) and seen.json (ids the title filter turned down, so later checks
know they've been seen).
"""
import json
import os
import sqlite3
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

# tracker.py keeps its settings and database in a data folder; give it a
# throwaway one, since this script keeps its state in the published files.
os.environ.setdefault("INTERNSHIP_TRACKER_HOME", tempfile.mkdtemp())
import tracker  # noqa: E402

KEEP_DAYS = 92  # LinkedIn's window is 90 days; a little slack so edge postings don't come and go


def get_previous(base):
    """(postings, seen ids, first-run time) the site serves now; empty on the first run.
    Any other failure stops the run so a hiccup can't wipe the list."""
    out = []
    for name in ("postings.json", "seen.json"):
        try:
            with urllib.request.urlopen(f"{base.rstrip('/')}/{name}", timeout=30) as r:
                out.append(json.load(r))
        except urllib.error.HTTPError as e:
            if e.code != 404:
                raise
            out.append({})
    posts, seen = out
    return {p["id"]: p for p in posts.get("postings", [])}, seen.get("seen", {}), posts.get("started_at")


def main(base, out_dir):
    cfg = tracker.load_config()  # the defaults: all business fields, United States, 90 days
    old, seen, started = get_previous(base)
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

    # Drop postings older than the window.
    cutoff = (now - timedelta(days=KEEP_DAYS)).date().isoformat()
    posts = {i: p for i, p in posts.items() if (p.get("posted") or p["found"][:10]) >= cutoff}
    seen = {i: d for i, d in seen.items() if d >= cutoff}
    added = sum(p["found"] == stamp for p in posts.values())

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
    print(f"{added} new posting(s) added; {len(rows)} on the site.")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(sys.argv[1], sys.argv[2])
