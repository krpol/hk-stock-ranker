"""B13: 新入池/价格不足股票 5 年月度价格回填脚本

独立可重跑工具。识别 stock_pool 内月度价格行数 < HISTORY_YEARS*12*0.8 (默认 48 月) 的 code，
拉 5 年数据后 upsert 到 hk_monthly_price.csv。

可重复运行，自动跳过已修复股票。AKShare 限流时建议 --workers 1 慢速跑。

用法：
    python -m scripts.backfill_new_pool_prices               # workers=2
    python -m scripts.backfill_new_pool_prices --workers 1   # 慢速重试
    python -m scripts.backfill_new_pool_prices --dry-run     # 仅列出待回填
"""
import argparse
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd

from collectors.hk_price import get_hk_monthly_price_dual
from config.settings import HISTORY_YEARS
from storage.store import load_monthly_price, load_stock_pool, save_monthly_price

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S"
)
logger = logging.getLogger(__name__)


def find_shortfall(threshold_months: int) -> tuple[list[str], int]:
    sl = load_stock_pool()
    sl["code"] = sl["code"].astype(str).str.zfill(5)
    mp = load_monthly_price()
    if mp.empty:
        counts = pd.Series(dtype=int)
    else:
        mp["code"] = mp["code"].astype(str).str.zfill(5)
        counts = mp.groupby("code").size()
    pool = sl["code"].tolist()
    shortfall = [c for c in pool if int(counts.get(c, 0)) < threshold_months]
    return shortfall, len(pool)


def fetch_one(code: str, start: str, end: str) -> pd.DataFrame:
    try:
        return get_hk_monthly_price_dual(code, start, end)
    except Exception as e:
        logger.warning(f"  价格回填异常 {code}: {e}")
        return pd.DataFrame()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=2, help="并发数 (推荐 1-2，避免限流)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument(
        "--ratio", type=float, default=0.8, help="阈值比例: threshold = HISTORY_YEARS*12*ratio"
    )
    args = ap.parse_args()

    threshold = int(HISTORY_YEARS * 12 * args.ratio)
    shortfall, total = find_shortfall(threshold)
    logger.info(
        f"池内 {total} 只，价格 < {threshold} 月: {len(shortfall)} 只"
        f" (HISTORY_YEARS={HISTORY_YEARS}, ratio={args.ratio})"
    )
    if args.dry_run:
        for c in shortfall:
            print(c)
        return
    if not shortfall:
        logger.info("无需回填")
        return

    today = datetime.now()
    start_date = (today - pd.DateOffset(years=HISTORY_YEARS)).strftime("%Y%m%d")
    end_date = today.strftime("%Y%m%d")
    logger.info(f"窗口 {start_date} ~ {end_date}, workers={args.workers}")

    t0 = time.time()
    dfs = []
    ok = empty = 0
    with ThreadPoolExecutor(max_workers=args.workers) as exe:
        futs = {exe.submit(fetch_one, c, start_date, end_date): c for c in shortfall}
        done = 0
        for fut in as_completed(futs):
            done += 1
            df = fut.result()
            if not df.empty:
                dfs.append(df)
                ok += 1
            else:
                empty += 1
            if done % 10 == 0 or done == len(shortfall):
                logger.info(
                    f"  [{done}/{len(shortfall)}] ok={ok} empty/fail={empty} "
                    f"elapsed={time.time()-t0:.0f}s"
                )

    if dfs:
        save_monthly_price(pd.concat(dfs, ignore_index=True))
        logger.info(f"保存 {sum(len(d) for d in dfs)} 行 (耗时 {time.time()-t0:.0f}s)")

    # 复核
    shortfall2, _ = find_shortfall(threshold)
    logger.info(
        f"复核：修复 {len(shortfall) - len(shortfall2)} 只，"
        f"仍不足 {len(shortfall2)} 只 (可能为次新股或长期停牌)"
    )


if __name__ == "__main__":
    main()
