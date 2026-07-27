from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
from zoneinfo import ZoneInfo


PAGE_SIZE = 10
TAIPEI = ZoneInfo("Asia/Taipei")
ORDER_SQL = """
CASE WHEN r.back_calculated_at IS NULL AND r.estimated_posted_date IS NULL THEN 1 ELSE 0 END,
COALESCE(
  julianday(r.back_calculated_at),
  julianday(r.estimated_posted_date || ' 12:00:00')
) DESC,
r.first_seen_at DESC
"""


def short_shop_name(name: str) -> str:
    return (
        name.replace("鬆肌LAY 運動筋膜放鬆 ", "")
        .replace("（預約制）", "")
        .replace("(預約制)", "")
        .strip()
    )


def local_timestamp(value: str | None) -> str:
    if not value:
        return "—"
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(TAIPEI).strftime("%Y-%m-%d %H:%M:%S")
    except ValueError:
        return value


def time_columns(row: sqlite3.Row) -> tuple[str, str]:
    status = row["time_parse_status"] or "pending"
    if row["back_calculated_at"]:
        return local_timestamp(row["back_calculated_at"]), "—"
    if row["estimated_posted_date"]:
        return "—", row["estimated_posted_date"]
    if status == "long":
        return "—", "觀察中"
    if status == "unparsed":
        return "無法解析", "無法解析"
    if status == "pending":
        return "尚未更新", "尚未更新"
    return "—", "—"


def format_stars(value: object) -> str:
    if value is None:
        return "未知"
    return f"{float(value):g}"


def _where_parameters(shop_key: str | None, keyword: str) -> tuple[str, list[object]]:
    filters: list[str] = []
    params: list[object] = []
    if shop_key:
        filters.append("r.shop_key=?")
        params.append(shop_key)
    if keyword:
        filters.append(
            "(json_extract(r.snapshot_json,'$.author') LIKE ? "
            "OR json_extract(r.snapshot_json,'$.text') LIKE ?)"
        )
        pattern = f"%{keyword}%"
        params.extend([pattern, pattern])
    return (" WHERE " + " AND ".join(filters) if filters else ""), params


def fetch_page(
    connection: sqlite3.Connection,
    shop_key: str | None,
    keyword: str,
    page: int,
) -> tuple[list[sqlite3.Row], int]:
    where, params = _where_parameters(shop_key, keyword)
    total = connection.execute("SELECT COUNT(*) FROM reviews r" + where, params).fetchone()[0]
    sql = """
        SELECT s.name,r.snapshot_json,r.first_seen_at,r.back_calculated_at,
               r.estimated_posted_date,r.time_parse_status
        FROM reviews r JOIN shops s ON s.shop_key=r.shop_key
    """ + where + " ORDER BY " + ORDER_SQL + " LIMIT ? OFFSET ?"
    rows = list(connection.execute(sql, [*params, PAGE_SIZE, page * PAGE_SIZE]))
    return rows, int(total)


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _native_where(shop_key: str | None, keyword: str) -> str:
    filters: list[str] = []
    if shop_key:
        filters.append(f"r.shop_key={_sql_literal(shop_key)}")
    if keyword:
        pattern = _sql_literal(f"%{keyword}%")
        filters.append(
            "(json_extract(r.snapshot_json,'$.author') LIKE " + pattern
            + " OR json_extract(r.snapshot_json,'$.text') LIKE " + pattern + ")"
        )
    return " WHERE " + " AND ".join(filters) if filters else ""


def native_queries(shop_key: str | None, keyword: str, page: int) -> tuple[str, str]:
    where = _native_where(shop_key, keyword)
    offset = page * PAGE_SIZE
    common = f"""
WITH filtered AS (
  SELECT
    replace(replace(s.name,'鬆肌LAY 運動筋膜放鬆 ',''),'（預約制）','') AS shop_name,
    coalesce(json_extract(r.snapshot_json,'$.author'),'—') AS author,
    CASE
      WHEN json_extract(r.snapshot_json,'$.stars') IS NULL THEN '未知'
      ELSE printf('%g',json_extract(r.snapshot_json,'$.stars'))
    END AS stars,
    coalesce(json_extract(r.snapshot_json,'$.time_text'),'—') AS review_time,
    trim(replace(replace(
      coalesce(nullif(json_extract(r.snapshot_json,'$.text'),''),'（無文字評論）'),
      char(13),' '),char(10),' ')) AS content,
    r.first_seen_at,r.back_calculated_at,r.estimated_posted_date,r.time_parse_status,
    row_number() OVER (ORDER BY {ORDER_SQL}) AS row_no
  FROM reviews r JOIN shops s ON s.shop_key=r.shop_key
  {where}
)
"""
    summary = common + f"""
SELECT
  row_no AS '編號',
  shop_name AS '店家',
  CASE WHEN length(author)>12 THEN substr(author,1,12)||'…' ELSE author END AS '評論者',
  stars AS '星等',
  review_time AS '評論時間',
  CASE WHEN length(content)>24 THEN substr(content,1,24)||'……' ELSE content END AS '評論摘要'
FROM filtered
WHERE row_no>{offset} AND row_no<={offset + PAGE_SIZE}
ORDER BY row_no;
"""
    times = common + f"""
SELECT
  row_no AS '編號',
  strftime('%Y-%m-%d %H:%M:%S',first_seen_at,'+8 hours') AS '首次偵測時間',
  CASE
    WHEN back_calculated_at IS NOT NULL
      THEN strftime('%Y-%m-%d %H:%M:%S',back_calculated_at,'+8 hours')
    WHEN time_parse_status='unparsed' THEN '無法解析'
    WHEN time_parse_status='pending' THEN '尚未更新'
    ELSE '—'
  END AS '回推發文時間',
  CASE
    WHEN back_calculated_at IS NOT NULL THEN '—'
    WHEN estimated_posted_date IS NOT NULL THEN estimated_posted_date
    WHEN time_parse_status='long' THEN '觀察中'
    WHEN time_parse_status='unparsed' THEN '無法解析'
    WHEN time_parse_status='pending' THEN '尚未更新'
    ELSE '—'
  END AS '預估發文時間'
FROM filtered
WHERE row_no>{offset} AND row_no<={offset + PAGE_SIZE}
ORDER BY row_no;
"""
    return summary, times


