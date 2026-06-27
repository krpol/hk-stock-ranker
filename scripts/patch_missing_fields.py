#!/usr/bin/env python3
"""空缺字段增量补漏脚本：多轮 + 字段级合并。

针对 hk_financial.csv 中的"采集失败"行重新调用 collect_financial_data()，
并按字段级合并：新值有意义就覆盖，新值无意义则保留旧值。

可疑判定（targets）:
  - fy_end_missing : current_fy_end is NaN（最确定的失败行）
  - all_zero       : fy_div=0 AND buyback=0 AND forward=0（可能真没回报，也可能采集失败）
  - both           : fy_end_missing OR all_zero

字段级合并规则：
  - 数值列：new_val 不是 NaN/None 且 new_val != 0 → 覆盖（除"零值有意义"列外）
  - 'code'/'updated_at' 始终保留新值
  - 字符串列（fiscal_periods/current_fy_end 等）：new_val 非空字符串 → 覆盖
  - 'has_dividend_history'：True 优先（任意一次为 True 即保留 True）

用法:
  python scripts/patch_missing_fields.py --target fy_end_missing --rounds 3 --workers 4
"""
import os
import sys
import time
import shutil
import random
import argparse
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd
import numpy as np

from collectors.hk_financial import collect_financial_data
from collectors.hk_forward_dividend import get_forward_div_map

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger(__name__)

CSV = "data/csv/hk_financial.csv"
BAK_DIR = "data/csv/bak"

# 这些字段：0 视作有效（不要因为新返回 0 就跳过）
ZERO_VALID_FIELDS = {
    "annual_dilution_rate",  # 0 表示无稀释
    "net_debt_ratio",        # 0 是可能的真实值
}

# 这些字段：永远不覆盖（基础键 / 时间戳特殊处理）
PROTECTED_FIELDS = {"code"}

# True 优先的字段
TRUE_FIRST_FIELDS = {"has_dividend_history"}


def _is_meaningful(val, col: str) -> bool:
    """判断新返回的字段值是否"有意义"（值得覆盖旧值）"""
    if val is None:
        return False
    if isinstance(val, float) and pd.isna(val):
        return False
    if isinstance(val, str):
        s = val.strip()
        if s == "" or s.lower() in ("nan", "none"):
            return False
        return True
    if isinstance(val, (int, float, np.integer, np.floating)):
        if col in ZERO_VALID_FIELDS:
            return True
        # 0 视为不确定，不覆盖
        return float(val) != 0.0
    if isinstance(val, (bool, np.bool_)):
        return True
    return val is not None


