"""B13-2 / B15: 增量容错预分红抓取（仅东方财富源）

【B15 升级】三态分类 + 增量重试：
  - ok    → 有预分红，写入 hk_forward_dividend.csv
  - empty → AKShare 返回但确认无未来除息日（合法"无预分红"），写入 hk_forward_div_empty.txt
  - fail  → 请求异常 / 网络错误 / 解析失败，写入 hk_forward_div_failed.txt

策略：
  - 默认对 pool 全量跑一遍，最终结果 merge 进现有 CSV，**不删旧成功条目**
  - --retry-failed：只重跑 fail 列表
  - --missing：只跑既不在 ok 也不在 empty 的码（含新入池）

使用：
    python -m scripts.scrape_forward_div_em_only                  # 全量但 merge
    python -m scripts.scrape_forward_div_em_only --retry-failed   # 只重试失败的
    python -m scripts.scrape_forward_div_em_only --missing        # 只跑未抓过的
    python -m scripts.scrape_forward_div_em_only --workers 4
"""
import argparse
import logging
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).parent.parent))

import akshare as ak
import pandas as pd

from collectors.hk_forward_dividend import (
    FORWARD_DIV_CSV,
    _parse_em_div_amount,
)
from config.settings import CSV_DIR
from storage.store import load_stock_pool

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S"
)
logger = logging.getLogger(__name__)

HKT = ZoneInfo("Asia/Hong_Kong")

EMPTY_TXT = os.path.join(CSV_DIR, "hk_forward_div_empty.txt")
FAILED_TXT = os.path.join(CSV_DIR, "hk_forward_div_failed.txt")


def fetch_one(code: str) -> tuple[str, str, dict | str]:
    """返回 (code, status, payload)，status ∈ {'ok','empty','fail'}。

    - ok: payload = dict (forward_div 记录)
    - empty: payload = "" (合法 - AK 返回了，但无未除息分红)
    - fail: payload = str (异常原因)
    """
    try:
        df = ak.stock_hk_dividend_payout_em(symbol=code)
    except Exception as e:
        return code, "fail", f"akshare_error: {e!s}"[:200]

    if df is None or len(df) == 0:
        # AKShare 返回空 DataFrame：通常是该股票从未在东财留下分红记录。
        # 视为 "empty" 即可（不 fail），下次 --missing 不会重跑。
        return code, "empty", ""

    today = datetime.now(HKT).date()
    last_parse_err: str | None = None

    for _, row in df.iterrows():
        ex_date_str = str(row.get("除净日", ""))
        if not ex_date_str or ex_date_str in ("nan", "NaT"):
            continue
        try:
            ex_date = datetime.strptime(ex_date_str.strip()[:10], "%Y-%m-%d").date()
        except ValueError as e:
            last_parse_err = f"date_parse: {ex_date_str} ({e})"
            continue

        if ex_date <= today:
            continue

        plan_text = str(row.get("分红方案", ""))
        amount_hkd, currency = _parse_em_div_amount(plan_text)
        if amount_hkd is None or amount_hkd <= 0:
            last_parse_err = f"amount_parse: {plan_text[:60]}"
            continue

        return code, "ok", {
            "code": code,
            "forward_div_per_share": amount_hkd,
            "ex_date": ex_date.isoformat(),
            "announcement_title": plan_text,
            "announcement_date": "",
            "source": "eastmoney",
        }

    # 没找到任何 ex_date > today 的有效记录
    if last_parse_err:
        # 数据存在但解析失败 → fail（值得重试或人工查）
        return code, "fail", last_parse_err
    return code, "empty", ""


def _load_lines(path: str) -> set[str]:
    if not os.path.exists(path):
        return set()
    with open(path, encoding="utf-8") as f:
        return {ln.strip().zfill(5) for ln in f if ln.strip()}


