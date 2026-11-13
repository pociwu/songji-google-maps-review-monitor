# 維運手冊

## 封面內容去重（v0.8.2）

封面版本判定不再比較整個路徑／檔名字典：相同檔案 SHA-256 直接視為相同；雜湊不同時，從白名單媒體目錄讀取原／新檔案，解碼並套用 EXIF 方向後比較 RGBA 像素、尺寸及 ICC 色彩描述檔。像素完全相同的 JPEG 中繼資料或封裝差異不建立新版本、不發送通知；若其他欄位改變，只記錄該欄位並沿用原封面參照。

不使用模糊相似門檻，避免漏掉細小的實際變更。尺寸、像素或色彩描述檔不同仍可能通知；無法讀取、動畫、超過 10 MB 或 1600 萬像素的檔案不推斷相等。相同圖片的歷史版本與已送 Telegram 訊息不回溯刪除，磁碟既有媒體不自動清除。此規則僅用於店家總覽封面，不改變評論證據模型的照片比對規則。

## 等待評論與 Google Maps 連結（v0.8.1）

首頁卡片與各店頁面提供 Google Maps 店家連結。完整店家頁具有店名、地址與「撰寫評論」入口，但無評分、評論數、評論分頁或卡片時，未曾成功初始化的店家改列「等待評論（尚無可讀入口）」，不再累計抓取失敗。這不代表已證實零評論，也不設定總評論數為 0。總覽照常保存，下輪仍重試；第一次真正抓到評論時仍建立靜默基準。已初始化或已存有評論的店家缺少入口時仍算失敗，以免掩蓋退化或限制頁。

營業時間兼容 Google 現行「顯示本週營業時間」按鈕與已展開表格；只擷取前兩欄（星期及時段），排除複製按鈕圖示。更換版面仍需以實機資料確認。

## 店家總覽與變更歷史（v0.8.0）

- 跟隨原本巡檢排程，每家店進入評論區前先讀取總覽，不增加獨立 timer。
- 首次取得總覽只建立基準；之後欄位改變會新增不可覆寫的完整快照與差異，並以同一 SQLite 交易建立 Telegram 待送通知。相同資料不新增版本；A → B → A 會保留三版。
- 總評分與評論總數也屬於變更通知。通知顯示原值、新值與偵測時間（不是 Google 實際修改時間）。封面變更附本機仍存在的原／新封面。
- 網頁店家頁上方顯示目前總覽，歷史每頁 20 版，封面檔案放在 `data/media/<shop_key>/overview/`，以 SHA-256 命名，不覆寫舊照片。備份時須包含資料庫與媒體。
- 欄位缺失不等同移除：保留最後已知值，歷史中標示本次是否觀測到。電話、網站等刪除目前不自動判定；首次未取得、後來取得時以「未確認 → 新值」通知。
- 營業時間只儲存完整每週七天表，不儲存會隨時間改變的「目前營業中」摘要。Google 版面不符或圖片下載失敗時保留舊值。
- 封面限制為 Google 圖片主機、不跟隨轉址、最多 10 MB，經圖片解碼驗證後保存；使用固定尺寸減少不同螢幕造成的差異。v0.8.2 起忽略像素相同的中繼資料／封裝變化；有損重新壓縮若改變像素仍可能觸發變更。
- 總覽失敗會寫入服務日誌並繼續評論抓取；評論失敗不撤銷已取得的總覽。既有零評論店家的評論抓取告警邏輯未因此改變。
- 升級前執行 `maps-review-monitor --config config.toml backup`，更新後重啟網頁服務。首次開啟資料庫自動備份並遷移至 schema 6；下一輪巡檢開始累積總覽，不回補未曾收集的歷史。

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

## Telegram 通知佇列安全

每個新通知事件從建立起只有 7 日有效期，單一通知部分最多嘗試 8 次。待送部分超過有效期會改為 `expired`，達重試上限會改為 `dead`；兩者都不會再由後續排程補送。發送程序會先把單一部分原子認領為 `sending`，所以兩個重疊的排程不會同時取得同一部分；若程序崩潰，認領 lease 到期後會自動回到 `pending`。每次實際呼叫 Telegram 前仍會重新確認事件未過期。升級資料庫時程式會先建立 `data/reviews.sqlite3.bak-<時間>`，再依原事件的 `created_at` 回填既有通知的 `expires_at`，因此很久以前累積的 pending 通知不會突然送出。

