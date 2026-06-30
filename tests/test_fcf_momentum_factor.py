"""
B33 测试：TTM 版 real_fcf 两期 + FCF/净资产 + 现金流动量因子，并入增长因子。

口径：
  real_fcf_ttm      = ocf_ttm      − min(capex_ttm + lease_ttm,  da_ttm)
  prev_real_fcf_ttm = prev_ttm_ocf − min(prev_capex + lease_prev, prev_da)
  fcf_to_equity     = real_fcf_ttm / equity_latest
  fcf_momentum      = (real_fcf_ttm − prev_real_fcf_ttm) / equity_latest
"""
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from engine.ranker import rank_stocks


def _mk_fin(code, **kw):
    base = {
        "code": code,
        "fy_div_per_share": 1.0,
        "forward_div_per_share": 0.0,
        "buyback_per_share": 0.0,
        "real_buyback_per_share": 0.0,
        "annual_dilution_rate": 0.0,
        "net_debt_ratio": 0.30,
        "shares_outstanding": 1_000_000,
        "total_buyback": 0,
        "has_dividend_history": True,
        "prev_year_div_per_share": 0.5,
        "two_years_ago_div_per_share": 0.4,
        "ttm_revenue": 100, "prev_ttm_revenue": 90, "revenue_yoy": 0.11,
        "ttm_operating_income": 20, "prev_ttm_operating_income": 18,
        "operating_income_yoy": 0.11,
        "ttm_operating_cashflow": 25, "prev_ttm_operating_cashflow": 22,
        "operating_cashflow_yoy": 0.13,
        # B33 TTM real_fcf 组件
        "capex_ttm": 5.0, "da_ttm": 6.0, "current_lease_obligation_ttm": 0.0,
        "prev_capex": 4.0, "prev_da": 5.0, "current_lease_obligation_prev": 0.0,
        "equity_latest": 100.0,
        "ocf_3y_avg": 24.0, "capex_3y_avg": 4.5, "dep_amortization_3y_avg": 5.5,
        "cash_and_equivalents": 50.0, "interest_bearing_debt": 10.0,
    }
    base.update(kw)
    return base


def _mk_stock(code, price=10.0):
    return {"code": code, "name": f"S{code}", "current_price": price,
            "pe_ttm": 10.0, "pb": 1.0, "market_cap": 100_000_000}


def _build(n=12, **override_first):
    """构造 n 只股票，第 1 只可覆盖参数以便断言。"""
    stocks, fins = [], []
    for i in range(n):
        code = f"{i+1:05d}"
        stocks.append(_mk_stock(code))
        kw = override_first if i == 0 else {}
        # 让各股 yoy 略有差异，保证 zscore 有方差
        fins.append(_mk_fin(code, revenue_yoy=0.10 + i * 0.01, **kw))
    return pd.DataFrame(stocks), pd.DataFrame(fins)


def test_real_fcf_ttm_and_derived_columns():
    sl, fin = _build()
    res = rank_stocks(sl, fin, require_dividend_history=False)
    row = res.set_index("code").loc["00001"]
    # real_fcf_ttm = 25 − min(5+0, 6) = 25 − 5 = 20
    assert row["real_fcf_ttm"] == pytest.approx(20.0)
    # prev_real_fcf_ttm = 22 − min(4+0, 5) = 22 − 4 = 18
    assert row["prev_real_fcf_ttm"] == pytest.approx(18.0)
    # fcf_to_equity = 20 / 100 = 0.20
    assert row["fcf_to_equity"] == pytest.approx(0.20, abs=1e-4)
    # fcf_momentum = (20 − 18) / 100 = 0.02
    assert row["fcf_momentum"] == pytest.approx(0.02, abs=1e-4)


def test_da_cap_applied():
    # capex_ttm + lease 超过 da 时，扣减取 da（更小的现金流出上限）
    sl, fin = _build(capex_ttm=100.0, da_ttm=6.0,
                     current_lease_obligation_ttm=0.0)
    res = rank_stocks(sl, fin, require_dividend_history=False)
    row = res.set_index("code").loc["00001"]
    # min(100+0, 6) = 6 → real_fcf_ttm = 25 − 6 = 19
    assert row["real_fcf_ttm"] == pytest.approx(19.0)


def test_equity_zero_yields_na():
    sl, fin = _build(equity_latest=0.0)
    res = rank_stocks(sl, fin, require_dividend_history=False)
    row = res.set_index("code").loc["00001"]
    assert pd.isna(row["fcf_to_equity"])
    assert pd.isna(row["fcf_momentum"])


def test_growth_factor_includes_new_subfactors():
    sl, fin = _build()
    res = rank_stocks(sl, fin, require_dividend_history=False)
    # 新增两个子因子 zscore 列存在
    assert "growth_zscore_fcf_equity" in res.columns
    assert "growth_zscore_fcf_momentum" in res.columns
    # growth_factor 非全 NaN（6 子因子加权后应有值）
    assert res["growth_factor"].notna().any()


def test_no_legacy_fcf_div_columns():
    sl, fin = _build()
    res = rank_stocks(sl, fin, require_dividend_history=False)
    assert "fcf_div_ratio" not in res.columns
    assert "fcf_div_zscore" not in res.columns
