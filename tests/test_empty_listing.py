from pathlib import Path
from types import SimpleNamespace

import pytest
from playwright.sync_api import sync_playwright

from maps_review_monitor.scraper import MapsScraper, ScrapeError


@pytest.fixture
def browser_page():
    chrome = Path('C:/Program Files/Google/Chrome/Application/chrome.exe')
    with sync_playwright() as pw:
        kwargs = {"executable_path": str(chrome)} if chrome.exists() else {}
        browser = pw.chromium.launch(headless=True, **kwargs)
        yield browser.new_page()
        browser.close()


EMPTY_PLACE = '''<main role="main"><h1 class="DUwDvf">串門子</h1>
<button role="tab">總覽</button><button role="tab">簡介</button>
<button data-item-id="address"><span class="Io6YTe">雲林縣麥寮鄉豐安路83-1號</span></button>
<button aria-label="撰寫評論">撰寫評論</button></main>'''


def scraper_stub():
    scraper = object.__new__(MapsScraper)
    scraper.settings = SimpleNamespace(scroll_delay_min_seconds=0, scroll_delay_max_seconds=0)
    scraper._scroll_place_panel = lambda page: None
    scraper._page_random_wait = lambda *args: None
    return scraper


def test_unrated_place_reports_pending_not_scrape_failure(browser_page):
    browser_page.set_content(EMPTY_PLACE)
    scraper = scraper_stub()
    # Must distinguish a complete unrated place from a broken review page.
    try:
        scraper._open_reviews(browser_page)
    except ScrapeError as exc:
        assert type(exc).__name__ == 'ReviewsUnavailable'
    else:
        pytest.fail('Expected a pending-review state, not a successful empty scrape')


@pytest.mark.parametrize('extra', [
    '<span role="img" aria-label="4.5 顆星">4.5</span>',
    '<button aria-label="12 則評論">12 則評論</button>',
    '<p>Google 地圖受限</p>',
])
def test_rated_or_blocked_page_is_not_pending(browser_page, extra):
    browser_page.set_content(EMPTY_PLACE.replace('</main>', extra + '</main>'))
    scraper = scraper_stub()
    if '受限' in extra:
        with pytest.raises(ScrapeError) as exc:
            scraper._open_reviews(browser_page)
        assert type(exc.value).__name__ != 'ReviewsUnavailable'
    else:
        assert not scraper._unrated_place_without_reviews(browser_page)


def test_incomplete_place_is_not_pending(browser_page):
    browser_page.set_content('<button aria-label="撰寫評論">撰寫評論</button>')
    with pytest.raises(ScrapeError) as exc:
        scraper_stub()._open_reviews(browser_page)
    assert type(exc.value).__name__ != 'ReviewsUnavailable'


def test_pending_does_not_initialize_or_hide_previously_known_shop(tmp_path):
    from maps_review_monitor.database import Database
    db = Database(tmp_path / 'reviews.sqlite3')
    try:
        db.sync_shop('new', '新店', 'https://www.google.com/maps?cid=123')
        for _ in range(3):
            db.mark_shop_failure('new')
        assert db.mark_shop_waiting_reviews('new')
        assert not db.is_initialized('new')
        status = next(db.iter_shop_statuses())
        assert status['last_result'] == 'waiting_reviews'
        assert status['consecutive_failures'] == 0
        assert status['last_success_at'] is None
        db.mark_shop_success('new')
        assert not db.mark_shop_waiting_reviews('new')
    finally:
        db.close()
