#!/usr/bin/env python3
"""补拉缺失的港股5年月度价格"""
import sys, os, time, logging
import pandas as pd
# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from collectors.hk_price import get_hk_monthly_price
from config.settings import HISTORY_YEARS
from datetime import datetime

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

def main():
    # 加载现有数据
    price_file = "data/csv/hk_monthly_price.csv"
    pool_file = "data/csv/hk_stock_pool.csv"
    
    existing = pd.read_csv(price_file)
    pool = pd.read_csv(pool_file)
    
    pool_codes = set(pool['code'].astype(str).str.zfill(5))
    existing_codes = set(existing['code'].astype(str).str.zfill(5))
    missing = sorted(pool_codes - existing_codes)
    
    if not missing:
        logger.info("所有股票的月度价格已齐全")
        return
    
    logger.info(f"缺失 {len(missing)} 只股票的月度价格，开始补拉...")
    
    end_date = datetime.now().strftime("%Y%m%d")
    start_date = f"{datetime.now().year - HISTORY_YEARS}0101"
    
    new_rows = []
    success = 0
    
    for i, code in enumerate(missing):
        logger.info(f"[{i+1}/{len(missing)}] 拉取 {code}...")
        df = get_hk_monthly_price(code, start_date, end_date, max_retries=3)
        if not df.empty:
            new_rows.append(df)
            success += 1
            logger.info(f"  {code} 成功，{len(df)} 个月数据")
        else:
            logger.warning(f"  {code} 失败（重试3次后仍失败）")
    
    if new_rows:
        new_df = pd.concat(new_rows, ignore_index=True)
        combined = pd.concat([existing, new_df], ignore_index=True)
        combined.to_csv(price_file, index=False)
        logger.info(f"已保存: {len(pool_codes - set(combined['code'].astype(str).str.zfill(5)))} 只仍缺失")
    
    logger.info(f"完成: {success}/{len(missing)} 拉取成功")

if __name__ == "__main__":
    main()