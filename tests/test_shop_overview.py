from pathlib import Path
from types import SimpleNamespace
import sqlite3

import pytest
from fastapi.testclient import TestClient

from maps_review_monitor.config import ShopConfig
from maps_review_monitor.database import Database
from maps_review_monitor.portal import create_app
from maps_review_monitor.shop_overview import history, normalize_overview, record


@pytest.fixture
def store(tmp_path):
    db = Database(tmp_path / "reviews.sqlite3")
    shop = ShopConfig("測試店", "https://www.google.com/maps?cid=123")
    db.sync_shop(shop.key, shop.name, shop.url)
    yield db, shop
    db.close()


def test_explicit_observations_only():
    assert normalize_overview({"name": "  店家  ", "rating": "4.8 顆星", "review_count": "1,234 則評論", "hours": ["營業中，22:00 結束"], "website": "javascript:alert(1)"}) == {
        "name": "店家", "rating": 4.8, "review_count": 1234,
    }
    assert normalize_overview({"review_count": "0 reviews"}) == {"review_count": 0}
    assert normalize_overview({"review_count": "撰寫評論"}) == {}
    hours = [f"星期{i} 09:00–18:00" for i in range(7)]
    assert normalize_overview({"hours": hours[::-1]})["hours"] == hours


def test_baseline_changes_missing_fields_and_revert(store):
    db, shop = store
    first = {"name": "測試店", "phone": "123", "rating": 4.8}
    assert not record(db, shop, first, notify=True)
    assert not db.conn.execute("SELECT 1 FROM events").fetchone()
    assert not record(db, shop, first, notify=True)
    assert not record(db, shop, {"name": "測試店"}, notify=True)
    assert len(history(db, shop.key)) == 1
    assert record(db, shop, {"name": "測試店", "phone": "456"}, notify=True)
    latest = history(db, shop.key)[0]
    assert latest["values"]["rating"] == 4.8
    assert latest["changes"] == {"phone": {"before": "123", "after": "456"}}
    assert record(db, shop, first, notify=True)
    assert len(history(db, shop.key)) == 3  # A → B → A must retain both transitions.
    assert db.conn.execute("SELECT count(*) FROM events").fetchone()[0] == 2
    part = db.conn.execute("SELECT payload_json FROM notification_parts ORDER BY id LIMIT 1").fetchone()[0]
    assert "電話" in part and "原值：123" in part and "新值：456" in part
    assert "總評分" not in part


def test_initial_missing_field_later_confirmed_and_baseline_mode(store):
    db, shop = store
    record(db, shop, {"name": "店"}, notify=True)
    assert record(db, shop, {"name": "店", "website": "https://example.com"}, notify=True)
    assert history(db, shop.key)[0]["changes"]["website"]["before"] is None
    assert not record(db, shop, {"name": "店", "phone": "123"}, notify=False)
    assert len(history(db, shop.key)) == 3
    assert db.conn.execute("SELECT count(*) FROM events").fetchone()[0] == 1


def test_atomic_outbox_failure_rolls_back_version(store):
    db, shop = store
    record(db, shop, {"name": "A"}, notify=True)
    db.conn.execute("CREATE TRIGGER fail_notification BEFORE INSERT ON notification_parts BEGIN SELECT RAISE(ABORT, 'test failure'); END")
    db.conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        record(db, shop, {"name": "B"}, notify=True)
    assert history(db, shop.key)[0]["values"]["name"] == "A"
    assert not db.conn.execute("SELECT 1 FROM events").fetchone()


def test_cover_versions_keep_distinct_paths(store):
    db, shop = store
    a = {"sha256": "a", "path": "data/media/a.png"}
    b = {"sha256": "b", "path": "data/media/b.png"}
    record(db, shop, {"name": "店", "cover": a}, notify=True)
    record(db, shop, {"name": "店", "cover": b}, notify=True)
    assert [v["values"]["cover"] for v in history(db, shop.key)] == [b, a]


