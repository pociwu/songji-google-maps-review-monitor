from pathlib import Path

import pytest

from maps_review_monitor.config import load_settings


def test_config_loads_relative_paths(tmp_path: Path, monkeypatch):
    (tmp_path / "config.toml").write_text(
        'timezone="Asia/Taipei"\n[[shops]]\nname="店"\nurl="https://www.google.com/maps/place/x"\n', encoding="utf-8"
    )
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    settings = load_settings(tmp_path / "config.toml")
    assert settings.data_dir == tmp_path / "data"
    assert settings.shops[0].key


def test_config_rejects_non_google_url(tmp_path: Path):
    path = tmp_path / "config.toml"
    path.write_text('[[shops]]\nname="店"\nurl="https://example.com/x"\n', encoding="utf-8")
    with pytest.raises(ValueError):
        load_settings(path)

