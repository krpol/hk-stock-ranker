"""
P3 测试：portfolio + metrics + rebalance
"""
from datetime import datetime
import math

import numpy as np
import pandas as pd
import pytest

from backtest.portfolio import build_top_n, build_quintiles, _assign_weights
from backtest import metrics
from backtest.rebalance import (
    build_month_end_price,
    calc_monthly_return,
    run_backtest,
    run_quintile_backtest,
    _turnover,
    _next_period,
)
from storage.store import save_ranking, save_monthly_price


# =====================================================================
# 工具
# =====================================================================

def _make_ranking(period, codes_scores, tmp_data_dir):
    rows = []
    for rank, (code, score) in enumerate(codes_scores, start=1):
        rows.append({
            "rank": rank, "code": code, "name": f"S{code}",
            "current_price": 10.0,
            "composite_score": score,
            "return_zscore": score, "growth_factor": 0.0,
            "return_rate": 0.0, "fy_div_per_share": 0.0,
        })
    df = pd.DataFrame(rows)
    save_ranking(df, period)


def _make_prices(price_map, tmp_data_dir):
    """
    price_map: {"2024-06": {"00001": 10, ...}, ...}
    """
    rows = []
    for period, mp in price_map.items():
        date_str = period + "-28"
        for code, close in mp.items():
            rows.append({
                "code": code, "date": date_str,
                "open": close, "high": close, "low": close, "close": close, "volume": 1000,
            })
    df = pd.DataFrame(rows)
    save_monthly_price(df)


# =====================================================================
# B: portfolio
# =====================================================================

class TestPortfolio:

    def test_B1_top_n_equal_weight(self, tmp_data_dir):
        _make_ranking("2024-06", [
            ("00001", 5.0), ("00002", 4.0), ("00003", 3.0),
            ("00004", 2.0), ("00005", 1.0),
        ], tmp_data_dir)

        h = build_top_n("2024-06", top_n=3, weight_scheme="equal")
        assert len(h) == 3
        assert h["code"].tolist() == ["00001", "00002", "00003"]
        assert all(abs(w - 1/3) < 1e-9 for w in h["weight"])
        assert abs(h["weight"].sum() - 1.0) < 1e-9

    def test_B2_score_weight(self, tmp_data_dir):
        _make_ranking("2024-06", [
            ("00001", 5.0), ("00002", 3.0), ("00003", 1.0),
        ], tmp_data_dir)
        h = build_top_n("2024-06", top_n=3, weight_scheme="score")
        # shifted: 4, 2, 0; 总和 6; 权重 4/6, 2/6, 0
        assert abs(h.set_index("code").loc["00001", "weight"] - 4/6) < 1e-9
        assert abs(h.set_index("code").loc["00002", "weight"] - 2/6) < 1e-9
        assert abs(h.set_index("code").loc["00003", "weight"]) < 1e-9
        assert abs(h["weight"].sum() - 1.0) < 1e-9

    def test_B3_rank_weight(self, tmp_data_dir):
        _make_ranking("2024-06", [
            ("00001", 5.0), ("00002", 4.0), ("00003", 3.0),
        ], tmp_data_dir)
        h = build_top_n("2024-06", top_n=3, weight_scheme="rank")
        # 1 + 1/2 + 1/3 = 11/6；权重 (1, 1/2, 1/3) / (11/6)
        denom = 1 + 0.5 + 1/3
        assert abs(h.set_index("code").loc["00001", "weight"] - 1/denom) < 1e-9
        assert abs(h.set_index("code").loc["00003", "weight"] - (1/3)/denom) < 1e-9
        assert abs(h["weight"].sum() - 1.0) < 1e-9

    def test_B4_quintiles(self, tmp_data_dir):
        codes_scores = [(f"{i:05d}", float(100 - i)) for i in range(1, 26)]  # 25 只
        _make_ranking("2024-06", codes_scores, tmp_data_dir)
        groups = build_quintiles("2024-06", n_quantiles=5, weight_scheme="equal")
        assert list(groups.keys()) == ["Q1", "Q2", "Q3", "Q4", "Q5"]
        assert all(len(g) == 5 for g in groups.values())
        # Q1 应该是分数最高的 5 只
        assert groups["Q1"]["code"].iloc[0] == "00001"
        assert groups["Q5"]["code"].iloc[-1] == "00025"

    def test_B5_top_n_min_score_filter(self, tmp_data_dir):
        _make_ranking("2024-06", [
            ("00001", 5.0), ("00002", 3.0), ("00003", -1.0),
        ], tmp_data_dir)
        h = build_top_n("2024-06", top_n=10, weight_scheme="equal", min_composite_score=0.0)
        assert len(h) == 2
        assert "00003" not in h["code"].tolist()


