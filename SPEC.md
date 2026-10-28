# internship-radar — Build Specification (v1.1)

Single-user internship posting aggregator. Polls structured job sources every few hours, diffs against known state, enriches new postings with an LLM (eligibility + fit scoring), and emails a ranked digest every morning plus **instant flash alerts** for top-target postings. Owner: a single student recruiting for Summer 2027 SWE/quant/AI-infra/robotics internships.

**Core value prop: latency.** Surface postings within hours of going live. Flash alerts (§8.6) are what make this claim true; the digest is the complete morning record.

> **v1.1 (2026-08-20):** all endpoints formerly marked VERIFY have been confirmed against live responses; registry pre-populated with probed tokens; design-review fixes applied throughout (prefilter logic, posting identity, closed-detection guards, cluster-aware digest, enrichment queue + structured outputs, Actions hardening); owner decisions incorporated: flash alerts (tier-S or fit ≥ 90), ranked backlog drained over 2–3 mornings, `fit_robotics` as a fourth score, expanded quant/AI-infra registry. §14 adds owner-ecosystem integrations (applied-state import, AgenticApplications export).

## 0. Instructions for Claude Code

- Implement milestones M0 → M4 (§11). **Milestones gate enablement, not development**: later-milestone code may be built and tested (dry-run, mocked API) while an earlier milestone soaks; it merges/enables only after the earlier acceptance passes. Optimize for time-to-first-real-backfill — most Summer 2027 quant apps are already open (verified live 2026-08-20); every build day is peak season.
- Conventions: Python 3.12, `uv` for env/deps (commit `uv.lock` and `.python-version`), `ruff`, `pytest`, type hints everywhere, Pydantic v2 for models. Keep dependencies minimal (§12 lists the allowed set — ask before adding others).
- Prefer the simplest implementation that satisfies the spec. No frameworks, no async unless trivially beneficial, no premature abstraction beyond the adapter interface.
- When the spec is ambiguous: pick the simplest reasonable option, leave a `# DECISION:` comment, and note it in your summary. Do not invent scope.
- Never build: auto-apply of any kind (the §14 export feeds a separate system; radar itself never applies), Instagram scraping, multi-user/auth, a web frontend (v1), CAPTCHAs/bot-detection workarounds.
- Endpoint shapes below were verified against live responses on **2026-08-20**; if a payload disagrees at build time, adjust to reality and note it.
- Politeness is a hard requirement: honest User-Agent, ETag/If-Modified-Since caching, per-host rate limits, backoff on 429/5xx (§6.4).

## 1. Product definition

### 1.1 What it does

- Every ~3 hours: poll all enabled sources, parse into a canonical `Posting`, dedup against the DB, store new/changed rows.
- On new postings: one LLM call per posting extracts eligibility (grad-year window, work auth, deadline, season) and scores fit against four profiles (quant, ai_infra, swe, robotics).
- **Flash alerts**: during any poll, a never-alerted new posting that is tier-S **or** scores fit ≥ 90 triggers an immediate short email (§8.6). Cap 3/day.
- Every morning (~8:00 AM America/New_York): email a digest of everything first seen since the last digest, ranked and grouped, with eligibility flags, plus a "Closing soon" section.

### 1.2 Non-goals (v1)

- No Instagram (no legitimate API for third-party accounts; trackers cover the same content).
- No auto-apply. Human stays in the loop. (§14's export hands data to the owner's separate AgenticApplications project; that boundary is one-way data out.)
- No Workday, no custom career-page scraping (both M3+).
- No dashboard, no multi-user, no mobile app.

### 1.3 The user, for scoring purposes

Example `config/profile.md` (the real profile is supplied via the `RADAR_PROFILE` secret or untracked `config/profile.local.md`) and inject it into the enrichment prompt:

```
Candidate: <school / major> undergrad. Possible graduation
dates: <e.g. May 2028, Dec 2028>. Seeking Summer 2027 internships in the US.
Strengths: <languages, systems, domains, projects, past internships — the more
specific the better; the enrichment LLM scores fit against this text>.
Target tracks, in priority order:
1. quant  — trading/HFT firms: SWE, quant research, or trading intern roles
2. ai_infra — GPU/accelerator, ML systems, inference/training infra roles
3. swe   — strong generalist SWE internships at top tech companies
4. robotics — perception, simulation, controls, robot-software intern roles
Work authorization: do not assume; never filter on it, only flag it.
```

## 2. Architecture

```
        ┌────────────────── every 3h (GitHub Actions cron) ──────────────────┐
        │                                                                    │
 sources ──► fetch ──► parse ──► normalize ──► prefilter ──► dedup ──► store │
 (adapters)                                                       │          │
                                                                  ▼          │
                                              LLM enrichment (queue: ALL     │
                                              unenriched, tier-first, capped)│
                                                                  │          │
                                              flash alert (tier-S or fit≥90, │
                                              ≤3/day, alert_log)             │
        └────────────────────────────────────────────────────────────────────┘
        ┌────────────────── daily 8am ET (second workflow) ──────────────────┐
        │  poll (same as above) ──► targeted enrich of digest set ──►        │
        │  select undigested clusters ──► render jinja2 ──► send via Resend  │
        │  ──► mark digested_at + log to digest_log                          │
        └────────────────────────────────────────────────────────────────────┘
 State: SQLite file committed back to the private repo after each run.
```

### 2.1 Repo layout

```
internship-radar/
├── SPEC.md                  # this file
├── pyproject.toml
├── uv.lock                  # committed; workflows run `uv sync --frozen`
├── .python-version          # "3.12"
├── config/
│   ├── registry.yaml        # company → ATS mapping + aliases (§4)
│   ├── profile.md           # candidate profile (§1.3)
│   └── settings.py          # env var loading, constants
├── src/radar/
│   ├── cli.py               # argparse: poll | digest | bootstrap | probe | dry-run | backfill | export | sync-applied | mark-applied
│   ├── models.py            # Pydantic: Posting, Enrichment, RunReport
│   ├── db.py                # sqlite3 wrapper, user_version migrations, all SQL lives here
│   ├── pipeline.py          # orchestrates fetch→…→store, run bookkeeping
│   ├── normalize.py         # cleaning, prefilter, hashing
│   ├── dedup.py             # identity upsert, clustering, repost/closed detection
│   ├── enrich.py            # Anthropic structured-output call, queue, cost guard
│   ├── digest.py            # cluster selection, grouping, jinja2 render, Resend send
│   ├── alerts.py            # flash alerts (§8.6)
│   ├── http.py              # shared httpx client: UA, caching, rate limit, retries
│   └── adapters/
│       ├── base.py          # SourceAdapter protocol (+ prefilter_policy)
│       ├── greenhouse.py
│       ├── lever.py
│       ├── ashby.py
│       ├── github_trackers.py   # simplify/vanshb03/zshah101 JSON + NUFT YAML + speedyapply tables
│       ├── icims.py             # SIG + AMD public JSON API (§5.9) — M1
│       ├── hn_whoishiring.py
│       ├── reddit_rss.py        # fail-soft; expected to 403 from CI (§5.7)
│       └── workday.py       # M3 (+ eightfold/avature/smartrecruiters/gem/workable per §5.10)
├── templates/digest.html.j2
├── templates/flash.html.j2
├── scripts/probe_ats.py     # registry population helper (§4.2)
├── tests/
│   ├── fixtures/            # recorded JSON/RSS/YAML/HTML responses per adapter
│   └── test_*.py
├── data/jobs.db             # committed state (private repo)
├── .gitignore               # must include data/jobs.db-journal, data/jobs.db-wal
└── .github/workflows/
    ├── poll.yml
    ├── digest.yml
    └── ci.yml
```

## 3. Data model

`db.py` owns all SQL. Migrations via `PRAGMA user_version`: keep an ordered `MIGRATIONS: list[str]` in db.py; on startup apply every entry above the current `user_version` in one transaction, then bump it. v1 ships as migration 1; every later schema change appends. (Plain `CREATE TABLE IF NOT EXISTS` alone has no ALTER path, and `data/jobs.db` is committed state that survives every deploy.)

**Timestamps**: every app-written timestamp is UTC in the exact format `YYYY-MM-DDTHH:MM:SSZ`. Adapters normalize `posted_at` into it at parse time (Lever epoch-ms, tracker epoch-seconds, Ashby ISO-with-offset all converge here). Never compare against SQLite `datetime('now')` output directly (space-separated format sorts differently); compute boundaries in Python.

**SQLite config**: default rollback journal — do NOT enable WAL (single process; WAL sidecars would make the committed jobs.db incomplete). Close the connection explicitly so the file is self-contained when the persist step runs.

