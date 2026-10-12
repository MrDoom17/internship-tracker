// Receives visit and usage events from the site (see track() in
// site/index.html) and stores them in the D1 database bound as DB.
const TYPES = new Set(["view", "search", "applied", "hidden", "restore", "open", "linkedin", "field", "place", "more", "markall", "leave"]);
const ID = /^[a-z0-9]{8,32}$/;
const ok = () => new Response(null, { status: 204 });

function device(ua) {
  if (/iPad|Tablet/i.test(ua) || (/Android/i.test(ua) && !/Mobile/i.test(ua))) return "Tablet";
  return /Mobi|iPhone|Android/i.test(ua) ? "Phone" : "Computer";
}
function browser(ua) {
  return /Edg\//.test(ua) ? "Edge" : /OPR\/|Opera/.test(ua) ? "Opera" : /Firefox|FxiOS/.test(ua) ? "Firefox"
    : /SamsungBrowser/.test(ua) ? "Samsung" : /Chrome|CriOS/.test(ua) ? "Chrome" : /Safari/.test(ua) ? "Safari" : "Other";
}
function os(ua) {
  return /iPhone|iPad|iPod/.test(ua) ? "iOS" : /Android/.test(ua) ? "Android" : /CrOS/.test(ua) ? "ChromeOS"
    : /Windows/.test(ua) ? "Windows" : /Mac OS X|Macintosh/.test(ua) ? "macOS" : /Linux/.test(ua) ? "Linux" : "Other";
}
const clip = (v, n) => String(v || "").trim().slice(0, n);
const RATE_LIMIT = 120;  // events per IP address per minute; anything over is dropped

// Only accept events sent by the site's own pages (blocks casual scripted junk).
function fromSite(request) {
  const self = new URL(request.url).hostname;
  const src = request.headers.get("origin") || request.headers.get("referer") || "";
  try {
    const h = new URL(src).hostname;
    return h === self || h.endsWith(".pages.dev") || h === "localhost";
  } catch (e) { return false; }
}
function refHost(ref, self) {
  try {
    const h = new URL(ref).hostname.replace(/^www\./, "");
    return h && h !== self ? h.slice(0, 80) : "";
  } catch (e) { return ""; }
}

export async function onRequestPost({ request, env }) {
  if (!env.DB) return ok();  // database not set up yet: drop quietly
  if (!fromSite(request)) return ok();
  const ua = request.headers.get("user-agent") || "";
  if (!ua || /bot|crawl|spider|slurp|preview|headless|lighthouse/i.test(ua)) return ok();
  let body;
  try {
    const text = await request.text();
    if (text.length > 4000) return ok();
    body = JSON.parse(text);
  } catch (e) { return ok(); }
  const vid = String(body.v || ""), sid = String(body.s || "");
  if (!ID.test(vid) || !ID.test(sid)) return ok();
  const self = new URL(request.url).hostname;
  // Rough location and network from Cloudflare, and the visitor's IP address.
  const cf = request.cf || {};
  const ip = clip(request.headers.get("cf-connecting-ip"), 45);
  const cty = cf.country || "", city = clip(cf.city, 60), region = clip(cf.region, 60), net = clip(cf.asOrganization, 80);
  const dev = device(ua), br = browser(ua), sys = os(ua), now = Date.now();
  const rows = (Array.isArray(body.e) ? body.e : []).slice(0, 20).filter((e) => e && TYPES.has(e.t));
  if (!rows.length) return ok();
  if (ip) {
    try {
      const recent = await env.DB.prepare("SELECT count(*) AS n FROM events WHERE ip = ? AND ts > ?").bind(ip, now - 60000).first("n");
      if (recent + rows.length > RATE_LIMIT) return ok();
    } catch (e) {}
  }
  const stmt = env.DB.prepare(`INSERT INTO events (ts, type, vid, sid, ret, ref, country, device, browser, detail, n,
    city, region, os, net, title, screen, lang, ip) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`);
  const batch = rows.map((e) => stmt.bind(now, e.t, vid, sid, body.r ? 1 : 0,
    e.t === "view" ? refHost(String(e.ref || ""), self) : "", cty, dev, br,
    clip(e.t === "search" ? String(e.d || "").toLowerCase() : e.d, 60), Math.max(0, Math.min(86400, parseInt(e.n, 10) || 0)),
    city, region, sys, net, clip(e.x, 160),
    e.t === "view" && /^\d{2,5}x\d{2,5}$/.test(e.sc || "") ? e.sc : "", e.t === "view" ? clip(e.lang, 20) : "", ip));
  // Now and then, drop events older than 13 months so the free database never fills up,
  // and blank out IP addresses older than 90 days.
  if (Math.random() < 0.01) batch.push(env.DB.prepare("DELETE FROM events WHERE ts < ?").bind(now - 400 * 864e5),
    env.DB.prepare("UPDATE events SET ip = NULL WHERE ts < ? AND ip IS NOT NULL").bind(now - 90 * 864e5));
  try { await env.DB.batch(batch); } catch (e) {}
  return ok();
}
