"""
B31: 剩余可用现金流偿债覆盖率指标 回测验证。

对比项：
  1. 原 filter_debt_to_equity (DE > 0.70) 砍掉的股票在新指标下的评级分布
  2. 当前 TOP 50 在新指标下的评级分布
  3. 新指标 filter (frfc) 单独使用时的砍杀情况
  4. 哪些公司被新指标"救回"或"新发现风险"

用法：
    python scripts/verify_real_fcf.py [--period 2026-06]
"""
import sys
import argparse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd

from web.app import _attach_cashflow_factors
from storage.store import load_ranking, list_ranking_periods


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--period", default=None)
    args = parser.parse_args()

    periods = list_ranking_periods()
    period = args.period or periods[0]
    print(f"Period: {period}\n")

    df = load_ranking(period)
    df = _attach_cashflow_factors(df)
    total = len(df)
    print(f"Total stocks: {total}")
    print()

    # 1. 评级分布（全池）
    print("=" * 60)
    print("1. fcf_rating 分布（全池）")
    print("=" * 60)
    dist = df["fcf_rating"].value_counts()
    for k, v in dist.items():
        print(f"  {k:8s}  {v:4d}  ({v/total*100:.1f}%)")
    print()

    # 2. 原 DE>0.70 砍掉的股票，在新指标下的评级
    print("=" * 60)
    print("2. DE>0.70 砍掉的股票（B19）在新指标下的评级")
    print("=" * 60)
    de = pd.to_numeric(df["debt_to_equity"], errors="coerce")
    de_cut = df[de > 0.70].copy()
    print(f"DE>0.70 砍掉: {len(de_cut)} 只")
    print()
    de_cut_dist = de_cut["fcf_rating"].value_counts()
    for k, v in de_cut_dist.items():
        print(f"  {k:8s}  {v:4d}  ({v/len(de_cut)*100:.1f}%)")
    print()
    # 被救回（DE>0.70 但 rating in STRONG/OK/AAA）
    saved = de_cut[de_cut["fcf_rating"].isin(["STRONG", "OK", "AAA"])]
    print(f"被新指标救回（STRONG/OK/AAA）: {len(saved)} 只")
    if len(saved):
        cols = ["code", "name", "debt_to_equity", "real_fcf_yi", "coverage_ratio", "fcf_rating"]
        cols = [c for c in cols if c in saved.columns]
        s = saved[cols].copy()
        s["coverage_ratio"] = pd.to_numeric(s["coverage_ratio"], errors="coerce").apply(
            lambda v: f"{v*100:.0f}%" if pd.notna(v) else "—"
        )
        s["real_fcf_yi"] = pd.to_numeric(s["real_fcf_yi"], errors="coerce").round(1)
        s["debt_to_equity"] = pd.to_numeric(s["debt_to_equity"], errors="coerce").round(2)
        s = s.sort_values("real_fcf_yi", ascending=False).head(20)
        print(s.to_string(index=False))
    print()

    # 3. DE<=0.70 但新指标 DROP（"零负债陷阱"被发现）
    print("=" * 60)
    print("3. DE<=0.70 但被新指标 DROP（零负债陷阱）")
    print("=" * 60)
    de_keep = df[(de <= 0.70) | de.isna()].copy()
    trap = de_keep[de_keep["fcf_rating"] == "DROP"]
    print(f"被新指标新发现风险: {len(trap)} 只")
    if len(trap):
        cols = ["code", "name", "debt_to_equity", "real_fcf_yi", "coverage_ratio", "fcf_rating"]
        cols = [c for c in cols if c in trap.columns]
        s = trap[cols].copy()
        s["coverage_ratio"] = pd.to_numeric(s["coverage_ratio"], errors="coerce").apply(
            lambda v: f"{v*100:.0f}%" if pd.notna(v) else "—"
        )
        s["real_fcf_yi"] = pd.to_numeric(s["real_fcf_yi"], errors="coerce").round(1)
        s["debt_to_equity"] = pd.to_numeric(s["debt_to_equity"], errors="coerce").round(2)
        s = s.sort_values("real_fcf_yi", ascending=True).head(20)
        print(s.to_string(index=False))
    print()

    # 4. 当前 TOP 50 在新指标下的评级
    print("=" * 60)
    print("4. 当前 TOP 50 在新指标下的评级")
    print("=" * 60)
    if "rank" in df.columns:
        top = df[df["rank"] <= 50]
    else:
        top = df.head(50)
    top_dist = top["fcf_rating"].value_counts()
    for k, v in top_dist.items():
        print(f"  {k:8s}  {v:4d}  ({v/len(top)*100:.1f}%)")
    print()
    # TOP50 中 DROP 的（关注）
    top_drop = top[top["fcf_rating"] == "DROP"]
    print(f"TOP 50 中 DROP 评级: {len(top_drop)} 只")
    if len(top_drop):
        cols = ["rank", "code", "name", "real_fcf_yi", "coverage_ratio", "fcf_rating", "debt_to_equity"]
        cols = [c for c in cols if c in top_drop.columns]
        s = top_drop[cols].copy()
        s["coverage_ratio"] = pd.to_numeric(s["coverage_ratio"], errors="coerce").apply(
            lambda v: f"{v*100:.0f}%" if pd.notna(v) else "—"
        )
        s["real_fcf_yi"] = pd.to_numeric(s["real_fcf_yi"], errors="coerce").round(1)
        s["debt_to_equity"] = pd.to_numeric(s["debt_to_equity"], errors="coerce").round(2)
        print(s.to_string(index=False))
    print()

    # 5. 现金堡垒公司
    print("=" * 60)
    print("5. 现金堡垒（AAA）公司")
    print("=" * 60)
    aaa = df[df["fcf_rating"] == "AAA"]
    print(f"AAA: {len(aaa)} 只")
    if len(aaa):
        cols = ["rank", "code", "name", "real_fcf_yi", "coverage_ratio", "fcf_rating"]
        cols = [c for c in cols if c in aaa.columns]
        s = aaa[cols].copy()
        s["coverage_ratio"] = pd.to_numeric(s["coverage_ratio"], errors="coerce").apply(
            lambda v: f"{v*100:.0f}%" if pd.notna(v) else "—"
        )
        s["real_fcf_yi"] = pd.to_numeric(s["real_fcf_yi"], errors="coerce").round(1)
        print(s.head(20).to_string(index=False))
    print()


if __name__ == "__main__":
    main()
