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
    """01890 case (B50 修正 B39-r8 的过度修复)：

    记录：中期分配 ex_date=2026-03-06，AKShare 标 fiscal_year=2025

    B39-r8 曾断言应归 2025-02-28（"优先信 fiscal_year"），但那是错的：
      - 归 2025-02-28 → 除息比该中报期晚 371 天，不合派息节奏
      - 归 2026-02-28 → 除息比该中报期晚 6 天，典型「财报后立即派息」

    B50 用 ex_date 延迟裁决（interim 窗口 [-180, 365)）在 fy 与 fy+1 中
    选 |延迟| 最小者 → 命中 2026-02-28，与 B5/B11 badcase 基于真实数据的
    期望一致（latest_period_end == "2026-02-28"）。
    """
    today = pd.Timestamp("2026-05-24")
    records = [
        DividendRecord(ex_date=pd.Timestamp("2026-03-06"), amount_hkd=0.07,
                       fiscal_year=2025, distribution_type="中期分配", source="akshare"),
    ]
    out = compute_dividend_fields(records, _periods_aug31(),
                                   fy_anchor=(8, 31), today=today)
    # B50: 归到 2026-02-28（延迟 6 天），该期有派息 → 无需 B38 shift
    assert out["latest_period_end"] == "2026-02-28"
    assert out["latest_period_div_per_share"] == pytest.approx(0.07, abs=1e-6)
    # 同期上年 = 2025-02-28，本记录集无派息
    assert out["latest_period_yoy_end"] == "2025-02-28"
    assert out["latest_period_yoy_div_per_share"] == pytest.approx(0.0, abs=1e-6)
    # fy_div 窗口 = [2026-02-28, 2025-08-31] 含 0.07
    assert out["fy_div_per_share"] == pytest.approx(0.07, abs=1e-6)


# ---- B11-2: 年度分配应归到年报期 ----

def test_annual_distribution_attributed_to_annual_period():
    """B39-r8: fiscal_year=2024 年度分配 → 归到 FY2024 年报期 2024-08-31（不是 FY2025）"""
    today = pd.Timestamp("2026-05-24")
    records = [
        DividendRecord(ex_date=pd.Timestamp("2025-08-01"), amount_hkd=0.06,
                       fiscal_year=2024, distribution_type="年度分配", source="akshare"),
    ]
    out = compute_dividend_fields(records, _periods_aug31(),
                                   fy_anchor=(8, 31), today=today)
    # fy=2024 年度 → 2024-08-31
    # TTM 窗口 [2026-02-28, 2025-08-31] 不含 2024-08-31 → fy_div=0
    assert out["fy_div_per_share"] == pytest.approx(0.0, abs=1e-6)
    # prev_year TTM 窗口 [2025-02-28, 2024-08-31] → 含 0.06
    assert out["prev_year_div_per_share"] == pytest.approx(0.06, abs=1e-6)


# ---- B11-3: 完整 01890 数据复现 ----

def test_b11_01890_full_scenario():
    """01890 完整 4 笔派息归属（B50 修正 B39-r8）：

    B50 裁决规则：interim 窗口 [-180, 365)，annual 窗口 [0, 365)，
    在 fy 与 fy+1 两个候选中选 |延迟| 最小者。

      2024-12-06 fy=2024 中期 0.07
        fy=2024 → 2024-02-29 (+281) 合格 | fy=2025 → 2025-02-28 (-84) 合格
        → 选 2025-02-28（|84| < |281|）
      2025-08-01 fy=2024 年度 0.06
        fy=2024 → 2024-08-31 (+335) 合格 | fy=2025 → 2025-08-31 (-30) 超窗（年度不可提前）
        → 选 2024-08-31
      2026-03-06 fy=2025 中期 0.07
        fy=2025 → 2025-02-28 (+371) 超窗 | fy=2026 → 2026-02-28 (+6) 合格
        → 选 2026-02-28
      2026-07-31 fy=2025 年度 0.06
        fy=2025 → 2025-08-31 (+334) 合格 | fy=2026 → 2026-08-31 (-31) 超窗
        → 选 2025-08-31（pending，尚未除息? 否——2026-07-31 < today 已除息）

    today=2026-05-24：2026-02-28 期有 0.07 → 无需 B38 shift，latest=2026-02-28。
    该结果与 B5/B11 badcase 基于真实数据的期望一致。
    """
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

    # B50: latest = 2026-02-28（该期有 0.07，不触发 B38 shift）
    assert out["latest_period_end"] == "2026-02-28"
    assert out["latest_period_div_per_share"] == pytest.approx(0.07, abs=1e-6)
    # 同期上年 2025-02-28 = 0.07（2024-12-06 那笔）
    assert out["latest_period_yoy_end"] == "2025-02-28"
    assert out["latest_period_yoy_div_per_share"] == pytest.approx(0.07, abs=1e-6)
    # TTM = [2026-02-28, 2025-08-31] = 0.07 + 0.06 = 0.13
    assert out["fy_div_per_share"] == pytest.approx(0.13, abs=1e-6)
    # forward = 2026-08-31 期（未到）+ 当前财年 pending
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
    """B39-r8: fiscal_year=2024 末期分配 → 2024-08-31（不在 FY2025 TTM 窗口）"""
    today = pd.Timestamp("2026-05-24")
    records = [
        DividendRecord(ex_date=pd.Timestamp("2025-08-01"), amount_hkd=0.06,
                       fiscal_year=2024, distribution_type="末期分配", source="akshare"),
    ]
    out = compute_dividend_fields(records, _periods_aug31(),
                                   fy_anchor=(8, 31), today=today)
    # 归到 2024-08-31，不在 TTM [2026-02-28, 2025-08-31] 内 → fy_div=0
    assert out["fy_div_per_share"] == pytest.approx(0.0, abs=1e-6)
    # 在 prev_year TTM [2025-02-28, 2024-08-31] 内
    assert out["prev_year_div_per_share"] == pytest.approx(0.06, abs=1e-6)
