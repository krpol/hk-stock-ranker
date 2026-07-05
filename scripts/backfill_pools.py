"""
历史股票池回填（年度独立池，严格策略）

为指定的若干基准日（默认每年 12-31）各生成一个独立股票池快照，输出到
data/csv/pools/hk_stock_pool_{YYYY-MM-DD}.csv。

严格策略只采用可精确回溯的过滤条件：
1. 全量列表：今日 AKShare 列表（无法回溯历史列表）
2. 分红历史：3 年内至少 2 年（fallback：近 12 月有除息）—— 时间序列可精确切片
3. 合股/增发：3 年内年度增幅 ≥ 8% 增发 / 减幅 ≥ 50% 合股 —— 时间序列可精确切片

放弃的条件（无法精确回溯，避免引入未来信息）：
- 流动性/当日成交额（AKShare 不返回历史）
- 有息负债率（yfinance 仅返回近期 balance sheet）

幸存者偏差：今日 AKShare 列表不含已退市港股，历史池仍漏掉这部分样本。
此偏差无法在数据源层面消除，请在使用回测结果时知悉。

性能优化（A10）：
- 每只股票只调 yfinance 2 次（hist + shares_full）拉取最早 cutoff~今日的全量序列；
- 6 个基准日通过对同一序列做内存切片完成判定。
- 总 yfinance 调用从 N×6×2 降到 N×2（6 倍提速）。

CLI:
    python -m scripts.backfill_pools                      # 默认回填 6 年
    python -m scripts.backfill_pools --years-back 5
    python -m scripts.backfill_pools --as-of 2022-12-31  # 单独某一年
    python -m scripts.backfill_pools --no-skip-existing  # 强制重做
"""
import argparse
import json
import logging
import os
import random
import sys
import time
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

import pandas as pd

# 允许直接 python scripts/backfill_pools.py
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from collectors.hk_price import get_hk_stock_list
from collectors.hk_financial import (
    has_dividend_history,
    has_share_dilution,
    has_recent_dividend_or_forward,
    AH_DUAL_LISTED_STOCKS,
    DUAL_LISTED_EXEMPT,
    _get_ticker,
)
from storage import store as _store

logger = logging.getLogger(__name__)

# ============================================================
# A11: yfinance 原始数据缓存（避免每次全量重拉）
# ============================================================
_CACHE_DIR = os.path.join(PROJECT_ROOT, "data", "cache")
_CACHE_FILE = os.path.join(_CACHE_DIR, "yf_raw_cache.json")
_FAILED_FILE = os.path.join(_CACHE_DIR, "yf_raw_failed.json")


def _load_raw_cache() -> dict:
    """加载缓存 {code: {divs: [[date, val], ...], shares: [[date, val], ...] or null, ts: str}}"""
    if os.path.exists(_CACHE_FILE):
        try:
            with open(_CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"读取 yf_raw_cache 失败: {e}")
    return {}


def _save_raw_cache(cache: dict) -> None:
    """写回缓存"""
    os.makedirs(_CACHE_DIR, exist_ok=True)
    with open(_CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False)