首次發現但 Google 已明確顯示為 72 小時以上、數週、數月或數年前的評論，仍會寫入資料庫，但不建立 `review_new` Telegram 通知。71 小時內與 2 天內的評論會通知；無法解析的時間文字也會保留通知資格，避免 Google 語系或格式改版造成新評論漏報。評論內容更新及店家回覆新增／移除不受這個新評論時效篩選限制。

查看最近已終止與待送的通知：

```bash
sqlite3 data/reviews.sqlite3 \
  "SELECT status,COUNT(*) FROM notification_parts GROUP BY status ORDER BY status;"
sqlite3 data/reviews.sqlite3 \
  "SELECT id,event_id,status,attempts,next_attempt_at,expires_at,claim_expires_at,last_error FROM notification_parts WHERE status IN ('pending','sending','expired','dead') ORDER BY id DESC LIMIT 30;"
```

## 從網頁新增店家

在首頁展開「＋ 新增監控店家」，貼上 Google 店家分享連結。解析完成後確認 Maps 預覽及店家名稱，再按「確認新增監控」。`share.google` 若導向 Google 搜尋店家資訊，會轉為 Maps 搜尋網址；請確認搜尋結果指向預期分店。

`config.toml` 會以完整檔案替換方式儲存；原有店家 URL 與識別碼保持不變。最近一次新增前的備份為 `config.toml.before-add.bak`。首頁即時讀取清單，監控服務在下一次啟動時載入新店；不需重新安裝 timer，也不需要對所有店家執行 `init`。若有一輪監控已在執行，新店會從下一輪加入。

若顯示無法儲存，確認 `maps-review-portal.service` 的 `User` 對 `/opt/maps-review-monitor` 目錄及 `config.toml` 有寫入權限。若預覽失效，重新按「解析網址」即可；預覽有效期 15 分鐘，網頁服務重啟也會使尚未送出的預覽失效。此版本使用既有單 worker 部署設定。

## 相似評論分析

分析只使用評論者全文建立文字候選，不把店家回覆或人工貼文納入相似分數。預設門檻為中文字符相似度 85% 或語意相似度 92%；低於 12 個有效中文字的短評，只有同一分析範圍內至少 3 則完全相同才會納入。文字相似、跨店篇數與星等方向都不是獨立行為證據。

證據模型 v2 使用完整連結分群，避免 A≈B、B≈C 但 A≉C 的橋接群組。群組詳情會顯示最低／中位／最高相似度、直接配對密度、資料覆蓋率，以及三類非內容訊號：

- 時間鄰近（背景提示）：只採用秒、分鐘、小時或天等可以界定誤差的相對時間；週、月、年等粗略日期不會命中。因尚未相對各店平時流量校正，不參與等級升級。
- 跨店評論者群組（獨立升級證據）：依 Google 個人檔案網址中的 canonical contributor ID，比對至少 2 個評論者是否共同跨越同一組至少 2 家店。
- 跨帳號共用照片（獨立升級證據）：圖片先通過 Pillow 完整解碼驗證，再比較完整檔案 SHA-256；目前只辨識內容完全相同的有效圖片，詳情頁只並排顯示實際命中該指紋的縮圖，不以同則評論的其他照片充當佐證。

命中一類獨立升級訊號時列為「異常組合，待人工複核」。只有至少兩類獨立訊號同時命中，且至少有 3 個可確認的不同 contributor ID，才列為「高優先複核」；因此目前高優先必須同時命中評論者網絡與已驗證共用照片。

證據分數由內容候選 20 分、群內最弱直接配對超過字面或語意候選門檻的餘裕最多 10 分、時間鄰近目前 10 分、跨店評論者群組 25 分及跨帳號共用照片 20 分組成，目前實際最高 85 分。等級不依總分決定；分數只用於相同規則版本內安排人工複核，不是造假機率。目前可取得的公開資料不能證明付費、受託或虛假評論。

監控批次結束後會自動分析，也可手動重跑所有歷史資料：

```bash
cd /opt/maps-review-monitor
conda run --no-capture-output -n maps-review-monitor \
  maps-review-monitor --config /opt/maps-review-monitor/config.toml analyze
```

