"""
Point-in-Time 数据层（PIT）：为回测提供「时点正确」的历史因子输入。

背景与经济学逻辑
----------------
回测的黄金准则是「只用 t 时点已公开的信息做 t 时点的决策」，否则产生
前视偏差（look-ahead bias）——这是任何严谨量化研究（AQR、Fama-French
等）的第一原则。此前的 backfill_rankings.py 用「今日财务 ÷ 历史股价」
回填历史排名，正是典型的 look-ahead：2022 年的 return_rate 用了
2026 年才知道的分红，导致回测 alpha 系统性高估。

本模块用「天然时点」重建历史因子值：
- 分红：yfinance 的 ex-dividend date（除息日）是天然时点——某时点 t 的
  TTM 分红，就是「除息日落在 (t-12m, t] 区间」的分红之和，精确无偏差。
- 回购 / 财务 / 股本：用「报告期 + 保守披露滞后」近似公告日（港股年报
  通常在财政年结后 3~4 个月披露），避免用到尚未公告的数据。

设计
----
数据以「历史序列」存储（一只股票多行，一行一个事件时点），回测时按
as_of(t) 动态切片。这样数据只存一份，时点口径由切片函数统一保证，低耦合高内聚。

    data/csv/pit/dividend_history.csv : code, ex_date, div_per_share
    （回购 / 财务 / 股本序列后续追加）
"""
from __future__ import annotations

import logging
import os
from typing import Iterable, Optional

import pandas as pd

from config.settings import CSV_DIR

logger = logging.getLogger(__name__)

PIT_DIR = os.path.join(CSV_DIR, "pit")
DIVIDEND_HISTORY_CSV = os.path.join(PIT_DIR, "dividend_history.csv")

# 港股年报披露滞后的保守近似（财政年结后推 N 个月视为「已公告」）。
# 港股主板上市规则：年报须在财政年结后 4 个月内刊发，中期 3 个月内。
# 这里取偏保守的 4 个月，宁可少用一期、不可提前用。
_FINANCIAL_DISCLOSURE_LAG_MONTHS = 4


def _ensure_pit_dir():
    os.makedirs(PIT_DIR, exist_ok=True)


# ──────────────────────────────────────────────────────────────────
# 分红历史序列
# ──────────────────────────────────────────────────────────────────
def collect_dividend_history(codes: Iterable[str], force: bool = False,
                             max_workers: int = 6) -> pd.DataFrame:
    """批量采集分红历史序列（ex-dividend date + 每股分红），增量缓存。

    已采集过的股票（非 force）跳过，复用缓存；只拉缺失的，符合「成功先缓存、
    只重拉失败」原则。

    Returns:
        DataFrame[code, ex_date, div_per_share]，按 (code, ex_date) 去重。
    """
    import yfinance as yf
    from concurrent.futures import ThreadPoolExecutor, as_completed

    _ensure_pit_dir()
    existing = load_dividend_history()
    have_codes = set(existing["code"]) if not existing.empty else set()

    codes = [str(c).zfill(5) for c in codes]
    todo = [c for c in codes if force or c not in have_codes]

    if not todo:
        logger.info("分红历史：%d 只全部已缓存，跳过采集", len(codes))
        return existing

    def _one(code: str):
        try:
            t = yf.Ticker(f"{int(code):04d}.HK")
            d = t.dividends
            if d is None or len(d) == 0:
                return code, []
            d = d.copy()
            if isinstance(d, pd.DataFrame):
                col = d.columns[0]
                s = d[col]
            else:
                s = d
            s = s.dropna()
            if s.empty:
                return code, []
            rows = []
            for dt, v in s.items():
                dt = pd.to_datetime(dt).tz_localize(None).normalize()
                rows.append({"code": code, "ex_date": dt, "div_per_share": float(v)})
            return code, rows
        except Exception as e:
            logger.warning("[%s] 分红历史采集失败: %s", code, str(e)[:80])
            return code, None

    records: list = []
    failed: list = []
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {ex.submit(_one, c): c for c in todo}
        for f in as_completed(futs):
            code, rows = f.result()
            if rows is None:
                failed.append(code)
            else:
                records.extend(rows)

    new_df = pd.DataFrame(records, columns=["code", "ex_date", "div_per_share"]) if records else pd.DataFrame()
    if new_df.empty:
        logger.info("分红历史：本次 %d 只全部为空/失败（失败 %d 只）", len(todo), len(failed))
        return existing

    new_df["ex_date"] = pd.to_datetime(new_df["ex_date"])
    new_df["code"] = new_df["code"].astype(str).str.zfill(5)

    if not existing.empty:
        combined = pd.concat([existing, new_df], ignore_index=True)
    else:
        combined = new_df
    combined = combined.drop_duplicates(subset=["code", "ex_date"], keep="last")
    combined = combined.sort_values(["code", "ex_date"]).reset_index(drop=True)
    combined.to_csv(DIVIDEND_HISTORY_CSV, index=False, encoding="utf-8-sig")

    logger.info("分红历史已保存：%d 只、%d 条除息记录（失败 %d 只）",
                combined["code"].nunique(), len(combined), len(failed))
    return combined


def load_dividend_history() -> pd.DataFrame:
    """加载分红历史序列缓存（无则返回空 DataFrame）。"""
    if not os.path.exists(DIVIDEND_HISTORY_CSV):
        return pd.DataFrame(columns=["code", "ex_date", "div_per_share"])
    df = pd.read_csv(DIVIDEND_HISTORY_CSV, dtype={"code": str})
    df["ex_date"] = pd.to_datetime(df["ex_date"])
    return df


# ──────────────────────────────────────────────────────────────────
# 时点切片（as-of）
# ──────────────────────────────────────────────────────────────────
def ttm_dividend_as_of(code: str, as_of: pd.Timestamp, months: int = 12,
                       div_history: Optional[pd.DataFrame] = None) -> Optional[float]:
    """t 时点前 `months` 个月内的 TTM 每股分红（point-in-time 正确）。

    只用 ex_date ∈ (as_of - months, as_of] 的分红，天然排除未来数据。
    """
    if div_history is None:
        div_history = load_dividend_history()
    if div_history.empty:
        return None
    sub = div_history[div_history["code"] == str(code).zfill(5)]
    if sub.empty:
        return 0.0
    as_of = pd.Timestamp(as_of)
    start = as_of - pd.DateOffset(months=months)
    mask = (sub["ex_date"] <= as_of) & (sub["ex_date"] > start)
    win = sub[mask]
    return float(win["div_per_share"].sum()) if len(win) else 0.0


def dividend_as_of_series(code: str, as_of_dates: Iterable[pd.Timestamp],
                          months: int = 12,
                          div_history: Optional[pd.DataFrame] = None) -> pd.Series:
    """批量：对一组时点返回对应的 TTM 分红序列（回测调仓用）。"""
    if div_history is None:
        div_history = load_dividend_history()
    sub = div_history[div_history["code"] == str(code).zfill(5)].copy()
    out = {}
    for t in as_of_dates:
        t = pd.Timestamp(t)
        if sub.empty:
            out[t] = 0.0
            continue
        start = t - pd.DateOffset(months=months)
        win = sub[(sub["ex_date"] <= t) & (sub["ex_date"] > start)]
        out[t] = float(win["div_per_share"].sum()) if len(win) else 0.0
    return pd.Series(out)
