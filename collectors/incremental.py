"""
财务数据增量更新策略（按行 TTL 刷新 + B16 三态子维度容错）

B16 核心：
- collect_financial_data 返回 _status: {domain: ok|empty|fail} + _reason
- 写库时按子维度处理：
    ok    → 写新值，从 failed/empty sidecar 移除
    empty → 字段写 NaN，记 empty.json，从 failed 移除
    fail  → 保留 row 中该子维度旧值，记 failed.json
- 提供 retry_failed_only / domains 模式精准重试
"""
import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from concurrent.futures import TimeoutError as FuturesTimeoutError
from datetime import datetime
from typing import Iterable, Optional

import pandas as pd

from config.settings import (
    FINANCIAL_REFRESH_TTL_DAYS,
    FORCE_REFRESH_AFTER_FY_DAYS,
    FY_DUE_MIN_AGE_DAYS,
)
from collectors.hk_financial import (
    collect_financial_data,
    DOMAIN_FIELDS,
    ALL_DOMAINS,
)
from storage.store import load_financial_data, save_financial_data
from config.settings import CSV_DIR

logger = logging.getLogger(__name__)


FAILED_SIDECAR = os.path.join(CSV_DIR, "hk_financial_failed.json")
EMPTY_SIDECAR = os.path.join(CSV_DIR, "hk_financial_empty.json")


# ============ B45: stale 财务数据扫描 ============
# 早期采集失败（如 yfinance 拿不到财年 → get_fiscal_year_dates 返回 None → pipeline 提前退出）
# 会留下 dividend/buyback 写了但核心字段全 NaN 的股票。此处提供扫描能力，
# 供 main.py --update / --rebuild-pool 尾部自动补采。
KEY_FIELDS_STALE = [
    "ocf_3y_avg",
    "equity_latest",
    "total_liability_ratio",
    "ttm_revenue",
    "roe",
    "net_debt_ratio",
]
STALE_NAN_THRESHOLD = 3  # 关键字段中 NaN 数 >= 该阈值 视为 stale


def scan_stale_codes(
    fin_df: pd.DataFrame,
    pool_codes: Optional[Iterable[str]] = None,
    threshold: int = STALE_NAN_THRESHOLD,
    key_fields: Optional[list] = None,
) -> list:
    """
    扫描 hk_financial 中关键字段 NaN 项数 >= threshold 的 code。

    参数:
        fin_df: hk_financial 数据框（须含 code 列）
        pool_codes: 若提供，仅扫描 pool 内的 code
        threshold: NaN 计数阈值（含）
        key_fields: 覆盖默认 KEY_FIELDS_STALE

    返回:
        stale code 列表（5 位 zfill）
    """
    if fin_df is None or fin_df.empty:
        return []
    kf = key_fields or KEY_FIELDS_STALE
    kf = [c for c in kf if c in fin_df.columns]
    if not kf:
        return []
    df = fin_df.copy()
    df["code"] = df["code"].astype(str).str.zfill(5)
    if pool_codes is not None:
        pc = {str(c).zfill(5) for c in pool_codes}
        df = df[df["code"].isin(pc)]
    if df.empty:
        return []
    nan_count = df[kf].isna().sum(axis=1)
    stale_mask = nan_count >= threshold
    return df.loc[stale_mask, "code"].tolist()


# ── Sidecar I/O ─────────────────────────────────────────────────
def load_failed() -> dict:
    """{code: {domain: reason}}"""
    if os.path.exists(FAILED_SIDECAR):
        try:
            with open(FAILED_SIDECAR, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"读取 failed sidecar 失败: {e}")
    return {}


def load_empty() -> dict:
    """{code: [domain, ...]}"""
    if os.path.exists(EMPTY_SIDECAR):
        try:
            with open(EMPTY_SIDECAR, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"读取 empty sidecar 失败: {e}")
    return {}


def save_failed(d: dict) -> None:
    os.makedirs(CSV_DIR, exist_ok=True)
    with open(FAILED_SIDECAR, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2, sort_keys=True)


def save_empty(d: dict) -> None:
    os.makedirs(CSV_DIR, exist_ok=True)
    with open(EMPTY_SIDECAR, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2, sort_keys=True)


