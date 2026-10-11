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
   - `config/profile.md` — rewrite completely: who you are, your strengths,
     your target tracks in priority order, your possible graduation dates.
     This text IS the scoring rubric; the LLM scores every posting against it.
   - `config/registry.yaml` — the company list. Keep it (it's live-verified) but
     re-tier it: `S` = your dream companies (instant alerts + top placement),
     `1`/`2` = priority order. Add/remove companies freely; run
     `uv run python scripts/probe_ats.py` to auto-resolve board tokens for
     additions.
   - `src/radar/digest.py` grad-flag dates + `src/radar/models.py` fit tracks —
     only if your grad dates / track names differ from the profile defaults.

3. **Secrets and variables** (repo → Settings → Secrets and variables → Actions,
   or `gh secret set` / `gh variable set`):
   - Secrets: `OPENAI_API_KEY` (or `ANTHROPIC_API_KEY`), `RESEND_API_KEY`
   - Variables: `DIGEST_TO` = your Resend signup email, `OWNER_EMAIL` = your
     email (goes in the polite User-Agent)

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
> (1) rewrite config/profile.md for me from my resume below, keeping the same
> structure (strengths, numbered target tracks, grad dates, the work-auth
> line); (2) re-tier config/registry.yaml to my preferences — S for companies
> I'd drop everything for; (3) if my graduation dates differ from the profile,
> update GRAD_CANDIDATES in src/radar/digest.py and the four fit tracks in
> src/radar/models.py + the §7 prompt in src/radar/enrich.py only if my tracks
> differ; (4) run `uv sync && uv run pytest` and `uv run radar dry-run` to
> verify; (5) walk me through step 3 above (secrets) — never ask me to paste
> key values into chat.

## Honest limitations

Citadel, DE Shaw and a few other firms bot-wall their sites — they arrive via
the community tracker repos (hours-level delay), and their invite-only events
only reach you by joining their talent communities. Reddit blocks GitHub's IPs
(expected, fail-soft). This is a radar, not an auto-applier — it will never
submit anything for you.
