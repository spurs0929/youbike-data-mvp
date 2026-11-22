# 部署（GCP e2-micro）：D0 最小擷取、D1 完整部署

> **D1-a（PostgreSQL 與 4C 排程）、D1-b（固定 IP、網域、Caddy HTTPS）見文末各節。** 以下到〈更新程式〉為止是 D0 的內容與紀錄。

依《YouBike 2.0 專案設計定版 v1.3》11.2。只部署既有的 `collect.py`＋cron＋`flock`，
目的是提早累積平日尖峰、離峰與週末的真實資料。

- 不部署 PostgreSQL、API、Caddy；防火牆只開 SSH。
- 不修改 `src/`，不改變資料格式（gzip 檔名與內容、JSONL 欄位）。
- VM 上不放任何密碼、Token 或金鑰。

## 檔案

| 檔案 | 安裝位置 | 用途 |
|---|---|---|
| `crontab.txt` | `youbike` 使用者的 crontab | 每分鐘擷取（`flock -n`）、每 5 分鐘健康檢查；D1-a 起加上每 5 分鐘的 4C 快照 |
| `check_health.py` | 不安裝，從 repo 執行 | 唯讀檢查：最近成功擷取（> 10 分鐘警告）、磁碟使用率（≥ 80% 警告） |
| `logrotate-youbike` | `/etc/logrotate.d/youbike` | `logs/*.log`（`collect.log`、`health.log`，D1-a 起含 `snapshot.log`）每日輪替，保留 14 天，單檔 20 MB 上限 |
| `journald-youbike.conf` | `/etc/systemd/journald.conf.d/youbike.conf` | 系統日誌保留 14 天、最多 200 MB |
| `env.example` | 不安裝 | 環境變數說明（`YOUBIKE_DATA_DIR`；D1-a 起加上 `YOUBIKE_DATABASE_URL`） |
| `postgresql-youbike.conf` | `/etc/postgresql/16/main/conf.d/youbike.conf` | D1-a：只用 Unix socket、`max_connections`、`shared_buffers` |
| `Caddyfile` | `/etc/caddy/Caddyfile` | D1-b：HTTPS 入口，只有 `/healthz`，其他路徑 404 |
| `unattended-upgrades-caddy` | `/etc/apt/apt.conf.d/52unattended-upgrades-caddy` | D1-b：讓 Caddy 也納入自動更新 |

## VM 規格（2026-11-20 建立）

| 項目 | 設定 |
|---|---|
| 名稱／可用區 | `youbike-vm`／`us-west1-b`（GCP 專案 `youbike-mvp`） |
| 機型 | e2-micro，佈建模型「標準」（非 Spot） |
| 作業系統 | Ubuntu 24.04 LTS Minimal（x86/64） |
| 磁碟 | 標準永久磁碟（pd-standard）30 GB，無快照排程、不備份 |
| 網路 | 外部 IP 臨時；防火牆不開 HTTP／HTTPS |
| 安全性 | 無服務帳戶；不裝 Ops Agent |

Free Tier 只涵蓋 us-west1／us-central1／us-east1 的 e2-micro 與 30 GB 標準永久磁碟；
選錯區域或磁碟類型會照一般價格收費。

## VM 上的目錄配置

```
/home/youbike/                  專用使用者 youbike（非 root）
  youbike-data-mvp/             repo（含 .venv/）
  data/                         YOUBIKE_DATA_DIR：raw/ gzip、fetch_batches.jsonl（權限 700）
  logs/                         collect.log、health.log、snapshot.log（D1-a）（logrotate 管理）
  collect.lock                  擷取的 flock 鎖檔
  snapshot.lock                 4C 的 flock 鎖檔（D1-a）
```

**日誌與資料分開**：logrotate 只處理 `logs/*.log`。`data/` 底下的原始 gzip 與
`fetch_batches.jsonl` 是資料，不會被輪替或刪除；12/11 前不刪原始檔，保留期限在 D1 決定。

---

## 安裝步驟

以下指令都在 GCP Console 的 SSH 視窗執行。標示「以 youbike 身分」的步驟，
先執行 `sudo -iu youbike` 切換使用者，做完再 `exit` 回到自己的帳號。

