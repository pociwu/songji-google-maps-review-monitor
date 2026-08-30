# Songji Google Maps 評論監控工具

以 Python 與 Playwright 監控公開的 Google Maps 店家評論，將新增評論、內容修改、店家回覆異動與連續監控失敗通知至 Telegram 群組。資料會保存於本機 SQLite，並可匯出與備份。

## 功能

- 最多監控 10 間 Google Maps 店家。
- 偵測新增評論、評論內容更新、店家回覆新增或移除。
- 評論更新通知只比較評論者名稱、星等與文字；Google 圖片或個人檔案網址變動不會誤發通知。
- 保存評論快照與觀測紀錄，支援推估相對時間評論的發布日期。
- 展開並保存評論與店家回覆全文，保留換行與評論編輯歷史。
- 在本機以中文字符比對與 `multilingual-e5-small` ONNX 語意向量分析同店／跨店相似評論。
- 提供相似評論群組、佐證、正負向分類、篩選、統計與分析百分比進度。
- 提供評論者相同／類似名稱搜尋及跨店同名清單，忽略全半形、空白與標點並顯示名稱相似度；名稱結果不自動列入疑似協同。
- 提供全站搜尋，可查店家、評論者、評論全文、店家回覆及人工貼文。
- 提供評論發文時間分析，依店家與期間繪製每日／每週、星期、凌晨／上午／下午／晚上四時段、三小時區段圖表，以及 X 軸星期、Y 軸每兩小時一格的發文熱度圖，並以保守門檻提示固定間隔或時間集中。
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
git clone https://github.com/pociwu/songji-google-maps-review-monitor.git
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
maps-review-monitor analyze
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

相似評論分析完全在 OCI 主機本機執行，不使用外部 AI API。每次監控結束後會獨立執行；失敗時不影響監控、Telegram 或上一版分析結果。可將 `data/portal/review-analysis.example.yaml` 複製為 `review-analysis.yaml`，依理由排除特定評論、群組或常見固定用語。

在 Ubuntu 安裝或更新依賴後啟用服務：

```bash
sudo cp deploy/systemd/maps-review-portal.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now maps-review-portal.service
systemctl status maps-review-portal.service
```

### OCI 既有安裝更新

```bash
cd /opt/maps-review-monitor
git pull --ff-only origin main
conda env update -n maps-review-monitor -f environment.yml --prune
sudo cp deploy/systemd/maps-review-monitor.service /etc/systemd/system/
sudo cp deploy/systemd/maps-review-portal.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart maps-review-portal.service
maps-review-monitor --config /opt/maps-review-monitor/config.toml analyze
sudo systemctl enable --now maps-review-monitor.timer
```

第一次執行 `analyze` 會下載約 0.1B 參數的本機模型，所需時間較長。網頁右下角會顯示分析階段與百分比；後續批次會重用模型快取。

查看完整部署與操作說明請見 [操作手冊](docs/operations.md)。

## 授權

本專案以 [MIT License](LICENSE) 發布。
