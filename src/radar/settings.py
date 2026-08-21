"""Env + config loading (SPEC §12).

# DECISION: SPEC §2.1 places settings.py under config/; it lives in the package
# instead so it is importable after install — config/ stays data-only
# (registry.yaml, profile.md).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = Path(os.environ.get("RADAR_CONFIG_DIR", _ROOT / "config"))


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


@dataclass(frozen=True)
class Settings:
    db_path: str
    model: str
    max_llm_calls_per_run: int
    bootstrap_max_llm_calls: int
    send_empty: bool
    send_flash: bool
    flash_max_per_day: int
    digest_max_rows: int
    digest_to: str
    owner_email: str
    anthropic_api_key: str
    openai_api_key: str
    openai_model: str
    llm_provider: str  # 'anthropic' | 'openai' (resolved, SPEC §7)
    resend_api_key: str
    sheet_webapp_url: str
    sheet_token: str


def _resolve_provider(env) -> str:
    """RADAR_LLM_PROVIDER wins; else auto-select from whichever key exists (SPEC §7)."""
    explicit = env.get("RADAR_LLM_PROVIDER", "").strip().lower()
    if explicit in ("anthropic", "openai"):
        return explicit
    if not env.get("ANTHROPIC_API_KEY") and env.get("OPENAI_API_KEY"):
        return "openai"
    return "anthropic"


def load_settings() -> Settings:
    env = os.environ
    return Settings(
        db_path=env.get("RADAR_DB", "data/jobs.db"),
        model=env.get("RADAR_MODEL", "claude-haiku-4-5-20251001"),
        max_llm_calls_per_run=int(env.get("MAX_LLM_CALLS_PER_RUN", "300")),
        bootstrap_max_llm_calls=int(env.get("BOOTSTRAP_MAX_LLM_CALLS", "2000")),
        send_empty=_env_bool("SEND_EMPTY", True),
        send_flash=_env_bool("SEND_FLASH", True),
        flash_max_per_day=int(env.get("FLASH_MAX_PER_DAY", "3")),
        digest_max_rows=int(env.get("DIGEST_MAX_ROWS", "150")),
        digest_to=env.get("DIGEST_TO", ""),
        owner_email=env.get("OWNER_EMAIL", ""),
        anthropic_api_key=env.get("ANTHROPIC_API_KEY", ""),
        openai_api_key=env.get("OPENAI_API_KEY", ""),
        openai_model=env.get("RADAR_OPENAI_MODEL", "gpt-4.1-mini"),
        llm_provider=_resolve_provider(env),
        resend_api_key=env.get("RESEND_API_KEY", ""),
        sheet_webapp_url=env.get("SHEET_WEBAPP_URL", ""),
        sheet_token=env.get("SHEET_TOKEN", ""),
    )


def load_registry(config_dir: Path | None = None) -> dict:
    """Returns {'companies': [...], 'aliases': {...}} from config/registry.yaml."""
    path = (config_dir or CONFIG_DIR) / "registry.yaml"
    with open(path, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    data.setdefault("companies", [])
    data.setdefault("aliases", {})
    # YAML keys like `1x` load as ints; normalize the alias map to str -> str.
    data["aliases"] = {str(k): str(v) for k, v in data["aliases"].items()}
    return data


def load_profile(config_dir: Path | None = None) -> str:
    path = (config_dir or CONFIG_DIR) / "profile.md"
    return path.read_text(encoding="utf-8")


def companies_for(registry: dict, ats: str) -> list[tuple[str, str]]:
    """[(display_name, token)] for one ats kind, skipping rows without tokens."""
    out = []
    for row in registry["companies"]:
        if row.get("ats") == ats and row.get("token"):
            out.append((str(row["name"]), str(row["token"])))
    return out


# ---------------------------------------------------------------- tiers

TIER_ORDER = {"S": 0, "1": 1, "2": 2}  # None (unregistered) sorts after all of these


def tier_index(registry: dict) -> dict[str, str]:
    """company_norm(name) -> tier for every registry row. Tracker/HN rows inherit
    their tier through the alias map — that wiring is load-bearing (SPEC §4.1)."""
    from .normalize import company_norm

    aliases = registry.get("aliases", {})
    return {
        company_norm(str(row["name"]), aliases): str(row.get("tier", ""))
        for row in registry["companies"]
    }


def tier_for(company_name: str, registry: dict, index: dict[str, str] | None = None) -> str | None:
    from .normalize import company_norm

    index = index if index is not None else tier_index(registry)
    return index.get(company_norm(company_name, registry.get("aliases", {})))


def tier_rank(tier: str | None) -> int:
    return TIER_ORDER.get(tier or "", 3)
