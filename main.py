"""
主入口
用法：
  python main.py --rebuild-pool     # 年度重建股票池（筛选有分红的股票）
  python main.py --update           # 月度更新：采集数据 + 计算排名
  python main.py --rank-only        # 仅重新计算排名（使用已保存的财务数据）
  python main.py --web              # 启动 Web 服务
  python main.py --schedule         # 启动月度定时调度（后台常驻）
  python main.py --update --web     # 月度更新后直接启动 Web
"""
import os
import sys
import time
import logging
import argparse
from datetime import datetime

import pandas as pd

# 确保项目根目录在 PATH 中
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config.settings import HK_STOCK_FILTERS, HISTORY_YEARS
from collectors.hk_price import get_hk_stock_list, get_hk_monthly_price, get_hk_monthly_price_dual, get_hk_current_price, save_monthly_failed, load_monthly_failed_codes
from collectors.hk_financial import collect_financial_data, has_dividend_history, has_share_dilution, has_recent_dividend_or_forward
from collectors.hk_forward_dividend import collect_forward_dividends, get_forward_div_map
from storage.store import (
    save_stock_list, load_stock_list,
    save_stock_pool, load_stock_pool, get_stock_pool_age_days,
    save_monthly_price, load_monthly_price, load_latest_close_map,
    save_financial_data, load_financial_data,
    save_ranking, list_ranking_periods,
    save_industry_data, load_industry_data, upsert_industry_data,
    prune_old_data,
)
from engine.ranker import rank_stocks

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("hk_ranker.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


def _run_stale_scan_and_refresh(stock_list, forward_div_map, tag: str = "stale-scan"):
    """
    B45: 扫描 hk_financial 关键字段 stale 股票并自动补采。
    由 --update / --rebuild-pool 尾部调用；受 STALE_FINANCIAL_SCAN 配置控制。
    """
    from config.settings import STALE_FINANCIAL_SCAN
    if not STALE_FINANCIAL_SCAN.get("enabled", True):
        logger.info(f"{tag}: STALE_FINANCIAL_SCAN.enabled=False，跳过")
        return
    from collectors.incremental import (
        scan_stale_codes,
        incremental_update_financial,
        KEY_FIELDS_STALE,
        load_failed,
        load_empty,
    )
    fin = load_financial_data()
    if fin.empty:
        logger.info(f"{tag}: hk_financial 为空，跳过扫描")
        return
    pool_codes = set(stock_list["code"].astype(str).str.zfill(5).tolist()) if stock_list is not None and not stock_list.empty else None
    threshold = STALE_FINANCIAL_SCAN.get("threshold", 3)
    stale = scan_stale_codes(fin, pool_codes=pool_codes, threshold=threshold)
    logger.info(f"{tag}: 关键字段 NaN>={threshold} 扫描完成，stale={len(stale)} 只 {KEY_FIELDS_STALE}")
    if not stale:
        logger.info(f"{tag}: 未发现 stale ✅")
        return
    logger.warning(f"{tag}: 发现 stale {stale}")
    if not STALE_FINANCIAL_SCAN.get("auto_refresh", True):
        logger.info(f"{tag}: auto_refresh=False，仅报告不补采")
        return
    workers = STALE_FINANCIAL_SCAN.get("workers", 4)
    failed_before = len(load_failed())
    empty_before = len(load_empty())
    logger.info(f"{tag}: 启动自动补采 {len(stale)} 只 (workers={workers})...")
    incremental_update_financial(
        stock_list=stock_list,
        forward_div_map=forward_div_map,
        workers=workers,
        check_dividend_history=False,
        refresh_codes=stale,
    )
    fin_after = load_financial_data()
    stale_after = scan_stale_codes(fin_after, pool_codes=pool_codes, threshold=threshold)
    fixed = len(stale) - len(stale_after)
    logger.info(
        f"{tag}: 补采完成 stale {len(stale)}→{len(stale_after)}（修复 {fixed}）"
        f"; failed {failed_before}→{len(load_failed())}, empty {empty_before}→{len(load_empty())}"
    )
    if stale_after:
        logger.warning(f"{tag}: 仍残留 stale {stale_after}")


"""
主入口
用法：
  python main.py --rebuild-pool     # 年度重建股票池（筛选有分红的股票）
  python main.py --update           # 月度更新：采集数据 + 计算排名
  python main.py --rank-only        # 仅重新计算排名（使用已保存的财务数据）
  python main.py --web              # 启动 Web 服务
  python main.py --schedule         # 启动月度定时调度（后台常驻）
  python main.py --update --web     # 月度更新后直接启动 Web
"""
import os
import sys
import time
import logging
import argparse
from datetime import datetime

import pandas as pd

# 确保项目根目录在 PATH 中
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config.settings import HK_STOCK_FILTERS, HISTORY_YEARS, POOL_REBUILD_INTERVAL_DAYS
from collectors.hk_price import get_hk_stock_list, get_hk_monthly_price, get_hk_monthly_price_dual, get_hk_current_price
from collectors.hk_financial import collect_financial_data, has_dividend_history
from collectors.hk_forward_dividend import collect_forward_dividends, get_forward_div_map
from storage.store import (
    save_stock_list, load_stock_list,
    save_stock_pool, load_stock_pool, get_stock_pool_age_days,
    save_monthly_price, load_monthly_price,
    save_financial_data, load_financial_data,
    save_ranking, list_ranking_periods,
    prune_old_data,
)
from engine.ranker import rank_stocks

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("hk_ranker.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


def run_rebuild_pool(as_of_date: datetime = None, liquidity_window: int = None):
    """
    年度股票池重建：
    1. 获取全量港股列表
    2. 按市值过滤（>= min_market_cap）；对单日不通过的票用近 N 日 max(amount) 复核
    3. 逐只检查 3 年内是否有分红记录
    4. 将符合条件的股票保存为股票池
    5. 采集股票池中所有股票的 5 年历史月度价格

    建议每年运行一次（年初）。

    Args:
        as_of_date: 基准日期，默认为今天。
            为 None 时（默认）保持原有行为：覆盖 hk_stock_list.csv + hk_stock_pool.csv
                + 写入 snapshot{今日}。
            非 None 时（年度调度调用）：仅写入 snapshot{as_of_date}，不覆盖 stock_list/stock_pool，
                也不重新采价（避免污染当前数据）。
        liquidity_window: 流动性窗口复核天数（默认读 settings.LIQUIDITY_WINDOW_DAYS=5）。
            对单日 amount<门槛 的票，用近 N 个交易日 max(close*volume) 复核；只要任一天满足即入池。
            设为 0 则禁用复核（退回到旧行为）。
    """
    from config.settings import LIQUIDITY_WINDOW_DAYS
    from collectors.hk_price import get_hk_recent_amount_window

    if liquidity_window is None:
        liquidity_window = LIQUIDITY_WINDOW_DAYS

    start_time = time.time()
    is_snapshot_only = as_of_date is not None
    if is_snapshot_only:
        logger.info(f"===== 开始历史股票池重建 (as_of={as_of_date:%Y-%m-%d}) =====")
    else:
        logger.info("===== 开始年度股票池重建 =====")

    # ---- Step 1: 获取全量港股列表 ----
    logger.info("Step 1: 获取全量港股列表...")
    stock_list = get_hk_stock_list()
    if stock_list.empty:
        logger.error("获取港股列表失败，退出")
        return
    logger.info(f"全量港股共 {len(stock_list)} 只")

    # ---- Step 2: 市值过滤（仅当前模式下生效，as_of_date 历史模式跳过流动性） ----
    cap_filter = HK_STOCK_FILTERS.get("min_market_cap", 0)
    if not is_snapshot_only and cap_filter and "market_cap" in stock_list.columns:
        spot_pass_mask = stock_list["market_cap"].fillna(0) >= cap_filter
        spot_pass_count = int(spot_pass_mask.sum())
        spot_fail_codes = stock_list.loc[~spot_pass_mask, "code"].astype(str).str.zfill(5).tolist()
        logger.info(
            f"单日快照过滤：通过 {spot_pass_count} / {len(stock_list)} 只，"
            f"待复核 {len(spot_fail_codes)} 只（门槛={cap_filter/1e4:.0f}万港元）"
        )

        # B9: 近 N 日窗口复核（yfinance 拉 daily hist 取 max(close*volume)）
        recovered = set()
        if liquidity_window > 0 and spot_fail_codes:
            recent_amounts = get_hk_recent_amount_window(
                spot_fail_codes, lookback_days=liquidity_window, max_workers=10
            )
            recovered = {c for c, a in recent_amounts.items() if a >= cap_filter}
            logger.info(
                f"近 {liquidity_window} 交易日复核：补回 {len(recovered)} / {len(spot_fail_codes)} 只"
            )
            # 保存复核结果供审计
            try:
                from datetime import datetime as _dt
                audit_path = os.path.join(
                    os.path.dirname(os.path.abspath(__file__)),
                    "data", "csv",
                    f"liquidity_window_{_dt.now():%Y%m%d}.csv",
                )
                audit_df = pd.DataFrame([
                    {"code": c, "max_amount_window": a, "recovered": (a >= cap_filter)}
                    for c, a in recent_amounts.items()
                ])
                audit_df.to_csv(audit_path, index=False, encoding="utf-8-sig")
                logger.info(f"  ✓ 复核明细已保存：{os.path.basename(audit_path)}")
            except Exception as e:
                logger.warning(f"复核明细写入失败：{e}")
        else:
            logger.info("已禁用窗口复核（liquidity_window=0 或无待复核票）")

        # 合并：单日通过 ∪ 窗口复核通过
        codes_normalized = stock_list["code"].astype(str).str.zfill(5)
        keep_mask = spot_pass_mask | codes_normalized.isin(recovered)
        stock_list = stock_list[keep_mask].copy()
        logger.info(
            f"流动性过滤总计保留 {len(stock_list)} 只"
            f"（单日 {spot_pass_count} + 窗口补回 {len(recovered)}）"
        )
    elif is_snapshot_only:
        logger.info("(历史模式) 跳过流动性过滤（AKShare 无历史成交额）")

    # ---- Step 3: 分红历史过滤 ----
    years = HK_STOCK_FILTERS.get("require_dividend_years", 3)
    logger.info(f"Step 3: 检查 {years} 年内分红历史（at as_of={(as_of_date or datetime.now()):%Y-%m-%d}）...")
    pool_rows = []
    fallback_count = 0
    step3_filtered_rows = []
    for i, (_, row) in enumerate(stock_list.iterrows()):
        code = row["code"]
        if (i + 1) % 50 == 0 or i == 0:
            logger.info(f"  进度: [{i+1}/{len(stock_list)}]")
        has_div = (
            has_dividend_history(code, years=years, min_years_with_div=2,
                                  as_of_date=as_of_date)
            if years else True
        )
        if has_div:
            pool_rows.append(row)
        elif years:
            if has_recent_dividend_or_forward(code, as_of_date=as_of_date):
                pool_rows.append(row)
                fallback_count += 1
                logger.info(f"  ✅ [{code}] {row.get('name','')} 分红历史不达标，但近期/预分红入选")
            else:
                step3_filtered_rows.append(row.to_dict())

    if not pool_rows:
        logger.error("过滤后股票池为空，请检查过滤条件")
        return

    stock_pool_div = pd.DataFrame(pool_rows).reset_index(drop=True)
    if fallback_count > 0:
        logger.info(f"分红历史过滤后：{len(stock_pool_div)} 只（其中 {fallback_count} 只靠近期分红/预分红入选）")
    else:
        logger.info(f"分红历史过滤后：{len(stock_pool_div)} 只（过滤掉 {len(stock_list) - len(stock_pool_div)} 只）")

    # 保存 Step 3 中间结果（仅当前模式）
    if not is_snapshot_only:
        step3_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "csv", "step3_dividend.csv")
        stock_pool_div.to_csv(step3_path, index=False, encoding="utf-8-sig")
        logger.info(f"  ✓ 中间结果已保存: step3_dividend.csv")

        if step3_filtered_rows:
            df_step3_filtered = pd.DataFrame(step3_filtered_rows)
            step3_filtered_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "csv", "step3_filtered.csv")
            df_step3_filtered.to_csv(step3_filtered_path, index=False, encoding="utf-8-sig")
            logger.info(f"  ✓ 已保存 Step3 过滤明细: step3_filtered.csv（共 {len(step3_filtered_rows)} 只）")

    # ---- Step 4: 合股/增发/配售过滤 ----
    logger.info(f"Step 4: 检查 3 年内是否有合股/增发/配售行为（at as_of={(as_of_date or datetime.now()):%Y-%m-%d}）...")
    logger.info(f"  共 {len(stock_pool_div)} 只待检查")
    no_dilution_rows = []
    dilution_filtered_rows = []
    dilution_count = 0
    ambiguous_cases = []
    for i, (_, row) in enumerate(stock_pool_div.iterrows()):
        code = row["code"]
        name = row.get("name", "")
        if (i + 1) % 50 == 0 or i == 0:
            logger.info(f"  进度: [{i+1}/{len(stock_pool_div)}]")
        should_filter, max_decrease = has_share_dilution(code, years=3, as_of_date=as_of_date)
        if should_filter:
            dilution_count += 1
            reason = "增发(股份增加≥8%)" if max_decrease < 0.08 else "合股(股份减少≥50%)"
            row_dict = row.to_dict()
            row_dict["filter_reason"] = reason
            row_dict["max_decrease_pct"] = f"{max_decrease*100:.0f}%"
            dilution_filtered_rows.append(row_dict)
            logger.debug(f"  [{code}] {name} {reason}，剔除")
        else:
            no_dilution_rows.append(row)
            if max_decrease >= 0.10:
                pct_str = f"{max_decrease*100:.0f}%"
                ambiguous_cases.append({"code": code, "name": name, "max_decrease_pct": pct_str})
                logger.info(f"  ⚠ [{code}] {name} 股份数减少 {pct_str}（10%~50%灰色地带），保留待验证")

    stock_pool = pd.DataFrame(no_dilution_rows).reset_index(drop=True)
    logger.info(f"合股/增发过滤后：股票池共 {len(stock_pool)} 只（剔除 {dilution_count} 只有稀释行为的股票）")

    # Step 4 过滤明细仅当前模式落盘
    if not is_snapshot_only and dilution_filtered_rows:
        df_filtered = pd.DataFrame(dilution_filtered_rows)
        step4_filtered_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "csv", "step4_dilution_filtered.csv")
        df_filtered.to_csv(step4_filtered_path, index=False, encoding="utf-8-sig")
        logger.info(f"  ✓ 已保存 Step4 过滤明细: step4_dilution_filtered.csv（共 {len(dilution_filtered_rows)} 只）")

    if not is_snapshot_only and ambiguous_cases:
        import json
        ambiguous_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "ambiguous_share_decrease.json")
        os.makedirs(os.path.dirname(ambiguous_path), exist_ok=True)
        with open(ambiguous_path, "w", encoding="utf-8") as f:
            json.dump(ambiguous_cases, f, ensure_ascii=False, indent=2)
        logger.info(f"⚠ 发现 {len(ambiguous_cases)} 只股份数减少 10%~30% 的股票（灰色地带），"
                    f"已保留在股票池中，详情见 data/ambiguous_share_decrease.json")

    # ---- Step 5: 保存股票池快照 ----
    from storage.store import save_stock_pool_snapshot
    snapshot_date = as_of_date or datetime.now()
    save_stock_pool_snapshot(stock_pool, snapshot_date)

    if not is_snapshot_only:
        # 当前模式：覆盖 hk_stock_pool.csv + 同步 stock_list
        save_stock_pool(stock_pool)
        save_stock_list(stock_pool)

        # ---- Step 6: 采集 5 年历史月度价格（仅当前模式）----
        logger.info("Step 6: 采集股票池月度价格（5年历史）...")
        today = datetime.now()
        end_date = today.strftime("%Y%m%d")
        start_date = (today - pd.DateOffset(years=HISTORY_YEARS)).strftime("%Y%m%d")
        all_price_dfs = []

        for i, (_, row) in enumerate(stock_pool.iterrows()):
            code = row["code"]
            logger.info(f"  [{i+1}/{len(stock_pool)}] {code} {row.get('name', '')}")
            df = get_hk_monthly_price_dual(code, start_date, end_date)
            if not df.empty:
                all_price_dfs.append(df)

        if all_price_dfs:
            combined_price = pd.concat(all_price_dfs, ignore_index=True)
            save_monthly_price(combined_price)

        prune_old_data(HISTORY_YEARS)

    elapsed = time.time() - start_time
    logger.info(f"===== 股票池重建完成，共 {len(stock_pool)} 只，总耗时 {elapsed/60:.1f} 分钟 =====")
    if not is_snapshot_only:
        print(f"\n股票池已构建完成：{len(stock_pool)} 只股票（当日成交额 >= {cap_filter/1e4:.0f}万港元 且 {years}年内有分红记录）")
        print("请每年运行一次 --rebuild-pool 更新股票池。")

    # ---- Step 7: [B45] 财务数据 stale 扫描 + 自动补采（仅当前模式；snapshot-only 跳过）----
    if not is_snapshot_only:
        try:
            from collectors.hk_forward_dividend import get_forward_div_map
            fwd = get_forward_div_map()
        except Exception as e:
            logger.warning(f"预分红 map 加载失败: {e}")
            fwd = {}
        _run_stale_scan_and_refresh(stock_pool, fwd, tag="Step 7")


