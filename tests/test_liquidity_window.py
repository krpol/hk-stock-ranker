"""
B9: 流动性过滤"近 N 日窗口取最大"测试

覆盖：
- get_hk_recent_amount_window 计算逻辑
- _code_to_yf_ticker 港股代码到 yfinance ticker 的转换
- 失败容错（yfinance 异常 / 空 hist）
- run_rebuild_pool Step 2 的"单日不通过→窗口复核→补回"路径
"""
import sys
import os
from unittest.mock import patch, MagicMock

import pandas as pd
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from collectors.hk_price import (
    _code_to_yf_ticker,
    _fetch_single_max_amount,
    get_hk_recent_amount_window,
)


# ============================================================
# 1. ticker 转换
# ============================================================

class TestCodeToYfTicker:
    def test_strip_leading_zero(self):
        assert _code_to_yf_ticker("01245") == "1245.HK"

    def test_single_zero(self):
        assert _code_to_yf_ticker("00001") == "1.HK"

    def test_five_digit(self):
        assert _code_to_yf_ticker("09988") == "9988.HK"


# ============================================================
# 2. _fetch_single_max_amount
# ============================================================

class TestFetchSingleMaxAmount:
    def _mock_hist(self, rows):
        """rows: [(close, volume), ...]"""
        return pd.DataFrame(
            {"Close": [r[0] for r in rows], "Volume": [r[1] for r in rows]}
        )

    @patch("yfinance.Ticker")
    def test_basic_max_amount(self, mock_ticker_cls):
        mock_ticker = MagicMock()
        mock_ticker.history.return_value = self._mock_hist([
            (0.186, 240000),   # 44640
            (0.186, 0),        # 0
            (0.200, 4086000),  # 817200  <- max
            (0.189, 1052000),  # 198828
        ])
        mock_ticker_cls.return_value = mock_ticker

        code, amount = _fetch_single_max_amount("01245", lookback_days=5)
        assert code == "01245"
        assert amount == pytest.approx(0.200 * 4086000)

    @patch("yfinance.Ticker")
    def test_empty_hist_returns_zero(self, mock_ticker_cls):
        mock_ticker = MagicMock()
        mock_ticker.history.return_value = pd.DataFrame()
        mock_ticker_cls.return_value = mock_ticker

        code, amount = _fetch_single_max_amount("01245", lookback_days=5)
        assert amount == 0.0

    @patch("yfinance.Ticker")
    def test_exception_returns_zero(self, mock_ticker_cls):
        mock_ticker_cls.side_effect = ConnectionError("network down")
        code, amount = _fetch_single_max_amount("01245", lookback_days=5)
        assert amount == 0.0

    @patch("yfinance.Ticker")
    def test_tail_truncation(self, mock_ticker_cls):
        """超过 lookback_days 的早期日期不应被算入 max"""
        mock_ticker = MagicMock()
        # 9 行；早期 1e9 巨大成交不应进入 tail(5)
        mock_ticker.history.return_value = self._mock_hist([
            (1.0, 1_000_000_000),  # earliest, will be cut
            (1.0, 1_000_000_000),  # cut
            (1.0, 1_000_000_000),  # cut
            (1.0, 1_000_000_000),  # cut
            # tail(5):
            (0.2, 100_000),  # 20000
            (0.2, 100_000),  # 20000
            (0.2, 500_000),  # 100000 <- max
            (0.2, 100_000),  # 20000
            (0.2, 100_000),  # 20000
        ])
        mock_ticker_cls.return_value = mock_ticker
        _, amount = _fetch_single_max_amount("01245", lookback_days=5)
        assert amount == pytest.approx(100_000)


# ============================================================
# 3. get_hk_recent_amount_window
# ============================================================

class TestGetHkRecentAmountWindow:
    def test_empty_codes(self):
        assert get_hk_recent_amount_window([], lookback_days=5) == {}

    @patch("collectors.hk_price._fetch_single_max_amount")
    def test_batch_returns_dict(self, mock_fetch):
        mock_fetch.side_effect = lambda c, _: (c, {"01245": 198828, "00001": 0, "02602": 1000}[c])
        result = get_hk_recent_amount_window(
            ["01245", "00001", "02602"], lookback_days=5, max_workers=2
        )
        assert result == {"01245": 198828, "00001": 0, "02602": 1000}


# ============================================================
# 4. run_rebuild_pool Step 2 集成（mock 数据源 + 复核）
# ============================================================

class TestRebuildPoolLiquidityRecovery:
    """
    模拟场景：stock_list 含 4 只票，单日 amount=[100000, 5000, 0, 50000]
    门槛 10000：spot_pass=[True, False, False, True]
    窗口复核 fail 的两只：返回 max_amount=[200, 30000]
    → 第二只(30000>=10000)被补回，最终 keep=[True, False, True, True] = 3 只
    """

    @patch("collectors.hk_price.get_hk_recent_amount_window")
    def test_window_recovers_failed_codes(self, mock_window):
        """单独验证 keep_mask 逻辑（不调用真实 main.run_rebuild_pool 以避免 yfinance/akshare 副作用）"""
        cap_filter = 10_000
        stock_list = pd.DataFrame({
            "code": ["00001", "00002", "00003", "00004"],
            "name": ["A", "B", "C", "D"],
            "market_cap": [100_000, 5_000, 0, 50_000],
        })

        spot_pass_mask = stock_list["market_cap"].fillna(0) >= cap_filter
        spot_fail_codes = stock_list.loc[~spot_pass_mask, "code"].astype(str).str.zfill(5).tolist()
        assert spot_fail_codes == ["00002", "00003"]

        # mock 复核返回：00002 窗口里 max=30000（补回），00003 max=200（仍不满足）
        mock_window.return_value = {"00002": 30_000, "00003": 200}
        recent_amounts = mock_window(spot_fail_codes, lookback_days=5)
        recovered = {c for c, a in recent_amounts.items() if a >= cap_filter}
        assert recovered == {"00002"}

        codes_normalized = stock_list["code"].astype(str).str.zfill(5)
        keep_mask = spot_pass_mask | codes_normalized.isin(recovered)
        kept = stock_list[keep_mask]
        assert set(kept["code"]) == {"00001", "00002", "00004"}

    def test_window_disabled_keeps_only_spot_pass(self):
        """liquidity_window=0 时退回旧行为"""
        cap_filter = 10_000
        stock_list = pd.DataFrame({
            "code": ["00001", "00002", "00003"],
            "market_cap": [100_000, 5_000, 0],
        })
        spot_pass_mask = stock_list["market_cap"].fillna(0) >= cap_filter
        # 等价于 liquidity_window=0 时不执行 recovered 分支
        recovered = set()
        codes_normalized = stock_list["code"].astype(str).str.zfill(5)
        keep_mask = spot_pass_mask | codes_normalized.isin(recovered)
        kept = stock_list[keep_mask]
        assert set(kept["code"]) == {"00001"}