```sql
CREATE TABLE postings (
  id            INTEGER PRIMARY KEY,
  company_name  TEXT NOT NULL,          -- normalized display name
  title         TEXT NOT NULL,
  location_raw  TEXT,                   -- joined with '; ' if source has multiple
  url           TEXT NOT NULL,          -- canonical apply/detail link
  source        TEXT NOT NULL,          -- adapter name, e.g. 'greenhouse'
  source_ref    TEXT NOT NULL DEFAULT '',  -- board token / repo / thread id
  source_key    TEXT NOT NULL,          -- per-source natural key, NEVER NULL (see below)
  description   TEXT,                   -- ≤ 12000 chars plain text, middle-out truncated (§7)
  posted_at     TEXT,                   -- normalized UTC if source provides it
  first_seen_at TEXT NOT NULL,
  last_seen_at  TEXT NOT NULL,
  active        INTEGER NOT NULL DEFAULT 1,
  missing_since TEXT,                   -- first successful snapshot this row was absent from
  repost_count  INTEGER NOT NULL DEFAULT 0,
  content_hash  TEXT NOT NULL,          -- sha256(company_norm|title_norm)  ← no location (§6.1)
  cluster_id    INTEGER,                -- dedup cluster; NULL = own cluster
  digested_at   TEXT,                   -- set when included in a sent digest (or bootstrap-marked)
  alerted_at    TEXT,                   -- set when a flash alert included this posting/cluster
  applied_at    TEXT,                   -- set via import-applied / mark-applied (§14)
  enrich_attempts INTEGER NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX idx_post_identity ON postings(source, source_ref, source_key);
CREATE INDEX idx_post_hash ON postings(content_hash);
CREATE INDEX idx_post_cluster ON postings(cluster_id);

CREATE TABLE enrichments (
  posting_id    INTEGER PRIMARY KEY REFERENCES postings(id),
  category      TEXT,                   -- quant | ai_infra | swe | robotics | other
  season        TEXT,                   -- summer_2027 | other | unclear
  grad_earliest TEXT,                   -- 'YYYY-MM' or NULL (no lower bound)
  grad_latest   TEXT,                   -- 'YYYY-MM' or NULL (no upper bound)
  workauth      TEXT,                   -- citizen_only | no_sponsorship | sponsors_ok | unspecified
  deadline      TEXT,                   -- 'YYYY-MM-DD' or NULL
  fit_quant     INTEGER, fit_ai_infra INTEGER, fit_swe INTEGER, fit_robotics INTEGER,  -- 0..100, all-set or all-null
  rationale     TEXT,                   -- <= 25 words
  red_flags     TEXT,                   -- JSON array of strings
  model         TEXT NOT NULL,
  enriched_at   TEXT NOT NULL
);

CREATE TABLE runs (
  id            INTEGER PRIMARY KEY,
  mode          TEXT NOT NULL,          -- poll | digest | bootstrap | dry-run
  started_at    TEXT NOT NULL, finished_at TEXT,
  sources_ok    INTEGER, sources_failed TEXT,   -- JSON {adapter: error}
  new_postings  INTEGER, llm_calls INTEGER, flash_alerts INTEGER DEFAULT 0
);

CREATE TABLE digest_log (            -- audit record; selection uses postings.digested_at
  id            INTEGER PRIMARY KEY,
  sent_at       TEXT NOT NULL,
  posting_ids   TEXT NOT NULL,          -- JSON array of ALL cluster-member ids sent
  subject       TEXT NOT NULL,
  resend_id     TEXT                    -- Resend email id from the 2xx response
);

CREATE TABLE alert_log (
  id            INTEGER PRIMARY KEY,
  posting_id    INTEGER NOT NULL REFERENCES postings(id),
  sent_at       TEXT NOT NULL,
  reason        TEXT NOT NULL           -- 'tier_s' | 'fit'
);

CREATE TABLE kv (                    -- ETags, Last-Modified, tracker SHAs
  k             TEXT PRIMARY KEY,       -- namespaced: 'etag:{url}', 'lastmod:{url}', 'tracker_sha:{repo}'
  v             TEXT NOT NULL,
  updated_at    TEXT NOT NULL
);
```

**`source_key` per source** (the identity — never NULL): greenhouse/lever/ashby/workday = ATS job id (cast ints to str); github_trackers = tracker listing id (UUID); hn = comment id; reddit = entry id (`t3_xxx`); fallback for any future source = `sha256(url)`. Identity is `(source, source_ref, source_key)`; `content_hash` is a dedup **signal**, not identity — a title/location edit on the same key is an in-place UPDATE (title, location_raw, description, content_hash), never a second row, never a repost.

**Digest selection** (cluster-level, see §8.1): representative rows of clusters where **no member** has `digested_at`, `first_seen_at` within the last 14 days (safety window — log a warning if anything undigested ever ages past it), plus never-digested `active=0` rows rendered with an "already closed" badge (they carry signal; never silently drop them).

## 4. Company registry

### 4.1 Format — config/registry.yaml

