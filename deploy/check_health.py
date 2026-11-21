"""D0 健康檢查：最近成功擷取＋磁碟容量。由 cron 每 5 分鐘呼叫。

    python deploy/check_health.py
    python deploy/check_health.py --max-age-minutes 10 --disk-threshold 80

檢查兩件事，結果輸出成一行（交給 cron 導向 health.log）：

1. 最近成功擷取：fetch_batches.jsonl 最後一筆 status = success 的 fetched_at
   距離現在超過 --max-age-minutes（預設 10 分鐘）→ 警告。
   找不到 JSONL、或 JSONL 裡沒有任何 success 紀錄 → 也是警告。
2. 磁碟容量：--disk-path（預設 /）使用率達 --disk-threshold（預設 80%）→ 警告。

輸出格式（時間一律 UTC）：

    2026-11-21T06:00:00Z OK   last_success=2026-11-21T05:59:00+00:00 age=1.0m disk=16%
    2026-11-21T06:00:00Z WARN last_success=... age=12.0m disk=16% | stale: ...

結束碼：全部正常為 0，有任何警告為 1。

只讀取既有檔案，不寫入資料目錄、不修改 JSONL（定版 v1.3 11.2）。
只用標準函式庫，相容 Python 3.10。
資料目錄與 collect.py 相同：環境變數 YOUBIKE_DATA_DIR，預設為 data/。
"""

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

BATCH_LOG = "fetch_batches.jsonl"  # 與 collect.py 相同
STATUS_SUCCESS = "success"

DEFAULT_MAX_AGE_MINUTES = 10
DEFAULT_DISK_THRESHOLD = 80


def last_success_at(log_path: Path) -> datetime | None:
    """回傳 JSONL 中最後一筆 success 的 fetched_at；找不到時回傳 None。

    從最後一行往前找。無法解析的行（例如寫到一半的最後一行）直接略過，
    與 collect.append_batch 的復原規則一致。檔案不存在時拋出 FileNotFoundError。
    """
    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    for line in reversed(lines):
        try:
            record = json.loads(line)
            if record.get("status") != STATUS_SUCCESS:
                continue
            fetched_at = datetime.fromisoformat(record["fetched_at"])
        except (ValueError, TypeError, KeyError, AttributeError):
            continue
        if fetched_at.tzinfo is None:  # collect.py 一律寫 UTC，保險起見
            fetched_at = fetched_at.replace(tzinfo=timezone.utc)
        return fetched_at
    return None


def disk_usage_percent(path: str) -> float:
    """與 df 相同的算法：used / (used + avail)，不把保留給 root 的空間算進可用量。"""
    # shutil.disk_usage：used＝已用區塊（df 的 Used），free＝一般使用者可用量（df 的 Avail）。
    usage = shutil.disk_usage(path)
    denominator = usage.used + usage.free
    return usage.used / denominator * 100 if denominator else 0.0


def check(
    data_dir: Path,
    now: datetime,
    max_age: timedelta,
    disk_percent: float,
    disk_threshold: float,
) -> tuple[bool, str]:
    """執行兩項檢查，回傳 (是否全部正常, 一行結果)。"""
    warnings = []
    log_path = data_dir / BATCH_LOG

    try:
        last = last_success_at(log_path)
    except FileNotFoundError:
        last = None
        warnings.append(f"missing: {log_path} not found")
    except OSError as e:
        last = None
        warnings.append(f"unreadable: {log_path} ({e})")
    else:
        if last is None:
            warnings.append("no_success: no success record in batch log")

    if last is not None:
        age = now - last
        age_text = f"{age.total_seconds() / 60:.1f}m"
        if age > max_age:
            warnings.append(
                f"stale: last success {age_text} ago "
                f"(> {max_age.total_seconds() / 60:g}m)"
            )
        last_text = f"last_success={last.isoformat()} age={age_text}"
    else:
        last_text = "last_success=none"

    disk_text = f"disk={disk_percent:.0f}%"
    if disk_percent >= disk_threshold:
        warnings.append(f"disk: usage {disk_percent:.0f}% >= {disk_threshold:g}%")

    status = "WARN" if warnings else "OK  "
    line = f"{now:%Y-%m-%dT%H:%M:%SZ} {status} {last_text} {disk_text}"
    if warnings:
        line += " | " + "; ".join(warnings)
    return not warnings, line


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="D0 擷取健康檢查（唯讀）")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path(os.environ.get("YOUBIKE_DATA_DIR", "data")),
        help="資料目錄，預設為環境變數 YOUBIKE_DATA_DIR 或 data/",
    )
    parser.add_argument(
        "--max-age-minutes", type=float, default=DEFAULT_MAX_AGE_MINUTES
    )
    parser.add_argument(
        "--disk-threshold",
        type=float,
        default=DEFAULT_DISK_THRESHOLD,
        help="磁碟使用率警戒值（%%）",
    )
    parser.add_argument("--disk-path", default="/")
    args = parser.parse_args(argv)

    ok, line = check(
        data_dir=args.data_dir,
        now=datetime.now(timezone.utc),
        max_age=timedelta(minutes=args.max_age_minutes),
        disk_percent=disk_usage_percent(args.disk_path),
        disk_threshold=args.disk_threshold,
    )
    print(line, file=sys.stdout if ok else sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
