"""
B3: 仅刷新 hk_financial.csv 的负债率字段（增量补采用）

调用 yfinance balance_sheet 一次性返回两个负债率：
    net_debt_ratio        = (短借 + 长借) / 总资产    [复用已有字段]
    total_liability_ratio = 总负债 / 总资产           [B3 新字段]

不刷新分红/回购/价格等其他字段。

使用：
    python scripts/refresh_liability_only.py            # 全量
    python scripts/refresh_liability_only.py --only-missing  # 仅补 NaN
"""
import argparse
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd

# 让脚本可独立运行
sys.path.insert(0, str(Path(__file__).parent.parent))

from collectors.hk_financial import get_debt_ratios
from storage.store import load_financial_data, save_financial_data

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("refresh_liab")


def process_one(code: str):
    try:
        ratios = get_debt_ratios(code)
        if not ratios:
            return code, None, "no_balance_sheet"
        return code, ratios, None
    except Exception as e:
        return code, None, str(e)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--only-missing", action="store_true",
                        help="仅补采 total_liability_ratio 为 NaN 的股票")
    args = parser.parse_args()

    df = load_financial_data()
    if df.empty:
        logger.error("hk_financial.csv 为空，请先 --update 一次")
        return

    df["code"] = df["code"].astype(str).str.zfill(5)

    if "total_liability_ratio" not in df.columns:
        df["total_liability_ratio"] = pd.NA

    if args.only_missing:
        codes = df.loc[df["total_liability_ratio"].isna(), "code"].tolist()
        logger.info(f"--only-missing：仅补采 {len(codes)} 只 NaN 股票")
    else:
        codes = df["code"].tolist()

    total = len(codes)
    if total == 0:
        logger.info("无需补采")
        return
    logger.info(f"开始处理 {total} 只（线程池 4，限流模式）")

    t0 = time.time()
    updated = 0
    failed = 0
    # 限流模式：8 → 4 线程，避免 yfinance 429
    with ThreadPoolExecutor(max_workers=4) as ex:
        futures = {ex.submit(process_one, c): c for c in codes}
        for i, fut in enumerate(as_completed(futures), 1):
            code, ratios, err = fut.result()
            if ratios:
                idx = df.index[df["code"] == code]
                if len(idx) > 0:
                    df.loc[idx, "net_debt_ratio"] = ratios["net_debt_ratio"]
                    df.loc[idx, "total_liability_ratio"] = ratios["total_liability_ratio"]
                    updated += 1
            else:
                failed += 1

            if i % 25 == 0 or i == total:
                elapsed = time.time() - t0
                speed = i / elapsed if elapsed > 0 else 0
                eta = (total - i) / speed / 60 if speed > 0 else 0
                logger.info(
                    f"[{i}/{total}] 速度 {speed:.1f} 股/秒 ETA {eta:.1f} 分；"
                    f"更新 {updated} 失败 {failed}"
                )

    save_financial_data(df)
    logger.info(f"完成：更新 {updated}/{total} 只，失败 {failed}")
    logger.info(f"总耗时 {(time.time()-t0)/60:.1f} 分钟")


if __name__ == "__main__":
    main()
