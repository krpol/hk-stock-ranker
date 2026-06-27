"""
B16: 财务数据三态容错重试 CLI

Usage:
    # 只跑 failed sidecar 中所有 (code, domain)
    python scripts/refresh_financial_failed.py --retry-failed

    # 只刷某个子维度（无视 sidecar，全池跑该 domain）
    python scripts/refresh_financial_failed.py --domain growth

    # 单只调试
    python scripts/refresh_financial_failed.py --code 06686
    python scripts/refresh_financial_failed.py --code 06686 --domain growth

    # 清空 empty sidecar 强制重新探测
    python scripts/refresh_financial_failed.py --reset-empty
"""
import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from collectors.incremental import (
    incremental_update_financial,
    EMPTY_SIDECAR,
    FAILED_SIDECAR,
    load_failed,
    load_empty,
    save_empty,
)
from collectors.hk_forward_dividend import get_forward_div_map
from collectors.hk_financial import ALL_DOMAINS
from storage.store import load_stock_pool, load_stock_list

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--retry-failed", action="store_true",
                        help="只跑 failed sidecar 中的码")
    parser.add_argument("--domain", action="append",
                        help=f"限制 domain（可重复）；可选: {ALL_DOMAINS}")
    parser.add_argument("--code", action="append",
                        help="只跑指定 code（可重复，5位字符串）")
    parser.add_argument("--reset-empty", action="store_true",
                        help="清空 empty sidecar 强制重新探测")
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()

    if args.reset_empty:
        save_empty({})
        logger.info(f"已清空 {EMPTY_SIDECAR}")
        if not args.retry_failed and not args.domain and not args.code:
            return

    # 加载股票池
    sl = load_stock_pool()
    if sl.empty:
        sl = load_stock_list()
    if sl.empty:
        logger.error("股票池为空，请先 python main.py --update")
        return

    # 加载预分红 map
    try:
        forward_div_map = get_forward_div_map()
    except Exception as e:
        logger.warning(f"加载预分红 map 失败: {e}")
        forward_div_map = {}

    # domains 校验
    domains = None
    if args.domain:
        domains = [d for d in args.domain if d in ALL_DOMAINS]
        if not domains:
            logger.error(f"无效 domain，可选: {ALL_DOMAINS}")
            return

    refresh_codes = None
    if args.code:
        refresh_codes = [c.zfill(5) for c in args.code]
        logger.info(f"指定 codes: {refresh_codes}")

    # 打印 sidecar 概况
    failed_map = load_failed()
    empty_map = load_empty()
    logger.info(f"sidecar 状态：failed={len(failed_map)} 只，empty={len(empty_map)} 只")

    if args.retry_failed:
        if not failed_map:
            logger.info("failed sidecar 为空，无需重试")
            return
        # 显示 failed 详情
        from collections import Counter
        domain_count = Counter()
        for code, doms in failed_map.items():
            for d in doms.keys():
                domain_count[d] += 1
        logger.info(f"待重试 domain 分布: {dict(domain_count)}")

    incremental_update_financial(
        stock_list=sl,
        forward_div_map=forward_div_map,
        workers=args.workers,
        check_dividend_history=False,
        refresh_codes=refresh_codes,
        retry_failed_only=args.retry_failed,
        domains=domains,
    )

    failed_map_after = load_failed()
    empty_map_after = load_empty()
    logger.info(
        f"完成：failed {len(failed_map)} → {len(failed_map_after)}，"
        f"empty {len(empty_map)} → {len(empty_map_after)}"
    )


if __name__ == "__main__":
    main()
