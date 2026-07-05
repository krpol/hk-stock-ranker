"""B31 + B32: real_fcf 单测。"""
import pytest
from engine.real_fcf import (
    compute_real_fcf,
    compute_real_fcf_ttm,
    compute_drop_ratio,
    compute_coverage_ratio,
    classify_rating,
    evaluate,
    _downgrade,
)


# ────────────── compute_real_fcf ──────────────

def test_real_fcf_basic():
    # OCF 100, CapEx 30, D&A 50, Lease 10 → deduct = min(40, 50) = 40 → 60
    assert compute_real_fcf(100, 30, 50, 10) == 60


def test_real_fcf_dep_caps_deduction():
    # CapEx + Lease 大于 D&A → 用 D&A 上限
    # OCF 100, CapEx 80, D&A 30, Lease 10 → deduct = min(90, 30) = 30 → 70
    assert compute_real_fcf(100, 80, 30, 10) == 70


def test_real_fcf_no_dep():
    # D&A 缺失 → 不取 min
    assert compute_real_fcf(100, 30, None, 10) == 60


def test_real_fcf_none_ocf():
    assert compute_real_fcf(None, 10, 10, 10) is None


def test_real_fcf_none_optionals():
    # capex/lease 缺失按 0
    assert compute_real_fcf(100, None, 50, None) == 100


# ────────────── compute_coverage_ratio ──────────────

def test_coverage_basic():
    # real_fcf 100, ibd 1000, cash 200, equity 5000
    # 净债 = 800, ε = max(50, 1e8) = 1e8 → denom = max(800, 1e8) = 1e8
    # 数值过小被 ε 拉平
    r = compute_coverage_ratio(100, 1000, 200, 5000)
    assert r == pytest.approx(100 / 1e8)


def test_coverage_real_scale():
    # 用亿级数据
    real_fcf = 100e8
    ibd, cash, eq = 1000e8, 200e8, 5000e8
    # 净债 = 800 亿, ε = max(50 亿, 1 亿) = 50 亿 → denom = 800 亿
    r = compute_coverage_ratio(real_fcf, ibd, cash, eq)
    assert r == pytest.approx(100 / 800)


def test_coverage_netcash_uses_eps():
    # 净现金: ibd 100, cash 800 → 净债=-700
    # equity 5000 → ε = 50；max(-700, 50) = 50
    r = compute_coverage_ratio(100, 100, 800, 5000)
    # 测亿级: real 100亿, ibd 100亿, cash 800亿, eq 5000亿
    real_fcf = 100e8
    r2 = compute_coverage_ratio(real_fcf, 100e8, 800e8, 5000e8)
    assert r2 == pytest.approx(100 / 50)


def test_coverage_missing():
    assert compute_coverage_ratio(None, 100, 100, 100) is None
    assert compute_coverage_ratio(100, None, 100, 100) is None
    assert compute_coverage_ratio(100, 100, None, 100) is None


def test_coverage_zero_equity_uses_floor():
    # equity = 0 → ε = floor 1e8
    real_fcf = 50e8
    r = compute_coverage_ratio(real_fcf, 0, 0, 0)
    assert r == pytest.approx(50 / 1)  # 50e8 / 1e8


# ────────────── classify_rating ──────────────

def test_rating_aaa_overrides():
    assert classify_rating(-100, -0.50, cash_fortress=True) == "AAA"


def test_rating_drop_negative_fcf():
    assert classify_rating(-1, 0.50, False) == "DROP"


def test_rating_drop_low_coverage():
    assert classify_rating(10, 0.12, False) == "DROP"


def test_rating_warn():
    assert classify_rating(10, 0.17, False) == "WARN"


def test_rating_ok():
    # 20% ≤ 覆盖率 < 100% → OK
    assert classify_rating(10, 0.25, False) == "OK"
    assert classify_rating(10, 0.50, False) == "OK"


def test_rating_strong():
    # 覆盖率 ≥ 100% → STRONG
    assert classify_rating(10, 1.50, False) == "STRONG"


def test_rating_na_missing():
    assert classify_rating(None, 0.50, False) == "N/A"
    assert classify_rating(10, None, False) == "N/A"


# ────────────── evaluate (integration) ──────────────

