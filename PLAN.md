# PLAN: Swahili Abusive-Comment Dataset Collector (Instagram, Route B)

Implementation plan for Claude Code. Build in Python. Work through the milestones in order; each has acceptance criteria that must pass before moving on.

## 1. Goal

Collect comments from consenting Instagram professional accounts through the official Instagram API (Business Login for Instagram), detect abusive Swahili words written in any obfuscated form, and export a sampled, anonymized dataset ready for human labelling.

## 2. Hard constraints

- Official API for consenting accounts. (Changed 2026-10-14: an optional browser scraper for public Instagram/Facebook comments was added as a second source; see README, "Scraping public comments".)
- Only collect from accounts that completed the OAuth flow.
- Never store a raw commenter username or ID. Hash at ingestion with HMAC-SHA256 and a secret key.
- Access tokens are encrypted at rest (Fernet). Secrets come from environment variables only.
- Before coding each API call, verify endpoint, fields and permission names against the current docs at https://developers.facebook.com/docs/instagram-platform (the notes in section 6 were checked in October 2026 against Graph API v25.0 and may have moved).

## 3. Stack

- Python 3.12, `uv` for dependency management
- FastAPI + Uvicorn (OAuth callback, webhook receiver)
- httpx (API client), tenacity (retries)
- PostgreSQL, SQLAlchemy 2.x, Alembic
- Typer (CLI)
- rapidfuzz (fuzzy matching), fasttext or `lingua` (language ID)
- cryptography (Fernet), pydantic-settings
- pytest, ruff, mypy
- Docker Compose for Postgres + app

## 4. Repository layout

```
ig-abuse-dataset/
  pyproject.toml
  docker-compose.yml
  .env.example
  alembic/
  src/collector/
    config.py            # pydantic-settings
    db.py                # engine, session
    models.py            # SQLAlchemy models
    security.py          # Fernet token crypto, HMAC hashing
    ig/
      oauth.py           # authorize URL, code exchange, long-lived token, refresh
      client.py          # paged GET wrapper, rate-limit handling
      backfill.py        # media -> comments -> replies
      webhook.py         # verification + signature check + ingestion
    nlp/
      normalize.py       # Swahili-aware normalizer
      lexicon.py         # load roots, build patterns
      matcher.py         # regex + skeleton + fuzzy matching
      langid.py
    pipeline/
      process.py         # normalize + match + langid for new comments
      sample.py          # stratified sampling for labelling
      export.py          # CSV / JSONL export
    api.py               # FastAPI app
    cli.py               # Typer commands
  data/lexicon/sw_roots.yaml
  tests/
```

## 5. Database schema

`accounts`
- id (pk), ig_user_id (unique), username, consent_ref, access_token_enc, token_expires_at, connected_at, active

`media`
- id (pk), ig_media_id (unique), account_id (fk), caption, media_type, permalink, posted_at, comments_count, last_synced_at

`comments`
- id (pk), ig_comment_id (unique), media_id (fk), parent_ig_comment_id (nullable), author_hash, text_raw, like_count, hidden, commented_at, source (`backfill` | `webhook`), raw_json (jsonb), ingested_at

`comment_features`
- comment_id (pk, fk), text_norm, lang, lang_conf, matched_roots (text[]), match_methods (text[]), match_score, processed_at, pipeline_version

`samples`
- id (pk), comment_id (fk), batch_name, stratum (`lexicon` | `model` | `random`), exported_at

`sync_state`
- key (pk), value (jsonb) — paging cursors for resumable backfill

Indexes: `comments(media_id)`, `comments(commented_at)`, GIN on `comment_features(matched_roots)`.

## 6. Instagram API notes (verify before use)

Permissions: `instagram_business_basic`, `instagram_business_manage_comments`. Host: `graph.instagram.com`. Standard Access covers accounts added to the app in the dashboard; Advanced Access (App Review) is needed for other accounts.

OAuth:
1. Redirect to `https://www.instagram.com/oauth/authorize` with `client_id`, `redirect_uri`, `response_type=code`, `scope`, and a signed `state`.
2. Exchange code: `POST https://api.instagram.com/oauth/access_token`.
3. Long-lived token: `GET /access_token?grant_type=ig_exchange_token`.
4. Refresh before expiry: `GET /refresh_access_token?grant_type=ig_refresh_token`.

Collection:
- `GET /me/media?fields=id,caption,media_type,permalink,timestamp,comments_count`
- `GET /{media-id}/comments?fields=id,text,timestamp,username,like_count,hidden,parent_id`
- `GET /{comment-id}/replies` with the same fields
- Follow `paging.next` until exhausted.

Webhooks (`comments`, `live_comments`):
- `GET /webhook`: echo `hub.challenge` when `hub.verify_token` matches.
- `POST /webhook`: validate `X-Hub-Signature-256` (HMAC-SHA256 of the raw body with the app secret), then ingest. Respond 200 fast; process in a background task.

## 7. Milestones

### M1 — Scaffold
- Project, Docker Compose, config, models, first Alembic migration, CI-ready `ruff` + `pytest`.
- Accept: `docker compose up` starts Postgres; `alembic upgrade head` creates all tables; `pytest` passes.

