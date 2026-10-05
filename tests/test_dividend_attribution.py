"""B1 派息归属测试 — 验证 AKShare 「财政年度」精确归属修复"""
import pandas as pd
import pytest
from collectors.hk_dividend_history import parse_dividend_amount_hkd


# ---- D1: 金额解析（不依赖网络） ----

@pytest.mark.parametrize("scheme,expected", [
    ("每股派港币0.388元", 0.388),
    ("每股派港币5.3元", 5.3),
    ("每股派港币0.28元", 0.28),
    # 多币种：优先抓「相当于港币」
    ("每股派美元0.45元(相当于港币3.522942元)", 3.522942),
    ("每股派美元0.13125元(相当于港币1.027545元(计算值))", 1.027545),
    # B7: 原文末尾缺「元」字（02602 万物云 2024 中期），旧正则匹配失败致 silently 丢弃
    ("每股派人民币0.562元(相当于港币0.622)", 0.622),
    # 特别分红（实物派息换算）
    ("特殊说明:每10股分派1股美团B类普通股股份(相当于每股派18.13港元)", 18.13),
    # 备用语法 "每股派 X.X 港元"
    ("每股派0.5港元", 0.5),
    # 异常 / 空 / 0
    ("", None),
    ("无可解析金额", None),
    (None, None),
])
def test_D1_parse_dividend_amount(scheme, expected):
    """金额解析覆盖港股各种派息文案格式"""
    got = parse_dividend_amount_hkd(scheme)
    if expected is None:
        assert got is None
    else:
        assert got == pytest.approx(expected, rel=1e-6)


# ---- D2: AKShare 数据归属（mock 数据，不打外网） ----

def test_D2_fy_attribution_mocked(monkeypatch):
    """
    验证：用模拟的 AKShare 派息表（含「财政年度」），9616 类延迟派息的归属正确。

    模拟场景：
        fy=2025: 0.28（2026-08-31 除净，未除）
        fy=2024: 0.388（2025-08-29 已除）— 业务上属于 2024 年报，但 yfinance 启发式会错归
        fy=2023: 0.366（2024-08-12 已除）
    期望：
        fy_div_per_share = 0.28（current_fy=2025-12-31）
        prev_year_div_per_share = 0.388
        two_years_ago_div_per_share = 0.366
    """
    import pandas as pd
    from collectors import hk_financial, hk_dividend_history

    fake_div_records = [
        {"ex_date": pd.Timestamp("2026-08-31"), "fiscal_year": 2025,
         "amount_hkd": 0.28, "distribution_type": "年度分配", "raw_scheme": "每股派港币0.28元"},
        {"ex_date": pd.Timestamp("2025-08-29"), "fiscal_year": 2024,
         "amount_hkd": 0.388, "distribution_type": "年度分配", "raw_scheme": "每股派港币0.388元"},
        {"ex_date": pd.Timestamp("2024-08-12"), "fiscal_year": 2023,
         "amount_hkd": 0.366, "distribution_type": "年度分配", "raw_scheme": "每股派港币0.366元"},
        {"ex_date": pd.Timestamp("2023-08-07"), "fiscal_year": 2022,
         "amount_hkd": 0.165, "distribution_type": "年度分配", "raw_scheme": "每股派港币0.165元"},
    ]

    monkeypatch.setattr(
        hk_dividend_history, "get_hk_dividends_with_fy_attribution",
        lambda code, **kw: fake_div_records,
    )
    # 确保 hk_financial 内部 import 也走到 mock
    monkeypatch.setattr(
        "collectors.hk_dividend_history.get_hk_dividends_with_fy_attribution",
        lambda code, **kw: fake_div_records,
    )

    # mock yfinance：财报截止日（年报锚点 12-31，及对应中报 6-30）
    fake_periods = [pd.Timestamp(y, m, d) for y, m, d in [
        (2026, 6, 30), (2025, 12, 31), (2025, 6, 30),
        (2024, 12, 31), (2024, 6, 30), (2023, 12, 31), (2023, 6, 30),
        (2022, 12, 31), (2022, 6, 30),
    ]]
    monkeypatch.setattr(
        hk_financial, "_get_fiscal_period_ends",
        lambda code: (fake_periods, (12, 31)),
    )

    # mock yfinance.Ticker.dividends（返回空，让 AKShare 数据接管）
    class FakeTicker:
        @property
        def dividends(self):
            return pd.Series(dtype=float)
        @property
        def fast_info(self):
            class FI: last_dividend_value = 0.0; last_dividend_date = None
            return FI()
    monkeypatch.setattr(hk_financial, "_get_ticker", lambda code: FakeTicker())

    r = hk_financial.get_fiscal_year_dividends("09616", forward_div_override=None,
                                               today=pd.Timestamp("2026-05-23"))

    assert r["current_fy_end"] == "2025-12-31"
    assert r["prev_fy_end"] == "2024-12-31"
    # 关键断言：fy=2025 仅有 0.28（pending，未除净）
    assert r["fy_div_per_share"] == pytest.approx(0.28, abs=1e-6)
    # fy=2024 派 0.388（已除）
    assert r["prev_year_div_per_share"] == pytest.approx(0.388, abs=1e-6)
    # fy=2023 派 0.366（已除）
    assert r["two_years_ago_div_per_share"] == pytest.approx(0.366, abs=1e-6)
    # forward 仍为 0.28（pending 部分）
    assert r["forward_div_per_share"] == pytest.approx(0.28, abs=1e-6)


