from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys

from .admin import create_backup, export_reviews, restore_backup
from .config import load_settings
from .database import Database
from .lock import process_lock
from .logging_setup import configure_logging
from .monitor import run_check
from .notifications import TelegramClient


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(prog="maps-review-monitor", description="Google Maps 評論監控")
    root.add_argument("--config", default="config.toml", help="TOML 設定檔")
    root.add_argument("--verbose", action="store_true")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="首次建立評論基準（不發送通知）")
    commands.add_parser("check", help="檢查評論並發送通知")
    commands.add_parser("doctor", help="檢查設定、資料庫、瀏覽器與 Telegram")
    listing = commands.add_parser("list-reviews", help="列出最近評論")
    listing.add_argument("--limit", type=int, default=20)
    export = commands.add_parser("export", help="匯出 CSV 或 JSON")
    export.add_argument("--format", choices=("csv", "json"), required=True)
    export.add_argument("--output", required=True)
    export.add_argument("--shop-key")
    backup = commands.add_parser("backup", help="建立一致性備份")
    backup.add_argument("--output")
    restore = commands.add_parser("restore", help="還原備份")
    restore.add_argument("archive")
    restore.add_argument("--force", action="store_true")
    commands.add_parser("interactive-login", help="手動處理 Google 同意或登入頁")
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        require_telegram = args.command == "check"
        settings = load_settings(args.config, require_telegram=require_telegram)
        configure_logging(settings.log_dir, args.verbose)
        if args.command in {"init", "check"}:
            with process_lock(settings.root / ".monitor.lock"):
                stats = run_check(settings, baseline_only=args.command == "init")
            print(json.dumps(stats, ensure_ascii=False, indent=2))
            return 1 if stats["shops_failed"] else 0
        if args.command == "doctor":
            return doctor(settings)
        if args.command == "list-reviews":
            db = Database(settings.database_path, settings.timezone)
            try:
                for item in list(db.iter_reviews())[: args.limit]:
                    print(f"{item['first_seen_at']} | {item['shop_name']} | {item['stars']}★ | {item['author']} | {item['text'][:80]}")
            finally:
                db.close()
            return 0
        if args.command == "export":
            count = export_reviews(settings, Path(args.output), args.format, args.shop_key)
            print(f"已匯出 {count} 則評論至 {Path(args.output).resolve()}")
            return 0
        if args.command == "backup":
            output = create_backup(settings, Path(args.output) if args.output else None)
            print(f"備份完成：{output.resolve()}")
            return 0
        if args.command == "restore":
            restore_backup(settings, Path(args.archive), args.force)
            print("還原完成")
            return 0
        if args.command == "interactive-login":
            return interactive_login(settings)
    except Exception as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 2
    return 0


def doctor(settings) -> int:
    failures = 0
    print("✓ 設定檔有效")
    try:
        db = Database(settings.database_path, settings.timezone)
        db.close()
        print("✓ SQLite 可讀寫")
    except Exception as exc:
        print(f"✗ SQLite：{exc}")
        failures += 1
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            kwargs = {"headless": True}
            if settings.browser_channel:
                kwargs["channel"] = settings.browser_channel
            if settings.browser_executable:
                kwargs["executable_path"] = str(Path(settings.browser_executable).expanduser())
            browser = pw.chromium.launch(**kwargs)
            browser.close()
        print("✓ Chromium 可啟動")
    except Exception as exc:
        print(f"✗ Chromium：{exc}")
        failures += 1
    if settings.telegram_bot_token and settings.telegram_chat_id:
        try:
            client = TelegramClient(settings.telegram_bot_token, settings.telegram_chat_id)
            bot = client.verify()
            chat = client.verify_chat()
            client.close()
            chat_name = chat.get("title") or chat.get("username") or chat.get("id", "")
            print(f"✓ Telegram Bot：@{bot.get('username', '')}；目標：{chat_name}")
        except Exception as exc:
            print(f"✗ Telegram：{exc}")
            failures += 1
    else:
        print("△ Telegram 尚未設定（建立 Bot 後再重跑 doctor）")
    return 1 if failures else 0


def interactive_login(settings) -> int:
    from playwright.sync_api import sync_playwright
    profile = settings.browser_profile_dir
    profile.mkdir(parents=True, exist_ok=True)
    if settings.browser_executable:
        executable = Path(settings.browser_executable).expanduser()
    else:
        with sync_playwright() as pw:
            executable = Path(pw.chromium.executable_path)
    if not executable.exists():
        raise RuntimeError(f"找不到瀏覽器執行檔：{executable}")
    print("即將以一般瀏覽器模式開啟專用設定檔。")
    print("請完成 Google 登入，確認 Maps 顯示帳戶頭像，再關閉所有這個瀏覽器的視窗。")
    result = subprocess.run([
        str(executable), f"--user-data-dir={profile.resolve()}", "--no-first-run", settings.shops[0].url
    ], check=False)
    if result.returncode not in (0, None):
        raise RuntimeError(f"瀏覽器結束碼：{result.returncode}")
    print(f"專用瀏覽器設定檔已保存：{profile.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
