"""
B46: 修复 pool snapshot 的流动性字段。

背景:
    历史 pool snapshot (data/csv/pools/hk_stock_pool_YYYY-MM-DD.csv) 里的
    price/volume/amount/market_cap 来自"跑脚本当天"的 AKShare 实时 spot 接口，
    并非 as_of_date 当天真实值。本脚本用 ak.stock_hk_daily 拉真实日线覆盖。

用法:
    # 修单个 snapshot
    python scripts/fix_pool_snapshot_liquidity.py --only 2025-12-31

    # 修全部（含备份到 data/csv/pools/bak/）
    python scripts/fix_pool_snapshot_liquidity.py --all

    # 试运行不写盘
    python scripts/fix_pool_snapshot_liquidity.py --all --dry-run
"""
import argparse
import logging
import os
import re
import shutil
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from collectors.hk_price import get_hk_prices_at_date

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

POOL_DIR = os.path.join("data", "csv", "pools")
BAK_DIR = os.path.join(POOL_DIR, "bak")
_FN_RE = re.compile(r"^hk_stock_pool_(\d{4}-\d{2}-\d{2})\.csv$")


def list_snapshots() -> list:
    if not os.path.isdir(POOL_DIR):
        return []
    dates = []
    for fn in os.listdir(POOL_DIR):
        m = _FN_RE.match(fn)
        if m:
            dates.append(m.group(1))
    return sorted(dates)


def fix_one(as_of_str: str, dry_run: bool = False, workers: int = 8) -> tuple:
    """
    修单个 snapshot。返回 (修复数, 未命中数)
    """
    path = os.path.join(POOL_DIR, f"hk_stock_pool_{as_of_str}.csv")
    if not os.path.exists(path):
        logger.error(f"{as_of_str} 快照不存在: {path}")
        return 0, 0
    df = pd.read_csv(path, dtype={"code": str})
    df["code"] = df["code"].str.zfill(5)
    logger.info(f"=== {as_of_str}: 共 {len(df)} 只 ===")
    as_of_date = datetime.strptime(as_of_str, "%Y-%m-%d")

    # 拉真实值
    codes = df["code"].tolist()
    real = get_hk_prices_at_date(codes, as_of_date, fallback_days=7, max_workers=workers)

    if not real:
        logger.warning(f"{as_of_str}: 无任何 code 命中，跳过")
        return 0, len(codes)

    # 覆盖字段：price=close, volume, amount, market_cap=amount（保留原语义）
    hit = miss = 0
    price_col = "price" if "price" in df.columns else None
    for i, row in df.iterrows():
        code = row["code"]
        rec = real.get(code)
        if not rec or rec.get("close") is None:
            miss += 1
            continue
        if price_col:
            df.at[i, price_col] = rec["close"]
        # 保存 open/high/low 若列存在
        for k_src, k_dst in [("open", "今开"), ("high", "最高"), ("low", "最低")]:
            if k_dst in df.columns and rec.get(k_src) is not None:
                df.at[i, k_dst] = rec[k_src]
        if "volume" in df.columns:
            df.at[i, "volume"] = rec["volume"]
        if "amount" in df.columns:
            df.at[i, "amount"] = rec["amount"]
        if "market_cap" in df.columns:
            df.at[i, "market_cap"] = rec["amount"]  # 与现有 pipeline 语义一致：mcap=当日成交额
        hit += 1

    logger.info(f"{as_of_str}: 命中 {hit}, 未命中 {miss}（保持原值）")

    if dry_run:
        logger.info(f"{as_of_str}: --dry-run 未写盘")
        # 抽样打印 5 条
        sample = df.head(3)[["code", "name", "price", "amount", "market_cap"]] if all(c in df.columns for c in ["code","name","price","amount","market_cap"]) else df.head(3)
        logger.info(f"抽样:\n{sample.to_string(index=False)}")
        return hit, miss

    # 备份
    os.makedirs(BAK_DIR, exist_ok=True)
    bak_path = os.path.join(BAK_DIR, f"hk_stock_pool_{as_of_str}.before_liquidity_fix.csv")
    if not os.path.exists(bak_path):
        shutil.copy2(path, bak_path)
        logger.info(f"{as_of_str}: 备份至 {bak_path}")

    df.to_csv(path, index=False, encoding="utf-8-sig")
    logger.info(f"{as_of_str}: 已写回 {path}")
    return hit, miss


def main():
    p = argparse.ArgumentParser()
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--only", help="仅修某一 snapshot，格式 YYYY-MM-DD")
    g.add_argument("--all", action="store_true", help="修全部 snapshot")
    p.add_argument("--dry-run", action="store_true", help="不写盘")
    p.add_argument("--workers", type=int, default=8)
    args = p.parse_args()

    if args.only:
        targets = [args.only]
    else:
        targets = list_snapshots()
        logger.info(f"发现 {len(targets)} 个 snapshot: {targets}")

    summary = {}
    for d in targets:
        try:
            hit, miss = fix_one(d, dry_run=args.dry_run, workers=args.workers)
            summary[d] = (hit, miss)
        except Exception as e:
            logger.error(f"{d} 失败: {e}", exc_info=True)
            summary[d] = ("ERR", str(e))

    logger.info("=== 汇总 ===")
    for d, s in summary.items():
        logger.info(f"  {d}: {s}")


if __name__ == "__main__":
    main()