# ---- D3: AKShare 失败时回退到 yfinance 启发式（不打外网，仅验证流程） ----

def test_D3_fallback_to_yfinance_heuristic(monkeypatch):
    """
    AKShare 返回空 -> 应回落到 yfinance 时间窗口启发式（旧路径）。
    验证：流程不崩溃，并能根据窗口归属。
    """
    import pandas as pd
    from collectors import hk_financial, hk_dividend_history

    monkeypatch.setattr(
        hk_dividend_history, "get_hk_dividends_with_fy_attribution",
        lambda code, **kw: [],
    )
    monkeypatch.setattr(
        "collectors.hk_dividend_history.get_hk_dividends_with_fy_attribution",
        lambda code, **kw: [],
    )

    # 年报 + 中报锚点（TTM 窗口 = 最新两期，需中报期才能区分 fy / prev_fy）
    fake_periods = [pd.Timestamp("2025-12-31"), pd.Timestamp("2025-06-30"),
                    pd.Timestamp("2024-12-31"), pd.Timestamp("2024-06-30")]
    monkeypatch.setattr(
        hk_financial, "_get_fiscal_period_ends",
        lambda code: (fake_periods, (12, 31)),
    )

    # yfinance 派息：仅 2025-03-15 派 0.5（在 (2024-12-31, 2025-08-31] 窗口内 -> 归 2024 年报）
    fake_divs = pd.Series(
        [0.5],
        index=pd.DatetimeIndex([pd.Timestamp("2025-03-15")], name="Date"),
    )
    class FakeTicker:
        @property
        def dividends(self):
            return fake_divs
        @property
        def fast_info(self):
            class FI: last_dividend_value = 0.0; last_dividend_date = None
            return FI()
    monkeypatch.setattr(hk_financial, "_get_ticker", lambda code: FakeTicker())

    r = hk_financial.get_fiscal_year_dividends("test", forward_div_override=None,
                                               today=pd.Timestamp("2026-05-23"))
    # 启发式将 0.5 归到 prev_fy（2024-12-31）
    assert r["prev_year_div_per_share"] == pytest.approx(0.5, abs=1e-6)
    assert r["fy_div_per_share"] == pytest.approx(0.0, abs=1e-6)


# ---- D4: ex_date == NaT 的「未表决预案」必须从 fy_div / forward_div 中剔除 ----

