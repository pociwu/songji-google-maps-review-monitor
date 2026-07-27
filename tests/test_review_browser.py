import json
import sqlite3

from maps_review_monitor.review_browser import fetch_page, native_queries, run_browser, short_shop_name


def browser_database(path):
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE shops(shop_key TEXT PRIMARY KEY,name TEXT);
        CREATE TABLE reviews(
          shop_key TEXT,review_id TEXT,snapshot_json TEXT,first_seen_at TEXT,
          back_calculated_at TEXT,estimated_posted_date TEXT,time_parse_status TEXT
        );
        """
    )
    shops = [
        ("s1", "鬆肌LAY 運動筋膜放鬆 麥寮店"),
        ("s2", "鬆肌LAY 運動筋膜放鬆 虎尾店"),
        ("s3", "鬆肌LAY 運動筋膜放鬆 斗六店"),
    ]
    connection.executemany("INSERT INTO shops VALUES(?,?)", shops)
    rows = []
    for number in range(12):
        snapshot = json.dumps({
            "author": f"評論者{number}", "stars": 5, "time_text": "17 分鐘前",
            "text": "完整評論內容測試",
        }, ensure_ascii=False)
        rows.append((
            shops[number % 3][0], str(number), snapshot,
            "2026-07-19T06:40:35+00:00", "2026-07-19T14:23:35+08:00", None, "short",
        ))
    connection.executemany("INSERT INTO reviews VALUES(?,?,?,?,?,?,?)", rows)
    connection.commit()
    connection.close()


def test_fetch_page_filters_shop_and_keyword(tmp_path):
    path = tmp_path / "reviews.sqlite3"
    browser_database(path)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    rows, total = fetch_page(connection, "s1", "評論者", 0)
    connection.close()
    assert total == 4
    assert len(rows) == 4
    assert all(short_shop_name(row["name"]) == "麥寮店" for row in rows)


def test_native_queries_use_summary_and_escape_keyword(tmp_path):
    path = tmp_path / "reviews.sqlite3"
    browser_database(path)
    summary_sql, time_sql = native_queries("s1", "O'Reilly", 1)
    assert "substr(content,1,24)" in summary_sql
    assert "row_no>10 AND row_no<=20" in summary_sql
    assert "O''Reilly" in summary_sql
    connection = sqlite3.connect(path)
    connection.execute(summary_sql).fetchall()
    connection.execute(time_sql).fetchall()
    connection.close()


def test_native_query_returns_numbered_summary(tmp_path):
    path = tmp_path / "reviews.sqlite3"
    browser_database(path)
    connection = sqlite3.connect(path)
    summary_sql, time_sql = native_queries(None, "", 0)
    rows = connection.execute(summary_sql).fetchall()
    time_rows = connection.execute(time_sql).fetchall()
    connection.close()
    assert len(rows) == 10
    assert rows[0][0] == 1
    assert rows[0][1] in {"麥寮店", "虎尾店", "斗六店"}
    assert len(rows[0][5]) <= 26
    assert len(time_rows) == 10
    assert len(time_rows[0]) == 4


def test_interactive_browser_pages_and_returns(tmp_path, monkeypatch, capsys):
    path = tmp_path / "reviews.sqlite3"
    browser_database(path)
    rendered_pages = []
    monkeypatch.setattr(
        "maps_review_monitor.review_browser.render_native_table",
        lambda _path, _shop, _keyword, page: rendered_pages.append(page),
    )
    answers = iter(["4", "", "v 1", "", "n", "p", "q", "0"])
    monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))
    run_browser(path)
    output = capsys.readouterr().out
    assert "1. 麥寮店" in output
    assert "4. 全部" in output
    assert "第 1/2 頁，共 12 筆" in output
    assert "第 2/2 頁，共 12 筆" in output
    assert "完整評論 #1" in output
    assert rendered_pages == [0, 0, 1, 0]
