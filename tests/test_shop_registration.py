from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tomllib

import httpx
import pytest
from fastapi.testclient import TestClient

from maps_review_monitor.config import load_settings
from maps_review_monitor.portal import create_app
from maps_review_monitor.shop_registration import (
    RegistrationError, ResolvedShop, add_shop, normalize_shop_url, resolve_shop_url,
)


def config_at(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(
        '# keep this comment\nscan_limit = 50\n[[shops]]\nname = "原店"\n'
        'url = "https://www.google.com/maps?cid=123"\nenabled = true\n',
        encoding="utf-8",
    )
    return path


def test_share_google_search_redirect_becomes_maps_preview():
    visited = []

    def respond(request):
        visited.append(str(request.url))
        if request.url.host == "share.google":
            return httpx.Response(302, headers={"location": "https://www.google.com/share.google?q=example"})
        return httpx.Response(302, headers={"location": str(httpx.URL(
            "https://www.google.com/search", params={"kgmid": "/g/11nvbf2lfj", "q": "月沐 夜間洗髮 麥寮店", "utm_source": "share"},
        ))})

    result = resolve_shop_url("https://share.google/wfKzhfSewaEaYWlTn", transport=httpx.MockTransport(respond))
    assert result.name == "月沐 夜間洗髮 麥寮店"
    assert result.url.startswith("https://www.google.com/maps/search/?api=1&query=")
    assert "utm_source" not in result.url
    assert "確認" in result.note
    assert len(visited) == 2


def test_maps_short_link_gets_stable_cid():
    result = resolve_shop_url("https://maps.app.goo.gl/example", transport=httpx.MockTransport(
        lambda request: httpx.Response(302, headers={
            "location": "https://www.google.com/maps/place/Test/data=!4m2!3m1!1s0x123:0xabc?hl=zh-TW&entry=ttu",
        })
    ))
    assert result.url == "https://www.google.com/maps?cid=2748"
    assert result.name == "Test"


def test_google_html_refresh_supported():
    result = resolve_shop_url("https://share.google/example", transport=httpx.MockTransport(
        lambda request: httpx.Response(200, text='<meta http-equiv="refresh" content="0;url=https://www.google.com/maps?cid=456">')
    ))
    assert result.url.endswith("cid=456")


@pytest.mark.parametrize("url", [
    "http://share.google/example", "https://google.com.evil.test/maps?cid=1",
    "https://127.0.0.1/maps?cid=1", "https://www.google.com:444/maps?cid=1",
    "https://user:pass@www.google.com/maps?cid=1", "https://evilgoogle.com/maps?cid=1",
])
def test_untrusted_urls_rejected_before_network(url):
    def no_network(request):
        pytest.fail("untrusted URL contacted")
    with pytest.raises(RegistrationError):
        resolve_shop_url(url, transport=httpx.MockTransport(no_network))


def test_redirect_to_private_network_never_contacted():
    visited = []
    def respond(request):
        visited.append(request.url.host)
        return httpx.Response(302, headers={"location": "https://127.0.0.1/private"})
    with pytest.raises(RegistrationError):
        resolve_shop_url("https://share.google/example", transport=httpx.MockTransport(respond))
    assert visited == ["share.google"]


def test_loop_and_unknown_google_search_rejected():
    with pytest.raises(RegistrationError):
        resolve_shop_url("https://share.google/example", transport=httpx.MockTransport(
            lambda request: httpx.Response(302, headers={"location": str(request.url)})
        ))
    with pytest.raises(RegistrationError):
        resolve_shop_url("https://www.google.com/search?q=coffee")
    with pytest.raises(RegistrationError):
        normalize_shop_url("https://www.google.com/maps/contrib/123/reviews")


def test_atomic_append_preserves_original_and_backup(tmp_path):
    config = config_at(tmp_path)
    original = config.read_bytes()
    old_key = load_settings(config).shops[0].key
    shop = add_shop(config, name='新店「甲」 "分店"', url="https://www.google.com/maps?cid=456&hl=zh-TW")
    assert config.read_bytes().startswith(original)
    assert config.with_name("config.toml.before-add.bak").read_bytes() == original
    current = load_settings(config)
    assert current.shops[0].key == old_key
    assert current.shops[1] == shop
    assert current.scan_limit == 50
    assert shop.url == "https://www.google.com/maps?cid=456"
    with pytest.raises(RegistrationError, match="已在"):
        add_shop(config, name="再次加入", url="https://www.google.com/maps/place/Other/data=!1s0x1:0x1c8")
    assert len(load_settings(config).shops) == 2


def test_limit_and_disabled_duplicate_preserve_file(tmp_path):
    config = config_at(tmp_path)
    for index in range(9):
        add_shop(config, name=f"店 {index}", url=f"https://www.google.com/maps?cid={500+index}")
    before = config.read_bytes()
    with pytest.raises(RegistrationError, match="10"):
        add_shop(config, name="第十一店", url="https://www.google.com/maps?cid=999")
    assert config.read_bytes() == before
    config.write_bytes(before.replace(b'enabled = true', b'enabled = false', 1))
    with pytest.raises(RegistrationError, match="尚未啟用"):
        add_shop(config, name="原店", url="https://www.google.com/maps?cid=123")


def test_concurrent_adds_do_not_lose_entries(tmp_path):
    config = config_at(tmp_path)
    def save(index):
        try:
            return add_shop(config, name=f"店 {index}", url=f"https://www.google.com/maps?cid={700+index}")
        except RuntimeError:
            return None  # busy is a safe retryable result
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(save, [1, 2]))
    raw = tomllib.loads(config.read_text(encoding="utf-8"))
    assert len(raw["shops"]) == 1 + sum(result is not None for result in results)


HEADERS = {"Origin": "http://testserver", "X-Requested-With": "SongjiPortal"}


def test_web_add_preview_then_immediate_home_and_next_monitor_config(tmp_path, monkeypatch):
    config = config_at(tmp_path)
    client = TestClient(create_app(str(config)))
    monkeypatch.setattr("maps_review_monitor.portal.resolve_shop_url", lambda url: ResolvedShop(
        "https://www.google.com/maps?cid=456", "新店", "請確認店家",
    ))
    assert 'id="add-shop-form"' in client.get("/").text
    preview = client.post("/api/shops/resolve", json={"url": "https://share.google/example"}, headers=HEADERS)
    assert preview.status_code == 200
    assert len(load_settings(config).shops) == 1  # preview does not register
    saved = client.post("/api/shops", json={"name": "新店", "token": preview.json()["token"]}, headers=HEADERS)
    assert saved.status_code == 201
    assert len(load_settings(config).shops) == 2
    home = client.get("/")
    assert "新店" in home.text
    assert saved.json()["shop_key"] in home.text
    detail = client.get("/shops/" + saved.json()["shop_key"])
    assert detail.status_code == 200
    assert "尚未完成首次查詢" in detail.text
    duplicate = client.post("/api/shops", json={"name": "新店", "token": preview.json()["token"]}, headers=HEADERS)
    assert duplicate.status_code == 409


def test_web_rejects_cross_origin_and_forged_preview(tmp_path):
    config = config_at(tmp_path)
    client = TestClient(create_app(str(config)))
    before = config.read_bytes()
    for headers in ({}, {**HEADERS, "Origin": "http://evil.test"}):
        assert client.post("/api/shops/resolve", json={"url": "https://share.google/example"}, headers=headers).status_code == 403
    assert client.post("/api/shops", json={"name": "新店", "token": 'fake.{"url":"https://www.google.com/maps?cid=456"}'}, headers=HEADERS).status_code == 422
    assert config.read_bytes() == before
