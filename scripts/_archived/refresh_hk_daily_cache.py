"""
B47: hk_daily 缓存 CLI —— 永久缓存 + 失败持续重试。

用法:
    # 展示缓存与失败状态
    python scripts/refresh_hk_daily_cache.py --stats

    # 重试所有 failed 记录（attempts<5 才会重试；死码除外）
    python scripts/refresh_hk_daily_cache.py --retry-failed

    # 重试并把死码也拉进来（复活所有 failed）
    python scripts/refresh_hk_daily_cache.py --retry-failed --include-dead

    # 强制刷新指定 code（无论缓存是否存在）
    python scripts/refresh_hk_daily_cache.py --force 09928 --force 03718

    # 刷新 pool 里所有 code 里未缓存的
    python scripts/refresh_hk_daily_cache.py --refresh-pool 2026-06-02

    # 清空 failed sidecar（重置所有 attempts）
    python scripts/refresh_hk_daily_cache.py --clear-failed
"""
import argparse
import json
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from collectors.hk_price import (
    _HK_DAILY_CACHE_DIR,
    _HK_DAILY_FAILED_PATH,
    _FAILED_MAX_ATTEMPTS,
    _load_failed_daily,
    _save_failed_daily,
    _fetch_single_daily,
    _load_daily_cache,
    _is_dead_code,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def show_stats():
    n_cached = 0
    total_size = 0
    if os.path.isdir(_HK_DAILY_CACHE_DIR):
        for fn in os.listdir(_HK_DAILY_CACHE_DIR):
            if fn.endswith(".pkl"):
                n_cached += 1
                try:
                    total_size += os.path.getsize(os.path.join(_HK_DAILY_CACHE_DIR, fn))
                except OSError:
                    pass
    fd = _load_failed_daily()
    dead = [c for c, r in fd.items() if int(r.get("attempts", 0)) >= _FAILED_MAX_ATTEMPTS]
    logger.info(f"=== hk_daily 缓存 stats ===")
    logger.info(f"  cache dir : {_HK_DAILY_CACHE_DIR}")
    logger.info(f"  cached codes  : {n_cached}")
    logger.info(f"  disk size     : {total_size/1024/1024:.1f} MB")
    logger.info(f"  failed sidecar: {_HK_DAILY_FAILED_PATH}")
    logger.info(f"  failed codes  : {len(fd)}  (dead >= {_FAILED_MAX_ATTEMPTS}: {len(dead)})")
    if fd:
        # top 10 by attempts
        top = sorted(fd.items(), key=lambda kv: -int(kv[1].get("attempts", 0)))[:10]
        logger.info(f"  top failed:")
        for c, r in top:
            logger.info(f"    {c}: attempts={r.get('attempts')} last_err={str(r.get('last_error'))[:80]}")


def retry_failed(include_dead: bool = False):
    fd = _load_failed_daily()
    if not fd:
        logger.info("failed sidecar 为空，无需重试")
        return
    codes = list(fd.keys())
    if not include_dead:
        codes = [c for c in codes if int(fd[c].get("attempts", 0)) < _FAILED_MAX_ATTEMPTS]
    logger.info(f"重试 {len(codes)}/{len(fd)} 只 failed code（include_dead={include_dead}）")
    if include_dead:
        # 临时清 attempts 使 _is_dead_code=False
        for c in list(fd.keys()):
            if int(fd[c].get("attempts", 0)) >= _FAILED_MAX_ATTEMPTS:
                fd[c]["attempts"] = _FAILED_MAX_ATTEMPTS - 1
        _save_failed_daily()

    ok = fail = 0
    for i, code in enumerate(codes, 1):
        df = _fetch_single_daily(code, use_cache=False, force=True)
        if df is not None and not df.empty:
            ok += 1
        else:
            fail += 1
        if i % 20 == 0:
            logger.info(f"  进度 [{i}/{len(codes)}] ok={ok} fail={fail}")
    _save_failed_daily()
    logger.info(f"重试完成 ok={ok} fail={fail}")


def force_refresh(codes):
    for code in codes:
        code = str(code).zfill(5)
        logger.info(f"强制刷新 {code}")
        df = _fetch_single_daily(code, use_cache=False, force=True)
        if df is None or df.empty:
            logger.warning(f"  {code} 失败")
        else:
            logger.info(f"  {code} OK ({len(df)} 行)")
    _save_failed_daily()


def refresh_pool(pool_date: str):
    pool_path = os.path.join("data", "csv", "pools", f"hk_stock_pool_{pool_date}.csv")
    if not os.path.exists(pool_path):
        logger.error(f"pool 不存在: {pool_path}")
        return
    df = pd.read_csv(pool_path, dtype={"code": str})
    df["code"] = df["code"].str.zfill(5)
    codes = df["code"].tolist()
    missing = [c for c in codes if _load_daily_cache(c) is None and not _is_dead_code(c)]
    logger.info(f"pool {pool_date}: {len(codes)} 只，缺缓存 {len(missing)} 只（跳过死码）")
    ok = fail = 0
    for i, code in enumerate(missing, 1):
        df_ = _fetch_single_daily(code, use_cache=False)
        if df_ is not None and not df_.empty:
            ok += 1
        else:
            fail += 1
        if i % 20 == 0:
            logger.info(f"  进度 [{i}/{len(missing)}] ok={ok} fail={fail}")
    _save_failed_daily()
    logger.info(f"补齐完成 ok={ok} fail={fail}")


def clear_failed():
    if os.path.exists(_HK_DAILY_FAILED_PATH):
        os.remove(_HK_DAILY_FAILED_PATH)
        logger.info(f"已删除 {_HK_DAILY_FAILED_PATH}")
    else:
        logger.info("failed sidecar 不存在，无需清空")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--stats", action="store_true", help="显示缓存与失败统计")
    p.add_argument("--retry-failed", action="store_true", help="重试 failed 里 attempts<max 的 code")
    p.add_argument("--include-dead", action="store_true", help="配合 --retry-failed 使用；同时重试死码")
    p.add_argument("--force", action="append", default=[], help="强制刷新指定 code，可多次指定")
    p.add_argument("--refresh-pool", help="补齐指定 pool 快照里所有未缓存 code，格式 YYYY-MM-DD")
    p.add_argument("--clear-failed", action="store_true", help="删除 failed sidecar 文件")
    args = p.parse_args()

    if args.clear_failed:
        clear_failed()
    if args.stats:
        show_stats()
    if args.retry_failed:
        retry_failed(include_dead=args.include_dead)
    if args.force:
        force_refresh(args.force)
    if args.refresh_pool:
        refresh_pool(args.refresh_pool)

    if not any([args.stats, args.retry_failed, args.force, args.refresh_pool, args.clear_failed]):
        show_stats()


if __name__ == "__main__":
    main()
