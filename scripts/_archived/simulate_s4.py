#!/usr/bin/env python3
"""用修复后的 has_share_dilution 模拟 S4 过滤，生成新股票池"""
import sys, os, time, logging
import pandas as pd
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from collectors.hk_financial import has_share_dilution, AH_DUAL_LISTED_STOCKS as ah

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

def main():
    step3 = pd.read_csv("data/csv/step3_dividend.csv")
    codes = step3['code'].astype(str).str.zfill(5).tolist()
    logger.info(f"Step3 进入: {len(codes)} 只, A+H豁免: {len([c for c in codes if c in ah])} 只")
    
    passed = []
    filtered = []
    grey_zone = []
    
    for i, code in enumerate(codes):
        should_filter, max_dec = has_share_dilution(code, years=3)
        row = step3[step3['code'].astype(str).str.zfill(5) == code].iloc[0].to_dict()
        row['max_decrease_pct'] = f"{max_dec*100:.1f}%"
        
        if code in ah:
            row['filter_reason'] = 'A+H豁免'
            row['s4_status'] = 'passed'
            passed.append(row)
        elif should_filter:
            row['filter_reason'] = '合股(>=50%)' if max_dec > 0.3 else '增发(>=8%)'
            row['s4_status'] = 'filtered'
            filtered.append(row)
        else:
            if max_dec >= 0.10:
                row['s4_status'] = 'grey_zone'
                grey_zone.append(row)
            else:
                row['s4_status'] = 'passed'
            passed.append(row)
        
        if (i+1) % 50 == 0:
            logger.info(f"S4进程: {i+1}/{len(codes)} (通过:{len(passed)}, 过滤:{len(filtered)}, 灰色:{len(grey_zone)})")
    
    # 保存结果
    pool_df = pd.DataFrame(passed + grey_zone)
    
    # 构建股票池（需要 current_price — 从 step3 继承）
    pool_output = pool_df[['code', 'name', 'current_price']].copy() if 'current_price' in pool_df.columns else pool_df[['code', 'name']].copy()
    if 'current_price' not in pool_output.columns:
        logger.warning("step3 中无 current_price，从现有 pool 获取(price 列)")
        old_pool = pd.read_csv("data/csv/hk_stock_pool.csv", dtype={"code": str})
        old_pool['code'] = old_pool['code'].astype(str).str.zfill(5)
        pool_output['code'] = pool_output['code'].astype(str).str.zfill(5)
        price_col = 'price' if 'price' in old_pool.columns else 'current_price'
        pool_output = pool_output.merge(old_pool[['code', price_col]].rename(columns={price_col: 'current_price'}), on='code', how='left')
    
    pool_output.to_csv("data/csv/hk_stock_pool_new.csv", index=False)
    
    filtered_df = pd.DataFrame(filtered)
    filtered_df.to_csv("data/csv/step4_dilution_filtered_new.csv", index=False)
    
    logger.info("=" * 50)
    logger.info(f"S4 模拟完成:")
    logger.info(f"  Step3 进入:   {len(codes)}")
    logger.info(f"  A+H 豁免:     {len([r for r in passed if r.get('s4_status')=='passed' and r.get('filter_reason')=='A+H豁免'])}")
    logger.info(f"  S4 通过:      {len(passed)} (含灰色{len(grey_zone)})")
    logger.info(f"  S4 过滤:      {len(filtered)} ({len(filtered)/len(codes)*100:.1f}%)")
    logger.info(f"  最终池:       {len(passed)+len(grey_zone)}")
    logger.info("=" * 50)

if __name__ == "__main__":
    main()