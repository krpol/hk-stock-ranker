"""
数据质量体检器 —— hk_financial.csv 三维校验。

用法：
    # 完整体检（覆盖率 + 时效 + 合理性）
    python scripts/data_quality_check.py

    # CI 模式：有 FAIL 级别问题时 exit 1
    python scripts/data_quality_check.py --strict

    # 交叉验证：随机抽 N 只对 AKShare/yfinance 实时拉取，检测静默漂移
    python scripts/data_quality_check.py --cross-check 20

    # 只检查某个维度
    python scripts/data_quality_check.py --dim coverage
    python scripts/data_quality_check.py --dim freshness
    python scripts/data_quality_check.py --dim sanity

三维校验：
  1. Coverage（覆盖率）：字段非空率 vs registry 中 min_coverage
  2. Freshness（时效）：as_of 报告期距今 vs registry 中 max_lag_days；
     交叉验证时对比数据源实际最新期，识别「本可更新但没刷新」的股票
  3. Sanity（合理性）：值域越界、负值违规、量纲异常

输出：
  - 控制台摘要（PASS/WARN/FAIL 计数 + 前 N 条明细）
  - data/quality_report.json（完整明细）
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402
from collectors.field_registry import FIELD_REGISTRY, IGNORED_FIELDS, FieldMeta  # noqa: E402

CSV_PATH = Path("data/csv/hk_financial.csv")
REPORT_PATH = Path("data/quality_report.json")


# ---------------------------------------------------------------------------
# 维度 1：Coverage
# ---------------------------------------------------------------------------
def check_coverage(df: pd.DataFrame) -> list[dict]:
    """检查每字段非空率是否达到 registry 中 min_coverage。"""
    issues: list[dict] = []
    total = len(df)
    for col, meta in FIELD_REGISTRY.items():
        if col not in df.columns:
            issues.append({
                "dim": "coverage", "severity": "fail",
                "field": col, "detail": "字段缺失",
            })
            continue
        # 日期/字符串字段：不做 to_numeric，直接看空串/NaN
        is_date_like = (
            col.endswith("_as_of") or col.endswith("_end")
            or col.endswith("_date") or col in {"forward_div_source", "fiscal_periods"}
        )
        s = df[col]
        if is_date_like:
            s = s.astype(object).replace("", pd.NA)
            nonnull = s.notna().sum()
        else:
            s2 = pd.to_numeric(s, errors="coerce")
            nonnull = s2.notna().sum() if s2.dtype.kind in "fi" else s.notna().sum()
        pct = nonnull / total if total else 0.0
        if pct < meta.min_coverage:
            issues.append({
                "dim": "coverage",
                "severity": meta.severity,
                "field": col,
                "detail": f"非空率 {pct:.1%} < 下限 {meta.min_coverage:.1%}",
                "nonnull": int(nonnull), "total": int(total),
            })
    return issues


# ---------------------------------------------------------------------------
# 维度 2：Freshness
# ---------------------------------------------------------------------------
def check_freshness(df: pd.DataFrame, today: Optional[datetime] = None) -> list[dict]:
    """检查 as_of 报告期距今是否超过 max_lag_days。"""
    if today is None:
        today = datetime.now()
    issues: list[dict] = []
    for col, meta in FIELD_REGISTRY.items():
        if meta.max_lag_days is None or col not in df.columns:
            continue
        s = pd.to_datetime(df[col], errors="coerce")
        if s.isna().all():
            continue
        lag = (today - s).dt.days
        stale_mask = lag > meta.max_lag_days
        stale_n = int(stale_mask.sum())
        if stale_n == 0:
            continue
        # 汇总 + 样例 5 只
        sample = df.loc[stale_mask, ["code"]].head(5)["code"].tolist() if "code" in df.columns else []
        issues.append({
            "dim": "freshness",
            "severity": meta.severity,
            "field": col,
            "detail": f"{stale_n} 只股票 {col} 滞后 > {meta.max_lag_days} 天",
            "stale_count": stale_n,
            "sample_codes": sample,
            "max_lag_days": int(lag[stale_mask].max()) if stale_n else 0,
        })
    return issues


# ---------------------------------------------------------------------------
# 维度 3：Sanity
# ---------------------------------------------------------------------------
def check_sanity(df: pd.DataFrame, sample_n: int = 5) -> list[dict]:
    """检查值域越界、负值违规、量纲异常。"""
    issues: list[dict] = []
    for col, meta in FIELD_REGISTRY.items():
        if col not in df.columns or col.endswith("_as_of"):
            continue
        s = pd.to_numeric(df[col], errors="coerce")
        if s.dtype.kind not in "fi":
            continue
        # 负值
        if not meta.allow_negative:
            neg_mask = s < 0
            if neg_mask.any():
                sample = df.loc[neg_mask, "code"].head(sample_n).tolist() if "code" in df.columns else []
                issues.append({
                    "dim": "sanity",
                    "severity": meta.severity,
                    "field": col, "kind": "negative",
                    "detail": f"{int(neg_mask.sum())} 只出现负值（不允许）",
                    "sample_codes": sample,
                    "min": float(s.min()),
                })
        # 值域
        lo, hi = meta.sane_range
        if lo is not None:
            below = (s < lo) & meta.allow_negative if not (not meta.allow_negative) else (s < lo) & (s >= 0)
            below = s < lo
            if below.any():
                sample = df.loc[below, "code"].head(sample_n).tolist() if "code" in df.columns else []
                issues.append({
                    "dim": "sanity",
                    "severity": meta.severity,
                    "field": col, "kind": "below_range",
                    "detail": f"{int(below.sum())} 只低于下限 {lo}",
                    "sample_codes": sample, "min": float(s.min()),
                })
        if hi is not None:
            above = s > hi
            if above.any():
                sample = df.loc[above, "code"].head(sample_n).tolist() if "code" in df.columns else []
                issues.append({
                    "dim": "sanity",
                    "severity": meta.severity,
                    "field": col, "kind": "above_range",
                    "detail": f"{int(above.sum())} 只超过上限 {hi}",
                    "sample_codes": sample, "max": float(s.max()),
                })
    return issues


# ---------------------------------------------------------------------------
# 交叉验证：抽样对比 AKShare vs CSV 存量值（识别静默漂移）
# ---------------------------------------------------------------------------
def cross_check(df: pd.DataFrame, n: int = 20) -> list[dict]:
    """随机抽 n 只，实时拉 AKShare 最新报告期，对比 CSV 里 fcf_as_of/debt_as_of。"""
    from collectors.hk_financial import _get_hk_report_akshare, _AK_REPORT_CACHE
    issues: list[dict] = []
    codes = df["code"].astype(str).str.zfill(5).tolist()
    sample = random.sample(codes, min(n, len(codes)))
    print(f"[cross-check] 抽样 {len(sample)} 只：{sample}")
    for code in sample:
        _AK_REPORT_CACHE.clear()
        row = df[df["code"].astype(str).str.zfill(5) == code]
        if row.empty:
            continue
        row = row.iloc[0]
        try:
            ak = _get_hk_report_akshare(code, "现金流量表")
            if ak is None or ak.empty:
                continue
            ak_latest = sorted(ak["REPORT_DATE"].astype(str).unique(), reverse=True)[0][:10]
        except Exception:
            continue
        fcf_as_of = str(row.get("fcf_as_of", ""))[:10]
        if fcf_as_of and ak_latest > fcf_as_of:
            issues.append({
                "dim": "freshness",
                "severity": "warn",
                "field": "fcf_as_of",
                "kind": "silent_drift",
                "detail": f"[{code}] AKShare 已更新到 {ak_latest} 但 CSV 里 fcf_as_of={fcf_as_of}",
                "code": code,
                "ak_latest": ak_latest, "csv_as_of": fcf_as_of,
            })
    return issues


# ---------------------------------------------------------------------------
# 无登记字段检测（防止新增字段漏登记）
# ---------------------------------------------------------------------------
def check_registry_completeness(df: pd.DataFrame) -> list[dict]:
    """CSV 里有但 registry 里没登记 & 不在 IGNORED 的字段。"""
    registered = set(FIELD_REGISTRY.keys())
    csv_fields = set(df.columns)
    unregistered = csv_fields - registered - IGNORED_FIELDS
    return [
        {"dim": "registry", "severity": "fail",
         "field": c, "detail": "CSV 有此字段但 field_registry 未登记，请补充或加入 IGNORED_FIELDS"}
        for c in sorted(unregistered)
    ]


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def run_check(dims: set[str], cross_n: int = 0) -> dict:
    df = pd.read_csv(CSV_PATH, dtype={"code": str})
    df["code"] = df["code"].astype(str).str.zfill(5)

    all_issues: list[dict] = []
    if "registry" in dims:
        all_issues.extend(check_registry_completeness(df))
    if "coverage" in dims:
        all_issues.extend(check_coverage(df))
    if "freshness" in dims:
        all_issues.extend(check_freshness(df))
    if "sanity" in dims:
        all_issues.extend(check_sanity(df))
    if cross_n > 0:
        all_issues.extend(cross_check(df, cross_n))

    warn_n = sum(1 for i in all_issues if i.get("severity") == "warn")
    fail_n = sum(1 for i in all_issues if i.get("severity") == "fail")

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "csv_path": str(CSV_PATH),
        "total_rows": len(df),
        "summary": {"warn": warn_n, "fail": fail_n, "total_issues": len(all_issues)},
        "issues": all_issues,
    }


def print_report(report: dict, verbose: bool = True) -> None:
    s = report["summary"]
    print(f"\n{'='*60}")
    print(f"数据质量体检报告  |  行数：{report['total_rows']}  |  {report['generated_at']}")
    print(f"{'='*60}")
    print(f"WARN: {s['warn']:3d}    FAIL: {s['fail']:3d}    合计: {s['total_issues']}")
    if not report["issues"]:
        print("✅ 无异常")
        return
    print("-" * 60)
    # 按维度分组打印
    by_dim: dict = {}
    for i in report["issues"]:
        by_dim.setdefault(i["dim"], []).append(i)
    for dim, items in by_dim.items():
        print(f"\n[{dim.upper()}] {len(items)} 条")
        limit = len(items) if verbose else 10
        for i in items[:limit]:
            sev = "🔴FAIL" if i["severity"] == "fail" else "🟡WARN"
            print(f"  {sev} {i['field']:32s} {i['detail']}")
            if i.get("sample_codes"):
                print(f"         样例: {', '.join(i['sample_codes'][:5])}")
        if not verbose and len(items) > 10:
            print(f"  ... 还有 {len(items)-10} 条（见 {REPORT_PATH}）")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dim", choices=["all", "registry", "coverage", "freshness", "sanity"],
                    default="all")
    ap.add_argument("--cross-check", type=int, default=0,
                    help="随机抽 N 只做实时数据源对比（较慢，需要网络）")
    ap.add_argument("--strict", action="store_true",
                    help="有 FAIL 级别问题时 exit 1（CI 模式）")
    ap.add_argument("--quiet", action="store_true", help="仅打印摘要不打印明细")
    args = ap.parse_args()

    if args.dim == "all":
        dims = {"registry", "coverage", "freshness", "sanity"}
    else:
        dims = {args.dim}

    report = run_check(dims, cross_n=args.cross_check)

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2))

    print_report(report, verbose=not args.quiet)
    print(f"\n详细报告：{REPORT_PATH}")

    if args.strict and report["summary"]["fail"] > 0:
        print(f"\n❌ strict 模式：发现 {report['summary']['fail']} 条 FAIL，退出 1")
        sys.exit(1)


if __name__ == "__main__":
    main()
