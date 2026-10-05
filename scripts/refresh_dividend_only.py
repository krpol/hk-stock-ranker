"""
仅刷新 hk_financial.csv 的分红字段（B1 修复立即生效用）

【C1 重构】统一使用 engine.dividend_attribution.compute_dividend_fields。
本脚本与 collectors/hk_financial.py:get_fiscal_year_dividends 共享同一计算逻辑，
保证两条路径的输出 byte-for-byte 一致。

调用 AKShare `stock_hk_dividend_payout_em` 接口（含「财政年度」字段），
按统一权威算法更新以下字段：
    fy_div_per_share
    prev_year_div_per_share
    two_years_ago_div_per_share
    latest_period_div_per_share
    latest_period_yoy_div_per_share
    forward_div_per_share
    latest_attributed_fy
    fiscal_periods

不刷新：current_price、buyback、shares_outstanding、ttm_revenue 等其他字段。
不打 yfinance（避免与正在跑的回填进程争抢限流）。

使用：
    python scripts/refresh_dividend_only.py
"""
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import pandas as pd

# 让脚本可独立运行
sys.path.insert(0, str(Path(__file__).parent.parent))

from collectors.hk_dividend_history import get_hk_dividends_with_fy_attribution
from collectors.hk_forward_dividend import get_forward_div_map
from engine.dividend_attribution import DividendRecord, compute_dividend_fields
from storage.store import load_financial_data, save_financial_data

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("refresh_div")


# 港股年报锚点：默认 12-31，少数为 6-30/3-31，从已有 CSV 中读取
def get_fy_anchor_for_code(row) -> tuple:
    cur_fy = row.get("current_fy_end")
    if pd.notna(cur_fy):
        try:
            d = pd.Timestamp(cur_fy)
            return (d.month, d.day)
        except Exception:
            pass
    return (12, 31)


def _build_periods_from_records(records, fy_anchor, today, current_fy_year_end):
    """
    从派息记录的 fiscal_year + CSV 已知的 current_fy_end 推断 periods 列表。

    refresh 路径不调用 yfinance，无法直接拿到 _get_fiscal_period_ends 的完整结果。
    策略：
    - 以 current_fy_year_end 为锚，向前推至少 5 个年度（覆盖派息历史）
    - 同时为每个年报年度生成对应的中报期（fy - 6 个月）
    """
    if current_fy_year_end is None:
        return []
    # 派息记录覆盖的年度
    fy_years = {int(r.fiscal_year) for r in records if r.fiscal_year is not None}
    fy_years.add(current_fy_year_end.year)
    # 拓展到至少包含上 4 个完整年度（用于 prev_year / two_years_ago）
    min_year = min(fy_years) - 1 if fy_years else current_fy_year_end.year - 4
    max_year = max(fy_years) + 1 if fy_years else current_fy_year_end.year
    min_year = min(min_year, current_fy_year_end.year - 4)
    max_year = max(max_year, current_fy_year_end.year)

    periods = []
    for yr in range(min_year, max_year + 1):
        try:
            year_end = pd.Timestamp(year=yr, month=fy_anchor[0], day=fy_anchor[1])
        except Exception:
            continue
        periods.append(year_end)
        # 中报期 = 年报期 - 6 个月
        try:
            interim = (year_end - pd.DateOffset(months=6)).normalize()
            periods.append(interim)
        except Exception:
            pass
    return periods


