"""
B6: 真实股票 Badcase 回归验证集。

读取 data/csv/hk_financial.csv 实际值，对照 tests/badcases/known_badcases.yaml
中沉淀的历史 bug 案例做断言。任何重构再次引入老 bug，pytest 立即 fail。

设计原则：
- 不依赖 mock：直接验证产出文件，端到端覆盖 collector → engine → storage 全链路
- 宽松断言（range/lt/contains）避免数据每月微漂移阻断
- csv 缺失或股票退市时 skip 不 fail，本地/CI 都能跑

新增 case 流程：
  修复某只股票 bug 后 → 在 known_badcases.yaml 追加一条 → 跑 pytest 确认通过
"""
from pathlib import Path

import pandas as pd
import pytest

try:
    import yaml
except ImportError:
    pytest.skip("PyYAML not installed; pip install pyyaml", allow_module_level=True)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BADCASES_FILE = Path(__file__).parent / "badcases" / "known_badcases.yaml"
FINANCIAL_CSV = PROJECT_ROOT / "data" / "csv" / "hk_financial.csv"


# ---------- 加载 ----------

def _load_badcases():
    if not BADCASES_FILE.exists():
        return [], []
    with BADCASES_FILE.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data.get("cases", []) or [], data.get("global_invariants", []) or []


_CASES, _INVARIANTS = _load_badcases()


# ---------- 断言原语 ----------

def _to_float(v):
    """容错转浮点；NaN/None/空串返回 None。"""
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    if isinstance(v, str) and v.strip() == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _check_expect(actual, spec, row=None):
    """
    根据 spec 语法返回 (passed: bool, reason: str)。

    spec 是 dict，含以下任一键：
        exact, approx, lt, le, gt, ge, range, contains,
        not_equal_to_field, non_empty
    """
    if not isinstance(spec, dict):
        return False, f"spec must be dict, got {type(spec).__name__}: {spec!r}"

    if "exact" in spec:
        passed = str(actual) == str(spec["exact"])
        return passed, f"expect exact={spec['exact']!r}, got {actual!r}"

    if "approx" in spec:
        a = _to_float(actual)
        if a is None:
            return False, f"non-numeric {actual!r}"
        target = float(spec["approx"])
        passed = abs(a - target) < 1e-6
        return passed, f"expect ≈{target}, got {a}"

    if "lt" in spec:
        a = _to_float(actual)
        if a is None:
            return False, f"non-numeric {actual!r}"
        return a < spec["lt"], f"expect <{spec['lt']}, got {a}"

    if "le" in spec:
        a = _to_float(actual)
        if a is None:
            return False, f"non-numeric {actual!r}"
        return a <= spec["le"], f"expect ≤{spec['le']}, got {a}"

    if "gt" in spec:
        a = _to_float(actual)
        if a is None:
            return False, f"non-numeric {actual!r}"
        return a > spec["gt"], f"expect >{spec['gt']}, got {a}"

    if "ge" in spec:
        a = _to_float(actual)
        if a is None:
            return False, f"non-numeric {actual!r}"
        return a >= spec["ge"], f"expect ≥{spec['ge']}, got {a}"

    if "range" in spec:
        lo, hi = spec["range"]
        a = _to_float(actual)
        if a is None:
            return False, f"non-numeric {actual!r}"
        return lo <= a <= hi, f"expect [{lo}, {hi}], got {a}"

    if "contains" in spec:
        s = "" if actual is None else str(actual)
        missing = [sub for sub in spec["contains"] if sub not in s]
        return not missing, f"expect contain {spec['contains']}, missing {missing} (actual={s!r})"

    if "not_equal_to_field" in spec:
        if row is None:
            return False, "not_equal_to_field requires row context"
        other_field = spec["not_equal_to_field"]
        other = row.get(other_field)
        passed = str(actual) != str(other)
        return passed, f"expect != {other_field}={other!r}, got {actual!r}"

    if "non_empty" in spec and spec["non_empty"]:
        if actual is None:
            return False, "expect non-empty, got None"
        if isinstance(actual, float) and pd.isna(actual):
            return False, "expect non-empty, got NaN"
        if isinstance(actual, str) and actual.strip() == "":
            return False, "expect non-empty, got empty string"
        return True, "ok"

    return False, f"unknown spec keys: {list(spec.keys())}"


