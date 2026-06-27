#!/usr/bin/env python3
"""检查 TOP 100 排名股票的 TTM 分红体系数据是否正确（实时重算 vs CSV 对比）"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pandas as pd
import time
import logging
logging.basicConfig(level=logging.ERROR)

from collectors.hk_financial import get_fiscal_year_dividends
from collectors.hk_forward_dividend import get_forward_div_map

CSV = "data/csv/hk_ranking_2026-05.csv"
TOP_N = 100
TOL = 0.005  # 容差（受 yfinance 浮点波动影响）

CHECK_COLS = [
    ("fy_div_per_share",                "TTM"),
    ("prev_year_div_per_share",         "上年TTM"),
    ("latest_period_div_per_share",     "最近期"),
    ("latest_period_yoy_div_per_share", "同期上年"),
    ("forward_div_per_share",           "预分红"),
]

DATE_COLS = [
    ("current_fy_end",       "财年末"),
    ("prev_fy_end",          "上年末"),
    ("latest_period_end",    "最近期日"),
    ("latest_period_yoy_end","同期上年日"),
]


def main():
    df = pd.read_csv(CSV, dtype={"code": str})
    df["code"] = df["code"].astype(str).str.zfill(5)
    top = df.head(TOP_N).copy()
    print(f"=== 校验 TOP {TOP_N} 股票的 TTM 分红体系 ===\n", flush=True)

    fwd_map = get_forward_div_map()
    print(f"已加载 HKEX 预告 override: {len(fwd_map)} 只\n", flush=True)

    issues = []  # (rank, code, name, col, csv_val, real_val, diff)
    fail = []
    t0 = time.time()

    for i, row in top.iterrows():
        code = row["code"]
        try:
            real = get_fiscal_year_dividends(code, forward_div_override=fwd_map.get(code, 0.0))
        except Exception as e:
            fail.append((row["rank"], code, row["name"], str(e)))
            continue

        # 数值字段
        for col, label in CHECK_COLS:
            csv_v = float(row.get(col, 0) or 0)
            real_v = float(real.get(col, 0) or 0)
            diff = abs(csv_v - real_v)
            if diff > TOL:
                issues.append((int(row["rank"]), code, row["name"], label, csv_v, real_v, diff))

        # 日期字段
        for col, label in DATE_COLS:
            csv_v = str(row.get(col, "") or "")
            real_v = str(real.get(col, "") or "")
            if csv_v != real_v and not (csv_v == "nan" and real_v == "None"):
                issues.append((int(row["rank"]), code, row["name"], label, csv_v, real_v, None))

        if (i + 1) % 20 == 0:
            elapsed = time.time() - t0
            print(f"  进度 {i+1}/{TOP_N} | 异常累计 {len(issues)} | 失败 {len(fail)} | 用时 {elapsed:.0f}s", flush=True)

    elapsed = time.time() - t0
    print(f"\n=== 校验完成（用时 {elapsed:.0f}s）===\n", flush=True)
    print(f"总异常条数: {len(issues)}（涉及 {len(set((x[1] for x in issues)))} 只股票）", flush=True)
    print(f"调用失败: {len(fail)}\n", flush=True)

    if issues:
        print("=== 异常明细（按排名）===", flush=True)
        print(f"{'rank':>4} {'code':>6} {'name':<14} {'col':<10} {'csv':>12} {'real':>12} {'diff':>8}", flush=True)
        for rank, code, name, col, csv_v, real_v, diff in sorted(issues):
            try:
                csv_s = f"{csv_v:.4f}" if isinstance(csv_v, float) else str(csv_v)[:12]
                real_s = f"{real_v:.4f}" if isinstance(real_v, float) else str(real_v)[:12]
                diff_s = f"{diff:.4f}" if diff is not None else "—"
            except:
                csv_s = str(csv_v)[:12]; real_s = str(real_v)[:12]; diff_s = "—"
            print(f"{rank:>4} {code:>6} {str(name)[:14]:<14} {col:<10} {csv_s:>12} {real_s:>12} {diff_s:>8}", flush=True)

    if fail:
        print("\n=== 调用失败 ===", flush=True)
        for rank, code, name, e in fail:
            print(f"  #{rank} {code} {name}: {e}", flush=True)


if __name__ == "__main__":
    main()