def _save_lines(path: str, codes: set[str]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for c in sorted(codes):
            f.write(c + "\n")


def _load_existing_success() -> dict[str, dict]:
    """读现有 CSV 转成 {code: row_dict}（保留完整列）。"""
    if not os.path.exists(FORWARD_DIV_CSV):
        return {}
    df = pd.read_csv(FORWARD_DIV_CSV, dtype=str)
    if "code" not in df.columns or df.empty:
        return {}
    df["code"] = df["code"].astype(str).str.zfill(5)
    out: dict[str, dict] = {}
    for _, r in df.iterrows():
        out[r["code"]] = {k: r.get(k, "") for k in df.columns}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument(
        "--retry-failed",
        action="store_true",
        help="只重试上次 fail 列表中的码",
    )
    ap.add_argument(
        "--missing",
        action="store_true",
        help="只跑既不在 ok 也不在 empty 的码（新入池或从未跑过）",
    )
    args = ap.parse_args()

    # 加载历史状态
    success_map = _load_existing_success()
    success_set = set(success_map.keys())
    empty_set = _load_lines(EMPTY_TXT)
    failed_set = _load_lines(FAILED_TXT)

    sl = load_stock_pool()
    sl["code"] = sl["code"].astype(str).str.zfill(5)
    pool_codes = set(sl["code"].tolist())

    # 决定本次 codes
    if args.retry_failed:
        codes = sorted(failed_set & pool_codes)
        mode = "retry-failed"
    elif args.missing:
        codes = sorted(pool_codes - success_set - empty_set)
        mode = "missing"
    else:
        codes = sorted(pool_codes)
        mode = "full-merge"

    logger.info(
        f"[{mode}] 待抓 {len(codes)} 只 (workers={args.workers}); "
        f"现有 ok={len(success_set)}, empty={len(empty_set)}, failed={len(failed_set)}, pool={len(pool_codes)}"
    )

    if not codes:
        logger.info("无待抓股票，退出。")
        return

    t0 = time.time()
    new_ok: dict[str, dict] = {}
    new_empty: set[str] = set()
    new_fail: dict[str, str] = {}

    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as exe:
        futs = {exe.submit(fetch_one, c): c for c in codes}
        for fut in as_completed(futs):
            done += 1
            code, status, payload = fut.result()
            if status == "ok":
                new_ok[code] = payload  # type: ignore[assignment]
            elif status == "empty":
                new_empty.add(code)
            else:
                new_fail[code] = str(payload)
            if done % 50 == 0 or done == len(codes):
                logger.info(
                    f"  [{done}/{len(codes)}] ok={len(new_ok)} empty={len(new_empty)} "
                    f"fail={len(new_fail)} elapsed={time.time()-t0:.0f}s"
                )

    # === Merge ===
    # success: 旧 success_map ∪ new_ok（new_ok 覆盖同码旧记录）
    merged_success = dict(success_map)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for code, rec in new_ok.items():
        rec["updated_at"] = now_str
        merged_success[code] = rec
        # 抓到 ok 后，从 empty/failed 移除
        empty_set.discard(code)
        failed_set.discard(code)

    # empty: 累加 new_empty；从 failed 移除
    for code in new_empty:
        empty_set.add(code)
        failed_set.discard(code)
        # 若以前是 ok 但现在 empty → 保留 ok（公司可能撤销了未来分红，但保留更安全；用户可手删）
        # 这里选择保留旧 ok 记录避免误删

    # failed: 累加 new_fail（同码旧 fail 信息被覆盖）；保留未本次跑的旧 fail
    for code, reason in new_fail.items():
        failed_set.add(code)

    # 写 CSV
    rows = list(merged_success.values())
    columns = [
        "code",
        "forward_div_per_share",
        "ex_date",
        "announcement_title",
        "announcement_date",
        "source",
        "updated_at",
    ]
    df = pd.DataFrame(rows, columns=columns) if rows else pd.DataFrame(columns=columns)
    if not df.empty:
        df["code"] = df["code"].astype(str).str.zfill(5)
        df = df.sort_values("code").reset_index(drop=True)
    os.makedirs(os.path.dirname(FORWARD_DIV_CSV), exist_ok=True)
    df.to_csv(FORWARD_DIV_CSV, index=False, encoding="utf-8-sig")
    _save_lines(EMPTY_TXT, empty_set)
    _save_lines(FAILED_TXT, failed_set)

    logger.info(
        f"[{mode}] 本次新增/更新: ok+{len(new_ok)} empty+{len(new_empty)} fail+{len(new_fail)}"
    )
    logger.info(
        f"累计状态: ok={len(merged_success)} empty={len(empty_set)} failed={len(failed_set)}"
    )
    logger.info(f"已保存 -> {FORWARD_DIV_CSV} (耗时 {time.time()-t0:.0f}s)")
    if new_fail:
        sample = list(new_fail.items())[:5]
        logger.info(f"fail 示例: {sample}")


if __name__ == "__main__":
    main()
