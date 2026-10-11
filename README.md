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
- The first run fetches the last 90 days of postings; later runs only add new ones.
  Postings older than 90 days drop off.

## Setup (once)

1. Settings → Pages → Build and deployment → Source: **GitHub Actions**.
2. Actions tab → **Check LinkedIn** → **Run workflow**. The first run takes several
   minutes. The site is then at `https://<your-username>.github.io/<repo-name>/`.

3. Optional, for an address without your GitHub username: make a free
   Cloudflare account, create an API token with the "Edit Cloudflare Workers"
   template, and add it plus your Cloudflare account ID as the repository secrets
   `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID`. The next run publishes to
   `https://internship-tracker.pages.dev` (Cloudflare adds a few letters if the
   name is taken; change `SITE_NAME` in the workflow to pick another).

To change the searches, edit `DEFAULT_CONFIG` in `tracker.py`.

## Limits

- No Handshake (it needs each person's own school login) and no phone alerts.
- If LinkedIn ever blocks GitHub's servers, the page keeps the last list and says
  the last check didn't return anything.