def test_D4_skip_unscheduled_special_dividend(monkeypatch):
    """
    验证：AKShare 派息表中 ex_date == NaT 的派息（董事会建议但股东大会未通过）
    必须被剔除，不能进入 fy_div 或 forward_div。

    模拟场景（02355 宝业集团 2025）：
        fy=2025 特别分配 amount=5.5 ex_date=NaT      ← 未表决预案，应剔除
        fy=2025 年度分配 amount=0.096 ex_date=2026-06-25  ← 已通过，应保留
        fy=2024 年度分配 amount=0.13  ex_date=2025-06-18  ← 已除净，归 prev
    期望：
        fy_div_per_share = 0.096（仅保留有除净日的）
        prev_year_div_per_share = 0.13
        forward_div_per_share = 0.096（2026-06-25 > today=2026-05-23 仍是未来）
        ✗ 不应该等于 5.596（5.5 + 0.096）
    """
    import pandas as pd
    from collectors import hk_financial, hk_dividend_history

    fake_div_records = [
        {"ex_date": pd.NaT, "fiscal_year": 2025,
         "amount_hkd": 5.5, "distribution_type": "特别分配", "raw_scheme": "每股派港币5.5元"},
        {"ex_date": pd.Timestamp("2026-06-25"), "fiscal_year": 2025,
         "amount_hkd": 0.096, "distribution_type": "年度分配",
         "raw_scheme": "每股派人民币0.085元(相当于港币0.096元)"},
        {"ex_date": pd.Timestamp("2025-06-18"), "fiscal_year": 2024,
         "amount_hkd": 0.13, "distribution_type": "年度分配",
         "raw_scheme": "每股派人民币0.12元(相当于港币0.13元)"},
    ]

    monkeypatch.setattr(
        hk_dividend_history, "get_hk_dividends_with_fy_attribution",
        lambda code, **kw: fake_div_records,
    )
    monkeypatch.setattr(
        "collectors.hk_dividend_history.get_hk_dividends_with_fy_attribution",
        lambda code, **kw: fake_div_records,
    )

    # mock 财报锚点（年报 12-31 + 中报 6-30）
    fake_periods = [pd.Timestamp(y, m, d) for y, m, d in [
        (2026, 6, 30), (2025, 12, 31), (2025, 6, 30),
        (2024, 12, 31), (2024, 6, 30), (2023, 12, 31),
    ]]
    monkeypatch.setattr(
        hk_financial, "_get_fiscal_period_ends",
        lambda code: (fake_periods, (12, 31)),
    )

    class FakeTicker:
        @property
        def dividends(self):
            return pd.Series(dtype=float)
        @property
        def fast_info(self):
            class FI: last_dividend_value = 0.0; last_dividend_date = None
            return FI()
    monkeypatch.setattr(hk_financial, "_get_ticker", lambda code: FakeTicker())

    r = hk_financial.get_fiscal_year_dividends("02355", forward_div_override=None,
                                               today=pd.Timestamp("2026-05-23"))

    # 关键断言：5.5 港元的特别分配（未表决预案）必须不计入
    assert r["fy_div_per_share"] != pytest.approx(5.596, abs=1e-3), \
        "未表决预案（ex_date=NaT）不应被计入 fy_div"
    assert r["fy_div_per_share"] != pytest.approx(5.5, abs=1e-3)

    # fy=2025 仅保留 0.096（有 ex_date 且未除）
    assert r["fy_div_per_share"] == pytest.approx(0.096, abs=1e-3)
    # forward 同样剔除 NaT，仅 0.096
    assert r["forward_div_per_share"] == pytest.approx(0.096, abs=1e-3)
    # prev_year 不受影响
    assert r["prev_year_div_per_share"] == pytest.approx(0.13, abs=1e-3)


# ---- D7: forward_override 双计 bug 修复（refresh_dividend_only.py）----

