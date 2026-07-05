"""
B45: 财务数据 stale 扫描 + 可选自动补采

用法:
    # 只扫描（不补采）
    python scripts/scan_stale_financial.py
    python scripts/scan_stale_financial.py --threshold 3 --output data/csv/hk_financial_stale.txt

    # 扫描 + 自动补采
    python scripts/scan_stale_financial.py --auto-refresh --workers 4

    # 全池扫描（不限 pool）
    python scripts/scan_stale_financial.py --all

判定:
    KEY_FIELDS_STALE 中 NaN 项数 >= threshold（默认 3） → stale

退出码:
    0 无 stale 或 auto-refresh 成功
    1 存在 stale 且未 auto-refresh（供 shell 判定）
"""
import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collectors.incremental import (
    scan_stale_codes,
    incremental_update_financial,
    KEY_FIELDS_STALE,
    STALE_NAN_THRESHOLD,
    load_failed,
    load_empty,
)
from collectors.hk_forward_dividend import get_forward_div_map
from storage.store import load_financial_data, load_stock_pool, load_stock_list

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


def main():
    p = argparse.ArgumentParser(description="扫描 hk_financial 关键字段 NaN 过多的 stale 股票")
    p.add_argument("--threshold", type=int, default=STALE_NAN_THRESHOLD,
                   help=f"NaN 计数阈值（默认 {STALE_NAN_THRESHOLD}）")
    p.add_argument("--all", action="store_true",
                   help="扫描全池（默认仅 pool 内）")
    p.add_argument("--auto-refresh", action="store_true",
                   help="扫描后自动补采 stale 股票")
    p.add_argument("--workers", type=int, default=4)
    p.add_argument("--output", default=os.path.join("data", "csv", "hk_financial_stale.txt"),
                   help="stale 清单输出路径")
    args = p.parse_args()

    fin = load_financial_data()
    if fin.empty:
        logger.error("hk_financial.csv 为空，请先 --update 或 --rebuild-pool")
        sys.exit(2)

    pool_codes = None
    if not args.all:
        pool = load_stock_pool()
        if pool.empty:
            pool = load_stock_list()
        if not pool.empty:
            pool_codes = set(pool["code"].astype(str).str.zfill(5).tolist())

    logger.info(
        f"扫描范围: {'全池 ('+str(len(fin))+' 只)' if args.all else 'pool 内 ('+str(len(pool_codes) if pool_codes else 0)+' 只)'} "
        f"threshold={args.threshold} 关键字段={KEY_FIELDS_STALE}"
    )

    stale = scan_stale_codes(fin, pool_codes=pool_codes, threshold=args.threshold)

    if not stale:
        logger.info("未发现 stale 股票 ✅")
        # 清空旧输出文件
        if os.path.exists(args.output):
            os.remove(args.output)
        sys.exit(0)

    logger.warning(f"发现 {len(stale)} 只 stale: {stale}")

    # 写清单
    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        for c in stale:
            f.write(c + "\n")
    logger.info(f"清单已写入 {args.output}")

    if not args.auto_refresh:
        sys.exit(1)

    # ---- auto-refresh 分支 ----
    logger.info(f"启动自动补采 ({args.workers} workers)...")
    sl = load_stock_pool()
    if sl.empty:
        sl = load_stock_list()
    try:
        fwd = get_forward_div_map()
    except Exception as e:
        logger.warning(f"预分红 map 加载失败: {e}")
        fwd = {}

    failed_before = len(load_failed())
    empty_before = len(load_empty())

    incremental_update_financial(
        stock_list=sl,
        forward_div_map=fwd,
        workers=args.workers,
        check_dividend_history=False,
        refresh_codes=stale,
    )

    failed_after = len(load_failed())
    empty_after = len(load_empty())
    logger.info(
        f"补采完成: failed {failed_before}→{failed_after}, empty {empty_before}→{empty_after}"
    )

    # 再扫描一次看修复效果
    fin_after = load_financial_data()
    stale_after = scan_stale_codes(fin_after, pool_codes=pool_codes, threshold=args.threshold)
    fixed = len(stale) - len(stale_after)
    logger.info(f"stale {len(stale)} → {len(stale_after)}（修复 {fixed} 只）")
    if stale_after:
        logger.warning(f"仍残留 stale: {stale_after}")
    sys.exit(0 if not stale_after else 1)


if __name__ == "__main__":
    main()
