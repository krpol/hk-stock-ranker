"""
按月调仓 + 净值计算

核心逻辑（防未来函数）：
- 第 m 月持仓由第 m-1 月末的 ranking 信号决定
- 第 m 月收益 = Σ w_i × r_i，r_i = price[m] / price[m-1] - 1
- 缺价（停牌）→ r_i = 0（保留权重）
- 调仓成本 = transaction_cost_bps × turnover × 2（双边？我们 bps=15 已是单边，turnover 已含买卖）
  实际：cost_t = bps_per_side × turnover_t（turnover 定义为单边换手 = 0.5 × Σ|Δw|）

Returns:
  trace DataFrame[period, holdings_n, gross_return, turnover, cost, net_return, nav]
"""
import logging
import os
from typing import Optional

import numpy as np
import pandas as pd

from backtest.portfolio import build_top_n, build_quintiles, load_ranking_period
from config.settings import BACKTEST_TRANSACTION_COST_BPS
from storage import store as _store
from storage.store import load_monthly_price

logger = logging.getLogger(__name__)


# =====================================================================
# 工具：月底价格表
# =====================================================================

def build_month_end_price(price_df: Optional[pd.DataFrame] = None) -> pd.DataFrame:
    """
    从月度价格生成每月最后一个交易日的收盘价宽表：
        index = period (YYYY-MM), columns = code, values = close_qfq

    B24: 回测/价格变化率统一使用前复权价 close_qfq（跨除权点连续）。
    若文件无 close_qfq 列（旧 schema），fallback 到 close。
    若同一 period 多条记录，取该 period 内最大 date 对应的价。
    """
    if price_df is None:
        price_df = load_monthly_price()
    if price_df.empty:
        return pd.DataFrame()

    df = price_df.copy()
    df["code"] = df["code"].astype(str).str.zfill(5)
    df["date"] = pd.to_datetime(df["date"])
    df["period"] = df["date"].dt.strftime("%Y-%m")
    df = df.sort_values(["code", "date"])
    last = df.groupby(["code", "period"]).tail(1)
    price_col = "close_qfq" if "close_qfq" in last.columns else "close"
    wide = last.pivot(index="period", columns="code", values=price_col)
    return wide.sort_index()


# =====================================================================
# 单步月度收益
# =====================================================================

def calc_monthly_return(holdings: pd.DataFrame, t_prev: str, t_cur: str,
                        price_wide: pd.DataFrame) -> tuple[float, pd.Series]:
    """
    计算 holdings 在 t_prev → t_cur 期间的组合收益。
    缺任一端价格 → 该股收益记 0（停牌按 0 收益，保留权重）。

    Returns:
        gross_return, per_stock_return Series（index=code）
    """
    codes = holdings["code"].tolist()
    weights = holdings.set_index("code")["weight"]

    if t_prev not in price_wide.index or t_cur not in price_wide.index:
        # 价格表缺整月（罕见）→ 返回 0
        return 0.0, pd.Series(0.0, index=codes)

    p_prev = price_wide.loc[t_prev].reindex(codes)
    p_cur = price_wide.loc[t_cur].reindex(codes)

    # 收益：缺价位 → 0
    valid = p_prev.notna() & p_cur.notna() & (p_prev > 0)
    ret = pd.Series(0.0, index=codes)
    ret[valid] = (p_cur[valid] / p_prev[valid] - 1).values

    gross = float((weights * ret).sum())
    return gross, ret


# =====================================================================
# 全周期回测
# =====================================================================

def _turnover(prev_holdings: Optional[pd.DataFrame],
              cur_holdings: pd.DataFrame) -> float:
    """单边换手 = 0.5 × Σ|Δw|"""
    if prev_holdings is None or prev_holdings.empty:
        return 1.0  # 首期建仓全换手
    a = prev_holdings.set_index("code")["weight"]
    b = cur_holdings.set_index("code")["weight"]
    union_codes = a.index.union(b.index)
    a = a.reindex(union_codes, fill_value=0.0)
    b = b.reindex(union_codes, fill_value=0.0)
    return float(0.5 * (a - b).abs().sum())


def _next_period(period: str) -> str:
    """'2024-06' → '2024-07'"""
    ts = pd.Timestamp(period + "-01") + pd.DateOffset(months=1)
    return ts.strftime("%Y-%m")


def _list_periods_in_range(start: str, end: str, tag: str = None) -> list[str]:
    avail = _store.list_ranking_periods(tag=tag)
    return sorted([p for p in avail if start <= p <= end])


