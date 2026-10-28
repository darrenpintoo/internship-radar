# internship-radar

A single-user internship radar. Every 3 hours it polls ~215 company job boards
(official Greenhouse/Lever/Ashby/iCIMS APIs — no scraping) plus 7 GitHub
tracker repos and HN, dedupes across sources, scores every new posting against
YOUR profile with an LLM (~$0.004/posting), and emails you a ranked digest
every morning — plus instant alerts for top-target postings. Runs entirely on
GitHub Actions; state is one SQLite file committed back to the repo. Zero
servers, roughly $5–15/month in LLM calls, free everything else.

Design doc: [SPEC.md](SPEC.md). Source research with evidence: [SOURCES.md](SOURCES.md).

## Set up your own copy (~20 minutes)

You need: a GitHub account, an OpenAI **or** Anthropic API key, and a free
[Resend](https://resend.com) account **signed up with the email you want the
digests sent to** (the free sender only delivers to the account's own address).

1. **Create your private repo from this code** (never run someone else's copy —
   the DB and digests are personal):
   ```bash
   cd internship-radar
   git init -b main && git add -A && git commit -m "init"
   gh repo create YOURNAME/internship-radar --private --source=. --push
   ```

2. **Personalize — the three files that make it YOURS** (this is the part to
   hand to your AI assistant along with this repo):
   - your profile — who you are, your strengths, your target tracks in
     priority order, your possible graduation dates. This text IS the scoring
     rubric; the LLM scores every posting against it. The tracked
     `config/profile.md` is a public example: put your real profile in
     `config/profile.local.md` (gitignored) for local runs and in the
     `RADAR_PROFILE` Actions secret for CI — both override the example, so
     your personal details never land in the repo.
   - `config/registry.yaml` — the company list. Keep it (it's live-verified) but
     re-tier it: `S` = your dream companies (instant alerts + top placement),
     `1`/`2` = priority order. Add/remove companies freely; run
     `uv run python scripts/probe_ats.py` to auto-resolve board tokens for
     additions.
   - grad-flag dates: set the `RADAR_GRAD_DATES` repo variable (comma-separated
     `YYYY-MM`, e.g. `2028-05,2028-12`); `src/radar/models.py` fit tracks —
     only if your track names differ from the defaults.

3. **Secrets and variables** (repo → Settings → Secrets and variables → Actions,
   or `gh secret set` / `gh variable set`):
   - Secrets: `OPENAI_API_KEY` (or `ANTHROPIC_API_KEY`), `RESEND_API_KEY`,
     `RADAR_PROFILE` = your real profile text (keeps it out of the public repo)
   - Variables: `DIGEST_TO` = your Resend signup email, `OWNER_EMAIL` = your
     email (goes in the polite User-Agent), `RADAR_GRAD_DATES` = your possible
     graduation dates (`YYYY-MM`, comma-separated)

4. **First run**: Actions tab → `bootstrap` → Run workflow. It ingests
   everything (~2,000 postings), scores the backlog (one-time ~$5–10), and the
   daily 8 AM ET digest cron takes over from there. Check spam for the first
   email and mark it "not spam".

## Daily behavior

- `poll` every 3h: fetch → dedup → score new postings → flash-alert if a tier-S
  company or a ≥90 fit appears (max 3 emails/day, `SEND_FLASH=false` to disable).
- `digest` every morning: everything new since yesterday, ranked, with
  grad-date flags ("⚠ use 2029-05" on sophomore-gated programs), work-auth
  warnings, deadlines, and a Closing-soon section. Overflow rolls to tomorrow.
- All knobs are env vars — see SPEC §12 (`RADAR_LLM_PROVIDER`, caps, row limits).

## For your AI assistant

Paste this with the repo:

> This is internship-radar (see SPEC.md — the full design doc). Don't rebuild
> anything; the endpoints and tokens are live-verified. Do the personalization:
> (1) write config/profile.local.md for me from my resume below, keeping the
> same structure as config/profile.md (strengths, numbered target tracks, grad
> dates, the work-auth line) — it's gitignored; also set it as the
> RADAR_PROFILE Actions secret; (2) re-tier config/registry.yaml to my
> preferences — S for companies I'd drop everything for; (3) set the
> RADAR_GRAD_DATES repo variable to my graduation dates, and touch the four
> fit tracks in src/radar/models.py + the §7 prompt in src/radar/enrich.py
> only if my tracks differ; (4) run `uv sync && uv run pytest` and `uv run radar dry-run` to
> verify; (5) walk me through step 3 above (secrets) — never ask me to paste
> key values into chat.

## Targeting new-grad roles instead of internships

Set the repo variable `RADAR_TARGET=newgrad`. The prefilter switches to
entry-level titles (new grad / graduate engineer / entry-level...) and the
tracker sources switch to the new-grad repos (SimplifyJobs/New-Grad-Positions,
vanshb03/New-Grad-2027, speedyapply NEW_GRAD_USA). Rewrite your profile
accordingly ("seeking full-time new-grad roles").

## Filtering knobs (repo variables)

- `RADAR_WORKAUTH_OK` — comma list of work-auth statuses that are FINE for you;
  the digest warns only about the rest. US citizen:
  `citizen_only,no_sponsorship,sponsors_ok`. Needs sponsorship: leave the
  default (`sponsors_ok`). Also state your status in your profile so the
  scorer stops flagging it.
- `DIGEST_MIN_FIT` — hide scored postings below this fit (e.g. `40`).
- `DIGEST_HIDE_CATEGORIES` — e.g. `other` to drop off-track roles entirely.

## No OpenAI/Anthropic key? OpenRouter works

Set `RADAR_LLM_PROVIDER=openrouter`, secret `OPENROUTER_API_KEY`, and variable
`RADAR_OPENROUTER_MODEL`. Caveats: pick a model that supports structured
outputs (json_schema); OpenRouter's *free*-tier models are capped around
50 requests/day — fine for daily upkeep (~10-50 scorings/day) but the one-time
~1,500-posting bootstrap would take weeks, so either buy ~$5 of OpenRouter
credits for the bootstrap or accept a slow ramp-up. A paid mini-tier model
(e.g. `openai/gpt-4.1-mini` via OpenRouter) costs the same ~$5-15/month as
going direct.

## Honest limitations

Citadel, DE Shaw and a few other firms bot-wall their sites — they arrive via
the community tracker repos (hours-level delay), and their invite-only events
only reach you by joining their talent communities. Reddit blocks GitHub's IPs
(expected, fail-soft). This is a radar, not an auto-applier — it will never
submit anything for you.
