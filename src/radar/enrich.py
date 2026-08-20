"""LLM enrichment via structured outputs (SPEC §7). Lands in M2.

Design notes locked at spec time:
- client.messages.parse(..., output_format=EnrichmentOut) — no JSON prompting,
  no fence-stripping, no invalid-JSON retry path.
- NO prompt caching: Haiku 4.5's minimum cacheable prefix is 4096 tokens and
  our stable system prefix is ~600, so cache_control would silently no-op
  (cache_creation_input_tokens: 0). Do not "optimize" it in.
- Queue = ALL active postings lacking an enrichments row with
  enrich_attempts < 3, tier-first; transport errors write NO row (retry next
  run); a null row with red_flags=["enrichment_failed"] only after 3 attempts.
"""

from __future__ import annotations


def enrich_new(conn, settings, posting_ids: list[int]) -> int:
    """Returns number of LLM calls made. M2."""
    raise NotImplementedError("enrichment ships in M2 (SPEC §11)")