def test_D7_forward_override_no_double_count(monkeypatch):
    """
    验证：当 AKShare 派息表已经收录了某笔派息（已除净），
    HKEX 抓到的 forward_override 又指向同一笔派息时，
    不能再次注入到 fy_div，避免双计。

    模拟场景（00255 龙记集团 2026-05-23）：
        fy=2025 中期 0.08 ex_date=2025-09-05 (已除)
        fy=2025 年度 0.12 ex_date=2026-05-22 (已除，just yesterday)
        HKEX forward_override = 0.12（2026-05-14 抓的，那时还没除）

    期望：
        fy_div = 0.20 (= 0.08 + 0.12)
        ✗ 不应该 = 0.32 (= 0.20 + 0.12 双计)
    """
    import pandas as pd
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from scripts.refresh_dividend_only import recompute_dividends_for_code
    from collectors import hk_dividend_history

    today = pd.Timestamp("2026-05-23")
    fake_records = [
        {"ex_date": pd.Timestamp("2025-09-05"), "fiscal_year": 2025,
         "amount_hkd": 0.08, "distribution_type": "中期分配", "raw_scheme": "每股派港币0.08元"},
        {"ex_date": today - pd.Timedelta(days=1), "fiscal_year": 2025,
         "amount_hkd": 0.12, "distribution_type": "年度分配", "raw_scheme": "每股派港币0.12元"},
        {"ex_date": pd.Timestamp("2025-05-23"), "fiscal_year": 2024,
         "amount_hkd": 0.06, "distribution_type": "年度分配", "raw_scheme": "每股派港币0.06元"},
        {"ex_date": pd.Timestamp("2024-09-09"), "fiscal_year": 2024,
         "amount_hkd": 0.05, "distribution_type": "中期分配", "raw_scheme": "每股派港币0.05元"},
    ]

    monkeypatch.setattr(
        hk_dividend_history, "get_hk_dividends_with_fy_attribution",
        lambda code, **kw: fake_records,
    )
    monkeypatch.setattr(
        "scripts.refresh_dividend_only.get_hk_dividends_with_fy_attribution",
        lambda code, **kw: fake_records,
    )

    row = pd.Series({
        "code": "00255",
        "current_fy_end": "2025-12-31",
        "prev_fy_end": "2024-12-31",
        "two_yrs_ago_fy_end": "2023-12-31",
    })

    # 关键：传入 forward_override = 0.12（与年度派息金额相同）
    out = recompute_dividends_for_code("00255", row, forward_override=0.12, today=today)

    assert out["fy_div_per_share"] == pytest.approx(0.20, abs=1e-3), \
        f"fy_div 应该 = 0.20，但 = {out['fy_div_per_share']}（疑似双计）"
    assert out["fy_div_per_share"] != pytest.approx(0.32, abs=1e-3), \
        "明确不应该是 0.32（0.20 + 0.12 双计）"
    assert out["prev_year_div_per_share"] == pytest.approx(0.11, abs=1e-3)


def test_D7b_forward_override_fallback_when_akshare_missing(monkeypatch):
    """
    验证：当 AKShare 没有当年派息记录、HKEX forward_override 是唯一来源时，
    应该正确兜底注入。
    """
    import pandas as pd
    from scripts.refresh_dividend_only import recompute_dividends_for_code
    from collectors import hk_dividend_history

    fake_records = [
        # AKShare 当年（2025）完全没有派息
        {"ex_date": pd.Timestamp("2025-05-23"), "fiscal_year": 2024,
         "amount_hkd": 0.06, "distribution_type": "年度分配", "raw_scheme": "每股派港币0.06元"},
    ]
    monkeypatch.setattr(
        hk_dividend_history, "get_hk_dividends_with_fy_attribution",
        lambda code, **kw: fake_records,
    )
    monkeypatch.setattr(
        "scripts.refresh_dividend_only.get_hk_dividends_with_fy_attribution",
        lambda code, **kw: fake_records,
    )

    row = pd.Series({
        "code": "00255",
        "current_fy_end": "2025-12-31",
        "prev_fy_end": "2024-12-31",
        "two_yrs_ago_fy_end": "2023-12-31",
    })

    # HKEX 抓到了 0.20，AKShare 完全没有
    out = recompute_dividends_for_code("00255", row, forward_override=0.20)

    # 应当兜底注入
    assert out["fy_div_per_share"] == pytest.approx(0.20, abs=1e-3), \
        f"AKShare 缺失时应兜底注入 forward_override=0.20，但 = {out['fy_div_per_share']}"
    assert out["forward_div_per_share"] == pytest.approx(0.20, abs=1e-3)