### 0. 基本設定（已完成，2026-11-21）

```bash
timedatectl | grep "Time zone"          # 應為 Etc/UTC，不要改
sudo apt update && sudo apt install -y cron logrotate git python3-venv

# 2 GB swap
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

### 1. 建立專用使用者與目錄

```bash
sudo useradd --create-home --shell /bin/bash youbike
sudo -u youbike mkdir -m 700 /home/youbike/data
sudo -u youbike mkdir -m 750 /home/youbike/logs
```

### 2. 下載程式並建立虛擬環境（以 youbike 身分）

```bash
sudo -iu youbike
git clone https://github.com/spurs0929/youbike-data-mvp.git
cd youbike-data-mvp
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

D0 只用到 `requests`，但仍安裝完整的 `requirements.txt`，D1 不必重裝。
1 GB 記憶體安裝 pandas 時會用到 swap，速度慢屬正常。

### 3. 手動試跑一次（以 youbike 身分，仍在 `youbike-data-mvp/`）

這時 cron 還沒安裝，手動執行不會和排程同時寫入。

```bash
export YOUBIKE_DATA_DIR=/home/youbike/data
.venv/bin/python src/collect.py          # 應印出 <batch_id> ok  stations=...
.venv/bin/python deploy/check_health.py  # 應印出 ... OK   last_success=... disk=..%
exit
```

### 4. 安裝 crontab

```bash
sudo crontab -u youbike /home/youbike/youbike-data-mvp/deploy/crontab.txt
sudo crontab -u youbike -l               # 確認內容
systemctl is-enabled cron                # 應為 enabled（重開機後自動啟動）
```

**安裝之後**，VM 上不要再手動執行 `collect.py`，也不要執行 `--loop`（單一寫入者，定版 4.1）。

### 5. 安裝日誌輪替與 journald 上限

```bash
sudo install -m 644 -o root -g root /home/youbike/youbike-data-mvp/deploy/logrotate-youbike /etc/logrotate.d/youbike
sudo logrotate -d /etc/logrotate.d/youbike     # 試跑（不會真的輪替），確認沒有 error

sudo install -D -m 644 /home/youbike/youbike-data-mvp/deploy/journald-youbike.conf /etc/systemd/journald.conf.d/youbike.conf
sudo systemctl restart systemd-journald
journalctl --disk-usage
```

Ubuntu 的 `logrotate.timer` 每天執行一次，不需另外排程。

### 6. 確認運作（安裝 crontab 後等 3 分鐘）

```bash
sudo tail -n 5 /home/youbike/logs/collect.log
sudo tail -n 3 /home/youbike/data/fetch_batches.jsonl
sudo tail -n 3 /home/youbike/logs/health.log
```

---

## 驗收（定版 v1.3 11.2）

### A. 連續 24 小時擷取

安裝 crontab 滿 24 小時後：

```bash
sudo wc -l /home/youbike/data/fetch_batches.jsonl                 # 約 1,440 行以上
sudo grep -c '"status": "success"' /home/youbike/data/fetch_batches.jsonl
sudo grep -c '"status": "failed"'  /home/youbike/data/fetch_batches.jsonl
sudo grep -c 'WARN' /home/youbike/logs/health.log                 # 除了 B 的測試，應為 0
```

### B. 兩項警告都實際觸發過一次

**最近成功擷取**：暫停擷取，讓檢查自己發現。

```bash
sudo crontab -u youbike -e     # 在「每分鐘擷取」那一行最前面加上 #，存檔
# 等 15 分鐘
sudo tail -n 3 /home/youbike/logs/health.log    # 應出現 WARN ... stale: last success 1x.xm ago
sudo crontab -u youbike /home/youbike/youbike-data-mvp/deploy/crontab.txt   # 還原
```

**磁碟警戒**：暫時把門檻調到目前使用率以下，只執行一次，不改 crontab。

```bash
sudo -iu youbike bash -c 'cd youbike-data-mvp && YOUBIKE_DATA_DIR=/home/youbike/data .venv/bin/python deploy/check_health.py --disk-threshold 10'
# 應出現 WARN ... disk: usage xx% >= 10%
```

