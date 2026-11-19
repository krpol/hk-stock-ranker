"""
独立行业数据采集脚本

用法：
  python scripts/refresh_industry_only.py              # 仅补缺失（默认）
  python scripts/refresh_industry_only.py --all        # 全量刷新
  python scripts/refresh_industry_only.py --workers 8  # 并发数
"""
import argparse
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description="刷新行业归属数据")
    parser.add_argument("--all", action="store_true", help="全量刷新（默认仅补缺失）")
    parser.add_argument("--workers", type=int, default=8, help="并发数")
    args = parser.parse_args()

    from collectors.hk_industry import collect_industries
    from storage.store import (
        load_stock_pool,
        load_stock_list,
        load_industry_data,
        upsert_industry_data,
    )

    pool = load_stock_pool()
    if pool.empty:
        pool = load_stock_list()
    if pool.empty:
        logger.error("股票池/股票列表为空，请先 --rebuild-pool")
        sys.exit(1)

    pool_codes = sorted(set(pool["code"].astype(str).str.zfill(5)))
    logger.info(f"股票池共 {len(pool_codes)} 只")

    if args.all:
        target_codes = pool_codes
        logger.info("模式：全量刷新")
    else:
        existing = load_industry_data()
        if existing.empty:
            have = set()
        else:
            existing["code"] = existing["code"].astype(str).str.zfill(5)
            have = set(
                existing.loc[
                    existing["industry"].astype(str).str.strip().replace("nan", "") != "",
                    "code",
                ]
            )
        target_codes = [c for c in pool_codes if c not in have]
        logger.info(f"模式：仅补缺失（已缓存 {len(have)} 只，待采集 {len(target_codes)} 只）")

    if not target_codes:
        logger.info("无待采集股票")
        return

    ind_map = collect_industries(target_codes, max_workers=args.workers)
    upsert_industry_data(ind_map)

    # 统计
    succ = sum(1 for v in ind_map.values() if v)
    fail = len(ind_map) - succ
    logger.info(f"✅ 完成：成功 {succ}/{len(ind_map)}，失败 {fail}")


if __name__ == "__main__":
    main()
