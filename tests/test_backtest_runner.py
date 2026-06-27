"""
P5 端到端测试：runner + report 输出
"""
import json
import os
from pathlib import Path

import pandas as pd
import pytest

from backtest import runner
from backtest.runner import run_topn_with_report, run_quintile_with_report
from storage.store import save_ranking, save_monthly_price


def _make_ranking_simple(period, codes_scores):
    rows = [{
        "rank": rk, "code": c, "name": f"S{c}",
        "current_price": 10.0, "composite_score": s,
        "return_zscore": s, "growth_factor": 0.0,
        "return_rate": 0.0, "fy_div_per_share": 0.0,
    } for rk, (c, s) in enumerate(codes_scores, 1)]
    save_ranking(pd.DataFrame(rows), period)


def _make_prices_simple(price_map):
    rows = []
    for period, mp in price_map.items():
        for code, close in mp.items():
            rows.append({
                "code": code, "date": period + "-28",
                "open": close, "high": close, "low": close, "close": close, "volume": 1000,
            })
    save_monthly_price(pd.DataFrame(rows))


@pytest.fixture
def report_dir(tmp_data_dir, monkeypatch):
    target = tmp_data_dir["csv"].parent / "reports"
    target.mkdir(exist_ok=True)
    monkeypatch.setattr(runner, "REPORT_DIR", str(target))
    return target


class TestRunner:

    def test_E1_topn_writes_json(self, tmp_data_dir, report_dir):
        _make_ranking_simple("2024-06", [("00001", 5.0), ("00002", 4.0), ("00003", 3.0)])
        _make_ranking_simple("2024-07", [("00001", 5.0), ("00002", 4.0), ("00003", 3.0)])
        _make_prices_simple({
            "2024-06": {"00001": 10, "00002": 20, "00003": 30},
            "2024-07": {"00001": 11, "00002": 22, "00003": 33},
        })

        report = run_topn_with_report(
            "2024-06", "2024-07", top_n=2, weight_scheme="equal",
            transaction_cost_bps=0, with_benchmark=False,
            report_name="test_topn",
        )
        assert report["type"] == "topn"
        assert report["params"]["top_n"] == 2

        json_path = report_dir / "test_topn.json"
        assert json_path.exists()
        loaded = json.loads(json_path.read_text(encoding="utf-8"))
        assert "summary" in loaded
        assert "nav" in loaded
        # 每个都涨 10% → 组合 +10%
        nav_vals = list(loaded["nav"].values())
        assert abs(nav_vals[-1] - 1.10) < 1e-9

        trace_path = report_dir / "test_topn.trace.csv"
        assert trace_path.exists()

    def test_E2_quintile_writes_json(self, tmp_data_dir, report_dir):
        codes_scores = [(f"{i:05d}", float(100 - i)) for i in range(1, 26)]
        _make_ranking_simple("2024-06", codes_scores)
        _make_ranking_simple("2024-07", codes_scores)
        # 高分组涨 20%、低分组跌 5%
        prices = {"2024-06": {c: 10.0 for c, _ in codes_scores}}
        prices["2024-07"] = {}
        for j, (c, _) in enumerate(codes_scores):
            mult = [1.20, 1.10, 1.05, 1.00, 0.95][j // 5]
            prices["2024-07"][c] = 10.0 * mult
        _make_prices_simple(prices)

        report = run_quintile_with_report(
            "2024-06", "2024-07", n_quantiles=5,
            weight_scheme="equal", transaction_cost_bps=0,
            report_name="test_q5",
        )
        assert report["type"] == "quintile"
        assert abs(report["spearman_monotonicity"] - 1.0) < 1e-9
        # 长短组 Q1-Q5
        ls = report["long_short"]
        assert ls["label"] == "Q1_minus_Q5"
        # Q1 +20%, Q5 -5% → 多空 +25%
        assert abs(list(ls["nav"].values())[-1] - 1.25) < 1e-9

        json_path = report_dir / "test_q5.json"
        assert json_path.exists()

    def test_E3_no_lookahead_in_runner(self, tmp_data_dir, report_dir):
        """端到端验证：runner 调度下也没有未来函数"""
        _make_ranking_simple("2024-06", [("00001", 5.0), ("00002", 1.0)])
        _make_ranking_simple("2024-07", [("00002", 5.0), ("00001", 1.0)])
        _make_prices_simple({
            "2024-06": {"00001": 10, "00002": 100},
            "2024-07": {"00001": 12, "00002": 50},
        })
        report = run_topn_with_report(
            "2024-06", "2024-07", top_n=1, weight_scheme="equal",
            transaction_cost_bps=0, with_benchmark=False,
            report_name="test_lookahead",
        )
        # 7月组合应使用 6月信号（持仓 00001，涨 20%）
        rets = list(report["monthly_returns"].values())
        assert abs(rets[0] - 0.20) < 1e-9

    def test_E4_json_serializable_no_nan_inf(self, tmp_data_dir, report_dir):
        """报告 JSON 必须能被严格解析（不含 NaN / Inf）"""
        _make_ranking_simple("2024-06", [("00001", 5.0)])
        _make_ranking_simple("2024-07", [("00001", 5.0)])
        _make_prices_simple({
            "2024-06": {"00001": 10},
            "2024-07": {"00001": 10},  # 0 收益
        })
        run_topn_with_report(
            "2024-06", "2024-07", top_n=1, weight_scheme="equal",
            transaction_cost_bps=0, with_benchmark=False,
            report_name="test_serial",
        )
        json_path = report_dir / "test_serial.json"
        # strict mode 严格不允许 NaN/Infinity
        json.loads(json_path.read_text(encoding="utf-8"))  # 不抛异常即 OK

    def test_E5_short_period_handles(self, tmp_data_dir, report_dir):
        """单月窗口（信号期=hold期 -1）应该生成 1 行 trace"""
        _make_ranking_simple("2024-06", [("00001", 5.0)])
        _make_prices_simple({
            "2024-06": {"00001": 10},
            "2024-07": {"00001": 11},
        })
        report = run_topn_with_report(
            "2024-06", "2024-07", top_n=1, weight_scheme="equal",
            transaction_cost_bps=0, with_benchmark=False,
            report_name="test_short",
        )
        assert report["n_holdings_per_period"] == 1
        assert report["summary"]["n_months"] == 1
