"""
pytest 通用 fixture：
- tmp_data_dir: 临时数据目录（每个测试隔离 data/csv）
- patch_storage: 重定向 storage.store 的 CSV 路径到 tmp_data_dir
- mock_collect_financial: mock collectors.hk_financial.collect_financial_data
"""
import os
import sys
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import pytest

# 确保项目根目录在 PATH 中
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


@pytest.fixture
def tmp_data_dir(tmp_path, monkeypatch):
    """创建临时 data/csv 目录并 patch 所有相关路径"""
    csv_dir = tmp_path / "csv"
    excel_dir = tmp_path / "excel"
    csv_dir.mkdir()
    excel_dir.mkdir()

    # patch storage.store 的所有路径常量
    import storage.store as store
    monkeypatch.setattr(store, "CSV_DIR", str(csv_dir))
    monkeypatch.setattr(store, "EXCEL_DIR", str(excel_dir))
    monkeypatch.setattr(store, "STOCK_LIST_CSV", str(csv_dir / "hk_stock_list.csv"))
    monkeypatch.setattr(store, "STOCK_POOL_CSV", str(csv_dir / "hk_stock_pool.csv"))
    pools_dir = csv_dir / "pools"
    pools_dir.mkdir(exist_ok=True)
    monkeypatch.setattr(store, "POOL_SNAPSHOT_DIR", str(pools_dir))
    monkeypatch.setattr(store, "POOL_SNAPSHOT_CSV", str(pools_dir / "hk_stock_pool_{date}.csv"))
    monkeypatch.setattr(store, "MONTHLY_PRICE_CSV", str(csv_dir / "hk_monthly_price.csv"))
    monkeypatch.setattr(store, "FINANCIAL_CSV", str(csv_dir / "hk_financial.csv"))
    monkeypatch.setattr(store, "RANKING_CSV", str(csv_dir / "hk_ranking_{period}.csv"))
    monkeypatch.setattr(store, "RANKING_EXCEL", str(excel_dir / "hk_ranking_{period}.xlsx"))

    return {"csv": csv_dir, "excel": excel_dir, "root": tmp_path}


@pytest.fixture
def fake_stock_list():
    """3 只股票的小股票池"""
    return pd.DataFrame([
        {"code": "00001", "name": "Alpha"},
        {"code": "00002", "name": "Beta"},
        {"code": "00003", "name": "Gamma"},
    ])


def _make_fin_record(code: str, fy_div: float = 0.5, current_fy_end: str = "2024-12-31"):
    """构造一条 collect_financial_data 期望返回的字典"""
    return {
        "code": code,
        "fy_div_per_share": fy_div,
        "prev_year_div_per_share": fy_div * 0.9,
        "two_years_ago_div_per_share": fy_div * 0.85,
        "latest_period_div_per_share": fy_div * 0.5,
        "latest_period_yoy_div_per_share": fy_div * 0.45,
        "forward_div_per_share": 0.0,
        "current_fy_end": current_fy_end,
        "prev_fy_end": "2023-12-31",
        "latest_period_end": "2024-06-30",
        "latest_period_yoy_end": "2023-06-30",
        "latest_attributed_fy": "2024-12-31",
        "fiscal_periods": "[]",
        "total_buyback": 0.0,
        "shares_outstanding": 1_000_000,
        "buyback_per_share": 0.0,
        "net_debt_ratio": 0.30,
        "annual_dilution_rate": 0.0,
        "forward_div_source": "yfinance_estimate",
        "has_dividend_history": True,
        "current_price": 10.0,
        "prev_buyback_per_share": 0.0,
        "prev_total_buyback": 0.0,
        "prev_shares_outstanding": 1_000_000,
        "splits_ratio_in_window": 1.0,
        "splits_ratio_calculated": False,
        "ttm_revenue": 100.0,
        "prev_ttm_revenue": 90.0,
        "revenue_yoy": 0.111,
        "ttm_operating_income": 20.0,
        "prev_ttm_operating_income": 18.0,
        "operating_income_yoy": 0.111,
        "ttm_operating_cashflow": 25.0,
        "prev_ttm_operating_cashflow": 22.0,
        "operating_cashflow_yoy": 0.136,
    }


@pytest.fixture
def make_fin_record():
    return _make_fin_record


@pytest.fixture
def mock_collect(monkeypatch):
    """mock collect_financial_data 返回固定结构，记录所有调用"""
    call_log = []

    def fake_collect(code, forward_div_override=None, check_dividend_history=True):
        call_log.append(code)
        return _make_fin_record(code)

    # 在 collectors.incremental 模块的命名空间中替换
    import collectors.incremental as inc
    monkeypatch.setattr(inc, "collect_financial_data", fake_collect)
    return call_log
