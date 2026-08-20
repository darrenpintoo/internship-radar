"""SourceAdapter protocol (SPEC §5.1).

Rules: adapters never touch the DB; fetch() is network I/O only, parse() is
pure and fixture-testable. A board whose fetch failed yields a RawItem with
`error` set so the pipeline records it in sources_failed and SKIPS closed
detection for that board (SPEC §6.3) without losing the adapter's other boards.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from ..models import Posting


@dataclass
class RawItem:
    source: str
    source_ref: str  # board token / repo / thread id
    payload: Any = None
    not_modified: bool = False  # 304 — snapshot unchanged, skip the diff
    error: str | None = None
    meta: dict[str, str] = field(default_factory=dict)


@runtime_checkable
class SourceAdapter(Protocol):
    name: str
    prefilter_policy: str  # 'full' | 'excludes_only' (SPEC §6.2)

    def fetch(self) -> list[RawItem]: ...

    def parse(self, raw: RawItem) -> list[Posting]: ...
