"""
pytest 通用 fixture：
- tmp_data_dir: 临时数据目录（每个测试隔离 data/csv）
- patch_storage: 重定向 storage.store 的 CSV 路径到 tmp_data_dir
- mock_collect_financial: mock collectors.hk_financial.collect_financial_data
"""
import os
import sys
from datetime import datetime, timedelta
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

    # collectors.incremental 在模块级把 CSV_DIR 固化进 sidecar 路径，
    # 只 patch store.CSV_DIR 不生效 —— 测试会读写项目真实 sidecar，
    # 导致假股票码（00001 等）污染 data/csv 且上一次运行的 failed 记录
    # 让本次「不应刷新」的断言失效。
    import collectors.incremental as incremental
    monkeypatch.setattr(incremental, "FAILED_SIDECAR", str(csv_dir / "hk_financial_failed.json"))
    monkeypatch.setattr(incremental, "EMPTY_SIDECAR", str(csv_dir / "hk_financial_empty.json"))

    return {"csv": csv_dir, "excel": excel_dir, "root": tmp_path}


@pytest.fixture
def fake_stock_list():
    """3 只股票的小股票池"""
    return pd.DataFrame([
        {"code": "00001", "name": "Alpha"},
        {"code": "00002", "name": "Beta"},
        {"code": "00003", "name": "Gamma"},
    ])


def _make_fin_record(code: str, fy_div: float = 0.5, current_fy_end: str = None):
    """构造一条 collect_financial_data 期望返回的字典

    current_fy_end 默认取 60 天前（动态）：needs_refresh 在
    「距财年末 > FORCE_REFRESH_AFTER_FY_DAYS(120)」时会触发 fy_report_due 强刷，
    写死日期会随时间推移越过该阈值，使 F2/F3/F5/F8 等「不应刷新」的断言失效。
    """
    if current_fy_end is None:
        current_fy_end = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d")
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
        rec = _make_fin_record(code)
        # 真实 collect_financial_data 必带 _status；缺失时 _merge_record_by_status
        # 会把每个 domain 默认判 fail 而不写入任何字段，结果只剩 code 列。
        from collectors.hk_financial import ALL_DOMAINS
        rec["_status"] = {d: "ok" for d in ALL_DOMAINS}
        rec["_reason"] = {}
        return rec

    # 在 collectors.incremental 模块的命名空间中替换
    import collectors.incremental as inc
    monkeypatch.setattr(inc, "collect_financial_data", fake_collect)
    return call_log
