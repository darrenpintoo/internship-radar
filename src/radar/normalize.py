"""Cleaning, prefilter, hashing (SPEC §6.1, §6.2)."""

from __future__ import annotations

import hashlib
import html as html_mod
import re
import unicodedata
from datetime import UTC, datetime

# ---------------------------------------------------------------- company

_LEGAL_SUFFIX_RE = re.compile(r"\b(inc|llc|ltd|llp|lp|plc|corp|co)\b\.?", re.I)
_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WS_RE = re.compile(r"\s+")


def company_norm(name: str, aliases: dict[str, str] | None = None) -> str:
    s = unicodedata.normalize("NFKC", name).lower()
    s = _LEGAL_SUFFIX_RE.sub(" ", s)
    s = _PUNCT_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s).strip()
    if aliases:
        # EXACT full-string dict lookup only — substring/prefix matching would
        # merge Citadel with Citadel Securities (SPEC §6.1).
        s = aliases.get(s, s)
    return s


# ---------------------------------------------------------------- title

_DASHES_RE = re.compile(r"[‒–—−]")
_SEASON = r"(summer|spring|fall|autumn|winter)"
_SEASON_YEAR_RE = re.compile(rf"\(?\s*\b{_SEASON}\b[,\s\-]*(of\s+)?20\d{{2}}\s*\)?", re.I)
_YEAR_SEASON_RE = re.compile(rf"\(?\s*20\d{{2}}[,\s\-]*\b{_SEASON}\b\s*\)?", re.I)
_BARE_YEAR_RE = re.compile(r"\b20\d{2}\b")
_REQ_ID_RE = re.compile(r"\b(req|job|jr|r)[-#]?\d{3,}\b", re.I)
_PARENS_RE = re.compile(r"\([^)]*\)")


def title_norm(title: str) -> str:
    """Deterministic title normalization for hashing/fuzzy only (SPEC §6.1)."""
    s = unicodedata.normalize("NFKC", title).lower()
    s = _DASHES_RE.sub("-", s)
    s = _SEASON_YEAR_RE.sub(lambda m: f" {m.group(1).lower()} ", s)
    s = _YEAR_SEASON_RE.sub(lambda m: f" {m.group(1).lower()} ", s)
    s = _BARE_YEAR_RE.sub(" ", s)
    s = _REQ_ID_RE.sub(" ", s)
    s = _PARENS_RE.sub(" ", s)
    s = _PUNCT_RE.sub(" ", s)
    return _WS_RE.sub(" ", s).strip()


def content_hash(company_normed: str, title_normed: str) -> str:
    """sha256(company_norm | sorted token bag of title_norm) — no location (SPEC §6.1)."""
    bag = " ".join(sorted(title_normed.split()))
    return hashlib.sha256(f"{company_normed}|{bag}".encode()).hexdigest()


# ---------------------------------------------------------------- prefilter

# Early-talent programs are titled "Launch"/"Explore"/"FutureFocus", not
# "Intern" — the class-year tokens catch them on direct boards (a false
# positive costs one cheap LLM call; a false negative costs the posting).
INCLUDE_RE = re.compile(
    r"\bintern(s|ship|ships)?\b|\bco[-\s]?ops?\b|\bsummer\s+(analyst|associate)\b"
    r"|\bsophomores?\b|\bfreshm[ae]n\b|\bfirst[-\s]?year\b|\bsecond[-\s]?year\b"
    r"|\bunderclass\w*|\bearly\s+insights?\b|\bemerging\s+talent\b|\bdiscovery\s+day\b"
    r"|\bfellowship\b|\bfuturefocus\b|\blaunchpad\b|\bwinternship\b",
    re.I,
)
SEASON_2026_TITLE_RE = re.compile(
    rf"\b{_SEASON}\s*(of\s*)?['’]?\s*2026\b|\b2026\s*['’]?\s*{_SEASON}\b",
    re.I,
)
_2027_RE = re.compile(r"\b2027\b")

# RADAR_TARGET=newgrad swaps the include check: full-time entry-level titles.
NEWGRAD_INCLUDE_RE = re.compile(
    r"\bnew[-\s]?grad(uate)?s?\b|\bentry[-\s]?level\b|\buniversity grad(uate)?\b"
    r"|\bearly[-\s]?career\b|\bgraduate\s+(software|engineer|developer|program|scheme|analyst)"
    r"|\bcampus\s+hire\b",
    re.I,
)


def keep(title: str, policy: str, target: str = "internship") -> bool:
    """Prefilter in keep-form (SPEC §6.2). Gates INSERTION of new rows only.

    policy 'full'          — title must match the include regex (ATS boards)
    policy 'excludes_only' — trackers/HN/Reddit rows skip the include check
    target 'newgrad' swaps the include regex for entry-level titles.
    Never scans descriptions for years: nearly every live Summer-2027 posting
    mentions a 2026 date (deadlines, company history).
    """
    include = NEWGRAD_INCLUDE_RE if target == "newgrad" else INCLUDE_RE
    if policy == "full" and not include.search(title):
        return False
    if SEASON_2026_TITLE_RE.search(title) and not _2027_RE.search(title):
        return False
    return True


# ---------------------------------------------------------------- description

_TAG_RE = re.compile(r"<[^>]+>")

DESCRIPTION_CAP = 12_000
_HEAD = 8_000
_TAIL = 4_000 - len("\n[...truncated...]\n")


def strip_html(raw: str) -> str:
    """HTML → plain text. Greenhouse content arrives HTML-entity-escaped, so
    unescape FIRST (SPEC §5.2) — twice-escaped payloads need two passes."""
    s = html_mod.unescape(html_mod.unescape(raw))
    s = re.sub(r"(?i)<\s*(br|/p|/li|/div|/h[1-6])\s*/?>", "\n", s)
    s = _TAG_RE.sub(" ", s)
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n", s)
    return s.strip()


def truncate_description(text: str | None) -> str | None:
    """Middle-out truncation: head + tail, because eligibility text (grad window,
    work auth, deadline) lives at the BOTTOM of long postings (SPEC §7)."""
    if text is None or len(text) <= DESCRIPTION_CAP:
        return text
    return text[:_HEAD] + "\n[...truncated...]\n" + text[-_TAIL:]


# ---------------------------------------------------------------- timestamps


def iso_from_epoch_ms(ms: int | float) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_from_epoch_s(s: int | float) -> str:
    return datetime.fromtimestamp(s, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def iso_norm(value: str | None) -> str | None:
    """Any ISO-8601 string (offset or Z) → UTC 'YYYY-MM-DDTHH:MM:SSZ'."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
