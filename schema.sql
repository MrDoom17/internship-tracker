-- Visit and usage events for the developer portal (/admin). Anonymous:
-- a random visitor id kept in the browser, never a name, email or IP.
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY,
  ts INTEGER NOT NULL,      -- milliseconds since 1970 (UTC)
  type TEXT NOT NULL,       -- view, search, applied, hidden, restore, open, field, place, leave
  vid TEXT,                 -- random id per browser
  sid TEXT,                 -- random id per visit (tab session)
  ret INTEGER,              -- 1 when this browser had visited before
  ref TEXT,                 -- referring site (views only)
  country TEXT,
  device TEXT,              -- Phone, Tablet or Computer
  browser TEXT,
  detail TEXT,              -- search words, field name, etc.
  n INTEGER,                -- a number: seconds on page, postings in a bulk action
  city TEXT, region TEXT,   -- rough location from Cloudflare (no IP addresses kept)
  os TEXT,                  -- iOS, Android, Windows, macOS...
  net TEXT,                 -- network name, e.g. a school or phone carrier
  title TEXT,               -- posting title and company for posting clicks
  screen TEXT,              -- screen size (views only)
  lang TEXT                 -- browser language (views only)
);
-- Columns added after the first version; the workflow adds any that are missing.
CREATE INDEX IF NOT EXISTS events_ts ON events (ts);
CREATE INDEX IF NOT EXISTS events_type_ts ON events (type, ts);
CREATE INDEX IF NOT EXISTS events_vid_ts ON events (vid, ts);