def test_evaluate_nirku_like():
    # NIRAKU: OCF 66 亿, CapEx 21.5, D&A 41.9, Lease 28.1, IBD 11, Cash 8, Eq 214
    # real_fcf = 66 - min(21.5+28.1, 41.9) = 66 - 41.9 = 24.1 亿
    # 净债 = 3 亿, ε = max(2.14, 1) = 2.14 亿 → denom = max(3, 2.14) = 3 亿
    # 覆盖率 = 24.1/3 ≈ 8.03 (即 803%)
    r = evaluate(66e8, 21.5e8, 41.9e8, 28.1e8, 11e8, 8e8, 214e8)
    assert r["real_fcf"] == pytest.approx(24.1e8, rel=0.01)
    assert r["coverage_ratio"] > 1.0
    assert r["fcf_rating"] == "STRONG"


def test_evaluate_zhaojin_like():
    # 招金: real_fcf 6 亿, IBD 152, Cash 0, Eq 212
    # 净债 = 152 亿, ε = 2.12 亿 → denom = 152
    # 覆盖率 = 6/152 ≈ 0.04 → DROP
    r = evaluate(17e8, 19e8, 11e8, 19.2e8, 152e8, 0, 212e8)
    # real_fcf = 17 - min(19+19.2, 11) = 17 - 11 = 6
    assert r["real_fcf"] == pytest.approx(6e8, rel=0.01)
    assert r["coverage_ratio"] == pytest.approx(6/152, rel=0.02)
    assert r["fcf_rating"] == "DROP"


def test_evaluate_cash_fortress_overrides():
    # 即使 real_fcf 为负，cash_fortress=True 也给 AAA
    r = evaluate(-10e8, 0, 5e8, 0, 0, 0, 100e8, cash_fortress=True)
    assert r["fcf_rating"] == "AAA"


# ────────────── B32: TTM + drop_ratio + downgrade ──────────────

def test_compute_real_fcf_ttm():
    # OCF_TTM 100, CapEx_TTM 30, D&A_TTM 50, Lease_TTM 10 → deduct = min(40, 50) = 40 → 60
    assert compute_real_fcf_ttm(100, 30, 50, 10) == 60


def test_compute_real_fcf_ttm_none_ocf():
    assert compute_real_fcf_ttm(None, 10, 10, 10) is None


def test_drop_ratio_basic():
    # ttm 60, 3y 100 → 0.6
    assert compute_drop_ratio(60, 100) == pytest.approx(0.6)


def test_drop_ratio_negative_3y_returns_none():
    # 3y <= 0 → 不可对比，返回 None
    assert compute_drop_ratio(60, 0) is None
    assert compute_drop_ratio(60, -10) is None


def test_drop_ratio_negative_ttm():
    # ttm 转负 → 比率为负
    assert compute_drop_ratio(-30, 100) == pytest.approx(-0.3)


def test_drop_ratio_missing():
    assert compute_drop_ratio(None, 100) is None
    assert compute_drop_ratio(100, None) is None


def test_downgrade_steps():
    assert _downgrade("STRONG", 1) == "OK"
    assert _downgrade("STRONG", 2) == "WARN"
    assert _downgrade("OK", 1) == "WARN"
    assert _downgrade("OK", 2) == "DROP"
    assert _downgrade("WARN", 1) == "DROP"
    assert _downgrade("WARN", 2) == "DROP"
    assert _downgrade("DROP", 1) == "DROP"
    # AAA / N/A 不动
    assert _downgrade("AAA", 2) == "AAA"
    assert _downgrade("N/A", 2) == "N/A"


def test_rating_downgrade_warn_drop():
    # 原 STRONG（覆盖率 1.5），drop_ratio 0.2 (<0.3) → 下调一档 → OK
    assert classify_rating(10, 1.50, False, drop_ratio=0.2) == "OK"
    # drop_ratio -0.1 (转负) → 下调两档 → WARN
    assert classify_rating(10, 1.50, False, drop_ratio=-0.1) == "WARN"


def test_rating_downgrade_skip_aaa():
    # AAA 不被降级
    assert classify_rating(-100, -0.5, cash_fortress=True, drop_ratio=-1.0) == "AAA"


def test_rating_no_downgrade_when_drop_ratio_none():
    # drop_ratio=None 不降级
    assert classify_rating(10, 1.50, False, drop_ratio=None) == "STRONG"


def test_rating_downgrade_below_warn():
    # 原 WARN（覆盖率 0.07），drop_ratio < 0.3 → 下调一档 → DROP
    assert classify_rating(10, 0.17, False, drop_ratio=0.2) == "DROP"


