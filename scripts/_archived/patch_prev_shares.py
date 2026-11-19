#!/usr/bin/env python3
"""增量补丁：为 hk_financial.csv 添加 prev_shares_outstanding 列。
   只读 yfinance balance_sheet，找 prev_fy_end 那一列的股本。
"""
import os, sys, time, shutil
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pandas as pd
import logging
logging.basicConfig(level=logging.ERROR)

from collectors.hk_financial import _get_ticker, get_fiscal_year_dates

CSV = "data/csv/hk_financial.csv"
BAK = "data/csv/hk_financial.csv.bak.prev_shares"


def get_shares_pair(code: str):
    """返回 (cur_shares, prev_shares) 两个 fy_end 对应的股本。"""
    try:
        prev_fy_end, current_fy_end = get_fiscal_year_dates(code)
        if current_fy_end is None:
            return None, None
        t = _get_ticker(code)
        bs = t.balance_sheet
        if bs is None or bs.empty:
            return None, None
        cur_s, prev_s = None, None
        for col in bs.columns:
            ts = pd.Timestamp(col)
            for key in ["Ordinary Shares Number", "CommonStock", "ShareIssued"]:
                if key in bs.index:
                    v = bs.loc[key, col]
                    if pd.notna(v) and float(v) > 0:
                        if ts == current_fy_end and cur_s is None:
                            cur_s = float(v)
                        elif prev_fy_end is not None and ts == prev_fy_end and prev_s is None:
                            prev_s = float(v)
                        break
        return cur_s, prev_s
    except Exception:
        return None, None


def main():
    df = pd.read_csv(CSV, dtype={"code": str})
    df["code"] = df["code"].astype(str).str.zfill(5)

    if not os.path.exists(BAK):
        shutil.copy(CSV, BAK)
        print(f"已备份: {BAK}", flush=True)

    if "prev_shares_outstanding" not in df.columns:
        df["prev_shares_outstanding"] = None

    total = len(df)
    print(f"开始采集 {total} 只股票的上年股本...", flush=True)
    t0 = time.time()
    fail, missing = 0, 0
    for i, row in df.iterrows():
        code = row["code"]
        cur, prev = get_shares_pair(code)
        if prev is not None:
            df.at[i, "prev_shares_outstanding"] = prev
        else:
            missing += 1
        if (i + 1) % 30 == 0:
            elapsed = time.time() - t0
            eta = elapsed / (i + 1) * (total - i - 1)
            print(f"  {i+1}/{total}  缺数据 {missing}  用时 {elapsed:.0f}s  ETA {eta:.0f}s", flush=True)
            df.to_csv(CSV, index=False)

    df.to_csv(CSV, index=False)
    elapsed = time.time() - t0
    print(f"\n=== 完成（{elapsed:.0f}s）缺数据 {missing} ===", flush=True)


if __name__ == "__main__":
    main()
