"""Pydantic models: Posting, Enrichment, RunReport (SPEC §3, §7)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Posting(BaseModel):
    """Canonical parsed posting, pre-DB. Adapters emit these (SPEC §5.1)."""

    company_name: str
    title: str
    url: str
    source: str
    source_ref: str = ""
    source_key: str  # per-source natural key, never empty (SPEC §3)
    location_raw: str | None = None
    description: str | None = None
    posted_at: str | None = None  # UTC 'YYYY-MM-DDTHH:MM:SSZ'
    # Free pre-enrichment signals from trackers (sponsorship/category/degrees).
    tracker_hints: dict[str, str] = Field(default_factory=dict)


class GradWindow(BaseModel):
    earliest: str | None = None  # 'YYYY-MM'; null side = unbounded
    latest: str | None = None


class Fit(BaseModel):
    quant: int = Field(ge=0, le=100)
    ai_infra: int = Field(ge=0, le=100)
    swe: int = Field(ge=0, le=100)
    robotics: int = Field(ge=0, le=100)


class EnrichmentOut(BaseModel):
    """Structured-output schema for the enrichment call (SPEC §7)."""

    category: Literal["quant", "ai_infra", "swe", "robotics", "other"]
    season: Literal["summer_2027", "other", "unclear"]
    grad_window: GradWindow
    workauth: Literal["citizen_only", "no_sponsorship", "sponsors_ok", "unspecified"]
    deadline: str | None = None  # 'YYYY-MM-DD' only when explicitly stated
    fit: Fit
    rationale: str = Field(max_length=400)  # <= 25 words asked of the model
    red_flags: list[str] = Field(default_factory=list)


class RunReport(BaseModel):
    mode: str
    started_at: str
    finished_at: str | None = None
    sources_ok: int = 0
    sources_failed: dict[str, str] = Field(default_factory=dict)
    new_postings: int = 0
    llm_calls: int = 0
    flash_alerts: int = 0
