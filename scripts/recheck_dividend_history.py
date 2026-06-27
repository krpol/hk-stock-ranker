#!/usr/bin/env python3
"""重新校验 hk_financial.csv 中所有股票的 has_dividend_history 字段。

对 CSV 中每一行实时调用 has_dividend_history()，与旧值比较，
只更新有差异的行，保留其他所有字段不变。

适用场景：yfinance 数据更新后，旧 CSV 的 has_dividend_history
快照已失效，需要重新校验。
"""
import os
import sys
import time
import shutil
import logging
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from collectors.hk_financial import has_dividend_history
from config.settings import HK_STOCK_FILTERS

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)

CSV = "data/csv/hk_financial.csv"
BAK_DIR = "data/csv/bak"


def check_one(code: str, years: int, min_years: int):
    """单只股票校验，返回 (code, new_val, error_str)"""
    try:
        val = has_dividend_history(code, years=years, min_years_with_div=min_years)
        return code, val, None
    except Exception as e:
        return code, None, str(e)[:100]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--years", type=int, default=None,
                        help="要检查的年数（默认从 settings 读取，通常为 3）")
    parser.add_argument("--min-years", type=int, default=2,
                        help="最少有分红的年数（默认 2）")
    args = parser.parse_args()

    years = args.years or HK_STOCK_FILTERS.get("require_dividend_years", 3)
    min_years = args.min_years

    os.makedirs(BAK_DIR, exist_ok=True)

    df = pd.read_csv(CSV, dtype={"code": str})
    df["code"] = df["code"].astype(str).str.zfill(5)
    print(f"总行: {len(df)}，years={years}，min_years={min_years}", flush=True)

    bak_path = os.path.join(BAK_DIR, f"hk_financial_before_hist_recheck.csv")
    shutil.copy(CSV, bak_path)
    print(f"已备份: {bak_path}", flush=True)

    codes = df["code"].tolist()
    code_to_idx = {row["code"]: i for i, row in df.iterrows()}

    t0 = time.time()
    completed = 0
    changed_to_false = 0
    changed_to_true = 0
    errors = 0
    save_every = 50

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(check_one, c, years, min_years): c for c in codes
        }
        for f in as_completed(futures):
            code, new_val, err = f.result()
            completed += 1

            if err is not None:
                errors += 1
                if errors <= 5:
                    print(f"  [{code}] error: {err}")
            elif new_val is not None:
                idx = code_to_idx[code]
                old_val = bool(df.at[idx, "has_dividend_history"])
                if old_val != new_val:
                    df.at[idx, "has_dividend_history"] = new_val
                    if new_val:
                        changed_to_true += 1
                        print(f"  [{code}] False → True")
                    else:
                        changed_to_false += 1
                        print(f"  [{code}] True → False  *** 旧值有误 ***")

            if completed % save_every == 0:
                elapsed = time.time() - t0
                eta = elapsed / completed * (len(codes) - completed)
                print(f"  进度 {completed}/{len(codes)}  True→False {changed_to_false}  False→True {changed_to_true}  错误 {errors}  ETA {eta:.0f}s", flush=True)
                df.to_csv(CSV, index=False)

    df.to_csv(CSV, index=False)
    elapsed = time.time() - t0
    print(f"\n=== 完成 ({elapsed:.0f}s) ===")
    print(f"  True → False（旧值误判）: {changed_to_false}")
    print(f"  False → True（旧值漏判）: {changed_to_true}")
    print(f"  错误（yfinance 无数据）: {errors}")
    print(f"  总变化: {changed_to_false + changed_to_true}")

    # 统计
    n_true = df["has_dividend_history"].sum()
    print(f"\n  最终 has_dividend_history=True: {n_true} 只 / {len(df)} 只")


if __name__ == "__main__":
    main()
