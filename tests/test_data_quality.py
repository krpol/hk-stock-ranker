"""
tests/test_data_quality.py —— 数据质量治理体系单测。

覆盖：
1. field_registry 与 CSV 列的双向一致性（漏登记 & 幽灵字段）
2. 三维检测器行为（coverage / freshness / sanity）
3. FieldMeta.check_value 值域断言
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from collectors.field_registry import (  # noqa: E402
    FIELD_REGISTRY,
    IGNORED_FIELDS,
    FieldMeta,
    registered_fields,
)
from scripts.data_quality_check import (  # noqa: E402
    check_coverage,
    check_freshness,
    check_registry_completeness,
    check_sanity,
)


# ---------------------------------------------------------------------------
# 1. Registry ↔ CSV 一致性
# ---------------------------------------------------------------------------
class TestRegistryConsistency:
    """确保 hk_financial.csv 的每列要么登记、要么显式忽略——防止新增字段漏挂。"""

    @pytest.fixture(scope="class")
    def df(self):
        csv = ROOT / "data" / "csv" / "hk_financial.csv"
        if not csv.exists():
            pytest.skip("hk_financial.csv 不存在，跳过一致性测试")
        return pd.read_csv(csv, nrows=5, dtype={"code": str})

    def test_no_unregistered_fields(self, df):
        """CSV 中每一列都必须在 registry 或 IGNORED_FIELDS 中。"""
        unregistered = set(df.columns) - registered_fields() - IGNORED_FIELDS
        assert not unregistered, (
            f"发现 {len(unregistered)} 个未登记字段，请在 field_registry.py 里补充："
            f" {sorted(unregistered)}"
        )

    def test_no_ghost_fields(self, df):
        """registry 里登记但 CSV 里不存在的字段（防止 registry 陈旧）。"""
        ghosts = registered_fields() - set(df.columns) - IGNORED_FIELDS
        assert not ghosts, f"registry 中有 CSV 里不存在的幽灵字段：{sorted(ghosts)}"


# ---------------------------------------------------------------------------
# 2. Coverage 检测器
# ---------------------------------------------------------------------------
class TestCoverageCheck:
    def test_missing_column_is_fail(self):
        """字段完全缺失 → FAIL。"""
        df = pd.DataFrame({"code": ["00001"], "roe": [0.1]})
        issues = check_coverage(df)
        # roe 存在，ocf_3y_avg 应报缺失
        missing = [i for i in issues if i["field"] == "ocf_3y_avg"]
        assert missing and missing[0]["severity"] == "fail"

    def test_low_coverage_reported(self):
        """非空率低于 min_coverage → 报警。"""
        # 10 只，roe 只填 3 只（30% < 90% 下限）
        df = pd.DataFrame({
            "code": [f"{i:05d}" for i in range(10)],
            **{c: [None] * 10 for c in FIELD_REGISTRY},
        })
        df.loc[:2, "roe"] = 0.1
        issues = check_coverage(df)
        roe_issue = [i for i in issues if i["field"] == "roe"]
        assert roe_issue, "roe 应触发 coverage 报警"

    def test_date_columns_not_counted_as_nan(self):
        """日期字符串列不应被 to_numeric 误判为 NaN。"""
        df = pd.DataFrame({
            "code": ["00001", "00002"],
            "fcf_as_of": ["2025-12-31", "2025-12-31"],
            **{c: [1.0, 1.0] for c in FIELD_REGISTRY if c != "fcf_as_of"},
        })
        issues = check_coverage(df)
        as_of_issues = [i for i in issues if i["field"] == "fcf_as_of"]
        assert not as_of_issues, "日期列不应误报为 0% 覆盖"


# ---------------------------------------------------------------------------
# 3. Freshness 检测器
# ---------------------------------------------------------------------------
class TestFreshnessCheck:
    def test_recent_data_no_issue(self):
        """近期报告期不报警。"""
        df = pd.DataFrame({
            "code": ["00001"],
            "fcf_as_of": [datetime.now().strftime("%Y-%m-%d")],
            "debt_as_of": [datetime.now().strftime("%Y-%m-%d")],
        })
        issues = check_freshness(df, today=datetime.now())
        stale = [i for i in issues if i["field"] in {"fcf_as_of", "debt_as_of"}]
        assert not stale

    def test_stale_data_reported(self):
        """超过 max_lag_days 的报告期 → 报警。"""
        old = (datetime.now() - timedelta(days=1000)).strftime("%Y-%m-%d")
        df = pd.DataFrame({
            "code": ["00001", "00002"],
            "fcf_as_of": [old, old],
        })
        issues = check_freshness(df, today=datetime.now())
        stale = [i for i in issues if i["field"] == "fcf_as_of"]
        assert stale and stale[0]["stale_count"] == 2


# ---------------------------------------------------------------------------
# 4. Sanity 检测器
# ---------------------------------------------------------------------------
class TestSanityCheck:
    def test_negative_not_allowed(self):
        """allow_negative=False 的字段出现负值 → 报警。"""
        df = pd.DataFrame({
            "code": ["00001", "00002"],
            "cash_and_equivalents": [-1e6, 1e6],  # cash 不允许负
        })
        issues = check_sanity(df)
        neg = [i for i in issues if i["field"] == "cash_and_equivalents" and i["kind"] == "negative"]
        assert neg

    def test_above_range_reported(self):
        """值超过上限 → 报警。"""
        df = pd.DataFrame({
            "code": ["00001"],
            "total_liability_ratio": [2.5],  # 上限 1.2
        })
        issues = check_sanity(df)
        above = [i for i in issues if i["field"] == "total_liability_ratio" and i["kind"] == "above_range"]
        assert above

    def test_within_range_ok(self):
        """正常值不触发。"""
        df = pd.DataFrame({
            "code": ["00001"],
            "roe": [0.15],  # 在 (-2, 2) 内
        })
        issues = check_sanity(df)
        roe = [i for i in issues if i["field"] == "roe"]
        assert not roe


# ---------------------------------------------------------------------------
# 5. FieldMeta.check_value
# ---------------------------------------------------------------------------
class TestFieldMetaCheckValue:
    def test_none_value_returns_none(self):
        """None/NaN 由覆盖率处理，check_value 返回 None。"""
        meta = FieldMeta("test", "src", "ttm")
        assert meta.check_value(None) is None
        assert meta.check_value(float("nan")) is None

    def test_negative_not_allowed(self):
        meta = FieldMeta("test", "src", "ttm", allow_negative=False)
        msg = meta.check_value(-1.0)
        assert msg is not None and "负值" in msg

    def test_out_of_upper_bound(self):
        meta = FieldMeta("test", "src", "ttm", sane_range=(0, 10))
        msg = meta.check_value(15.0)
        assert msg is not None and "上限" in msg

    def test_out_of_lower_bound(self):
        meta = FieldMeta("test", "src", "ttm", sane_range=(0, 10))
        msg = meta.check_value(-5.0)
        # 允许负值 → 但会击穿下限
        assert msg is not None and "下限" in msg

    def test_within_range(self):
        meta = FieldMeta("test", "src", "ttm", sane_range=(0, 10))
        assert meta.check_value(5.0) is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
