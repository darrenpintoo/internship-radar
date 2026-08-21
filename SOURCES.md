# Source research — verified 2026-08-20

Every candidate source for internship-radar, live-verified on 2026-08-20 (7 parallel researchers, ~270 live fetches). Use this to diff against other research: if a source isn't listed, it wasn't found; if it's listed as rejected, the reason and evidence are recorded so a second opinion can challenge them.

**Verdicts**: `v1` = wired into SPEC.md now · `M3` = verified/feasible, scheduled later · `rejected` = evaluated with evidence, not worth a slot · `impossible` = blocked/login-gated/dead (would need workarounds SPEC §0 forbids).

## Adopted for v1

### Tracker repos (github_trackers adapter, §5.5)

| Repo | Data surface | Why |
|---|---|---|
| SimplifyJobs/Summer2027-Internships | listings.json (dev branch), 10.8MB, all seasons mixed — filter `terms="Summer 2027"` | Primary; 693 S27 items, updated multiple times daily; `sponsorship` field pre-fills workauth |
| vanshb03/Summer2027-Internships | listings.json (dev), schema variant (`season` field) | Cheap secondary |
| northwesternfintech/2027QuantInternships | YAML per company under ./data | Best quant-specific list; covers firms with no public boards |
| **zshah101/Automated-List-Of-Summer-2027…** | data/jobs.json (dict, is_open) + docs/api/jobs.json (active mirror), 30-min cadence | **Best new find**: scrapes ~4,300 boards incl. Workday/Oracle/SmartRecruiters/Workable/amazon.jobs — a free proxy for the whole pre-M3 gap. 125 open S27 today. Bonus: data/companies.json (probe input), docs/api/radar.json (predicted app-window openings) |
| speedyapply/2027-SWE-College-Jobs + 2027-AI-College-Jobs | README markdown pipe tables between `TABLE_*_START/END` sentinels | Direct FAANG+/Quant links (Citadel, HRT, NVIDIA-Workday, TikTok) + salary column; daily 12:00 UTC; one adapter, two configs |

### ATS platform APIs

| Platform | Endpoint | Companies unlocked | Status |
|---|---|---|---|
| Greenhouse / Lever / Ashby | (spec §5.2–5.4) | ~75 registry boards | v1 (already specced) |
| **iCIMS** | `https://{host}/api/jobs?page=N&limit=100` — keyless JSON, datacenter-verified | **SIG (47 "Summer 2027" postings live TODAY)**, AMD; Arm later | **v1 (§5.9)** — was wrongly tracker-only |
| **Jane Street via Greenhouse** (found 2026-08-20, post-research) | `boards-api.greenhouse.io/v1/boards/janestreet/jobs` — the public board WORKS; JS strips 'Intern' from titles, campus flag lives in `Employment Type` metadata ('Summer Internship' etc.) | **Jane Street: 50 campus roles live** (42 Summer Internship) — was wrongly 'custom, M3' | **v1** — greenhouse adapter folds the metadata into the title; this is also why title-keyword trackers (zshah101) show zero JS internships |

### New registry companies with live 2027/intern postings (tokens verified)

Quant: AQR (`aqr`, 10× 2027 Summer Analyst), Walleye (`walleyecapital-external-students`, 3× S27), TransMarket (`transmarketgroup`), Maven (`mavensecuritiesholdingltd`), Xantium (`xantium` — not on any tracker yet), Voloridge, BlackEdge, Stevens Capital (`scm`), Seven Research, GSA, Ellipsis Labs (ashby), + watch boards: WorldQuant, Schonfeld, Geneva, Valkyrie (lever), 3Red, Vatic, Voleon (ashby — NOT its stale lever board), Tudor, Marshall Wace (empty board).
AI-infra: **Etched (`etched`, 17 intern roles live — tier bumped to 1)**, Deepgram (S27 SWE internship live), + verified-token watch boards: xAI, CoreWeave (incl. W&B), Lambda, Tenstorrent, SambaNova (`sambanovasystems`), d-Matrix, Fireworks, Baseten, Mistral (`mistral.ai`), Cohere, Perplexity, Cursor, Vercel, Nebius, Crusoe, Astera Labs.
Robotics: **Zipline (`flyzipline`, 41× 2027 intern titles)**, **Astranis (19 interns, 6× 2027)**, Applied Intuition (`applied` — RL/robotics research interns), Rocket Lab (36 intern reqs), Machina Labs (`MachinaLabs`, 3 interns), Field AI (`field-ai`, 5), Skydio (5), Motional (2), Apptronik, Nuro, Shield AI (2027 co-op), Figure (`figureai`), Physical Intelligence (evergreen research internships), + watch: Zoox (Sept-Oct drop), Aurora (`aurora-operations-inc`), Saronic, Neros (`nerostechnologies`), Hadrian (`hadrian-automation`), Gecko (Pittsburgh), Agility, Kodiak, Serve, 1X, Waabi.