首次執行會下載 `intfloat/multilingual-e5-small`。分析使用一個 CPU 執行緒並繼承 systemd 的 `Nice=10`；OCI 降為 2 CPU 時不需調整。若模型載入或分析失敗，新結果不會成為目前快照，監控與 Telegram 仍照常完成。

### 更新至證據模型 v2

更新程式前先執行 `maps-review-monitor backup`。部署後必須成功重跑一次全量分析；尚未重跑時，舊快照會標示為「舊版證據」並降級，不會把舊版「跨店 3 篇相似」規則當成高優先證據。未知版本、訊號欄位互相矛盾或缺少完整 v2 輸入指紋時，也會停用獨立訊號並隱藏不可信的證據分數，直到完整分析成功。

```bash
cd /opt/maps-review-monitor
git pull --ff-only origin main
conda env update -n maps-review-monitor -f environment.yml --prune
sudo cp deploy/systemd/maps-review-monitor.service /etc/systemd/system/
sudo cp deploy/systemd/maps-review-portal.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart maps-review-portal.service
conda run --no-capture-output -n maps-review-monitor \
  maps-review-monitor --config /opt/maps-review-monitor/config.toml analyze
```

確認最近成功批次的設定包含 `"evidence_model_version":2` 與 `"grouping":"complete_linkage"`：

```bash
sqlite3 data/reviews.sqlite3 \
  "SELECT id,status,model_version,settings_json,completed_at FROM analysis_runs ORDER BY id DESC LIMIT 1;"
```

接著重新整理「全部店家分析」，確認群組頁顯示「證據模型 v2」、群組密度、最低／中位／最高相似度、訊號明細與資料覆蓋。沒有足夠日期、個人檔案或本機照片時，畫面應顯示資料限制，而不是把缺少資料解讀為安全或異常。

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

服務監聽 `0.0.0.0:8088`。請使用主機的 Tailscale IP 或 MagicDNS 名稱瀏覽；網站本身沒有登入機制，Tailnet ACL 是使用者存取邊界。仍應在 OCI 防火牆／安全清單阻擋公網直接連入 8088，並只把需要查看頁面的 Tailnet 裝置或使用者納入 ACL。

### 公開媒體白名單

`/media/<path>` 只提供下列實體目錄中的圖片與影片：

- `data/media/`：監控程式下載的評論媒體。
- `data/portal/`：人工管理的店家照片與影片。

允許副檔名為 `.avif`、`.gif`、`.jpeg`、`.jpg`、`.m4v`、`.mov`、`.mp4`、`.ogv`、`.png`、`.webm`、`.webp`，大小寫不拘。任何隱藏檔／隱藏目錄、白名單目錄外路徑、路徑穿越、設定檔、SQLite、YAML 或文字檔都回傳 `404`；成功回應包含 `X-Content-Type-Options: nosniff`。

部署後可從 OCI 主機確認敏感路徑無法讀取；只檢查 HTTP 狀態，不要把檔案內容輸出到終端紀錄：

```bash
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8088/media/.env
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8088/media/config.toml
curl -sS -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8088/media/data/reviews.sqlite3
```

三個結果都必須是 `404`。若不是，先停止展示服務並確認實際執行的 Git 版本與 systemd `WorkingDirectory`。

### 編輯內容素材

```bash
mkdir -p data/portal/<店家代號>
cp data/portal/content.example.yaml data/portal/content.yaml
```

在 `content.yaml` 的每筆資料需提供 `id`、`shop_key`、`published_at`、`title` 和 `body`。`photos` 是相對於專案根目錄的本機媒體路徑，但解析後必須位於 `data/portal/`；`videos` 可放同一白名單目錄內的本機影片路徑或外部影片 URL。不要把 `.env`、設定、YAML 或資料庫改成媒體副檔名，也不要在 `content.yaml` 中引用 `data/portal/` 以外的本機路徑。修改 YAML 或新增媒體後，重新整理網頁即可看到結果，不需要重啟服務。

驗證 YAML 是否可載入：

```bash
python -c "from pathlib import Path; from maps_review_monitor.portal import load_content_assets; print(load_content_assets(Path('data/portal/content.yaml')))"
```
