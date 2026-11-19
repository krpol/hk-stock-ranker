"""补刷指定月份缺失的价格数据（定向、只拉缺失 code，比全量快 5-10 倍）。

用法：
  .venv/bin/python scripts/backfill_missing_month.py --period 2026-07
  .venv/bin/python scripts/backfill_missing_month.py              # 默认当前月
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
from storage.store import load_monthly_price, load_stock_pool, save_monthly_price

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S"
)
logger = logging.getLogger(__name__)


def find_missing_period(period: str) -> tuple[list[str], int]:
    """识别股票池中缺失指定月份（YYYY-MM）价格的 code"""
    sl = load_stock_pool()
    sl["code"] = sl["code"].astype(str).str.zfill(5)
    pool_codes = set(sl["code"].tolist())

    mp = load_monthly_price()
    if mp.empty:
        return sorted(pool_codes), len(pool_codes)
    mp["code"] = mp["code"].astype(str).str.zfill(5)
    mp["date"] = pd.to_datetime(mp["date"])
    mp["period"] = mp["date"].dt.strftime("%Y-%m")
    period_codes = set(mp[mp["period"] == period]["code"])
    missing = sorted(pool_codes - period_codes)
    return missing, len(pool_codes)


def fetch_one(code: str, start: str, end: str) -> pd.DataFrame:
    try:
        return get_hk_monthly_price_dual(code, start, end)
    except Exception as e:
        logger.warning(f"  {code} 异常: {e}")
        return pd.DataFrame()


def main():
    parser = argparse.ArgumentParser(description="补刷指定月份缺失的价格数据（定向）")
    parser.add_argument("--period", default=datetime.now().strftime("%Y-%m"),
                        help="目标月份 YYYY-MM（默认当前月）")
    args = parser.parse_args()
    period = args.period

    # 校验 period 格式并定位窗口起点
    try:
        y, m = map(int, period.split("-"))
        period_start = datetime(y, m, 1)
    except Exception:
        logger.error(f"无效 period: {period}，应为 YYYY-MM")
        sys.exit(1)

    missing, total = find_missing_period(period)
    logger.info(f"{period} 缺失: {len(missing)}/{total} 只")

    # 窗口：目标月前 1 个月 ～ 今天（确保拉到的日度数据覆盖目标月整月）
    start_date = (period_start - pd.DateOffset(months=1)).strftime("%Y%m%d")
    end_date = datetime.now().strftime("%Y%m%d")
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
