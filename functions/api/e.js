// Receives anonymous visit and usage events from the site (see track() in
// site/index.html) and stores them in the D1 database bound as DB.
const TYPES = new Set(["view", "search", "applied", "hidden", "restore", "open", "field", "place", "leave"]);
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
function refHost(ref, self) {
  try {
    const h = new URL(ref).hostname.replace(/^www\./, "");
    return h && h !== self ? h.slice(0, 80) : "";
  } catch (e) { return ""; }
}

export async function onRequestPost({ request, env }) {
  if (!env.DB) return ok();  // database not set up yet: drop quietly
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
  const cty = (request.cf && request.cf.country) || "";
  const dev = device(ua), br = browser(ua), now = Date.now();
  const rows = (Array.isArray(body.e) ? body.e : []).slice(0, 20).filter((e) => e && TYPES.has(e.t));
  if (!rows.length) return ok();
  const stmt = env.DB.prepare("INSERT INTO events (ts, type, vid, sid, ret, ref, country, device, browser, detail, n) VALUES (?,?,?,?,?,?,?,?,?,?,?)");
  const batch = rows.map((e) => stmt.bind(now, e.t, vid, sid, body.r ? 1 : 0,
    e.t === "view" ? refHost(String(e.ref || ""), self) : "", cty, dev, br,
    (e.t === "search" ? String(e.d || "").toLowerCase() : String(e.d || "")).trim().slice(0, 60), Math.max(0, Math.min(86400, parseInt(e.n, 10) || 0))));
  // Now and then, drop events older than 13 months so the free database never fills up.
  if (Math.random() < 0.01) batch.push(env.DB.prepare("DELETE FROM events WHERE ts < ?").bind(now - 400 * 864e5));
  try { await env.DB.batch(batch); } catch (e) {}
  return ok();
}
