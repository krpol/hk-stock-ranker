"""
B48: 从 stock_hk_daily 日线缓存聚合月度价格（东财 push2his 失效时的备份通道）

背景
----
`ak.stock_hk_hist`（东财 push2his.eastmoney.com）在 2026-08 被限流，
全部返回 ConnectionError，导致 main.py --update 的 Step 2 完全跑不动。

而新浪源 `ak.stock_hk_daily` 正常，且 B47 已为 4545 只股建立永久 pkl 缓存，
schema 与 hk_monthly_price.csv 完全一致。本脚本按月重采样日线得到月度价。

聚合口径（与东财 stock_hk_hist period="monthly" 对齐）
----------------------------------------------------
  date   = 月内最后一个交易日
  open   = 月内第一个交易日开盘价
  close  = 月内最后一个交易日收盘价
  high   = 月内最高
  low    = 月内最低
  volume = 月内累加
  amount = 月内累加
  close_qfq = 同上逻辑，但取 adjust="qfq" 的日线

用法
----
  # 抽样验证（不写盘）
  python scripts/rebuild_monthly_from_daily.py --codes 00001,00700,09988 --dry-run

  # 全量重建（默认 pool 内 902 只，强制重拉日线）
  python scripts/rebuild_monthly_from_daily.py

  # 只保留 2026-01 及以后
  python scripts/rebuild_monthly_from_daily.py --start-month 2026-01

注意
----
stock_hk_daily 内部用 mini_racer(V8) 执行 JS，多线程会 fatal crash，必须串行。
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import time

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from collectors.hk_price import (  # noqa: E402
    _fetch_single_daily,
    _load_daily_cache,
    _is_dead_code,
    _save_failed_daily,
    AKSHARE_SLEEP,
)
from storage.store import load_stock_pool, load_monthly_price, MONTHLY_PRICE_CSV  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

OUT_COLS = ["date", "code", "open", "close", "high", "low", "volume", "amount", "close_qfq"]

# B50-r3: 缓存够新阈值。月度更新时，只要日线缓存已含该日期之后的交易日，
# 就复用缓存，不再重拉（新浪连续请求 ~250 只会挂起连接）。
FRESH_CUTOFF = pd.Timestamp("2026-08-01")


def _resample_monthly(df: pd.DataFrame) -> pd.DataFrame:
    """日线 -> 月度 OHLCV 聚合"""
    if df is None or df.empty or "date" not in df.columns:
        return pd.DataFrame()
    d = df.copy()
    d["date"] = pd.to_datetime(d["date"])
    d = d.sort_values("date")
    d["_ym"] = d["date"].dt.to_period("M")
    agg = d.groupby("_ym", sort=True).agg(
        date=("date", "max"),
        open=("open", "first"),
        close=("close", "last"),
        high=("high", "max"),
        low=("low", "min"),
        volume=("volume", "sum"),
        amount=("amount", "sum"),
    ).reset_index(drop=True)
    return agg


def _fetch_qfq_daily(code: str) -> pd.DataFrame:
    """
    拉 adjust='qfq' 日线（不进 pkl 缓存，因为缓存只存不复权口径）。
    失败返回空 DataFrame，调用方回退 close_qfq = close。
    """
    import akshare as ak
    import socket

    for attempt in range(3):
        try:
            _prev = socket.getdefaulttimeout()
            socket.setdefaulttimeout(45)
            try:
                df = ak.stock_hk_daily(symbol=code, adjust="qfq")
            finally:
                socket.setdefaulttimeout(_prev)
            if df is None or df.empty or "date" not in df.columns:
                return pd.DataFrame()
            time.sleep(AKSHARE_SLEEP)
            return df
        except Exception as e:
            if attempt == 2:
                logger.debug(f"[{code}] qfq 日线拉取失败: {str(e)[:80]}")
                return pd.DataFrame()
            time.sleep(1 + attempt)
    return pd.DataFrame()


def build_monthly_for_code(
    code: str,
    force_refresh: bool = True,
    with_qfq: bool = True,
) -> pd.DataFrame:
    """
    单只股票：日线 -> 月度价（含 close_qfq）。
    返回列: OUT_COLS；失败返回空 DataFrame。
    """
    if _is_dead_code(code) and not force_refresh:
        return pd.DataFrame()

    raw = None
    if force_refresh:
        # B50-r3: 新浪连续请求 ~250 只会挂起，已缓存且够新（>= FRESH_CUTOFF）的直接复用，
        # 避免无谓重拉再次触发限流。够新的判定：缓存最新交易日不早于 FRESH_CUTOFF。
        cached = _load_daily_cache(code)
        if cached is not None and not cached.empty and "date" in cached.columns:
            if pd.to_datetime(cached["date"]).max() >= FRESH_CUTOFF:
                raw = cached
    if raw is None:
        raw = _fetch_single_daily(code, use_cache=not force_refresh, force=force_refresh)
    if raw is None or raw.empty:
        return pd.DataFrame()

    m = _resample_monthly(raw)
    if m.empty:
        return pd.DataFrame()
    m["code"] = code

    # close_qfq
    if with_qfq:
        qdf = _fetch_qfq_daily(code)
        if not qdf.empty:
            qm = _resample_monthly(qdf)
            if not qm.empty:
                qm_slim = qm[["date", "close"]].rename(columns={"close": "close_qfq"})
                m = m.merge(qm_slim, on="date", how="left")
    if "close_qfq" not in m.columns:
        m["close_qfq"] = m["close"]
    m["close_qfq"] = m["close_qfq"].fillna(m["close"])

    return m[OUT_COLS]


def compare_with_existing(new_df: pd.DataFrame, code: str, n_months: int = 4) -> None:
    """抽样对比新旧月度价（验证聚合口径）"""
    old = load_monthly_price()
    if old.empty:
        logger.info(f"[{code}] 无历史数据可比对")
        return
    old_c = old[old["code"] == code].copy()
    if old_c.empty:
        logger.info(f"[{code}] 历史数据中无此 code")
        return
    old_c["ym"] = pd.to_datetime(old_c["date"]).dt.strftime("%Y-%m")
    new_c = new_df[new_df["code"] == code].copy()
    new_c["ym"] = pd.to_datetime(new_c["date"]).dt.strftime("%Y-%m")

    merged = old_c[["ym", "date", "close", "close_qfq"]].merge(
        new_c[["ym", "date", "close", "close_qfq"]],
        on="ym", how="inner", suffixes=("_old", "_new"),
    ).tail(n_months)
    if merged.empty:
        logger.info(f"[{code}] 无重叠月份")
        return
    merged["close_diff%"] = (
        (merged["close_new"] - merged["close_old"]) / merged["close_old"] * 100
    ).round(2)
    merged["qfq_diff%"] = (
        (merged["close_qfq_new"] - merged["close_qfq_old"]) / merged["close_qfq_old"] * 100
    ).round(2)
    logger.info(f"[{code}] 新旧对比（最近 {len(merged)} 个重叠月）:")
    for line in merged.to_string(index=False).split("\n"):
        logger.info(f"    {line}")


def main():
    ap = argparse.ArgumentParser(description="从日线缓存聚合月度价")
    ap.add_argument("--codes", type=str, default=None,
                    help="逗号分隔的 code，默认全 pool")
    ap.add_argument("--start-month", type=str, default=None,
                    help="只保留该月及以后 (YYYY-MM)")
    ap.add_argument("--no-force", action="store_true",
                    help="不强制重拉日线（用现有 pkl 缓存，速度快但数据可能旧）")
    ap.add_argument("--no-qfq", action="store_true",
                    help="跳过 qfq 拉取，close_qfq 直接 = close（快一倍）")
    ap.add_argument("--dry-run", action="store_true", help="不写盘")
    ap.add_argument("--compare", action="store_true",
                    help="与现有 monthly_price 对比（校验聚合口径）")
    args = ap.parse_args()

    force = not args.no_force
    with_qfq = not args.no_qfq

    if args.codes:
        codes = [c.strip().zfill(5) for c in args.codes.split(",") if c.strip()]
    else:
        sp = load_stock_pool()
        if sp.empty:
            logger.error("股票池为空，请先 --rebuild-pool")
            return 1
        codes = sp["code"].astype(str).str.zfill(5).tolist()

    logger.info(f"处理 {len(codes)} 只股票 (force_refresh={force}, with_qfq={with_qfq})")
    t0 = time.time()
    frames = []
    ok = fail = 0

    for i, code in enumerate(codes, 1):
        try:
            m = build_monthly_for_code(code, force_refresh=force, with_qfq=with_qfq)
        except Exception as e:
            logger.warning(f"[{code}] 异常: {str(e)[:100]}")
            m = pd.DataFrame()
        if m.empty:
            fail += 1
        else:
            ok += 1
            frames.append(m)

        if i % 50 == 0 or i == len(codes):
            el = time.time() - t0
            eta = el / i * (len(codes) - i)
            logger.info(f"  [{i}/{len(codes)}] ok={ok} fail={fail} "
                        f"耗时={el:.0f}s ETA={eta:.0f}s")
            _save_failed_daily()

    _save_failed_daily()

    if not frames:
        logger.error("无有效数据")
        return 1

    result = pd.concat(frames, ignore_index=True)
    result["date"] = pd.to_datetime(result["date"])

    if args.start_month:
        cutoff = pd.Timestamp(args.start_month + "-01")
        before = len(result)
        result = result[result["date"] >= cutoff]
        logger.info(f"按 --start-month {args.start_month} 过滤: {before} -> {len(result)} 行")

    logger.info(f"聚合完成: {len(result)} 行, {result['code'].nunique()} 只, "
                f"日期 {result['date'].min().date()} ~ {result['date'].max().date()}")

    if args.compare:
        for c in codes[:3]:
            compare_with_existing(result, c)

    if args.dry_run:
        logger.info("--dry-run 未写盘")
        logger.info("抽样:")
        for line in result.tail(8).to_string(index=False).split("\n"):
            logger.info(f"    {line}")
        return 0

    # 备份 + 写盘（复用 store 的去重 upsert）
    if os.path.exists(MONTHLY_PRICE_CSV):
        bak = MONTHLY_PRICE_CSV + ".bak_b48"
        pd.read_csv(MONTHLY_PRICE_CSV, dtype={"code": str}).to_csv(
            bak, index=False, encoding="utf-8-sig")
        logger.info(f"已备份原文件到 {bak}")

    from storage.store import save_monthly_price
    save_monthly_price(result)

    after = load_monthly_price()
    logger.info(f"写盘后 monthly_price: {len(after)} 行, "
                f"日期 {after['date'].min().date()} ~ {after['date'].max().date()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
