"""
B22 一次性回填脚本：把 hk_financial.csv 中所有股票的 roe 字段补齐。

复用 get_debt_ratios（同次 fetch balance_sheet + income_stmt）。
yfinance 已有 disk cache，整体很快。

用法：
    python scripts/backfill_roe.py [--workers 8] [--limit N]
"""
import sys
import argparse
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import logging
from collectors.hk_financial import get_debt_ratios

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

CSV_PATH = Path("data/csv/hk_financial.csv")


def fetch_one(code: str) -> tuple[str, dict]:
    try:
        ratios = get_debt_ratios(code) or {}
        return code, {
            "roe": ratios.get("roe"),
        }
    except Exception as e:
        logger.warning(f"[{code}] 回填失败: {e}")
        return code, {}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--limit", type=int, default=None,
                        help="只回填前 N 只（调试用）")
    args = parser.parse_args()

    df = pd.read_csv(CSV_PATH, dtype={"code": str})
    df["code"] = df["code"].str.zfill(5)
    if args.limit:
        codes = df["code"].head(args.limit).tolist()
    else:
        codes = df["code"].tolist()

    if "roe" not in df.columns:
        df["roe"] = None

    logger.info(f"开始回填 roe：{len(codes)} 只，workers={args.workers}")
    df = df.set_index("code", drop=False)

    done = 0
    ok = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(fetch_one, c): c for c in codes}
        for fut in as_completed(futs):
            code, ratios = fut.result()
            done += 1
            roe = ratios.get("roe")
            if roe is not None:
                ok += 1
                df.at[code, "roe"] = roe
            if done % 50 == 0:
                logger.info(f"  进度 [{done}/{len(codes)}] ok={ok}")

    df = df.reset_index(drop=True)
    df.to_csv(CSV_PATH, index=False)
    logger.info(f"完成：{ok}/{len(codes)} 成功写回 {CSV_PATH}")

    roe_ser = pd.to_numeric(df["roe"], errors="coerce")
    logger.info(
        f"roe 分布：非空 {roe_ser.notna().sum()} / 总 {len(df)} | "
        f"min={roe_ser.min():.4f} max={roe_ser.max():.4f} "
        f"median={roe_ser.median():.4f} mean={roe_ser.mean():.4f}"
    )


if __name__ == "__main__":
    main()