# ===================== C1 重构后新增 D8-D11 =====================
# D8-D11 验证 engine.dividend_attribution.compute_dividend_fields 作为单一权威实现的正确性


def test_D8_compute_dividend_fields_basic():
    """
    D8: compute_dividend_fields 单元测试 — 基本归属

    场景：典型港股年报+中报派息（2024 年报 + 2025 中报）
    """
    import pandas as pd
    from engine.dividend_attribution import DividendRecord, compute_dividend_fields

    today = pd.Timestamp("2026-01-15")  # 2025 年报已结束
    records = [
        # fy=2024 年报（2025-06 派发，已除）
        DividendRecord(ex_date=pd.Timestamp("2025-06-05"), amount_hkd=0.20,
                       fiscal_year=2024, source="akshare"),
        # fy=2024 中报（2024-09 派发，已除）
        DividendRecord(ex_date=pd.Timestamp("2024-09-09"), amount_hkd=0.10,
                       fiscal_year=2024, source="akshare"),
        # fy=2025 中报（2025-09 派发，已除）
        DividendRecord(ex_date=pd.Timestamp("2025-09-15"), amount_hkd=0.12,
                       fiscal_year=2025, source="akshare"),
    ]
    periods = [
        pd.Timestamp("2025-12-31"),  # current FY
        pd.Timestamp("2025-06-30"),  # current 中报
        pd.Timestamp("2024-12-31"),  # prev FY
        pd.Timestamp("2024-06-30"),  # prev 中报
        pd.Timestamp("2023-12-31"),
    ]
    out = compute_dividend_fields(records, periods, fy_anchor=(12, 31), today=today)

    # current_fy = 2025-12-31，覆盖 (2024-12-31, 2025-12-31] 的中报 = 0.12
    assert out["fy_div_per_share"] == pytest.approx(0.12, abs=1e-6)
    # prev fy = 2024-12-31，覆盖 (2023-12-31, 2024-12-31] 的中报 0.10 + 年报 0.20 = 0.30
    assert out["prev_year_div_per_share"] == pytest.approx(0.30, abs=1e-6)
    assert out["latest_attributed_fy"] == 2025


def test_D9_compute_dividend_fields_skip_NaT():
    """
    D9: R1 规则 — ex_date == NaT 应被跳过

    场景：02355 宝业集团 5.5 港元特别分红预案，未排定除净日。
    """
    import pandas as pd
    from engine.dividend_attribution import DividendRecord, compute_dividend_fields

    today = pd.Timestamp("2026-01-15")
    records = [
        # 正常派息（2024 年报）
        DividendRecord(ex_date=pd.Timestamp("2025-06-05"), amount_hkd=0.20,
                       fiscal_year=2024, source="akshare"),
        # 异常预案（NaT，未表决）
        DividendRecord(ex_date=pd.NaT, amount_hkd=5.5,
                       fiscal_year=2025, distribution_type="特别分配", source="akshare"),
    ]
    periods = [
        pd.Timestamp("2025-12-31"),
        pd.Timestamp("2025-06-30"),
        pd.Timestamp("2024-12-31"),
        pd.Timestamp("2024-06-30"),
    ]
    out = compute_dividend_fields(records, periods, fy_anchor=(12, 31), today=today)

    # 5.5 港元应被丢弃；2024 fy 的 0.20 归属到 2024-12-31
    # current_fy = 2025-12-31（覆盖区间 2024-12-31 < p <= 2025-12-31，无派息）
    assert out["fy_div_per_share"] == pytest.approx(0.0, abs=1e-6)
    # prev_fy = 2024-12-31，覆盖 (2023-12-31, 2024-12-31]，含 2024-12-31 = 0.20
    assert out["prev_year_div_per_share"] == pytest.approx(0.20, abs=1e-6)
    assert out["forward_div_per_share"] == pytest.approx(0.0, abs=1e-6)


