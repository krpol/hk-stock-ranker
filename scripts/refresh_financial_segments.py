"""财务数据增量刷新（分段前台执行）。

背景：环境特性下后台进程在 turn 结束会被清理，长任务必须前台分段同步跑。
本脚本把「需要刷新的财务数据股票」切成 N 段，逐段调用 incremental_update_financial，
段内自带 checkpoint（每 100 只落盘一次），跑完一段再跑下一段。

用法:
  python scripts/refresh_financial_segments.py --list --total 6
  python scripts/refresh_financial_segments.py --segment 0 --total 6 --workers 6
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from storage.store import load_stock_pool
from collectors.incremental import (
    incremental_update_financial,
    load_financial_data,
    load_failed,
    load_empty,
    needs_refresh,
)
from collectors.hk_forward_dividend import get_forward_div_map


def compute_refresh_codes() -> list:
    """按与 incremental_update_financial 相同的决策逻辑计算待刷新股票集合。"""
    existing = load_financial_data()
    if existing.empty:
        return []
    existing["code"] = existing["code"].astype(str).str.zfill(5)
    existing = existing.set_index("code", drop=False)

    failed_map = load_failed()
    empty_map = load_empty()

    to_refresh = []
    for code in existing["code"].tolist():
        if code in failed_map:
            to_refresh.append(code)
            continue
        if code in empty_map:
            # 默认 retry_empty=False，已确认空字段的码不主动重刷
            continue
        flag, _reason = needs_refresh(existing.loc[code])
        if flag:
            to_refresh.append(code)
    return sorted(to_refresh)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--segment", type=int, default=None, help="本次要跑的段号（0 起）")
    ap.add_argument("--total", type=int, default=6, help="总段数")
    ap.add_argument("--workers", type=int, default=6, help="并发数")
    ap.add_argument("--list", action="store_true", help="只列出刷新集合与分段，不实际跑")
    args = ap.parse_args()

    codes = compute_refresh_codes()
    seg_size = (len(codes) + args.total - 1) // args.total

    if args.list:
        print(f"待刷新 {len(codes)} 只，分 {args.total} 段（每段约 {seg_size} 只）")
        for i in range(args.total):
            seg = codes[i * seg_size:(i + 1) * seg_size]
            print(f"  段{i}: {len(seg)} 只  {seg[0]}..{seg[-1]}" if seg else f"  段{i}: 0 只")
        return

    if args.segment is None:
        ap.error("请指定 --segment N（或 --list 只查看分段）")

    seg = codes[args.segment * seg_size:(args.segment + 1) * seg_size]
    if not seg:
        print(f"段 {args.segment} 无待刷新股票，跳过")
        return

    print(f"=== 刷新段 {args.segment}/{args.total}: {len(seg)} 只, workers={args.workers} ===")
    stock_list = load_stock_pool()
    forward_div_map = get_forward_div_map()
    incremental_update_financial(
        stock_list=stock_list,
        forward_div_map=forward_div_map,
        workers=args.workers,
        refresh_codes=seg,
        check_dividend_history=False,
    )
    print(f"=== 段 {args.segment} 完成 ===")


if __name__ == "__main__":
    main()