def _load_failed() -> dict:
    """加载失败列表 {code: {reason: str, ts: str}}"""
    if os.path.exists(_FAILED_FILE):
        try:
            with open(_FAILED_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _save_failed(failed: dict) -> None:
    os.makedirs(_CACHE_DIR, exist_ok=True)
    with open(_FAILED_FILE, "w", encoding="utf-8") as f:
        json.dump(failed, f, ensure_ascii=False, indent=2)


def _series_from_cache(raw: list) -> Optional[pd.Series]:
    """[[date_str, val], ...] → pd.Series(DatetimeIndex, float)"""
    if not raw:
        return pd.Series(dtype=float)
    dates, vals = [], []
    for d, v in raw:
        dates.append(pd.Timestamp(d))
        vals.append(float(v))
    return pd.Series(vals, index=pd.DatetimeIndex(dates))


def _series_to_cache(s: Optional[pd.Series]) -> Optional[list]:
    """pd.Series → [[date_str, val], ...]"""
    if s is None:
        return None
    if s.empty:
        return []
    return [[str(d.date()), round(float(v), 10)] for d, v in s.items()]


# ============================================================
# A10: 批量预取 + 内存切片（核心优化）
# ============================================================

def _prefetch_ticker_data(
    code: str,
    oldest_cutoff: datetime,
    latest_end: datetime,
    _cache: Optional[dict] = None,
    _failed: Optional[dict] = None,
) -> Tuple[Optional[pd.Series], Optional[pd.Series]]:
    """
    带缓存的 yfinance 原始数据拉取。

    缓存命中（code 在 _cache 且 ts 足够新）→ 直接反序列化返回，不调 yfinance。
    缓存未命中 → 调 yfinance；成功则写缓存并从 failed 移除；失败则写 failed。

    Args:
        _cache: 外部传入的缓存 dict（避免每次读文件）；None 时不用缓存。
        _failed: 外部传入的失败列表 dict；None 时不用。
    """
    # ---- 缓存命中检查 ----
    if _cache is not None and code in _cache:
        entry = _cache[code]
        cached_ts = pd.Timestamp(entry.get("ts", "2000-01-01"))
        # 缓存覆盖范围: [oldest_cutoff, cached_ts]
        # 如果 cached_ts >= latest_end 且 oldest_cutoff 足够早 → 可用
        if cached_ts >= pd.Timestamp(latest_end):
            divs = _series_from_cache(entry.get("divs"))
            shares_raw = entry.get("shares")
            if shares_raw is None:
                monthly_shares = None  # 双重上市豁免
            else:
                monthly_shares = _series_from_cache(shares_raw)
            # 裁剪到本次窗口
            if divs is not None and not divs.empty:
                divs = divs[(divs.index >= pd.Timestamp(oldest_cutoff)) & (divs.index <= pd.Timestamp(latest_end))]
            if monthly_shares is not None and not monthly_shares.empty:
                monthly_shares = monthly_shares[(monthly_shares.index >= pd.Timestamp(oldest_cutoff)) & (monthly_shares.index <= pd.Timestamp(latest_end))]
            return divs, monthly_shares

    # ---- 缓存未命中 → 调 yfinance ----
    divs: Optional[pd.Series] = None
    monthly_shares: Optional[pd.Series] = None
    fetch_ok = True

    # ---- 1. 分红序列（hist[Dividends]） ----
    max_retries = 3
    for attempt in range(max_retries):
        try:
            t = _get_ticker(code)
            hist = t.history(
                start=oldest_cutoff.strftime("%Y-%m-%d"),
                end=(latest_end + timedelta(days=1)).strftime("%Y-%m-%d"),
                actions=True,
            )
            if hist is not None and not hist.empty and "Dividends" in hist.columns:
                d = hist["Dividends"]
                d = d[d > 0]
                if not d.empty:
                    if d.index.tz is not None:
                        d.index = d.index.tz_localize(None)
                    divs = d
                else:
                    divs = pd.Series(dtype=float)
            else:
                divs = pd.Series(dtype=float)
            time.sleep(random.uniform(0.5, 1.2))
            break
        except Exception as e:
            err = str(e)
            if any(kw in err for kw in (
                "Could not resolve", "timed out", "Connection aborted",
                "RemoteDisconnected", "Failed to perform",
            )):
                wait = 5 + random.uniform(2, 8) * (attempt + 1)
                logger.debug(f"[{code}] 分红预取网络异常，等待 {wait:.0f}s 重试 ({attempt+1}/{max_retries})")
                time.sleep(wait)
            else:
                logger.debug(f"[{code}] 分红预取失败: {err[:80]}")
                divs = pd.Series(dtype=float)
                fetch_ok = False
                break

    # ---- 2. 月末股份数（get_shares_full） ----
    # 双重上市股票直接跳过（与 has_share_dilution 行为一致）
    if code in DUAL_LISTED_EXEMPT:
        monthly_shares = None  # None 表示豁免（决策时不剔除）
    else:
        for attempt in range(max_retries):
            try:
                t = _get_ticker(code)
                shares = t.get_shares_full(
                    start=oldest_cutoff.strftime("%Y-%m-%d"),
                    end=latest_end.strftime("%Y-%m-%d"),
                )
                if shares is not None and len(shares) >= 2:
                    shares = shares.sort_index()
                    try:
                        if shares.index.tz is not None:
                            shares.index = shares.index.tz_localize(None)
                    except Exception:
                        pass
                    monthly_shares = shares.resample("ME").last().dropna()
                else:
                    monthly_shares = pd.Series(dtype=float)
                time.sleep(random.uniform(0.5, 1.2))
                break
            except Exception as e:
                err = str(e)
                if any(kw in err for kw in (
                    "Could not resolve", "timed out", "Connection aborted",
                    "RemoteDisconnected", "Failed to perform",
                )):
                    wait = 5 + random.uniform(2, 8) * (attempt + 1)
                    logger.debug(f"[{code}] 股份数预取网络异常，等待 {wait:.0f}s 重试 ({attempt+1}/{max_retries})")
                    time.sleep(wait)
                else:
                    logger.debug(f"[{code}] 股份数预取失败: {err[:80]}")
                    monthly_shares = pd.Series(dtype=float)
                    fetch_ok = False
                    break

    # ---- 写缓存 / 写 failed ----
    now_str = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    if _cache is not None:
        _cache[code] = {
            "divs": _series_to_cache(divs),
            "shares": _series_to_cache(monthly_shares) if monthly_shares is not None else None,
            "ts": now_str,
        }
    if _failed is not None:
        if not fetch_ok:
            _failed[code] = {"reason": "yf_fetch_fail", "ts": now_str}
        elif code in _failed:
            del _failed[code]  # 本次成功，从 failed 移除

    return divs, monthly_shares


def _decide_div_history(divs: pd.Series, as_of_date: datetime,
                        years: int = 3, min_years_with_div: int = 2) -> bool:
    """从已预取的分红序列内存切片判定（等价 has_dividend_history）"""
    if divs is None or divs.empty:
        return False
    cutoff = as_of_date - timedelta(days=years * 365)
    recent = divs[(divs.index >= cutoff) & (divs.index <= as_of_date)]
    if recent.empty:
        return False
    return recent.index.year.nunique() >= min_years_with_div


def _decide_recent_div(divs: pd.Series, as_of_date: datetime,
                        months: int = 12) -> bool:
    """从分红序列切片判定 fallback（等价 has_recent_dividend_or_forward）

    历史回填模式不查 forward_div CSV（CSV 仅反映今日已知的预公告，会引入未来信息）。
    """
    if divs is None or divs.empty:
        return False
    cut = as_of_date - timedelta(days=months * 30)
    recent = divs[(divs.index >= cut) & (divs.index <= as_of_date)]
    return not recent.empty


def _decide_share_dilution(monthly_shares: Optional[pd.Series],
                            as_of_date: datetime, code: str,
                            years: int = 3,
                            dilution_threshold: float = 0.08,
                            consolidation_threshold: float = 0.50) -> bool:
    """
    从月末股份数切片判定（等价 has_share_dilution，返回 should_filter）。
    monthly_shares=None 表示 A+H 豁免，直接 False（不剔除）。
    """
    if code in DUAL_LISTED_EXEMPT:
        return False
    if monthly_shares is None:
        return False
    if monthly_shares.empty:
        return False  # 数据不足保守不剔除

    # 切到 [start_year-01-01, as_of_date]
    start_year = as_of_date.year - years
    start_ts = pd.Timestamp(f"{start_year}-01-01")
    end_ts = pd.Timestamp(as_of_date)
    monthly = monthly_shares[(monthly_shares.index >= start_ts) & (monthly_shares.index <= end_ts)]
    if len(monthly) < 13:
        return False

    pct_change = monthly.pct_change(12).dropna()
    if pct_change.empty:
        return False

    # 排除最近 3 个月数据（避免 yfinance 数据口径切换的尾部异常）
    recent_cutoff = monthly.index[-1] - pd.DateOffset(months=3)
    stable_pct = pct_change[pct_change.index < recent_cutoff]
    if stable_pct.empty:
        return False

    up_vals = stable_pct[stable_pct > 0]
    down_vals = stable_pct[stable_pct < 0]
    median_up = float(up_vals.median()) if not up_vals.empty else 0.0
    median_down = float(down_vals.median()) if not down_vals.empty else 0.0

    has_dilution = median_up >= dilution_threshold
    has_consolidation = -median_down >= consolidation_threshold
    return has_dilution or has_consolidation


def backfill_all_pools_batched(
    targets: List[datetime],
    today_full_list: pd.DataFrame,
    log_progress_every: int = 50,
    retry_failed_only: bool = False,
    _cache: Optional[dict] = None,
    _failed: Optional[dict] = None,
) -> dict:
    """
    批量优化版：每只股票只调 1 次 yfinance（hist + shares_full），
    所有基准日通过内存切片完成。

    Args:
        targets: 基准日列表（升序），如 [2020-12-31, 2021-12-31, ..., 2025-12-31]
        today_full_list: 今日全量列表
        retry_failed_only: 只重试 _failed 中的股票（跳过缓存已有的）
        _cache: 外部传入的缓存 dict；None 时不用缓存
        _failed: 外部传入的失败列表 dict；None 时不用

    Returns:
        dict {as_of_str: pool_DataFrame}
    """
    if not targets:
        return {}
    if today_full_list is None or today_full_list.empty:
        logger.error("today_full_list 为空")
        return {}

    df = today_full_list.copy()
    df["code"] = df["code"].astype(str).str.zfill(5)

    # retry_failed_only 模式：只拉 failed 列表里的股票
    if retry_failed_only and _failed is not None:
        failed_codes = set(_failed.keys())
        df = df[df["code"].isin(failed_codes)]
        logger.info(f"retry-failed 模式: 只拉 {len(df)} 只 (共 {len(failed_codes)} 失败)")

    # 最早 cutoff = min(target) - 3 年
    oldest = min(targets) - timedelta(days=3 * 365 + 90)  # 多加 90 天 buffer
    latest = max(targets)
    logger.info(
        f"批量预取范围: {oldest:%Y-%m-%d} ~ {latest:%Y-%m-%d}; "
        f"基准日 {len(targets)} 个 × {len(df)} 只 = {len(targets) * len(df)} 决策"
    )

    # 每个基准日的 pool_rows 累积
    pool_rows_by_target: dict[str, list] = {t.strftime("%Y-%m-%d"): [] for t in targets}
    stats_by_target: dict[str, dict] = {
        t.strftime("%Y-%m-%d"): {"div_pass": 0, "div_fallback": 0, "dilution": 0}
        for t in targets
    }

    total = len(df)
    t_start = time.time()
    for i, (_, row) in enumerate(df.iterrows()):
        code = row["code"]
        if (i + 1) % log_progress_every == 0 or i == 0:
            elapsed = time.time() - t_start
            speed = (i + 1) / max(elapsed, 1)
            eta_min = (total - i - 1) / max(speed, 0.001) / 60
            sizes = {k: len(v) for k, v in pool_rows_by_target.items()}
            logger.info(
                f"  [{i+1}/{total}] 速度 {speed:.2f} 股/秒 ETA {eta_min:.1f} 分; 当前累积: {sizes}"
            )

        # 一次性预取（带缓存）
        divs, monthly_shares = _prefetch_ticker_data(code, oldest, latest, _cache=_cache, _failed=_failed)

        # 对每个基准日做内存判定
        for t_dt in targets:
            t_key = t_dt.strftime("%Y-%m-%d")

            # 条件 1: 分红历史
            ok_div = _decide_div_history(divs, t_dt)
            if not ok_div:
                if not _decide_recent_div(divs, t_dt):
                    continue
                stats_by_target[t_key]["div_fallback"] += 1
            else:
                stats_by_target[t_key]["div_pass"] += 1

            # 条件 2: 合股/增发
            should_filter = _decide_share_dilution(monthly_shares, t_dt, code)
            if should_filter:
                stats_by_target[t_key]["dilution"] += 1
                continue

            pool_rows_by_target[t_key].append(row.to_dict())

    # 转 DataFrame
    result: dict[str, pd.DataFrame] = {}
    for t_key, rows in pool_rows_by_target.items():
        s = stats_by_target[t_key]
        df_pool = pd.DataFrame(rows) if rows else pd.DataFrame()
        logger.info(
            f"[{t_key}] 入选 {len(df_pool)}/{total} "
            f"(分红 {s['div_pass']} + fallback {s['div_fallback']}; 合股增发剔除 {s['dilution']})"
        )
        result[t_key] = df_pool
    return result


# ============================================================
# 旧版逐基准日实现（保留供单基准日 / 测试使用）
# ============================================================

def backfill_one_pool(
    as_of_date: datetime,
    today_full_list: pd.DataFrame,
    log_progress_every: int = 50,
) -> pd.DataFrame:
    """
    针对一个基准日，从今日全量列表中筛出当时合格的股票（严格策略）。

    Args:
        as_of_date: 基准日（如 datetime(2022, 12, 31)）
        today_full_list: 今日 AKShare 全量列表（含 code 列）
        log_progress_every: 每 N 只打印一次进度

    Returns:
        合格股票池 DataFrame（同 today_full_list 的列结构）
    """
    if today_full_list is None or today_full_list.empty:
        logger.error("today_full_list 为空，无法回填")
        return pd.DataFrame()

    df = today_full_list.copy()
    df["code"] = df["code"].astype(str).str.zfill(5)

    pool_rows: List[dict] = []
    total = len(df)
    div_pass = 0
    div_fallback = 0
    dilution_filtered = 0

    for i, (_, row) in enumerate(df.iterrows()):
        code = row["code"]
        if (i + 1) % log_progress_every == 0 or i == 0:
            logger.info(
                f"  [{as_of_date:%Y-%m-%d}] 进度 [{i+1}/{total}] "
                f"已选 {len(pool_rows)} (其中 fallback {div_fallback})"
            )

        # ---- 条件 1: 分红历史 ----
        has_div = has_dividend_history(
            code, years=3, min_years_with_div=2, as_of_date=as_of_date
        )
        if not has_div:
            # fallback：近 12 月有除息
            if not has_recent_dividend_or_forward(code, months=12, as_of_date=as_of_date):
                continue
            div_fallback += 1
        else:
            div_pass += 1

        # ---- 条件 2: 合股/增发 ----
        should_filter, _ = has_share_dilution(
            code, years=3, as_of_date=as_of_date
        )
        if should_filter:
            dilution_filtered += 1
            continue

        pool_rows.append(row.to_dict())

    pool_df = pd.DataFrame(pool_rows)
    logger.info(
        f"[{as_of_date:%Y-%m-%d}] 完成: 入选 {len(pool_df)}/{total} "
        f"（分红 {div_pass} + fallback {div_fallback}; 合股增发剔除 {dilution_filtered}）"
    )
    return pool_df


def list_year_end_dates(years_back: int) -> List[datetime]:
    """
    生成需要回填的基准日列表。

    以「最近一个已完结的 12-31」为最大基准，再向前 years_back 年。
    例如今天是 2026-05，years_back=5 → 最近完结的年末是 2025-12-31，
    回填:
        [2020-12-31, 2021-12-31, 2022-12-31, 2023-12-31, 2024-12-31, 2025-12-31]
    （共 years_back+1 = 6 个基准日，覆盖回测的 5 年信号窗口）
    """
    today = datetime.now()
    year_end_this_year = datetime(today.year, 12, 31)
    # today 已过当年 12-31 才把当年纳入；否则用 today.year-1 作为基准
    base_year = today.year if today >= year_end_this_year else today.year - 1
    return [datetime(base_year - i, 12, 31) for i in range(years_back, -1, -1)]


def run_backfill(
    years_back: int = 5,
    only_as_of: datetime = None,
    skip_existing: bool = True,
    retry_failed: bool = False,
    from_local: bool = False,
) -> dict:
    """
    主流程。

    Args:
        retry_failed: 只重试上次失败的股票（跳过缓存已有的）
        from_local: 从本地 hk_stock_pool.csv 加载列表（不调 AKShare）

    Returns:
        dict {as_of_date_str: pool_size_or_'skipped'}
    """
    # ---- Step 1: 获取今日全量列表（仅一次） ----
    logger.info("Step 1: 获取今日全量港股列表（作为历史池的样本来源）...")
    if from_local:
        full_list = _store.load_stock_pool()
        if full_list.empty:
            # fallback: hk_stock_list.csv
            full_list = _store.load_stock_list()
        if full_list.empty:
            logger.error("本地列表为空")
            return {}
        logger.info(f"  从本地加载: {len(full_list)} 只")
    else:
        full_list = get_hk_stock_list()
        if full_list.empty:
            # fallback: 从本地缓存加载
            full_list = _store.load_stock_list()
            if not full_list.empty:
                logger.info(f"  AKShare 失败，使用本地 stock_list ({len(full_list)} 只)")
            else:
                logger.error("获取港股列表失败（本地也无缓存）")
                return {}
    logger.info(f"  今日全量: {len(full_list)} 只")

    # ---- Step 1a: 合并已有池（用 hk_stock_pool.csv 的 market_cap 覆盖 AKShare NaN）----
    # AKShare 主源部分小盘股 market_cap=NaN 被流动性预筛误杀，
    # 而 hk_stock_pool.csv（由 rebuild-pool 生成）含可靠 market_cap。
    # 合并策略：以 AKShare 为主，已有池覆盖 market_cap 缺失值；补充已有池独有股票
    existing_pool = _store.load_stock_pool()
    if not existing_pool.empty and not full_list.empty:
        existing_pool["code"] = existing_pool["code"].astype(str).str.zfill(5)
        full_list["code"] = full_list["code"].astype(str).str.zfill(5)
        # 覆盖 AKShare 中 market_cap=NaN 的（用已有池的值）
        if "market_cap" in full_list.columns and "market_cap" in existing_pool.columns:
            mcap_missing = full_list["market_cap"].isna() | (full_list["market_cap"] == 0)
            pool_map = existing_pool.set_index("code")["market_cap"].to_dict()
            for idx in full_list.index[mcap_missing]:
                code = full_list.at[idx, "code"]
                if code in pool_map and pd.notna(pool_map.get(code)) and pool_map.get(code, 0) > 0:
                    full_list.at[idx, "market_cap"] = pool_map[code]
        # 补充已有池中独有的股票（AKShare 列表里没有的）
        extra = existing_pool[~existing_pool["code"].isin(full_list["code"])]
        if not extra.empty:
            before = len(full_list)
            full_list = pd.concat([full_list, extra], ignore_index=True)
            logger.info(f"  合并已有池补 {len(extra)} 只独有股票: {before} -> {len(full_list)} 只")

    # 同步保存为最新 stock_list（不动 hk_stock_pool.csv）
    _store.save_stock_list(full_list)

    # ---- Step 1b: 流动性预筛（与 --rebuild-pool 同门槛）----
    # 严格策略放弃"历史时点流动性过滤"，但保留"今日流动性预筛"作为
    # 「该股票长期具备最低交易活跃度」的代理；避免把 ~2000 只仙股喂给 yfinance，
    # 使 yfinance 调用从 ~33000 次缩到 ~9000 次（~5 倍提速）。
    # 注意：从 hk_stock_pool.csv 补入的股票已有可靠 market_cap，不再预筛
    from config.settings import HK_STOCK_FILTERS
    cap_filter = HK_STOCK_FILTERS.get("min_market_cap", 0)
    if cap_filter and "market_cap" in full_list.columns:
        before = len(full_list)
        # 来自 hk_stock_pool.csv 的已在 Step 1a 检查过 market_cap，此处只筛 AKShare 源的
        pool_codes = set(existing_pool["code"]) if not existing_pool.empty else set()
        is_from_pool = full_list["code"].isin(pool_codes)
        needs_filter = ~is_from_pool
        mask = needs_filter & (full_list["market_cap"].fillna(0) < cap_filter)
        full_list = full_list[~mask].copy()
        logger.info(
            f"  流动性预筛（今日 market_cap/amount >= {cap_filter}）: "
            f"{before} -> {len(full_list)} 只"
        )

    # ---- Step 2: 决定要处理的基准日列表 ----
    if only_as_of is not None:
        targets = [only_as_of]
    else:
        targets = list_year_end_dates(years_back)
    logger.info(f"将回填 {len(targets)} 个基准日: {[t.strftime('%Y-%m-%d') for t in targets]}")

    # ---- Step 2b: 跳过已有快照 ----
    pending_targets: List[datetime] = []
    summary: dict = {}
    for as_of in targets:
        path_existing = _store._snapshot_path(as_of)
        if skip_existing and os.path.exists(path_existing):
            logger.info(f"[{as_of:%Y-%m-%d}] 已存在快照，跳过（如需重做请加 --no-skip-existing）")
            summary[as_of.strftime("%Y-%m-%d")] = "skipped"
        else:
            pending_targets.append(as_of)

    if not pending_targets:
        logger.info(f"===== 全部已存在，无需回填 =====\n{summary}")
        return summary

    # ---- Step 2c: 加载缓存 & failed ----
    _cache = _load_raw_cache()
    _failed = _load_failed()
    logger.info(f"  缓存: {len(_cache)} 只; failed: {len(_failed)} 只")

    # ---- Step 3: 批量预取 + 内存切片（一次性处理所有 pending 基准日） ----
    t0 = time.time()
    pools_by_date = backfill_all_pools_batched(
        pending_targets, full_list,
        retry_failed_only=retry_failed,
        _cache=_cache, _failed=_failed,
    )
    elapsed = time.time() - t0

    # ---- Step 3b: 保存缓存 & failed ----
    _save_raw_cache(_cache)
    _save_failed(_failed)
    logger.info(f"  缓存已保存: {len(_cache)} 只; failed: {len(_failed)} 只")

    for as_of in pending_targets:
        key = as_of.strftime("%Y-%m-%d")
        pool = pools_by_date.get(key, pd.DataFrame())
        if pool.empty:
            logger.warning(f"[{key}] 池为空，跳过保存")
            summary[key] = 0
            continue

        # ---- B46: 用 as_of 当天真实行情覆盖 price/volume/amount/market_cap ----
        # 原 pool.price/amount 来自 get_hk_stock_list()（AKShare 实时 spot，跑脚本当天值），
        # 与 as_of_date 无关；此处调 stock_hk_daily 覆盖为真实历史值。
        try:
            from collectors.hk_price import get_hk_prices_at_date
            pool_codes = pool["code"].astype(str).str.zfill(5).tolist()
            real = get_hk_prices_at_date(pool_codes, as_of, fallback_days=7)
            if real:
                pool = pool.copy()
                pool["code"] = pool["code"].astype(str).str.zfill(5)
                hit = 0
                for i, r in pool.iterrows():
                    c = r["code"]
                    rec = real.get(c)
                    if not rec or rec.get("close") is None:
                        continue
                    if "price" in pool.columns:
                        pool.at[i, "price"] = rec["close"]
                    for k_src, k_dst in [("open", "今开"), ("high", "最高"), ("low", "最低")]:
                        if k_dst in pool.columns and rec.get(k_src) is not None:
                            pool.at[i, k_dst] = rec[k_src]
                    if "volume" in pool.columns:
                        pool.at[i, "volume"] = rec["volume"]
                    if "amount" in pool.columns:
                        pool.at[i, "amount"] = rec["amount"]
                    if "market_cap" in pool.columns:
                        pool.at[i, "market_cap"] = rec["amount"]
                    hit += 1
                logger.info(f"[{key}] B46 流动性字段回填：{hit}/{len(pool)} 只命中真实价")
        except Exception as e:
            logger.warning(f"[{key}] B46 真实价回填失败（保留原值）: {e}")

        _store.save_stock_pool_snapshot(pool, as_of)
        summary[key] = len(pool)
        logger.info(f"[{key}] 保存 {len(pool)} 只")

    logger.info(f"===== 全部完成（总耗时 {elapsed/60:.1f} 分） =====\n{summary}")
    return summary


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )

    parser = argparse.ArgumentParser(description="历史股票池回填（年度独立池）")
    parser.add_argument("--years-back", type=int, default=5,
                        help="回填多少年（默认 5 年 + 上年末，共 6 个基准日）")
    parser.add_argument("--as-of", type=str, default=None,
                        help="只回填某一个基准日，格式 YYYY-MM-DD（覆盖 --years-back）")
    parser.add_argument("--no-skip-existing", action="store_true",
                        help="强制重做已有快照")
    parser.add_argument("--retry-failed", action="store_true",
                        help="只重试上次 yfinance 拉取失败的股票（跳过缓存已有的）")
    parser.add_argument("--from-local", action="store_true",
                        help="从本地 hk_stock_pool.csv 加载列表（不调 AKShare）")
    args = parser.parse_args()

    only_as_of = None
    if args.as_of:
        only_as_of = datetime.strptime(args.as_of, "%Y-%m-%d")

    run_backfill(
        years_back=args.years_back,
        only_as_of=only_as_of,
        skip_existing=not args.no_skip_existing,
        retry_failed=args.retry_failed,
        from_local=args.from_local,
    )


if __name__ == "__main__":
    main()
