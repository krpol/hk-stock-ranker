"""
B19 一次性回填脚本：把 hk_financial.csv 中所有股票的负债率三件套
(net_debt_ratio, total_liability_ratio, debt_to_equity) 重新计算。

仅刷新 debt 列，不动其他列。yfinance balance_sheet 已有 disk cache，
速度比完整 collect_financial_data 快得多。

用法：
    python scripts/backfill_debt_to_equity.py [--workers 8] [--limit N]
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
            "net_debt_ratio": ratios.get("net_debt_ratio"),
            "total_liability_ratio": ratios.get("total_liability_ratio"),
            "debt_to_equity": ratios.get("debt_to_equity"),
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

    # 确保三列都存在
    for col in ["net_debt_ratio", "total_liability_ratio", "debt_to_equity"]:
        if col not in df.columns:
            df[col] = None

    logger.info(f"开始回填 debt 字段：{len(codes)} 只，workers={args.workers}")
    df = df.set_index("code", drop=False)

    done = 0
    ok = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(fetch_one, c): c for c in codes}
        for fut in as_completed(futs):
            code, ratios = fut.result()
            done += 1
            if ratios:
                ok += 1
                for k, v in ratios.items():
                    if v is not None:
                        df.at[code, k] = v
            if done % 50 == 0:
                logger.info(f"  进度 [{done}/{len(codes)}] ok={ok}")

    df = df.reset_index(drop=True)
    df.to_csv(CSV_PATH, index=False)
    logger.info(f"完成：{ok}/{len(codes)} 成功写回 {CSV_PATH}")

    # 简单统计
    de = pd.to_numeric(df["debt_to_equity"], errors="coerce")
    logger.info(
        f"debt_to_equity 分布：非空 {de.notna().sum()} / 总 {len(df)} | "
        f"≤50%: {(de <= 0.5).sum()} | >50%: {(de > 0.5).sum()}"
    )


if __name__ == "__main__":
    main()