# ── 行刷新决策 ──────────────────────────────────────────────────
def needs_refresh(row: pd.Series, ttl_days: int = FINANCIAL_REFRESH_TTL_DAYS,
                  now: Optional[datetime] = None) -> tuple[bool, str]:
    now = now or datetime.now()
    updated_at = row.get("updated_at")
    if pd.isna(updated_at) or not updated_at:
        return True, "no_updated_at"
    try:
        updated_at_ts = pd.to_datetime(updated_at)
    except Exception:
        return True, "bad_updated_at"
    age_days = (now - updated_at_ts.to_pydatetime()).days
    if age_days > ttl_days:
        return True, f"stale_{age_days}d"
    fy_end = row.get("current_fy_end")
    if pd.notna(fy_end) and fy_end:
        try:
            fy_end_ts = pd.to_datetime(fy_end)
            days_since_fy = (now - fy_end_ts.to_pydatetime()).days
            if days_since_fy > FORCE_REFRESH_AFTER_FY_DAYS and age_days > FY_DUE_MIN_AGE_DAYS:
                return True, "fy_report_due"
        except Exception:
            pass
    return False, "fresh"


def _collect_one(code: str, forward_div_override: Optional[float],
                 check_dividend_history: bool, domains: Optional[list] = None) -> Optional[dict]:
    try:
        return collect_financial_data(
            code,
            forward_div_override=forward_div_override,
            check_dividend_history=check_dividend_history,
            domains=domains,
        )
    except Exception as e:
        logger.warning(f"采集失败 {code}: {e}")
        # 整体异常：请求的域标 fail、未请求的域标 skip（保留旧值，不误伤）
        want = set(domains) if domains else None
        return {
            "code": code,
            "_status": {d: ("fail" if want is None or d in want else "skip") for d in ALL_DOMAINS},
            "_reason": {d: f"top_exception: {str(e)[:160]}" for d in (ALL_DOMAINS if want is None else want)},
        }


def _merge_record_by_status(
    existing_row: Optional[pd.Series],
    new_rec: dict,
    failed_map: dict,
    empty_map: dict,
    now_str: str,
) -> dict:
    """
    根据 new_rec 的 _status 按 domain 精细合并到 existing_row：
    - ok    → 用 new_rec 的字段值
    - empty → 字段置 None
    - fail  → 保留 existing_row 中该 domain 字段旧值
    同时更新 failed_map / empty_map（in-place）
    """
    code = new_rec.get("code")
    status_map = new_rec.get("_status") or {}
    reason_map = new_rec.get("_reason") or {}

    # 起始 row：以 existing 为底
    out: dict = {}
    if existing_row is not None:
        out = {k: v for k, v in existing_row.items() if k not in ("_status", "_reason")}
    out["code"] = code

    any_fail = False
    for domain in ALL_DOMAINS:
        fields = DOMAIN_FIELDS[domain]
        st = status_map.get(domain, "fail")

        if st == "skip":
            # domains 限制下不在范围内：完全不动 row、不动 sidecar
            continue

        if st == "ok":
            for f in fields:
                if f in new_rec:
                    out[f] = new_rec.get(f)
            # 清除两个 sidecar 中此 (code, domain)
            if code in failed_map and domain in failed_map[code]:
                del failed_map[code][domain]
                if not failed_map[code]:
                    del failed_map[code]
            if code in empty_map and domain in empty_map[code]:
                empty_map[code] = [d for d in empty_map[code] if d != domain]
                if not empty_map[code]:
                    del empty_map[code]

        elif st == "empty":
            for f in fields:
                out[f] = None
            # 写入 empty
            empty_map.setdefault(code, [])
            if domain not in empty_map[code]:
                empty_map[code].append(domain)
            # 从 failed 移除
            if code in failed_map and domain in failed_map[code]:
                del failed_map[code][domain]
                if not failed_map[code]:
                    del failed_map[code]

        else:  # fail
            any_fail = True
            # 保留 existing 旧值（已在 out 中）
            failed_map.setdefault(code, {})
            failed_map[code][domain] = reason_map.get(domain, "fail")[:200]

    # 通用字段（has_dividend_history / forward_div_source / current_price 等）
    for k in ("has_dividend_history", "forward_div_source", "current_price"):
        if k in new_rec and new_rec.get(k) is not None:
            out[k] = new_rec.get(k)

    # 只有"全部 domain 都没 fail"时才更新 updated_at
    if not any_fail:
        out["updated_at"] = now_str
    elif existing_row is None or "updated_at" not in out or pd.isna(out.get("updated_at")):
        # 新股第一次抓但有 fail → 也打个时间戳避免被无限当成"无 updated_at"
        out["updated_at"] = now_str

    return out


