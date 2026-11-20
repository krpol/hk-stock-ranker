"""
Step 2 评测：PIT 重建 vs 旧回填 的选股差异与前视偏差影响。

评测维度：
1. 期数覆盖：实际生成多少期 PIT ranking；
2. 主因子差异：return_rate 的 PIT vs 旧回填 的 Spearman 相关、|差值|分布；
3. 选股差异：composite_score Top-20 的重叠率（前视偏差改变了多少选股决策）；
4. 汇总：前视偏差对回测 alpha 的影响评估。

用法：
    .venv/bin/python scripts/eval_pit_backfill.py
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
    print("Step 2 评测报告 · PIT 重建 vs 旧回填")
    print("=" * 72)

    pit_files = sorted(glob.glob(os.path.join(settings.CSV_DIR, "hk_ranking_pit_*.csv")))
    periods = [os.path.basename(f).replace("hk_ranking_pit_", "").replace(".csv", "") for f in pit_files]
    print(f"\n【1】期数覆盖：PIT 生成 {len(periods)} 期（{periods[0]} ~ {periods[-1]}）")

    # ── 2. return_rate 差异 ──────────────────────────────────────
    print("\n【2】return_rate 主因子差异（PIT vs 旧回填）")
    sps, maxdiffs, meddiffs = [], [], []
    for p in periods:
        old_f = os.path.join(settings.CSV_DIR, f"hk_ranking_{p}.csv")
        if not os.path.exists(old_f):
            continue
        old = pd.read_csv(old_f, dtype={"code": str})
        pit = pd.read_csv(os.path.join(settings.CSV_DIR, f"hk_ranking_pit_{p}.csv"), dtype={"code": str})
        m = old.merge(pit, on="code", suffixes=("_old", "_pit"))
        if len(m) < 20:
            continue
        ro = pd.to_numeric(m["return_rate_old"], errors="coerce")
        rp = pd.to_numeric(m["return_rate_pit"], errors="coerce")
        valid = ro.notna() & rp.notna() & ((ro > 0) | (rp > 0))
        if valid.sum() < 20:
            continue
        sps.append(ro[valid].rank().corr(rp[valid].rank()))
        d = (ro[valid] - rp[valid]).abs()
        maxdiffs.append(d.max())
        meddiffs.append(d.median())

    if sps:
        print(f"  Spearman 相关（return_rate PIT vs 旧）：中位 {np.median(sps):.3f}，"
              f"最低 {min(sps):.3f}（越低说明前视偏差扭曲越严重）")
        print(f"  |return_rate 差值|：中位 {np.median(meddiffs):.2f} 个百分点，"
              f"最大 {max(maxdiffs):.1f} 个百分点")

    # ── 3. Top-20 选股重叠 ───────────────────────────────────────
    print("\n【3】composite_score Top-20 选股重叠率")
    overlaps, jaccards = [], []
    for p in periods:
        old_f = os.path.join(settings.CSV_DIR, f"hk_ranking_{p}.csv")
        if not os.path.exists(old_f):
            continue
        old = pd.read_csv(old_f, dtype={"code": str})
        pit = pd.read_csv(os.path.join(settings.CSV_DIR, f"hk_ranking_pit_{p}.csv"), dtype={"code": str})
        o20 = set(old.nlargest(20, "composite_score")["code"])
        p20 = set(pit.nlargest(20, "composite_score")["code"])
        if not o20 or not p20:
            continue
        inter = o20 & p20
        overlaps.append(len(inter))
        jaccards.append(len(inter) / len(o20 | p20))

    if overlaps:
        print(f"  Top-20 重叠股票数：中位 {np.median(overlaps):.0f}/20，"
              f"最低 {min(overlaps)}/20，最高 {max(overlaps)}/20")
        print(f"  Top-20 Jaccard 相似度：中位 {np.median(jaccards):.1%}（1=完全相同，0=完全不同）")

    # ── 4. 汇总结论 ──────────────────────────────────────────────
    print("\n【4】结论")
    if sps:
        med_spear = np.median(sps)
        if med_spear < 0.9:
            print(f"  前视偏差对主因子排名造成显著扭曲（中位 Spearman {med_spear:.3f} < 0.9），")
            print(f"  旧回填的 Top-N 选股与「时点正确」的选股差异显著，")
            print(f"  因此旧回测的 alpha 不可信，需用 PIT 数据重跑回测（Step 6）。")
        else:
            print(f"  主因子排名高度相关（Spearman {med_spear:.3f}），前视偏差影响有限。")

    print("\n" + "=" * 72)


if __name__ == "__main__":
    main()
