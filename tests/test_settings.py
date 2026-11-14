from pathlib import Path

import pytest

from receiptbot.settings import ConfigError, load_settings, require_env

EXAMPLE = Path(__file__).parent.parent / "config.example.yaml"


def test_example_config_loads_with_safe_defaults(tmp_path, monkeypatch):
    monkeypatch.delenv("RECEIPTBOT_DATA_DIR", raising=False)
    path = tmp_path / "config.yaml"
    path.write_text(EXAMPLE.read_text())
    settings = load_settings(path)
    assert settings.dry_run is True  # the example starts in dry-run mode
    assert settings.ynab.approved is False
    assert settings.keep_attachments is False
    assert settings.llm.enabled is True and settings.llm.model == "claude-haiku-5-5"
    assert settings.rules_file == tmp_path / "rules.yaml"
    assert settings.signal.sender_name("+15550100002") == "Sam"
    assert settings.signal.sender_name("+15559999999", None) is None


def test_minimal_config_defaults_and_card_map(tmp_path, monkeypatch):
    monkeypatch.setenv("RECEIPTBOT_DATA_DIR", "/data")
    path = tmp_path / "config.yaml"
    path.write_text(
        "ynab:\n  plan_id: p\n  plan_name: Test\n  default_account_id: a\n  card_map: {'1234': b}\n"
    )
    settings = load_settings(path)
    assert settings.dry_run is False and settings.signal is None
    assert settings.ynab.account_for("1234") == "b"
    assert settings.ynab.account_for("9999") == "a"
    assert settings.ynab.account_for(None) == "a"
    assert settings.ynab.account_ids == ["a", "b"]
    assert settings.db_path == Path("/data/receiptbot.sqlite3")


def test_missing_config_and_env(tmp_path, monkeypatch):
    with pytest.raises(ConfigError):
        load_settings(tmp_path / "nope.yaml")
    monkeypatch.delenv("YNAB_ACCESS_TOKEN", raising=False)
    with pytest.raises(ConfigError):
        require_env("YNAB_ACCESS_TOKEN")
