#!/usr/bin/env python3
"""增量补丁：仅为现有 hk_financial.csv 添加 prev_buyback_per_share 列（与 prev_total_buyback）。
   不重新计算分红/稀释/负债等其他字段。
"""
import os, sys, time, shutil
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pandas as pd
import logging
logging.basicConfig(level=logging.ERROR)

from collectors.hk_financial import get_fiscal_year_buyback

CSV = "data/csv/hk_financial.csv"
BAK = "data/csv/hk_financial.csv.bak.prev_buyback"

NEW_COLS = ["prev_buyback_per_share", "prev_total_buyback"]


def main():
    df = pd.read_csv(CSV, dtype={"code": str})
    df["code"] = df["code"].astype(str).str.zfill(5)

    # backup
    if not os.path.exists(BAK):
        shutil.copy(CSV, BAK)
        print(f"已备份: {BAK}", flush=True)

    # 添加新列（若不存在）
    for col in NEW_COLS:
        if col not in df.columns:
            df[col] = 0.0

    total = len(df)
    print(f"开始采集 {total} 只股票的上年回购...\n", flush=True)
    t0 = time.time()
    fail = []

    for i, row in df.iterrows():
        code = row["code"]
        try:
            r = get_fiscal_year_buyback(code)
            df.at[i, "prev_buyback_per_share"] = r.get("prev_buyback_per_share", 0.0)
            df.at[i, "prev_total_buyback"] = r.get("prev_total_buyback", 0.0)
        except Exception as e:
            fail.append((code, str(e)))

        if (i + 1) % 30 == 0:
            elapsed = time.time() - t0
            eta = elapsed / (i + 1) * (total - i - 1)
            print(f"  {i+1}/{total}  失败 {len(fail)}  用时 {elapsed:.0f}s  ETA {eta:.0f}s", flush=True)
            # 中途保存
            df.to_csv(CSV, index=False)

    df.to_csv(CSV, index=False)
    elapsed = time.time() - t0
    print(f"\n=== 完成（{elapsed:.0f}s）失败 {len(fail)} ===", flush=True)
    if fail[:10]:
        for code, e in fail[:10]:
            print(f"  {code}: {e}", flush=True)


if __name__ == "__main__":
    main()
