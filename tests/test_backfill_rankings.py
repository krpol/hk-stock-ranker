"""
P3a 测试：历史 ranking 回填
"""
import os
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.backfill_rankings import (
    get_month_end_prices,
    list_available_months,
    backfill_one_month,
)
from storage.store import (
    save_stock_pool, save_financial_data, save_monthly_price, RANKING_CSV,
    load_ranking,
)


def _mk_fin(code, **kwargs):
    base = {
        "code": code,
        "fy_div_per_share": 1.0,
        "forward_div_per_share": 0.0,
        "buyback_per_share": 0.0,
        "real_buyback_per_share": 0.0,
        "annual_dilution_rate": 0.0,
        "current_price": 10.0,
        "net_debt_ratio": 0.30,
        "shares_outstanding": 1_000_000,
        "total_buyback": 0,
        "current_fy_end": "2024-12-31",
        "prev_fy_end": "2023-12-31",
        "latest_period_end": "2024-06-30",
        "latest_period_yoy_end": "2023-06-30",
        "fiscal_periods": "[]",
        "has_dividend_history": True,
        "prev_year_div_per_share": 0.5,
        "two_years_ago_div_per_share": 0.4,
        "latest_period_div_per_share": 0.5,
        "latest_period_yoy_div_per_share": 0.4,
        "ttm_revenue": 100,
        "prev_ttm_revenue": 90,
        "revenue_yoy": 0.111,
        "ttm_operating_income": 20,
        "prev_ttm_operating_income": 18,
        "operating_income_yoy": 0.111,
        "ttm_operating_cashflow": 25,
        "prev_ttm_operating_cashflow": 22,
        "operating_cashflow_yoy": 0.136,
    }
    base.update(kwargs)
    return base


def _mk_price(code, period, close):
    return {
        "code": code, "date": period + "-28",
        "open": close, "high": close, "low": close, "close": close, "volume": 1000,
    }


class TestBackfillRankings:

    def test_get_month_end_prices(self, tmp_data_dir):
        df = pd.DataFrame([
            _mk_price("00001", "2024-06", 10),
            _mk_price("00001", "2024-07", 11),
            _mk_price("00002", "2024-06", 20),
        ])
        save_monthly_price(df)
        wide = get_month_end_prices(df)
        assert "2024-06" in wide.index
        assert "2024-07" in wide.index
        assert wide.loc["2024-06", "00001"] == 10
        assert wide.loc["2024-07", "00001"] == 11

    def test_backfill_creates_ranking(self, tmp_data_dir):
        # 准备 stock_pool + financial + price
        sp = pd.DataFrame([
            {"code": "00001", "name": "A", "market_cap": 100_000_000},
            {"code": "00002", "name": "B", "market_cap": 100_000_000},
        ])
        save_stock_pool(sp)
        save_financial_data(pd.DataFrame([_mk_fin("00001"), _mk_fin("00002")]))

        prices = pd.DataFrame([
            _mk_price("00001", "2024-06", 10),  # return = 1/10 = 10%
            _mk_price("00002", "2024-06", 5),   # return = 1/5 = 20% → rank 1
        ])
        save_monthly_price(prices)

        sp_loaded = pd.read_csv(tmp_data_dir["csv"] / "hk_stock_pool.csv", dtype={"code": str})
        fin_loaded = pd.read_csv(tmp_data_dir["csv"] / "hk_financial.csv", dtype={"code": str})
        wide = get_month_end_prices(prices)

        ok = backfill_one_month("2024-06", sp_loaded, fin_loaded, wide, skip_existing=False)
        assert ok is True

        result = load_ranking("2024-06")
        assert len(result) == 2
        # 价格低的 00002 return_rate 更高 → rank 1
        result_idx = result.set_index("code")
        assert result_idx.loc["00002", "rank"] == 1
        assert result_idx.loc["00001", "rank"] == 2

    def test_skip_existing(self, tmp_data_dir):
        sp = pd.DataFrame([{"code": "00001", "name": "A", "market_cap": 100_000_000}])
        save_stock_pool(sp)
        save_financial_data(pd.DataFrame([_mk_fin("00001")]))
        prices = pd.DataFrame([_mk_price("00001", "2024-06", 10)])
        save_monthly_price(prices)
        wide = get_month_end_prices(prices)

        sp_loaded = pd.read_csv(tmp_data_dir["csv"] / "hk_stock_pool.csv", dtype={"code": str})
        fin_loaded = pd.read_csv(tmp_data_dir["csv"] / "hk_financial.csv", dtype={"code": str})

        ok1 = backfill_one_month("2024-06", sp_loaded, fin_loaded, wide, skip_existing=False)
        assert ok1 is True
        ok2 = backfill_one_month("2024-06", sp_loaded, fin_loaded, wide, skip_existing=True)
        assert ok2 is False  # 已存在，跳过