# ---------- Fixtures ----------

@pytest.fixture(scope="module")
def fin_df():
    """加载 hk_financial.csv；缺失时 skip 全部用例。"""
    if not FINANCIAL_CSV.exists():
        pytest.skip(f"{FINANCIAL_CSV} not found; run main.py --update first")
    df = pd.read_csv(FINANCIAL_CSV, dtype={"code": str})
    df["code"] = df["code"].astype(str).str.zfill(5)
    return df


# ---------- 逐 case 回归 ----------

@pytest.mark.parametrize(
    "case",
    _CASES,
    ids=lambda c: f"{c.get('issue', '?')}-{c.get('code', '?')}",
)
def test_known_badcase(case, fin_df):
    """对单只股票多个字段做断言。"""
    code = str(case["code"]).zfill(5)
    matched = fin_df[fin_df["code"] == code]
    if matched.empty:
        # require_present=true 的 case 必须存在（如 B9 流动性补回验证），不允许 skip
        if case.get("require_present"):
            pytest.fail(
                f"{case.get('issue', '?')} {code} {case.get('name', '')}: "
                f"require_present=true 但 code 不在 hk_financial.csv —— "
                f"可能流动性过滤把它误踢出股票池（请检查 B9 窗口复核逻辑）"
            )
        pytest.skip(f"{code} not in financial.csv (out of universe / delisted)")

    row = matched.iloc[0]
    failures = []
    for field, spec in (case.get("expect") or {}).items():
        actual = row.get(field) if field in row.index else None
        passed, reason = _check_expect(actual, spec, row=row)
        if not passed:
            failures.append(f"  - [{field}] {reason}")

    if failures:
        header = f"{case.get('issue', '?')} {code} {case.get('name', '')}"
        pytest.fail(f"{header}:\n" + "\n".join(failures))


# ---------- 全集不变量 ----------

@pytest.mark.parametrize(
    "inv",
    _INVARIANTS,
    ids=lambda i: f"{i.get('issue', '?')}-{i.get('rule', '?')}",
)
def test_global_invariant(inv, fin_df):
    rule = inv.get("rule")

    if rule == "forward_le_fy":
        tol = float(inv.get("tolerance", 1e-6))
        max_ratio = float(inv.get("max_violation_ratio", 0.0))
        fwd = fin_df["forward_div_per_share"].fillna(0)
        fy = fin_df["fy_div_per_share"].fillna(0)
        bad = fin_df[fwd > fy + tol]
        if len(bad) > max_ratio * len(fin_df):
            sample = bad[["code", "fy_div_per_share", "forward_div_per_share"]].head(5).to_string(index=False)
            pytest.fail(
                f"forward > fy at {len(bad)}/{len(fin_df)} rows "
                f"(> {max_ratio:.1%} 豁免额度，B4 双计 bug 复现):\n{sample}"
            )

    elif rule == "total_liability_ratio_coverage":
        max_nan = float(inv.get("max_nan_ratio", 0.5))
        assert "total_liability_ratio" in fin_df.columns, \
            "total_liability_ratio column missing (B3 regression)"
        nan_ratio = fin_df["total_liability_ratio"].isna().mean()
        assert nan_ratio < max_nan, \
            f"total_liability_ratio NaN ratio {nan_ratio:.2%} ≥ {max_nan:.0%}"

    elif rule == "no_outlier_dividend":
        threshold = float(inv.get("threshold", 30.0))
        for col in ("fy_div_per_share", "forward_div_per_share"):
            outliers = fin_df[fin_df[col].fillna(0) > threshold]
            if not outliers.empty:
                sample = outliers[["code", col]].head(5).to_string(index=False)
                pytest.fail(
                    f"{col} > {threshold} (B2 异常预案 bug 复现?):\n{sample}"
                )

    else:
        pytest.fail(f"unknown invariant rule: {rule!r}")
