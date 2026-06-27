"""
B11 单测：按 distribution_type 把派息归到正确的财报期。

修复前的 bug：
  - AKShare 的 `fiscal_year` 字段对部分公司有语义错位（偏小 1 年），
    导致跨财年的中期派息被错归到年报期，进而 fy_div 双计 + latest_period_div 为 0。

修复后的契约：
  - distribution_type 含 "中" → 归到距 ex_date 最近的中报期
  - distribution_type 含 "年" / "末" / "特别" → 归到距 ex_date 最近的年报期
  - 其他/空 → 走 R3 启发式（不在本测试覆盖范围）
"""
import pytest
import pandas as pd

from engine.dividend_attribution import DividendRecord, compute_dividend_fields


def _periods_aug31():
    """财年 8-31 公司的常见 6 个财报期"""
    return [
        pd.Timestamp("2026-08-31"), pd.Timestamp("2026-02-28"),
        pd.Timestamp("2025-08-31"), pd.Timestamp("2025-02-28"),
        pd.Timestamp("2024-08-31"), pd.Timestamp("2024-02-29"),
    ]


# ---- B11-1: 中期分配跨财年时应归到中报期，不归年报期 ----

def test_interim_distribution_attributed_to_interim_period_across_fy():
    """01890 case：FY2026 中期 ex_date=2026-03-06 → 必须归到 2026-02-28 中报期"""
    today = pd.Timestamp("2026-05-24")
    records = [
        DividendRecord(ex_date=pd.Timestamp("2026-03-06"), amount_hkd=0.07,
                       fiscal_year=2025, distribution_type="中期分配", source="akshare"),
    ]
    out = compute_dividend_fields(records, _periods_aug31(),
                                   fy_anchor=(8, 31), today=today)
    assert out["latest_period_end"] == "2026-02-28"
    assert out["latest_period_div_per_share"] == pytest.approx(0.07, abs=1e-6)


# ---- B11-2: 年度分配应归到年报期 ----

def test_annual_distribution_attributed_to_annual_period():
    today = pd.Timestamp("2026-05-24")
    records = [
        DividendRecord(ex_date=pd.Timestamp("2025-08-01"), amount_hkd=0.06,
                       fiscal_year=2024, distribution_type="年度分配", source="akshare"),
    ]
    out = compute_dividend_fields(records, _periods_aug31(),
                                   fy_anchor=(8, 31), today=today)
    # FY2025 = (2024-08-31, 2025-08-31] 含 2025-08-31 年报
    assert out["fy_div_per_share"] == pytest.approx(0.06, abs=1e-6)


# ---- B11-3: 完整 01890 数据复现 ----

def test_b11_01890_full_scenario():
    """01890 中国科培完整 4 笔派息归属，所有字段全检查"""
    today = pd.Timestamp("2026-05-24")
    records = [
        DividendRecord(ex_date=pd.Timestamp("2024-12-06"), amount_hkd=0.07,
                       fiscal_year=2024, distribution_type="中期分配", source="akshare"),
        DividendRecord(ex_date=pd.Timestamp("2025-08-01"), amount_hkd=0.06,
                       fiscal_year=2024, distribution_type="年度分配", source="akshare"),
        DividendRecord(ex_date=pd.Timestamp("2026-03-06"), amount_hkd=0.07,
                       fiscal_year=2025, distribution_type="中期分配", source="akshare"),
        DividendRecord(ex_date=pd.Timestamp("2026-07-31"), amount_hkd=0.06,
                       fiscal_year=2025, distribution_type="年度分配", source="akshare"),
    ]
    out = compute_dividend_fields(records, _periods_aug31(),
                                   fy_anchor=(8, 31), today=today)

    # FY2025 = (2024-08-31, 2025-08-31] 含中报 2025-02-28 + 年报 2025-08-31
    #   = 中期 0.07(2024-12-06) + 年度 0.06(2025-08-01) = 0.13
    assert out["fy_div_per_share"] == pytest.approx(0.13, abs=1e-6)
    # 最近期 2026-02-28 = 0.07
    assert out["latest_period_div_per_share"] == pytest.approx(0.07, abs=1e-6)
    # 同期上年 2025-02-28 = 0.07
    assert out["latest_period_yoy_div_per_share"] == pytest.approx(0.07, abs=1e-6)
    # forward = 2026-07-31 pending 0.06
    assert out["forward_div_per_share"] == pytest.approx(0.06, abs=1e-6)


# ---- B11-4: 09616 类延迟派息：年度 ex_date 比 fy_end 晚 8 个月仍应归到该年报期 ----

def test_annual_distribution_delayed_8months():
    """09616 case：fy=2024 年报 12-31，年度派息 ex_date=2025-08-29 → 归到 2024-12-31"""
    today = pd.Timestamp("2025-12-31")
    records = [
        DividendRecord(ex_date=pd.Timestamp("2025-08-29"), amount_hkd=0.388,
                       fiscal_year=2024, distribution_type="年度分配", source="akshare"),
    ]
    periods = [
        pd.Timestamp("2025-12-31"), pd.Timestamp("2025-06-30"),
        pd.Timestamp("2024-12-31"), pd.Timestamp("2024-06-30"),
    ]
    out = compute_dividend_fields(records, periods, fy_anchor=(12, 31), today=today)
    # prev_year_div (FY2024) 应该是 0.388
    assert out["prev_year_div_per_share"] == pytest.approx(0.388, abs=1e-6)


# ---- B11-5: 空 / 未知 distribution_type → 不被 R2 归属，留给 R3 ----

def test_unknown_distribution_type_falls_through_to_r3():
    """yfinance 兜底进来的派息 distribution_type='' → R2 不处理 → R3 启发式接管"""
    today = pd.Timestamp("2026-05-24")
    records = [
        DividendRecord(ex_date=pd.Timestamp("2025-08-01"), amount_hkd=0.06,
                       fiscal_year=None, distribution_type="", source="yfinance"),
    ]
    out = compute_dividend_fields(records, _periods_aug31(),
                                   fy_anchor=(8, 31), today=today)
    # R3 兜底窗口 (fy_end, fy_end+8月] 应能归到 2025-02-28 中报或 2024-08-31 年报
    # 至少应被某个 period 收录，fy_div 体系应非零
    total = (out["fy_div_per_share"] + out["prev_year_div_per_share"]
             + out["two_years_ago_div_per_share"])
    assert total >= 0.06 - 1e-6


# ---- B11-6: "末期" 派息（特别派息别名）归到年报期 ----

def test_terminal_distribution_attributed_to_annual_period():
    today = pd.Timestamp("2026-05-24")
    records = [
        DividendRecord(ex_date=pd.Timestamp("2025-08-01"), amount_hkd=0.06,
                       fiscal_year=2024, distribution_type="末期分配", source="akshare"),
    ]
    out = compute_dividend_fields(records, _periods_aug31(),
                                   fy_anchor=(8, 31), today=today)
    # "末期" 也按年报期归属
    assert out["fy_div_per_share"] == pytest.approx(0.06, abs=1e-6)