# =====================================================================
# M: metrics
# =====================================================================

class TestMetrics:

    def test_M1_annualized_return(self):
        # 全 0 收益 → 年化 0
        s = pd.Series([0.0] * 12)
        assert abs(metrics.annualized_return(s)) < 1e-12
        # 月收益 1% 共 12 个月 → 年化 = 1.01**12 - 1
        s = pd.Series([0.01] * 12)
        assert abs(metrics.annualized_return(s) - (1.01 ** 12 - 1)) < 1e-9

    def test_M2_max_drawdown(self):
        nav = pd.Series([1.0, 1.1, 1.2, 0.9, 1.0, 1.3])
        mdd, peak, trough = metrics.max_drawdown(nav)
        assert abs(mdd - (0.9 / 1.2 - 1)) < 1e-9  # -0.25

    def test_M3_sharpe_zero_vol(self):
        s = pd.Series([0.01] * 12)
        assert math.isnan(metrics.sharpe_ratio(s))

    def test_M4_quintile_monotonicity(self):
        # 完美单调
        m = {"Q1": 0.20, "Q2": 0.10, "Q3": 0.05, "Q4": 0.02, "Q5": -0.05}
        rho = metrics.quintile_monotonicity_spearman(m)
        assert abs(rho - 1.0) < 1e-9
        # 完全反向
        m = {"Q1": -0.05, "Q2": 0.02, "Q3": 0.05, "Q4": 0.10, "Q5": 0.20}
        rho = metrics.quintile_monotonicity_spearman(m)
        assert abs(rho - (-1.0)) < 1e-9

    def test_M5_win_rate(self):
        s = pd.Series([0.05, -0.02, 0.01, -0.03, 0.0])
        # >0 的有 0.05/0.01 = 2 个，5 个月 → 0.4
        assert abs(metrics.win_rate(s) - 0.4) < 1e-9


# =====================================================================
# R: rebalance
# =====================================================================

