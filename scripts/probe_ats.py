#!/usr/bin/env python3
"""Probe ATS endpoints for registry companies marked `ats: probe` (SPEC §4.2).

Manual helper, not part of the pipeline. For each probe company it generates
candidate board tokens, GETs the greenhouse/lever/ashby public endpoints at
<= 1 request/second TOTAL, and classifies results: a populated 200 beats an
empty 200 beats a 404. Handles the four live-verified traps:
  1. stale 200-with-empty-board (lever mistral/voleon/saronic, ashby vercel)
     — an empty 200 is only ever a "watch" entry, never an auto-confirmed hit;
  2. token collisions (greenhouse 'archer' = a veterinary clinic, not Archer
     Aviation) — hits must pass a company-identity sanity check;
  3. case-sensitive lever tokens ('MachinaLabs') — CamelCase candidates;
  4. unguessable tokens — if a row carries a careers_url, sniff its HTML for
     boards.greenhouse.io/{t} | jobs.lever.co/{t} | jobs.ashbyhq.com/{t}.

Hits are written back into registry.yaml by line-editing the matching
'- {name: ...' row (preserves file comments; a YAML round-trip would not).

Usage:
    python scripts/probe_ats.py [--registry config/registry.yaml]
                                [--dry-run] [--only NAME ...]
"""

from __future__ import annotations

import argparse
import datetime
import os
import re
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REGISTRY = REPO_ROOT / "config" / "registry.yaml"

ENDPOINTS = {
    "greenhouse": "https://boards-api.greenhouse.io/v1/boards/{t}/jobs",
    "lever": "https://api.lever.co/v0/postings/{t}?mode=json",
    "ashby": "https://api.ashbyhq.com/posting-api/job-board/{t}",
}
ATS_ORDER = ("greenhouse", "lever", "ashby")
MAX_CANDIDATES = 8  # bounds requests per company (x3 endpoints)

# Words that carry no identity signal on their own (legal suffixes + the
# finance/tech filler that collides across firms).
_GENERIC_WORDS = frozenset(
    "the inc llc ltd llp lp plc corp co company group holdings trading capital "
    "partners management asset securities markets technologies labs global".split()
)

_SNIFF_RE = re.compile(
    r"(?:boards|job-boards)\.greenhouse\.io/(?:embed/job_board\?(?:[^\"'\s>]*&)?for=)?"
    r"([A-Za-z0-9._%-]+)"
    r"|jobs\.lever\.co/([A-Za-z0-9._%-]+)"
    r"|jobs\.ashbyhq\.com/([A-Za-z0-9._%-]+)"
)
_SNIFF_JUNK = frozenset({"embed", "v1", "js"})


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", unicodedata.normalize("NFKC", s).lower())


def _name_words(name: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9]+", unicodedata.normalize("NFKC", name))


def distinctive_tokens(name: str) -> list[str]:
    words = [w.lower() for w in _name_words(name)]
    return [w for w in words if w not in _GENERIC_WORDS] or words


def candidate_tokens(name: str) -> list[str]:
    """Ordered, deduped token guesses: joined/hyphenated/CamelCase over the
    full name and its distinctive words, plus first-word and initials forms."""
    lower = [w.lower() for w in _name_words(name)]
    distinct = distinctive_tokens(name)
    cands: list[str] = []

    def add(t: str) -> None:
        if t and t not in cands:
            cands.append(t)

    for words in (lower, distinct):
        add("".join(words))
        add("-".join(words))
        add("".join(w.capitalize() for w in words))  # lever tokens are case-sensitive
    if len(lower) > 1:
        add(lower[0])  # first-word shorthand ('radix', 'optiver')
        add("".join(w[0] for w in lower))  # initials ('drw', 'sig')
    return cands[:MAX_CANDIDATES]


def extract_jobs(ats: str, payload) -> list | None:
    """Return the jobs list if the payload plausibly is a job board, else None."""
    if ats == "lever":
        return payload if isinstance(payload, list) else None
    if isinstance(payload, dict) and isinstance(payload.get("jobs"), list):
        return payload["jobs"]
    return None


_PAYLOAD_NAME_KEYS = ("name", "title", "company", "companyName", "board", "organizationName")
_JOB_EVIDENCE_KEYS = (
    "company_name",
    "company",
    "companyName",
    "absolute_url",
    "hostedUrl",
    "jobUrl",
    "applyUrl",
)


def identity_ok(company_name: str, ats: str, payload) -> bool:
    """Sanity check a populated hit really belongs to this company (trap 2).

    Every distinctive name token must appear in the payload's company/board
    fields or a sampled job's absolute_url/hostedUrl/jobUrl. Requiring ALL
    tokens (not any) is what rejects collisions: for 'Archer Aviation' the
    'archer' veterinary board echoes the slug in every URL but never says
    'aviation'. Single-token names still pass via the URL slug.
    """
    evidence: list[str] = []
    if isinstance(payload, dict):
        for key in _PAYLOAD_NAME_KEYS:
            if isinstance(payload.get(key), str):
                evidence.append(payload[key])
    jobs = extract_jobs(ats, payload) or []
    for job in jobs[:5]:
        if not isinstance(job, dict):
            continue
        for key in _JOB_EVIDENCE_KEYS:
            if isinstance(job.get(key), str):
                evidence.append(job[key])
    blob = _norm(" ".join(evidence))
    return all(tok in blob for tok in distinctive_tokens(company_name))


class Throttle:
    """One global rate limit across all hosts (SPEC §4.2: <= 1 req/sec total)."""

    def __init__(self, interval: float = 1.0):
        self.interval = interval
        self._last = 0.0

    def wait(self) -> None:
        delay = self._last + self.interval - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        self._last = time.monotonic()


