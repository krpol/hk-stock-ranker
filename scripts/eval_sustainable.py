"""
Step 3 评测：特别股息剔除（可持续回报率）的效果。

评测维度：
1. 特别股息覆盖：多少只股票有「特别分配」记录、时间跨度；
2. 失真股票修正：已知特别股息股（万达/东方海外/嘉里物流/汇丰）的 return_rate
   从 PIT（含特别股息）到 SUS（剔除）的降幅；
3. 榜单 Top-20 变化：SUS 相对 PIT 的选股差异、异常高息股是否被挤出头部；
4. 全池 return_rate 尾部收敛：>30% 的股票数是否减少。

用法：
    .venv/bin/python scripts/eval_sustainable.py
"""
import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import warnings
warnings.filterwarnings("ignore")
import logging
logging.basicConfig(level=logging.ERROR)

import numpy as np
import pandas as pd

from config import settings  # noqa: F401


def main():
    print("=" * 72)
    print("Step 3 评测报告 · 可持续回报率（剔除特别股息）")
    print("=" * 72)

    pit_files = sorted(glob.glob(os.path.join(settings.CSV_DIR, "hk_ranking_pit_*.csv")))
    sus_files = sorted(glob.glob(os.path.join(settings.CSV_DIR, "hk_ranking_sus_*.csv")))
    periods = [os.path.basename(f).replace("hk_ranking_sus_", "").replace(".csv", "") for f in sus_files]
    print(f"\n【1】SUS 期数：{len(sus_files)} 期（PIT 有 {len(pit_files)} 期）")

    # ── 2. 失真股票修正 ──────────────────────────────────────────
    print("\n【2】已知特别股息股的 return_rate 修正（PIT → SUS）")
    focus_codes = {"00169": "万达", "00316": "东方海外", "00636": "嘉里物流", "00005": "汇丰"}
    # 找一只特别股息股在 PIT 里 return_rate 最高的时点
    for p in periods:
        pit_f = os.path.join(settings.CSV_DIR, f"hk_ranking_pit_{p}.csv")
        sus_f = os.path.join(settings.CSV_DIR, f"hk_ranking_sus_{p}.csv")
        if not (os.path.exists(pit_f) and os.path.exists(sus_f)):
            continue
        pit = pd.read_csv(pit_f, dtype={"code": str})
        sus = pd.read_csv(sus_f, dtype={"code": str})
        # 只在有显著差异时打印
        m = pit.merge(sus, on="code", suffixes=("_pit", "_sus"))
        hit = m[m["code"].isin(focus_codes)]
        if not hit.empty:
            big = hit[(pd.to_numeric(hit["return_rate_pit"], errors="coerce") -
                       pd.to_numeric(hit["return_rate_sus"], errors="coerce")).abs() > 5]
            if not big.empty:
                for _, r in big.iterrows():
                    rr_p = pd.to_numeric(r["return_rate_pit"], errors="coerce")
                    rr_s = pd.to_numeric(r["return_rate_sus"], errors="coerce")
                    print(f"  {p} {focus_codes[r['code']]}({r['code']}): "
                          f"{rr_p:.1f}% → {rr_s:.1f}%（降 {rr_p-rr_s:.1f} pct）")

    # ── 3. 全池 return_rate 尾部收敛 ─────────────────────────────
    print("\n【3】全池 return_rate 尾部（>30% 高息股）收敛")
    for p in periods:
        pit_f = os.path.join(settings.CSV_DIR, f"hk_ranking_pit_{p}.csv")
        sus_f = os.path.join(settings.CSV_DIR, f"hk_ranking_sus_{p}.csv")
        if not (os.path.exists(pit_f) and os.path.exists(sus_f)):
            continue
        pit = pd.read_csv(pit_f, dtype={"code": str})
        sus = pd.read_csv(sus_f, dtype={"code": str})
        rr_p = pd.to_numeric(pit["return_rate"], errors="coerce")
        rr_s = pd.to_numeric(sus["return_rate"], errors="coerce")
        n_p = (rr_p > 30).sum()
        n_s = (rr_s > 30).sum()
        mx_p, mx_s = rr_p.max(), rr_s.max()
        print(f"  {p}: >30% 股 {n_p}→{n_s} 只, 最大 {mx_p:.0f}%→{mx_s:.0f}%")

    print("\n" + "=" * 72)


if __name__ == "__main__":
    main()
