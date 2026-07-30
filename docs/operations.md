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

## 相似評論分析

分析只使用評論者全文，不把店家回覆或人工貼文納入分數。預設門檻為中文字符相似度 85% 或語意相似度 92%；低於 12 個有效中文字的短評，只有同一分析範圍內至少 3 則完全相同才會納入。

監控批次結束後會自動分析，也可手動重跑所有歷史資料：

```bash
cd /opt/maps-review-monitor
conda run --no-capture-output -n maps-review-monitor \
  maps-review-monitor --config /opt/maps-review-monitor/config.toml analyze
```

首次執行會下載 `intfloat/multilingual-e5-small`。分析使用一個 CPU 執行緒並繼承 systemd 的 `Nice=10`；OCI 降為 2 CPU 時不需調整。若模型載入或分析失敗，新結果不會成為目前快照，監控與 Telegram 仍照常完成。

排除規則：

```bash
cp data/portal/review-analysis.example.yaml data/portal/review-analysis.yaml
```

可排除特定評論、群組指紋或常見固定用語。每筆規則都必須填寫 `reason`，原始評論與舊分析快照仍會保存。

查看最近狀態：

```bash
sqlite3 data/reviews.sqlite3 \
  "SELECT id,status,stage,percent,processed,total,error,started_at,completed_at FROM analysis_runs ORDER BY id DESC LIMIT 5;"
```

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
## 店家展示頁

展示頁與評論監控使用相同的 Python 環境與資料目錄，但為獨立 systemd 服務。它讀取評論、監控狀況、完整成功的分析快照和 `data/portal/content.yaml`。

首次部署或程式更新後，請在專案根目錄執行：

```bash
sudo cp deploy/systemd/maps-review-portal.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now maps-review-portal.service
systemctl status maps-review-portal.service
```

服務監聽 `0.0.0.0:8088`。請使用主機的 Tailscale IP 或 MagicDNS 名稱瀏覽；網站本身沒有登入機制，Tailnet ACL 是唯一存取邊界。

### 編輯內容素材

```bash
mkdir -p data/portal/<店家代號>
cp data/portal/content.example.yaml data/portal/content.yaml
```

在 `content.yaml` 的每筆資料需提供 `id`、`shop_key`、`published_at`、`title` 和 `body`。`photos` 是相對於專案根目錄的本機媒體路徑；`videos` 可放本機影片路徑或外部影片 URL。修改 YAML 或新增媒體後，重新整理網頁即可看到結果，不需要重啟服務。

驗證 YAML 是否可載入：

```bash
python -c "from pathlib import Path; from maps_review_monitor.portal import load_content_assets; print(load_content_assets(Path('data/portal/content.yaml')))"
```
