# D0：最小擷取部署（GCP e2-micro）

依《YouBike 2.0 專案設計定版 v1.3》11.2。只部署既有的 `collect.py`＋cron＋`flock`，
目的是提早累積平日尖峰、離峰與週末的真實資料。

- 不部署 PostgreSQL、API、Caddy；防火牆只開 SSH。
- 不修改 `src/`，不改變資料格式（gzip 檔名與內容、JSONL 欄位）。
- VM 上不放任何密碼、Token 或金鑰。

## 檔案

| 檔案 | 安裝位置 | 用途 |
|---|---|---|
| `crontab.txt` | `youbike` 使用者的 crontab | 每分鐘擷取（`flock -n`）、每 5 分鐘健康檢查 |
| `check_health.py` | 不安裝，從 repo 執行 | 唯讀檢查：最近成功擷取（> 10 分鐘警告）、磁碟使用率（≥ 80% 警告） |
| `logrotate-youbike` | `/etc/logrotate.d/youbike` | `collect.log`、`health.log` 每日輪替，保留 14 天，單檔 20 MB 上限 |
| `journald-youbike.conf` | `/etc/systemd/journald.conf.d/youbike.conf` | 系統日誌保留 14 天、最多 200 MB |
| `env.example` | 不安裝 | 環境變數說明（D0 只有 `YOUBIKE_DATA_DIR`） |

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
  logs/                         collect.log、health.log（logrotate 管理）
  collect.lock                  flock 鎖檔
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
| A 連續 24 小時 | 待確認：11/22 06:16 UTC（台灣 14:16）之後 | — |

- logrotate 試跑（`-d`）無 error；journald 上限生效時系統日誌佔 16 MB。
- **已知缺口**：06:23～06:36 為 B-2 驗收刻意暫停擷取，約 14 分鐘無資料，不補抓。
  驗收 A 統計 24 小時筆數時需扣除這段。

## 更新程式

```bash
sudo -iu youbike
cd youbike-data-mvp && git pull --ff-only
exit
```

`deploy/crontab.txt` 有變更時，需重新執行第 4 步的 `sudo crontab -u youbike ...`；
logrotate、journald 設定有變更時，重新執行第 5 步。
