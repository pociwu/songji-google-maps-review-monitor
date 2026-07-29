# Songji Google Maps 評論監控工具

以 Python 與 Playwright 監控公開的 Google Maps 店家評論，將新增評論、內容修改、店家回覆異動與連續監控失敗通知至 Telegram 群組。資料會保存於本機 SQLite，並可匯出與備份。

## 功能

- 最多監控 10 間 Google Maps 店家。
- 偵測新增評論、評論內容更新、店家回覆新增或移除。
- 保存評論快照與觀測紀錄，支援推估相對時間評論的發布日期。
- 將通知拆分、佇列化並於失敗時以指數退避重試。
- 提供 SQLite、Chromium、Telegram 設定檢查，以及 CSV／JSON 匯出與 ZIP 備份還原。
- 支援 Ubuntu 的 systemd timer 與 Windows 工作排程器。

## 需求

- Python 3.11 以上
- Conda（建議）
- Chromium 或 Google Chrome
- Telegram Bot Token 與接收通知的 Chat ID

## 快速開始

```bash
git clone <你的私有儲存庫網址>
cd songji-google-maps-review-monitor
conda env create -f environment.yml
conda activate maps-review-monitor
python -m playwright install chromium
cp config.example.toml config.toml
cp .env.example .env
```

編輯 `config.toml`，為每間店家新增一個 `[[shops]]` 區塊：

```toml
[[shops]]
name = "店家名稱"
url = "https://www.google.com/maps/place/..."
enabled = true
```

再將 Telegram 憑證填入 `.env`：

```dotenv
TELEGRAM_BOT_TOKEN=123456:token
TELEGRAM_CHAT_ID=-1001234567890
```

`.env`、`config.toml`、資料庫與瀏覽器登入資料均已在 `.gitignore` 中排除，不應提交至 Git。

## 初始化與執行

先確認環境：

```bash
maps-review-monitor doctor
```

初次建立基準資料，不會發送既有評論通知：

```bash
maps-review-monitor init
```

後續每次監控使用：

```bash
maps-review-monitor check
```

Google 需要登入才能查看完整內容時，執行下列指令登入後關閉瀏覽器；登入狀態會保存於 `data/chromium-profile`：

```bash
maps-review-monitor interactive-login
```

## 常用指令

```bash
maps-review-monitor list-reviews --limit 20
maps-review-monitor export --format csv --output reviews.csv
maps-review-monitor export --format json --output reviews.json
maps-review-monitor backup
maps-review-monitor restore backups/maps-review-monitor-YYYYMMDD-HHMMSS.zip --force
```

## 排程

### Ubuntu systemd

請先檢查 `deploy/systemd/maps-review-monitor.service` 中的使用者、Conda 路徑與專案路徑，再安裝 timer：

```bash
sudo cp deploy/systemd/maps-review-monitor.service /etc/systemd/system/
sudo cp deploy/systemd/maps-review-monitor.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now maps-review-monitor.timer
systemctl list-timers maps-review-monitor.timer
```

Timer 會在開機約 10 分鐘後首次執行，之後於每次監控完成後約 45 至 46 分鐘再次執行。

### Windows 工作排程器

在已啟用 `maps-review-monitor` Conda 環境的 PowerShell 中執行：

```powershell
.\deploy\windows\install-task.ps1 -ProjectPath "C:\path\to\songji-google-maps-review-monitor"
```

Ubuntu 與 Windows 請勿同時對同一份資料庫啟用排程。切換主機前，請先備份並在另一台主機還原。

詳細的排程確認與故障處理請見 [維運手冊](docs/operations.md)。核心術語請見 [領域詞彙表](CONTEXT.md)。

## 測試

```bash
pytest
```

## 店家展示頁（Tailscale）

展示頁會直接讀取既有的 SQLite 評論資料與本機媒體；它不需要網站登入，請只透過 Tailscale 存取。服務在主機綁定 `0.0.0.0:8088`，可從 Tailnet 裝置以 `http://<主機的-Tailscale-IP>:8088` 開啟。

人工貼文、照片與影片以檔案方式管理：將 `data/portal/content.example.yaml` 複製為 `data/portal/content.yaml`，並依範例新增內容；本機素材放在 `data/portal/<店家代號>/`。`shop_key` 是店家 Google Maps URL 的 SHA-256 前 16 碼，可由現有 SQLite 的 `shops` 資料表查得。

在 Ubuntu 安裝或更新依賴後啟用服務：

```bash
sudo cp deploy/systemd/maps-review-portal.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now maps-review-portal.service
systemctl status maps-review-portal.service
```

查看完整部署與操作說明請見 [操作手冊](docs/operations.md)。

## 授權

本專案以 [MIT License](LICENSE) 發布。
