"""
B3: 资产负债率 + 有息负债率 双阈值过滤测试
"""
import pandas as pd
import pytest

from engine.ranker import rank_stocks


def _stock_list():
    return pd.DataFrame([
        {"code": "00001", "name": "A", "current_price": 10.0, "market_cap": 1e10, "pe_ttm": 10, "pb": 1},
        {"code": "00002", "name": "B", "current_price": 10.0, "market_cap": 1e10, "pe_ttm": 10, "pb": 1},
        {"code": "00003", "name": "C", "current_price": 10.0, "market_cap": 1e10, "pe_ttm": 10, "pb": 1},
        {"code": "00004", "name": "D", "current_price": 10.0, "market_cap": 1e10, "pe_ttm": 10, "pb": 1},
        {"code": "00005", "name": "E", "current_price": 10.0, "market_cap": 1e10, "pe_ttm": 10, "pb": 1},
    ])


def _fin(net_debt_list, total_liab_list):
    rows = []
    for i, (nd, tl) in enumerate(zip(net_debt_list, total_liab_list), 1):
        rows.append({
            "code": f"{i:05d}",
            "fy_div_per_share": 0.5,
            "prev_year_div_per_share": 0.45,
            "two_years_ago_div_per_share": 0.4,
            "latest_period_div_per_share": 0.25,
            "latest_period_yoy_div_per_share": 0.22,
            "forward_div_per_share": 0.0,
            "buyback_per_share": 0.0,
            "prev_buyback_per_share": 0.0,
            "shares_outstanding": 1_000_000,
            "prev_shares_outstanding": 1_000_000,
            "splits_ratio_in_window": 1.0,
            "total_buyback": 0.0,
            "annual_dilution_rate": 0.0,
            "net_debt_ratio": nd,
            "total_liability_ratio": tl,
            "has_dividend_history": True,
        })
    return pd.DataFrame(rows)


# ---- D5: 双阈值过滤主路径 ----
def test_D5_dual_threshold_filter():
    """
    场景：5 只股票
        00001: net=0.40 total=0.50  -> 都通过 ✅
        00002: net=0.60 total=0.50  -> net 超 0.50 ❌
        00003: net=0.40 total=0.85  -> total 超 0.80 ❌
        00004: net=0.60 total=0.85  -> 都超 ❌
        00005: net=0.49 total=0.79  -> 都通过 ✅
    阈值：max_net_debt_ratio=0.50, max_total_liability_ratio=0.80
    期望：仅 00001 和 00005 保留
    """
    sl = _stock_list()
    fin = _fin(
        net_debt_list=[0.40, 0.60, 0.40, 0.60, 0.49],
        total_liab_list=[0.50, 0.50, 0.85, 0.85, 0.79],
    )
    out = rank_stocks(sl, fin, max_net_debt_ratio=0.50, max_total_liability_ratio=0.80)
    codes = set(out["code"].tolist())
    assert codes == {"00001", "00005"}, f"实际保留 {codes}"


# ---- D6: NaN 不被过滤 ----
def test_D6_nan_preserved():
    """
    场景：3 只股票
        00001: net=0.30 total=NaN  -> 都通过（NaN 不过滤）✅
        00002: net=NaN  total=0.40 -> 都通过（NaN 不过滤）✅
        00003: net=NaN  total=NaN  -> 都通过（NaN 不过滤）✅
    """
    sl = _stock_list().head(3)
    base = {
        "fy_div_per_share": 0.5,
        "prev_year_div_per_share": 0.45,
        "two_years_ago_div_per_share": 0.4,
        "latest_period_div_per_share": 0.25,
        "latest_period_yoy_div_per_share": 0.22,
        "forward_div_per_share": 0.0,
        "buyback_per_share": 0.0,
        "prev_buyback_per_share": 0.0,
        "shares_outstanding": 1_000_000,
        "prev_shares_outstanding": 1_000_000,
        "splits_ratio_in_window": 1.0,
        "total_buyback": 0.0,
        "annual_dilution_rate": 0.0,
        "has_dividend_history": True,
    }
    fin = pd.DataFrame([
        {"code": "00001", "net_debt_ratio": 0.30, "total_liability_ratio": float("nan"), **base},
        {"code": "00002", "net_debt_ratio": float("nan"), "total_liability_ratio": 0.40, **base},
        {"code": "00003", "net_debt_ratio": float("nan"), "total_liability_ratio": float("nan"), **base},
    ])
    out = rank_stocks(sl, fin, max_net_debt_ratio=0.50, max_total_liability_ratio=0.80)
    assert len(out) == 3, "NaN 行应保留，未发生过滤"
