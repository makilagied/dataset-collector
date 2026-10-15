# Swahili Abusive-Comment Dataset Collector

Collects comments from **consenting** Instagram professional accounts through the official
Instagram API (Business Login for Instagram). It detects abusive Swahili words written in
obfuscated forms (`M.J.1.N.G.@`, `mjiiinga`, `мjіngа`, `mjnga`). It then exports a
stratified, anonymized sample for human labelling.

- Two sources: the official API for consenting accounts, and an optional browser scraper
  for public Instagram and Facebook comments (see [Scraping](#scraping-public-comments)).
- Commenter usernames and IDs are never stored. They are HMAC-hashed at ingestion.
- Access tokens are encrypted at rest. Secrets come only from environment variables.

See [PLAN.md](PLAN.md) for the original specification.

---

## Quick start: a mocked end-to-end run in about 10 minutes

You need **Docker** with Docker Compose. You do **not** need Python, a Meta app or an
Instagram account. The demo backfills a synthetic account through a fake, in-process
Instagram API, using the same code path as the real backfill.

**1. Get the code and create your `.env` file**

```bash
git clone <repo-url> dataset-collector
cd dataset-collector
cp .env.example .env          # Windows PowerShell: Copy-Item .env.example .env
```

**2. Build the image, then generate the secret keys.** The first build takes a few
minutes because it downloads the language-ID model.

```bash
docker compose build
docker compose run --rm --no-deps app python -c "from cryptography.fernet import Fernet; import secrets; print('TOKEN_ENC_KEY=' + Fernet.generate_key().decode()); print('AUTHOR_HASH_KEY=' + secrets.token_hex(32)); print('IG_WEBHOOK_VERIFY_TOKEN=' + secrets.token_urlsafe(24))"
```

Paste the three printed lines into `.env`, replacing the empty `TOKEN_ENC_KEY=`,
`AUTHOR_HASH_KEY=` and `IG_WEBHOOK_VERIFY_TOKEN=` lines. Leave the `IG_APP_*` values empty
for the demo.

> Keep `TOKEN_ENC_KEY` and `AUTHOR_HASH_KEY` safe and **never change them** once real data
> is collected. A new `TOKEN_ENC_KEY` makes stored tokens unreadable. A new
> `AUTHOR_HASH_KEY` gives the same commenter a different hash.

**3. Start everything**

```bash
docker compose up -d
```

This starts Postgres, applies the database migrations and starts the web app on
<http://localhost:8000>. Open <http://localhost:8000/docs> to check that it's running.

**4. Run the pipeline on demo data**

```bash
docker compose exec app collector demo load        # backfill 3 posts, 45 comments
docker compose exec app collector process          # normalize + match + language ID
docker compose exec app collector stats            # counts, hit rate, languages
docker compose exec app collector sample --batch demo-1 --size 20 --seed 1
docker compose exec app collector export --batch demo-1 --format csv
```

The export appears on your machine at `exports/demo-1.csv`. Open it and check that
`@mentions` read `@USER` and that no `demo_user_*` names appear.

**5. Clean up**

```bash
docker compose exec app collector demo remove      # deletes the demo account and its data
docker compose down                                # stop; add -v to also delete the database
```

**Troubleshooting**

- **Port already in use:** something else is using 5432, 8000 or 4040. Stop it, or change
  the left-hand port in `docker-compose.yml`.
- **`ngrok` container exited:** expected until you set up ngrok (see "Development without a
  server" below). The demo doesn't need it.
- **`TOKEN_ENC_KEY` errors:** the key must be the full value printed in step 2.
- **Slow first build:** the image downloads a ~170 MB language model once. Later builds are
  cached.

---

## Admin UI

Open <http://localhost:8000/admin>. You'll see a login page: the username is `admin` (or
`ADMIN_USER`), and the password is `ADMIN_PASSWORD` from `.env`.

- **Disabled by default:** the UI is switched off while that password is empty.
- **Sessions:** a session lasts 12 hours. **Log out** is at the top right. Changing the
  password logs everyone out.
- **Scripts:** they can use HTTP Basic auth instead, e.g.
  `curl -u admin:<password> http://localhost:8000/admin/jobs`.

| Page | What you can do |
|---|---|
| **Overview** | Totals, hit rate, language split. Buttons to process new comments, reprocess everything, refresh tokens. Recent jobs |
| **Accounts** | For each account: start a backfill (optionally since a date), set its category, re-subscribe webhooks, pause or resume. Also makes `/connect` links that carry a consent reference |
| **Comments** | Browse comments with their normalized text, language, matched roots and score. Filter by account, by hits / excluded / unprocessed, or by text |
| **Batches** | Draw a labelling batch, and download any batch as CSV or JSONL |
| **Jobs** | Status and results of background jobs. The page refreshes itself while a job is running |

- **Background jobs:** backfills and processing run one at a time in the background. Their
  history is kept in the `jobs` table. Jobs that were running when the app stopped show up
  as `interrupted`.
- **Security:**
  - The UI is **only reachable locally**. Requests through the ngrok tunnel are refused
    unless you set `ADMIN_ALLOW_REMOTE=true`. Only do that with a strong password.
  - Every form carries a CSRF token.
  - Comment text is always HTML-escaped.
  - Pages can't be framed by other sites, and a strict Content-Security-Policy blocks all
    scripts.
- **Pausing** an account stops backfills, and its webhooks are ignored. Nothing is deleted.

## Commands

The same actions are available on the command line. All commands run inside the app
container:

```bash
docker compose exec app collector <command>
```

With a local install, run `collector <command>` instead.

| Command | What it does |
|---|---|
| `accounts list` | Connected accounts, category and token expiry |
| `accounts set-category ID CATEGORY` | Tag an account (e.g. `udaku`, `football`, `media`, `music`). Exported with its comments |
| `tokens refresh [--days 10]` | Refresh long-lived tokens expiring soon. Run daily, e.g. from cron |
| `webhooks subscribe --account ID` | Re-enable webhook delivery for an account. Done automatically on connect |
| `backfill --account ID [--since YYYY-MM-DD]` | Fetch posts → comments → replies. Safe to rerun, and resumes if interrupted |
| `process [--batch-size 500] [--all]` | Normalize, match and language-ID new or outdated comments |
| `sample --batch NAME --size N [--mix 50/30/20] [--seed N] [--lang sw]` | Draw a labelling batch: lexicon hits / model-flagged / random |
| `export --batch NAME [--format csv\|jsonl] [--out PATH]` | Write the batch with mentions, URLs and emails masked |
| `stats` | Per-account counts, abuse-hit rate, language split, exclusions, batches |
| `demo load` / `demo remove` | Synthetic demo account (no Meta app needed) |
| `scrape login instagram\|facebook` | Log in by hand in a browser window; the session is kept |
| `scrape instagram TARGET...` | Scrape comments from `@user`, `#tag` or post URLs |
| `scrape facebook TARGET...` | Scrape comments from page / group URLs or post URLs |

Web endpoints:

| Endpoint | Purpose |
|---|---|
| `GET /connect?consent_ref=...` | Send an account owner here to connect their account. `consent_ref` links to their signed consent form |
| `GET /oauth/callback` | OAuth redirect target. Stores the encrypted long-lived token and subscribes webhooks |
| `GET /webhook` | Meta's webhook verification handshake |
| `POST /webhook` | Receives `comments` / `live_comments`. Checks the signature, acknowledges at once and ingests in the background |

## How a comment flows through the system

```
Instagram API ──backfill──┐
                          ├─► comments (author HMAC-hashed, username stripped from raw_json)
Meta webhooks ────────────┘          │
                                     ▼  collector process
                          comment_features: text_norm, lang, matched_roots,
                          match_methods, match_score, exclude_reason
                                     │
                                     ▼  collector sample
                          samples (one batch per comment, ever)
                                     │
                                     ▼  collector export
                          exports/<batch>.csv|jsonl  (@USER, <URL>, <EMAIL>)
```

- **Normalizer** ([`nlp/normalize.py`](src/collector/nlp/normalize.py)) runs these steps in
  order: Unicode fold → homoglyphs → spaced-out letters → leetspeak → repeated letters →
  texting shorthand ([`data/sw_shorthand.yaml`](src/collector/data/sw_shorthand.yaml)).
- **Matcher** ([`nlp/matcher.py`](src/collector/nlp/matcher.py)) checks the roots in
  [`data/sw_roots.yaml`](src/collector/data/sw_roots.yaml) three ways: by regex, by
  consonant skeleton and by fuzzy matching. It has an allowlist of innocent lookalikes.
  Animal words count as hits only next to a second-person cue (`wewe ni mbwa`).
- **Excluded from sampling** but kept in the database: empty, emoji-only, link-only,
  mention-only and duplicate (same normalized text) comments.

## Connecting real accounts: Meta app configuration

> Meta changes its dashboard often. The menu names below were checked in October 2026,
> but verify them against the [Instagram Platform docs](https://developers.facebook.com/docs/instagram-platform).

1. **Create the app.** At <https://developers.facebook.com/apps>, create an app of type
   **Business** and add the Instagram use case ("Manage messaging and content on
   Instagram").
2. **Copy the Instagram credentials.** Go to **Instagram → API setup with Instagram
   business login**. Copy the **Instagram app ID** and **Instagram app secret** into `.env`
   as `IG_APP_ID` and `IG_APP_SECRET`. These are *not* the Facebook app ID and secret shown
   under App settings.
3. **Set the redirect URL.** In the business login settings on that page, add your redirect
   URL, e.g. `https://collector.example.org/oauth/callback`. Set the exact same value as
   `IG_REDIRECT_URI`. It must be **public HTTPS**.
4. **Configure webhooks.** On the same page:
   - **Callback URL:** `https://collector.example.org/webhook`
   - **Verify token:** the value of `IG_WEBHOOK_VERIFY_TOKEN`
   - **Fields to subscribe:** `comments` and `live_comments`.
5. **Add testers.** Under App roles, add the Instagram professional accounts you will
   collect from as testers. Each account owner accepts the invite in Instagram (Settings →
   website / app permissions → tester invites). With **Standard Access**, only accounts
   added this way can connect. Anyone else needs Advanced Access, which requires App Review
   (see the checklist below).
6. **Restart the app:** `docker compose up -d`.
7. **Connect an account.** Have the account owner sign the consent form, then open
   `https://<host>/connect?consent_ref=<their-form-id>`. After they approve:

   ```bash
   docker compose exec app collector accounts list
   docker compose exec app collector accounts set-category <id> football
   docker compose exec app collector backfill --account <id>
   docker compose exec app collector process
   ```

**Development without a server: the ngrok tunnel.** `docker compose up` also starts an
`ngrok` container that gives the app a public HTTPS address.

1. Create a free account at <https://dashboard.ngrok.com>. Copy **Your Authtoken**, and
   claim your free static domain under **Domains** (e.g. `something.ngrok-free.app`).
2. In `.env`:

   ```
   NGROK_AUTHTOKEN=<your authtoken>
   NGROK_DOMAIN=something.ngrok-free.app
   IG_REDIRECT_URI=https://something.ngrok-free.app/oauth/callback
   ```

3. Run `docker compose up -d`. Then open `https://something.ngrok-free.app/docs` to check
   the tunnel, or <http://localhost:4040> to watch every request in the ngrok inspector.
4. Use `https://something.ngrok-free.app/oauth/callback` (step 3) and
   `https://something.ngrok-free.app/webhook` (step 4) in the Meta dashboard.

Without these values only the `ngrok` container exits (`ERR_NGROK_4018`); everything else
runs. On the free plan, ngrok shows browsers a one-time warning page: click **Visit Site**.
Meta's webhook calls are not affected.

**Webhooks from a Facebook Login app** (optional): set `FB_APP_SECRET` to that app's
secret. Its webhooks are signed with it, and both payload shapes are accepted.

## Scraping public comments

`collector scrape` reads public comments from Instagram and Facebook through a real,
logged-in Chromium window. No Meta app or account-owner consent is involved. Scraped
sources become accounts named `scrape:ig:<name>` or `scrape:fb:<name>`, and their comments
have `source = 'scrape'`. From there they go through `process` → `sample` → `export`
like any other comments. Commenter IDs are HMAC-hashed and never stored.

**Before you use it, know that:**
- Scraping, especially while logged in, breaks Instagram's and Facebook's Terms of Use.
  The account you log in with can be checkpointed or banned. Use a separate account,
  not your personal one.
- The scraper depends on undocumented site internals and button labels. Expect to fix
  it when the sites change. All site-specific code is in
  [src/collector/scrape/instagram.py](src/collector/scrape/instagram.py) and
  [src/collector/scrape/facebook.py](src/collector/scrape/facebook.py).
- Scraped data has no consent behind it. Check with your ethics board how it may be used
  and shared before you publish anything built from it.

**Setup.** This runs on the host, not in Docker, because it needs a visible browser. It
uses the `DATABASE_URL` in `.env` (`localhost:5432`, which the compose `db` exposes).

```bash
pip install -e ".[scrape]"
python -m playwright install chromium
collector scrape login instagram     # log in in the window; it closes by itself
collector scrape login facebook      # set the account's Facebook language to English
```

The session is saved in `.scrape-profile/` (git-ignored). It holds live logins, so don't
share it.

**From the dashboard.** Run `collector ui` on the host and open
<http://127.0.0.1:8001/admin/scrape>. The Scrape page logs in, starts Instagram or Facebook
scrapes (same options as the commands below, plus "process new comments when finished"),
shows progress, and lists the scraped sources. Scrapes run as background jobs one at a
time, so a long scrape holds back other jobs until it ends. The Docker dashboard on port
8000 shows the same page but can't open a browser, so it only tells you to use `collector ui`.

**Examples.**

```bash
# 30 newest posts of two profiles, filed under category "udaku"
collector scrape instagram @someprofile @otherprofile --max-posts 30 --category udaku
# posts under a hashtag
collector scrape instagram "#udaku"
# one post
collector scrape instagram https://www.instagram.com/p/SHORTCODE/
# a Facebook page's recent posts, or one post
collector scrape facebook https://www.facebook.com/somepage --max-posts 10
collector scrape facebook https://www.facebook.com/somepage/posts/pfbid0...
```

**Only hits are stored**, from every source: scraping, API backfill and webhooks
(`STORE_ONLY_HITS=true`, the default). A hit is a comment with a word from the Swahili
([sw_roots.yaml](src/collector/data/sw_roots.yaml)) or English
([en_roots.yaml](src/collector/data/en_roots.yaml)) lexicon, in any disguised spelling,
scoring at least 0.5. Words that are only insults when aimed at someone ("dog", "ugly",
"mbwa") need a "you"/"wewe" nearby. Other comments are read and dropped, and runs report
how many. To catch a new word, add it to the lexicon. Set `STORE_ONLY_HITS=false` in `.env`
to store every comment again. Without it, a labelling batch's `random` and `model` strata
can only draw from hits. English words are matched before the Swahili shorthand step, which
would otherwise rewrite them ("fuck" -> "fusik"). Typo matching only accepts keyboard-style
typos ("malayq"), because a free one-letter change is usually a different real word
("mabaya" is not "malaya"). Mistakes found in real comments are kept as regression tests in
[reviewed_comments.yaml](tests/fixtures/reviewed_comments.yaml). The
scraper waits about `--delay` seconds (default 4, randomized) between requests. A rerun
skips posts whose comment count hasn't changed, unless you pass `--refresh`. If the site
asks for a login or a checkpoint, or starts rate limiting, the run stops with exit code 2.
Everything scraped so far is kept. Clear the challenge in the window (or with
`scrape login`) and wait a while before you run it again.

Limits: Instagram hashtag targets return only the posts the tag page shows (top and
recent, a few dozen). Facebook posts carry no post date, so `--since` applies to
Instagram only.

## Consent

Accounts collected through the API are only those whose owners have completed the OAuth
flow **and** signed a consent form (scraped sources have no consent; see above). A draft form is in [docs/consent-template.md](docs/consent-template.md). Have it
reviewed by your ethics board and legal counsel before use. Pass the signed form's
reference as `consent_ref` on `/connect`, so each account row records its consent.

## App Review checklist (Advanced Access)

Needed only when you connect accounts that aren't added as testers in the dashboard.

- [ ] **Permissions requested:** `instagram_business_basic` and
      `instagram_business_manage_comments` only, and nothing else.
- [ ] **Business verification** completed for the Meta business that owns the app.
- [ ] **Privacy policy URL** (public). It should cover what is collected, the hashing, the
      encryption, retention, the research purpose and contact details.
- [ ] **Data deletion:** a deletion instructions URL, or a deletion callback.
      **Open item: not implemented yet.** The app also needs to handle deauthorization by
      deleting the account's token and marking it inactive.
- [ ] **App icon, category and contact email** set in App settings.
- [ ] **Use-case description for each permission.** Explain why comments are read, and
      what the account owner gets in return (e.g. reports on abuse on their account).
      Meta expects `instagram_business_manage_comments` to serve the account owner.
- [ ] **Screencast:**
  1. The `/connect` flow.
  2. The consent screen.
  3. The success page.
  4. A backfill.
  5. The resulting (anonymized) data use.
- [ ] **Test instructions and credentials** for the reviewer (a test professional account).
- [ ] **Public HTTPS hosting** for `/oauth/callback` and `/webhook`, reachable by Meta.
- [ ] **Re-check** endpoint and permission names against current docs (see PLAN.md §2).

## Data protection notes

| Data | How it is stored |
|---|---|
| Commenter username / ID | Never stored. `author_hash` = HMAC-SHA256(`AUTHOR_HASH_KEY`, Instagram-scoped ID). `from`, `username` and `user` are stripped from `raw_json` |
| Account access tokens | Fernet-encrypted (`TOKEN_ENC_KEY`). Never logged, and removed from saved paging cursors |
| Comment text | Stored as written, since `@mentions` inside text are needed for context. Masked to `@USER` / `<URL>` / `<EMAIL>` on export |
| Exports | No commenter identifiers, hashed or not. CSV cells that a spreadsheet would read as formulas are prefixed with `'` |

A test scans every column of every table for a known commenter username, and others check
the exported files. See [tests/test_backfill.py](tests/test_backfill.py) and
[tests/test_export.py](tests/test_export.py).

## Local development

Requires Python 3.12 and Docker (for Postgres).

```bash
python -m venv .venv
source .venv/bin/activate              # Windows: .venv\Scripts\activate
pip install -e ".[dev,nlp]"            # or: uv sync --extra dev --extra nlp
docker compose up -d db
alembic upgrade head                   # uses DATABASE_URL from .env (localhost:5432)

pytest                                 # tests use a separate collector_test database
ruff check . && ruff format --check .
mypy
```

- **Tests make no network calls.** Instagram is mocked with respx and a fake Graph API
  ([tests/fake_graph.py](tests/fake_graph.py)).
- **Matcher metrics:** precision and recall on the matcher fixture are printed in a
  `metrics` section at the end of each `pytest` run.
- **Schema changes:** edit [models.py](src/collector/models.py), then run
  `alembic revision --autogenerate -m "..."` and review the file it generates.
- **After changing behaviour:** if you change the normalizer, lexicon or matcher, bump
  `PIPELINE_VERSION` in [pipeline/process.py](src/collector/pipeline/process.py). The next
  `collector process` then reprocesses every comment.

## Project layout

```
src/collector/
  config.py, db.py, models.py, security.py
  accounts.py          onboarding + token refresh
  api.py               FastAPI: /connect, /oauth/callback, /webhook
  cli.py               Typer commands
  demo.py              synthetic demo account
  ig/                  oauth.py, client.py, backfill.py, webhook.py
  nlp/                 normalize.py, lexicon.py, matcher.py, langid.py
  pipeline/            process.py, sample.py, scoring.py, export.py, stats.py
  data/                sw_roots.yaml, sw_shorthand.yaml   (edit these, no code needed)
alembic/versions/      database migrations
tests/                 pytest suite (+ fixtures/matcher_fixture.yaml)
docs/                  consent template
```

## Where this differs from PLAN.md

- **Lexicon and shorthand files** live in `src/collector/data/` rather than `data/`, so
  they ship inside the package and the Docker image.
- **Normalizer step order:** separators run before leetspeak. In `m j 1 n g a`, the `1`
  only has letter neighbours once the letters are joined.
- **Extra columns:** `comment_features.exclude_reason` records why a comment is skipped by
  sampling. It adds a `mention_only` reason. `accounts.category` feeds the export.
- **Fuzzy matching** applies to the root only, with the prefix matching exactly. Otherwise
  `mahaba` (love) would match `makahaba`.
- **Replies** are fetched by field expansion on the comments call. The `/replies` endpoint
  is used only for overflow pages.
- **Language ID** uses lingua (Swahili, English and French), with a stopword fallback.

## Open decisions

1. **Seed lexicon:** contents and native-speaker reviewers. It is currently a TODO-marked
   seed list in [sw_roots.yaml](src/collector/data/sw_roots.yaml).
2. **Account categories** to recruit first (udaku, football, media, musicians).
3. **Kenyan Sheng:** whether it is in scope. Sheng entries are marked in the lexicon notes.
4. **Hosting** for the public HTTPS endpoint.
5. **Data deletion:** implement the deletion and deauthorize callbacks before App Review.
6. **Duplicate rule:** whether near-duplicates (same *normalized* text) should count as
   duplicates, as they do now.
