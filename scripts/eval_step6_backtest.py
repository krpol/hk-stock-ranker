"""
Step 6 干净回测：五口径 TOP-20 回测对比 + ann_sus 五分位因子单调性检验

口径（所有口径均用「新流动性门槛：日成交额≥100万港元」）：
  1. 旧口径        —— 当前快照分红（前视偏差，上帝视角）
  2. pit           —— 时点 TTM 分红（除息日口径）
  3. ann           —— 时点 TTM 分红（首次公告日口径）
  4. sus           —— 时点 TTM 分红 − 特别股息（除息日口径）
  5. ann_sus       —— 时点 TTM 分红 − 特别股息（公告日口径）【干净】

回测约定：第 m 月持仓 = 第 m-1 月末排名 TOP-20，等权，单边 15bps 成本，
月度调仓，前复权价，基准 = 恒生指数。
"""
import warnings
warnings.filterwarnings("ignore")
import logging
logging.basicConfig(level=logging.WARNING)
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from backtest.runner import run_topn_with_report, run_quintile_with_report

START, END = "2021-11", "2026-11"
TAGS = [
    ("旧口径(前视偏差)", None),
    ("pit(时点·除息)", "pit"),
    ("ann(时点·公告日)", "ann"),
    ("sus(除息·剔特别)", "sus"),
    ("ann_sus(干净)", "ann_sus"),
]

rows = []
for label, tag in TAGS:
    r = run_topn_with_report(START, END, top_n=20, ranking_tag=tag,
                             report_name=f"step6_top20_{tag or 'legacy'}")
    s = r["summary"]
    rows.append({
        "口径": label,
        "期数": s["n_months"],
        "累计收益": s["cumulative_return"],
        "年化收益": s["annualized_return"],
        "年化波动": s["annualized_volatility"],
        "夏普": s["sharpe_ratio"],
        "最大回撤": s["max_drawdown"],
        "胜率": s["win_rate"],
        "信息比率": s.get("information_ratio"),
    })

df = pd.DataFrame(rows)
pd.set_option("display.width", 250)
pd.set_option("display.max_columns", None)
pd.set_option("display.float_format", lambda x: f"{x:.3f}")
print("=" * 90)
print(f"TOP-20 五口径回测对比（{START}~{END}，月度调仓，单边 15bps，含恒指基准）")
print("=" * 90)
print(df.to_string(index=False))

# 关键归因
legacy = rows[0]
clean = rows[-1]
print()
print("前视偏差虚增：年化 {:.1%} → {:.1%}（虚增 {:.1f}pp），夏普 {:.2f} → {:.2f}".format(
    legacy["年化收益"], clean["年化收益"],
    (legacy["年化收益"] - clean["年化收益"]) * 100,
    legacy["夏普"], clean["夏普"]))

# ann_sus 五分位
r_q = run_quintile_with_report(START, END, ranking_tag="ann_sus",
                               report_name="step6_ann_sus_quintile")
print()
print("=" * 90)
print("ann_sus 五分位回测（因子单调性检验）")
print("=" * 90)
for lbl in sorted(r_q["quintile_summaries"].keys()):
    q = r_q["quintile_summaries"][lbl]
    print(f"  {lbl}: 年化 {q['annualized_return']:.1%} | 夏普 {q['sharpe_ratio']:.2f} | "
          f"回撤 {q['max_drawdown']:.1%}")
print(f"  单调性 Spearman: {r_q['spearman_monotonicity']:.3f}")
if r_q["long_short"]:
    ls = r_q["long_short"]["summary"]
    print(f"  多空(Q1-Q5): 年化 {ls['annualized_return']:.1%} | 夏普 {ls['sharpe_ratio']:.2f} | "
          f"回撤 {ls['max_drawdown']:.1%}")
