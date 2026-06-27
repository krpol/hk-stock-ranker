"""
tests/test_dividend_parse.py

Unit tests for collectors.hk_dividend_history.parse_dividend_amount_hkd

Covers:
  - B7: AKShare 数据中「港币 X」后缺「元」字的解析修复（02602 万物云案例）
  - 各种正常格式
  - 边界/无效输入
"""

import pytest
from collectors.hk_dividend_history import parse_dividend_amount_hkd


# ---------------------------------------------------------------------------
# B7 regression: 缺「元」字
# ---------------------------------------------------------------------------

class TestB7MissingYuanSuffix:
    """02602 万物云 2024 中期派息方案：「每股派人民币0.562元(相当于港币0.622)」
    AKShare 原文末尾无「元」字，B7 修复前被 silently 丢弃。"""

    def test_rmb_to_hkd_without_yuan_suffix(self):
        """核心回归：「相当于港币X.X」无「元」后缀 → 正常解析"""
        result = parse_dividend_amount_hkd("每股派人民币0.562元(相当于港币0.622)")
        assert result == pytest.approx(0.622, abs=1e-6), (
            "B7 regression: 缺「元」后缀应能正常解析（02602 万物云 2024 中期）"
        )

    def test_rmb_to_hkd_with_yuan_suffix(self):
        """有「元」后缀的正常写法应照常解析"""
        result = parse_dividend_amount_hkd("每股派人民币0.562元(相当于港币0.622元)")
        assert result == pytest.approx(0.622, abs=1e-6)

    def test_rmb_to_hkd_without_yuan_suffix_larger(self):
        """另一种无「元」写法：更大数值"""
        result = parse_dividend_amount_hkd("每股派人民币1.234元(相当于港币1.366)")
        assert result == pytest.approx(1.366, abs=1e-6)


# ---------------------------------------------------------------------------
# 标准格式（确保改正则后未破坏原有解析）
# ---------------------------------------------------------------------------

class TestStandardFormats:
    def test_hkd_direct_yuan(self):
        """每股派港币5.3元"""
        assert parse_dividend_amount_hkd("每股派港币5.3元") == pytest.approx(5.3, abs=1e-6)

    def test_hkd_direct_no_yuan(self):
        """每股派港币0.388（无元后缀）"""
        assert parse_dividend_amount_hkd("每股派港币0.388") == pytest.approx(0.388, abs=1e-6)

    def test_hkd_per_share_港元(self):
        """每股派0.388港元"""
        assert parse_dividend_amount_hkd("每股派0.388港元") == pytest.approx(0.388, abs=1e-6)

    def test_usd_to_hkd_with_yuan(self):
        """每股派美元0.45元(相当于港币3.522942元) -> 3.522942"""
        result = parse_dividend_amount_hkd("每股派美元0.45元(相当于港币3.522942元)")
        assert result == pytest.approx(3.522942, abs=1e-6)

    def test_usd_to_hkd_jisuanzhi(self):
        """每股派美元0.13125元(相当于港币1.027545元(计算值)) -> 1.027545"""
        result = parse_dividend_amount_hkd("每股派美元0.13125元(相当于港币1.027545元(计算值))")
        assert result == pytest.approx(1.027545, abs=1e-6)

    def test_meituan_special_case(self):
        """特殊说明:每10股分派1股美团B类普通股股份(相当于每股派18.13港元) -> 18.13"""
        result = parse_dividend_amount_hkd(
            "特殊说明:每10股分派1股美团B类普通股股份(相当于每股派18.13港元)"
        )
        assert result == pytest.approx(18.13, abs=1e-6)

    def test_02602_special_distribution(self):
        """02602 特别分配（含元字，正常写法）"""
        result = parse_dividend_amount_hkd("每股派人民币0.46元(相当于港币0.509元)")
        assert result == pytest.approx(0.509, abs=1e-6)

    def test_相当于港元_no_yuan(self):
        """相当于每股派 X 港元（无元字，另一种写法）"""
        result = parse_dividend_amount_hkd("每股派美元0.12元(相当于每股派1.05港元)")
        assert result == pytest.approx(1.05, abs=1e-6)


# ---------------------------------------------------------------------------
# 边界/无效输入
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_none_input(self):
        assert parse_dividend_amount_hkd(None) is None  # type: ignore

    def test_empty_string(self):
        assert parse_dividend_amount_hkd("") is None

    def test_rmb_only_no_hkd_conversion(self):
        """只有人民币，无港币换算 → 返回 None（不能把 RMB 当 HKD）"""
        result = parse_dividend_amount_hkd("每股派人民币0.50元")
        assert result is None, "纯人民币派息不应被解析为 HKD"

    def test_zero_amount(self):
        """金额为 0 → 返回 None"""
        result = parse_dividend_amount_hkd("每股派港币0.0元")
        assert result is None

    def test_garbage_text(self):
        """无任何数字格式 → None"""
        assert parse_dividend_amount_hkd("暂不派息") is None

    def test_stock_distribution_no_cash(self):
        """股票分派（无港币换算）→ None"""
        result = parse_dividend_amount_hkd("每10股送1股")
        assert result is None
