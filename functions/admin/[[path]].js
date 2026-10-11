// Developer portal: /admin shows traffic and usage, /admin/stats returns the
// numbers. Password protected (the browser asks once and remembers it).
import PAGE from "./_page.html";

// SHA-256 of the password. Only the hash lives here, since the repo is public.
const PASSWORD_SHA256 = "73310104fbade62d3cb6638bba3aec191cb3cdb4966bfb728ca5252e75a244de";

async function sha256(text) {
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

async function allowed(request) {
  const h = request.headers.get("authorization") || "";
  if (!h.startsWith("Basic ")) return false;
  let pass = "";
  try { pass = atob(h.slice(6)).split(":").slice(1).join(":"); } catch (e) { return false; }
  return (await sha256(pass.trim())) === PASSWORD_SHA256;
}

const NO_STORE = { "cache-control": "no-store", "x-robots-tag": "noindex" };

export async function onRequest({ request, env, params }) {
  if (!(await allowed(request))) {
    return new Response("Password needed. Use any username.", {
      status: 401, headers: { ...NO_STORE, "www-authenticate": 'Basic realm="Internship Tracker portal", charset="UTF-8"' },
    });
  }
  const path = (params.path || []).join("/");
  if (path === "stats") return stats(request, env);
  if (path) return Response.redirect(new URL("/admin", request.url).toString(), 302);
  return new Response(PAGE, { headers: { ...NO_STORE, "content-type": "text/html; charset=utf-8" } });
}

const json = (o) => new Response(JSON.stringify(o), { headers: { ...NO_STORE, "content-type": "application/json" } });

async function stats(request, env) {
  if (!env.DB) return json({ ready: false });
  const q = new URL(request.url).searchParams;
  const since = Number(q.get("since")) || Date.now() - 7 * 864e5;
  const tz = (Number(q.get("tz")) || 0) * 60;  // browser's offset from UTC, in seconds
  const DB = env.DB, now = Date.now();
  const top = (col, extra = "") => DB.prepare(
    `SELECT ${col} AS k, count(DISTINCT vid) AS n FROM events WHERE ts >= ? ${extra} GROUP BY k ORDER BY n DESC LIMIT 12`).bind(since);
  const words = (type) => DB.prepare(
    `SELECT detail AS k, count(*) AS n, count(DISTINCT vid) AS p FROM events WHERE ts >= ? AND type = ? AND detail != '' GROUP BY k ORDER BY n DESC LIMIT 15`).bind(since, type);
  const r = await DB.batch([
    DB.prepare(`SELECT count(DISTINCT vid) AS visitors, count(DISTINCT sid) AS visits, coalesce(sum(type = 'view'), 0) AS views,
      count(DISTINCT CASE WHEN ret = 1 THEN vid END) AS back FROM events WHERE ts >= ?`).bind(since),
    DB.prepare(`SELECT avg(t) AS secs FROM (SELECT sum(n) AS t FROM events WHERE ts >= ? AND type = 'leave' GROUP BY sid)`).bind(since),
    DB.prepare(`SELECT date(ts / 1000 - ?, 'unixepoch') AS d, count(DISTINCT vid) AS visitors, count(DISTINCT sid) AS visits,
      sum(type = 'view') AS views FROM events WHERE ts >= ? GROUP BY d ORDER BY d`).bind(tz, since),
    DB.prepare(`SELECT CAST(strftime('%H', ts / 1000 - ?, 'unixepoch') AS INTEGER) AS h, count(DISTINCT sid) AS n
      FROM events WHERE ts >= ? GROUP BY h`).bind(tz, since),
    top("ref", "AND type = 'view'"),
    top("country", "AND type = 'view'"),
    top("device", "AND type = 'view'"),
    top("browser", "AND type = 'view'"),
    DB.prepare(`SELECT type AS k, count(*) AS n, count(DISTINCT vid) AS p, sum(n) AS items FROM events WHERE ts >= ? GROUP BY type`).bind(since),
    words("search"),
    words("applied"),
    words("field"),
    words("place"),
    DB.prepare(`SELECT count(DISTINCT vid) AS n FROM events WHERE ts >= ?`).bind(now - 5 * 60000),
    DB.prepare(`SELECT ts, type, country, device, detail, n, ret FROM events WHERE type != 'leave' ORDER BY ts DESC LIMIT 30`),
    DB.prepare(`SELECT count(DISTINCT vid) AS visitors, count(DISTINCT sid) AS visits, min(ts) AS first FROM events`),
  ]);
  const rows = (i) => r[i].results || [];
  return json({
    ready: true, now,
    totals: { ...rows(0)[0], secs: rows(1)[0]?.secs || 0, live: rows(13)[0]?.n || 0 },
    daily: rows(2), hours: rows(3),
    refs: rows(4), countries: rows(5), devices: rows(6), browsers: rows(7),
    actions: rows(8), searches: rows(9), applied: rows(10), fields: rows(11), places: rows(12),
    recent: rows(14), allTime: rows(15)[0] || {},
  });
}