### C. 資源基準

擷取運作中記錄一次，作為 D1 記憶體檢查的基準：

```bash
free -m
df -h /
du -sh /home/youbike/data
```

## 基準紀錄

| 時間（台灣） | 狀態 | `free -m` | `df -h /` | 備註 |
|---|---|---|---|---|
| 2026-11-21 13:50 | 擷取前（swap 已建） | total 953、used 317、available 636；swap 2047（已用 0） | 29G，已用 4.3G（16%） | Python 3.12.3 |
| 2026-11-21 14:38 | 擷取運作中 | total 953、used 327、available 625；swap 2047（已用 6） | 29G，已用 4.5G（16%） | `data/` 1.1 MB（約 10 批，每批約 110 KB） |

## 驗收紀錄（2026-11-21）

部署版本：`43140f8`。套件：pandas 3.1.1、psycopg 3.3.9（本機版本對齊留到 D1）。

| 項目 | 結果 | 時間（UTC） |
|---|---|---|
| cron 開始自動擷取 | 第一筆 `20261121T061601Z` ok，1,826 站 | 06:16 |
| 健康檢查自動執行 | `OK`，age 1.0m，disk 16% | 06:20 |
| B-1 磁碟警戒 | `--disk-threshold 10` 手動執行：`WARN ... disk: usage 16% >= 10%`，結束碼 1 | 06:22 |
| B-2 最近成功擷取 | 暫停擷取後，06:25、06:30 為 OK（age 3.0m、8.0m），06:35 `WARN ... stale: last success 13.0m ago (> 10m)` | 06:35 |
| 還原排程 | `grep -c PAUSED` 為 0；06:37 起恢復 ok | 06:36 |
| C 資源基準 | 見上方基準紀錄 | 06:38 |
| A 連續 24 小時 | **通過**（11/22 補記）：共 1,432 批，success 1,432、failed 0；`WARN` 只有 B-2 的 06:35 一筆 | 11/22 06:20 |

- logrotate 試跑（`-d`）無 error；journald 上限生效時系統日誌佔 16 MB。
- **已知缺口**：06:23～06:36 為 B-2 驗收刻意暫停擷取，約 14 分鐘無資料，不補抓。
  驗收 A 統計 24 小時筆數時需扣除這段。
- **A 的筆數推算**：11/21 06:16 至 11/22 06:20 每分鐘一批（含頭尾）為 24 × 60 + 4 + 1 = 1,445；
  扣除暫停的 14 分鐘為 1,431；加上安裝 crontab 前手動試跑的 1 筆為 1,432，與實際相符。
- **A 當時資源**（11/22 UTC 06:20）：available 629 MB、swap 已用 22 MB、磁碟 17%、`data/` 148 MB。

## 更新程式

```bash
sudo -iu youbike
cd youbike-data-mvp && git pull --ff-only
exit
```

`deploy/crontab.txt` 有變更時，需重新執行第 4 步的 `sudo crontab -u youbike ...`；
logrotate、journald 設定有變更時，重新執行第 5 步。

---

## D1-a：PostgreSQL 與 4C 排程

依定版 v1.3 11.3。D1 分兩段：**D1-a** 部署 PostgreSQL 與 4C 五分鐘排程，**D1-b** 處理固定 IP、網域與 Caddy HTTPS。
本節只涵蓋 D1-a：不開放任何對外連接埠，防火牆維持只開 SSH。

### 設計

