"""
B24 迁移脚本：把 hk_monthly_price.csv 从单一 hfq 后复权改为
[原始价 close + 前复权价 close_qfq] 双价存储。

数据源：yfinance（auto_adjust=False）
    Close      → close      （原始价，用于 PE/PB/股息率/ranking 展示）
    Adj Close  → close_qfq  （前复权价，用于动量/价格变化率/回测）

策略：
- 读 stock_pool 全量 code（保证池内全覆盖）
- 全量重拉，原子替换 hk_monthly_price.csv
- 完成前请确保已备份 hk_monthly_price.csv.bak_b24_pre

用法：
    python scripts/migrate_to_dual_price.py [--years 5] [--workers 8] [--limit N]
"""
import argparse
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from storage.store import MONTHLY_PRICE_CSV, load_stock_pool

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def fetch_one(code: str, years: int) -> pd.DataFrame:
    """用 yfinance 拉单只 5y 月线，返回 dual schema DataFrame。"""
    try:
        import yfinance as yf
        ticker = f"{int(code):04d}.HK"
        t = yf.Ticker(ticker)
        hist = t.history(period=f"{years}y", interval="1mo", auto_adjust=False)
        if hist.empty:
            return pd.DataFrame()
        hist = hist.reset_index()
        # 规范字段
        df = pd.DataFrame({
            "date": pd.to_datetime(hist["Date"]).dt.tz_localize(None).dt.strftime("%Y-%m-%d"),
            "code": code,
            "open": hist["Open"].astype(float),
            "close": hist["Close"].astype(float),
            "high": hist["High"].astype(float),
            "low": hist["Low"].astype(float),
            "volume": hist["Volume"].astype(float),
            "amount": 0.0,  # yfinance 不提供成交额
            "close_qfq": hist["Adj Close"].astype(float),
        })
        # 丢掉无效行（极少数月份 Close 为 NaN）
        df = df.dropna(subset=["close", "close_qfq"])
        return df
    except Exception as e:
        logger.warning(f"[{code}] yfinance 拉取异常: {str(e)[:80]}")
        return pd.DataFrame()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--years", type=int, default=5, help="拉取年数")
    parser.add_argument("--workers", type=int, default=8, help="并发数")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 只（调试用）")
    parser.add_argument("--out", type=str, default=MONTHLY_PRICE_CSV, help="输出路径")
    parser.add_argument("--pause-every", type=int, default=50, help="每 N 只 sleep 一次")
    args = parser.parse_args()

    pool = load_stock_pool()
    if pool.empty:
        logger.error("stock_pool 为空")
        return
    codes = pool["code"].astype(str).str.zfill(5).drop_duplicates().tolist()
    if args.limit:
        codes = codes[: args.limit]
    logger.info(f"目标 codes: {len(codes)} 只，并发 {args.workers}")

    rows = []
    ok, fail = 0, 0
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {ex.submit(fetch_one, c, args.years): c for c in codes}
        for fut in as_completed(futures):
            c = futures[fut]
            done += 1
            try:
                df = fut.result()
            except Exception as e:
                df = pd.DataFrame()
                logger.warning(f"[{c}] future 异常: {str(e)[:80]}")
            if df is None or df.empty:
                fail += 1
            else:
                rows.append(df)
                ok += 1
            if done % args.pause_every == 0 or done == len(codes):
                logger.info(f"  进度 {done}/{len(codes)} | ok={ok} fail={fail}")
                # 适度休眠规避限流
                if done < len(codes):
                    time.sleep(1)

    if not rows:
        logger.error("无任何数据，终止")
        return

    out_df = pd.concat(rows, ignore_index=True)
    out_df["code"] = out_df["code"].astype(str).str.zfill(5)
    out_df = out_df.drop_duplicates(subset=["code", "date"], keep="last")
    out_df = out_df.sort_values(["code", "date"]).reset_index(drop=True)
    out_df = out_df[["date", "code", "open", "close", "high", "low", "volume", "amount", "close_qfq"]]

    tmp = args.out + ".tmp"
    out_df.to_csv(tmp, index=False, encoding="utf-8-sig")
    os.replace(tmp, args.out)
    logger.info(f"✓ 写入 {args.out}, {len(out_df)} 行, codes={out_df['code'].nunique()}")
    logger.info(f"  ok={ok}, fail={fail}")


if __name__ == "__main__":
    main()