def test_D10_compute_dividend_fields_override_no_double_count():
    """
    D10: R4 规则 — forward_override 与 fy_total 比较去重

    场景：00255 龙记 — AKShare 已收录 0.12 年度分配，HKEX forward 抓到 0.12。
    期望：fy_div = 0.20（0.08 中期 + 0.12 年度），不应 = 0.32（双计）
    """
    import pandas as pd
    from engine.dividend_attribution import DividendRecord, compute_dividend_fields

    today = pd.Timestamp("2026-05-23")
    records = [
        # fy=2025 中期（已除）
        DividendRecord(ex_date=pd.Timestamp("2025-09-05"), amount_hkd=0.08,
                       fiscal_year=2025, source="akshare"),
        # fy=2025 年度（昨天刚除）
        DividendRecord(ex_date=today - pd.Timedelta(days=1), amount_hkd=0.12,
                       fiscal_year=2025, source="akshare"),
    ]
    periods = [
        pd.Timestamp("2025-12-31"),
        pd.Timestamp("2025-06-30"),
        pd.Timestamp("2024-12-31"),
        pd.Timestamp("2024-06-30"),
    ]
    out = compute_dividend_fields(records, periods, fy_anchor=(12, 31), today=today,
                                   forward_override=0.12)

    # 不应双计
    assert out["fy_div_per_share"] == pytest.approx(0.20, abs=1e-6), \
        f"fy_div 应 = 0.20，但 = {out['fy_div_per_share']}（疑似双计）"


def test_D11_compute_dividend_fields_override_fill_when_missing():
    """
    D11: R4 规则 — AKShare 缺失时 forward_override 兜底注入

    场景：HKEX 比 AKShare 更早抓到分红，AKShare 当年完全没有记录。
    期望：fy_div_per_share = override = 0.20
    """
    import pandas as pd
    from engine.dividend_attribution import DividendRecord, compute_dividend_fields

    today = pd.Timestamp("2025-09-15")
    records = [
        # 上一年的（2024 年报，2025-05 派）
        DividendRecord(ex_date=pd.Timestamp("2025-05-23"), amount_hkd=0.06,
                       fiscal_year=2024, source="akshare"),
    ]
    periods = [
        pd.Timestamp("2025-12-31"),
        pd.Timestamp("2025-06-30"),
        pd.Timestamp("2024-12-31"),
        pd.Timestamp("2024-06-30"),
    ]
    out = compute_dividend_fields(records, periods, fy_anchor=(12, 31), today=today,
                                   forward_override=0.20)

    # current_fy = 2024-12-31（最近一个 <= today 的年报锚点）
    # AKShare 已有 0.06 在 fy=2024 → 归属到 2024-12-31
    # override = 0.20 > 0.06 → R4 补差 0.14 到 pending
    # fy_div = 0.06 + 0.14 = 0.20
    assert out["fy_div_per_share"] == pytest.approx(0.20, abs=1e-6)
    assert out["forward_div_per_share"] == pytest.approx(0.20, abs=1e-6)
    # prev_fy = 2023-12-31，无派息
    assert out["prev_year_div_per_share"] == pytest.approx(0.0, abs=1e-6)


