"""Flash alerts (SPEC §8.6). Lands in M2.

tier-S or fit >= 90, never-alerted clusters only, cap FLASH_MAX_PER_DAY (3),
dedupe via alert_log + postings.alerted_at, suppressed during bootstrap.
"""

from __future__ import annotations


def send_flash_alerts(conn, settings, posting_ids: list[int]) -> int:
    """Returns number of alerts sent. M2."""
    raise NotImplementedError("flash alerts ship in M2 (SPEC §11)")
