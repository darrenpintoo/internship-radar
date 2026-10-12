"""GitHub tracker-repo adapter (SPEC §5.5) — verified live 2026-08-20.

One adapter, four parse modes over six repo configs:
- 'simplify'    SimplifyJobs + vanshb03 listings.json (flat array; vanshb03 uses
                a `season` field instead of `terms` — branch on which exists)
- 'zshah'       zshah101 data/jobs.json (dict keyed 'source:company_slug:jobid';
                docs/api/jobs.json mirror is a {"jobs": [...]} active-only shape)
- 'nuft_yaml'   northwesternfintech/2027QuantInternships — ONE codeload tarball
                per poll, data/*.yaml read in memory (never ~100 raw fetches)
- 'speedyapply' README markdown pipe tables between sentinel comments

DECISION: ETag conditional requests (Http already persists validators in kv)
replace the spec's commit-SHA-in-kv diffing — adapters never touch the DB, and
a 304 yields the same "snapshot unchanged" signal as an unchanged SHA.
"""

from __future__ import annotations

import hashlib
import html as html_mod
import io
import re
import tarfile

import yaml

from .. import http as http_mod
from ..models import Posting
from ..normalize import iso_from_epoch_s, iso_norm
from .base import RawItem

_RAW = "https://raw.githubusercontent.com"

DEFAULT_REPOS: list[dict] = [
    {
        "repo": "SimplifyJobs/Summer2027-Internships",
        "mode": "simplify",
        "url": f"{_RAW}/SimplifyJobs/Summer2027-Internships/dev/.github/scripts/listings.json",
    },
    {
        "repo": "vanshb03/Summer2027-Internships",
        "mode": "simplify",
        "url": f"{_RAW}/vanshb03/Summer2027-Internships/dev/.github/scripts/listings.json",
    },
    {
        "repo": "zshah101/Automated-List-Of-Summer-2027-and-Fall-2026-Tech-Internships",
        "mode": "zshah",
        "url": f"{_RAW}/zshah101/Automated-List-Of-Summer-2027-and-Fall-2026-Tech-Internships"
        "/main/data/jobs.json",
        # Active-only mirror; single-maintainer repo, so fail-soft (SPEC §5.5).
        "fallback_url": f"{_RAW}/zshah101"
        "/Automated-List-Of-Summer-2027-and-Fall-2026-Tech-Internships/main/docs/api/jobs.json",
    },
    {
        "repo": "northwesternfintech/2027QuantInternships",
        "mode": "nuft_yaml",
        "url": "https://codeload.github.com/northwesternfintech/2027QuantInternships"
        "/tar.gz/refs/heads/main",
    },
    {
        # Early-talent/sophomore programs (Launch/Explore/FutureFocus...) —
        # markdown-link pipe tables: | [Name](url) | Status | Year | Note |
        "repo": "zapplyjobs/underclassmen-internships",
        "mode": "zapply_md",
        "url": f"{_RAW}/zapplyjobs/underclassmen-internships/main/README.md",
    },
    {
        "repo": "speedyapply/2027-SWE-College-Jobs",
        "mode": "speedyapply",
        "url": f"{_RAW}/speedyapply/2027-SWE-College-Jobs/main/README.md",
    },
    {
        "repo": "speedyapply/2027-AI-College-Jobs",
        "mode": "speedyapply",
        "url": f"{_RAW}/speedyapply/2027-AI-College-Jobs/main/README.md",
    },
]

# New-grad sources (verified live 2026-10-11): same schemas as the internship
# repos, but no Summer-2027 season concept — season_filter: False bypasses it.
NEWGRAD_REPOS: list[dict] = [
    {
        "repo": "SimplifyJobs/New-Grad-Positions",
        "mode": "simplify",
        "season_filter": False,
        "url": f"{_RAW}/SimplifyJobs/New-Grad-Positions/dev/.github/scripts/listings.json",
    },
    {
        "repo": "vanshb03/New-Grad-2027",
        "mode": "simplify",
        "season_filter": False,
        "url": f"{_RAW}/vanshb03/New-Grad-2027/dev/.github/scripts/listings.json",
    },
    {
        "repo": "speedyapply/2027-SWE-College-Jobs#newgrad",
        "mode": "speedyapply",
        "url": f"{_RAW}/speedyapply/2027-SWE-College-Jobs/main/NEW_GRAD_USA.md",
    },
]