def test_D11b_override_already_pending_in_other_period_no_double_count():
    """
    B54: R4 的 override 去重基准必须覆盖**全部期**的 pending，不能只看当前财年期。

    真实场景（06181 太古，2026-10-05）：
        2026-11-27 中期 20.811438（未除息）→ AKShare 已收录，归到 2026-06-30 中报期
        HKEX/东财 forward_override = 20.811438  ← 同一笔
        current_fy_year_end = 2025-12-31（该期无 pending）

    旧实现只比 current_fy_year_end 的 done+pending，看不见 2026-06-30 上的 pending
    → 误判「AKShare 缺这笔」→ 再注入一次 → fy_div 从 34.46 虚高到 41.62（恰好多 20.81）。
    """
    from engine.dividend_attribution import DividendRecord, compute_dividend_fields

    today = pd.Timestamp("2026-10-05")
    records = [
        DividendRecord(ex_date=pd.Timestamp("2026-11-27"), amount_hkd=20.811438,
                       fiscal_year=2026, distribution_type="中期分配", source="akshare"),
        DividendRecord(ex_date=pd.Timestamp("2026-05-12"), amount_hkd=13.65,
                       fiscal_year=2025, distribution_type="年度分配", source="akshare"),
        DividendRecord(ex_date=pd.Timestamp("2025-11-20"), amount_hkd=10.52,
                       fiscal_year=2025, distribution_type="中期分配", source="akshare"),
        DividendRecord(ex_date=pd.Timestamp("2025-05-22"), amount_hkd=6.88,
                       fiscal_year=2024, distribution_type="年度分配", source="akshare"),
    ]
    periods = [pd.Timestamp(s) for s in (
        "2026-12-31", "2026-06-30", "2025-12-31", "2025-06-30",
        "2024-12-31", "2024-06-30",
    )]

    baseline = compute_dividend_fields(records, periods, fy_anchor=(12, 31), today=today)
    out = compute_dividend_fields(records, periods, fy_anchor=(12, 31), today=today,
                                   forward_override=20.811438)

    # override 指向 AKShare 已收录的那笔 → fy_div 不得变化
    assert out["fy_div_per_share"] == pytest.approx(baseline["fy_div_per_share"], abs=1e-6), \
        f"override 重复注入导致 fy_div 虚高：{baseline['fy_div_per_share']} → {out['fy_div_per_share']}"
    assert out["fy_div_per_share"] == pytest.approx(34.461438, abs=1e-6)
    assert out["forward_div_per_share"] == pytest.approx(20.811438, abs=1e-6)



# =====================================================================
# B5: 中报期闰年漂移 bug
# =====================================================================

