#!/usr/bin/env python3
"""重试 prev_buyback_per_share=0 但 buyback_per_share>0 的可疑股票。"""
import os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pandas as pd
import logging
logging.basicConfig(level=logging.ERROR)

from collectors.hk_financial import get_fiscal_year_buyback

CSV = "data/csv/hk_financial.csv"

def main():
    df = pd.read_csv(CSV, dtype={"code": str})
    df["code"] = df["code"].astype(str).str.zfill(5)
    targets = df[(df['buyback_per_share']>0) & (df['prev_buyback_per_share']==0)]['code'].tolist()
    print(f"重试 {len(targets)} 只可疑股票...\n", flush=True)
    t0 = time.time()
    changed = 0
    for i, code in enumerate(targets):
        try:
            r = get_fiscal_year_buyback(code)
            new_pbps = r.get("prev_buyback_per_share", 0.0)
            new_ptb = r.get("prev_total_buyback", 0.0)
            if new_pbps > 0:
                idx = df.index[df['code']==code][0]
                df.at[idx, 'prev_buyback_per_share'] = new_pbps
                df.at[idx, 'prev_total_buyback'] = new_ptb
                changed += 1
        except Exception as e:
            print(f"  {code} 失败: {e}", flush=True)
        if (i+1) % 20 == 0:
            print(f"  {i+1}/{len(targets)}  改 {changed}  用时 {time.time()-t0:.0f}s", flush=True)
            df.to_csv(CSV, index=False)
    df.to_csv(CSV, index=False)
    print(f"\n=== 重试完成: 改 {changed} 只，用时 {time.time()-t0:.0f}s ===", flush=True)

if __name__ == "__main__":
    main()