def test_evaluate_with_ttm_drop():
    # 3y 强劲：OCF 100亿, CapEx 30亿, D&A 50亿, Lease 0 → real_fcf=70亿
    # 净债 = 100-50 = 50 亿, ε = max(50, 1)=50 → cov = 70/50 = 1.4 → STRONG
    # TTM 突降: OCF_TTM 100亿, CapEx_TTM 150亿 (激进投资), D&A_TTM 50亿, Lease 0
    # real_fcf_ttm = 100 - min(150, 50) = 100 - 50 = 50 → drop_ratio = 50/70 ≈ 0.71 → 不降级
    r = evaluate(100e8, 30e8, 50e8, 0,
                 ibd=100e8, cash=50e8, equity=1000e8,
                 ocf_ttm=100e8, capex_ttm=150e8, da_ttm=50e8, lease_ttm=0)
    assert r["drop_ratio"] == pytest.approx(50/70, rel=0.01)
    assert r["fcf_rating"] == "STRONG"


def test_evaluate_with_ttm_severe_drop():
    # 3y real_fcf 70亿, TTM 转负
    # OCF_TTM 0, CapEx_TTM 100亿, D&A 50 → ttm = 0 - 50 = -50 亿
    r = evaluate(100e8, 30e8, 50e8, 0,
                 ibd=100e8, cash=50e8, equity=1000e8,
                 ocf_ttm=0, capex_ttm=100e8, da_ttm=50e8, lease_ttm=0)
    assert r["drop_ratio"] < 0
    # 原 STRONG → 下调两档 → WARN
    assert r["fcf_rating"] == "WARN"


# ────── B43: 对称升级测试 ──────

def test_rating_upgrade_ttm_1_5x():
    """B43: TTM real_fcf 达到 3y avg 1.5x → 升一档"""
    from engine.real_fcf import classify_rating
    # base = WARN (cov 0.18), drop_ratio = 1.6 → 升一档 → OK
    r = classify_rating(real_fcf=50e8, coverage_ratio=0.18, drop_ratio=1.6)
    assert r == "OK"


def test_rating_upgrade_ttm_2_5x():
    """B43: TTM real_fcf 达到 3y avg 2.5x → 升两档"""
    from engine.real_fcf import classify_rating
    # base = WARN (cov 0.18), drop_ratio = 3.0 → 升两档 → STRONG
    r = classify_rating(real_fcf=50e8, coverage_ratio=0.18, drop_ratio=3.0)
    assert r == "STRONG"


def test_rating_upgrade_from_drop():
    """B43: base=DROP 可升级"""
    from engine.real_fcf import classify_rating
    # cov=0.10 → DROP; drop_ratio=1.6 → 升一档 → WARN
    r = classify_rating(real_fcf=10e8, coverage_ratio=0.10, drop_ratio=1.6)
    assert r == "WARN"


def test_rating_upgrade_skip_strong():
    """B43: STRONG 已在顶端，不再升"""
    from engine.real_fcf import classify_rating
    r = classify_rating(real_fcf=100e8, coverage_ratio=1.5, drop_ratio=3.0)
    assert r == "STRONG"


def test_rating_no_upgrade_normal_ratio():
    """B43: drop_ratio ∈ (0.3, 1.5) 不变化"""
    from engine.real_fcf import classify_rating
    # base=WARN, drop_ratio=1.0 → 无变化
    r = classify_rating(real_fcf=50e8, coverage_ratio=0.18, drop_ratio=1.0)
    assert r == "WARN"


def test_evaluate_hk_beikong_like():
    """B43: 03718 北控城市资源类 - TTM 大幅优于 3y avg"""
    from engine.real_fcf import evaluate
    # 3y: OCF=6亿 CapEx=2.83 D&A=5.58 net_int=0.85 → real_fcf ≈ 2.35
    # TTM: OCF=9亿 CapEx=2.83 D&A=5.12 net_int=0.65 → real_fcf_ttm ≈ 5.49
    # drop_ratio = 5.49/2.35 ≈ 2.34 → 落在 [1.5, 2.5) → 升一档
    r = evaluate(6.03e8, 2.83e8, 5.58e8, 0,
                 ibd=25e8, cash=13e8, equity=32e8,
                 ocf_ttm=8.97e8, capex_ttm=2.83e8, da_ttm=5.12e8, lease_ttm=0,
                 interest_paid_cff_3y=0.95e8, interest_received_inv_3y=0.10e8,
                 interest_paid_cff_ttm=0.69e8, interest_received_inv_ttm=0.04e8)
    assert 1.5 <= r["drop_ratio"] < 2.5
    # base WARN (cov ≈ 0.196), 升一档 → OK
    assert r["fcf_rating"] == "OK"