# ── 主入口 ──────────────────────────────────────────────────────
def incremental_update_financial(
    stock_list: pd.DataFrame,
    forward_div_map: Optional[dict] = None,
    ttl_days: int = FINANCIAL_REFRESH_TTL_DAYS,
    workers: int = 4,
    force_refresh: bool = False,
    refresh_codes: Optional[Iterable[str]] = None,
    check_dividend_history: bool = False,
    current_price_map: Optional[dict] = None,
    now: Optional[datetime] = None,
    retry_failed_only: bool = False,
    retry_empty: bool = False,
    domains: Optional[list] = None,
) -> pd.DataFrame:
    """
    Args:
        retry_failed_only: 仅跑 failed.json 中的 code（无视 TTL）
        retry_empty: 把 empty.json 中的 code 也加入刷新队列（B18）
        domains: 限制采集的 domain 集合（其他 domain 不调用源/不修改 row）
                 注意：目前简化实现仍调用 collect_financial_data 全量，
                 但写库时只对 domains 内的字段生效（其他 domain 视为 'skip' 不动 row）
    """
    forward_div_map = forward_div_map or {}
    current_price_map = current_price_map or {}
    refresh_codes = set(refresh_codes) if refresh_codes else None

    stock_list = stock_list.copy()
    stock_list["code"] = stock_list["code"].astype(str).str.zfill(5)
    pool_codes = stock_list["code"].tolist()

    existing = load_financial_data()
    if not existing.empty:
        existing["code"] = existing["code"].astype(str).str.zfill(5)
        existing = existing.set_index("code", drop=False)

    failed_map = load_failed()
    empty_map = load_empty()

    # 只跑 failed sidecar 中的码
    if retry_failed_only:
        retry_codes = set(failed_map.keys())
        if refresh_codes:
            retry_codes &= refresh_codes
        if domains:
            retry_codes = {c for c in retry_codes
                           if any(d in domains for d in failed_map[c].keys())}
        retry_codes &= set(pool_codes)
        to_refresh: list[tuple[str, str]] = [(c, "retry_failed") for c in retry_codes]
        fresh: list[str] = [c for c in pool_codes if c not in retry_codes]
    else:
        to_refresh = []
        fresh = []
        for code in pool_codes:
            if refresh_codes is not None:
                if code in refresh_codes:
                    to_refresh.append((code, "user_specified"))
                elif not existing.empty and code in existing.index:
                    fresh.append(code)
                continue
            if force_refresh:
                to_refresh.append((code, "force"))
                continue
            # 失败的码无视 TTL 立即重试
            if code in failed_map:
                to_refresh.append((code, "has_failed"))
                continue
            # B18: 用户主动重试 empty
            if retry_empty and code in empty_map:
                to_refresh.append((code, "has_empty"))
                continue
            if existing.empty or code not in existing.index:
                to_refresh.append((code, "new"))
                continue
            flag, reason = needs_refresh(existing.loc[code], ttl_days=ttl_days, now=now)
            if flag:
                to_refresh.append((code, reason))
            else:
                fresh.append(code)

    reasons_count: dict = {}
    for _, r in to_refresh:
        bucket = r.split("_")[0] if r.startswith("stale") else r
        reasons_count[bucket] = reasons_count.get(bucket, 0) + 1

    logger.info(
        f"增量决策: 复用 {len(fresh)} 只, 刷新 {len(to_refresh)} 只 "
        f"(TTL={ttl_days}d, retry_failed={retry_failed_only}, retry_empty={retry_empty}, domains={domains})"
    )
    if reasons_count:
        logger.info(f"刷新原因分布: {reasons_count}")
    if failed_map:
        logger.info(f"failed sidecar: {len(failed_map)} 只待重试")
    if empty_map:
        logger.info(f"empty sidecar:  {len(empty_map)} 只有已确认空字段（不再探测）")

    now_obj = now or datetime.now()
    now_str = now_obj.strftime("%Y-%m-%d %H:%M:%S")

    # B49: 单只最长容忍时间。yfinance 内部走 curl_cffi，个别 ticker 会挂起且
    # 无全局超时（2026-08-01 实测 6979.HK 让整个 as_completed 死锁 3.5 小时）。
    PER_FUTURE_TIMEOUT = int(os.environ.get("FIN_FUTURE_TIMEOUT", "180"))
    # 每 N 只落一次盘，避免任何异常导致全量采集结果丢失
    CHECKPOINT_EVERY = int(os.environ.get("FIN_CHECKPOINT_EVERY", "100"))

    def _build_combined(records: list) -> pd.DataFrame:
        """把 new_records 与 existing 合并成待落盘的 DataFrame（供 checkpoint 与最终保存复用）"""
        if not records and existing.empty:
            return pd.DataFrame()
        if not records:
            return existing.reset_index(drop=True)
        new_df = pd.DataFrame(records)
        new_df["code"] = new_df["code"].astype(str).str.zfill(5)
        if existing.empty:
            out = new_df
        else:
            existing_reset = existing.reset_index(drop=True)
            keep_mask = ~existing_reset["code"].isin(new_df["code"])
            out = pd.concat([existing_reset[keep_mask], new_df], ignore_index=True)
        if not out.empty:
            out = out[out["code"].isin(pool_codes)].reset_index(drop=True)
        return out

    def _checkpoint(records: list, tag: str) -> None:
        """中途落盘（含 sidecar），失败不影响主流程"""
        try:
            partial = _build_combined(records)
            if partial.empty:
                return
            save_financial_data(partial)
            save_failed(failed_map)
            save_empty(empty_map)
            logger.info(f"  [checkpoint] {tag} 已落盘 {len(records)} 条新记录（累计 {len(partial)} 行）")
        except Exception as e:
            logger.warning(f"  [checkpoint] {tag} 落盘失败（不影响主流程）: {str(e)[:120]}")

    def _code_domains(code: str) -> Optional[list]:
        """该 code 本次要采集的 domain 集合；None 表示全量。

        - 外部显式 domains 优先（用户指定"只刷某域"）
        - 否则取 failed_map 里该 code 已失败的域（B52 精准重拉失败域，成功域复用缓存）
        """
        if domains:
            return list(domains)
        fd = failed_map.get(code)
        if isinstance(fd, dict):
            ds = [d for d in fd if d in ALL_DOMAINS]
            if ds:
                return ds
        return None

    new_records: list[dict] = []
    if to_refresh:
        with ThreadPoolExecutor(max_workers=workers) as exe:
            futures = {
                exe.submit(
                    _collect_one,
                    code,
                    forward_div_map.get(code),
                    check_dividend_history,
                    _code_domains(code),
                ): code
                for code, _ in to_refresh
            }
            done = 0
            total = len(futures)
            pending = set(futures.keys())
            # B49: 整体超时 = 单只超时 × 并发数的宽松倍数；防止个别 future 永久挂起
            _overall_timeout = PER_FUTURE_TIMEOUT * max(1, (total // max(1, workers)) + 2)
            try:
                for fut in as_completed(futures, timeout=_overall_timeout):
                    pending.discard(fut)
                    code = futures[fut]
                    try:
                        rec = fut.result(timeout=PER_FUTURE_TIMEOUT)
                    except Exception as e:
                        logger.warning(f"  [{code}] 采集异常/超时，记为 failed: {type(e).__name__} {str(e)[:80]}")
                        failed_map[code] = {
                            d: f"{type(e).__name__}: {str(e)[:150]}" for d in ALL_DOMAINS
                        }
                        rec = None
                    done += 1
                    if done % 25 == 0 or done == total:
                        logger.info(f"  [{done}/{total}] {code}")

                    if rec is not None:
                        # 注入 current_price
                        if code in current_price_map:
                            rec["current_price"] = current_price_map[code]

                        # B52: collect_financial_data / _collect_one 已按 domains 标 skip，此处直接合并
                        # 按 status 合并到现有 row
                        existing_row = existing.loc[code] if (not existing.empty and code in existing.index) else None
                        merged = _merge_record_by_status(existing_row, rec, failed_map, empty_map, now_str)
                        new_records.append(merged)

                    # B49: 中途落盘，避免异常导致全量丢失
                    if CHECKPOINT_EVERY > 0 and done % CHECKPOINT_EVERY == 0:
                        _checkpoint(new_records, f"{done}/{total}")
            except FuturesTimeoutError:
                # 整体超时：剩余 future 记为 failed（下次 --update 会因 failed_map 自动重试）
                logger.warning(
                    f"  as_completed 整体超时（{_overall_timeout}s），"
                    f"{len(pending)} 只未返回，标记为 failed 供下次重试"
                )
                for fut in pending:
                    code = futures[fut]
                    failed_map[code] = {
                        d: "future timeout (never returned)" for d in ALL_DOMAINS
                    }
                    fut.cancel()

    # 重新构造 DataFrame（B49: 复用 _build_combined，与 checkpoint 逻辑保持一致）
    combined = _build_combined(new_records)

    save_financial_data(combined)
    save_failed(failed_map)
    save_empty(empty_map)

    logger.info(
        f"完成: 共 {len(combined)} 行 | failed sidecar: {len(failed_map)} | empty sidecar: {len(empty_map)}"
    )
    return combined
