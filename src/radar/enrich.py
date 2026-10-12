"""LLM enrichment via structured outputs (SPEC §7).

Design notes locked at spec time:
- Structured outputs (Anthropic messages.parse / OpenAI responses.parse) with
  the EnrichmentOut Pydantic model — no JSON prompting, no fence-stripping,
  no invalid-JSON retry path.
- NO prompt caching: Haiku 4.5's minimum cacheable prefix is 4096 tokens and
  our stable system prefix is ~600, so cache_control would silently no-op
  (cache_creation_input_tokens: 0) and save <$0.10/day even if it worked.
  Do not "optimize" it in.
- Queue = ALL active postings lacking an enrichments row with
  enrich_attempts < 3, tier-first; any client failure writes NO row (retry next
  run); a null row with red_flags=["enrichment_failed"] only after 3 attempts.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Protocol

from . import db
from .models import EnrichmentOut
from .settings import Settings, load_profile, tier_for, tier_index, tier_rank

log = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
MAX_TOKENS = 500
ABORT_AFTER = 3  # consecutive failures with zero successes => abort the run

SYSTEM_TEMPLATE = """You extract structured recruiting data from job postings for one specific
candidate, and score fit for that candidate only.

CANDIDATE PROFILE:
{profile}

Score each track independently against this specific candidate:
  90-100 bullseye: squarely in-track AND the posting asks for the candidate's
         strengths (C++/systems, HPC, RL/game AI, robotics, backend+agents)
  75-89  strong: clearly in-track; candidate is a credible, competitive applicant
  50-74  plausible: adjacent or generalist; worth a look, not a priority
  25-49  weak: mostly off-track, or emphasizes skills the candidate lacks
  0-24   wrong direction: different discipline, seniority, or field
Most postings should score below 50 on at least two of the four tracks.
Use the full range; do not cluster scores in the 60-85 band.

rationale: at most 25 words — one sentence naming the decisive factor.

deadline: only if the posting states an explicit application deadline date;
never infer one from start dates, posting dates, or season language; if the
stated deadline is relative or ambiguous, use null.