def render_native_table(database_path: Path, shop_key: str | None, keyword: str, page: int) -> None:
    executable = shutil.which("sqlite3")
    if not executable:
        raise RuntimeError("找不到 sqlite3 指令，請先執行 sudo apt install sqlite3")
    labels = ("評論摘要", "時間資訊")
    for label, query in zip(labels, native_queries(shop_key, keyword, page)):
        print(f"\n【{label}】", flush=True)
        result = subprocess.run(
            [executable, "-cmd", ".headers on", "-cmd", ".mode box", str(database_path), query],
            check=False,
        )
        if result.returncode:
            raise RuntimeError(f"sqlite3 表格輸出失敗（結束碼 {result.returncode}）")


def show_detail(row: sqlite3.Row, number: int) -> None:
    item = json.loads(row["snapshot_json"])
    back, estimated = time_columns(row)
    print(f"\n完整評論 #{number}")
    print("=" * 60)
    print(f"店家：{short_shop_name(row['name'])}")
    print(f"評論者：{item.get('author') or '—'}")
    print(f"星等：{format_stars(item.get('stars'))}")
    print(f"評論時間：{item.get('time_text') or '—'}")
    print(f"首次偵測時間：{local_timestamp(row['first_seen_at'])}")
    print(f"回推發文時間：{back}")
    print(f"預估發文時間：{estimated}")
    print("評論內容：")
    print(item.get("text") or "（無文字評論）")
    print("=" * 60)


def browse(
    connection: sqlite3.Connection,
    database_path: Path,
    shop_key: str | None,
    keyword: str,
) -> None:
    page = 0
    while True:
        rows, total = fetch_page(connection, shop_key, keyword, page)
        pages = max(1, math.ceil(total / PAGE_SIZE))
        if page >= pages:
            page = pages - 1
            continue
        print()
        if rows:
            render_native_table(database_path, shop_key, keyword, page)
        else:
            print("找不到符合條件的評論。")
        print(f"第 {page + 1}/{pages} 頁，共 {total} 筆")
        command = input(
            "[N] 下一頁  [P] 上一頁  [V 編號] 完整評論  [Q] 返回店家選單："
        ).strip().lower()
        if command == "n" and page + 1 < pages:
            page += 1
        elif command == "p" and page > 0:
            page -= 1
        elif command in {"q", "0"}:
            return
        else:
            match = re.fullmatch(r"v\s*(\d+)", command)
            if match:
                number = int(match.group(1))
                first_number = page * PAGE_SIZE + 1
                if first_number <= number < first_number + len(rows):
                    show_detail(rows[number - first_number], number)
                    input("按 Enter 返回列表...")
                else:
                    print("編號不在目前頁面中。")


def run_browser(database_path: Path) -> None:
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(reviews)")}
        required = {"back_calculated_at", "estimated_posted_date", "time_parse_status"}
        if not required.issubset(columns):
            print("資料庫尚未升級，請先執行一次 maps-review-monitor check。")
            return
        shops = list(connection.execute("SELECT shop_key,name FROM shops ORDER BY rowid"))
        while True:
            print("\n店家選擇")
            print("----------")
            for number, shop in enumerate(shops, 1):
                print(f"{number}. {short_shop_name(shop['name'])}")
            all_number = len(shops) + 1
            print(f"{all_number}. 全部")
            print("0. 返回主選單")
            choice = input(f"請輸入選項 [0-{all_number}]：").strip()
            if choice in {"0", "q", "Q"}:
                return
            if not choice.isdigit() or not 1 <= int(choice) <= all_number:
                print("輸入錯誤，請重新選擇。")
                continue
            selected = None if int(choice) == all_number else shops[int(choice) - 1]["shop_key"]
            keyword = input("搜尋關鍵字（直接 Enter 顯示全部）：").strip()
            browse(connection, database_path, selected, keyword)
    finally:
        connection.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="互動式評論查詢")
    parser.add_argument("database", type=Path)
    args = parser.parse_args(argv)
    run_browser(args.database)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
