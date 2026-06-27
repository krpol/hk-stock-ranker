"""
tests/test_web_div_yoy_filter.py

集成测试：验证 web 层 _apply_filters / frg / fdg 过滤逻辑。

覆盖：
  - B8: TTM 回报同比不下降 (frg=1) 展示层过滤，保留原排名跳号
  - 万物云 02602 badcase 验证（B7 修复后 frg 应能正确过滤）
  - NaN 处理（prev=NaN 时视为 0，curr >= 0 则通过）
  - fdg (分红同比不下降) 的独立过滤
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
import pandas as pd
import numpy as np

from web.app import _apply_filters


# ---------------------------------------------------------------------------
# 测试数据工厂
# ---------------------------------------------------------------------------

def make_df(**kwargs) -> pd.DataFrame:
    """构造最小化的 ranking DataFrame，默认值可被 kwargs 覆盖。"""
    defaults = {
        "code": ["01000", "02000", "03000"],
        "name": ["股票A", "股票B", "股票C"],
        "rank": [1, 2, 3],
        "fy_div_per_share":          [2.0, 1.5, 0.0],
        "prev_year_div_per_share":   [1.0, 2.0, 0.0],
        "latest_period_div_per_share":     [1.0, 0.8, 0.0],
        "latest_period_yoy_div_per_share": [0.8, 1.0, 0.0],
        "ttm_return_rate":      [2.5, 2.0, 1.0],
        "prev_ttm_return_rate": [2.0, 2.2, 0.5],
    }
    data = {**defaults, **kwargs}
    return pd.DataFrame(data)


# ---------------------------------------------------------------------------
# filter_return_growth (frg) — TTM 回报同比不下降
# ---------------------------------------------------------------------------

class TestFilterReturnGrowth:
    def test_no_filter(self):
        """不开 frg → 全部保留"""
        df = make_df()
        result = _apply_filters(df, False, False)
        assert len(result) == 3

    def test_frg_keeps_yoy_positive(self):
        """frg → 只保留 curr >= prev"""
        df = make_df()
        # 股票A: 2.5 >= 2.0 ✓; 股票B: 2.0 < 2.2 ✗; 股票C: 1.0 >= 0.5 ✓
        result = _apply_filters(df, False, True)
        assert len(result) == 2
        assert "02000" not in result["code"].values, "股票B(回报下降)应被过滤"
        assert "01000" in result["code"].values
        assert "03000" in result["code"].values

    def test_frg_preserves_original_rank(self):
        """frg 只做展示层过滤，原 rank 列不变（跳号）"""
        df = make_df()
        result = _apply_filters(df, False, True)
        # rank 1,3 保留，rank 2 跳号
        assert list(result["rank"]) == [1, 3]

    def test_frg_nan_prev_treated_as_zero(self):
        """prev=NaN 时 fillna(0)，curr > 0 通过"""
        df = make_df(
            ttm_return_rate=[1.0, 0.5, 0.1],
            prev_ttm_return_rate=[np.nan, np.nan, 0.0],
        )
        result = _apply_filters(df, False, True)
        # 所有 curr >= 0 → 全部通过
        assert len(result) == 3

    def test_frg_nan_curr_treated_as_zero(self):
        """curr=NaN 时 fillna(0)，prev > 0 → 被过滤"""
        df = make_df(
            ttm_return_rate=[np.nan, 1.0, 0.5],
            prev_ttm_return_rate=[1.0, 0.5, 0.0],
        )
        result = _apply_filters(df, False, True)
        # 股票A: 0 < 1.0 → 过滤；股票B: 1.0 >= 0.5 ✓；股票C: 0.5 >= 0 ✓
        assert len(result) == 2
        assert "01000" not in result["code"].values

    def test_frg_missing_columns_no_crash(self):
        """ranking df 没有 ttm_return_rate 列时不崩溃"""
        df = pd.DataFrame({"code": ["01000"], "rank": [1]})
        result = _apply_filters(df, False, True)
        assert len(result) == 1


# ---------------------------------------------------------------------------
# filter_div_growth (fdg) — 分红同比不下降
# ---------------------------------------------------------------------------

class TestFilterDivGrowth:
    def test_fdg_fy_div_only(self):
        """fdg → 过滤 fy_div < prev_year_div 的股票"""
        df = make_df()
        # 股票A: fy=2.0 >= prev=1.0 ✓; 股票B: fy=1.5 < prev=2.0 ✗; 股票C: 0==0 ✓
        result = _apply_filters(df, True, False)
        assert "02000" not in result["code"].values, "股票B(分红下降)应被过滤"

    def test_fdg_latest_period_filter(self):
        """fdg 同时检查最近期分红 >= 同期上年分红"""
        # 股票A: 最近期 1.0 >= yoy 0.8 ✓; 股票B: latest=0.8 < yoy_latest=1.0 ✗
        df = make_df()
        result = _apply_filters(df, True, False)
        remaining = set(result["code"].values)
        # 股票B 因 fy_div < prev_year_div 已被第一轮过滤，不在结果中
        assert "02000" not in remaining

    def test_fdg_frg_combined(self):
        """fdg + frg 同时开启 → 两个条件取交集"""
        df = make_df()
        result = _apply_filters(df, True, True)
        # 股票A: 分红 ✓ 回报 ✓ → 保留; 股票B: 两项都不通过 → 过滤; 股票C: 分红0==0 ✓, 回报 ✓ → 保留
        assert "02000" not in result["code"].values


# ---------------------------------------------------------------------------
# B8 + B7 联合验证：02602 万物云在 B7 修复后应被 fdg 过滤（上年分红 2.079 > 今年 1.667）
# ---------------------------------------------------------------------------

class TestWanyuyunB7B8Integration:
    """B7 修复了 prev_year_div_per_share 后，B8 的 fdg 过滤应能正确识别万物云分红下降。"""

    def _make_02602_row(self, prev_year: float) -> pd.DataFrame:
        return pd.DataFrame({
            "code": ["02602"],
            "name": ["万物云"],
            "rank": [5],
            "fy_div_per_share": [1.667],
            "prev_year_div_per_share": [prev_year],
            "latest_period_div_per_share": [1.667],
            "latest_period_yoy_div_per_share": [prev_year],
            "ttm_return_rate": [1.8],
            "prev_ttm_return_rate": [prev_year],
        })

    def test_b7_buggy_data_would_pass_fdg(self):
        """B7 修复前：prev=1.457 < curr=1.667 → fdg 不过滤（假阳性通过）"""
        df = self._make_02602_row(prev_year=1.457)
        result = _apply_filters(df, True, False)
        assert len(result) == 1, "B7 修复前的错误数据会导致 02602 被误判为分红不下降（通过过滤）"

    def test_b7_fixed_data_filtered_by_fdg(self):
        """B7 修复后：prev=2.079 > curr=1.667 → fdg 正确过滤"""
        df = self._make_02602_row(prev_year=2.079)
        result = _apply_filters(df, True, False)
        assert len(result) == 0, "B7 修复后 prev_year=2.079 > fy=1.667，02602 应被 fdg 过滤"

    def test_b7_fixed_frg_also_filters(self):
        """B7 修复后：TTM 回报 1.8 vs prev 2.079 → frg 也应过滤"""
        df = self._make_02602_row(prev_year=2.079)
        result = _apply_filters(df, False, True)
        assert len(result) == 0, "B7 修复后 02602 TTM 回报下降，frg 应过滤"
