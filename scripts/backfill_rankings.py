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
from backtest.point_in_time import load_dividend_history, ttm_dividend_matrix
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
                        industry_df: pd.DataFrame = None,
                        pit_ttm: pd.DataFrame = None,
                        pit_prev: pd.DataFrame = None) -> bool:
    """对单月生成 ranking。返回是否实际生成（False=跳过）

    Args:
        use_pool_snapshot: True 时按 period 所属年份选 snapshot 池过滤；
                          False 时使用传入的 stock_list（向后兼容）
        pit_ttm: point-in-time TTM 分红矩阵（index=code, columns=YYYY-MM）。
                 传入时用「时点前12个月除息分红」替换当前快照 fy_div_per_share，
                 根治前视偏差。
        pit_prev: point-in-time 上年 TTM 分红矩阵，替换 prev_year_div_per_share。
    """
    if pit_ttm is not None:
        target_path = os.path.join(_store.CSV_DIR, f"hk_ranking_pit_{period}.csv")
    else:
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

    # PIT: 用 point-in-time TTM 分红替换当前快照（根治前视偏差）
    if pit_ttm is not None and period in pit_ttm.columns:
        ttm = pit_ttm[period]
        fin["fy_div_per_share"] = fin["code"].map(ttm).fillna(0.0)
        if pit_prev is not None and period in pit_prev.columns:
            prev = pit_prev[period]
            fin["prev_year_div_per_share"] = fin["code"].map(prev).fillna(0.0)
        logger.debug(f"  {period} 已应用 point-in-time 分红（时点 TTM 替换快照）")

    # 替换 current_price 为该月月底价（缺失则保留原值，避免误删）
    new_price = fin["code"].map(month_prices)
    coverage = new_price.notna().sum() / max(len(fin), 1)

    # B37: 当月无价时，用 price_wide 中该 code 最近一个月的价格作为 fallback（最新可用价）
    # 背景：AKShare 限流导致当月部分 code 拉不到价；用最近月度价保持股票可参与排名
    other_periods = [p for p in price_wide.index if p != period]
    fallback_price = pd.Series(dtype=float, index=fin.index)
    if other_periods:
        latest_prior = max(other_periods)
        prior_prices = price_wide.loc[latest_prior]
        fallback_price = fin["code"].map(prior_prices)

    fin["current_price"] = new_price.fillna(fallback_price).fillna(fin.get("current_price"))

    # 只剔除「当月无价 + fallback 也无价 + 原始 current_price 也无价」的股票
    has_any_price = new_price.notna() | fallback_price.notna() | fin["current_price"].notna()
    used_new = new_price.notna().sum()
    used_fallback = (new_price.isna() & fallback_price.notna()).sum()
    used_orig = (new_price.isna() & fallback_price.isna() & fin["current_price"].notna()).sum()
    logger.info(
        f"  {period} 当月月底价: {used_new} 只; "
        f"fallback 至 {latest_prior if other_periods else 'N/A'}: {used_fallback} 只; "
        f"原始 current_price: {used_orig} 只"
    )
    fin = fin[has_any_price].reset_index(drop=True)
    if fin.empty:
        logger.warning(f"  {period} 无任何股票有价格（当月+fallback+原始 均缺），跳过")
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

    if pit_ttm is not None:
        # PIT 模式：写独立文件（hk_ranking_pit_{period}.csv），便于与旧回填对比评测
        pit_path = os.path.join(_store.CSV_DIR, f"hk_ranking_pit_{period}.csv")
        ranked.to_csv(pit_path, index=False, encoding="utf-8-sig")
        logger.info(f"  {period} ✓ PIT 生成 {len(ranked)} 行 → {os.path.basename(pit_path)}")
    else:
        save_ranking(ranked, period)
        logger.info(f"  {period} ✓ 生成 {len(ranked)} 行（池 {len(pool_codes)}, 价格覆盖 {coverage:.0%}）")
    return True


def main():
    parser = argparse.ArgumentParser(description="历史 ranking 回填")
    parser.add_argument("--months", type=int, default=24, help="回填最近 N 个月（默认 24）")
    parser.add_argument("--start", type=str, default=None, help="起始 YYYY-MM")
    parser.add_argument("--end", type=str, default=None, help="结束 YYYY-MM（默认今天的上月）")
    parser.add_argument("--overwrite", action="store_true", help="覆盖已存在的 ranking 文件")
    parser.add_argument("--pit", action="store_true",
                        help="point-in-time 模式：用时点 TTM 分红替换当前快照 fy_div_per_share，"
                             "根治前视偏差，输出 hk_ranking_pit_{period}.csv")
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

    # PIT: 构建 point-in-time 分红矩阵（时点 TTM / 上年 TTM）
    pit_ttm = pit_prev = None
    if args.pit:
        hist = load_dividend_history()
        if hist.empty:
            logger.error("分红历史为空，请先采集 point-in-time 数据（backtest.point_in_time）")
            return
        all_months = list_available_months(price_wide)
        month_ends = [pd.Timestamp(m + "-01") + pd.offsets.MonthEnd(0) for m in all_months]
        pit_ttm = ttm_dividend_matrix(hist, month_ends, months=12)
        pit_prev = ttm_dividend_matrix(hist, month_ends, months=24) - pit_ttm
        logger.info(f"point-in-time 分红矩阵已构建：{len(pit_ttm.index)} 只 × {len(pit_ttm.columns)} 期")

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
            pit_ttm=pit_ttm,
            pit_prev=pit_prev,
        ):
            generated += 1

    logger.info(f"===== 完成：新生成 {generated} / {len(months)} 个月 =====")


if __name__ == "__main__":
    main()