```yaml
# ats: greenhouse | lever | ashby | icims | workday | eightfold | avature |
#      smartrecruiters | gem | workable | custom | tracker-only | probe
# token: board identifier in the ATS URL; for icims/avature/eightfold = careers host
# tier: S | 1 | 2   (drives digest grouping, enrichment priority, flash alerts — not filtering)
# A company that fails probing degrades to tracker-only (its tier still applies to
# tracker-sourced rows via the alias map below — that wiring is load-bearing).
# Rows whose adapter doesn't exist yet (workday/eightfold/avature/smartrecruiters/
# gem/workable = M3) behave as tracker-only until their adapter ships — the row
# exists now so tier/alias wiring works and M3 needs no registry changes.
companies:
  # ---- verified live 2026-08-20 (probed) ----
  - {name: Anthropic,            tier: S, ats: greenhouse, token: anthropic}
  - {name: Datadog,              tier: 1, ats: greenhouse, token: datadog}
  - {name: Stripe,               tier: 1, ats: greenhouse, token: stripe}
  - {name: Jump Trading,         tier: S, ats: greenhouse, token: jumptrading}
  - {name: Hudson River Trading, tier: S, ats: greenhouse, token: wehrtyou}          # S27 live
  - {name: Five Rings,           tier: S, ats: greenhouse, token: fiveringsllc}      # S27 live; 'fiverings' 404s
  - {name: Radix Trading,        tier: S, ats: greenhouse, token: radixuniversity}   # campus board; lateral = radixexperienced
  - {name: Optiver,              tier: 1, ats: greenhouse, token: optiverus}         # S27 live
  - {name: IMC Trading,          tier: 1, ats: greenhouse, token: imc}               # S27 live
  - {name: DRW,                  tier: 1, ats: greenhouse, token: drweng}            # S27 live; 'drw' 404s
  - {name: Akuna Capital,        tier: 1, ats: greenhouse, token: akunacapital}      # S27 live
  - {name: Old Mission,          tier: 1, ats: greenhouse, token: oldmissioncapital} # 2027 live
  - {name: CTC,                  tier: 1, ats: greenhouse, token: chicagotradingcampus}  # campus; lateral = chicagotrading
  - {name: Tower Research,       tier: 1, ats: greenhouse, token: towerresearchcapital}  # S27 live
  - {name: Belvedere Trading,    tier: 1, ats: lever,      token: belvederetrading}
  - {name: Virtu Financial,      tier: 2, ats: greenhouse, token: virtu}             # 2027 live
  - {name: XTX Markets,          tier: 1, ats: greenhouse, token: xtxmarketstechnologies}
  - {name: OpenAI,               tier: S, ats: ashby,      token: openai}            # ~12MB payload; no intern roles (Residency)
  - {name: Cerebras,             tier: 1, ats: ashby,      token: cerebras}          # migrated off greenhouse
  - {name: Groq,                 tier: 1, ats: tracker-only}   # Gem ATS (jobs.gem.com/groq); greenhouse token dead
  - {name: Together AI,          tier: 1, ats: greenhouse, token: togetherai}
  - {name: Modal,                tier: 1, ats: ashby,      token: modal}
  - {name: Anyscale,             tier: 2, ats: ashby,      token: anyscale}          # lever board is stale
  - {name: Databricks,           tier: 1, ats: greenhouse, token: databricks}        # S27 live
  - {name: Scale AI,             tier: 2, ats: greenhouse, token: scaleai}
  - {name: SpaceX,               tier: 1, ats: greenhouse, token: spacex}            # Spring 27 co-ops live
  - {name: Anduril,              tier: 1, ats: greenhouse, token: andurilindustries} # 2027 live; slow board, needs 45s+ timeout
  - {name: Palantir,             tier: 1, ats: lever,      token: palantir}
  - {name: Notion,               tier: 1, ats: ashby,      token: notion}            # S27 live
  - {name: Roblox,               tier: 2, ats: greenhouse, token: roblox}            # S27 live
  # ---- quant additions, verified live 2026-08-20 (deep source research) ----
  - {name: SIG,                  tier: 1, ats: icims, token: careers.sig.com}  # 47 S27 postings LIVE via public JSON API (§5.9)
  - {name: AQR,                  tier: 1, ats: greenhouse, token: aqr}         # 10× "2027 Summer Analyst" live
  - {name: Walleye Capital,      tier: 1, ats: greenhouse, token: walleyecapital-external-students}  # 3× S27; alias quantic
  - {name: TransMarket Group,    tier: 1, ats: greenhouse, token: transmarketgroup}  # 4 intern roles live
  - {name: Arrowstreet Capital,  tier: 1, ats: workday}   # M3; CXS verified: 2× S27 QD/QR intern live (arrowstreetcapital.wd5, site Campus_Careers)
  - {name: Two Sigma,            tier: 1, ats: avature, token: careers.twosigma.com}  # M3 HTML adapter; 2× S27 intern live
  - {name: Maven Securities,     tier: 2, ats: greenhouse, token: mavensecuritiesholdingltd}  # Chicago 2027 trader intern live
  - {name: Stevens Capital,      tier: 2, ats: greenhouse, token: scm}
  - {name: Xantium,              tier: 2, ats: greenhouse, token: xantium}     # 2 intern roles, posted ~Aug 17, not on trackers yet
  - {name: Seven Research,       tier: 2, ats: greenhouse, token: sevenresearch}
  - {name: BlackEdge Capital,    tier: 2, ats: greenhouse, token: blackedgecapital}
  - {name: Voloridge,            tier: 2, ats: greenhouse, token: voloridgeinvestmentmanagement}  # 2× 2027 intern live
  - {name: GSA Capital,          tier: 2, ats: greenhouse, token: gsacapital}
  - {name: WorldQuant,           tier: 2, ats: greenhouse, token: worldquant}
  - {name: Schonfeld,            tier: 2, ats: greenhouse, token: schonfeld}
  - {name: Geneva Trading,       tier: 2, ats: greenhouse, token: genevatrading}
  - {name: Valkyrie Trading,     tier: 2, ats: lever,      token: valkyrietrading}
  - {name: 3Red Partners,        tier: 2, ats: greenhouse, token: 3redpartners}
  - {name: Vatic Labs,           tier: 2, ats: greenhouse, token: vaticlabs}
  - {name: Voleon,               tier: 2, ats: ashby,      token: voleon}      # lever/voleon is a stale 200+[] — never use
  - {name: Tudor,                tier: 2, ats: greenhouse, token: tudorgroup}
  - {name: Marshall Wace,        tier: 2, ats: greenhouse, token: marshallwace}  # board currently EMPTY; drop at M3 if still empty
  - {name: Ellipsis Labs,        tier: 2, ats: ashby,      token: ellipsislabs}  # "SWE - 2027 Interns" live
  - {name: InfiniteQuant,        tier: 2, ats: smartrecruiters, token: InfiniteQuant}  # M3; 2× S27 QR/QD live (zshah tracker covers meanwhile)
  - {name: PEAK6,                tier: 2, ats: custom}     # M3 sitemap-diff (careers.peak6.com); S27 trading interns live (women+-focused flagship)
  - {name: Balyasny,             tier: 2, ats: tracker-only}  # Salesforce Experience site, no public feed; OpenQuant/trackers cover
  # ---- ai-infra additions, tokens verified live 2026-08-20 ----
  - {name: xAI,                  tier: 1, ats: greenhouse, token: xai}
  - {name: CoreWeave,            tier: 1, ats: greenhouse, token: coreweave}   # includes Weights & Biases roles (alias)
  - {name: Lambda,               tier: 1, ats: ashby,      token: lambda}
  - {name: Tenstorrent,          tier: 1, ats: greenhouse, token: tenstorrent} # migrated off Lever
  - {name: Etched,               tier: 1, ats: ashby,      token: etched}      # 17 intern roles LIVE (inference/chip-sim/firmware/RTL) — tier bumped 2→1
  - {name: SambaNova,            tier: 2, ats: greenhouse, token: sambanovasystems}  # bare 'sambanova' 404s
  - {name: d-Matrix,             tier: 2, ats: ashby,      token: d-matrix}
  - {name: Fireworks AI,         tier: 2, ats: ashby,      token: fireworks}
  - {name: Baseten,              tier: 2, ats: ashby,      token: baseten}
  - {name: Mistral AI,           tier: 2, ats: ashby,      token: mistral.ai}  # dot included; lever 'mistral' is stale 200+[]
  - {name: Cohere,               tier: 2, ats: ashby,      token: cohere}      # intern cycles proven (Canada-located)
  - {name: Perplexity,           tier: 2, ats: ashby,      token: perplexity}
  - {name: Cursor,               tier: 2, ats: ashby,      token: cursor}
  - {name: Vercel,               tier: 2, ats: greenhouse, token: vercel}      # ashby 'vercel' is stale 200+0 jobs
  - {name: Nebius,               tier: 2, ats: greenhouse, token: nebius}
  - {name: Crusoe,               tier: 2, ats: ashby,      token: crusoe}
  - {name: Astera Labs,          tier: 2, ats: greenhouse, token: asteralabs}
  - {name: Deepgram,             tier: 2, ats: ashby,      token: deepgram}    # "SWE Internship (Fall 2026/Summer 2027)" live
  - {name: Hugging Face,         tier: 2, ats: workable,   token: huggingface} # M3; 0 interns today, posts seasonally
  # ---- robotics additions (track 4), tokens verified live 2026-08-20 ----
  - {name: Zipline,              tier: 1, ats: greenhouse, token: flyzipline}  # 41× 2027 intern titles LIVE; 'zipline' 404s
  - {name: Applied Intuition,    tier: 1, ats: ashby,      token: applied}     # RL/robotics research interns live; token unguessable
  - {name: Figure,               tier: 1, ats: greenhouse, token: figureai}    # ashby 'figure' is stale 200+0
  - {name: Physical Intelligence, tier: 1, ats: ashby,     token: physicalintelligence}  # evergreen Research Internships
  - {name: Astranis,             tier: 1, ats: greenhouse, token: astranis}    # 19 interns live, 6× 2027 titles
  - {name: Rocket Lab,           tier: 1, ats: greenhouse, token: rocketlab}   # 36 intern reqs; large board — 60s timeout tier
  - {name: Zoox,                 tier: 1, ats: lever,      token: zoox}        # summer cohort posts ~Sept-Oct
  - {name: Skydio,               tier: 2, ats: ashby,      token: skydio}
  - {name: Shield AI,            tier: 2, ats: lever,      token: shieldai}
  - {name: Apptronik,            tier: 2, ats: greenhouse, token: apptronik}   # "Robotics SW Intern – Real-Time Controls" live
  - {name: Field AI,             tier: 2, ats: lever,      token: field-ai}    # 5 robotics internships live (Fall 26 terms)
  - {name: Nuro,                 tier: 2, ats: greenhouse, token: nuro}
  - {name: Aurora,               tier: 2, ats: ashby,      token: aurora-operations-inc}  # migrated off greenhouse
  - {name: Motional,             tier: 2, ats: greenhouse, token: motional}    # 2 interns live (ML behaviors, mapping SWE)
  - {name: Saronic,              tier: 2, ats: ashby,      token: saronic}     # lever/saronic is stale 200+[]
  - {name: Neros,                tier: 2, ats: greenhouse, token: nerostechnologies}
  - {name: Hadrian,              tier: 2, ats: ashby,      token: hadrian-automation}
  - {name: Gecko Robotics,       tier: 2, ats: ashby,      token: gecko-robotics}  # Pittsburgh robotics
  - {name: Agility Robotics,     tier: 2, ats: greenhouse, token: agilityrobotics}
  - {name: Kodiak,               tier: 2, ats: greenhouse, token: kodiak}
  - {name: Machina Labs,         tier: 2, ats: lever,      token: MachinaLabs} # CASE-SENSITIVE lever token; 3 interns live
  - {name: Serve Robotics,       tier: 2, ats: ashby,      token: serverobotics}
  - {name: 1X,                   tier: 2, ats: ashby,      token: 1x}          # interns currently Norway
  - {name: Waabi,                tier: 2, ats: lever,      token: waabi}       # sim-first AV; interns PhD/Toronto-leaning
  - {name: Boston Dynamics,      tier: 1, ats: workday}    # M3 tenant #3 (bostondynamics.wd1); tracker covers meanwhile
  - {name: Skild AI,             tier: 2, ats: tracker-only}  # CMU spinout; custom site, no public ATS
  # ---- custom stacks: tracker-only until/unless M3 adapter (stack noted) ----
  - {name: Jane Street,          tier: S, ats: greenhouse, token: janestreet}  # public board works; campus flag in Employment Type metadata (§5.2) — no custom adapter needed
  - {name: Citadel,              tier: S, ats: tracker-only}    # custom, bot-protected (403), NOT on Eightfold (verified); no public API
  - {name: Citadel Securities,   tier: S, ats: tracker-only}    # separate firm/pipeline from Citadel — never alias-merge
  - {name: Millennium,           tier: 1, ats: eightfold, token: career.mlp.com}  # M3 (build before Oct campus drop); legacy Workday tenant EMPTY
  - {name: DE Shaw,              tier: S, ats: tracker-only}    # own site
  - {name: NVIDIA,               tier: S, ats: workday}         # M3 tenant #1; tracker covers meanwhile
  - {name: AMD,                  tier: 1, ats: icims, token: careers.amd.com}  # same §5.9 adapter as SIG; legacy Workday dead
  - {name: Arm,                  tier: 1, ats: tracker-only}    # iCIMS (careers.arm.com) — ride §5.9 adapter at M3
  - {name: Qualcomm,             tier: 2, ats: tracker-only}    # Eightfold — ride M3 eightfold adapter
  - {name: Micron,               tier: 2, ats: tracker-only}    # Eightfold (tenant 403s — verify at M3)
  - {name: Marvell,              tier: 2, ats: tracker-only}    # Workday (marvell.wd1/MarvellCareers2) — M3 candidate
  - {name: Waymo,                tier: 1, ats: tracker-only}
  - {name: Google,               tier: 1, ats: tracker-only}
  - {name: Meta,                 tier: 1, ats: tracker-only}
  - {name: Uber,                 tier: 1, ats: tracker-only}    # in-house careers API
  - {name: Netflix,              tier: 2, ats: eightfold, token: explore.jobs.netflix.net}  # M3
  # ---- still probe at M1 (research found no public board; may resolve) ----
  - {name: Point72,              tier: 1, ats: probe}   # incl. Cubist; tracker rows point at greenhouse — resolve at M1
  - {name: PDT Partners,         tier: 1, ats: probe}   # ditto
  - {name: Squarepoint,          tier: 1, ats: probe}
  - {name: Flow Traders,         tier: 1, ats: probe}   # tracker rows point at greenhouse
  - {name: Wolverine Trading,    tier: 2, ats: probe}
  - {name: Headlands,            tier: 1, ats: probe}
  - {name: Aquatic Capital,      tier: 1, ats: probe}
  - {name: DV Trading,           tier: 2, ats: probe}
  - {name: Bridgewater,          tier: 2, ats: probe}

# Alias map: EXACT full-string match on the normalized name (dict lookup — no
# substring/prefix logic, ever; that's how Citadel and Citadel Securities stay
# distinct). Keys and values are company_norm forms (§6.1). This map is what
# gives tracker/HN rows their registry tier — keep it current as trackers
# surface new spellings.
aliases:
  drw trading: drw
  drw holdings: drw
  susquehanna international group: sig
  susquehanna: sig
  hrt: hudson river trading
  imc: imc trading
  imc financial markets: imc trading
  chicago trading company: ctc
  millennium management: millennium
  tower research capital: tower research
  virtu: virtu financial
  d e shaw: de shaw
  the d e shaw group: de shaw
  point72 asset management: point72
  cubist systematic strategies: point72
  quantic: walleye capital
  stevens capital management: stevens capital
  weights biases: coreweave
  weights and biases: coreweave
  figure ai: figure
  kodiak ai: kodiak
  kodiak robotics: kodiak
  aurora innovation: aurora
  1x technologies: 1x
  balyasny asset management: balyasny
  bam: balyasny
  arrowstreet: arrowstreet capital
```

`tracker-only` = rely on the GitHub trackers for this company. The long tail of companies not in the registry still arrives via trackers and HN.

### 4.2 scripts/probe_ats.py

