#!/usr/bin/env python3
"""仅更新财务数据 + 排名（跳过价格更新，避开 AKShare 故障）。

适用场景：修复 `get_fiscal_year_dates()` 后重新跑全量财务数据。
"""
import os
import sys
import time
import logging

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("logs/update_financial_only.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

from collectors.hk_financial import collect_financial_data
from collectors.hk_forward_dividend import get_forward_div_map
from storage.store import load_stock_pool, save_financial_data
from main import run_rank_only


def main():
    start = time.time()
    stock_list = load_stock_pool()
    logger.info(f"已加载股票池：{len(stock_list)} 只")

    forward_div_map = get_forward_div_map()
    logger.info(f"预分红数据：{len(forward_div_map)} 只")

    # 读取已有 current_price（避免再次访问 AKShare）
    try:
        old_fin = pd.read_csv("data/csv/hk_financial.csv", dtype={"code": str})
        old_fin["code"] = old_fin["code"].astype(str).str.zfill(5)
        old_price_map = old_fin.set_index("code")["current_price"].to_dict()
    except Exception:
        old_price_map = {}

    records = []
    for i, (_, row) in enumerate(stock_list.iterrows()):
        code = str(row["code"]).zfill(5)
        name = row.get("name", "")
        logger.info(f"  [{i+1}/{len(stock_list)}] {code} {name}")
        forward_override = forward_div_map.get(code)
        try:
            fin = collect_financial_data(
                code, forward_div_override=forward_override, check_dividend_history=False
            )
        except Exception as e:
            logger.warning(f"  [{code}] 采集失败: {e}")
            fin = {"code": code}

        # 复用已有 current_price（来自 hk_stock_pool.csv 的 price 列）
        fin["current_price"] = old_price_map.get(code) or row.get("price")
        records.append(fin)

        if (i + 1) % 50 == 0:
            elapsed = time.time() - start
            avg = elapsed / (i + 1)
            remaining = avg * (len(stock_list) - i - 1) / 60
            logger.info(f"  >>> 进度 {i+1}/{len(stock_list)}, 平均 {avg:.2f}s/只, 预计剩余 {remaining:.1f} 分钟")

    fin_df = pd.DataFrame(records)
    save_financial_data(fin_df)
    logger.info(f"财务数据已保存：{len(fin_df)} 只")

    # 重新计算排名
    run_rank_only(stock_list=stock_list, financial_df=fin_df)
    logger.info(f"===== 完成，总耗时 {(time.time()-start)/60:.1f} 分钟 =====")


if __name__ == "__main__":
    main()
