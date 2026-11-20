"""
Step 3.5 补充评测：分红「公告日」双数据商交叉验证（同花顺 vs 东方财富）。

背景：
  同花顺 stock_hk_fhpx_detail_ths 的「公告日期」被用作红利因子的首次宣布时点，
  但评测发现 8.47%（1455 条 / 279 只）记录的「公告日」晚于除息日（lag<0），
  明显不是「首次宣布日」。本脚本拉取东方财富 stock_hk_dividend_payout_em 的
  「最新公告日期」作为独立数据商，按 (code, 除净日=ex_date) 对齐交叉验证：
  - 两源公告日是否一致（一致 → 可信；不一致 → 需甄别）
  - lag<0 异常在两源中是否同样存在（同源 → 无法互证，须回退 ex_date）

用法：
    .venv/bin/python scripts/eval_announce_compare.py
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

from config import settings  # noqa: F401

EM_CSV = os.path.join(settings.CSV_DIR, "pit", "dividend_payout_em.csv")


def main():
    print("=" * 72)
    print("分红「公告日」双数据商交叉验证 · 同花顺 vs 东方财富")
    print("=" * 72)

    ths = pd.read_csv(os.path.join(settings.CSV_DIR, "pit", "dividend_history.csv"), dtype={"code": str})
    ths["ex_date"] = pd.to_datetime(ths["ex_date"])
    ths["announce_date"] = pd.to_datetime(ths["announce_date"])

    if not os.path.exists(EM_CSV):
        print("东财数据不存在，请先采集 dividend_payout_em.csv")
        return
    em = pd.read_csv(EM_CSV, dtype={"code": str})
    em["除净日"] = pd.to_datetime(em["除净日"], errors="coerce")
    em["最新公告日期"] = pd.to_datetime(em["最新公告日期"], errors="coerce")
    print(f"\n东财覆盖：{em['code'].nunique()} 只 / {len(em)} 条；"
          f"同花顺：{ths['code'].nunique()} 只 / {len(ths)} 条")

    # 按 (code, ex_date=除净日) 对齐
    m = ths.merge(
        em[["code", "除净日", "最新公告日期", "分配类型", "分红方案"]],
        left_on=["code", "ex_date"], right_on=["code", "除净日"], how="inner",
        suffixes=("_ths", "_em"),
    )
    print(f"对齐成功：{len(m)} 条（同花顺 ∩ 东财，按除净日精确匹配）")

    if m.empty:
        print("无对齐记录，无法交叉验证。")
        return

    m["lag_ths"] = (m["ex_date"] - m["announce_date"]).dt.days
    m["lag_em"] = (m["ex_date"] - m["最新公告日期"]).dt.days
    m["day_diff"] = (m["announce_date"] - m["最新公告日期"]).dt.days

    # ── 1. 两源公告日一致性 ──────────────────────────────────────
    print("\n【1】两源公告日一致性（对齐记录）")
    exact = int((m["day_diff"].abs() <= 1).sum())
    within3 = int((m["day_diff"].abs() <= 3).sum())
    within7 = int((m["day_diff"].abs() <= 7).sum())
    print(f"  完全一致(±1天)：{exact} ({exact/len(m):.1%})")
    print(f"  ±3 天内：{within3} ({within3/len(m):.1%})")
    print(f"  ±7 天内：{within7} ({within7/len(m):.1%})")
    d = m["day_diff"].abs()
    print(f"  差异天数：中位 {d.median():.0f}，P90 {d.quantile(0.9):.0f}，最大 {d.max():.0f}")

    # ── 2. lag<0 异常在两源中的分布 ───────────────────────────────
    print("\n【2】lag<0（公告日晚于除息）在两源中的分布")
    neg_ths = int((m["lag_ths"] < 0).sum())
    neg_em = int((m["lag_em"] < 0).sum())
    both_neg = int(((m["lag_ths"] < 0) & (m["lag_em"] < 0)).sum())
    print(f"  同花顺 lag<0：{neg_ths} ({neg_ths/len(m):.1%})")
    print(f"  东财   lag<0：{neg_em} ({neg_em/len(m):.1%})")
    print(f"  两源同时 lag<0：{both_neg} ({both_neg/len(m):.1%})")
    if neg_ths:
        print(f"  → 同花顺 lag<0 记录中，东财也 lag<0 的比例：{both_neg/neg_ths:.1%}")
    # 同花顺正常(lag>=0)但东财异常(lag<0) 的情况
    ths_ok_em_neg = int(((m["lag_ths"] >= 0) & (m["lag_em"] < 0)).sum())
    print(f"  同花顺正常但东财 lag<0：{ths_ok_em_neg}")

    # ── 3. 按分配类型看一致性 ─────────────────────────────────────
    print("\n【3】按分配类型的一致性（东财口径）")
    if "分配类型" in m.columns:
        for typ, g in m.groupby("分配类型"):
            if len(g) < 20:
                continue
            d_abs = g["day_diff"].abs()
            neg_ths_t = int((g["lag_ths"] < 0).sum())
            print(f"  {typ}: {len(g)} 条 | 公告日差中位 {d_abs.median():.0f} 天 | "
                  f"同花顺lag<0 {neg_ths_t} ({neg_ths_t/len(g):.1%})")

    # ── 4. 结论锚点：知名分红股的两源公告日对照 ──────────────────
    print("\n【4】锚点抽样（年度/中期 vs 季度分红）")
    anchors = {"00941": "中移动(半年度)", "00005": "汇丰(季度)", "02888": "渣打(季度)",
               "00700": "腾讯(年度)", "00066": "港铁(半年度)"}
    for code, label in anchors.items():
        sub = m[m["code"] == code].sort_values("ex_date").tail(3)
        if sub.empty:
            print(f"  {code} {label}: 无对齐记录")
            continue
        for _, r in sub.iterrows():
            print(f"  {code} {label} 除净{r['ex_date'].date()} "
                  f"同花顺{r['announce_date'].date()} 东财{r['最新公告日期'].date()} "
                  f"[ths {r['lag_ths']:.0f}d / em {r['lag_em']:.0f}d]")

    print("\n结论：")
    print("  若两源公告日高度一致 → 交叉验证通过，同花顺口径可信；")
    print("  若 lag<0 异常在两源中『同现』→ 两源同源，无法互证，异常须保守回退 ex_date；")
    print("  若仅东财 lag<0 → 东财季度分红字段错位，同花顺更可信。")


if __name__ == "__main__":
    main()
