"""
Step 1 评测：Point-in-Time 数据层（分红）的覆盖率与前视偏差量化。

评测三个维度：
1. 数据覆盖：分红历史序列的股票数、记录数、时间跨度、每股条数分布；
2. 时点覆盖率：回测区间（月度调仓时点）上，有时点分红数据的股票比例；
3. 前视偏差：as_of(t) 时点值 vs 当前快照值的差异——量化「用快照回填会虚高多少」。

用法：
    .venv/bin/python scripts/eval_point_in_time.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import warnings
warnings.filterwarnings("ignore")
import logging
logging.basicConfig(level=logging.ERROR)

import numpy as np
import pandas as pd

from config import settings  # noqa: F401  触发代理
from backtest.point_in_time import load_dividend_history, ttm_dividend_as_of


def _fmt_pct(x, digits=1):
    return "—" if pd.isna(x) else f"{x*100:.{digits}f}%"


def main():
    print("=" * 72)
    print("Step 1 评测报告 · Point-in-Time 数据层（分红）")
    print("=" * 72)

    hist = load_dividend_history()
    fin = pd.read_csv(os.path.join(settings.CSV_DIR, "hk_financial.csv"), dtype={"code": str})
    pool = pd.read_csv(os.path.join(settings.CSV_DIR, "hk_stock_pool.csv"), dtype={"code": str})

    # ── 1. 数据覆盖 ──────────────────────────────────────────────
    print("\n【1】数据覆盖")
    n_stocks = hist["code"].nunique() if not hist.empty else 0
    print(f"  分红历史：{n_stocks} 只股票、{len(hist)} 条除息记录")
    if not hist.empty:
        print(f"  时间跨度：{hist['ex_date'].min().date()} ~ {hist['ex_date'].max().date()}")
        per = hist.groupby("code").size()
        print(f"  每股除息条数：中位数 {per.median():.0f}，最小 {per.min()}，最大 {per.max()}")
        # 股票池覆盖率
        pool_codes = set(pool["code"].astype(str).str.zfill(5))
        covered = set(hist["code"])
        print(f"  股票池覆盖：{len(covered & pool_codes)}/{len(pool_codes)} = {len(covered & pool_codes)/len(pool_codes):.1%}")

    # ── 2. 时点覆盖率 ────────────────────────────────────────────
    print("\n【2】时点覆盖率（回测区间月度时点）")
    price = pd.read_csv(os.path.join(settings.CSV_DIR, "hk_monthly_price.csv"), dtype={"code": str})
    price["date"] = pd.to_datetime(price["date"])
    months = sorted(price["date"].dt.to_period("M").unique())
    months = [m for m in months if m >= pd.Period("2021-12", "M")]  # 回测从 2021-12 起
    print(f"  回测月度时点数：{len(months)}（{months[0]} ~ {months[-1]}）")

    # 抽样若干时点测覆盖率（全部 61 个时点全测太慢，抽 6 个 + 首尾）
    sample_idx = sorted(set([0, len(months)//5, 2*len(months)//5, 3*len(months)//5, 4*len(months)//5, len(months)-1]))
    print(f"  {'时点':12} {'有时点分红数据的股票':>16} {'占股票池':>10}")
    for i in sample_idx:
        m = months[i]
        as_of = m.to_timestamp("M")
        # 该时点股票池 = 该月有价格的股票
        month_codes = set(price[price["date"].dt.to_period("M") == m]["code"])
        n_with_data = sum(1 for c in list(month_codes)[:200] if ttm_dividend_as_of(c, as_of, div_history=hist) is not None)
        print(f"  {str(m):12} {n_with_data:>16} {n_with_data/200:>9.1%}（抽样200只）")

    # ── 3. 前视偏差量化 ──────────────────────────────────────────
    print("\n【3】前视偏差量化（快照值 / 时点值 的分布）")
    print("  偏差比 > 1 表示「用今日快照回填」会比「当时真实值」虚高。")
    if hist.empty:
        print("  （无数据，跳过）")
        return

    ratios_all = []
    ratios_by_year = {}
    for y in [2022, 2023, 2024, 2025]:
        as_of = pd.Timestamp(f"{y}-06-30")
        ratios = []
        for code in list(pool["code"].astype(str).str.zfill(5)):
            cur = fin.loc[fin["code"] == code, "fy_div_per_share"]
            if cur.empty:
                continue
            cur_v = cur.iloc[0]
            pit_v = ttm_dividend_as_of(code, as_of, div_history=hist)
            if pit_v is None or pit_v <= 0 or pd.isna(cur_v) or cur_v <= 0:
                continue
            ratios.append(cur_v / pit_v)
        if ratios:
            ratios_by_year[y] = np.array(ratios)
            ratios_all.extend(ratios)
        arr = np.array(ratios) if ratios else np.array([])
        print(f"  {y}-06 时点：样本 {len(arr)} 只，中位 {np.median(arr):.2f}，P90 {np.percentile(arr,90):.2f}，"
              f"P99 {np.percentile(arr,99):.2f}，>2倍占比 {np.mean(arr>2):.1%}")

    if ratios_all:
        arr = np.array(ratios_all)
        print(f"\n  汇总（4 个时点）：中位 {np.median(arr):.2f}，>2倍占比 {np.mean(arr>2):.1f}，"
              f">5倍占比 {np.mean(arr>5):.1f}，极端最大值 {arr.max():.1f}")

    print("\n" + "=" * 72)
    print("评测完成")
    print("=" * 72)


if __name__ == "__main__":
    main()