| 項目 | 決定 | 理由 |
|---|---|---|
| 連線方式 | 只用本機 Unix socket，`listen_addresses = ''`（不開 TCP） | 4C 與之後的 API 都在同一台 VM，不需要 5432 port |
| 驗證 | peer：系統使用者 `youbike` 對應資料庫角色 `youbike` | VM 上不需要任何資料庫密碼 |
| 連線字串 | `YOUBIKE_DATABASE_URL=postgresql:///youbike`（寫在 `crontab.txt`） | 三個斜線＝不指定主機，psycopg 的 libpq 預設找 `/var/run/postgresql` |
| 排程時間 | 每 5 分鐘的第 1 分鐘（`1-59/5`） | 例：00:06 處理 `[00:00, 00:05)`；晚 1 分鐘讓槽內最後一批的 JSONL 先寫入 |
| 重疊保護 | `flock -n -E 75`，被擋下時在 `snapshot.log` 寫一行 `skipped` | 防止多個 4C 行程同時佔用記憶體；同槽互斥另由資料庫的同槽鎖保證 |
| 記憶體 | `max_connections = 20`、`shared_buffers = 128MB`（預設值） | 以實測為準，不以「available − shared_buffers」估算 |

以上連線方式在 PostgreSQL 16、psycopg 3.3.9 上實測過：只開 Unix socket、peer 驗證、完全不設密碼時，
`YOUBIKE_TEST_DATABASE_URL=postgresql:///<測試資料庫>` 跑完整測試 396 項全部通過；TCP 連線被拒。

`snapshot.log` 每次執行一行 JSON（格式見 `docs/schema.md` 第六節「處理結果」），
被 flock 擋下時多一行 `<UTC 時間> skipped: previous snapshot_job still running`（不是 JSON）。

### 安裝步驟

以下修改系統設定的步驟，**一次只做一步**，確認結果後再做下一步。

#### D1-1. 安裝前基準

```bash
free -m
df -h /
```

#### D1-2. 安裝 PostgreSQL 16

```bash
sudo apt update && sudo apt install -y postgresql
pg_lsclusters
```

`pg_lsclusters` 應顯示 `16  main  5432 online`。

#### D1-3. 先更新 repo（取得本節的設定檔）

```bash
sudo -iu youbike bash -c 'cd youbike-data-mvp && git pull --ff-only && git log -1 --oneline'
```

只更新程式，不會改變正在執行的排程；擷取照常運作。

#### D1-4. 安裝 PostgreSQL 設定並重新啟動

```bash
sudo install -m 644 -o root -g root /home/youbike/youbike-data-mvp/deploy/postgresql-youbike.conf /etc/postgresql/16/main/conf.d/youbike.conf
sudo systemctl restart postgresql
sudo -u postgres psql -Atc "SELECT name, setting FROM pg_settings WHERE name IN ('listen_addresses','max_connections','shared_buffers') ORDER BY name"
sudo ss -ltnp | grep -c 5432
```

應顯示 `listen_addresses|`（空白）、`max_connections|20`、`shared_buffers|16384`（單位 8 kB，即 128 MB）；
最後一行為 `0`（沒有任何 TCP 連接埠在聽 5432）。

#### D1-5. 建立資料庫角色與資料庫

```bash
sudo -u postgres createuser youbike
sudo -u postgres createdb -O youbike youbike
sudo -iu youbike psql -d youbike -Atc "SELECT current_user, current_database()"
```

最後一行應為 `youbike|youbike`，且**不會詢問密碼**（peer 驗證）。

#### D1-6. 套用 migration

```bash
sudo -iu youbike bash -c 'cd youbike-data-mvp && psql -d youbike -v ON_ERROR_STOP=1 -q -f migrations/001_init.sql && psql -d youbike -Atc "\dt"'
```

應列出 `fetch_batches`、`station_hourly_stats`、`station_snapshots`、`stations` 四張表。只能執行一次。

#### D1-7. 手動跑一次 4C，並量記憶體

```bash
sudo -iu youbike bash -c 'cd youbike-data-mvp && YOUBIKE_DATA_DIR=/home/youbike/data YOUBIKE_DATABASE_URL=postgresql:///youbike /usr/bin/time -v .venv/bin/python src/snapshot_job.py 2> /tmp/snapshot-time.txt; echo "exit=$?"'
grep -E "Maximum resident|Elapsed" /tmp/snapshot-time.txt
```

- 第一行輸出一行 JSON，`result` 為 `success`，`exit=0`。
- `Maximum resident set size` 是 4C 單次執行的最大記憶體（kB）。
- 沒有 `/usr/bin/time` 時先 `sudo apt install -y time`。

#### D1-8. 安裝新的 crontab