@dataclass
class ProbeResult:
    ats: str
    token: str
    status: str  # 'populated' | 'empty' | 'miss'
    n_jobs: int = 0
    identity: bool = False


@dataclass
class Outcome:
    name: str
    hit: ProbeResult | None = None
    watch: list[ProbeResult] = field(default_factory=list)  # empty 200s
    suspect: list[ProbeResult] = field(default_factory=list)  # populated, identity failed


class Prober:
    def __init__(self, client, throttle: Throttle | None = None):
        self._client = client
        self._throttle = throttle or Throttle()

    def _get(self, url: str):
        self._throttle.wait()
        return self._client.get(url)

    def probe_endpoint(self, name: str, ats: str, token: str) -> ProbeResult:
        url = ENDPOINTS[ats].format(t=token)
        try:
            resp = self._get(url)
        except httpx.HTTPError:
            return ProbeResult(ats, token, "miss")
        if resp.status_code != 200:
            return ProbeResult(ats, token, "miss")
        try:
            payload = resp.json()
        except ValueError:
            return ProbeResult(ats, token, "miss")
        jobs = extract_jobs(ats, payload)
        if jobs is None:
            return ProbeResult(ats, token, "miss")
        if not jobs:
            return ProbeResult(ats, token, "empty")
        return ProbeResult(
            ats, token, "populated", n_jobs=len(jobs), identity=identity_ok(name, ats, payload)
        )

    def sniff_careers_page(self, careers_url: str) -> list[tuple[str, str]]:
        try:
            resp = self._get(careers_url)
        except httpx.HTTPError:
            return []
        if resp.status_code != 200:
            return []
        found: list[tuple[str, str]] = []
        for gh, lv, ab in _SNIFF_RE.findall(resp.text):
            for ats, token in (("greenhouse", gh), ("lever", lv), ("ashby", ab)):
                if token and token not in _SNIFF_JUNK and (ats, token) not in found:
                    found.append((ats, token))
        return found

    def probe_company(self, company: dict) -> Outcome:
        name = str(company["name"])
        out = Outcome(name)
        tried: set[tuple[str, str]] = set()
        for token in candidate_tokens(name):
            for ats in ATS_ORDER:
                tried.add((ats, token))
                res = self.probe_endpoint(name, ats, token)
                if res.status == "populated":
                    if res.identity:
                        out.hit = res
                        return out
                    out.suspect.append(res)
                elif res.status == "empty":
                    out.watch.append(res)
        careers_url = company.get("careers_url")
        if careers_url:
            for ats, token in self.sniff_careers_page(str(careers_url)):
                if (ats, token) in tried:
                    continue
                res = self.probe_endpoint(name, ats, token)
                if res.status == "populated":
                    # DECISION: a token embedded in the company's own careers
                    # page IS the identity evidence — accept even if the
                    # payload text check can't confirm the name.
                    res.identity = True
                    out.hit = res
                    return out
                if res.status == "empty":
                    out.watch.append(res)
        return out


def edit_registry_line(text: str, name: str, ats: str, token: str, date: str) -> tuple[str, bool]:
    """Rewrite the '- {name: ..., ats: probe}' row in place, keeping the rest
    of the line (and every other line/comment in the file) untouched."""
    pattern = re.compile(
        r"^(?P<pre>\s*-\s*\{\s*name:\s*\"?"
        + re.escape(name)
        + r"\"?\s*,[^}]*?)ats:\s*probe(?P<post>\s*\})(?P<rest>.*)$",
        re.MULTILINE,
    )
    repl = rf"\g<pre>ats: {ats}, token: {token}\g<post>\g<rest>   # probed {date}"
    new_text, n = pattern.subn(repl, text, count=1)
    return new_text, n == 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--dry-run", action="store_true", help="print results without writing")
    parser.add_argument("--only", nargs="+", default=None, metavar="NAME")
    args = parser.parse_args(argv)

    registry = yaml.safe_load(args.registry.read_text(encoding="utf-8"))
    companies = [c for c in registry["companies"] if c.get("ats") == "probe"]
    if args.only:
        wanted = {n.lower() for n in args.only}
        companies = [c for c in companies if str(c["name"]).lower() in wanted]
    if not companies:
        print("nothing to probe")
        return 0

    email = os.environ.get("OWNER_EMAIL", "unset")
    ua = f"internship-radar/0.1 (ats probe; contact {email})"
    date = datetime.date.today().isoformat()
    hits: list[tuple[str, ProbeResult]] = []
    with httpx.Client(headers={"User-Agent": ua}, timeout=30.0, follow_redirects=True) as client:
        prober = Prober(client)
        for company in companies:
            out = prober.probe_company(company)
            if out.hit:
                hits.append((out.name, out.hit))
                print(f"HIT   {out.name}: {out.hit.ats}/{out.hit.token} ({out.hit.n_jobs} jobs)")
            else:
                print(f"MISS  {out.name}: stays ats: probe")
                for r in out.suspect:
                    print(f"      suspect (identity failed): {r.ats}/{r.token} ({r.n_jobs} jobs)")
                for r in out.watch:
                    print(f"      watch (empty 200): {r.ats}/{r.token}")

    if not hits:
        return 0
    text = args.registry.read_text(encoding="utf-8")
    for name, hit in hits:
        text, ok = edit_registry_line(text, name, hit.ats, hit.token, date)
        if not ok:
            print(f"WARN  could not line-edit registry row for {name}", file=sys.stderr)
    if args.dry_run:
        print("--dry-run: registry not written; would write:")
        for name, hit in hits:
            print(f"  {name}: ats: {hit.ats}, token: {hit.token}  # probed {date}")
    else:
        args.registry.write_text(text, encoding="utf-8")
        print(f"wrote {len(hits)} hit(s) to {args.registry}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