def _merge_row(old_row: pd.Series, new_dict: dict) -> tuple[pd.Series, int]:
    """字段级合并：返回 (新行, 修改字段数)"""
    changed = 0
    merged = old_row.copy()
    for col, new_val in new_dict.items():
        if col in PROTECTED_FIELDS:
            continue

        if col in TRUE_FIRST_FIELDS:
            # True 优先
            if bool(new_val):
                if not bool(old_row.get(col, False)):
                    merged[col] = True
                    changed += 1
            continue

        if not _is_meaningful(new_val, col):
            continue

        old_val = old_row.get(col, None)
        # 比较是否实际变化（避免无谓 changed++）
        try:
            if isinstance(new_val, float) or isinstance(old_val, float):
                if not (pd.isna(old_val) and pd.isna(new_val)):
                    if float(new_val) != float(old_val) if not pd.isna(old_val) else True:
                        merged[col] = new_val
                        if pd.isna(old_val) or float(new_val) != float(old_val):
                            changed += 1
            elif old_val != new_val:
                merged[col] = new_val
                changed += 1
        except (TypeError, ValueError):
            if str(old_val) != str(new_val):
                merged[col] = new_val
                changed += 1

    # updated_at 总是更新为本次时间
    merged["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    return merged, changed


def _select_targets(df: pd.DataFrame, target: str) -> pd.DataFrame:
    """根据 target 名筛选可疑行"""
    if target == "fy_end_missing":
        mask = df["current_fy_end"].isna() | (df["current_fy_end"].astype(str).str.strip() == "")
    elif target == "all_zero":
        fy = pd.to_numeric(df["fy_div_per_share"], errors="coerce").fillna(0)
        bb = pd.to_numeric(df["buyback_per_share"], errors="coerce").fillna(0)
        fwd = pd.to_numeric(df["forward_div_per_share"], errors="coerce").fillna(0)
        mask = (fy == 0) & (bb == 0) & (fwd == 0)
    elif target == "both":
        m1 = df["current_fy_end"].isna() | (df["current_fy_end"].astype(str).str.strip() == "")
        fy = pd.to_numeric(df["fy_div_per_share"], errors="coerce").fillna(0)
        bb = pd.to_numeric(df["buyback_per_share"], errors="coerce").fillna(0)
        fwd = pd.to_numeric(df["forward_div_per_share"], errors="coerce").fillna(0)
        m2 = (fy == 0) & (bb == 0) & (fwd == 0)
        mask = m1 | m2
    else:
        raise ValueError(f"unknown target: {target}")
    return df[mask]


def _fetch_one(code: str, fwd_override):
    """单只股票采集"""
    try:
        data = collect_financial_data(
            code,
            forward_div_override=fwd_override,
            check_dividend_history=True,
        )
        return code, data, None
    except Exception as e:
        return code, None, str(e)[:120]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", choices=["fy_end_missing", "all_zero", "both"],
                        default="fy_end_missing")
    parser.add_argument("--rounds", type=int, default=3, help="最多轮数")
    parser.add_argument("--workers", type=int, default=4, help="并发线程数")
    parser.add_argument("--sleep-between-round", type=int, default=30, help="轮间秒")
    parser.add_argument("--limit", type=int, default=0, help="单轮最多处理多少只（0=全部）")
    args = parser.parse_args()

    os.makedirs(BAK_DIR, exist_ok=True)

    # 加载预分红 override map
    try:
        fwd_map = get_forward_div_map() or {}
        print(f"已加载 forward_div_map: {len(fwd_map)} 条")
    except Exception as e:
        print(f"加载 forward_div_map 失败（继续）: {e}")
        fwd_map = {}

    for round_no in range(1, args.rounds + 1):
        print(f"\n========== Round {round_no}/{args.rounds} target={args.target} ==========")
        df = pd.read_csv(CSV, dtype={"code": str})
        df["code"] = df["code"].astype(str).str.zfill(5)

        targets_df = _select_targets(df, args.target)
        print(f"总行: {len(df)}  本轮可疑: {len(targets_df)}")
        if len(targets_df) == 0:
            print("无可疑行 → 完成")
            break

        if args.limit and args.limit < len(targets_df):
            targets_df = targets_df.head(args.limit)
            print(f"  --limit 应用，仅处理前 {args.limit} 只")

        # 备份
        bak_path = os.path.join(BAK_DIR, f"hk_financial_round_{round_no}.csv")
        shutil.copy(CSV, bak_path)
        print(f"  已备份: {bak_path}")

        codes = targets_df["code"].tolist()
        code_to_idx = {row["code"]: i for i, row in targets_df.iterrows()}

        t0 = time.time()
        completed = 0
        round_changed_rows = 0
        round_changed_fields = 0
        errors = 0
        save_every = 30

        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {
                pool.submit(_fetch_one, c, fwd_map.get(c)): c for c in codes
            }
            for f in as_completed(futures):
                code, data, err = f.result()
                completed += 1
                if err is not None:
                    errors += 1
                    if errors <= 5:
                        print(f"  [{code}] error: {err}")
                else:
                    idx = code_to_idx[code]
                    old_row = df.loc[idx]
                    new_row, n_changed = _merge_row(old_row, data)
                    if n_changed > 0:
                        df.loc[idx] = new_row
                        round_changed_rows += 1
                        round_changed_fields += n_changed

                if completed % save_every == 0:
                    elapsed = time.time() - t0
                    eta = elapsed / completed * (len(codes) - completed)
                    print(f"  进度 {completed}/{len(codes)}  改动行 {round_changed_rows}  改动字段 {round_changed_fields}  错误 {errors}  ETA {eta:.0f}s")
                    df.to_csv(CSV, index=False)

        df.to_csv(CSV, index=False)
        elapsed = time.time() - t0
        print(f"  Round {round_no} 完成 ({elapsed:.0f}s)：改动行 {round_changed_rows} / 改动字段 {round_changed_fields} / 错误 {errors}")

        if round_changed_rows == 0 and errors == 0:
            print("  本轮无改动且无错误 → 提前退出")
            break

        if round_no < args.rounds:
            sleep_s = args.sleep_between_round + random.uniform(0, 15)
            print(f"  休息 {sleep_s:.0f}s 后进入下一轮...")
            time.sleep(sleep_s)

    # 最终统计
    df = pd.read_csv(CSV, dtype={"code": str})
    df["code"] = df["code"].astype(str).str.zfill(5)
    fy_missing = df["current_fy_end"].isna().sum()
    fy = pd.to_numeric(df["fy_div_per_share"], errors="coerce").fillna(0)
    bb = pd.to_numeric(df["buyback_per_share"], errors="coerce").fillna(0)
    fwd = pd.to_numeric(df["forward_div_per_share"], errors="coerce").fillna(0)
    all_zero = ((fy == 0) & (bb == 0) & (fwd == 0)).sum()
    print(f"\n=== 最终统计 ===")
    print(f"  总行: {len(df)}")
    print(f"  current_fy_end 缺失: {fy_missing}")
    print(f"  fy/buyback/forward 全 0: {all_zero}")


if __name__ == "__main__":
    main()
