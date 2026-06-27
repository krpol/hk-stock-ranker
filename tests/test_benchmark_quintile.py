"""
P4 测试：benchmark 与五分位扩展
"""
import os
from pathlib import Path

import pandas as pd
import pytest

from backtest import benchmark
from backtest.metrics import quintile_monotonicity_spearman, summarize


class TestBenchmarkBasic:
    """benchmark 模块单元测试（不打网络）"""

    def test_save_and_load(self, tmp_data_dir, monkeypatch):
        # 重定向 BENCHMARK_CSV 到 tmp
        target = tmp_data_dir["csv"] / "hk_benchmark_hsi.csv"
        monkeypatch.setattr(benchmark, "BENCHMARK_CSV", str(target))

        df = pd.DataFrame({
            "date": pd.to_datetime(["2024-05-31", "2024-06-30", "2024-07-31"]),
            "close": [18000.0, 18500.0, 17800.0],
        })
        benchmark.save_hsi(df)
        assert target.exists()
        loaded = benchmark.load_hsi()
        assert len(loaded) == 3
        assert abs(loaded.iloc[0]["close"] - 18000.0) < 1e-9

    def test_get_monthly_returns(self, tmp_data_dir, monkeypatch):
        target = tmp_data_dir["csv"] / "hk_benchmark_hsi.csv"
        monkeypatch.setattr(benchmark, "BENCHMARK_CSV", str(target))
        df = pd.DataFrame({
            "date": pd.to_datetime(["2024-05-31", "2024-06-30", "2024-07-31"]),
            "close": [100.0, 110.0, 99.0],
        })
        benchmark.save_hsi(df)
        rets = benchmark.get_hsi_monthly_returns()
        # 6月 vs 5月 = +10%；7月 vs 6月 = -10%
        assert abs(rets["2024-06"] - 0.10) < 1e-9
        assert abs(rets["2024-07"] - (-0.10)) < 1e-9

    def test_get_returns_with_period_filter(self, tmp_data_dir, monkeypatch):
        target = tmp_data_dir["csv"] / "hk_benchmark_hsi.csv"
        monkeypatch.setattr(benchmark, "BENCHMARK_CSV", str(target))
        df = pd.DataFrame({
            "date": pd.to_datetime(["2024-05-31", "2024-06-30", "2024-07-31", "2024-08-31"]),
            "close": [100.0, 110.0, 99.0, 105.0],
        })
        benchmark.save_hsi(df)
        rets = benchmark.get_hsi_monthly_returns(start_period="2024-07", end_period="2024-07")
        assert list(rets.index) == ["2024-07"]


class TestQuintileMonotonicityExtra:
    """五分位单调性 Spearman 扩展测试"""

    def test_partial_monotonic(self):
        # Q1>Q2>Q4>Q3>Q5  (期望 1,2,3,4,5；实际 1,2,4,3,5；只交换 3-4 两位)
        m = {"Q1": 0.20, "Q2": 0.15, "Q3": 0.05, "Q4": 0.08, "Q5": -0.02}
        rho = quintile_monotonicity_spearman(m)
        # Spearman 应该接近 1 但小于 1
        assert 0.5 < rho < 1.0

    def test_all_equal(self):
        # 全相等：Spearman 退化到方差为零；实现允许返回 nan/0/1（rank 强制单调）
        m = {"Q1": 0.05, "Q2": 0.05, "Q3": 0.05, "Q4": 0.05, "Q5": 0.05}
        import math
        rho = quintile_monotonicity_spearman(m)
        # nan / 0 / ±1 都可接受（实现细节，主要是不能崩）
        assert math.isnan(rho) or abs(rho) <= 1.0 + 1e-9

    def test_summarize_with_benchmark(self):
        """summarize() 接受 benchmark_returns 计算 IR"""
        rets = pd.Series([0.01, 0.02, -0.01, 0.03, 0.005],
                          index=["2024-05", "2024-06", "2024-07", "2024-08", "2024-09"])
        bench = pd.Series([0.005, 0.01, -0.005, 0.02, 0.0],
                           index=["2024-05", "2024-06", "2024-07", "2024-08", "2024-09"])
        nav = (1 + rets).cumprod()
        res = summarize(rets, nav, benchmark_returns=bench)
        assert "annualized_return" in res
        assert "max_drawdown" in res
        assert "sharpe_ratio" in res
        # IR 必须存在（且为有限实数）
        assert "information_ratio" in res
        import math
        assert math.isfinite(res["information_ratio"])
