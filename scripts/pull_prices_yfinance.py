#!/usr/bin/env python3
"""用 yfinance 批量补拉港股5年月度价格（备选方案，AKShare 不可用时的 fallback）"""
import sys, os, time, logging
import pandas as pd
import yfinance as yf

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

PRICE_FILE = "data/csv/hk_monthly_price.csv"
POOL_FILE = "data/csv/hk_stock_pool.csv"

def main():
    existing = pd.read_csv(PRICE_FILE)
    pool = pd.read_csv(POOL_FILE)
    
    pool_codes = set(pool['code'].astype(str).str.zfill(5))
    existing_codes = set(existing['code'].astype(str).str.zfill(5))
    missing = sorted(pool_codes - existing_codes)
    
    if not missing:
        logger.info("月度价格已齐全！")
        return
    
    logger.info(f"共 {len(pool_codes)} 只股票，已有 {len(pool_codes & existing_codes)}，缺失 {len(missing)}")
    
    new_rows = []
    success = 0
    failed = 0
    
    for i, code in enumerate(missing):
        num = int(code)
        ticker_str = f'{num:04d}.HK'
        try:
            t = yf.Ticker(ticker_str)
            hist = t.history(period='5y', interval='1mo')
            if hist.empty:
                logger.warning(f"[{i+1}/{len(missing)}] {code} - 无数据")
                failed += 1
                continue
            
            hist = hist.reset_index()
            hist['code'] = code
            hist = hist.rename(columns={
                'Date': 'date', 'Open': 'open', 'Close': 'close',
                'High': 'high', 'Low': 'low', 'Volume': 'volume'
            })
            # 保持与 AKShare 格式一致
            hist['amount'] = 0  # yfinance 不提供成交额
            cols = ['date', 'code', 'open', 'close', 'high', 'low', 'volume', 'amount']
            hist = hist[[c for c in cols if c in hist.columns]]
            new_rows.append(hist)
            success += 1
            if (i+1) % 50 == 0:
                logger.info(f"进度: {i+1}/{len(missing)} (成功:{success}, 失败:{failed})")
        except Exception as e:
            logger.warning(f"[{i+1}/{len(missing)}] {code} 失败: {str(e)[:60]}")
            failed += 1
        
        if (i+1) % 20 == 0:
            time.sleep(2)  # 每20只停顿2秒，避免限流
    
    if new_rows:
        new_df = pd.concat(new_rows, ignore_index=True)
        combined = pd.concat([existing, new_df], ignore_index=True)
        combined.to_csv(PRICE_FILE, index=False)
        logger.info(f"已保存: {success} 只新增，{failed} 只失败")
        remaining = pool_codes - set(combined['code'].astype(str).str.zfill(5))
        logger.info(f"仍缺失: {len(remaining)} 只")
    else:
        logger.warning("无新增数据")

if __name__ == "__main__":
    main()