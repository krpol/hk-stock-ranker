#!/usr/bin/env python3
"""B40: 补充 prev_cash_and_equivalents + prev_interest_bearing_debt 两列。

从 _combine_series 序列第 2 期取上年现金及有息负债，用于 net_cash_change_rate 计算。
"""
import os, sys, time, shutil
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

logging.basicConfig(level=logging.ERROR)

from collectors.hk_financial import get_debt_ratios

CSV = "data/csv/hk_financial.csv"
BAK_DIR = "data/csv/bak"

NEW_COLS = ["prev_cash_and_equivalents", "prev_interest_bearing_debt"]


def fetch_one(code: str):
    try:
        data = get_debt_ratios(code) or {}
        return code, data, None
    except Exception as e:
        return code, None, str(e)[:80]


def main():
    os.makedirs(BAK_DIR, exist_ok=True)
    df = pd.read_csv(CSV, dtype={"code": str})
    df["code"] = df["code"].astype(str).str.zfill(5)
    print(f"总行: {len(df)}", flush=True)

    bak = os.path.join(BAK_DIR, "hk_financial_before_b40_prev_cash_patch.csv")
    shutil.copy(CSV, bak)
    print(f"已备份: {bak}", flush=True)

    for col in NEW_COLS:
        if col not in df.columns:
            df[col] = pd.NA

    codes = df["code"].tolist()
    code_to_idx = {r["code"]: i for i, r in df.iterrows()}

    t0 = time.time()
    completed = 0
    errors = 0
    save_every = 50

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(fetch_one, c): c for c in codes}
        for f in as_completed(futures):
            code, data, err = f.result()
            completed += 1
            if err:
                errors += 1
                print(f"  [{code}] ERROR: {err}", flush=True)
                continue
            idx = code_to_idx[code]
            for col in NEW_COLS:
                df.at[idx, col] = data.get(col)

            if completed % save_every == 0:
                elapsed = time.time() - t0
                eta = elapsed / completed * (len(codes) - completed)
                print(f"  进度 {completed}/{len(codes)}  错误 {errors}  ETA {eta:.0f}s", flush=True)
                df.to_csv(CSV, index=False)

    df.to_csv(CSV, index=False)
    elapsed = time.time() - t0
    print(f"\n=== 完成 ({elapsed:.0f}s) ===")
    print(f"  错误: {errors}")
    for col in NEW_COLS:
        n = df[col].notna().sum()
        print(f"  {col} 有效样本: {n} / {len(df)}")


if __name__ == "__main__":
    main()