# Sentinel spelling verified live 2026-08-20 in both speedyapply READMEs.
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_SPEEDY_TABLES = ("TABLE_FAANG", "TABLE_QUANT", "TABLE")
_STRONG_RE = re.compile(r"<strong>(.*?)</strong>")
_HREF_RE = re.compile(r'href="([^"]+)"')


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _hints(item: dict) -> dict[str, str]:
    hints: dict[str, str] = {}
    if item.get("sponsorship"):
        hints["sponsorship"] = item["sponsorship"]
    if item.get("category"):
        hints["category"] = item["category"]
    if item.get("degrees"):
        hints["degrees"] = ", ".join(item["degrees"])
    return hints


class GithubTrackersAdapter:
    name = "github_trackers"
    prefilter_policy = "excludes_only"

    def __init__(self, http, repos: list[dict] | None = None, target: str = "internship"):
        self._http = http
        if repos is None:
            repos = NEWGRAD_REPOS if target == "newgrad" else DEFAULT_REPOS
        self._repos = repos

    def fetch(self) -> list[RawItem]:
        items: list[RawItem] = []
        for cfg in self._repos:
            meta = {
                "mode": cfg["mode"],
                "repo": cfg["repo"],
                "season_filter": cfg.get("season_filter", True),
            }
            try:
                payload = self._fetch_one(cfg)
                items.append(RawItem(self.name, cfg["repo"], payload=payload, meta=meta))
            except http_mod.NotModified:
                items.append(RawItem(self.name, cfg["repo"], not_modified=True, meta=meta))
            except Exception as exc:  # one repo's failure never kills the rest
                items.append(
                    RawItem(self.name, cfg["repo"], error=f"{type(exc).__name__}: {exc}", meta=meta)
                )
        return items

    def _fetch_one(self, cfg: dict):
        mode, url = cfg["mode"], cfg["url"]
        if mode in ("simplify", "zshah"):
            # Simplify's listings.json is ~11 MB — use the large-board timeout.
            timeout = (
                http_mod.LARGE_BOARD_TIMEOUT if mode == "simplify" else http_mod.DEFAULT_TIMEOUT
            )
            try:
                return self._http.get_json(url, timeout=timeout)
            except http_mod.NotModified:
                raise
            except Exception:
                if cfg.get("fallback_url"):
                    return self._http.get_json(cfg["fallback_url"])
                raise
        resp = self._http.get(url, timeout=http_mod.LARGE_BOARD_TIMEOUT)
        return resp.content if mode == "nuft_yaml" else resp.text

    def parse(self, raw: RawItem) -> list[Posting]:
        mode = raw.meta["mode"]
        if mode == "simplify":
            return self._parse_simplify(raw)
        if mode == "zshah":
            return self._parse_zshah(raw)
        if mode == "nuft_yaml":
            return self._parse_nuft(raw)
        if mode == "speedyapply":
            return self._parse_speedyapply(raw)
        if mode == "zapply_md":
            return self._parse_zapply_md(raw)
        raise ValueError(f"unknown github_trackers mode {mode!r}")

    def _parse_simplify(self, raw: RawItem) -> list[Posting]:
        postings = []
        season_filter = raw.meta.get("season_filter", True)
        for item in raw.payload:
            if season_filter:
                terms = item.get("terms")
                if terms is not None:
                    if "Summer 2027" not in terms:
                        continue
                # DECISION: vanshb03's `season` is a bare season word live
                # ('Summer', 'Fall', ...) in a Summer-2027-scoped repo — accept
                # both the spec's 'Summer 2027' and the observed 'Summer'.
                elif item.get("season") not in ("Summer 2027", "Summer"):
                    continue
            if item.get("is_visible") is False or item.get("active") is False:
                continue
            posted = item.get("date_posted")
            postings.append(
                Posting(
                    company_name=item["company_name"],
                    title=item["title"],
                    url=item["url"],
                    source=self.name,
                    source_ref=raw.source_ref,
                    source_key=item["id"],
                    location_raw="; ".join(item.get("locations") or []) or None,
                    posted_at=iso_from_epoch_s(posted) if posted else None,
                    tracker_hints=_hints(item),
                )
            )
        return postings

    def _parse_zshah(self, raw: RawItem) -> list[Posting]:
        payload = raw.payload
        if isinstance(payload.get("jobs"), list):
            entries = [(item["id"], item) for item in payload["jobs"]]  # mirror shape
        else:
            entries = list(payload.items())
        postings = []
        for key, item in entries:
            if item.get("season") not in ("Summer 2027", "Not stated"):
                continue
            if item.get("is_open") is False:
                continue
            postings.append(
                Posting(
                    company_name=item["company"],
                    title=item["title"],
                    url=item["url"],
                    source=self.name,
                    source_ref=raw.source_ref,
                    source_key=key,
                    location_raw=item.get("location"),
                    posted_at=iso_norm(item.get("posted_at")),
                    tracker_hints=_hints(item),
                )
            )
        return postings

    def _parse_nuft(self, raw: RawItem) -> list[Posting]:
        postings = []
        with tarfile.open(fileobj=io.BytesIO(raw.payload), mode="r:*") as tar:
            for member in tar.getmembers():
                if "/data/" not in member.name or not member.name.endswith((".yaml", ".yml")):
                    continue
                doc = yaml.safe_load(tar.extractfile(member).read())
                if not doc or not doc.get("name"):
                    continue
                locations = doc.get("locations")
                if isinstance(locations, list):  # plain string in every live file
                    locations = "; ".join(locations)
                for role in doc.get("roles") or []:
                    role_type = role.get("role_type") or "Quant"
                    for link in role.get("links") or []:
                        url = link.get("url")
                        if not url:
                            continue
                        title = f"{role_type} Internship"
                        if link.get("label"):
                            title += f" - {link['label']}"
                        postings.append(
                            Posting(
                                company_name=doc["name"],
                                title=title,
                                url=url,
                                source=self.name,
                                source_ref=raw.source_ref,
                                source_key=_sha256(url),
                                location_raw=locations or None,
                                tracker_hints={"category": "Quant"},
                            )
                        )
        return postings

    def _parse_speedyapply(self, raw: RawItem) -> list[Posting]:
        text = raw.payload
        postings = []
        seen: set[str] = set()
        for table in _SPEEDY_TABLES:
            start = text.find(f"<!-- {table}_START -->")
            end = text.find(f"<!-- {table}_END -->")
            if start < 0 or end < 0:
                continue
            for line in text[start:end].splitlines():
                cells = [c.strip() for c in line.strip().strip("|").split("|")]
                # FAANG/QUANT tables have 6 columns; the main table omits
                # Salary and has 5. Company/Position/Location always lead.
                if len(cells) < 5 or "<strong>" not in cells[0]:
                    continue  # header / separator rows
                company = _STRONG_RE.search(cells[0])
                hrefs = _HREF_RE.findall(line)
                if not company or not hrefs:
                    continue
                url = hrefs[-1]  # apply anchor is the LAST anchor in the row
                if url in seen:
                    continue
                seen.add(url)
                postings.append(
                    Posting(
                        company_name=html_mod.unescape(company.group(1)),
                        title=html_mod.unescape(cells[1]),
                        url=url,
                        source=self.name,
                        source_ref=raw.source_ref,
                        source_key=_sha256(url),
                        location_raw=cells[2] or None,
                        # Age column is relative ("36d") — first_seen_at carries.
                    )
                )
        return postings

    def _parse_zapply_md(self, raw: RawItem) -> list[Posting]:
        """underclassmen-internships: | [Name](url) | Status | Year | Note | rows
        (one section drops the Status column — map cells by count)."""
        postings = []
        for line in raw.payload.splitlines():
            if not line.startswith("|") or "](" not in line:
                continue
            m = _MD_LINK_RE.search(line)
            if not m:
                continue
            name, url = m.group(1).strip(), m.group(2).strip()
            cells = [c.strip() for c in line.strip("|").split("|")]
            rest = cells[1:]
            status, year, note = "", "", ""
            if len(rest) >= 3:
                status, year, note = rest[0], rest[1], " ".join(rest[2:])
            elif len(rest) == 2:
                year, note = rest[0], rest[1]
            hints = {"program": "underclassmen"}
            if year:
                hints["eligibility"] = year
            postings.append(
                Posting(
                    company_name=name,
                    title=name,
                    url=url,
                    source=self.name,
                    source_ref=raw.source_ref,
                    source_key=_sha256(url),
                    description=f"Early-talent program. Eligibility: {year or 'unknown'}. "
                    f"Status: {status or 'unknown'}. {note}".strip(),
                    tracker_hints=hints,
                )
            )
        return postings
