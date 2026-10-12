# Internship Tracker (shared website)

A website that lists new business internships on LinkedIn (finance, data analytics,
accounting, marketing, sales, consulting, HR, supply chain, economics and general
business), checked every hour. Anyone with the link can use it, no sign-up.

Each person's **Applied**, **Hidden** and seen marks, and their filters, are saved in
their own browser, so friends don't see or change each other's lists.

## How it works

- `.github/workflows/update.yml` runs every hour on GitHub Actions (free). It runs
  `update.py`, which uses the same LinkedIn searches and title filters as the
  desktop tracker (`tracker.py`), and publishes the result to GitHub Pages.
- `site/index.html` is the page. It loads `postings.json` and keeps your marks in
  the browser (localStorage).
- The first run fetched the last 90 days of postings; later runs only add new ones.
  Postings are never removed, so the list keeps growing.

## Setup (once)

1. Settings → Pages → Build and deployment → Source: **GitHub Actions**.
2. Actions tab → **Check LinkedIn** → **Run workflow**. The first run takes several
   minutes. The site is then at `https://<your-username>.github.io/<repo-name>/`.

3. Optional, for an address without your GitHub username: make a free
   Cloudflare account, create a custom API token with the permission
   Account / Cloudflare Pages / Edit, and add it plus your Cloudflare account ID as the repository secrets
   `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID`. The next run publishes to
   `https://internship-tracker.pages.dev` (Cloudflare adds a few letters if the
   name is taken; change `SITE_NAME` in the workflow to pick another).

## Developer portal (traffic and usage)

`https://<site>.pages.dev/admin` shows visitors, visits, returning visitors, time on
the page, where people came from, countries, devices, and what they do (searches,
postings opened, Applied/Hide clicks, filters). It asks for a password (any username).

- `functions/api/e.js` receives visit and click events from the page (a random id per
  browser, plus IP address, city and network; no names or emails). The site footer says
  visits and IP addresses are logged. `functions/admin/` is the portal.
- Events are stored in a free Cloudflare D1 database (`tracker-analytics`, created by
  the workflow). The Cloudflare token needs **Account / D1 / Edit** in addition to
  Cloudflare Pages / Edit. Events older than 13 months are deleted, and IP addresses
  are blanked after 90 days. Events must come from the site's own pages, and each IP
  address can log at most 120 events a minute.
- Opening the portal stops your own browser from being counted (checkbox at the bottom).
- To change the password, put the SHA-256 of the new one in `PASSWORD_SHA256` in
  `functions/admin/[[path]].js`.

To change the searches, edit `DEFAULT_CONFIG` in `tracker.py`.

## Limits

- No Handshake (it needs each person's own school login) and no phone alerts.
- If LinkedIn ever blocks GitHub's servers, the page keeps the last list and says
  the last check didn't return anything.