def run_backtest(start: str, end: str,
                  top_n: int = 20,
                  weight_scheme: str = "equal",
                  transaction_cost_bps: int = BACKTEST_TRANSACTION_COST_BPS,
                  price_wide: Optional[pd.DataFrame] = None,
                  strategy_name: str = "top_n",
                  ranking_tag: Optional[str] = None) -> dict:
    """
    主策略 TOP N 回测。

    时序约定：
      第 m 月持仓 = 第 m-1 月末 ranking 信号的 TOP N（防未来函数）
      第 m 月收益 = 持仓在 [m-1 月末, m 月末] 的加权收益

    Args:
        start, end: ranking 期范围（YYYY-MM）。例如 start='2024-06' end='2026-05' 共 24 个月信号
        top_n: 持仓数量
        weight_scheme: equal / score / rank
        transaction_cost_bps: 单边交易成本（基点）
        price_wide: 月底价宽表，None 时自动加载
        strategy_name: 策略名（用于报告）
        ranking_tag: 排名标签（如 "ann_sus"），None 读旧口径 hk_ranking_{period}.csv

    Returns:
        dict: {strategy, params, trace_df, nav, monthly_returns}
    """
    if price_wide is None:
        price_wide = build_month_end_price()
    if price_wide.empty:
        raise RuntimeError("无月度价格数据，请先运行 --backfill-prices")

    # 区间内所有信号期（升序），每期作为下个月持仓的信号
    periods = _list_periods_in_range(start, end, tag=ranking_tag)
    if len(periods) == 0:
        raise RuntimeError(
            f"区间 {start}~{end} 无 ranking 数据。请先运行 scripts/backfill_rankings.py 回填历史。"
        )

    cost_rate = transaction_cost_bps / 10000.0
    nav = 1.0
    prev_holdings = None
    rows = []

    # 每个信号期 m → 持仓在第 (m+1) 月运行；要求 hold_period <= end
    for signal_period in periods:
        hold_period = _next_period(signal_period)
        if hold_period > end:
            continue

        cur_holdings = build_top_n(signal_period, top_n=top_n, weight_scheme=weight_scheme, tag=ranking_tag)
        if cur_holdings.empty:
            logger.warning(f"信号期 {signal_period} 无持仓，跳过")
            continue

        turnover = _turnover(prev_holdings, cur_holdings)
        cost = cost_rate * turnover

        gross, _ = calc_monthly_return(cur_holdings, signal_period, hold_period, price_wide)
        net = gross - cost
        nav *= (1 + net)

        rows.append({
            "signal_period": signal_period,
            "hold_period": hold_period,
            "holdings_n": len(cur_holdings),
            "gross_return": gross,
            "turnover": turnover,
            "cost": cost,
            "net_return": net,
            "nav": nav,
        })
        prev_holdings = cur_holdings

    trace = pd.DataFrame(rows)
    monthly_returns = trace.set_index("hold_period")["net_return"] if not trace.empty else pd.Series(dtype=float)
    nav_series = trace.set_index("hold_period")["nav"] if not trace.empty else pd.Series(dtype=float)

    return {
        "strategy": strategy_name,
        "params": {
            "start": start, "end": end, "top_n": top_n,
            "weight_scheme": weight_scheme,
            "transaction_cost_bps": transaction_cost_bps,
        },
        "trace": trace,
        "nav": nav_series,
        "monthly_returns": monthly_returns,
    }


def run_quintile_backtest(start: str, end: str,
                           n_quantiles: int = 5,
                           weight_scheme: str = "equal",
                           transaction_cost_bps: int = BACKTEST_TRANSACTION_COST_BPS,
                           price_wide: Optional[pd.DataFrame] = None,
                           ranking_tag: Optional[str] = None) -> dict:
    """
    五分位回测：对每个分位独立跑全周期，返回 dict[label -> result]

    Args:
        ranking_tag: 排名标签（如 "ann_sus"），None 读旧口径。
    """
    if price_wide is None:
        price_wide = build_month_end_price()
    if price_wide.empty:
        raise RuntimeError("无月度价格数据")

    periods = _list_periods_in_range(start, end, tag=ranking_tag)
    if len(periods) == 0:
        raise RuntimeError(f"区间 {start}~{end} 无 ranking 数据")

    cost_rate = transaction_cost_bps / 10000.0
    quintile_results: dict[str, dict] = {}

    # 用 Q1..QN 作为标签，每个分位维护自己的 nav/prev_holdings
    state = {}  # label -> {nav, prev, rows}
    for signal_period in periods:
        hold_period = _next_period(signal_period)
        if hold_period > end:
            continue

        groups = build_quintiles(signal_period, n_quantiles=n_quantiles, weight_scheme=weight_scheme, tag=ranking_tag)
        for label, holdings in groups.items():
            st = state.setdefault(label, {"nav": 1.0, "prev": None, "rows": []})
            turnover = _turnover(st["prev"], holdings)
            cost = cost_rate * turnover
            gross, _ = calc_monthly_return(holdings, signal_period, hold_period, price_wide)
            net = gross - cost
            st["nav"] *= (1 + net)
            st["rows"].append({
                "signal_period": signal_period,
                "hold_period": hold_period,
                "holdings_n": len(holdings),
                "gross_return": gross,
                "turnover": turnover,
                "cost": cost,
                "net_return": net,
                "nav": st["nav"],
            })
            st["prev"] = holdings

    for label, st in state.items():
        trace = pd.DataFrame(st["rows"])
        quintile_results[label] = {
            "trace": trace,
            "nav": trace.set_index("hold_period")["nav"] if not trace.empty else pd.Series(dtype=float),
            "monthly_returns": trace.set_index("hold_period")["net_return"] if not trace.empty else pd.Series(dtype=float),
        }

    return {
        "n_quantiles": n_quantiles,
        "params": {
            "start": start, "end": end, "weight_scheme": weight_scheme,
            "transaction_cost_bps": transaction_cost_bps,
        },
        "quintiles": quintile_results,
    }