def recompute_dividends_for_code(code: str, row: pd.Series,
                                   forward_override: float = None,
                                   today: pd.Timestamp = None) -> dict:
    """
    从 AKShare 派息表 + CSV 中已有的 fy 锚点重新计算分红字段。

    【C1 重构】内部委托给 engine.dividend_attribution.compute_dividend_fields。

    today: 基准日（默认系统当天），仅供测试注入固定时点。

    Returns: dict 包含要更新的列；空 dict 表示跳过（无数据）。
    """
    today = pd.Timestamp(today).normalize() if today is not None \
        else pd.Timestamp.today().normalize()
    fy_anchor = get_fy_anchor_for_code(row)

    raw_records = get_hk_dividends_with_fy_attribution(code)
    if not raw_records:
        return {}

    # 转换为统一 DividendRecord（NaT 由 compute_dividend_fields 的 R1 自动跳过）
    records = [
        DividendRecord(
            ex_date=r.get("ex_date"),
            amount_hkd=float(r.get("amount_hkd") or 0.0),
            fiscal_year=r.get("fiscal_year"),
            distribution_type=str(r.get("distribution_type") or ""),
            source="akshare",
        )
        for r in raw_records
    ]

    # current_fy_year_end: 当前未结束的财年（如果未到年报披露日，取今年；否则用 csv 已有）
    cur_fy_str = row.get("current_fy_end")
    if pd.notna(cur_fy_str):
        current_fy_year_end = pd.Timestamp(cur_fy_str)
    else:
        current_fy_year_end = pd.Timestamp(year=today.year, month=fy_anchor[0], day=fy_anchor[1])
        if current_fy_year_end > today + pd.DateOffset(months=4):
            current_fy_year_end = pd.Timestamp(year=today.year - 1, month=fy_anchor[0], day=fy_anchor[1])

    # 构造 periods（refresh 路径无 yfinance，从 fy_anchor + 派息历史推断）
    periods = _build_periods_from_records(records, fy_anchor, today, current_fy_year_end)
    if not periods:
        return {}

    out = compute_dividend_fields(
        records=records,
        periods=periods,
        fy_anchor=fy_anchor,
        today=today,
        forward_override=forward_override,
    )
    # refresh 脚本更新计算字段 + 财报期时间字段（B5：latest_period_end 也需随分红刷新）
    return {
        "fy_div_per_share": out["fy_div_per_share"],
        "prev_year_div_per_share": out["prev_year_div_per_share"],
        "two_years_ago_div_per_share": out["two_years_ago_div_per_share"],
        "latest_period_div_per_share": out["latest_period_div_per_share"],
        "latest_period_yoy_div_per_share": out["latest_period_yoy_div_per_share"],
        "forward_div_per_share": out["forward_div_per_share"],
        "latest_attributed_fy": out["latest_attributed_fy"],
        "fiscal_periods": out["fiscal_periods"],
        "latest_period_end": out["latest_period_end"],
        "latest_period_yoy_end": out["latest_period_yoy_end"],
    }


def process_one(args):
    idx, row, forward_map = args
    code = str(row["code"]).zfill(5)
    forward_override = forward_map.get(code)
    try:
        updates = recompute_dividends_for_code(code, row, forward_override)
        return idx, code, updates, None
    except Exception as e:
        return idx, code, {}, str(e)


def main():
    df = load_financial_data()
    if df.empty:
        logger.error("hk_financial.csv 为空，先跑 --update")
        return
    df["code"] = df["code"].astype(str).str.zfill(5)
    logger.info(f"加载 {len(df)} 只股票的财务数据")

    forward_map = get_forward_div_map()
    logger.info(f"加载 {len(forward_map)} 只股票的 HKEX 预派息覆盖")

    start = time.time()
    updated_count = 0
    skipped_count = 0
    error_count = 0

    # 并发拉 AKShare（每股 ~0.5s）
    args_list = [(idx, row, forward_map) for idx, row in df.iterrows()]
    n = len(args_list)

    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(process_one, a): a[0] for a in args_list}
        for i, fut in enumerate(as_completed(futures)):
            idx, code, updates, err = fut.result()
            if err:
                logger.warning(f"[{code}] 失败: {err}")
                error_count += 1
                continue
            if not updates:
                skipped_count += 1
                continue
            for k, v in updates.items():
                df.at[idx, k] = v
            updated_count += 1
            if (i + 1) % 50 == 0 or i + 1 == n:
                elapsed = time.time() - start
                rate = (i + 1) / max(elapsed, 0.1)
                eta_min = (n - i - 1) / max(rate, 0.01) / 60
                logger.info(f"[{i+1}/{n}] 速度 {rate:.1f} 股/秒 ETA {eta_min:.1f} 分；"
                            f"更新 {updated_count} 跳过 {skipped_count} 失败 {error_count}")

    # 更新 updated_at 字段
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    df["updated_at"] = df["updated_at"].astype(str)  # 防止 dtype 不一致
    df["updated_at"] = now_str

    save_financial_data(df)
    logger.info(f"完成：更新 {updated_count}/{n} 只，跳过 {skipped_count}，失败 {error_count}")
    logger.info(f"总耗时 {(time.time() - start) / 60:.1f} 分钟")


if __name__ == "__main__":
    main()

