"""B37: 针对 2026-07 失败的 575 只 code 做定向补刷。

只拉 2 个月数据，且只针对 7 月缺失的 code，比 main.py --backfill-prices 快 5-10 倍。
"""
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import pandas as pd
from collectors.hk_price import get_hk_monthly_price_dual
from storage.store import load_monthly_price, load_stock_pool, save_monthly_price

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S"
)
logger = logging.getLogger(__name__)


def find_missing_july() -> tuple[list[str], int]:
    sl = load_stock_pool()
    sl["code"] = sl["code"].astype(str).str.zfill(5)
    pool_codes = set(sl["code"].tolist())

    mp = load_monthly_price()
    if mp.empty:
        return sorted(pool_codes), len(pool_codes)
    mp["code"] = mp["code"].astype(str).str.zfill(5)
    mp["date"] = pd.to_datetime(mp["date"])
    mp["period"] = mp["date"].dt.strftime("%Y-%m")
    july_codes = set(mp[mp["period"] == "2026-07"]["code"])
    missing = sorted(pool_codes - july_codes)
    return missing, len(pool_codes)


def fetch_one(code: str, start: str, end: str) -> pd.DataFrame:
    try:
        return get_hk_monthly_price_dual(code, start, end)
    except Exception as e:
        logger.warning(f"  {code} 异常: {e}")
        return pd.DataFrame()


def main():
    missing, total = find_missing_july()
    logger.info(f"2026-07 缺失: {len(missing)}/{total} 只")

    today = datetime.now()
    end_date = today.strftime("%Y%m%d")
    start_date = (today - pd.DateOffset(months=2)).strftime("%Y%m%d")
    logger.info(f"窗口 {start_date} ~ {end_date}")

    all_dfs = []
    ok = 0
    fail = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=4) as exe:
        futures = {exe.submit(fetch_one, c, start_date, end_date): c for c in missing}
        for i, fut in enumerate(as_completed(futures), 1):
            df = fut.result()
            if not df.empty:
                all_dfs.append(df)
                ok += 1
            else:
                fail += 1
            if i % 50 == 0 or i == len(missing):
                logger.info(f"  [{i}/{len(missing)}] 成功 {ok} 失败 {fail} ({time.time()-t0:.0f}s)")

    if all_dfs:
        combined = pd.concat(all_dfs, ignore_index=True)
        save_monthly_price(combined)
        logger.info(f"补刷完成，新增/更新 {len(combined)} 行（成功 {ok}/{len(missing)}）")
    else:
        logger.warning("无可保存数据")


if __name__ == "__main__":
    main()
