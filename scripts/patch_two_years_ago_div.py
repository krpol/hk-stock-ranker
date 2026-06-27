#!/usr/bin/env python3
"""为现有 hk_financial.csv 补充 two_years_ago_div_per_share 字段，
并按方案 A 规则重算 has_dividend_history。

方案 A 规则：最近 3 个财年里，有 2 个财年有过分红（含预分红公告）
  - FY_cur 有分红 = fy_div > 0 OR forward_div > 0
  - FY_cur-1 有分红 = prev_year_div > 0
  - FY_cur-2 有分红 = two_years_ago_div > 0
  - 三者之和 >= 2 → True
"""
import os, sys, time, shutil
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

logging.basicConfig(level=logging.ERROR)

from collectors.hk_financial import get_fiscal_year_dividends
from collectors.hk_forward_dividend import get_forward_div_map

CSV = "data/csv/hk_financial.csv"
BAK_DIR = "data/csv/bak"


def fetch_one(code: str, fwd_override):
    try:
        data = get_fiscal_year_dividends(code, forward_div_override=fwd_override)
        return code, data, None
    except Exception as e:
        return code, None, str(e)[:80]


def calc_has_div_history(fy_div, fwd_div, prev_div, two_yrs_div) -> bool:
    count = (
        (1 if fy_div > 0 or fwd_div > 0 else 0)
        + (1 if prev_div > 0 else 0)
        + (1 if two_yrs_div > 0 else 0)
    )
    return count >= 2


def main():
    os.makedirs(BAK_DIR, exist_ok=True)

    df = pd.read_csv(CSV, dtype={"code": str})
    df["code"] = df["code"].astype(str).str.zfill(5)
    print(f"总行: {len(df)}", flush=True)

    bak = os.path.join(BAK_DIR, "hk_financial_before_two_yrs_patch.csv")
    shutil.copy(CSV, bak)
    print(f"已备份: {bak}", flush=True)

    # 确保列存在
    if "two_years_ago_div_per_share" not in df.columns:
        df["two_years_ago_div_per_share"] = 0.0

    try:
        fwd_map = get_forward_div_map() or {}
    except Exception:
        fwd_map = {}

    codes = df["code"].tolist()
    code_to_idx = {r["code"]: i for i, r in df.iterrows()}

    t0 = time.time()
    completed = 0
    errors = 0
    changed_field = 0
    changed_hist = 0
    save_every = 50

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {pool.submit(fetch_one, c, fwd_map.get(c)): c for c in codes}
        for f in as_completed(futures):
            code, data, err = f.result()
            completed += 1
            if err:
                errors += 1
                continue

            idx = code_to_idx[code]
            two_yrs = float(data.get("two_years_ago_div_per_share", 0) or 0)

            # 写 two_years_ago_div_per_share
            old_two = float(df.at[idx, "two_years_ago_div_per_share"] or 0)
            if abs(two_yrs - old_two) > 1e-9:
                df.at[idx, "two_years_ago_div_per_share"] = two_yrs
                changed_field += 1

            # 重算 has_dividend_history
            fy_div  = float(data.get("fy_div_per_share", 0) or 0)
            fwd_div = float(data.get("forward_div_per_share", 0) or 0)
            prev_div = float(data.get("prev_year_div_per_share", 0) or 0)
            new_hist = calc_has_div_history(fy_div, fwd_div, prev_div, two_yrs)
            old_hist = bool(df.at[idx, "has_dividend_history"])
            if old_hist != new_hist:
                df.at[idx, "has_dividend_history"] = new_hist
                changed_hist += 1
                direction = "False→True" if new_hist else "True→False"
                print(f"  [{code}] has_dividend_history {direction}  fy={fy_div:.3f} fwd={fwd_div:.3f} prev={prev_div:.3f} two={two_yrs:.3f}", flush=True)

            if completed % save_every == 0:
                elapsed = time.time() - t0
                eta = elapsed / completed * (len(codes) - completed)
                print(f"  进度 {completed}/{len(codes)}  field变化 {changed_field}  hist变化 {changed_hist}  错误 {errors}  ETA {eta:.0f}s", flush=True)
                df.to_csv(CSV, index=False)

    df.to_csv(CSV, index=False)
    elapsed = time.time() - t0
    print(f"\n=== 完成 ({elapsed:.0f}s) ===")
    print(f"  two_years_ago 字段变化: {changed_field}")
    print(f"  has_dividend_history 变化: {changed_hist}")
    print(f"  错误: {errors}")
    n_true = df["has_dividend_history"].sum()
    print(f"  最终 has_dividend_history=True: {n_true} / {len(df)}")


if __name__ == "__main__":
    main()
