#  Website-targeted Scraper

A targeted, authenticated scraper for LinkedIn company pages. It uses a
real Chromium browser, passively intercepts the company's Voyager
GraphQL responses, and patches any gaps from the rendered DOM.

> **Scope.** This is a **scraper**, not a crawler — you give it a
> company URL, it returns structured data for that company's tabs. It
> does not discover new URLs.

---

## Features

- Posts, comments, reactions, reactors, jobs, products, "About" overview
- Persistent login (one-time seed via `login.py`, then auto-recovery)
- Modern + legacy DOM markup support
- API-first extraction with DOM fallback per field (resilient when
  Voyager calls don't fire)
- Per-tab JSON output: `scraped_data/scraped_<slug>_<tab>.json`

---

## Requirements

- Python 3.10+
- Windows / macOS / Linux
- A real LinkedIn account

Install:

```powershell
pip install -r requirements.txt
playwright install chromium firefox
```

Create a `.env` in the repo root:

```
USER_NAME1=you@example.com
PASSWORD1=********

# Aliases also accepted
# LINKEDIN_EMAIL=...
# LINKEDIN_PASSWORD=...
```

---

## First-time setup

Seed a persistent logged-in browser profile. This runs once and writes
cookies into `user-data-dir/`:

```powershell
python login.py
```

Complete any MFA / captcha in the window that opens. When you reach the
LinkedIn feed, close the window. You're set.

---

## Usage

Single command:

```powershell
python run_scraper.py --url https://www.linkedin.com/company/<slug>/
```

### Common variants

```powershell
# Just the posts tab, 5 posts, with reactor lists
python run_scraper.py --url https://www.linkedin.com/company/zenarate/ --tabs posts --max-posts 5 --reactors

# All default tabs, 20 posts each
python run_scraper.py --url https://www.linkedin.com/company/<slug>/ --max-posts 20

# Custom output path
python run_scraper.py --url ... --output my_company.json
```

### CLI flags

| Flag | Default | Description |
|---|---|---|
| `--url` | required | Target company URL |
| `--tabs` | all | Comma list from `home,about,posts,jobs,products` |
| `--max-posts` | 10 | Cap on posts per feed-style tab |
| `--all-posts` | off | Scroll the feed until plateau (safety cap 5000). Overrides `--max-posts`. |
| `--max-comment-pages` | 5 | "Load more comments" click cap per post |
| `--all-comments` | off | Load EVERY comment per post (plateau-stop, safety cap 200 clicks). Overrides `--max-comment-pages`. |
| `--reactors` | off | Open the reactions modal on each post and harvest rows |
| `--max-reactor-scrolls` | 25 | Scroll cap inside the reactors modal |
| `--all-reactors` | off | Scrape EVERY reactor per post (plateau-stop, safety cap 500 scrolls). Implies `--reactors`. |
| `--headless` | off | Run Chromium headless (see [docs/CONSIDERATIONS.md](docs/CONSIDERATIONS.md)) |
| `--output` | `scraped_data/` | Output directory for per-tab JSON files |

---

## Output

One file per tab. Example shape (truncated):

```json
{
  "source_url": "https://www.linkedin.com/company/zenarate/posts/",
  "tab": "posts",
  "slug": "zenarate",
  "company": { "name": "Zenarate", "tagline": "..." },
  "posts": [
    {
      "urn": "urn:li:activity:...",
      "author_name": "...",
      "author_profile": "...",
      "text": "...",
      "reactions_count": 31,
      "comments_count": 3,
      "comments": [ { "comment_urn": "...", "author_name": "...", "text": "..." } ],
      "reactors": [ { "name": "...", "profile_url": "...", "headline": "..." } ]
    }
  ],
  "stats": {
    "posts": 9,
    "comments": 12,
    "unresolved_authors": 0,
    "api_buckets": { "company_overview": 1, "updates": 0, "comments": 5, "...": 0 },
    "posts_with_reactors": 5
  }
}
```

`stats.api_buckets` shows how many Voyager payloads were captured per
category — useful for diagnosing why a field was empty (e.g. `updates:
0` means we relied entirely on DOM for post bodies).

---

## Project layout

```
scraping/
├── run_scraper.py        # CLI entry point
├── login.py              # one-time login seeder (Camoufox)
├── scraper/              # package — see docs/ARCHITECTURE.md
├── user-data-dir/        # Camoufox/Firefox profile (login.py)
├── user-data-dir-chrome/ # Chromium profile (scraper)
├── api_dumps/            # raw Voyager payloads (debug aid)
└── docs/
    ├── ARCHITECTURE.md   # modules, data flow, auth flow
    └── CONSIDERATIONS.md # engine/headless/Camoufox trade-offs, limits
```

---

## Docs

- **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** — module
  responsibilities, per-tab data flow, auth recovery diagram,
  field-by-field merge strategy.
- **[docs/CONSIDERATIONS.md](docs/CONSIDERATIONS.md)** — engine choice
  (Playwright Chromium vs Camoufox vs `requests`), headed vs headless,
  click-navigation, persistent profiles, detection signals, known
  limitations, and where Camoufox specifically helps.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Pauses with `Press ENTER once you've signed in` | Cookie expired and MFA required | Sign in manually in the window, press ENTER. Or re-run `login.py`. |
| `updates: 0` in stats but posts populated | Voyager feed call didn't fire during scroll | Normal — DOM fallback handled it. No action needed. |
| `comments: []` on a post with `comments_count > 0` | Post scrolled out of view before toggle click | Re-run with smaller `--max-posts`, or see known limitations in CONSIDERATIONS.md. |
| `[reactors] modal did not open` | Reaction button selector mismatch on a new post type | File an issue with the post URL; selector in `scraper/config.py:POST_REACTIONS_BUTTON` |
| Empty `author_name` on company posts | API path empty, post-level DOM author tier missing | Cosmetic; comment authors are unaffected. |

---

## Legal

LinkedIn's Terms of Service restrict automated scraping. Use this code
only against your own account, with appropriate authorization, and in
compliance with applicable laws and the terms you've agreed to.