class TestRebalance:

    def test_R1_calc_monthly_return_basic(self, tmp_data_dir):
        _make_prices({
            "2024-06": {"00001": 10.0, "00002": 20.0},
            "2024-07": {"00001": 11.0, "00002": 22.0},
        }, tmp_data_dir)
        wide = build_month_end_price()
        holdings = pd.DataFrame([
            {"code": "00001", "weight": 0.5},
            {"code": "00002", "weight": 0.5},
        ])
        gross, per = calc_monthly_return(holdings, "2024-06", "2024-07", wide)
        # 两只都涨 10%，组合 10%
        assert abs(gross - 0.10) < 1e-9
        assert abs(per["00001"] - 0.10) < 1e-9

    def test_R2_suspended_zero_return(self, tmp_data_dir):
        # 00002 在 t_cur 没有价格 → 收益 0；组合 = 0.5*10% + 0.5*0 = 5%
        _make_prices({
            "2024-06": {"00001": 10.0, "00002": 20.0},
            "2024-07": {"00001": 11.0},
        }, tmp_data_dir)
        wide = build_month_end_price()
        holdings = pd.DataFrame([
            {"code": "00001", "weight": 0.5},
            {"code": "00002", "weight": 0.5},
        ])
        gross, per = calc_monthly_return(holdings, "2024-06", "2024-07", wide)
        assert abs(gross - 0.05) < 1e-9
        assert abs(per["00002"]) < 1e-12

    def test_R3_turnover_first_period(self):
        h = pd.DataFrame([{"code": "00001", "weight": 1.0}])
        assert _turnover(None, h) == 1.0

    def test_R4_turnover_full_swap(self):
        a = pd.DataFrame([{"code": "00001", "weight": 1.0}])
        b = pd.DataFrame([{"code": "00002", "weight": 1.0}])
        # |1 - 0| + |0 - 1| = 2; 0.5*2 = 1.0
        assert abs(_turnover(a, b) - 1.0) < 1e-9

    def test_R5_turnover_partial(self):
        a = pd.DataFrame([{"code": "00001", "weight": 0.5}, {"code": "00002", "weight": 0.5}])
        b = pd.DataFrame([{"code": "00001", "weight": 0.5}, {"code": "00003", "weight": 0.5}])
        # 00001: 0.5-0.5=0; 00002: 0-0.5=-0.5; 00003: 0.5-0=0.5；Σ|Δ|=1.0; turnover=0.5
        assert abs(_turnover(a, b) - 0.5) < 1e-9

    def test_R6_no_lookahead(self, tmp_data_dir):
        """
        关键回归：第 m 月持仓必须基于第 m-1 月信号。
        构造：2024-06 信号 TOP1=00001；2024-07 信号 TOP1=00002。
        7 月组合应使用 6 月信号 = 持有 00001。
        """
        _make_ranking("2024-06", [("00001", 5.0), ("00002", 1.0)], tmp_data_dir)
        _make_ranking("2024-07", [("00002", 5.0), ("00001", 1.0)], tmp_data_dir)
        _make_prices({
            "2024-06": {"00001": 10.0, "00002": 100.0},
            "2024-07": {"00001": 12.0, "00002": 50.0},
        }, tmp_data_dir)
        result = run_backtest("2024-06", "2024-07", top_n=1,
                              weight_scheme="equal", transaction_cost_bps=0)
        trace = result["trace"]
        # 应只有一行：信号期 2024-06，持仓期 2024-07
        assert len(trace) == 1
        row = trace.iloc[0]
        assert row["signal_period"] == "2024-06"
        assert row["hold_period"] == "2024-07"
        # 持仓 00001（不是 00002，否则就是未来函数）
        # 组合收益 = +20%（00001 涨 20%）
        assert abs(row["gross_return"] - 0.20) < 1e-9

    def test_R7_full_backtest_with_cost(self, tmp_data_dir):
        """完整 3 期回测 + 成本验证"""
        # 信号期 2024-05, 06；持仓期 06, 07
        _make_ranking("2024-05", [("00001", 5.0)], tmp_data_dir)
        _make_ranking("2024-06", [("00001", 5.0)], tmp_data_dir)  # 同股不换手
        _make_prices({
            "2024-05": {"00001": 10.0},
            "2024-06": {"00001": 11.0},
            "2024-07": {"00001": 12.1},
        }, tmp_data_dir)

        result = run_backtest("2024-05", "2024-07", top_n=1,
                              weight_scheme="equal", transaction_cost_bps=15)
        trace = result["trace"]
        # 信号期循环：2024-05 → 持仓 06；2024-06 → 持仓 07
        assert len(trace) == 2
        # 第一期：首期建仓 turnover=1.0，cost=15bps
        first = trace.iloc[0]
        assert abs(first["turnover"] - 1.0) < 1e-9
        assert abs(first["cost"] - 0.0015) < 1e-9
        assert abs(first["gross_return"] - 0.10) < 1e-9
        assert abs(first["net_return"] - (0.10 - 0.0015)) < 1e-9
        # 第二期：同持仓，turnover=0
        second = trace.iloc[1]
        assert abs(second["turnover"]) < 1e-9
        assert abs(second["cost"]) < 1e-9
        # NAV 复利
        nav_expect = (1 + 0.10 - 0.0015) * (1 + 0.10)
        assert abs(second["nav"] - nav_expect) < 1e-9


# =====================================================================
# Q: quintile end-to-end
# =====================================================================

class TestQuintile:

    def test_Q1_quintile_runs(self, tmp_data_dir):
        # 25 只股，5 个分位每组 5 只
        codes_scores = [(f"{i:05d}", float(100 - i)) for i in range(1, 26)]
        _make_ranking("2024-06", codes_scores, tmp_data_dir)
        _make_ranking("2024-07", codes_scores, tmp_data_dir)
        # 给每个 code 制造价格：高分组涨多，低分组涨少
        prices = {}
        for period, mult in [("2024-06", 1.0), ("2024-07", 1.0)]:
            prices[period] = {code: 10.0 + (100 - score) * 0.0 + 10 for code, score in codes_scores}
        # 修改 2024-07 让 Q1 涨 20%，Q5 跌 5%
        for j, (code, score) in enumerate(codes_scores):
            grp = j // 5  # 0..4
            mult = [1.20, 1.10, 1.05, 1.00, 0.95][grp]
            prices["2024-07"][code] = prices["2024-06"][code] * mult
        _make_prices(prices, tmp_data_dir)

        result = run_quintile_backtest("2024-06", "2024-07", n_quantiles=5,
                                        weight_scheme="equal", transaction_cost_bps=0)
        q = result["quintiles"]
        assert set(q.keys()) == {"Q1", "Q2", "Q3", "Q4", "Q5"}
        # Q1 收益 ~20%，Q5 ~-5%
        q1_ret = q["Q1"]["monthly_returns"].iloc[0]
        q5_ret = q["Q5"]["monthly_returns"].iloc[0]
        assert abs(q1_ret - 0.20) < 1e-9
        assert abs(q5_ret - (-0.05)) < 1e-9
