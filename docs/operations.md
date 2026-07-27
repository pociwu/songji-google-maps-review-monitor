# 維運手冊

## 確認 Ubuntu 排程

在實際執行監控的 Ubuntu 主機上執行：

```bash
sudo systemctl status maps-review-monitor.timer
sudo systemctl list-timers --all maps-review-monitor.timer
```

正常狀態是 timer 顯示 `active (waiting)`，且 `list-timers` 的 `NEXT` 欄位有下次執行時間。`maps-review-monitor.service` 是 `oneshot` 服務，完成後顯示 `inactive (dead)` 屬正常狀態。

若顯示 `enabled` 但 timer 為 `inactive`，代表它會在下次開機啟用、但目前沒有排程；重新啟用：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now maps-review-monitor.timer
sudo systemctl status maps-review-monitor.timer
```

## 檢查日誌與手動測試

檢視最近的 timer 與監控服務紀錄：

```bash
sudo journalctl -u maps-review-monitor.timer -u maps-review-monitor.service \
  --since "7 days ago" --no-pager
```

手動觸發一次監控並立即查看結果：

```bash
sudo systemctl start maps-review-monitor.service
sudo systemctl status maps-review-monitor.service --no-pager
sudo journalctl -u maps-review-monitor.service -n 100 --no-pager
```

服務失敗時，優先確認：

- `deploy/systemd/maps-review-monitor.service` 的 `User`、`WorkingDirectory` 與 Conda 路徑是否正確。
- `config.toml`、`.env` 與 `data/chromium-profile` 是否屬於服務使用者且可讀寫。
- `maps-review-monitor doctor` 是否能通過 Chromium 與 Telegram 檢查。
- `logs/` 與 `debug/` 是否有 Google Maps 頁面或登入相關紀錄。

## 確認 Windows 排程

```powershell
Get-ScheduledTask -TaskName MapsReviewMonitor
Get-ScheduledTaskInfo -TaskName MapsReviewMonitor
```

需要重新建立時：

```powershell
.\deploy\windows\install-task.ps1 -ProjectPath "C:\path\to\songji-google-maps-review-monitor"
```

## 備份與還原

建立備份：

```bash
maps-review-monitor backup
```

備份 ZIP 包含 SQLite 資料庫、設定檔與環境設定。還原會覆寫既有資料，請確認目標主機的設定後再執行：

```bash
maps-review-monitor restore backups/maps-review-monitor-YYYYMMDD-HHMMSS.zip --force
```

切換到另一台排程主機前，先停用原主機 timer 或工作排程器，再備份、傳送 ZIP 並還原，以避免兩個監控程序同時寫入同一組資料。
