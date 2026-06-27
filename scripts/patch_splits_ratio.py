#!/usr/bin/env python3
"""增量补丁：为 hk_financial.csv 添加 splits_ratio_in_window 列。
   读 yfinance Ticker.splits，按 prev_fy_end 到现在的窗口累乘 split ratio。
   - 支持 resume（跳过已采集行，靠 splits_ratio_calculated 列）
   - 用并发线程池加速
"""
import os, sys, time, shutil
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pandas as pd
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
logging.basicConfig(level=logging.ERROR)

from collectors.hk_financial import _get_ticker, _get_splits_ratio_in_window

CSV = "data/csv/hk_financial.csv"
BAK = "data/csv/hk_financial.csv.bak.splits_ratio"
WORKERS = 8


def fetch_one(code: str, prev_fy_end):
    if pd.isna(prev_fy_end) or not prev_fy_end:
        return code, 1.0
    try:
        t = _get_ticker(code)
        ratio = _get_splits_ratio_in_window(t, pd.Timestamp(prev_fy_end))
    except Exception:
        ratio = 1.0
    return code, ratio


def main():
    df = pd.read_csv(CSV, dtype={"code": str})
    df["code"] = df["code"].astype(str).str.zfill(5)

    if not os.path.exists(BAK):
        shutil.copy(CSV, BAK)
        print(f"已备份: {BAK}", flush=True)

    if "splits_ratio_in_window" not in df.columns:
        df["splits_ratio_in_window"] = 1.0
    if "splits_ratio_calculated" not in df.columns:
        df["splits_ratio_calculated"] = False

    if "prev_fy_end" not in df.columns:
        print("ERROR: CSV 缺 prev_fy_end 列")
        return

    # 找出未处理的行
    todo_mask = ~df["splits_ratio_calculated"].fillna(False).astype(bool)
    todo_df = df[todo_mask]
    print(f"总行: {len(df)}，待处理: {len(todo_df)}（已处理 {len(df) - len(todo_df)}）", flush=True)
    if len(todo_df) == 0:
        print("已全部处理完")
        return

    t0 = time.time()
    nontrivial = 0
    completed = 0
    save_every = 50

    code_to_idx = {row["code"]: i for i, row in todo_df.iterrows()}
    code_to_prev = {row["code"]: row.get("prev_fy_end") for _, row in todo_df.iterrows()}

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = {pool.submit(fetch_one, code, prev_fy_end): code for code, prev_fy_end in code_to_prev.items()}
        for f in as_completed(futures):
            code, ratio = f.result()
            idx = code_to_idx[code]
            df.at[idx, "splits_ratio_in_window"] = ratio
            df.at[idx, "splits_ratio_calculated"] = True
            completed += 1
            if abs(ratio - 1.0) > 1e-6:
                nontrivial += 1
                name = df.at[idx, "code"]
                print(f"  [{name}] splits_ratio = {ratio} (prev_fy_end={code_to_prev[code]})", flush=True)
            if completed % save_every == 0:
                elapsed = time.time() - t0
                eta = elapsed / completed * (len(todo_df) - completed)
                print(f"  进度 {completed}/{len(todo_df)}  非平凡 {nontrivial}  用时 {elapsed:.0f}s  ETA {eta:.0f}s", flush=True)
                df.to_csv(CSV, index=False)

    df.to_csv(CSV, index=False)
    elapsed = time.time() - t0
    total_nontriv = (df["splits_ratio_in_window"].round(6) != 1.0).sum()
    print(f"\n=== 完成（{elapsed:.0f}s）本轮非平凡 {nontrivial}，全集累计 {total_nontriv} 只 ===", flush=True)


if __name__ == "__main__":
    main()