```bash
sudo crontab -u youbike /home/youbike/youbike-data-mvp/deploy/crontab.txt
sudo crontab -u youbike -l | grep -v '^#' | grep -v '^$'
```

應看到三行排程（擷取、健康檢查、4C）。

### 驗收

1. **連線與權限**：PostgreSQL 沒有任何 TCP 連接埠（D1-4 最後一行為 `0`）；防火牆規則不變；VM 上沒有資料庫密碼。
2. **連續 1 小時、12 個槽**：`snapshot.log` 每 5 分鐘一行 JSON，`result` 為 `success`；
   出現 `partial`、`no_batch`、`failed` 或 `skipped` 時要能說明原因。
3. **站點數與來源批次核對**（不以固定站數為標準）：每個槽的快照數加上該次的 `skipped`、`failed`，
   應等於來源批次的 `station_count`；不相等時，差額只能來自同一批內內容完全相同的重複站（去重）：

   ```bash
   sudo -iu youbike psql -d youbike -c "
   SELECT s.slot_time, b.batch_id, b.station_count,
          count(*) AS snapshots, count(*) FILTER (WHERE s.is_valid) AS valid
   FROM station_snapshots s JOIN fetch_batches b ON b.id = s.fetch_batch_id
   GROUP BY 1, 2, 3 ORDER BY 1"
   ```

4. **擷取不受影響**：同一段時間 `collect.log` 每分鐘 `ok`，`health.log` 都是 `OK`。
5. **記憶體**：
   - 4C 執行中（每 5 分鐘的第 1 分鐘）`free -m` 的 available 維持在 200 MB 以上，swap 用量沒有持續增加。
   - **沒有 OOM Kill**：`sudo journalctl -k --since "<D1-2 開始時間>" | grep -ciE "out of memory|oom-kill|killed process"` 為 `0`。
   - 記錄 4C 單次最大記憶體（D1-7）與 PostgreSQL 常駐時的 `free -m`。
6. **版本紀錄**：記下 VM 上的 Python、pandas、psycopg、PostgreSQL 版本（下方表格）。
   依賴版本的可重現性（本機與 VM 的 pandas 主版本不同）在 12/6 封板前處理。

### D1-a 驗收紀錄（2026-11-22，通過）

部署版本：`80d209c`。

| 項目 | 結果 | 時間（UTC） |
|---|---|---|
| 安裝前基準 | available 640 MB、swap 已用 22 MB、磁碟 17%（4.6G）；擷取 `ok`、健康檢查 `OK` | 06:50 |
| PostgreSQL 安裝 | 16.15（Ubuntu 16.15-0ubuntu0.24.04.1）；預設設定時 available 625 MB、swap 41 MB（apt 安裝時增加） | 06:53 |
| PostgreSQL 設定 | `listen_addresses` 空白、`max_connections` 20、`shared_buffers` 128 MB；TCP 5432 監聽數 0；available 回到 640 MB | 06:54 |
| 角色與資料庫 | peer 連線成功、不需密碼；角色 `youbike` 非超級使用者、不能建資料庫與角色、沒有密碼 | 06:55 |
| migration | 四張表建立，擁有者 `youbike` | 06:56 |
| 4C 手動執行 | 槽 06:50、`success`、有效 1,805、`inactive` 21；耗時 5.82 秒；**最大記憶體 98,904 kB（約 96.6 MB）** | 06:57 |
| 安裝 crontab | 三行排程；07:01 第一次自動執行 `success`（槽 06:55，挑到槽內最後一批 06:59） | 06:58 |
| 連續 1 小時 12 個槽 | 槽 06:55～07:50 由 cron 執行，12 次全部 `success`；`Traceback` 0；沒有 flock 跳過紀錄 | 07:01～07:56 |
| 站點數核對 | 13 個槽（含手動的 06:50）每槽快照數 1,826 ＝ 來源批次 `station_count` 1,826；有效 1,805；每槽各一個來源批次，皆為槽內最後一批 | 07:58 |
| 擷取不受影響 | `collect.log` 持續 `ok`；`health.log` 皆 `OK`；`WARN` 仍只有 D0 的 1 筆 | 07:58 |
| 4C 執行中記憶體 | 07:16 取樣（與擷取同時執行）：available 623 → 577 MB 仍在下降時取樣結束；swap 維持 44 MB | 07:16 |
| 記憶體、磁碟、OOM | available 634 MB、swap 已用 46 MB、磁碟 18%（4.9G）；OOM 0 次（06:50 起） | 07:58 |
| 版本 | Python 3.12.3、pandas 3.1.1、psycopg 3.3.9、PostgreSQL 16.15 | 07:58 |