## M3 pipeline (verified feasible, scheduled)

| Source | Mechanics | Trigger |
|---|---|---|
| Workday CXS | POST per-tenant | NVIDIA → **Arrowstreet (2× S27 QD/QR live, pre-verified)** → Boston Dynamics → Marvell |
| Eightfold API | `GET /api/apply/v2/jobs?domain=…` keyless | **Build before ~Oct** — Millennium campus drop; + Netflix. **NOT Citadel** (verified: no Eightfold tenants; their sites 403) |
| Avature (Two Sigma) | server-rendered HTML, `JobDetail/{slug}/{id}` anchors | 2× S27 intern live (QR, AI Research) |
| SmartRecruiters | `GET api.smartrecruiters.com/v1/companies/{id}/postings` keyless | InfiniteQuant (2× S27 QR/QD live); Bosch, ServiceNow optional |
| Gem GraphQL | POST jobs.gem.com/api/public/graphql | Groq (board empty today) |
| Workable | GET widget API | Hugging Face (0 interns today) |
| OpenQuant | `__NEXT_DATA__` on openquant.co/?level=Internship, direct ATS links | Quant discovery (surfaced Xantium/Stevens/Balyasny before trackers) |
| The Wall Street Quants | HTML, 65× "Summer 2027" cards | Redundancy for no-API firms (DE Shaw, Bridgewater, PEAK6) |
| PEAK6 | sitemap.xml diff | 2× S27 trading internships live (women+-focused flagship) |
| Google Alerts RSS | 3-5 site:-scoped alerts → Atom feeds (owner creates once) | $0 long-tail net; days of latency — coverage only, never flash alerts |
| Tracker watch-list | zapplyjobs (15-min cadence, README-only), Emjumaev FAANG (in-house APIs, 7★ risk), ApplyGuy, aprameyak | If coverage gaps appear |

## Rejected — with reasons (challenge these if other research disagrees)

