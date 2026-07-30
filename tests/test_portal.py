from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from maps_review_monitor.portal import build_same_name_groups, create_app, load_content_assets


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
    home = client.get("/")
    assert home.status_code == 200
    assert 'id="similar-review-toggle"' in home.text
    assert 'id="suspected-review-toggle"' in home.text
    assert "顯示高度相似評論" in home.text
    assert "顯示疑似協同評論" in home.text
    assert "hide-highly-similar" in home.text
    assert "hide-suspected" in home.text
    assert "版本 0.3.1" in home.text
    assert 'href="/reviewers"' in home.text
    css = client.get("/static/portal.css")
    assert css.status_code == 200
    assert ".hide-highly-similar .similar-only" in css.text
    assert ".hide-suspected .suspected-only" in css.text
    response = client.get("/analysis")
    assert response.status_code == 200
    assert "全部店家分析" in response.text
    status = client.get("/api/analysis-status").json()
    assert status["status"] == "none"
    assert client.get("/reviewers").status_code == 200


def test_same_name_groups_are_cross_shop_and_do_not_assume_identity():
    reviews = [
        {
            "author": "王 小明",
            "shop_key": "a",
            "shop_name": "甲店",
            "review_id": "1",
            "first_seen_at": "2026-07-01",
            "profile": {"url": "https://example.com/profile/1"},
        },
        {
            "author": "王　小明",
            "shop_key": "b",
            "shop_name": "乙店",
            "review_id": "2",
            "first_seen_at": "2026-07-02",
            "profile": {"url": "https://example.com/profile/2"},
        },
        {
            "author": "只有單店",
            "shop_key": "a",
            "shop_name": "甲店",
            "review_id": "3",
            "first_seen_at": "2026-07-03",
            "profile": {"url": ""},
        },
    ]
    groups = build_same_name_groups(reviews)
    assert len(groups) == 1
    assert groups[0]["shop_count"] == 2
    assert groups[0]["identity_status"] == "different_profiles"
    assert "未確認為同一人" in groups[0]["identity_text"]