def test_cover_change_enqueues_old_and_new_media(store):
    import json
    db, shop = store
    folder = db.media_root / shop.key / "overview"
    folder.mkdir(parents=True)
    old_path, new_path = folder / "old.png", folder / "new.png"
    old_path.write_bytes(b"fixture-old")
    new_path.write_bytes(b"fixture-new")
    record(db, shop, {"name": "店", "cover": {"sha256": "old", "path": str(old_path)}}, notify=True)
    record(db, shop, {"name": "店", "cover": {"sha256": "new", "path": str(new_path)}}, notify=True)
    parts = [json.loads(row[0]) for row in db.conn.execute("SELECT payload_json FROM notification_parts ORDER BY part_no")]
    assert [part["paths"] for part in parts if part["kind"] == "media"] == [[str(old_path)], [str(new_path)]]


def test_overview_portal_history_escaped_and_paginated(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text('data_dir = "data"\nanalysis_enabled = false\n[[shops]]\nname = "店"\nurl = "https://www.google.com/maps?cid=123"\n', encoding="utf-8")
    client = TestClient(create_app(str(config)))
    shop = ShopConfig("店", "https://www.google.com/maps?cid=123")
    db = Database(tmp_path / "data" / "reviews.sqlite3")
    db.sync_shop(shop.key, shop.name, shop.url)
    for number in range(22):
        record(db, shop, {"name": "<script>bad</script>", "phone": str(number)}, notify=False)
    db.close()
    response = client.get(f"/shops/{shop.key}")
    assert response.status_code == 200
    assert "店家總覽" in response.text and "總覽歷史版本" in response.text
    assert 'href="https://www.google.com/maps?cid=123"' in response.text
    home = client.get('/')
    assert 'Google Maps 店家頁' in home.text
    assert 'rel="noopener noreferrer"' in home.text
    assert "<script>bad</script>" not in response.text
    assert "&lt;script&gt;bad&lt;/script&gt;" in response.text
    assert "較舊的版本" in response.text
    second = client.get(f"/shops/{shop.key}?overview_page=2")
    assert "首次基準" in second.text
    assert client.get(f"/shops/{shop.key}?overview_page=0").status_code == 404


def test_monitor_captures_overview_even_when_reviews_fail(store, monkeypatch):
    from maps_review_monitor import monitor
    db, shop = store
    settings = SimpleNamespace(database_path=db.path, timezone="Asia/Taipei", root=db.path.parent,
        telegram_batch_limit=10, shops=[shop], analysis_enabled=False)
    class Scraper:
        def __init__(self, settings): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def scrape_shop(self, shop, lookup, overview_callback):
            overview_callback({"name": "店", "address": "地址"})
            raise RuntimeError("沒有評論按鈕")
    monkeypatch.setattr(monitor, "MapsScraper", Scraper)
    result = monitor.run_check(settings, baseline_only=True)
    assert result["shops_failed"] == 1
    assert history(db, shop.key)[0]["values"]["address"] == "地址"


def test_monitor_pending_is_not_failure_or_success(store, monkeypatch):
    from maps_review_monitor import monitor
    from maps_review_monitor.scraper import ReviewsUnavailable
    db, shop = store
    settings = SimpleNamespace(database_path=db.path, timezone="Asia/Taipei", root=db.path.parent,
        telegram_batch_limit=10, shops=[shop], analysis_enabled=False)
    class Scraper:
        def __init__(self, settings): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def scrape_shop(self, shop, lookup, overview_callback):
            overview_callback({"name": "店"})
            raise ReviewsUnavailable("等待評論")
    monkeypatch.setattr(monitor, "MapsScraper", Scraper)
    result = monitor.run_check(settings, baseline_only=True)
    assert result['shops_failed'] == result['shops_ok'] == 0
    assert result['shops_waiting_reviews'] == 1
    assert not db.is_initialized(shop.key)


def test_v5_migration_preserves_shop(store):
    db, shop = store
    path = db.path
    db.conn.execute("DROP TABLE shop_overview_versions")
    db.conn.execute("PRAGMA user_version=5")
    db.conn.commit()
    other = Database(path)
    assert other.conn.execute("PRAGMA user_version").fetchone()[0] == 6
    assert other.conn.execute("SELECT name FROM shops WHERE shop_key=?", (shop.key,)).fetchone()[0] == shop.name
    assert history(other, shop.key) == []
    other.close()


@pytest.mark.parametrize("hours_layout", ["legacy", "current"])
def test_real_browser_overview_extraction(tmp_path, monkeypatch, hours_layout):
    """Exercise the actual DOM selectors and schedule extraction in Chromium."""
    from playwright.sync_api import sync_playwright
    from maps_review_monitor.scraper import MapsScraper
    chrome = Path("C:/Program Files/Google/Chrome/Application/chrome.exe")
    if not chrome.exists():
        pytest.skip("Local Chrome unavailable; run this browser fixture on a Chrome host")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=str(chrome), headless=True)
        page = browser.new_page()
        rows = "".join(f"<tr><td>星期{i}</td><td>09:00–18:00</td></tr>" for i in range(7))
        hours_control = '<button data-item-id="oh">營業時間</button>' if hours_layout == "legacy" else '''<div onclick="document.querySelector('table').hidden=false"><span aria-label="顯示本週營業時間" role="img">展開</span></div>'''
        table_hidden = '' if hours_layout == "legacy" else 'hidden'
        page.set_content('''<main role="main"><h1 class="DUwDvf">測試店</h1>
          <span role="img" aria-label="4.8 顆星"></span>
          <button aria-label="1,234 則評論">評論</button>
          <button data-item-id="address"><span class="Io6YTe">測試地址</span></button>
          <button data-item-id="phone:tel:123"><span class="Io6YTe">123</span></button>
          <a data-item-id="authority" href="https://example.com">網站</a>
          ''' + hours_control + '<table class="eK4R0e" ' + table_hidden + '>' + rows + '</table></main>')
        scraper = object.__new__(MapsScraper)
        monkeypatch.setattr(scraper, "_capture_overview_cover", lambda *args: None)
        result = scraper._extract_overview(page, ShopConfig("店", "https://www.google.com/maps?cid=123"))
        assert result["name"] == "測試店"
        assert result["rating"] == 4.8 and result["review_count"] == 1234
        assert result["address"] == "測試地址" and result["phone"] == "123"
        assert result["website"] == "https://example.com/"
        assert len(result["hours"]) == 7
        browser.close()


def test_cover_download_is_immutable_and_rejects_non_google(tmp_path):
    import httpx
    from io import BytesIO
    from PIL import Image
    from maps_review_monitor.scraper import MapsScraper
    image = BytesIO()
    Image.new("RGB", (3, 3), "red").save(image, format="PNG")
    data = image.getvalue()
    requested = []
    def respond(request):
        requested.append(str(request.url))
        return httpx.Response(200, content=data, headers={"content-type": "image/png"})
    scraper = object.__new__(MapsScraper)
    scraper.settings = SimpleNamespace(data_dir=tmp_path / "data", root=tmp_path)
    scraper.http = httpx.Client(transport=httpx.MockTransport(respond))
    try:
        assert scraper._capture_overview_cover("https://example.com/a", "shop") is None
        first = scraper._capture_overview_cover("https://lh3.googleusercontent.com/p/a=w400-h200-k-no", "shop")
        second = scraper._capture_overview_cover("https://lh3.googleusercontent.com/p/a=w900-h700-k-no", "shop")
        assert first == second and first
        assert (tmp_path / first["path"]).read_bytes() == data
        assert requested == ["https://lh3.googleusercontent.com/p/a=w800-h600-k-no"] * 2
    finally:
        scraper.http.close()
