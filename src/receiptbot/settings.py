"""Configuration: secrets from the environment, everything else from config.yaml."""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

# Verified against the Claude model list on 2026-11-13: the current small model
# that accepts PDF and image input. Override with llm.model in config.yaml.
DEFAULT_LLM_MODEL = "claude-haiku-5-5"


class ConfigError(Exception):
    pass


class YnabSettings(BaseModel):
    plan_id: str
    plan_name: str
    default_account_id: str
    card_map: dict[str, str] = Field(default_factory=dict)  # card last four -> account id
    approved: bool = False

    def account_for(self, last4: str | None) -> str:
        return self.card_map.get(last4 or "", self.default_account_id)

    @property
    def account_ids(self) -> list[str]:
        return list(dict.fromkeys([self.default_account_id, *self.card_map.values()]))


class Sender(BaseModel):
    id: str  # phone number (+15551234567) or Signal account UUID
    name: str


class SignalSettings(BaseModel):
    group_id: str
    allowed_senders: list[Sender]
    url: str = "http://signal-cli:8080"

    def sender_name(self, *ids: str | None) -> str | None:
        for sender in self.allowed_senders:
            if sender.id in ids:
                return sender.name
        return None


class LlmSettings(BaseModel):
    enabled: bool = True
    model: str = DEFAULT_LLM_MODEL
    min_confidence: float = 0.8


class ReconcileSettings(BaseModel):
    interval_minutes: int = 30
    grace_days: int = 2
    window_days: int = 21
    recurring_tolerance: float = 0.05


class Limits(BaseModel):
    max_file_bytes: int = 10 * 1024 * 1024
    max_pdf_pages: int = 15
    min_text_chars: int = 150  # a text message shorter than this is not a pasted receipt


class Settings(BaseModel):
    ynab: YnabSettings
    signal: SignalSettings | None = None
    llm: LlmSettings = Field(default_factory=LlmSettings)
    reconcile: ReconcileSettings = Field(default_factory=ReconcileSettings)
    limits: Limits = Field(default_factory=Limits)
    dry_run: bool = False
    keep_attachments: bool = False
    data_dir: Path = Path("data")
    rules_file: Path = Path("rules.yaml")
    recurring_file: Path = Path("recurring.yaml")

    @property
    def db_path(self) -> Path:
        return self.data_dir / "receiptbot.sqlite3"


def load_settings(path: str | Path | None = None) -> Settings:
    path = Path(path or os.environ.get("RECEIPTBOT_CONFIG", "config.yaml"))
    if not path.exists():
        raise ConfigError(f"{path} not found. Copy config.example.yaml and fill it in.")
    raw = yaml.safe_load(path.read_text()) or {}
    return Settings.model_validate(raw)


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ConfigError(f"Environment variable {name} is not set.")
    return value