- **hiring.cafe** — API now 401/405; Cloudflare blocks datacenter IPs (documented by scrapers). Impossible politely.
- **jobright.ai** — fresh (18-min-old listings in SEO shell) but all links funnel to their login, search filters ignored anonymously. Login-gated.
- **wellfound/AngelList** — PerimeterX bot-wall + login-gated filters; anonymous "intern" page renders senior roles.
- **Y Combinator Work at a Startup** — embedded Algolia key returns 0 hits logged-out (deliberately gated).
- **untapped.io** — login-only app; public page is an empty JS shell.
- **intern-list.com** — Airtable embed; polling would require spoofing Airtable internals (forbidden).
- **Simplify's own site API** — no public posting endpoint (verified: list metadata only, posting paths 404). The GitHub repo IS their machine surface.
- **cscareers.dev** — dead (API host has no DNS; page 500s; org archived 2020-2022).
- **levels.fyi** — internshipData.json is polite+public but it's a salary dataset: 9 rows for 2027, zero apply links.
- **aijobs.net** — public API + RSS retired (404 verified). **RemoteML** — domain parked. **MLOps.community jobs** — 403. **latent.space / GPU MODE** — Discord-only. **MLH Fellowship** — a program, not a board (manual-apply item).
- **QuantNet, Wall Street Oasis, eFinancialCareers** — Cloudflare/bot-walls (403/405 even residentially). **quantfinancejobs.com** — domain now a UK accountancy firm.
- **ROS Discourse jobs** — perfect JSON/RSS mechanics, wrong content (senior/international; ~0 US intern posts). **IEEE JobSite** — 403 bot-wall. **robotics.jobs** — NXDOMAIN. **Robohub jobs feed** — 200 but zero items (dormant). **ClearanceJobs** — login + clearance-gated.
- **Adzuna / Jooble / Careerjet** — scraped meta-aggregators: lagging copies of ATS data radar polls first-hand, redirect-wrapped links, key management for nothing.
- **The Muse API** — genuinely open (710 SWE-internship rows, some S27) but client-limited coverage and no date sort; M3 pilot at most.
- **USAJOBS API** — polite and free but federal/citizen-only, off all four tracks.
- **Google News RSS** — works, but indexes articles not postings (1/47 items even loosely relevant).
- **HN Algolia keyword search** — 7 all-time hits for "summer 2027 intern", none a company posting.
- **Search APIs** — Bing: retired Aug 2025. Google Custom Search: closed to new customers (sunset Jan 2027). Brave: free tier eliminated ~Feb 2026, ~$7-11/mo for this use. Google Alerts RSS wins at $0.
- **Newsletter bridges (Kill the Newsletter + SWE List)** — bridge works; newsletter content is a slower duplicate of trackers already polled 3-hourly.
- **Citadel / Citadel Securities direct** — 403 to honest requests even residentially; NOT on Eightfold (NXDOMAIN); no public API. Tracker-only permanently (Simplify carries their live S27 rows).
- **Derivative trackers** — SuryaHarikrishnan (self-declared Simplify+vanshb03 subset), DereC4/internatlas (re-aggregates radar's own sources), dreamworkhq (685/685 links via own tracking domain), workopia (broken data file, SEO filler), jobright-ai repos (no 2027 season; affiliate funnels), sndsh404 (manual, stale 17 days), awesome-lists (all stale).
- **Quant firms with no pollable surface** — Balyasny (Salesforce site; OpenQuant/trackers cover), Group One (hCaptcha form), Quantlab (Jobvite client-rendered), Trexquant (JS shell), G-Research (custom UK), Campbell (ADP WFN), Renaissance (page fine, zero intern roles ever), Seven Points/Seven Eight (no ATS), Da Vinci (EU), Gelber/Mako (no interns), Ansatz ("No intern program at this time" — literally the posting title), HAP (empty board).
- **Robotics/defense skips** — Joby + Archer (iCIMS/unresolvable token — note greenhouse `archer` is a VET CLINIC), Castelion (Manatal, ITAR-heavy), Firehawk (AppOne), Gatik (archived site), Overland AI (Rippling, no public API), Ambi/Carbon/Diligent/Outrider/Path/Torc/Dexterity/Cobot (verified boards, zero interns — tokens recorded here if ever wanted).
- **Rippling ATS** — public API exists (`api.rippling.com/platform/api/ats/v1/board/{board}/jobs`, verified; Rippling itself has Winter-2027 SWE/ML interns) but no track-relevant tenant beyond Rippling itself; one row per job×location (dedupe by UUID). M3 fringe.
- **SmartRecruiters/Workable/Recruitee/Teamtailor/Breezy/Jobvite/Personio/Pinpoint/Dover** as platforms — open patterns exist but no relevant tenants beyond those already listed.

## probe_ats.py traps (all found live — encoded in SPEC §4.2)

1. Stale 200-with-empty-array boards: lever `mistral`/`voleon`/`saronic`, ashby `vercel`/`figure` — a populated board on another ATS must win.
2. Token collisions: greenhouse `archer` = veterinary clinic, not Archer Aviation — identity-check every hit.
3. Case-sensitive Lever tokens: `MachinaLabs` works, `machinalabs` 404s.
4. Unguessable tokens (`applied`, `wehrtyou`, `flyzipline`, `aurora-operations-inc`, `hadrian-automation`, `field-ai`) — sniff careers-page HTML for board embeds before giving up.