- `snapshot.log` 只記錄 cron 的執行；D1-7 手動執行的結果輸出在終端機，所以日誌有 12 筆、資料庫有 13 個槽。
- **依賴版本**：VM 為 pandas 3.1.1，本機（Python 3.10）無法安裝 pandas 3。4C 已在 VM 的 pandas 3.1.1 以真實資料驗證；
  依賴版本的可重現性在 12/6 封板前處理。

---

## D1-b：固定 IP、網域、Caddy HTTPS

依定版 v1.3 11.3。D1-a 驗收通過後才開始。

### 設計

| 項目 | 決定 |
|---|---|
| 網域 | `youbike-data-mvp.duckdns.org`（DuckDNS） |
| IP | 把 VM 原本的臨時外部 IP 升級為固定 IP（位址不變） |
| DNS 更新 | 在 DuckDNS 網站**手動設定一次** IP。IP 固定，不需要自動更新，**VM 上不放 DuckDNS Token，也不裝更新腳本** |
| 憑證 | Caddy 自動向 Let's Encrypt 申請與續期，HTTP-01 驗證（需要 80 port），不用 DuckDNS API |
| 對外內容 | 只有 `/healthz` 回 200 `ok`；其他路徑 404、內容為空；移除 `Server` 標頭；不開 access log |
| 內部服務 | PostgreSQL 只用 Unix socket；之後 FastAPI 只綁 `127.0.0.1`，一律由 Caddy 對外 |
| 日誌 | Caddy 只寫 journald（200 MB、14 天上限，`journald-youbike.conf`），不產生額外日誌檔 |
| 安全更新 | Ubuntu 的 `unattended-upgrades`；Caddy 來自官方 Cloudsmith 套件庫，另外加入自動更新清單 |
| 防火牆 | 開放 80、443（GCP 的 `http-server`、`https-server` 網路標記）；SSH 另見 D1-c |

**限制**：80 port 自動轉 HTTPS 的回應仍會帶 `Server: Caddy`（不含版本號），`header -Server` 管不到這個自動產生的轉址。

`/healthz` 正常只代表 Caddy 與 HTTPS 正常，**不代表** FastAPI 或資料庫服務已上線。

### 費用與停機注意事項

- Compute Engine 免費方案不對外部 IP 收費（e2-micro 在免費方案內時）；以 Billing 報表依 SKU 確認。
- **保留了固定 IP 但沒有接在 VM 上時會收費，且費率比使用中更高**。停機或刪除 VM 時，要一併**釋放固定 IP**。
- **預算警示（NT$30）只寄通知，不是費用上限**，不會自動停機或停止計費。

### 安裝步驟

修改系統或雲端設定的步驟，**一次只做一步**，確認結果後再做下一步。

#### B-1. 臨時 IP 升級為固定 IP（GCP Console）

VPC 網路 →「IP 位址」→ 找到 `youbike-vm` 使用的外部 IP →「升級為靜態 IP 位址」，名稱 `youbike-ip`。位址不變，VM 不需重新啟動。

#### B-2. DuckDNS 設定 IP（DuckDNS 網站）

在 `youbike-data-mvp` 那一列的 `current ip` 填入 B-1 的 IP，按「update ip」。**Token 只留在 DuckDNS 網站，不複製到任何地方。**

#### B-3. 確認網域解析

```bash
getent hosts youbike-data-mvp.duckdns.org
curl -s -4 https://ifconfig.me; echo
```

兩行的 IP 必須相同（第二行是 VM 實際對外的 IP）。