def run_backfill_prices(months: int = None, workers: int = 4):
    """
    历史月度价格回填（用于回测）。
    遍历股票池，对每只股票补齐近 N 个月的复权价格，与已有数据按 (code, date) 去重合并。
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from config.settings import HISTORY_BACKFILL_MONTHS
    months = months or HISTORY_BACKFILL_MONTHS
    logger.info(f"===== 历史价格回填：近 {months} 个月 =====")

    stock_list = load_stock_pool()
    if stock_list.empty:
        logger.error("股票池为空，请先 --rebuild-pool")
        return

    today = datetime.now()
    end_date = today.strftime("%Y%m%d")
    start_date = (today - pd.DateOffset(months=months)).strftime("%Y%m%d")
    logger.info(f"窗口 {start_date} ~ {end_date}, 股票数 {len(stock_list)}")

    codes = stock_list["code"].astype(str).str.zfill(5).tolist()

    def fetch(code):
        try:
            return get_hk_monthly_price_dual(code, start_date, end_date)
        except Exception as e:
            logger.warning(f"  {code} 价格采集失败: {e}")
            return pd.DataFrame()

    all_dfs = []
    with ThreadPoolExecutor(max_workers=workers) as exe:
        futures = {exe.submit(fetch, c): c for c in codes}
        done = 0
        for fut in as_completed(futures):
            done += 1
            df = fut.result()
            if not df.empty:
                all_dfs.append(df)
            if done % 50 == 0 or done == len(codes):
                logger.info(f"  [{done}/{len(codes)}]")

    if all_dfs:
        combined = pd.concat(all_dfs, ignore_index=True)
        save_monthly_price(combined)
        logger.info(f"回填完成，新增/更新 {len(combined)} 行")
    else:
        logger.warning("无可保存数据")


def run_update(force_refresh: bool = False, refresh_codes: list = None,
               ttl_days: int = None, workers: int = 4,
               retry_empty: bool = False):
    """
    月度数据更新（默认增量）：
    1. 加载已有股票池（不重新筛选）
    2. 更新月度价格（最近数据，按日期去重）
    3. 爬取预分红公告
    4. 财务数据 TTL 增量刷新（仅过期/财年到期/新进股票被重新采集）
    5. 计算排名并保存

    Args:
        force_refresh: True 时强制全量刷新所有财务数据
        refresh_codes: 仅刷新指定股票（与 force_refresh 互斥）
        ttl_days: 财务数据复用窗口（默认 settings.FINANCIAL_REFRESH_TTL_DAYS）
        workers: 并发数

    依赖：先执行 --rebuild-pool 初始化股票池。
    """
    from collectors.incremental import incremental_update_financial
    from config.settings import FINANCIAL_REFRESH_TTL_DAYS

    if ttl_days is None:
        ttl_days = FINANCIAL_REFRESH_TTL_DAYS

    start_time = time.time()
    logger.info(
        f"===== 开始月度数据更新 (force={force_refresh}, "
        f"codes={'spec' if refresh_codes else 'auto'}, ttl={ttl_days}d) ====="
    )

    # ---- 加载股票池 ----
    age_days = get_stock_pool_age_days()
    if age_days < 0:
        logger.error("股票池不存在！请先运行: python main.py --rebuild-pool")
        print("\n错误：股票池不存在，请先运行:\n  python main.py --rebuild-pool\n")
        return
    if age_days > POOL_REBUILD_INTERVAL_DAYS:
        logger.warning(f"股票池已有 {age_days} 天未更新（建议每 {POOL_REBUILD_INTERVAL_DAYS} 天重建一次），"
                       f"建议运行: python main.py --rebuild-pool")

    stock_list = load_stock_pool()
    logger.info(f"已加载股票池：{len(stock_list)} 只（构建于 {age_days} 天前）")

    # ---- Step 2: 月度价格（增量更新，仅补充最近数据）----
    logger.info("Step 2: 更新月度价格...")
    today = datetime.now()
    end_date = today.strftime("%Y%m%d")
    # 仅补充最近 2 个月的数据（减少重复请求）
    start_date = (today - pd.DateOffset(months=2)).strftime("%Y%m%d")
    all_price_dfs = []

    import os as _os
    if _os.environ.get("SKIP_PRICE") == "1":
        logger.info("  SKIP_PRICE=1 → 跳过月度价格更新")
    else:
        # B51: 上次拉取失败的 code（attempts < 死码阈值）本次优先重试
        retry_codes = set(load_monthly_failed_codes())
        if retry_codes:
            logger.info(f"  本次将重试 {len(retry_codes)} 只上次月度价格拉取失败的股票")

        for i, (_, row) in enumerate(stock_list.iterrows()):
            code = str(row["code"]).zfill(5)
            if (i + 1) % 50 == 0 or i == 0:
                logger.info(f"  [{i+1}/{len(stock_list)}] {code} {row.get('name', '')}")
            df = get_hk_monthly_price_dual(code, start_date, end_date)
            if not df.empty:
                all_price_dfs.append(df)

        if all_price_dfs:
            combined_price = pd.concat(all_price_dfs, ignore_index=True)
            save_monthly_price(combined_price)

        save_monthly_failed()   # B51: 持久化本月失败记录，供下次启动自动重试

    # ---- Step 2b: [B13] 新入池/价格不足股票 5 年回填 ----
    # 触发条件：池内 code 在 hk_monthly_price.csv 中的月数 < HISTORY_YEARS*12*0.8
    # （容忍 20% 缺失/停牌；新股、长期停牌均会触发）
    logger.info("Step 2b: 检测新入池/价格不足股票...")
    if _os.environ.get("SKIP_PRICE") == "1":
        logger.info("  SKIP_PRICE=1 → 跳过 Step 2b")
    else:
        try:
         from concurrent.futures import ThreadPoolExecutor, as_completed
         from storage.store import load_monthly_price as _load_mp
         mp_df = _load_mp()
         threshold = int(HISTORY_YEARS * 12 * 0.8)
         if mp_df.empty:
             counts = pd.Series(dtype=int)
         else:
             mp_df["code"] = mp_df["code"].astype(str).str.zfill(5)
             counts = mp_df.groupby("code").size()
         pool_codes_zf = stock_list["code"].astype(str).str.zfill(5).tolist()
         new_or_short = [
             c for c in pool_codes_zf
             if int(counts.get(c, 0)) < threshold
         ]
         if new_or_short:
             bf_start = (today - pd.DateOffset(years=HISTORY_YEARS)).strftime("%Y%m%d")
             logger.info(
                 f"  发现 {len(new_or_short)} 只新入池/价格不足，回填 {bf_start}~{end_date} "
                 f"(阈值 {threshold} 月，复用 {len(pool_codes_zf) - len(new_or_short)} 只)"
             )
             bf_dfs = []
             def _fetch_bf(code):
                 try:
                     return get_hk_monthly_price_dual(code, bf_start, end_date)
                 except Exception as e:
                     logger.warning(f"  价格回填失败 {code}: {e}")
                     return pd.DataFrame()
             with ThreadPoolExecutor(max_workers=workers) as exe:
                 futs = {exe.submit(_fetch_bf, c): c for c in new_or_short}
                 done = 0
                 for fut in as_completed(futs):
                     done += 1
                     df = fut.result()
                     if not df.empty:
                         bf_dfs.append(df)
                     if done % 25 == 0 or done == len(new_or_short):
                         logger.info(f"  价格回填 [{done}/{len(new_or_short)}]")
             if bf_dfs:
                 save_monthly_price(pd.concat(bf_dfs, ignore_index=True))
                 logger.info(f"  价格回填完成，新增/更新 {sum(len(d) for d in bf_dfs)} 行")
         else:
             logger.info("  无新入池/价格不足股票，跳过回填")
        except Exception as e:
            logger.warning(f"Step 2b 价格回填异常（不影响主流程）: {e}")

    prune_old_data(HISTORY_YEARS)

    # ---- Step 3a: 预分红爬虫 ----
    logger.info("Step 3a: 爬取预分红公告（港交所披露易）...")
    codes = stock_list["code"].tolist()
    import os as _os
    if _os.environ.get("SKIP_FORWARD_DIV") == "1":
        logger.info("  SKIP_FORWARD_DIV=1 → 跳过爬虫，复用已有缓存")
    else:
        collect_forward_dividends(codes, lookback_days=180)
    forward_div_map = get_forward_div_map()
    logger.info(f"预分红数据：找到 {len(forward_div_map)} 只股票有预告")

    # ---- Step 3b: 财务数据（增量）----
    logger.info("Step 3b: 采集财务数据（增量 TTL 模式）...")

    # 当前价：优先实时采集；失败时回退到月度价格表最新 close（B51）
    logger.info("  采集当前价格...")
    latest_close = load_latest_close_map()
    current_price_map = {}
    for i, (_, row) in enumerate(stock_list.iterrows()):
        code = str(row["code"]).zfill(5)
        try:
            cp = get_hk_current_price(code)
            if cp is None:
                cp = latest_close.get(code)   # 实时价失败 → 回退月度价格缓存
            current_price_map[code] = cp
        except Exception as e:
            logger.debug(f"current_price {code}: {e}")
            current_price_map[code] = latest_close.get(code)
        if (i + 1) % 100 == 0:
            logger.info(f"  current_price [{i+1}/{len(stock_list)}]")

    financial_df = incremental_update_financial(
        stock_list=stock_list,
        forward_div_map=forward_div_map,
        ttl_days=ttl_days,
        workers=workers,
        force_refresh=force_refresh,
        refresh_codes=refresh_codes,
        retry_empty=retry_empty,
        check_dividend_history=False,
        current_price_map=current_price_map,
    )

    # 对未刷新的旧记录也用最新价格覆盖（current_price 应永远是最新的）
    if current_price_map:
        financial_df["code"] = financial_df["code"].astype(str).str.zfill(5)
        financial_df["current_price"] = financial_df["code"].map(current_price_map).fillna(
            financial_df.get("current_price")
        )
        # 直接覆盖保存（不会重置 updated_at，因为 save_financial_data 已改为只填充缺失）
        save_financial_data(financial_df)

    # ---- Step 3c: 行业归属（增量补采，相对静态）----
    logger.info("Step 3c: 增量采集所属行业...")
    try:
        from collectors.hk_industry import collect_industries
        existing_ind = load_industry_data()
        existing_ind["code"] = existing_ind["code"].astype(str).str.zfill(5) if not existing_ind.empty else existing_ind
        if not existing_ind.empty:
            have_codes = set(
                existing_ind.loc[
                    existing_ind["industry"].astype(str).str.strip().replace("nan", "") != "",
                    "code",
                ]
            )
        else:
            have_codes = set()
        missing_codes = [c for c in stock_list["code"].astype(str).str.zfill(5) if c not in have_codes]
        if missing_codes:
            logger.info(f"  待采集 {len(missing_codes)} 只（已缓存 {len(have_codes)} 只）")
            ind_map = collect_industries(missing_codes, max_workers=8)
            upsert_industry_data(ind_map)
        else:
            logger.info("  所有股票行业已缓存，跳过")
    except Exception as e:
        logger.warning(f"行业采集失败（不影响主流程）: {e}")

    # ---- Step 3d: [B45] 财务数据 stale 扫描 + 自动补采 ----
    _run_stale_scan_and_refresh(stock_list, forward_div_map, tag="Step 3d")
    # 补采后重新加载最新财务数据供 rank 使用
    financial_df = load_financial_data()

    # ---- Step 4: 计算排名 ----
    run_rank_only(stock_list=stock_list, financial_df=financial_df)

    elapsed = time.time() - start_time
    logger.info(f"===== 月度更新完成，总耗时 {elapsed/60:.1f} 分钟 =====")


def run_rank_only(stock_list: pd.DataFrame = None, financial_df: pd.DataFrame = None):
    """仅重新计算排名（使用已保存数据）"""
    if stock_list is None:
        # 优先从股票池加载，回退到旧的 stock_list
        stock_list = load_stock_pool()
        if stock_list.empty:
            stock_list = load_stock_list()
    if financial_df is None:
        financial_df = load_financial_data()

    if stock_list.empty or financial_df.empty:
        logger.error("数据不足，无法计算排名")
        return

    # B51: current_price 来源优先级 = 月度价格最新 close > financial current_price > pool price
    # 修复历史 bug：旧逻辑无条件用股票池 price（可能滞后数月）作 current_price。
    stock_list = stock_list.copy()
    stock_list["code"] = stock_list["code"].astype(str).str.zfill(5)

    # 1) 主来源：月度价格表最新 close（最新鲜、已持久化缓存）
    latest_close = load_latest_close_map()
    stock_list["current_price"] = stock_list["code"].map(latest_close)

    # 2) 兜底：financial current_price（实时采集，覆盖月度价缺失/异常的 code）
    if "current_price" in financial_df.columns:
        fin = financial_df.copy()
        fin["code"] = fin["code"].astype(str).str.zfill(5)
        fin_price_map = (
            fin.dropna(subset=["current_price"])
            .set_index("code")["current_price"]
            .to_dict()
        )
        cur = pd.to_numeric(stock_list["current_price"], errors="coerce")
        need_fill = cur.isna() | (cur <= 0)
        if need_fill.any():
            stock_list.loc[need_fill, "current_price"] = (
                stock_list.loc[need_fill, "code"].map(fin_price_map)
            )
        # 从 financial_df 移除 current_price，避免 merge 时的列名冲突
        financial_df = financial_df.drop(columns=["current_price"])

    # 3) 最后兜底：pool price（最老，仅当月度和 financial 都缺失时使用）
    if "price" in stock_list.columns:
        cur = pd.to_numeric(stock_list["current_price"], errors="coerce")
        need_fill = cur.isna() | (cur <= 0)
        if need_fill.any():
            stock_list.loc[need_fill, "current_price"] = pd.to_numeric(
                stock_list.loc[need_fill, "price"], errors="coerce"
            )

    industry_df = load_industry_data()

    # B14: 不再在 ranking 阶段做 min_market_cap 过滤。
    # stock_pool 在年度建池时已用近 5 日 max(amount) 复核（见 run_rebuild_pool），
    # 若此处再用单日 amount 过滤，会把"靠 5 日复核入池"的低成交日股票误剔除（如 01036 万科海外）。
    ranked = rank_stocks(
        stock_list=stock_list,
        financial_df=financial_df,
        max_net_debt_ratio=HK_STOCK_FILTERS.get("max_net_debt_ratio"),
        max_total_liability_ratio=HK_STOCK_FILTERS.get("max_total_liability_ratio"),
        max_debt_to_equity=HK_STOCK_FILTERS.get("max_debt_to_equity"),
        industry_df=industry_df,
    )

    period = datetime.now().strftime("%Y-%m")
    save_ranking(ranked, period)
    logger.info(f"排名已保存，期间: {period}, 共 {len(ranked)} 只")

    # 打印前20名
    print("\n" + "="*80)
    print(f"港股回报率排名 TOP 20 ({period})")
    print("="*80)
    top20 = ranked.head(20)
    pd.set_option("display.max_columns", None)
    pd.set_option("display.width", 200)
    print(top20[["rank", "code", "name", "return_rate", "current_price",
                  "fy_div_per_share", "forward_div_per_share", "buyback_per_share",
                  "annual_dilution_rate"]].to_string(index=False))
    print("="*80 + "\n")


def _print_summary(report: dict):
    """终端输出 TOP-N 回测摘要"""
    s = report.get("summary", {})
    p = report.get("params", {})
    print()
    print("=" * 60)
    print(f"  TOP-{p.get('top_n')} 月度调仓回测  {p.get('start')} ~ {p.get('end')}")
    print(f"  权重={p.get('weight_scheme')}, 单边成本={p.get('transaction_cost_bps')} bps")
    print("-" * 60)
    for k in ["n_months", "cumulative_return", "annualized_return",
               "annualized_volatility", "sharpe_ratio", "calmar_ratio",
               "win_rate", "max_drawdown", "information_ratio"]:
        v = s.get(k)
        if v is None:
            continue
        if isinstance(v, float):
            print(f"  {k:<24} {v:.4f}")
        else:
            print(f"  {k:<24} {v}")
    print("=" * 60)
    print()


def _print_quintile_summary(report: dict):
    """终端输出五分位摘要"""
    p = report.get("params", {})
    rho = report.get("spearman_monotonicity", float("nan"))
    print()
    print("=" * 60)
    print(f"  {p.get('n_quantiles')} 分位月度调仓回测  {p.get('start')} ~ {p.get('end')}")
    print(f"  权重={p.get('weight_scheme')}, 成本={p.get('transaction_cost_bps')} bps")
    print(f"  Spearman 单调性: {rho:.4f}")
    print("-" * 60)
    qs = report.get("quintile_summaries", {})
    for label in sorted(qs.keys()):
        s = qs[label]
        ar = s.get("annualized_return")
        sr = s.get("sharpe_ratio")
        mdd = s.get("max_drawdown")
        print(f"  {label}: 年化 {ar*100:>7.2f}%   Sharpe {sr:>6.3f}   MDD {mdd*100:>7.2f}%")
    ls = report.get("long_short")
    if ls:
        s = ls.get("summary", {})
        print(f"  {ls.get('label')}: 年化 {s.get('annualized_return', 0)*100:>7.2f}%   Sharpe {s.get('sharpe_ratio', 0):>6.3f}")
    print("=" * 60)
    print()


def run_web():
    """启动 Web 服务"""
    logger.info("启动 Web 服务...")
    from web.app import app
    from config.settings import WEB_PORT, WEB_DEBUG
    app.run(host="0.0.0.0", port=WEB_PORT, debug=WEB_DEBUG)


def run_scheduler():
    """启动定时调度器"""
    from scheduler.tasks import start_scheduler
    start_scheduler()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="港股回报率排名工具")
    parser.add_argument("--rebuild-pool", action="store_true",
                        help="年度重建股票池（全量港股 → 市值过滤 → 分红历史过滤）")
    parser.add_argument("--liquidity-window", type=int, default=None,
                        help="（配合 --rebuild-pool）近 N 个交易日 max(amount) 复核窗口，默认读配置 LIQUIDITY_WINDOW_DAYS=5；设 0 禁用窗口复核")
    parser.add_argument("--backfill-pools", action="store_true",
                        help="历史股票池回填：为过去 N 年的每年末（12-31）"
                             "构建独立池快照（严格策略：仅分红+合股增发可回溯条件）")
    parser.add_argument("--years-back", type=int, default=5,
                        help="（配合 --backfill-pools）回填多少年（默认 5 + 上年末，共 6 个）")
    parser.add_argument("--pool-as-of", type=str, default=None,
                        help="（配合 --backfill-pools）只回填某一基准日 YYYY-MM-DD")
    parser.add_argument("--update", action="store_true",
                        help="月度数据更新（基于已有股票池采集财务数据并计算排名）")
    parser.add_argument("--force-refresh", action="store_true",
                        help="（配合 --update）强制全量刷新财务数据，等价旧的全量模式")
    parser.add_argument("--retry-empty", action="store_true",
                        help="（配合 --update）把 empty sidecar 中的码也加入刷新（默认只刷 new+fail+TTL过期）")
    parser.add_argument("--refresh-codes", type=str, default=None,
                        help="（配合 --update）仅刷新指定股票代码（逗号分隔，如 00308,00700）")
    parser.add_argument("--ttl-days", type=int, default=None,
                        help="（配合 --update）财务数据复用 TTL 天数（默认 30）")
    parser.add_argument("--backfill-prices", action="store_true",
                        help="历史月度价格回填（用于回测）")
    parser.add_argument("--months", type=int, default=None,
                        help="（配合 --backfill-prices / --backfill-rankings）回填月数")
    parser.add_argument("--backfill-rankings", action="store_true",
                        help="历史 ranking 回填（用当前财务因子 + 历史月底价，默认 24 个月）")
    parser.add_argument("--update-benchmark", action="store_true",
                        help="更新基准指数 HSI（yfinance）")
    parser.add_argument("--backtest", action="store_true",
                        help="跑 TOP-N 月度调仓回测")
    parser.add_argument("--backtest-quintile", action="store_true",
                        help="跑五分位回测（默认 5）")
    parser.add_argument("--backtest-start", type=str, default=None, help="回测起始 YYYY-MM")
    parser.add_argument("--backtest-end", type=str, default=None, help="回测结束 YYYY-MM")
    parser.add_argument("--top-n", type=int, default=None, help="TOP-N 默认 20")
    parser.add_argument("--weight", type=str, default=None, choices=["equal", "score", "rank"],
                        help="权重方案（默认 equal）")
    parser.add_argument("--cost-bps", type=float, default=None,
                        help="交易成本 bps（单边）默认 15")
    parser.add_argument("--n-quantiles", type=int, default=None,
                        help="分位数（默认 5）")
    parser.add_argument("--rank-only", action="store_true", help="仅重新计算排名")
    parser.add_argument("--scrape-forward-div", action="store_true", help="仅运行预分红爬虫（港交所披露易）")
    parser.add_argument("--web", action="store_true", help="启动 Web 服务")
    parser.add_argument("--schedule", action="store_true", help="启动月度定时调度")
    args = parser.parse_args()

    if args.rebuild_pool:
        run_rebuild_pool(liquidity_window=args.liquidity_window)

    if args.backfill_pools:
        from scripts.backfill_pools import run_backfill as run_backfill_pools
        only_as_of = None
        if args.pool_as_of:
            only_as_of = datetime.strptime(args.pool_as_of, "%Y-%m-%d")
        run_backfill_pools(
            years_back=args.years_back,
            only_as_of=only_as_of,
            skip_existing=True,
        )

    if args.scrape_forward_div:
        sl = load_stock_pool()
        if sl.empty:
            sl = load_stock_list()
        if sl.empty:
            logger.error("未找到股票池，请先运行: python main.py --rebuild-pool")
        else:
            codes = sl["code"].tolist()
            collect_forward_dividends(codes, lookback_days=180)

    if args.update:
        codes_arg = None
        if args.refresh_codes:
            codes_arg = [c.strip().zfill(5) for c in args.refresh_codes.split(",") if c.strip()]
        run_update(
            force_refresh=args.force_refresh,
            refresh_codes=codes_arg,
            ttl_days=args.ttl_days,
            retry_empty=args.retry_empty,
        )

    if args.backfill_prices:
        run_backfill_prices(months=args.months)

    if args.backfill_rankings:
        # 调用 scripts/backfill_rankings.py 的 main 逻辑
        from scripts.backfill_rankings import (
            list_available_months, get_month_end_prices, backfill_one_month,
        )
        sp = load_stock_pool()
        fin = load_financial_data()
        from storage.store import load_monthly_price
        prices = load_monthly_price()
        if sp.empty or fin.empty or prices.empty:
            logger.error("股票池/财务/价格任一为空，请先 --rebuild-pool / --update / --backfill-prices")
        else:
            wide = get_month_end_prices(prices)
            from datetime import datetime
            end = (datetime.now() - pd.DateOffset(months=1)).strftime("%Y-%m")
            n = args.months or 24
            start_dt = pd.Timestamp(end + "-01") - pd.DateOffset(months=n - 1)
            start = start_dt.strftime("%Y-%m")
            months = list_available_months(wide, start=start, end=end)
            logger.info(f"回填 ranking 区间 {start}~{end}, 可用 {len(months)} 个月")
            for m in months:
                backfill_one_month(m, sp, fin, wide, skip_existing=True)

    if args.update_benchmark:
        from backtest.benchmark import update_benchmark
        update_benchmark()

    if args.backtest or args.backtest_quintile:
        from backtest.runner import run_topn_with_report, run_quintile_with_report
        from config.settings import (
            BACKTEST_DEFAULT_TOP_N, BACKTEST_DEFAULT_WEIGHT,
            BACKTEST_TRANSACTION_COST_BPS, BACKTEST_QUANTILE_COUNT,
        )
        if not args.backtest_start or not args.backtest_end:
            logger.error("请同时提供 --backtest-start 和 --backtest-end（YYYY-MM）")
        else:
            top_n = args.top_n or BACKTEST_DEFAULT_TOP_N
            weight = args.weight or BACKTEST_DEFAULT_WEIGHT
            cost = args.cost_bps if args.cost_bps is not None else BACKTEST_TRANSACTION_COST_BPS
            n_q = args.n_quantiles or BACKTEST_QUANTILE_COUNT
            if args.backtest:
                logger.info(f"跑 TOP-{top_n} 回测: {args.backtest_start} ~ {args.backtest_end}, 权重={weight}, 成本={cost}bps")
                report = run_topn_with_report(
                    args.backtest_start, args.backtest_end,
                    top_n=top_n, weight_scheme=weight,
                    transaction_cost_bps=cost,
                )
                _print_summary(report)
            if args.backtest_quintile:
                logger.info(f"跑 {n_q} 分位回测: {args.backtest_start} ~ {args.backtest_end}, 权重={weight}")
                report = run_quintile_with_report(
                    args.backtest_start, args.backtest_end,
                    n_quantiles=n_q, weight_scheme=weight,
                    transaction_cost_bps=cost,
                )
                _print_quintile_summary(report)

    if args.rank_only:
        run_rank_only()

    if args.schedule:
        run_scheduler()
    elif args.web:
        run_web()
    elif not any([args.rebuild_pool, args.update, args.rank_only,
                  args.scrape_forward_div, args.backfill_prices,
                  args.backfill_rankings, args.update_benchmark,
                  args.backtest, args.backtest_quintile,
                  args.backfill_pools]):
        parser.print_help()
