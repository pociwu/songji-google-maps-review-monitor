from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from maps_review_monitor.portal import create_app, load_content_assets


def test_load_content_assets_sorts_newest_first(tmp_path: Path):
    path = tmp_path / "content.yaml"
    path.write_text(
        """content:
  - id: older
    shop_key: shop-a
    published_at: '2026-07-01T09:00:00+08:00'
    title: Older
    body: First
  - id: newer
    shop_key: shop-a
    published_at: '2026-07-02T09:00:00+08:00'
    title: Newer
    body: Second
    photos: [data/portal/shop-a/photo.jpg]
""",
        encoding="utf-8",
    )
    assets = load_content_assets(path)
    assert [asset["id"] for asset in assets] == ["newer", "older"]
    assert assets[0]["photos"] == ["data/portal/shop-a/photo.jpg"]


def test_load_content_assets_rejects_missing_required_field(tmp_path: Path):
    path = tmp_path / "content.yaml"
    path.write_text("content:\n  - id: incomplete\n", encoding="utf-8")
    with pytest.raises(ValueError, match="缺少必要欄位"):
        load_content_assets(path)


def test_portal_analysis_pages_load_without_completed_run(tmp_path: Path):
    config = tmp_path / "config.toml"
    config.write_text(
        """data_dir = "data"
analysis_enabled = false
[[shops]]
name = "測試店家"
url = "https://www.google.com/maps/place/example"
enabled = true
""",
        encoding="utf-8",
    )
    client = TestClient(create_app(str(config)))
    assert client.get("/").status_code == 200
    response = client.get("/analysis")
    assert response.status_code == 200
    assert "全部店家分析" in response.text
    status = client.get("/api/analysis-status").json()
    assert status["status"] == "none"
