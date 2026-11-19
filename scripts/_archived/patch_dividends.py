#!/usr/bin/env python3
"""
增量补丁：仅重新计算 TTM 分红体系（5 列），保留其他字段（回购/负债/股本/价格等）

修复 bug：季度财报误归属 → 中概回港股（如 03660）TTM 错误

特性：失败股票每 5 分钟重试一次，最多 5 轮（共 ~25 分钟），直到全部成功
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import time
import logging
from collectors.hk_financial import get_fiscal_year_dividends
from collectors.hk_forward_dividend import get_forward_div_map

logging.basicConfig(level=logging.WARNING, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

CSV_PATH = "data/csv/hk_financial.csv"
DIV_COLS = [
    "fy_div_per_share",
    "prev_year_div_per_share",
    "latest_period_div_per_share",
    "latest_period_yoy_div_per_share",
    "forward_div_per_share",
    "current_fy_end",
    "prev_fy_end",
    "latest_period_end",
    "latest_period_yoy_end",
    "fiscal_periods",
    "latest_attributed_fy",
]

RETRY_INTERVAL_SEC = 300  # 5 分钟
MAX_RETRY_ROUNDS = 5


def process_codes(df, codes, fwd_map, label="主轮"):
    """对指定 code 列表跑一遍。返回 (changed_count, fail_codes)"""
    changed = 0
    fail_codes = []
    t0 = time.time()
    total = len(codes)
    for j, code in enumerate(codes):
        idx = df.index[df["code"] == code]
        if len(idx) == 0:
            continue
        i = idx[0]
        row = df.loc[i]
        try:
            new = get_fiscal_year_dividends(code, forward_div_override=fwd_map.get(code, 0.0))
            old_ttm = float(row.get("fy_div_per_share", 0) or 0)
            new_ttm = float(new.get("fy_div_per_share", 0) or 0)
            if abs(old_ttm - new_ttm) > 0.001:
                changed += 1
                if changed <= 30 and label == "主轮":
                    print(f"  [{code}] {row.get('name','')} TTM {old_ttm:.3f} → {new_ttm:.3f}", flush=True)
            for col in DIV_COLS:
                if col in new:
                    df.at[i, col] = new[col]
        except Exception as e:
            fail_codes.append(code)
            if len(fail_codes) <= 10:
                print(f"  [{code}] FAIL: {e}", flush=True)
        if (j + 1) % 50 == 0:
            elapsed = time.time() - t0
            rate = (j + 1) / max(elapsed, 1)
            eta = (total - j - 1) / max(rate, 0.01)
            print(f"  [{label}] 进度 {j+1}/{total} | 改 {changed} | 失败 {len(fail_codes)} | {rate:.1f}/s | ETA {eta:.0f}s", flush=True)
    return changed, fail_codes


def main():
    df = pd.read_csv(CSV_PATH, dtype={"code": str})
    df["code"] = df["code"].astype(str).str.zfill(5)
    print(f"加载 {len(df)} 条记录", flush=True)

    fwd_map = get_forward_div_map()
    print(f"HKEX 预告分红 override: {len(fwd_map)} 只", flush=True)

    backup = CSV_PATH + ".bak"
    df.to_csv(backup, index=False)
    print(f"已备份 → {backup}\n", flush=True)

    # 优化：只处理 fiscal_periods 含季度日期的股票（受 bug 影响的）
    affected_file = "/tmp/affected_codes.txt"
    if os.path.exists(affected_file):
        with open(affected_file) as f:
            target_codes = [c.strip() for c in f if c.strip()]
        print(f"仅处理受影响的 {len(target_codes)} 只股票（来自 {affected_file}）\n", flush=True)
    else:
        target_codes = df["code"].tolist()
        print(f"处理全部 {len(target_codes)} 只股票\n", flush=True)
    total_changed = 0

    # 主轮
    changed, fail_codes = process_codes(df, target_codes, fwd_map, label="主轮")
    total_changed += changed
    df.to_csv(CSV_PATH, index=False)
    print(f"\n主轮完成. 改 {changed} 只, 失败 {len(fail_codes)} 只\n", flush=True)

    # 重试轮
    round_no = 0
    while fail_codes and round_no < MAX_RETRY_ROUNDS:
        round_no += 1
        print(f"\n等待 {RETRY_INTERVAL_SEC}s 后重试 {len(fail_codes)} 只失败股票（第 {round_no}/{MAX_RETRY_ROUNDS} 轮）...", flush=True)
        time.sleep(RETRY_INTERVAL_SEC)
        changed, fail_codes = process_codes(df, fail_codes, fwd_map, label=f"重试{round_no}")
        total_changed += changed
        df.to_csv(CSV_PATH, index=False)
        print(f"第 {round_no} 轮重试完成. 改 {changed} 只, 仍失败 {len(fail_codes)} 只", flush=True)

    print(f"\n=== 最终统计 ===", flush=True)
    print(f"总变化: {total_changed} 只", flush=True)
    print(f"最终失败: {len(fail_codes)} 只", flush=True)
    if fail_codes:
        print(f"失败列表: {fail_codes[:30]}{'...' if len(fail_codes)>30 else ''}", flush=True)


if __name__ == "__main__":
    main()
