"""
回测 runner：统一封装 TOP-N / 五分位 / 含基准对比，输出 JSON + CSV 报告
"""
import json
import logging
import os
from typing import Optional

import numpy as np
import pandas as pd

from config.settings import CSV_DIR
from backtest.rebalance import run_backtest, run_quintile_backtest
from backtest.metrics import (
    summarize, quintile_monotonicity_spearman, annualized_return,
    max_drawdown,
)
from backtest.benchmark import get_hsi_monthly_returns

logger = logging.getLogger(__name__)

REPORT_DIR = os.path.join(CSV_DIR, "..", "reports")


def _ensure_report_dir():
    os.makedirs(REPORT_DIR, exist_ok=True)


def _df_safe_dict(df: pd.DataFrame) -> list:
    return json.loads(df.to_json(orient="records", date_format="iso"))


def _series_safe_dict(s: pd.Series) -> dict:
    return {str(k): (None if pd.isna(v) else float(v)) for k, v in s.items()}


def _clean_for_json(obj):
    """递归清理 NaN/Inf → None，让 json.dump 不报 ValueError"""
    if isinstance(obj, dict):
        return {k: _clean_for_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_clean_for_json(v) for v in obj]
    if isinstance(obj, float):
        if np.isnan(obj) or np.isinf(obj):
            return None
        return obj
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        v = float(obj)
        return None if (np.isnan(v) or np.isinf(v)) else v
    return obj


def run_topn_with_report(
    start: str,
    end: str,
    top_n: int = 20,
    weight_scheme: str = "equal",
    transaction_cost_bps: float = 15,
    with_benchmark: bool = True,
    report_name: Optional[str] = None,
    ranking_tag: Optional[str] = None,
) -> dict:
    """
    跑 TOP-N 月度调仓回测，输出指标 + 净值曲线 + trace。
    报告保存到 data/reports/{name}.json + .trace.csv

    Args:
        ranking_tag: 排名标签（如 "ann_sus"），None 读旧口径。
    """
    _ensure_report_dir()
    name = report_name or f"backtest_top{top_n}_{start}_{end}_{weight_scheme}"

    result = run_backtest(start, end, top_n=top_n,
                           weight_scheme=weight_scheme,
                           transaction_cost_bps=transaction_cost_bps,
                           ranking_tag=ranking_tag)
    rets = result["monthly_returns"]
    nav = result["nav"]
    trace = result["trace"]

    bench = None
    if with_benchmark and len(rets) > 0:
        try:
            bench = get_hsi_monthly_returns(
                start_period=str(rets.index.min()),
                end_period=str(rets.index.max()),
            )
            bench = bench.reindex(rets.index)
        except Exception as e:
            logger.warning(f"基准采集失败：{e}")
            bench = None

    summary = summarize(rets, nav, benchmark_returns=bench.dropna() if bench is not None and bench.notna().any() else None)

    bench_nav = None
    if bench is not None and bench.notna().any():
        bench_nav = (1 + bench.fillna(0)).cumprod()

    report = {
        "type": "topn",
        "params": {
            "start": start, "end": end, "top_n": top_n,
            "weight_scheme": weight_scheme,
            "transaction_cost_bps": transaction_cost_bps,
        },
        "summary": summary,
        "nav": _series_safe_dict(nav),
        "monthly_returns": _series_safe_dict(rets),
        "benchmark_nav": _series_safe_dict(bench_nav) if bench_nav is not None else None,
        "benchmark_monthly_returns": _series_safe_dict(bench) if bench is not None else None,
        "n_holdings_per_period": len(trace),
    }

    json_path = os.path.join(REPORT_DIR, f"{name}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(_clean_for_json(report), f, ensure_ascii=False, indent=2)

    trace_path = os.path.join(REPORT_DIR, f"{name}.trace.csv")
    trace.to_csv(trace_path, index=False, encoding="utf-8-sig")

    logger.info(f"回测报告已保存: {json_path}")
    logger.info(f"调仓 trace 已保存: {trace_path}")
    return report


def run_quintile_with_report(
    start: str,
    end: str,
    n_quantiles: int = 5,
    weight_scheme: str = "equal",
    transaction_cost_bps: float = 15,
    report_name: Optional[str] = None,
    ranking_tag: Optional[str] = None,
) -> dict:
    """
    跑五分位回测，输出每个分位的指标和单调性 Spearman。

    Args:
        ranking_tag: 排名标签（如 "ann_sus"），None 读旧口径。
    """
    _ensure_report_dir()
    name = report_name or f"quintile_q{n_quantiles}_{start}_{end}_{weight_scheme}"

    result = run_quintile_backtest(start, end,
                                     n_quantiles=n_quantiles,
                                     weight_scheme=weight_scheme,
                                     transaction_cost_bps=transaction_cost_bps,
                                     ranking_tag=ranking_tag)
    quintiles = result["quintiles"]

    quintile_summaries = {}
    annual_returns_by_q = {}
    for label, info in quintiles.items():
        rets = info["monthly_returns"]
        nav = info["nav"]
        s = summarize(rets, nav, benchmark_returns=None)
        quintile_summaries[label] = s
        annual_returns_by_q[label] = s.get("annualized_return", float("nan"))

    spearman = quintile_monotonicity_spearman(annual_returns_by_q)

    # 多空 Q1-QN
    q_labels = sorted(quintiles.keys())
    long_short = None
    if len(q_labels) >= 2:
        first, last = q_labels[0], q_labels[-1]
        ls_rets = quintiles[first]["monthly_returns"] - quintiles[last]["monthly_returns"]
        ls_nav = (1 + ls_rets).cumprod()
        long_short = {
            "label": f"{first}_minus_{last}",
            "monthly_returns": _series_safe_dict(ls_rets),
            "nav": _series_safe_dict(ls_nav),
            "summary": summarize(ls_rets, ls_nav, benchmark_returns=None),
        }

    report = {
        "type": "quintile",
        "params": {
            "start": start, "end": end, "n_quantiles": n_quantiles,
            "weight_scheme": weight_scheme,
            "transaction_cost_bps": transaction_cost_bps,
        },
        "spearman_monotonicity": spearman,
        "quintile_summaries": quintile_summaries,
        "quintile_navs": {
            lbl: _series_safe_dict(info["nav"]) for lbl, info in quintiles.items()
        },
        "quintile_monthly_returns": {
            lbl: _series_safe_dict(info["monthly_returns"]) for lbl, info in quintiles.items()
        },
        "long_short": long_short,
    }

    json_path = os.path.join(REPORT_DIR, f"{name}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(_clean_for_json(report), f, ensure_ascii=False, indent=2)

    logger.info(f"五分位报告已保存: {json_path}")
    logger.info(f"  Spearman 单调性: {spearman:.4f}")
    return report
