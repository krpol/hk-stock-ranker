"""
B31 + B32 一次性回填脚本：把 hk_financial.csv 中所有股票的 FCF 组件 + TTM 字段回填。
同时重算 debt_to_equity 为 Net D/E（B32: max(IBD−Cash, 0) / Equity）。

字段：
  - ocf_3y_avg                       经营现金流 3 年平均
  - capex_3y_avg                     资本开支 3 年平均（abs）
  - dep_amortization_3y_avg          折旧摊销 3 年平均（abs）
  - current_lease_obligation_prev    期初一年内到期租赁
  - equity_latest                    最新期净资产
  - capex_ttm                        最新一期 CapEx (abs, B32)
  - da_ttm                           最新一期 D&A (abs, B32)
  - current_lease_obligation_ttm     最新期租赁 (B32)

用法：
    python scripts/backfill_fcf_components.py [--workers 8] [--limit N]
"""
import sys
import argparse
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import logging
from collectors.hk_financial import get_fcf_components

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

CSV_PATH = Path("data/csv/hk_financial.csv")

FIELDS = [
    "ocf_3y_avg",
    "capex_3y_avg",
    "dep_amortization_3y_avg",
    "current_lease_obligation_prev",
    "equity_latest",
    "capex_ttm",
    "da_ttm",
    "current_lease_obligation_ttm",
    "prev_capex",
    "prev_da",
]


def fetch_one(code: str) -> tuple[str, dict]:
    try:
        comps = get_fcf_components(code) or {}
        return code, {k: comps.get(k) for k in FIELDS}
    except Exception as e:
        logger.warning(f"[{code}] 回填失败: {e}")
        return code, {}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()

    df = pd.read_csv(CSV_PATH, dtype={"code": str})
    df["code"] = df["code"].str.zfill(5)
    if args.limit:
        codes = df["code"].head(args.limit).tolist()
    else:
        codes = df["code"].tolist()

    for col in FIELDS:
        if col not in df.columns:
            df[col] = None

    logger.info(f"开始回填 FCF 组件：{len(codes)} 只，workers={args.workers}")
    df = df.set_index("code", drop=False)

    done = 0
    ok = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(fetch_one, c): c for c in codes}
        for fut in as_completed(futs):
            code, vals = fut.result()
            done += 1
            if vals:
                has_any = any(v is not None for v in vals.values())
                if has_any:
                    ok += 1
                for k, v in vals.items():
                    if v is not None:
                        df.at[code, k] = v
            if done % 50 == 0:
                logger.info(f"  进度 [{done}/{len(codes)}] ok={ok}")

    df = df.reset_index(drop=True)

    # B32: 重算 debt_to_equity = max(IBD − Cash, 0) / Equity
    if all(c in df.columns for c in ["interest_bearing_debt", "cash_and_equivalents", "equity_latest"]):
        ibd = pd.to_numeric(df["interest_bearing_debt"], errors="coerce")
        cash = pd.to_numeric(df["cash_and_equivalents"], errors="coerce")
        eq = pd.to_numeric(df["equity_latest"], errors="coerce")
        net_debt = (ibd.fillna(0) - cash.fillna(0)).clip(lower=0)
        mask = (eq > 0) & ibd.notna()
        df.loc[mask, "debt_to_equity"] = (net_debt[mask] / eq[mask]).round(4)
        logger.info(f"重算 Net D/E: 影响 {mask.sum()} 只")

    df.to_csv(CSV_PATH, index=False)
    logger.info(f"完成：{ok}/{len(codes)} 成功写回 {CSV_PATH}")

    # 统计
    for col in FIELDS:
        s = pd.to_numeric(df[col], errors="coerce")
        logger.info(f"  {col}: 非空 {s.notna().sum()} / 总 {len(df)}")


if __name__ == "__main__":
    main()
