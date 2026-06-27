"""
B10 单元测试：has_share_dilution 噪声鲁棒性

验证修复后的算法对以下 case 的判定：
1. 上行点只有 1 个 0.10 → False（单点噪声，不能采信）
2. 上行点有 2 个 0.10 → True（多点持续增发）
3. 全是 <0.005 的微抖动 → False（EPSILON 过滤）
4. 下行点只有 1 个 -0.60 → False（单点合股可疑，不采信）
5. 实际持续合股（2 个 -0.55）→ True
"""
import pandas as pd
import numpy as np
import pytest
from unittest.mock import patch, MagicMock


def _build_shares_series(monthly_vals: list[float], start: str = "2024-01-31") -> pd.Series:
    """构造月末股份数 series。"""
    idx = pd.date_range(start=start, periods=len(monthly_vals), freq="ME")
    return pd.Series(monthly_vals, index=idx, dtype=float)


def _patch_get_shares(monkeypatch, shares_series: pd.Series):
    """patch _get_ticker().get_shares_full 返回指定 series。"""
    from collectors import hk_financial

    fake_ticker = MagicMock()
    fake_ticker.get_shares_full = MagicMock(return_value=shares_series)
    monkeypatch.setattr(hk_financial, "_get_ticker", lambda code: fake_ticker)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    """关掉测试里的 sleep 提速。"""
    import time as _t
    monkeypatch.setattr(_t, "sleep", lambda *_a, **_k: None)


# 构造满足 monthly>=13 + 排除最近 3 月后还有点 的最小数据集（24 个月起步保险）

def test_single_up_spike_should_not_filter(monkeypatch):
    """case1: 24 个月中只有 1 个月相对一年前 +10%，其余持平 → False"""
    # 12 个月 100，再 12 个月：第 1 个 110（YoY +10%），其余 100
    vals = [100.0] * 12 + [110.0] + [100.0] * 11
    _patch_get_shares(monkeypatch, _build_shares_series(vals, start="2023-01-31"))

    from collectors.hk_financial import has_share_dilution
    should_filter, _ = has_share_dilution("FAKE1", years=3)
    assert should_filter is False, "单点 +10% 应被视为噪声，不应过滤"


def test_two_up_spikes_should_filter(monkeypatch):
    """case2: 24 个月中有 2 个月相对一年前 +10%，应过滤"""
    # 12 个月 100，再 12 个月：第 1、第 2 个 110（连续两月 YoY +10%），其余 100
    vals = [100.0] * 12 + [110.0, 110.0] + [100.0] * 10
    _patch_get_shares(monkeypatch, _build_shares_series(vals, start="2023-01-31"))

    from collectors.hk_financial import has_share_dilution
    should_filter, _ = has_share_dilution("FAKE2", years=3)
    assert should_filter is True, "连续 2 月 +10% YoY 是真实增发，应过滤"


def test_micro_noise_below_epsilon_should_not_filter(monkeypatch):
    """case3: 全是 <0.5% 的微抖动 → False"""
    # 让 YoY 全在 ±0.3% 内：基线 100，逐月微抖
    rng = np.random.default_rng(42)
    base = [100.0 + rng.uniform(-0.3, 0.3) for _ in range(24)]
    _patch_get_shares(monkeypatch, _build_shares_series(base, start="2023-01-31"))

    from collectors.hk_financial import has_share_dilution
    should_filter, _ = has_share_dilution("FAKE3", years=3)
    assert should_filter is False, "<0.5% 微抖动应被 EPSILON 过滤掉"


def test_single_down_spike_should_not_filter(monkeypatch):
    """case4: 单点合股可疑（YoY -60%）应不采信"""
    # 12 个月 100，再 12 个月：第 1 个 40（YoY -60%），其余 100
    vals = [100.0] * 12 + [40.0] + [100.0] * 11
    _patch_get_shares(monkeypatch, _build_shares_series(vals, start="2023-01-31"))

    from collectors.hk_financial import has_share_dilution
    should_filter, _ = has_share_dilution("FAKE4", years=3)
    assert should_filter is False, "单点 -60% 应视为数据噪声，不应过滤"


def test_two_down_spikes_should_filter(monkeypatch):
    """case5: 真实合股（连续 2 个月 YoY -55%）→ True"""
    vals = [100.0] * 12 + [45.0, 45.0] + [45.0] * 10
    _patch_get_shares(monkeypatch, _build_shares_series(vals, start="2023-01-31"))

    from collectors.hk_financial import has_share_dilution
    should_filter, _ = has_share_dilution("FAKE5", years=3)
    assert should_filter is True, "连续 2 月 -55% YoY 是真实合股，应过滤"


def test_b10_01245_pattern_should_not_filter(monkeypatch):
    """B10 真实 case: 模拟 01245 实际 yfinance 数据形态——
    只有 2024-08 单月 +8.2% YoY，其余都接近 0 或微负，应不过滤"""
    # 模拟 24 个月：第 12 月开始稳定 100；第 13 月 +8.2%（一次抖动）；剩余回到 100；混入 -3% 几个月模拟另一向单点
    vals = (
        [100.0] * 12        # 2023-01 ~ 2023-12
        + [108.2]           # 2024-01: YoY +8.2%（对应 2023-01 的 100）—单点
        + [100.0] * 2       # 2024-02, 2024-03
        + [96.5]            # 2024-04: YoY -3.5%（对应 2023-04）
        + [100.0] * 2       # 2024-05, 2024-06
        + [96.3]            # 2024-07: YoY -3.7%
        + [100.0] * 4       # 2024-08 ~ 2024-11
    )
    _patch_get_shares(monkeypatch, _build_shares_series(vals, start="2023-01-31"))

    from collectors.hk_financial import has_share_dilution
    should_filter, _ = has_share_dilution("01245SIM", years=3)
    assert should_filter is False, "01245 真实形态：单点 +8.2% + 多个 -3% 噪声 → 不应过滤"