### M2 — Security helpers
- `encrypt_token` / `decrypt_token` (Fernet), `hash_author(username_or_id)` (HMAC-SHA256, hex).
- Accept: unit tests for round-trip encryption and deterministic hashing; no secret has a default value.

### M3 — OAuth and account onboarding
- `/connect` (redirect), `/oauth/callback` (exchange, store encrypted long-lived token), minimal success page.
- CLI: `collector accounts list`, `collector tokens refresh` (refresh any token expiring within 10 days).
- Accept: with mocked HTTP (respx), callback stores an account row with an encrypted token; invalid `state` is rejected.

### M4 — API client and backfill
- Paged client with retry/backoff on 429 and 5xx, reading rate-limit headers.
- `collector backfill --account <id> [--since YYYY-MM-DD]`: media, then comments, then replies. Idempotent upserts on `ig_comment_id`. Resumable through `sync_state`.
- Accept: mocked three-page comment listing is fully ingested; rerun inserts zero new rows; interrupting and rerunning resumes from the saved cursor; `author_hash` is set and no username is stored anywhere, including `raw_json` (strip before saving).

### M5 — Webhook receiver
- Verification handshake, signature check, ingestion with `source='webhook'`.
- Accept: bad signature returns 403; valid payloads for both Business Login and Facebook Login shapes are ingested; duplicates are ignored.

### M6 — Swahili normalizer
Steps, in order, each a pure function:
1. Unicode NFKC, lowercase, strip zero-width characters and diacritics.
2. Homoglyph map (Cyrillic/Greek lookalikes to Latin).
3. Leetspeak map: `@→a, 4→a, 3→e, 1→i, !→i, 0→o, $→s, 5→s, 7→t`.
4. Remove separators inside words (`m.j.i.n.g.a`, `m j i n g a`, `m_j_i_n_g_a`).
5. Collapse repeated characters (`mjiiinga → mjinga`).
6. Swahili texting shorthand, token-level: `c→si` prefix (`cjui→sijui`), `x→s` (`xaxa→sasa`), `2→tu`, `ww→wewe`, `nn→nini`, `km→kama`. Keep this table in YAML so it can grow.
- Keep both `text_raw` and `text_norm`.
- Accept: table-driven tests with at least 40 obfuscation cases.

### M7 — Lexicon and matcher
- `sw_roots.yaml`: entries with `root`, `prefixes` (noun-class prefixes such as `m, wa, u, ki, vi, ma`), `severity`, `context_dependent` (true for animal words like `mbwa`, `nyani`, `punda`), `notes`. Ship a small seed list with clearly marked TODO for native-speaker expansion.
- Three matching methods, each recorded in `match_methods`:
  1. Regex generated per root: optional prefix + root with optional junk between letters.
  2. Consonant-skeleton match for dropped vowels (`mjng`), only for roots with 3 or more consonants.
  3. rapidfuzz on tokens with a length-scaled threshold.
- Guard against false positives with an allowlist of innocent words that collide with roots.
- Accept: precision/recall test on a hand-made fixture of 100 positive and 100 negative comments; report numbers in the test output.

### M8 — Processing pipeline
- `collector process`: for unprocessed comments run langid, normalize, match; write `comment_features` with `pipeline_version`.
- Drop from sampling (but keep in DB): empty, emoji-only, link-only, exact duplicates.
- Accept: reprocessing with a bumped version updates rows; runs in batches without loading the whole table.

### M9 — Sampling and export
- `collector sample --batch <name> --size N --mix 50/30/20`: lexicon hits, model-flagged (stub scorer interface for now, pluggable later with AfroXLMR), random.
- `collector export --batch <name> --format csv|jsonl`: columns `sample_id, text_raw, text_norm, matched_roots, stratum, account_category, commented_at` plus empty `label`, `target`, `annotator` columns.
- Accept: no comment appears in two batches; export contains no usernames, IDs of commenters, or @mentions (replace with `@USER`).

### M10 — Ops
- `collector stats` (counts per account, abuse-hit rate, language split).
- README with setup, Meta app configuration steps, consent template pointer, and App Review checklist.
- Accept: a new developer can go from clone to a mocked end-to-end run using only the README.

## 8. Environment variables

```
DATABASE_URL=
IG_APP_ID=
IG_APP_SECRET=
IG_REDIRECT_URI=
IG_WEBHOOK_VERIFY_TOKEN=
TOKEN_ENC_KEY=        # Fernet key
AUTHOR_HASH_KEY=      # HMAC key
GRAPH_API_VERSION=v25.0
```

## 9. Testing rules

- No live API calls in tests; mock with respx and recorded-shape fixtures.
- Every milestone ships with tests; keep coverage of `nlp/` above 90%.
- Add a test that scans all persisted columns for a known test username to prove nothing leaks.

## 10. Out of scope for now

- Annotation UI, classifier training, other platforms, any data from accounts that have not connected.

## 11. Open decisions for Erick

- Seed lexicon contents and who the native-speaker reviewers are.
- Account categories to recruit first (udaku, football, media, musicians).
- Whether Kenyan Sheng is in scope.
- Hosting for the webhook endpoint (needs public HTTPS).
