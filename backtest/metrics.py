"""
回测绩效指标
"""
from typing import Optional

import numpy as np
import pandas as pd

from config.settings import BACKTEST_RISK_FREE_RATE


def annualized_return(monthly_returns: pd.Series, periods_per_year: int = 12) -> float:
    """
    年化复利收益：(prod(1+r))^(12/n) - 1
    """
    if len(monthly_returns) == 0:
        return float("nan")
    cum = (1 + monthly_returns).prod()
    n = len(monthly_returns)
    return float(cum ** (periods_per_year / n) - 1)


def annualized_volatility(monthly_returns: pd.Series, periods_per_year: int = 12) -> float:
    if len(monthly_returns) < 2:
        return float("nan")
    return float(monthly_returns.std(ddof=1) * np.sqrt(periods_per_year))


def sharpe_ratio(monthly_returns: pd.Series,
                 risk_free_rate: float = BACKTEST_RISK_FREE_RATE,
                 periods_per_year: int = 12) -> float:
    """夏普比率 = (年化收益 - rf) / 年化波动"""
    vol = annualized_volatility(monthly_returns, periods_per_year)
    if not np.isfinite(vol) or vol < 1e-12:
        return float("nan")
    ret = annualized_return(monthly_returns, periods_per_year)
    return (ret - risk_free_rate) / vol


def max_drawdown(nav: pd.Series) -> tuple[float, Optional[pd.Timestamp], Optional[pd.Timestamp]]:
    """
    最大回撤（基于净值序列）。
    Returns:
        (最大回撤值（负数 0~-1）, 峰值日期, 谷值日期)
    """
    if len(nav) == 0:
        return float("nan"), None, None
    running_max = nav.cummax()
    drawdown = nav / running_max - 1
    trough_idx = drawdown.idxmin()
    if pd.isna(trough_idx):
        return 0.0, None, None
    peak_idx = running_max.loc[:trough_idx].idxmax()
    return float(drawdown.min()), peak_idx, trough_idx


def calmar_ratio(monthly_returns: pd.Series, nav: pd.Series,
                 periods_per_year: int = 12) -> float:
    mdd, _, _ = max_drawdown(nav)
    if mdd == 0 or not np.isfinite(mdd):
        return float("nan")
    ar = annualized_return(monthly_returns, periods_per_year)
    return ar / abs(mdd)


def win_rate(monthly_returns: pd.Series) -> float:
    if len(monthly_returns) == 0:
        return float("nan")
    return float((monthly_returns > 0).mean())


def information_ratio(strategy_returns: pd.Series,
                       benchmark_returns: pd.Series,
                       periods_per_year: int = 12) -> float:
    """信息比率 = 年化超额收益 / 年化跟踪误差"""
    aligned = pd.concat([strategy_returns, benchmark_returns], axis=1, join="inner").dropna()
    if len(aligned) < 2:
        return float("nan")
    excess = aligned.iloc[:, 0] - aligned.iloc[:, 1]
    te = excess.std(ddof=1) * np.sqrt(periods_per_year)
    if te == 0 or not np.isfinite(te):
        return float("nan")
    ann_excess = excess.mean() * periods_per_year
    return float(ann_excess / te)


def quintile_monotonicity_spearman(quintile_annual_returns: dict) -> float:
    """
    五分位单调性的 Spearman 秩相关系数：
    输入 {Q1: 0.18, Q2: 0.10, Q3: 0.05, Q4: 0.02, Q5: -0.03}
    Q1 排第 1（按收益降序），单调下降时 ρ ≈ 1.0；完全反向 ρ ≈ -1.0。
    """
    if not quintile_annual_returns:
        return float("nan")
    items = sorted(quintile_annual_returns.items(), key=lambda kv: kv[0])  # Q1, Q2, ...
    expected_rank = list(range(1, len(items) + 1))  # 我们期望 Q1 收益最高 → rank 1
    actual_returns = [v for _, v in items]
    # 按收益降序得到实际 rank（最高收益 rank=1）
    sorted_by_ret = sorted(range(len(actual_returns)), key=lambda i: -actual_returns[i])
    actual_rank = [0] * len(actual_returns)
    for rank, i in enumerate(sorted_by_ret, start=1):
        actual_rank[i] = rank
    # Spearman = Pearson on ranks
    er = np.array(expected_rank, dtype=float)
    ar = np.array(actual_rank, dtype=float)
    if er.std() == 0 or ar.std() == 0:
        return float("nan")
    return float(np.corrcoef(er, ar)[0, 1])


def summarize(monthly_returns: pd.Series, nav: pd.Series,
              benchmark_returns: Optional[pd.Series] = None) -> dict:
    """汇总常用指标"""
    out = {
        "n_months": len(monthly_returns),
        "cumulative_return": float(nav.iloc[-1] - 1) if len(nav) else float("nan"),
        "annualized_return": annualized_return(monthly_returns),
        "annualized_volatility": annualized_volatility(monthly_returns),
        "sharpe_ratio": sharpe_ratio(monthly_returns),
        "win_rate": win_rate(monthly_returns),
    }
    mdd, peak, trough = max_drawdown(nav)
    out["max_drawdown"] = mdd
    out["max_drawdown_peak"] = str(peak) if peak is not None else None
    out["max_drawdown_trough"] = str(trough) if trough is not None else None
    out["calmar_ratio"] = calmar_ratio(monthly_returns, nav)
    if benchmark_returns is not None:
        out["information_ratio"] = information_ratio(monthly_returns, benchmark_returns)
    return out
