# Songji Google Maps 評論監控工具

以 Python 與 Playwright 監控公開的 Google Maps 店家評論，將新增評論、內容修改、店家回覆異動與連續監控失敗通知至 Telegram 群組。資料會保存於本機 SQLite，並可匯出與備份。

## 功能

- 最多監控 10 間 Google Maps 店家。
- 偵測新增評論、評論內容更新、店家回覆新增或移除。
- 評論更新通知只比較評論者名稱、星等與文字；Google 圖片或個人檔案網址變動不會誤發通知。
- 保存評論快照與觀測紀錄，支援推估相對時間評論的發布日期。
- 展開並保存評論與店家回覆全文，保留換行與評論編輯歷史。
- 在本機以中文字符比對與 `multilingual-e5-small` ONNX 語意向量找出同店／跨店相似評論候選；文字相似本身不作為協同行為證據。
- 使用證據模型 v2 顯示群組最低／中位／最高相似度、直接配對密度、資料覆蓋率、時間背景與獨立的評論者網絡／共用照片訊號；共用照片會直接列出命中同一指紋的縮圖，完整連結分群則避免單一橋接評論把不相似內容串成同一群。
- 將候選分為「內容高度相似」、「異常組合，待人工複核」及「高優先複核」，並提供正負向分類、篩選、統計與分析百分比進度。
- 提供評論者相同／類似名稱搜尋及跨店同名清單，忽略全半形、空白與標點並顯示名稱相似度；名稱結果不作身分佐證，也不自動提高複核等級。
- 提供全站搜尋，可查店家、評論者、評論全文、店家回覆及人工貼文。
- 提供評論發文時間分析，依店家與期間繪製每日／每週、星期、每兩小時與三小時區段圖表，以及 X 軸星期、Y 軸每兩小時一格的發文熱度圖，並以保守門檻提示固定間隔或時間集中。
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

### 從首頁新增監控店家

1. 展開首頁的「＋ 新增監控店家」，貼上 Google 分享網址。
2. 網站自動解析 `https://share.google/…`、`https://maps.app.goo.gl/…`、舊版 `goo.gl/maps/…` 或完整 Maps 店家網址，帶入可取得的店名。
3. 開啟 Maps 預覽確認店家，補填或調整名稱，再按「確認新增監控」。Google 搜尋型分享若未提供店家 CID，會轉為 Maps 搜尋網址並提示確認。

新增成功後卡片立即出現，下一輪監控排程會建立首次基準，既有評論不會補發通知；不需重啟服務。網頁會更新 `config.toml` 並保留原有設定、註解與店家網址，儲存前的設定備份在 `config.toml.before-add.bak`（保留最近一次）。同一 CID／相同正規化網址、重複店名及超過 10 家啟用店家會被拒絕；不同名稱但無共同 CID 的多種網址仍須人工確認是否重複。已停用店家需在設定檔重新啟用。

此操作沿用 Tailscale 存取權限，能使用網站的使用者可新增店家。網頁服務的使用者需能寫入專案目錄及 `config.toml`。轉址只允許既定 Google 網域，儲存要求同源表單與有效預覽。

`/media` 只會提供 `data/media/` 與 `data/portal/` 內、非隱藏路徑的圖片或影片。`.env`、`config.toml`、SQLite、YAML、文字檔，以及白名單目錄外的檔案一律回傳 `404`；素材不要放在其他專案目錄後再交由展示頁提供。

人工貼文、照片與影片以檔案方式管理：將 `data/portal/content.example.yaml` 複製為 `data/portal/content.yaml`，並依範例新增內容；本機素材放在 `data/portal/<店家代號>/`。`shop_key` 是店家 Google Maps URL 的 SHA-256 前 16 碼，可由現有 SQLite 的 `shops` 資料表查得。

相似評論分析完全在 OCI 主機本機執行，不使用外部 AI API。每次監控結束後會獨立執行；失敗時不影響監控、Telegram 或上一版分析結果。可將 `data/portal/review-analysis.example.yaml` 複製為 `data/portal/review-analysis.yaml`，依理由排除特定評論、群組或常見固定用語。

證據模型 v2 把文字相似度限制為候選入口，不再把「跨店至少 3 篇相似文字」視為獨立佐證。命中一類可驗證的獨立非內容訊號時列為「異常組合，待人工複核」；只有至少兩類獨立非內容訊號同時成立，且可確認至少 3 個不同 Google 個人檔案，才列為「高優先複核」。個人檔案以 Google contributor ID 正規化識別；時間鄰近會納入相對時間的不確定範圍，但因尚未相對各店平時流量校正，目前只作背景提示，不參與高優先升級。現階段高優先必須同時命中「跨店評論者群組」與「經驗證的跨帳號共用照片」。

這些結果只協助安排人工複核。證據等級或證據分數不是造假機率；僅憑系統可取得的公開資料，無法證明評論是否付費、受託或虛假。

在 Ubuntu 安裝或更新依賴後啟用服務：

```bash
sudo cp deploy/systemd/maps-review-portal.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now maps-review-portal.service
systemctl status maps-review-portal.service
```

### OCI 既有安裝更新

v0.8.0 新增店家總覽：每輪巡檢在開啟評論前，擷取店名、總評分、評論總數、地址、電話、完整每週營業時間、網站與封面照片。各店頁面提供目前總覽、歷史完整版本與欄位差異（每頁 20 版）。首次成功擷取建立基準，不發通知；後續評分、評論總數等任何已確認欄位改變，都透過 Telegram 顯示原值與新值。封面改變會附原／新圖片（檔案仍存在時）。

未讀到的欄位視為「未確認」，沿用既有值，不將暫時缺失當成刪除；因此目前不自動通知電話／網站等欄位被移除。營業時間只接受完整七天排程，不追蹤「營業中／即將打烊」等即時文字。Google 自動更換或重新編碼封面也可能產生圖片差異，不代表店家主動修改。總覽與評論獨立保存，總覽成功不代表評論抓取成功。

升級前請先備份；資料庫會遷移到 schema 6 並保留遷移前備份。歷史從升級後第一次成功擷取開始，無法回補過去未收集的資料。

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

更新至證據模型 v2 後必須至少成功執行一次 `analyze`；舊快照、未知版本或缺少完整 v2 輸入指紋的快照都會安全降級，不會沿用高優先訊號、分數或照片宣稱。第一次執行 `analyze` 會下載約 0.1B 參數的本機模型，所需時間較長。網頁右下角會顯示分析階段與百分比；後續批次會重用模型快取。

查看完整部署與操作說明請見 [操作手冊](docs/operations.md)。

## 授權

本專案以 [MIT License](LICENSE) 發布。