#### B-4. 確認 Ubuntu 自動安全更新

```bash
dpkg -l unattended-upgrades | tail -1
systemctl is-enabled unattended-upgrades
cat /etc/apt/apt.conf.d/20auto-upgrades
```

應為已安裝（`ii`）、`enabled`，且兩行設定值都是 `"1"`。

#### B-5. 安裝 Caddy（官方套件庫）並先停止

```bash
sudo apt install -y debian-keyring debian-archive-keyring apt-transport-https curl gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | sudo tee /etc/apt/sources.list.d/caddy-stable.list
sudo apt update && sudo apt install -y caddy
sudo systemctl stop caddy
caddy version
apt-cache policy caddy | head -4
```

安裝後先停止：套件附的預設設定會在 80 port 提供歡迎頁，換成本 repo 的設定前不讓它對外。

#### B-6. 加入 Caddy 自動更新

```bash
sudo install -m 644 -o root -g root /home/youbike/youbike-data-mvp/deploy/unattended-upgrades-caddy /etc/apt/apt.conf.d/52unattended-upgrades-caddy
sudo unattended-upgrade --dry-run --debug 2>&1 | grep -iE "allowed origins|caddy" | head -5
```

`Allowed origins` 那一行要包含 `origin=cloudsmith/caddy/stable`。

#### B-7. 安裝 Caddyfile 並檢查

```bash
sudo install -m 644 -o root -g root /home/youbike/youbike-data-mvp/deploy/Caddyfile /etc/caddy/Caddyfile
sudo caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
```

最後應出現 `Valid configuration`。

#### B-8. 開放 80、443（GCP Console）

VM 執行個體 →「編輯」→「網路」→ 勾選「允許 HTTP 流量」「允許 HTTPS 流量」→ 儲存。
到「防火牆」確認有來源 `0.0.0.0/0`、目標標記 `http-server`（tcp:80）與 `https-server`（tcp:443）的規則。

#### B-9. 啟動 Caddy 並確認取得憑證

```bash
sudo systemctl start caddy
sleep 30
systemctl is-active caddy
sudo journalctl -u caddy --since "-5 min" --no-pager | grep -iE "certificate obtained|error" | tail -5
```

應為 `active`，並出現 `certificate obtained successfully`。

### 驗收

【本機 PowerShell】（Windows 內建 `curl.exe`）：

```powershell
curl.exe -s -i https://youbike-data-mvp.duckdns.org/healthz
curl.exe -s -i https://youbike-data-mvp.duckdns.org/
curl.exe -s -i https://youbike-data-mvp.duckdns.org/admin
curl.exe -s -i http://youbike-data-mvp.duckdns.org/healthz
```

1. `/healthz`：狀態 200、內容 `ok`、**沒有 `Server` 標頭**。
2. `/`、`/admin`：狀態 404、內容為空、沒有 `Server` 標頭。
3. `http://`：狀態 308，`Location` 指向 `https://`。
4. 憑證：瀏覽器開 `https://youbike-data-mvp.duckdns.org/healthz` 不出現警告；簽發者為 Let's Encrypt。
5. 續期：由 Caddy 自動處理；日常檢查時以 `journalctl -u caddy | grep -i renew` 確認沒有錯誤。
6. 內部服務沒有對外：`sudo ss -ltnp` 只有 22、80、443 對外，`127.0.0.1:2019`（Caddy 管理介面）只聽本機，沒有 5432。
7. **部署後重新驗收 D1-a**：4C 連續成功、`free -m`（available ≥ 200 MB、swap 不持續增加）、`df -h`、OOM 次數為 0。

### D1-b 驗收紀錄

| 項目 | 結果 | 時間（UTC） |
|---|---|---|
| 固定 IP | 待填 | — |
| DNS 解析 | 待填 | — |
| 自動安全更新（Ubuntu、Caddy） | 待填 | — |
| Caddy 版本、憑證取得 | 待填 | — |
| `/healthz`、404、轉址、標頭 | 待填 | — |
| 對外連接埠 | 待填 | — |
| 部署後 4C、記憶體、swap、磁碟、OOM | 待填 | — |