def test_D12_interim_md_match_for_aug_fy_anchor_in_non_leap_year():
    """
    回归 B5 bug：fy_anchor=(8,31)，平年中报期 (2,28) 应被保留为合法 period。
    旧实现用 2024 闰年算 interim_md=(2,29)，会把所有平年 (2,28) 的中报期错误过滤。

    B11 更新：补充 distribution_type 字段（"中期分配"/"年度分配"），
    并验证中期派息被正确归到对应的中报期：
      - 2026-03-06 中期 0.07 → 2026-02-28（latest_period_div=0.07）
      - 2024-12-06 中期 0.07 → 2025-02-28（latest_period_yoy_div=0.07）
    """
    from engine.dividend_attribution import DividendRecord, compute_dividend_fields

    today = pd.Timestamp("2026-05-24")
    # 模拟 01890 中国科培：fy=8/31
    records = [
        # 上一财年（FY2025=2024-09~2025-08）的中期 + 年度派息
        DividendRecord(ex_date=pd.Timestamp("2024-12-06"), amount_hkd=0.07,
                       fiscal_year=2024, distribution_type="中期分配", source="akshare"),
        DividendRecord(ex_date=pd.Timestamp("2025-08-01"), amount_hkd=0.06,
                       fiscal_year=2024, distribution_type="年度分配", source="akshare"),
        # 当前财年（FY2026=2025-09~2026-08）已宣派的中期 + 待除净的年度
        DividendRecord(ex_date=pd.Timestamp("2026-03-06"), amount_hkd=0.07,
                       fiscal_year=2025, distribution_type="中期分配", source="akshare"),
        DividendRecord(ex_date=pd.Timestamp("2026-07-31"), amount_hkd=0.06,
                       fiscal_year=2025, distribution_type="年度分配", source="akshare"),
    ]
    periods = [
        pd.Timestamp("2026-08-31"),
        pd.Timestamp("2026-02-28"),  # ← 关键：平年中报期 (2, 28)
        pd.Timestamp("2025-08-31"),
        pd.Timestamp("2025-02-28"),
        pd.Timestamp("2024-08-31"),
        pd.Timestamp("2024-02-29"),  # 闰年中报期 (2, 29)
    ]
    out = compute_dividend_fields(records, periods, fy_anchor=(8, 31), today=today,
                                   forward_override=None)

    # B50 修正 B39-r8：fiscal_year 映射需经 ex_date 裁决
    #   2024-12-06 fy=2024 中期 → 2024-02-29(+281) vs 2025-02-28(-84) → 选 2025-02-28
    #   2026-03-06 fy=2025 中期 → 2025-02-28(+371 超窗) vs 2026-02-28(+6) → 选 2026-02-28
    #   2025-08-01 fy=2024 年度 → 2024-08-31(+335) vs 2025-08-31(-30 年度不可提前) → 2024-08-31
    #   2026-07-31 fy=2025 年度 → 2025-08-31(+334) → 2025-08-31
    # today=2026-05-24: past_periods[0]=2026-02-28 有 0.07 → 不触发 B38 shift
    # 这与本测试 docstring 开头声明的预期一致（B39-r8 曾改断言但未同步 docstring）
    assert out["latest_period_end"] == "2026-02-28"
    assert out["latest_period_div_per_share"] == pytest.approx(0.07, abs=1e-6)
    # 同期上年 2025-02-28 ← 2024-12-06 中期 0.07
    assert out["latest_period_yoy_div_per_share"] == pytest.approx(0.07, abs=1e-6)
    # current_fy_end = 2025-08-31（fy_anchor 决定，不受归属影响）
    assert out["current_fy_end"] == "2025-08-31"
    # TTM = [2026-02-28, 2025-08-31] = 0.07 + 0.06 = 0.13
    assert out["fy_div_per_share"] == pytest.approx(0.13, abs=1e-6)
    # forward_div = 未来期 pending 0.06
    assert out["forward_div_per_share"] == pytest.approx(0.06, abs=1e-6)
    assert "2025-08-31" in out["fiscal_periods"]
    assert "2025-02-28" in out["fiscal_periods"]


def test_D13_feb_fy_anchor_with_leap_year_period():
    """
    fy_anchor=(2,28)：财年截止 2 月底，闰年财报期会变为 (2, 29)。
    验证 month-day 月末容差：闰年 (2,29) 与平年 (2,28) 都应被识别为合法年报锚点。
    """
    from engine.dividend_attribution import DividendRecord, compute_dividend_fields

    today = pd.Timestamp("2025-06-15")
    records = []  # 不关心派息，只验证锚点识别
    periods = [
        pd.Timestamp("2026-02-28"),  # 平年（>today，不计入）
        pd.Timestamp("2025-08-31"),  # 中报（平年，月末容差匹配 (8,31)）
        pd.Timestamp("2025-02-28"),  # 平年年报锚点
        pd.Timestamp("2024-08-31"),  # 中报
        pd.Timestamp("2024-02-29"),  # 闰年年报锚点
    ]
    out = compute_dividend_fields(records, periods, fy_anchor=(2, 28), today=today,
                                   forward_override=None)

    # 关键：闰年/平年的年报锚点都应被识别（month-day 容差）
    # current_fy_end = 2025-02-28（最新 ≤ today=2025-06-15 的年报锚点）
    assert out["current_fy_end"] == "2025-02-28"
    # prev_fy_end = 2024-02-29（闰年也合法，B5 月末容差关键）
    assert out["prev_fy_end"] == "2024-02-29"
    # latest_period 应能含中报期（不应被错误过滤）
    # 2025-06-15 之前最近一期 = 2025-02-28（年报）；2024-08-31 中报也保留
    assert out["latest_period_end"] == "2025-02-28"