grad_window = graduation dates the posting accepts (e.g. "graduating Dec 2027
– Jun 2028" → earliest 2027-12, latest 2028-06). Null side = unbounded.
"""


def build_system_prompt(profile: str) -> str:
    return SYSTEM_TEMPLATE.replace("{profile}", profile.strip())


def build_user_prompt(row: sqlite3.Row) -> str:
    lines = [
        f"company: {row['company_name']}",
        f"title: {row['title']}",
        f"location: {row['location_raw'] or 'unspecified'}",
        f"posted_at: {row['posted_at'] or 'unknown'}",
    ]
    hints = row["tracker_hints"]
    if hints:
        for k, v in json.loads(hints).items():
            lines.append(f"tracker_{k}: {v}")
    lines.append("")
    lines.append("description:")
    lines.append(row["description"] or "(no description provided)")
    return "\n".join(lines)


# ---------------------------------------------------------------- clients


class Truncated(Exception):
    """Model hit max_tokens before completing the structured output (SPEC §7 guard)."""


class LLMClient(Protocol):
    model_name: str

    def parse(self, system: str, user: str) -> EnrichmentOut: ...


class AnthropicClient:
    def __init__(self, api_key: str, model: str) -> None:
        import anthropic

        self._client = anthropic.Anthropic(api_key=api_key)
        self.model_name = model

    def parse(self, system: str, user: str) -> EnrichmentOut:
        kwargs: dict = {}
        # Claude 4.6+/5 reject sampling params; only Haiku 4.5 accepts temperature.
        if self.model_name.startswith("claude-haiku-4-5"):
            kwargs["temperature"] = 0
        resp = self._client.messages.parse(
            model=self.model_name,
            max_tokens=MAX_TOKENS,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_format=EnrichmentOut,
            **kwargs,
        )
        if resp.stop_reason == "max_tokens":
            raise Truncated(self.model_name)
        out = resp.parsed_output
        if out is None:
            raise Truncated(self.model_name)
        return out


class OpenAIClient:
    def __init__(self, api_key: str, model: str) -> None:
        import openai

        self._client = openai.OpenAI(api_key=api_key)
        self.model_name = model

    def parse(self, system: str, user: str) -> EnrichmentOut:
        resp = self._client.responses.parse(
            model=self.model_name,
            input=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            text_format=EnrichmentOut,
        )
        out = resp.output_parsed
        if out is None:
            raise Truncated(self.model_name)
        return out


class OpenRouterClient:
    """OpenRouter is OpenAI-compatible but has no Responses API — use
    chat.completions.parse against its base_url. Structured outputs require a
    model that supports json_schema (most paid ones; many :free ones do not)."""

    def __init__(self, api_key: str, model: str) -> None:
        import openai

        self._client = openai.OpenAI(api_key=api_key, base_url="https://openrouter.ai/api/v1")
        self.model_name = model

    def parse(self, system: str, user: str) -> EnrichmentOut:
        resp = self._client.chat.completions.parse(
            model=self.model_name,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            response_format=EnrichmentOut,
        )
        out = resp.choices[0].message.parsed
        if out is None:
            raise Truncated(self.model_name)
        return out


def build_client(settings: Settings) -> LLMClient:
    if settings.llm_provider == "openrouter":
        if not settings.openrouter_api_key:
            raise RuntimeError("RADAR_LLM_PROVIDER=openrouter but OPENROUTER_API_KEY is empty")
        return OpenRouterClient(settings.openrouter_api_key, settings.openrouter_model)
    if settings.llm_provider == "openai":
        if not settings.openai_api_key:
            raise RuntimeError("RADAR_LLM_PROVIDER=openai but OPENAI_API_KEY is empty")
        return OpenAIClient(settings.openai_api_key, settings.openai_model)
    if settings.llm_provider == "anthropic":
        if not settings.anthropic_api_key:
            raise RuntimeError("RADAR_LLM_PROVIDER=anthropic but ANTHROPIC_API_KEY is empty")
        return AnthropicClient(settings.anthropic_api_key, settings.model)
    raise RuntimeError(f"unknown RADAR_LLM_PROVIDER {settings.llm_provider!r}")


# ---------------------------------------------------------------- driver


@dataclass
class EnrichStats:
    calls: int = 0
    enriched: int = 0
    deferred: int = 0
    failed_final: int = 0
    skipped_cluster: int = 0


def _cluster_key(row: sqlite3.Row) -> int:
    return int(row["cluster_id"] if row["cluster_id"] is not None else row["id"])


def _ordered_queue(
    conn: sqlite3.Connection, registry: dict, posting_ids: list[int] | None
) -> list[sqlite3.Row]:
    rows = db.enrichment_queue(conn)
    if posting_ids is not None:
        wanted = set(posting_ids)
        rows = [r for r in rows if r["id"] in wanted]
    index = tier_index(registry)
    # enrichment_queue already yields first_seen_at desc; the sort is stable.
    return sorted(rows, key=lambda r: tier_rank(tier_for(r["company_name"], registry, index)))


def enrich_postings(
    conn: sqlite3.Connection,
    settings: Settings,
    registry: dict,
    posting_ids: list[int] | None = None,
    cap: int | None = None,
    client: LLMClient | None = None,
    now: str | None = None,
) -> EnrichStats:
    """Enrich queued postings up to `cap` LLM calls, tier-first (SPEC §7)."""
    stats = EnrichStats()
    cap = settings.max_llm_calls_per_run if cap is None else cap
    queue = _ordered_queue(conn, registry, posting_ids)
    if not queue or cap <= 0:
        return stats

    # One representative per cluster (SPEC §7), chosen up-front so the
    # selection is independent of call ordering.
    selected: list[sqlite3.Row] = []
    seen_clusters: set[int] = set()
    for row in queue:
        if len(selected) >= cap:
            break
        ckey = _cluster_key(row)
        if ckey in seen_clusters or db.cluster_has_enrichment(conn, ckey):
            stats.skipped_cluster += 1
            continue
        seen_clusters.add(ckey)
        selected.append(row)
    if not selected:
        return stats

    client = client or build_client(settings)
    system = build_system_prompt(load_profile())
    workers = max(1, int(getattr(settings, "llm_workers", 1) or 1))
    consecutive_failures = 0
    total = len(selected)
    log.info(
        "enrich: %d posting(s) queued, %d worker(s), model=%s", total, workers, client.model_name
    )

    # LLM calls are I/O-bound: run each chunk concurrently, then persist results
    # on this thread (the sqlite connection never crosses threads).
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for offset in range(0, total, workers):
            chunk = selected[offset : offset + workers]
            futures = [pool.submit(client.parse, system, build_user_prompt(r)) for r in chunk]
            for row, fut in zip(chunk, futures, strict=True):
                pid = int(row["id"])
                stamp = now or db.now_utc()
                stats.calls += 1
                try:
                    out = fut.result()
                except Exception as exc:
                    consecutive_failures += 1
                    if stats.enriched == 0 and consecutive_failures >= ABORT_AFTER:
                        # Bad key / unknown model / hard rate limit: stop before marking
                        # the whole queue as attempted.
                        raise RuntimeError(
                            f"enrichment aborted after {ABORT_AFTER} consecutive failures "
                            f"with no success ({type(exc).__name__}: {str(exc)[:200]})"
                        ) from exc
                    db.bump_enrich_attempts(conn, pid)
                    attempts = int(row["enrich_attempts"]) + 1
                    if attempts >= MAX_ATTEMPTS:
                        db.insert_enrichment(conn, pid, None, client.model_name, stamp)
                        stats.failed_final += 1
                        log.warning(
                            "enrichment failed for good: posting=%s %s", pid, type(exc).__name__
                        )
                    else:
                        stats.deferred += 1
                        log.info("enrichment deferred: posting=%s %s", pid, type(exc).__name__)
                    conn.commit()
                    continue
                consecutive_failures = 0
                db.insert_enrichment(conn, pid, out, client.model_name, stamp)
                conn.commit()
                stats.enriched += 1
            done = min(offset + workers, total)
            if done % 50 < workers or done == total:
                log.info(
                    "enrich: %d/%d done (%d ok, %d deferred, %d failed)",
                    done,
                    total,
                    stats.enriched,
                    stats.deferred,
                    stats.failed_final,
                )

    return stats


def re_enrich(
    conn: sqlite3.Connection,
    settings: Settings,
    registry: dict,
    posting_ids: list[int],
    client: LLMClient | None = None,
) -> EnrichStats:
    """Drop and redo enrichment for specific postings (repost / pre-digest edit, SPEC §7)."""
    if not posting_ids:
        return EnrichStats()
    for pid in posting_ids:
        db.delete_enrichment(conn, pid)
        db.reset_enrich_attempts(conn, pid)
    conn.commit()
    return enrich_postings(
        conn, settings, registry, posting_ids=posting_ids, cap=len(posting_ids), client=client
    )
