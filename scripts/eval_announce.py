"""
Step 3.5 评测：分红因子「公告日 vs 除息日」时点口径对比。

背景（用户口径 2026-11-20）：
  红利因子的信息事件是「首次公告日」（随年报/中报），而非「除息日」——除息日
  只是价格机械调整，市场在公告日就已定价。用除息日回填因子，会在「公告~除息」
  之间系统性地晚于市场识别分红：因子的分子（TTM 分红）滞后于分母（已反映分红
  的价格），系统性低估真实股息率，遗漏公告日跳空收益。

本评测量化三件事：
  1. 公告日覆盖：多少条分红记录有 announce_date、公告→除息的滞后天数分布；
  2. 信号滞后量：每个时点 t，announce 口径的 TTM 分红比 ex 口径多出多少
     （即「市场已知、但除息口径还没算进来」的分红）；
  3. 排名影响：两种口径下 return_rate 因子的差异与高股息 Top-N 重叠率。

用法：
    .venv/bin/python scripts/eval_announce.py
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
from backtest.point_in_time import (
    load_dividend_history, ttm_dividend_matrix,
)


def _fmt_pct(x, digits=1):
    return "—" if pd.isna(x) else f"{x*100:.{digits}f}%"


def main():
    print("=" * 72)
    print("Step 3.5 评测报告 · 分红因子时点口径：公告日 vs 除息日")
    print("=" * 72)

    hist = load_dividend_history()
    if hist.empty:
        print("分红历史为空，请先采集 point-in-time 数据。")
        return

    # ── 1. 公告日覆盖 + 滞后分布 ────────────────────────────────
    print("\n【1】公告日覆盖与「公告→除息」滞后")
    n_total = len(hist)
    n_ann = int(hist["announce_date"].notna().sum())
    print(f"  分红记录：{n_total} 条，有公告日 {n_ann} 条 = {n_ann/n_total:.1%}")
    ann_hist = hist[hist["announce_date"].notna()].copy()
    if len(ann_hist):
        lag = (ann_hist["ex_date"] - ann_hist["announce_date"]).dt.days
        print(f"  公告→除息 滞后天数：中位数 {lag.median():.0f}，均值 {lag.mean():.1f}，"
              f"P90 {lag.quantile(0.90):.0f}，最大 {lag.max():.0f}")
        neg = int((lag < 0).sum())
        print(f"  异常（公告日晚于除息日）：{neg} 条 = {neg/len(lag):.2%}")
        # 按年度看滞后是否稳定
        ann_hist["yr"] = ann_hist["announce_date"].dt.year
        med = ann_hist.groupby("yr")[["ex_date", "announce_date"]].apply(
            lambda g: (g["ex_date"] - g["announce_date"]).dt.days.median()
        )
        print(f"  滞后中位数按年：{dict(zip(med.index.astype(str), med.round(0).astype(int)))}")

    # ── 2. 信号滞后量（时点 TTM 差）─────────────────────────────
    print("\n【2】信号滞后量：announce 口径 TTM − ex 口径 TTM")
    price = pd.read_csv(os.path.join(settings.CSV_DIR, "hk_monthly_price.csv"), dtype={"code": str})
    price["date"] = pd.to_datetime(price["date"])
    months = sorted(price["date"].dt.to_period("M").unique())
    months = [m for m in months if m >= pd.Period("2021-12", "M")]  # 回测起点对齐
    month_ends = [m.to_timestamp(how="end") for m in months]

    ttm_ex = ttm_dividend_matrix(hist, month_ends, months=12, event_col="ex_date")
    ttm_ann = ttm_dividend_matrix(hist, month_ends, months=12, event_col="announce_date")
    # 对齐行列
    common_idx = ttm_ex.index.intersection(ttm_ann.index)
    common_col = ttm_ex.columns.intersection(ttm_ann.columns)
    ttm_ex = ttm_ex.loc[common_idx, common_col]
    ttm_ann = ttm_ann.loc[common_idx, common_col]

    diff = (ttm_ann - ttm_ex)
    diff_vals = diff.values
    n_cells = diff_vals.size
    n_pos = int((diff_vals > 1e-9).sum())
    n_zero = int((np.abs(diff_vals) <= 1e-9).sum())
    n_neg = int((diff_vals < -1e-9).sum())
    print(f"  时点×股票 单元格：{n_cells:,}（{len(common_idx)} 只 × {len(common_col)} 期）")
    print(f"  announce > ex：{n_pos:,} ({n_pos/n_cells:.1%}) | "
          f"相等：{n_zero:,} ({n_zero/n_cells:.1%}) | "
          f"announce < ex：{n_neg:,} ({n_neg/n_cells:.1%})")
    pos = diff_vals[diff_vals > 1e-9]
    if len(pos):
        print(f"  正值分布（每股多识别的分红）：中位 {np.median(pos):.3f}，"
              f"P90 {np.percentile(pos, 90):.3f}，最大 {pos.max():.2f}")
    # 每期受影响股票数
    per_period = (diff > 1e-9).sum(axis=0)
    print(f"  每期受影响股票数：中位 {per_period.median():.0f}，"
          f"最大 {per_period.max():.0f}（占 {per_period.max()/len(common_idx):.1%}）")

    # 相对低估幅度：diff / ttm_ex（仅正值、denom>0）
    denom_vals = ttm_ex.values
    rel_vals = diff_vals / np.where(denom_vals <= 0, np.nan, denom_vals)
    rel_pos = rel_vals[(rel_vals > 0) & np.isfinite(rel_vals)]
    if len(rel_pos):
        print(f"  相对低估幅度（diff/ttm_ex，仅正值）：中位 {np.median(rel_pos):.1%}，"
              f"P90 {np.percentile(rel_pos, 90):.1%}，最大 {rel_pos.max():.1%}")

    # ── 3. 排名影响：return_rate 相关 + Top-N 重叠 ──────────────
    print("\n【3】排名影响：两种口径 return_rate 因子对比")
    fin = pd.read_csv(os.path.join(settings.CSV_DIR, "hk_financial.csv"), dtype={"code": str})
    fin["code"] = fin["code"].astype(str).str.zfill(5)
    buyback = fin.set_index("code")["buyback_amount"] if "buyback_amount" in fin.columns else None
    price_wide = price.sort_values("date").drop_duplicates("code", keep="last").set_index("code")

    def _rate(ttm):
        m = ttm.T  # period × code
        p = price_wide["close"].reindex(m.columns)
        b = buyback.reindex(m.columns).fillna(0) if buyback is not None else 0
        return (m + b) / p * 100

    # 取最后一期（最新时点）做截面对比
    last = common_col[-1]
    r_ex = (ttm_ex[last] / price_wide["close"].reindex(ttm_ex.index) * 100).dropna()
    r_ann = (ttm_ann[last] / price_wide["close"].reindex(ttm_ann.index) * 100).dropna()
    both = r_ex.index.intersection(r_ann.index)
    r_ex, r_ann = r_ex[both], r_ann[both]
    corr = r_ex.corr(r_ann)
    print(f"  最新时点 {last}：return_rate 两种口径 Spearman 相关 = {r_ex.rank().corr(r_ann.rank()):.3f} "
          f"(Pearson {corr:.3f})")
    for topn in (10, 20, 50):
        t_ex = set(r_ex.nlargest(topn).index)
        t_ann = set(r_ann.nlargest(topn).index)
        ov = len(t_ex & t_ann) / topn
        print(f"  Top-{topn} 重叠率 = {ov:.0%}")

    # 全期（各期 Top-20 重叠率中位数）
    top_overlaps = []
    for c in common_col:
        a = ttm_ex[c].replace(0, np.nan).dropna()
        b = ttm_ann[c].replace(0, np.nan).dropna()
        common = a.index.intersection(b.index)
        if len(common) < 20:
            continue
        t_a = set(a[common].nlargest(20).index)
        t_b = set(b[common].nlargest(20).index)
        top_overlaps.append(len(t_a & t_b) / 20)
    if top_overlaps:
        print(f"  全期各期 Top-20 重叠率：中位 {np.median(top_overlaps):.0%}，"
              f"最低 {np.min(top_overlaps):.0%}，最高 {np.max(top_overlaps):.0%}")

    print("\n结论：")
    if n_ann / n_total >= 0.9 and len(pos) and np.median(pos) > 0:
        print("  公告日口径可行——多数分红已补齐公告日，且除息口径确实系统性低估了")
        print("  「市场已公告但未除息」的分红；建议后续回测统一用 --announce 口径。")
    else:
        print("  公告日覆盖不足或滞后量不显著，需先核查 announce_date 采集质量。")


if __name__ == "__main__":
    main()