For each `ats: probe` company: generate candidate tokens (lowercase, strip spaces/punctuation, hyphenated, known-word variants), then GET each against:

- `https://boards-api.greenhouse.io/v1/boards/{t}/jobs`
- `https://api.lever.co/v0/postings/{t}?mode=json`
- `https://api.ashbyhq.com/posting-api/job-board/{t}`

HTTP 200 with a plausible jobs payload = hit. Write results back into `registry.yaml` as `ats`/`token` with a `# probed YYYY-MM-DD` comment; leave misses as `ats: probe` and print them for manual follow-up (they behave as tracker-only until resolved). Rate limit: ≤ 1 req/sec total. The 2026-08-20 pre-population above came from exactly this procedure; run the script at M1 for the remaining `probe` rows and rerun manually as needed.

**Traps found live 2026-08-20 — the probe MUST handle all four:**
1. **Stale 200-with-empty-board**: Lever and Ashby return 200 + empty jobs array for abandoned org slugs (lever `mistral`, lever `voleon`, lever `saronic`, ashby `vercel`, ashby `figure` are all live examples). A 200 with 0 jobs never beats a 200 with a populated board on another ATS; a 200 with 0 jobs alone is at best a "watch" entry, never an auto-confirmed hit.
2. **Token collisions**: greenhouse `archer` is a veterinary clinic, not Archer Aviation. A hit must pass a company-identity sanity check (company name/board title appears in the payload or a sampled job's `absolute_url`/company field) before being written to the registry.
3. **Case-sensitive Lever tokens**: `MachinaLabs` works, `machinalabs` 404s. Candidate generation must include CamelCase variants, not lowercase-only.
4. **Unguessable tokens**: `applied` (Applied Intuition), `flyzipline`, `wehrtyou`, `aurora-operations-inc`, `hadrian-automation`, `field-ai`. When token generation misses, fetch the company's careers page HTML and sniff for `boards.greenhouse.io/{t}`, `jobs.lever.co/{t}`, `jobs.ashbyhq.com/{t}` embeds before giving up.

Bonus input: the zshah101 tracker repo (§5.5) publishes `data/companies.json` (~4,300 discovered employer boards with ATS tokens) — feed it to the probe as a candidate list for future registry expansion.

## 5. Source adapters

### 5.1 Interface — adapters/base.py

```python
class SourceAdapter(Protocol):
    name: str
    prefilter_policy: str  # 'full' (title include-check + excludes) | 'excludes_only' (§6.2)

    def fetch(self) -> list[RawItem]: ...  # network I/O only
    def parse(self, raw: RawItem) -> list[Posting]: ...  # pure, unit-testable
```

Rules: adapters never touch the DB; failures raise, `pipeline.py` catches per-adapter so one failure never kills the run (record in `runs.sources_failed`; a failed adapter **must skip closed-detection for its boards this run** — §6.3). Every `parse` must be covered by a fixture test. One `Posting` per source item even when multi-location (join locations with '; '), never one row per location.

### 5.2 greenhouse — per registry company ✅ verified

- GET `https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true`
- Response: `{jobs: [...], meta: {total}}`, full board in one response (no pagination; `len(jobs) == meta.total`).
- Map: `title`; `absolute_url` → url; `location.name` → location_raw; `id` (**int — cast to str**) → source_key; **`first_published` → posted_at** (NOT `updated_at`, which moves on any edit and would resurface year-old postings as new); `content` → description.
- `content` is **HTML-entity-escaped**: `html.unescape()` first, then HTML→text.
- `application_deadline` exists as a field but was null on 1495/1495 live jobs sampled — do not rely on it.
- Big boards (SpaceX 2153 jobs, Anduril 2237 — Anduril needed >20s) — use the 60s timeout tier (§6.4).
- **Employment Type metadata folding** (verified live 2026-08-20): some boards — Jane Street, with 50 campus roles live — strip 'Intern' from public titles and carry the campus flag only in the `metadata` field `Employment Type` ('Summer Internship', 'Winter Co-Op', ...). When that value is intern-flavored and the title lacks an intern token, the adapter appends it to the title (`Sales and Trading — Summer Internship`) so the §6.2 prefilter and the digest see it. This is also why title-keyword trackers (zshah101 etc.) show zero Jane Street internships.

### 5.3 lever — per registry company ✅ verified

- GET `https://api.lever.co/v0/postings/{token}?mode=json` — response is a top-level JSON array.
- Map: `text` → title; `hostedUrl` → url; `categories.location` → location_raw; `id` (UUID string) → source_key; `createdAt` (epoch **ms**) → posted_at.
- **Description must be assembled**: `descriptionPlain` contains ONLY the opening prose — the bullet `lists` sections (where grad-year/work-auth eligibility lives; present on 303/308 live postings sampled) and `additionalPlain` are separate. Build `descriptionPlain + "\n" + each lists[].text + stripped lists[].content + "\n" + additionalPlain`, then truncate per §7.
- Also available if useful: `country` (ISO-2), `workplaceType`, `applyUrl`.

### 5.4 ashby — per registry company ✅ verified

- GET `https://api.ashbyhq.com/posting-api/job-board/{token}` — response `{jobs: [...], apiVersion: "1"}`. Descriptions included by default (no query param needed; `?includeCompensation=true` only adds comp fields — not needed).
- Map: `title`; `jobUrl` → url (posting page; `applyUrl` = jobUrl + '/application'); `location` (**plain string**) → location_raw, appending `secondaryLocations[].location` joined '; '; `id` (UUID string) → source_key; `publishedAt` (ISO-with-offset) → posted_at; `descriptionPlain` → description.
- OpenAI's board is ~12 MB / 737 jobs — no small response-size caps in http.py.

### 5.5 github_trackers ✅ verified

Three repos, three parse modes, one adapter (config-driven):

1. **`SimplifyJobs/Summer2027-Internships`** (primary; updated multiple times daily). Default branch is **`dev`** — fetch `https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/.github/scripts/listings.json` (10.8 MB; the path is fixed — tree-walk only as fallback). Diff against last processed commit SHA in `kv`.
   - **The file mixes ALL seasons** (8,415 Summer-2026 items vs 693 Summer-2027 live right now): keep only items where `terms` contains `"Summer 2027"` AND `is_visible` is true; use `active` for closed/repost detection.
   - Fields: `company_name`, `title`, `locations` (**list** — join '; '), `url` (clean direct link), `date_posted`/`date_updated` (**epoch seconds** — convert), `id` (UUID) → source_key.
   - Free pre-enrichment signals — map directly, no LLM needed: `sponsorship` → workauth (`'U.S. Citizenship is Required'`→citizen_only, `'Does Not Offer Sponsorship'`→no_sponsorship, `'Offers Sponsorship'`→sponsors_ok, `'Other'`→unspecified); `category` (`Quant`/`AI/ML/Data`/`Software`/`Hardware`/`Product`) seeds category; `degrees` hints the grad window. Store these on a stub enrichment only if enrichment is skipped; otherwise pass them as context in the prompt.
   - No description field — tracker rows enrich on company/title/location only; expect more `unclear` values (render gracefully, §8).
   - listings.json contains true near-dupes the README dedupes — the §6.3 fuzzy dedup must handle same-source dupes (fixture-test it).
2. **`vanshb03/Summer2027-Internships`** (secondary; default branch `dev`; same listings.json path). Schema variant: `season` field instead of `terms`, no category/degrees. Small parse branch.
3. **`northwesternfintech/2027QuantInternships`** (quant-focused — owner's #1 track). One YAML per company under `./data` (`name`, `website`, `locations`, `notes`, `roles[].role_type` QT/QR/SWE/FPGA, `roles[].links[].url`). Ingest the YAMLs (README is CI-generated from them). No dates — first_seen_at carries. source_key = `sha256(url)` per role link.
4. **`zshah101/Automated-List-Of-Summer-2027-and-Fall-2026-Tech-Internships`** (the M3-gap proxy — verified 2026-08-20). Auto-scrapes ~4,300 employer boards every ~30 min INCLUDING ATSs radar can't reach pre-M3: Workday, Oracle, SmartRecruiters, Workable, amazon.jobs. Machine-readable JSON: `data/jobs.json` (dict keyed `source:company_slug:jobid` — that key is source_key — with title, company, DIRECT employer url, location, season, sponsorship, category, posted_at, first_seen_at, is_open/closed_at) and an active-only mirror at `docs/api/jobs.json`; prefer `data/jobs.json` (is_open feeds closed-detection) and verify both paths at build. Filter `season ∈ {"Summer 2027","Not stated"}` and `is_open`. MIT-licensed, 30-min commit cadence verified. Caveat: single-maintainer repo — fail-soft and don't build anything that breaks if it vanishes. Bonus files: `data/companies.json` (probe input, §4.2) and `docs/api/radar.json` (predicted application-window opening dates for ~200 companies incl. quant firms — surface interesting ones in the digest footer as a v1.5 nicety, optional).
5. **`speedyapply/2027-SWE-College-Jobs` + `speedyapply/2027-AI-College-Jobs`** (one adapter, two config rows — verified 2026-08-20). READMEs are **markdown pipe tables** (HTML anchors inside cells) between stable sentinel comments `TABLE_FAANG_START/END`, `TABLE_QUANT_START/END`, `TABLE_START/END`, fixed 6 columns, direct employer apply links (metacareers, nvidia.wd5, citadel.com, optiver.com — no affiliate redirects). Parse README.md only (= USA internships; ignore INTERN_INTL.md / NEW_GRAD_*.md). ~240 rows each, updated daily 12:00 UTC. source_key = `sha256(apply_url)`; the Age column is relative — first_seen_at carries. Value: FAANG+/Quant sections with tier-S echoes (Citadel, HRT, Five Rings) and salary data.

Use the GitHub API sparingly (unauthenticated = 60 req/hr; in Actions pass `GITHUB_TOKEN`); prefer raw.githubusercontent.com for file fetches. Simplify README fallback: **emergency-only** — it is an **HTML `<table>`, not a markdown pipe table** (speedyapply's is the pipe-table one), is a strict subset of listings.json, lacks absolute dates, and its links carry `?utm_source=Simplify&ref=Simplify` (strip). Do not add: cvrve/SharunKumar/jobright/Ouckah 2027 repos (verified 404); SuryaHarikrishnan (declared strict subset of Simplify+vanshb03); DereC4/internatlas (re-aggregate sources radar polls first-hand); dreamworkhq (all 685 URLs route through their own tracking domain); workopia (data file broken, SEO filler). M3 watch-list if more coverage is ever wanted: zapplyjobs/Internships-2027 (15-min cadence but README-only; their JSON is 401-gated — recheck for a public data file), Emjumaev/FAANG-2027-Internships-Tracker (scrapes 28 in-house big-tech careers APIs incl. Amazon/Apple; 7-star durability risk), ApplyGuy/2027-Internships (use listingUrl, never their affiliate url), aprameyak/2027-tech-jobs (only source with SIG iCIMS + Tesla rows, but 45% of records lack URLs — moot if §5.9 ships).

### 5.6 hn_whoishiring ✅ verified

- Find latest thread: GET `https://hn.algolia.com/api/v1/search_by_date?tags=story,author_whoishiring&query=who%20is%20hiring` — **do not take hits[0] blindly**: "Who wants to be hired?" is posted the same second and ties on created_at. Filter hits by `/^Ask HN: Who is hiring\?/i`, take the newest.
- Fetch comments: GET `https://hn.algolia.com/api/v1/items/{story_id}`; top-level `children` only (fields: id, author, text HTML-escaped, created_at).
- Keep comments matching `/\bintern(s|ship)?\b|\bco[- ]?op\b/i` (strict — bare `/intern/i` matched 25/243 in the live Aug 2026 thread but 21 were internal/internet/international false positives; strict matched 4).
- Posting per kept comment: company_name = first `|`-separated token of the first line (best effort); **title = first non-empty line of the comment, HTML-stripped, truncated to 120 chars** (fallback "See posting" only if empty — a constant title would collapse the dedup hash across months and read terribly in the digest); url = HN comment permalink; source_key = comment id; description = comment text.
- Expectations: ~0–2 relevant postings/month (live thread had zero Summer-2027-US-relevant intern comments). Keep because it's nearly free.

### 5.7 reddit_rss ✅ verified — expected to fail from CI; fail-soft

- Reality as of 2026-08: anonymous RSS is throttled to ~1 request/minute even from residential IPs (live x-ratelimit headers); unauthenticated `.json` is 403 (since ~May 2026); old.reddit RSS 302s to login; **datacenter/cloud IPs (GitHub Actions) get 403'd** via IP reputation + TLS fingerprinting. Polling from Actions will most likely 403 — build fail-soft per §5.1, record in sources_failed without noise, and per §13's rule drop the source if it 403s consistently for two weeks. The only sanctioned contingency is the official OAuth script-app API — not v1.
- If it does work: feeds `https://www.reddit.com/r/csMajors/new.rss` (+ optionally r/quantfinance), spaced **≥ 60s apart** (the anonymous bucket empties after one request). Parse with feedparser (entry fields: title, link, published, author, `id` `t3_xxx` → source_key, content HTML).
- Adapter title filter: `/intern|co[- ]?op|2027|summer analyst/i` — the 'new grad' arm is dropped (owner graduates after the new-grad window; new-grad roles are unusable noise). `entry.link` is always the comments permalink; an outbound link exists only as an `<a>` inside the content HTML — use permalink as url in v1 (don't parse content HTML for links; low-value source).

### 5.8 workday (M3)

- Per-tenant: POST `https://{tenant}.wd{n}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs` with `{"limit": 20, "offset": 0, "searchText": "intern"}`, paginate. Shapes vary per company — verify per tenant.
- Tenant order: (1) NVIDIA; (2) **Arrowstreet Capital** — CXS verified live 2026-08-20 (`arrowstreetcapital.wd5`, site `Campus_Careers`, returns 2× Summer 2027 QD/QR intern postings); (3) Boston Dynamics (`bostondynamics.wd1`, verify site name); (4) Marvell (`marvell.wd1/MarvellCareers2`). Note: Millennium's Workday tenant responds but holds 0 jobs (they moved to Eightfold) — Workday will NOT cover them.

### 5.9 icims — SIG + AMD (ship with M1; it's a plain public JSON API) ✅ verified

- GET `https://{careers-host}/api/jobs?page={n}&limit=100` (optionally `&keywords=intern`). Hosts: `careers.sig.com`, `careers.amd.com` (Arm's `careers.arm.com` rides the same adapter at M3). No auth, no cookies; **verified returning clean JSON to datacenter IPs** on both hosts 2026-08-20.
- Response `{jobs: [{data: {...}}], totalCount}`; paginate while `page*limit < totalCount`. Map: `req_id` → source_key; `title`; `description` (+`qualifications`) → description (full text inline); `posted_date` (ISO+offset) → posted_at; `city`/`state`/`country` → location_raw; `apply_url` → url.
- Why M1 not M3: SIG had **47 postings matching "Summer 2027" live on 2026-08-20** (QR, Quant Systematic Trading, Trading System Engineering, Quant Strategy Developer internships) — tier-1 quant coverage for one adapter that is mechanically identical to the big three.

### 5.10 other verified surfaces (M3, in this order)

All verified live 2026-08-20; each is a small generic adapter serving every tenant on that platform:
1. **Eightfold** — GET `https://{host}/api/apply/v2/jobs?domain={domain}&start=0&num=100`; positions[].id → source_key, t_create epoch-s → posted_at. Tenants: Millennium (`career.mlp.com`/mlp.com — **build before ~October**, their 2027 campus drop lands then), Netflix (`explore.jobs.netflix.net`/netflix.com); Qualcomm/Micron possible later (Micron's tenant 403s — verify). **Does NOT cover Citadel** — verified: no Citadel Eightfold hosts exist; their sites 403 honest requests. Citadel-family stays tracker-only, full stop.
2. **Avature (Two Sigma)** — server-rendered HTML, no JSON: GET `https://careers.twosigma.com/careers/InternshipsAndEarlyCareers` (+ `/careers/OpenRoles?jobRecordsPerPage=100`), parse `JobDetail/{slug}/{id}` anchors, trailing id → source_key. 2× Summer 2027 intern postings live (QR, AI Research Scientist). Route names are per-portal config (`SearchJobs` 404s here but works on other Avature tenants, e.g. bloomberg.avature.net — an optional swe add).
3. **SmartRecruiters** — GET `https://api.smartrecruiters.com/v1/companies/{id}/postings?limit=100&offset={n}`, keyless; list lacks descriptions (one extra GET per posting at `/postings/{id}`). Tenants: InfiniteQuant (2× S27 QR/QD live), optionally Bosch (`BoschGroup` — robotics, Pittsburgh research; EU-heavy, filter country=us) and ServiceNow.
4. **Gem (Groq)** — POST `https://jobs.gem.com/api/public/graphql` `{operationName: 'JobBoardList', variables: {boardId: 'groq'}, query: ...}` (query string documented in research notes; endpoint verified, board currently 0 postings).
5. **Workable (Hugging Face)** — GET `https://apply.workable.com/api/v1/widget/accounts/{account}?details=true` (or POST v3 for pagination); shortcode → source_key.
6. **Quant boards** — OpenQuant: parse `__NEXT_DATA__` from `https://openquant.co/?level=Internship` (25 newest internships, direct ATS ApplicationUrl → use as canonical url so rows cluster with ATS rows; pagination is server-ignored; robots permissive). The Wall Street Quants (`/quant-internships`, server-rendered HTML, 65× "Summer 2027" today, carries DE Shaw/Bridgewater/Verition/PEAK6 rows; stay off their /api/ per robots). PEAK6: sitemap-diff `https://careers.peak6.com/sitemap.xml`.
7. **Google Alerts RSS** (long-tail net, $0): owner creates 3-5 site:-scoped alerts once (`site:job-boards.greenhouse.io intern 2027`, same for boards.greenhouse.io / jobs.lever.co / jobs.ashbyhq.com), delivery = RSS; radar polls the Atom feeds with feedparser, unwraps Google redirect `url=` params, source_key = sha256(unwrapped url), prefilter_policy = excludes_only. Crawl-latency-bound (days) — a coverage net, never a flash-alert source; empty feed is normal, never closure evidence. Feed URLs are capability URLs — store as secrets.

Evaluated and rejected (do not revisit without new evidence, all verified 2026-08-20): hiring.cafe (API 401/405 + Cloudflare datacenter blocks), jobright/untapped/wellfound/WaaS/intern-list (login-gated or bot-walled or Airtable-internal), Simplify's own site API (no public posting surface beyond the repo), cscareers.dev (dead: no DNS on api host, page 500s), levels.fyi (salary dataset, 9 rows for 2027, no links), aijobs.net (API+RSS retired), RemoteML (domain parked), MLOps.community jobs (403), latent.space/GPU MODE (Discord-only), ROS Discourse jobs (open JSON but senior/international content), IEEE JobSite (403), QuantNet/WSO/eFinancialCareers (Cloudflare/bot-walled), Adzuna/Jooble/Careerjet (lagging scraped copies with redirect links), The Muse API (open but client-limited coverage, no date sort — M3 pilot at most), USAJOBS (federal/citizen-only, off-track), Google News RSS (announcements not postings), HN Algolia keyword search (zero signal beyond who-is-hiring), Brave/Bing/Google CSE search APIs (paid/retired/closed to new customers), newsletter bridges (SWE List = slower duplicate of trackers already polled).

## 6. Normalize, prefilter, dedup

### 6.1 Normalize (normalize.py)

- `company_norm`: NFKC, lowercase, strip legal suffixes (`inc|llc|ltd|llp|lp|plc|corp|co`), strip punctuation, collapse whitespace; then **exact full-string dict lookup** through registry `aliases` (never substring/prefix matching — regression test: `company_norm('Citadel Securities') != company_norm('Citadel')` and no alias maps one to the other).
- `title_norm` (for hashing/fuzzy only): (1) NFKC, lowercase, map Unicode dashes `–—−` to `-`; (2) rewrite season-year phrases (parenthesized or not, either order) to the bare season word: `(Summer 2027)` / `- Summer 2027` / `2027 Summer` → ` summer `; (3) remove remaining standalone years `\b20\d{2}\b` and req IDs `\b(req|job|jr|r)[-#]?\d{3,}\b`; (4) strip remaining parentheticals; (5) strip punctuation Unicode-aware (`[^\w\s]`), collapse whitespace. Keeping the season word (dropping the year) preserves Summer-vs-Fall distinctions.
- `content_hash = sha256(f"{company_norm}|{' '.join(sorted(title_norm.split()))}")` — sorted token bag, **no location** (location strings drift too much across sources for the hash to ever collide; cross-city same-role postings belong in one cluster, with locations aggregated at render).
- Test vectors: 'Software Engineering Intern - Summer 2027 (Remote)', 'Software Engineering Intern — Summer 2027', '2027 Summer Software Engineering Intern', and 'Software Engineering Intern (Summer 2027)' must produce one hash.

### 6.2 Deterministic prefilter (gates INSERTION of new rows only — never touches existing rows)

Stated in keep-form (the v1.0 "drop rows failing ALL of" phrasing was logically inverted — the truth-table tests below pin the fix):

```python
INCLUDE_RE = re.compile(
    r"\bintern(s|ship|ships)?\b|\bco[-\s]?ops?\b|\bsummer\s+(analyst|associate)\b", re.I
)
SEASON_2026_TITLE = re.compile(
    r"\b(summer|spring|fall|autumn|winter)\s*(of\s*)?['’]?\s*2026\b"
    r"|\b2026\s*['’]?\s*(summer|spring|fall|autumn|winter)\b",
    re.I,
)


def keep(posting, policy):  # policy from adapter.prefilter_policy
    if policy == "full" and not INCLUDE_RE.search(posting.title):
        return False  # ATS boards: title is professionally written
    if SEASON_2026_TITLE.search(posting.title) and not re.search(r"\b2027\b", posting.title):
        return False  # explicit other-season title, unrescued
    return True
```

- `prefilter_policy = 'full'` for greenhouse/lever/ashby/workday (full boards, mostly non-intern roles). `'excludes_only'` for github_trackers (internships by construction), hn (adapter already gates on the intern regex), reddit (adapter already gates on title).
- **Never scan descriptions for years** — nearly every live Summer-2027 posting mentions a 2026 date (deadlines, "posted", company history); that is exactly the ambiguity enrichment exists to classify.
- The v1.0 `/unpaid/i` and `/\bPhD\b.*required/i` hard-excludes are **deleted**: both had verified false-negative modes ('this is NOT an unpaid internship'; 'BS/MS/PhD required' — which *accepts* undergrads and disproportionately hits ai_infra research roles). The LLM red_flags cover both; a false positive costs one $0.004 call, a false negative costs a posting.
- Required truth-table tests: intern-title/no-exclude→keep; intern-title/'Fall 2026' title→drop; non-intern-title (policy full)→drop; 'Summer 2027 ... Apply by Oct 15, 2026' in description→**keep**; '2027 Software Engineering Internships' (plural)→keep; 'Quantitative Trading Summer Analyst'→keep; HN 'See posting'-shaped row (policy excludes_only)→keep.

### 6.3 Dedup (dedup.py)

- **Identity upsert**: `(source, source_ref, source_key)` exists → UPDATE last_seen_at, active=1, clear missing_since; if fetched content differs, also UPDATE title/location_raw/description/content_hash in place (an edit is an update, not a new posting, not a repost).
- **Clustering** (new rows): if `content_hash` matches an existing row, or fuzzy matches within the same company_norm (`rapidfuzz.token_sort_ratio(title_norm_a, title_norm_b) > 92` — do NOT use token_set_ratio; it scores 'Software Intern' vs 'Software Intern, Machine Learning' at 100), set `new.cluster_id = COALESCE(match.cluster_id, match.id)` and backfill `match.cluster_id = match.id` if NULL. If a new row bridges two clusters, merge (`UPDATE postings SET cluster_id=:keep WHERE cluster_id=:other`). A row joining a cluster is *not new* for digest purposes iff any member is already digested (§8.1 handles this at selection — late tracker echoes of an already-sent ATS posting stay suppressed).
- **Repost**: an identity that went `active=0` and reappears → repost_count += 1, clear digested_at? No — keep digested_at history but mark repost: it re-enters the digest via a `reposted` flag (render again with badge; suppress if `applied_at` set, §14).
- **Closed detection** — guarded: presence is computed from the **post-parse, pre-prefilter** item set (a prefilter change must never inactivate stored rows), per `(source, source_ref)` board, and ONLY when that adapter succeeded this run. A 304 Not-Modified means "snapshot unchanged" — skip the diff entirely. Two-strike: first absent successful snapshot sets `missing_since`; `active=0` only on the second consecutive absence; presence clears `missing_since`. Refuse to close and flag in the run report if a snapshot would close 100% of a board's ≥5 active rows (empty-payload guard).

### 6.4 HTTP hygiene (http.py)

Single shared client: `User-Agent: internship-radar/0.1 (personal aggregator; contact {OWNER_EMAIL})`; per-host min interval 1s (reddit: ≥60s between feed fetches); honor ETag/Last-Modified (persist in `kv`); retries ×3 with exponential backoff + jitter on 429/5xx; timeout 30s default with a 60s tier for known-large boards (spacex, andurilindustries, openai); never retry 4xx≠429; no response-size cap below ~15 MB (Ashby/tracker payloads).

## 7. LLM enrichment (enrich.py)

- Model: `claude-haiku-4-5-20251001` via the Anthropic Messages API (`RADAR_MODEL` to override). Pricing $1/$5 per MTok → ~$0.004/posting; steady state ~$0.20–0.60/day; full backlog bootstrap ~$8 one-time.
- **Provider switch (owner request 2026-08-21):** `RADAR_LLM_PROVIDER` = `anthropic` (default) | `openai`. With `openai`, enrich.py uses the OpenAI SDK's structured-output parse against the same `EnrichmentOut` Pydantic model (`RADAR_OPENAI_MODEL`, default a current mini-tier model — verify the exact id at build time). If only `OPENAI_API_KEY` is configured, the provider auto-selects `openai`. One prompt, one schema, two thin clients; everything downstream is provider-agnostic.
- **Structured outputs, not JSON-prompting**: use `client.messages.parse(model=..., max_tokens=500, system=SYSTEM, messages=[posting_block], output_format=EnrichmentOut)` with the Pydantic model from models.py — grammar-constrained decode, enums enforced server-side, `resp.parsed_output` validated. No fence-stripping, no invalid-JSON retry path (delete those tests). Guard: if `stop_reason == "max_tokens"`, treat as a transient failure. Only pass `temperature` when the model accepts it (Haiku 4.5 does; Claude 4.6+/5 reject non-default sampling params and RADAR_MODEL is overridable).
- **No prompt caching** — deliberate: Haiku 4.5's minimum cacheable prefix is 4,096 tokens; our stable prefix is ~600, so `cache_control` silently no-ops (and would save <$0.10/day even if it worked). Leave a comment in enrich.py so nobody "optimizes" it in.
- **Queue**: each run enriches up to the cap from **ALL active postings lacking an enrichments row** with `enrich_attempts < 3` (not just this run's inserts — the backlog must drain), ordered: tier S → 1 → 2 → registry-matched → everything else, then first_seen_at desc. One representative per cluster is enough (prefer the member with the richest description); copy the enrichment reference to the cluster at render time.
- **Failure classes**: transport/API errors (429 after SDK retries, 5xx, timeouts) → increment enrich_attempts, write NO row (retried next run). Validation surprises → same. After 3 attempts → null enrichment row with `red_flags=["enrichment_failed"]`. Test: mock a 500, assert re-enrichment next run.
- Cost guard: `MAX_LLM_CALLS_PER_RUN` (default 300); `radar bootstrap` uses `BOOTSTRAP_MAX_LLM_CALLS` (default 2000). Beyond cap: leave unenriched (they stay queued), flag in run report.
- Re-enrich when: repost_count changed, or description changed while the posting is not yet digested (the only window where fresher scores are user-visible). Never re-enrich already-digested postings on edits.
- Tracker rows: pass the tracker's `sponsorship`/`category`/`degrees` fields as extra context lines in the posting block; they're free signal.

System prompt (stable; profile lives here, not in the user turn):

```
You extract structured recruiting data from job postings for one specific
candidate, and score fit for that candidate only.

CANDIDATE PROFILE:
{contents of config/profile.md}

Score each track independently against this specific candidate:
  90-100 bullseye: squarely in-track AND the posting asks for the candidate's
         strengths (C++/systems, HPC, RL/game AI, robotics, backend+agents)
  75-89  strong: clearly in-track; candidate is a credible, competitive applicant
  50-74  plausible: adjacent or generalist; worth a look, not a priority
  25-49  weak: mostly off-track, or emphasizes skills the candidate lacks
  0-24   wrong direction: different discipline, seniority, or field
Most postings should score below 50 on at least two of the four tracks.
Use the full range; do not cluster scores in the 60-85 band.

deadline: only if the posting states an explicit application deadline date;
never infer one from start dates, posting dates, or season language; if the
stated deadline is relative or ambiguous, use null.

grad_window = graduation dates the posting accepts (e.g. "graduating Dec 2027
– Jun 2028" → earliest 2027-12, latest 2028-06). Null side = unbounded.
```

User prompt: the posting block only (`company/title/location/description` + tracker extras). Output schema = the Enrichment Pydantic model: category (quant|ai_infra|swe|robotics|other), season (summer_2027|other|unclear), grad_window {earliest,latest}, workauth, deadline, fit {quant, ai_infra, swe, robotics} 0–100 (all-set, Pydantic-enforced), rationale ≤25 words, red_flags (e.g. 'juniors only', 'US citizens only', 'unpaid', 'PhD only').

**Description handling** (normalize.py, at store time): cap 12,000 chars; if longer, middle-out truncate — first ~8,000 + `\n[...truncated...]\n` + last ~4,000 (eligibility text lives at the bottom of long postings; head-only truncation cuts exactly what enrichment extracts). enrich.py sends the stored description verbatim.

**Grad-date flag logic** (computed in digest.py, not by the LLM): candidate dates (from the profile) ∩ `[grad_earliest, grad_latest]`: all three fit → `✓ any grad date`; some fit → `⚠ use {matching dates}`; none fit → `✗ outside window` (still shown, never hidden); window null → no flag.

## 8. Digest email (digest.py + templates/digest.html.j2)

### 8.1 Selection and ordering

- Unit of selection/render is the **cluster** (`GROUP BY COALESCE(cluster_id, id)`): clusters where no member has `digested_at`, newest-first within groups. Representative = member with a direct-ATS source if any (better URL + description), else earliest first_seen_at; display aggregates distinct locations and shows compact source badges (`gh · tracker · hn`). On send, set `digested_at` on ALL members and log all ids to digest_log — late-arriving members of a sent cluster stay suppressed.
- Sort key: `COALESCE(MAX(fit_quant, fit_ai_infra, fit_swe, fit_robotics), -1) DESC` (SQLite scalar MAX null-poisons if any arg is null — Pydantic enforces all-or-none; the COALESCE guards the render regardless), tier as tiebreak.
- Never-digested rows with `active=0` are included with an **"already closed"** badge (fast-closing quant postings carry signal; a §6.3 false-closure must not hide a posting).
- Row caps: Strong fits / Worth a look uncapped (small in steady state); "Everything else new" capped at 40 rows + "and N more — they roll into tomorrow" (only rendered rows get digested_at, so the remainder rolls forward automatically). Total target < 150 rows/email, AND a hard **95 KB HTML byte budget** (Gmail clips at ~102 KB and hides the footer): build_digest trims from the tail — Everything-else first, then Worth-a-look, then Strong — until the rendered HTML fits; trimmed clusters stay undigested. (Measured 2026-08-21: the raw backlog digest was 174 KB at 150 rows; the budget rendered 78.)

### 8.2 Groups

- **Strong fits** — max fit ≥ 75, or unenriched tier-S. Tier-S enriched rows qualify only if `category ∈ {quant, ai_infra, swe, robotics}` or max fit ≥ 50 (a tier-S recruiting-coordinator intern does not outrank a fit-85 SWE role).
- **Worth a look** — max fit 50–74.
- **Closing soon** — active postings with `deadline` within 14 days, sorted by deadline; exempt from the never-resend rule (this section may repeat rows); suppressed for `applied_at` rows.
- **Everything else new** — compact single-line list (includes unenriched non-tier-S).

### 8.3 Row and footer

Row: Company — Title (linked) · locations · category chip · season chip **only when season != summer_2027** ('off-season' / muted 'season?') · grad-date flag (§7) · workauth if not unspecified · deadline if present · `reposted` / `already closed` / `⚡ alerted` / `applied` badges · rationale in small text.
Footer: run stats — sources polled/ok/failed (name the failures), new postings, LLM calls, flash alerts sent.

### 8.4 Subject and heartbeat

- Subject: `Radar: {n} new ({s} strong) — {Ddd Mon D}` where n = rendered clusters, s = Strong-fits rows. **When any source failed: prefix `Radar [{k} sources down]: ...`** — the failure signal must be inbox-level, not buried in a footer nobody reads on a 0-new day.
- Empty day: still send (`Radar: 0 new — …`) — the heartbeat. `SEND_EMPTY=false` disables (documented tradeoff: you lose the aliveness signal).

### 8.5 Sending

- POST `https://api.resend.com/emails`, `Authorization: Bearer {RESEND_API_KEY}`, JSON `{from, to, subject, html}`. Include an `Idempotency-Key` header derived from the digest date (verify exact header name in Resend docs) so an Actions retry can't double-send.
- **Order: send first; write digested_at + digest_log (with the returned Resend email id) only after a 2xx.** Invariant: prefer a duplicate digest over a lost posting. `radar digest` exits nonzero if the send fails (trips Actions notifications).
- From: `onboarding@resend.dev` — **works ONLY when DIGEST_TO equals the Resend account's own signup email** (403 otherwise, verified in Resend docs). Domain verification is the upgrade the moment that constraint chafes; until then, add a Gmail filter (`from:resend.dev → never send to spam`) and check the inbox (not spam) for M2 acceptance.
- HTML: single jinja2 template, inline CSS, table layout, dark-mode-safe (no pure #fff/#000; mid-tone chip backgrounds with explicit text colors — test category chips in Gmail iOS dark mode), renders fine in Gmail. No images, no JS.

### 8.6 Flash alerts (alerts.py) — owner decision D1

- During every poll (and bootstrap is exempt — no alert storm from the backlog): after store + enrichment, select never-alerted clusters (`alerted_at IS NULL` on all members) that are (a) tier-S, or (b) max fit ≥ 90. Tier-S alerts fire even pre-enrichment (company-based); fit alerts fire in the same run their enrichment lands.
- Send one short email per posting (subject `Radar ⚡ {Company} — {Title}`, body = the digest row partial + apply link), set `alerted_at` on all cluster members, insert alert_log.
- Cap: `FLASH_MAX_PER_DAY` (default 3) — overflow postings skip the alert (they're in tomorrow's digest; note the skip in the run report). `SEND_FLASH=false` disables. Alerted postings still appear in the next digest with an `⚡ alerted` badge — the digest stays the complete record.

## 9. Scheduling and state (GitHub Actions)

Private repo. SQLite state (`data/jobs.db`) is committed back after each run — simplest durable state with zero infra. Repo-bloat check: ~9 commits/day for a ~10-month project life stays comfortably inside GitHub limits; if it ever matters, a one-time history squash (orphan branch, force-push) resets it in minutes, and the deeper swap point is db.py (Turso/GCS). Do not design for either now.

Notes that must survive into the YAML comments: (1) pushes made with the built-in GITHUB_TOKEN do not trigger push-event workflows — that, not `[skip ci]`, is the recursion guard (keep `[skip ci]` anyway as insurance against a future PAT swap); (2) Actions cron routinely fires 5–30+ min late — delivery time is approximate by design; do not tighten the cron to chase latency, that's what flash alerts are for; (3) the bot's own commits keep the repo "active" for the 60-day scheduled-workflow auto-disable rule; if the schedule ever shows disabled after a long outage, re-enable in Settings → Actions; (4) scheduled workflows in a private repo bill Actions minutes (Free plan: 2,000/month; 9 runs/day × ~5 min ≈ 1,350/month) — `timeout-minutes: 30` bounds a wedged run, and if the budget pinches, drop the poll cron to every 4h.

### 9.1 .github/workflows/poll.yml

```yaml
name: poll
on:
  schedule: [{cron: "17 */3 * * *"}]   # offset minute: politeness + avoids the on-the-hour stampede
  workflow_dispatch: {}
permissions:
  contents: write                       # REQUIRED: default token is read-only; push 403s without this
concurrency: {group: radar, cancel-in-progress: false}   # literal 'radar' — MUST match digest.yml; serializes all writers of data/jobs.db
jobs:
  poll:
    runs-on: ubuntu-latest
    timeout-minutes: 30
    steps:
      - uses: actions/checkout@v4       # fetch-depth 1 is fine for the rebase below
      - uses: astral-sh/setup-uv@v4     # verify latest major at build time
        with: {enable-cache: true}
      - run: uv sync --frozen
      - run: uv run radar poll
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
          RESEND_API_KEY: ${{ secrets.RESEND_API_KEY }}    # flash alerts send from poll runs
          DIGEST_TO: ${{ vars.DIGEST_TO }}
          OWNER_EMAIL: ${{ vars.OWNER_EMAIL }}
          GITHUB_TOKEN: ${{ github.token }}
      - name: persist state
        if: ${{ !cancelled() }}
        run: |
          git config user.name radar-bot
          git config user.email bot@users.noreply.github.com
          git add data/jobs.db
          git diff --cached --quiet && exit 0
          git commit -m "state: poll $(date -u +%FT%TZ) [skip ci]"
          for i in 1 2 3; do
            git push && exit 0
            git fetch origin "$GITHUB_REF_NAME"
            # binary sqlite can't merge; -X theirs during rebase keeps THIS runner's DB
            git rebase -X theirs "origin/$GITHUB_REF_NAME" || { git rebase --abort; exit 1; }
          done
          exit 1
```

Policy: with both workflows in one concurrency group, the only writer that can race is a manual push from the owner's machine; `-X theirs` means the runner's DB wins (postings self-heal next poll). Owner should not push data/jobs.db manually.

### 9.2 .github/workflows/digest.yml (written out — 'same shape' hid load-bearing details)

```yaml
name: digest
on:
  schedule: [{cron: "10 12 * * *"}]   # ≈8:10 AM EDT; accept the 1h winter drift or adjust in November
  workflow_dispatch: {}
permissions:
  contents: write
concurrency: {group: radar, cancel-in-progress: false}   # SAME literal group as poll.yml — load-bearing
jobs:
  digest:
    runs-on: ubuntu-latest
    timeout-minutes: 30
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v4
        with: {enable-cache: true}
      - run: uv sync --frozen
      - id: poll
        run: uv run radar poll
        continue-on-error: true        # a crashed poll must not suppress the morning email
        env: { ... same as poll.yml ... }
      - run: uv run radar digest       # nonzero iff the Resend send fails
        env: { ... }
      - name: persist state
        if: ${{ !cancelled() }}
        run: ...                       # identical to poll.yml persist step
      - name: propagate poll failure
        if: steps.poll.outcome == 'failure'
        run: exit 1                    # run shows red so Actions notifications fire
```

**Exit-code contract**: `radar poll` exits 0 whenever the run completed and state was written — including with some sources failed (recorded in runs.sources_failed, surfaced in the digest subject/footer); nonzero is reserved for infrastructure failure (DB unwritable, config unparseable, ALL sources failed). `radar digest` exits nonzero iff the send fails. Before rendering, the digest run performs a targeted enrichment pass over its own selection (small cap, 100) so unenriched rows in the email are the rare fallback — and unenriched non-tier-S rows may be held back from digested_at marking so they return ranked tomorrow.

### 9.3 Secrets / repo variables

| Name | Type | Purpose |
|---|---|---|
| ANTHROPIC_API_KEY | secret | enrichment |
| RESEND_API_KEY | secret | digest + flash send |
| DIGEST_TO | variable | recipient — MUST equal the Resend account signup email while sending from onboarding@resend.dev |
| OWNER_EMAIL | variable | User-Agent contact |
| SHEET_WEBAPP_URL | secret | Applied sheet's Apps Script /exec URL (§14 applied-sync; optional until provided) |
| SHEET_TOKEN | secret | Applied sheet's SHARED_TOKEN (§14; optional until provided) |

Actions' native failure notifications cover the "pipeline is broken" alert path (the digest subject-prefix covers "pipeline is degraded").

## 10. Testing and local dev

- Every adapter: ≥1 recorded fixture (real response trimmed) + parse test asserting the field mapping — including the Simplify **season filter** (a fixture containing Summer-2026 + Summer-2027 items keeps only 2027), the NUFT YAML shape, and the emergency README **HTML-table** fallback.
- Unit tests: normalize rules incl. the four title_norm hash vectors and the Citadel/Citadel-Securities alias regression; the §6.2 prefilter truth table; dedup (identity upsert-on-edit updates in place, cross-source cluster, same-source tracker dupes, fuzzy >92, cluster merge, repost); closed-detection guards (failed adapter skips closure; empty snapshot refused; two-strike; 304 skip; prefilter change never inactivates); grad-window flag logic (all/some/none/null); enrichment (mocked parse success, transport-failure→retry-next-run, attempts-exhausted→null row, stop_reason max_tokens); digest (cluster renders once + all member ids marked; never resend; closed badge; sort with null enrichments; row caps; Closing-soon repeats; subject failure prefix); flash alerts (dedupe, daily cap, bootstrap exemption).
- `uv run radar dry-run`: full pipeline against fixtures + temp DB, prints the would-be digest to stdout, zero network, zero LLM (mocked). Default local loop; must stay green in CI.
- `uv run radar poll --limit-sources greenhouse` for targeted live testing.
- CI: ruff + pytest on PRs (`ci.yml`).

## 11. Milestones

Reordered for time-to-backfill (most Summer 2027 quant apps are already open). Milestones gate enablement, not development.

**M0 — Scaffold.** pyproject + uv.lock + .python-version, layout, models (incl. fit_robotics), db + user_version migrations, http client, CLI stubs, greenhouse adapter + fixture test, dry-run prints parsed postings. Accept: `uv run radar dry-run` and `pytest` green locally.

**M1 — Ingest end-to-end (no email yet).** lever, ashby, icims (§5.9 — SIG's 47 live S27 postings ride on it), github_trackers (×6 repo configs: simplify, vanshb03, NUFT, zshah101, speedyapply ×2), hn, reddit (fail-soft) adapters; normalize/prefilter/dedup/clustering/closed+repost detection; probe_ats.py (with the §4.2 trap handling) run for the remaining `probe` rows; poll.yml live and committing state. Accept: two consecutive scheduled polls commit state; an immediate rerun reports 0 new; a deliberately-broken adapter doesn't stop the run, shows in sources_failed, and triggers no closures for its board.

**M2 — Triage + delivery (the backlog milestone).** enrich.py (structured outputs, queue, failure classes); digest + flash alerts + digest.yml live; `radar sync-applied` (§14) so the backlog arrives pre-triaged against the owner's Applied sheet; `radar bootstrap`: first full poll → enrich the entire backlog at BOOTSTRAP_MAX_LLM_CALLS → ranked backlog drains through normal digests tier-first over 2–3 mornings (~150 rows each), flash alerts suppressed during bootstrap. Accept: backlog digests received ranked on 2–3 consecutive mornings (inbox, not spam); an unchanged-board rerun makes 0 LLM calls; a flash alert fires for a (real or injected) tier-S posting; grad-date flag visible on ≥1 real posting; runs.llm_calls recorded.

**M3 — Coverage.** (Jane Street needs NO custom adapter — its public Greenhouse board works with §5.2's Employment-Type folding; resolved 2026-08-20.) Priority order: (1) Workday — NVIDIA, then Arrowstreet (2 live S27 quant intern postings, CXS pre-verified), then Boston Dynamics; (2) Eightfold **before ~October** (Millennium's 2027 campus drop) — covers Millennium + Netflix, and explicitly NOT Citadel (verified: no Citadel Eightfold tenants exist; Citadel-family stays tracker-only permanently); (3) Avature HTML (Two Sigma — 2 live S27 postings); (4) SmartRecruiters (InfiniteQuant, optionally Bosch/ServiceNow); (5) the rest of §5.10 as appetite allows (Gem/Groq, Workable/HF, OpenQuant, TWSQ, PEAK6 sitemap, Google Alerts RSS). Accept: fixtures + tests per adapter; a posting removed from a board flips active=0 after two consecutive absent snapshots.

**M4 — Quality of life + integrations (§14).** `radar backfill --days N` (re-render without resending marks); `radar export --since DATE` (AgenticApplications feed — JSONL, one canonical posting+enrichment object per line; formatter is one small function); optional embeddings-based dedup behind a flag, only if fuzzy dedup demonstrably misses cases. (`radar sync-applied` ships earlier, with M2 — see §14.)

## 12. Config reference

Dependencies (runtime): `httpx`, `pydantic`, `feedparser`, `jinja2`, `rapidfuzz`, `anthropic`, `openai` (owner-approved 2026-08-21 for the provider switch), `pyyaml`. Dev: `pytest`, `ruff`. Nothing else without asking.

| Env var | Default | Meaning |
|---|---|---|
| RADAR_DB | data/jobs.db | SQLite path |
| RADAR_MODEL | claude-haiku-4-5-20251001 | enrichment model (anthropic provider) |
| RADAR_LLM_PROVIDER | anthropic | `anthropic` or `openai`; auto-selects `openai` when only OPENAI_API_KEY is set |
| RADAR_OPENAI_MODEL | (current mini-tier model) | enrichment model (openai provider) |
| MAX_LLM_CALLS_PER_RUN | 300 | per-poll cost guard |
| BOOTSTRAP_MAX_LLM_CALLS | 2000 | one-time backlog cap (`radar bootstrap`) |
| SEND_EMPTY | true | send digest on 0-new days (heartbeat) |
| SEND_FLASH | true | flash alerts on/off |
| FLASH_MAX_PER_DAY | 3 | flash alert daily cap |
| DIGEST_MAX_ROWS | 150 | total row target per digest email |
| DIGEST_TO / OWNER_EMAIL | — | recipient / UA contact |
| ANTHROPIC_API_KEY / OPENAI_API_KEY / RESEND_API_KEY | — | secrets (one LLM key is enough) |

Budget: enrichment ~$0.20–0.60/day steady state (~$6–19/month), ~$8 one-time bootstrap; Resend free tier (100/day) is 100× headroom; Actions minutes ≈ 1,350/month at 5-min runs (fits the Free plan — measure at M1).

## 13. Explicitly deferred (do not build unless asked)

Instagram ingestion in any form; auto-apply; per-posting resume tailoring; web dashboard; multi-user; notification channels beyond email (flash alerts are email — in scope per D1); scraping of sites that require login or block bots (this is why Citadel/Reddit degrade to tracker-only/dropped rather than gaining workarounds). If a source resists polite polling, drop it and note it — the trackers provide the safety net. Known manual complement the system cannot cover: university mailing lists and Handshake — targeted outreach lands there first; radar is not exhaustive.

## 14. Owner-ecosystem integrations

The owner runs **Applied** (a sibling project — a browser extension that one-click logs applications to a Google Sheet through a token-gated Apps Script web app, and watches Gmail for status updates). The Sheet's `Applications` tab is the live source of truth for applied-state: columns Date, Company, Title, Location, Mode, Salary, Status (Applied/Phone screen/Interview/Offer/Rejected/Ghosted), Source, Notes, Link, RowID. Radar never applies to anything (§0) and never writes to the Sheet — both integrations are one-way reads/exports.

1. **Applied-state sync (`radar sync-applied`).** POST `{"token": SHEET_TOKEN, "action": "list"}` to `SHEET_WEBAPP_URL` (the Apps Script `/exec` endpoint — same call the extension's own "Sync from sheet" makes; response `{ok, rows:[{rowId, date, company, title, location, status, link, ...}]}` with dates as `yyyy-mm-dd`). Match each row to a radar cluster: by canonicalized `link` URL first (strip tracking params; match against every cluster member's url), else company_norm + fuzzy title (token_sort_ratio > 92). Set `applied_at` (= the sheet's Date) on all cluster members; store the sheet `status` in kv (`applied_status:{cluster}`) so the digest can distinguish. Effects: `applied` badge on digest rows; suppression from Closing-soon and repost re-surfacing; a `Rejected` status additionally suppresses the row from every section (it's over). Unmatched sheet rows are reported in the run footer once (postings radar never saw — coverage signal, not an error). Run automatically at the start of every digest run when the two secrets are configured; skip silently (with a footer note) when they aren't. `radar mark-applied <url>` stays as the manual one-off. Ship with M2 — it directly shapes the backlog digests.
2. **AgenticApplications export.** The owner's application-bot concept consumes radar's output as its feed. Contract: `radar export --since DATE --format jsonl` emitting one JSON object per posting-cluster: canonical posting fields + enrichment + flags (grad-date match, workauth, deadline, applied/alerted state, sheet status when known). The bot (or the human) applies; the application lands in the Sheet via the Applied extension; `sync-applied` closes the loop back into radar. Radar's job ends at the export.
