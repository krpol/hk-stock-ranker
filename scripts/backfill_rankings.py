"""
历史 ranking 回填脚本

策略 C（用户决策）：
- 用当前 hk_financial.csv 的财务数据（fy_div, forward_div, buyback, growth_yoy 等）
- 把 current_price 替换为历史月底价
- 用现有 rank_stocks 函数重新排序
- 输出 hk_ranking_{YYYY-MM}.csv 到 data/csv/

⚠️ 偏差声明：
1. 前瞻偏差：用今日财务因子估计历史 ranking，分红/回购等财务字段在历史时点可能尚未公告
2. 幸存者偏差：仅用今日股票池，未纳入历史已退市的票
此结果仅作为冷启动样本，待真实 ranking 累积满 24 月后弃用。

用法：
    python scripts/backfill_rankings.py --months 24
    python scripts/backfill_rankings.py --start 2024-06 --end 2026-04
"""
import argparse
import logging
import os
import sys
from datetime import datetime

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import HK_STOCK_FILTERS
from engine.ranker import rank_stocks
from storage import store as _store
from storage.store import (
    load_stock_pool, load_financial_data, load_monthly_price,
    load_industry_data, save_ranking,
)

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def get_month_end_prices(price_df: pd.DataFrame) -> pd.DataFrame:
    """按 (code, period) 取月末收盘价，宽表 index=period, columns=code"""
    df = price_df.copy()
    df["code"] = df["code"].astype(str).str.zfill(5)
    df["date"] = pd.to_datetime(df["date"])
    df["period"] = df["date"].dt.strftime("%Y-%m")
    df = df.sort_values(["code", "date"])
    last = df.groupby(["code", "period"]).tail(1)
    return last.pivot(index="period", columns="code", values="close").sort_index()


def list_available_months(price_wide: pd.DataFrame, start: str = None, end: str = None) -> list:
    months = list(price_wide.index)
    if start:
        months = [m for m in months if m >= start]
    if end:
        months = [m for m in months if m <= end]
    return sorted(months)


def backfill_one_month(period: str, stock_list: pd.DataFrame,
                        financial_df: pd.DataFrame, price_wide: pd.DataFrame,
                        skip_existing: bool = True,
                        use_pool_snapshot: bool = True,
                        industry_df: pd.DataFrame = None) -> bool:
    """对单月生成 ranking。返回是否实际生成（False=跳过）

    Args:
        use_pool_snapshot: True 时按 period 所属年份选 snapshot 池过滤；
                          False 时使用传入的 stock_list（向后兼容）
    """
    target_path = _store.RANKING_CSV.format(period=period)
    if skip_existing and os.path.exists(target_path):
        logger.info(f"  {period} 已存在，跳过（--overwrite 覆盖）")
        return False

    if period not in price_wide.index:
        logger.warning(f"  {period} 无价格快照，跳过")
        return False

    month_prices = price_wide.loc[period]  # Series: code -> close

    # ---- 选池：优先用 period 所属年份的 snapshot ----
    sl = stock_list
    if use_pool_snapshot:
        from backtest.portfolio import _select_pool_for_signal_period
        snap_pool = _select_pool_for_signal_period(period)
        if snap_pool is not None and not snap_pool.empty:
            sl = snap_pool
            logger.debug(f"  {period} 使用年度池快照 ({len(sl)} 只)")
        else:
            logger.warning(f"  {period} 无对应年度池快照，fallback 到当前池/传入列表")

    fin = financial_df.copy()
    fin["code"] = fin["code"].astype(str).str.zfill(5)
    # 替换 current_price 为该月月底价（缺失则保留原值，避免误删）
    new_price = fin["code"].map(month_prices)
    coverage = new_price.notna().sum() / max(len(fin), 1)
    fin["current_price"] = new_price.fillna(fin.get("current_price"))

    # 仅保留有月底价的股票（覆盖率不足时跳过）
    fin = fin[new_price.notna()].reset_index(drop=True)
    if fin.empty:
        logger.warning(f"  {period} 无任何股票有月底价，跳过")
        return False

    # 限制 fin 到所选 stock_list（年度池）以收敛 ranking
    sl = sl.copy()
    sl["code"] = sl["code"].astype(str).str.zfill(5)
    pool_codes = set(sl["code"].tolist())
    if pool_codes:
        fin = fin[fin["code"].isin(pool_codes)].reset_index(drop=True)
        if fin.empty:
            logger.warning(f"  {period} 池内股票无月底价或财务数据，跳过")
            return False

    # 移除 stock_list 自带的价格列，避免与 financial_df 的 current_price 冲突
    for col in ["price", "current_price"]:
        if col in sl.columns:
            sl = sl.drop(columns=[col])

    ranked = rank_stocks(
        stock_list=sl,
        financial_df=fin,
        min_market_cap=HK_STOCK_FILTERS.get("min_market_cap"),
        require_dividend_history=False,  # 历史回填不再过滤
        max_net_debt_ratio=HK_STOCK_FILTERS.get("max_net_debt_ratio"),
        max_total_liability_ratio=HK_STOCK_FILTERS.get("max_total_liability_ratio"),
        max_debt_to_equity=HK_STOCK_FILTERS.get("max_debt_to_equity"),
        industry_df=industry_df,
    )

    if ranked.empty:
        logger.warning(f"  {period} 排序结果为空，跳过")
        return False

    save_ranking(ranked, period)
    logger.info(f"  {period} ✓ 生成 {len(ranked)} 行（池 {len(pool_codes)}, 价格覆盖 {coverage:.0%}）")
    return True


def main():
    parser = argparse.ArgumentParser(description="历史 ranking 回填")
    parser.add_argument("--months", type=int, default=24, help="回填最近 N 个月（默认 24）")
    parser.add_argument("--start", type=str, default=None, help="起始 YYYY-MM")
    parser.add_argument("--end", type=str, default=None, help="结束 YYYY-MM（默认今天的上月）")
    parser.add_argument("--overwrite", action="store_true", help="覆盖已存在的 ranking 文件")
    args = parser.parse_args()

    logger.info("===== 历史 ranking 回填开始 =====")

    stock_list = load_stock_pool()
    if stock_list.empty:
        logger.error("股票池为空，请先 --rebuild-pool")
        return
    financial_df = load_financial_data()
    if financial_df.empty:
        logger.error("财务数据为空，请先 --update")
        return
    price_df = load_monthly_price()
    if price_df.empty:
        logger.error("月度价格为空，请先 --backfill-prices")
        return
    industry_df = load_industry_data()

    price_wide = get_month_end_prices(price_df)

    # 起止月份计算
    end = args.end
    if end is None:
        # 默认上个月
        end = (datetime.now() - pd.DateOffset(months=1)).strftime("%Y-%m")
    start = args.start
    if start is None:
        start_dt = pd.Timestamp(end + "-01") - pd.DateOffset(months=args.months - 1)
        start = start_dt.strftime("%Y-%m")

    months = list_available_months(price_wide, start=start, end=end)
    logger.info(f"目标区间 {start} ~ {end}, 可用月份 {len(months)}")
    logger.info(f"股票池 {len(stock_list)} 只, 财务数据 {len(financial_df)} 条")

    generated = 0
    for m in months:
        if backfill_one_month(
            m, stock_list, financial_df, price_wide,
            skip_existing=not args.overwrite,
            industry_df=industry_df,
        ):
            generated += 1

    logger.info(f"===== 完成：新生成 {generated} / {len(months)} 个月 =====")


if __name__ == "__main__":
    main()
