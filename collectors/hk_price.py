"""
港股价格与基本行情采集
来源：AKShare（东方财富）
"""
import time
import logging
import socket
from typing import Optional
import requests
import pandas as pd
import akshare as ak
from config.settings import AKSHARE_SLEEP

logger = logging.getLogger(__name__)

# 东财限流识别（akshare 抛出的限流异常文本）
_RATE_LIMIT_KEYWORDS = ("限流", "RateLimit", "Too Many Requests", "push2 行情服务器断开", "断开了连接")
_last_rate_limit_wait = 0.0


def _is_rate_limited(e: Exception) -> bool:
    return any(k in str(e) for k in _RATE_LIMIT_KEYWORDS)


def _rate_limit_backoff(seconds: float = 60.0) -> None:
    """触发东财限流时做一次全局退避（30s 去重，避免并发下多 worker 重复等待）。"""
    global _last_rate_limit_wait
    now = time.time()
    if now - _last_rate_limit_wait < 30:
        return
    _last_rate_limit_wait = now
    logger.warning(f"检测到东财限流，全局退避 {seconds:.0f}s（等待期间勿重复请求）...")
    time.sleep(seconds)

# 东方财富港股行情接口（直接请求，绕开 AKShare Session 问题）
_EM_HK_URL = "https://72.push2.eastmoney.com/api/qt/clist/get"
_EM_HK_PARAMS_BASE = {
    "pn": "1",
    "pz": "5000",   # 一次取全量（港股约4600只）
    "po": "1",
    "np": "1",
    "ut": "bd1d9ddb04089700cf9c27f6f7426281",
    "fltt": "2",
    "invt": "2",
    "fid": "f12",
    "fs": "m:128 t:3,m:128 t:4,m:128 t:1,m:128 t:2",
    # f12=代码 f14=名称 f2=最新价 f3=涨跌幅 f5=成交量 f6=成交额
    "fields": "f12,f14,f2,f3,f5,f6",
}
_EM_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/124.0.0.0 Safari/537.36",
    "Referer": "https://quote.eastmoney.com/",
}


def _fetch_hk_spot_direct(max_retries: int = 5) -> pd.DataFrame:
    """
    直接调用系统 curl 请求东方财富接口获取港股行情。
    绕开 Python requests/urllib3 的 TLS fingerprint 被服务器封锁的问题。
    """
    import subprocess, json, shutil

    if not shutil.which("curl"):
        logger.warning("系统未找到 curl，跳过直接请求")
        return pd.DataFrame()

    url = (
        "https://72.push2.eastmoney.com/api/qt/clist/get"
        "?pn=1&pz=5000&po=1&np=1"
        "&ut=bd1d9ddb04089700cf9c27f6f7426281"
        "&fltt=2&invt=2&fid=f12"
        "&fs=m:128%20t:3,m:128%20t:4,m:128%20t:1,m:128%20t:2"
        "&fields=f12,f14,f2,f3,f5,f6"
    )
    cmd = [
        "curl", "-s", "--max-time", "30",
        "-H", "User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "-H", "Referer: https://quote.eastmoney.com/",
        url,
    ]

    for attempt in range(1, max_retries + 1):
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=35)
            if result.returncode != 0 or not result.stdout:
                raise RuntimeError(f"curl 返回码 {result.returncode}: {result.stderr[:100]}")
            data = json.loads(result.stdout)
            diff = data.get("data", {}).get("diff", [])
            if not diff:
                raise ValueError("返回数据为空")
            rows = [
                {
                    "code": str(item.get("f12", "")).zfill(5),
                    "name": item.get("f14", ""),
                    "price": item.get("f2"),
                    "pct_change": item.get("f3"),
                    "volume": item.get("f5"),
                    "amount": item.get("f6"),
                }
                for item in diff
            ]
            df = pd.DataFrame(rows)
            logger.info(f"curl 直接请求成功，获取 {len(df)} 只港股")
            return df
        except Exception as e:
            wait = 5 * attempt
            logger.warning(f"curl 请求港股行情失败（第{attempt}/{max_retries}次）: {e}，{wait}秒后重试...")
            time.sleep(wait)

    logger.error("curl 请求港股行情：已达最大重试次数")
    return pd.DataFrame()

def get_hk_stock_list(max_retries: int = 5) -> pd.DataFrame:
    """
    获取港股主板全量股票列表（含实时行情快照）。
    按优先级依次尝试：
      1. AKShare stock_hk_spot_em（东方财富，字段最全）
      2. curl 直接请求东方财富接口（stock_hk_spot 港交所源已在 akshare 移除）

    过滤逻辑：
      1. 成交量为 0 → 当日停牌，直接过滤
      2. 用当日成交额（amount）替代 market_cap 字段，用于 min_market_cap 门槛过滤
    """
    df = pd.DataFrame()

    # ---- 方案1：AKShare stock_hk_spot_em（东方财富）----
    for attempt in range(1, max_retries + 1):
        try:
            df = ak.stock_hk_spot_em()
            rename_map = {
                "代码": "code", "名称": "name", "最新价": "price",
                "涨跌幅": "pct_change", "成交量": "volume", "成交额": "amount",
                "市盈率(动)": "pe_ttm", "市净率": "pb",
                "总市值": "market_cap", "流通市值": "float_market_cap",
            }
            df = df.rename(columns={k: v for k, v in rename_map.items() if k in df.columns})
            df["code"] = df["code"].astype(str).str.zfill(5)
            logger.info(f"AKShare stock_hk_spot_em 成功，共 {len(df)} 只")
            break
        except Exception as e:
            wait = 3 * attempt
            logger.warning(f"stock_hk_spot_em 失败（第{attempt}/{max_retries}次）: {e}，{wait}秒后重试...")
            time.sleep(wait)

    # ---- 方案2：curl 直接请求东方财富（港交所源 stock_hk_spot 已在 akshare 移除）----
    if df.empty:
        logger.info("回退到 curl 直接请求东方财富...")
        df = _fetch_hk_spot_direct(max_retries=max_retries)

    if df.empty:
        logger.error("获取港股列表失败（所有方案均已尝试）")
        return pd.DataFrame()

    # 不再用 volume=0 判断停牌 —— 零成交量不一定是停牌（很多仙股交易不活跃）
    # 流动性过滤只需靠成交额门槛（main.py Step 2: min_market_cap >= 10000）

    # market_cap 回退到成交额
    if "market_cap" not in df.columns and "amount" in df.columns:
        df["market_cap"] = df["amount"]
        logger.info("已用当日成交额（amount）代替 market_cap 做流动性过滤")

    return df


def _fetch_yf_monthly(code: str, start_date: str, end_date: str) -> pd.DataFrame:
    """
    用 yfinance 拉单只港股月线，一次请求同时拿到不复权价 + 复权价（Adj Close）。

    返回列: date, code, open, close, high, low, volume, amount, close_qfq
        close      —— 原始（不复权）收盘价，用于 PE/PB/股息率/ranking 展示价
        close_qfq  —— 复权收盘价（Adj Close，股息+拆股全复权），用于动量/价格变化率/回测
        amount     —— close × volume 近似成交额（港元）

    date 统一为月末（MonthEnd），与既有东财历史数据口径对齐，避免同月两条记录。
    """
    import yfinance as yf
    code = str(code).zfill(5)
    ticker = _code_to_yf_ticker(code)
    start = pd.to_datetime(start_date).strftime("%Y-%m-%d")
    end = pd.to_datetime(end_date).strftime("%Y-%m-%d")

    for attempt in range(1, 4):
        try:
            hist = yf.Ticker(ticker).history(
                start=start, end=end, interval="1mo", auto_adjust=False,
            )
            if hist is None or hist.empty:
                return pd.DataFrame()
            hist = hist.copy()
            hist.index = hist.index + pd.offsets.MonthEnd(0)  # 月初 → 月末
            df = pd.DataFrame({
                "date": hist.index.strftime("%Y-%m-%d"),
                "code": code,
                "open": hist["Open"].astype(float),
                "close": hist["Close"].astype(float),
                "high": hist["High"].astype(float),
                "low": hist["Low"].astype(float),
                "volume": hist["Volume"].astype(float).fillna(0),
                "amount": (hist["Close"] * hist["Volume"]).astype(float).fillna(0),
                "close_qfq": hist["Adj Close"].astype(float),
            })
            df = df.dropna(subset=["close"]).reset_index(drop=True)
            return df[["date", "code", "open", "close", "high", "low", "volume", "amount", "close_qfq"]]
        except Exception as e:
            if _is_rate_limited(e):
                _rate_limit_backoff()
                return pd.DataFrame()
            if attempt < 3:
                time.sleep(2 * attempt)
            else:
                logger.warning(f"[{code}] yfinance 月线拉取失败: {str(e)[:80]}")

    logger.warning(f"[{code}] yfinance 月线达最大重试次数")
    mark_monthly_failed(code, "monthly price max retries")   # B51: 网络失败记录，下次启动优先重试
    return pd.DataFrame()


def get_hk_monthly_price(
    code: str,
    start_date: str,
    end_date: str,
    adjust: str = "hfq",
    max_retries: int = 3,
) -> pd.DataFrame:
    """
    获取港股月度收盘价（复权）。数据源：yfinance（走统一出口代理）。

    adjust:
        ""        —— 不复权（原始收盘价）
        "qfq"/"hfq" —— 复权价（Adj Close，股息+拆股全复权），用于动量/回测

    Returns:
        DataFrame with columns: date, code, open, close, high, low, volume, amount
    """
    code = str(code).zfill(5)   # B51: 统一 5 位，保证 failed sidecar key 一致
    df = _fetch_yf_monthly(code, start_date, end_date)
    if df.empty:
        return pd.DataFrame()
    if adjust in ("qfq", "hfq"):
        df = df.assign(close=df["close_qfq"])
    clear_monthly_failed(code)   # B51: 成功拉取，清除失败记录
    return df[["date", "code", "open", "close", "high", "low", "volume", "amount"]]


def get_hk_monthly_price_dual(
    code: str,
    start_date: str,
    end_date: str,
    max_retries: int = 3,
) -> pd.DataFrame:
    """
    获取港股月度价格的【原始价 + 复权价】双价版本。数据源：yfinance（一次请求）。

    返回列: date, code, open, close, high, low, volume, amount, close_qfq
        close      —— 原始（不复权）收盘价，用于 PE/PB/股息率/ranking 展示价
        close_qfq  —— 复权收盘价（Adj Close），用于动量/价格变化率/回测
        OHL/V/amount 维持不复权口径
    """
    code = str(code).zfill(5)
    return _fetch_yf_monthly(code, start_date, end_date)


def get_hk_current_price(code: str) -> Optional[float]:
    """获取港股当前（最新）收盘价（不复权），数据源：yfinance。"""
    import yfinance as yf
    code = str(code).zfill(5)
    try:
        hist = yf.Ticker(_code_to_yf_ticker(code)).history(period="5d", auto_adjust=False)
        if hist is None or hist.empty:
            return None
        close = float(hist["Close"].dropna().iloc[-1])
        return close if close > 0 else None
    except Exception as e:
        if _is_rate_limited(e):
            _rate_limit_backoff()
        else:
            logger.warning(f"[{code}] 获取当前价格失败: {e}")
        return None


def get_hk_pe_pb(code: str) -> dict:
    """
    获取 PE/PB（先尝试 spot 行情，再尝试 financial_indicator_em）
    返回 {"pe_ttm": float, "pb": float}
    """
    try:
        # 优先尝试逐只财务指标接口（更准确，含市值/PE/PB）
        df = ak.stock_hk_financial_indicator_em(symbol=code)
        if not df.empty:
            row = df.iloc[0]
            return {
                "pe_ttm": float(row.get("市盈率", float("nan"))),
                "pb": float(row.get("市净率", float("nan"))),
                "market_cap": float(row.get("总市值(港元)", float("nan"))),
            }
    except Exception:
        pass

    # 回退：旧版 spot 行情（若包含 PE/PB 列）
    try:
        df = ak.stock_hk_spot_em()
        df["代码"] = df["代码"].astype(str).str.zfill(5)
        row = df[df["代码"] == code]
        if not row.empty:
            return {
                "pe_ttm": float(row.iloc[0].get("市盈率(动)", float("nan"))),
                "pb": float(row.iloc[0].get("市净率", float("nan"))),
            }
    except Exception:
        pass

    return {"pe_ttm": None, "pb": None}


def _code_to_yf_ticker(code: str) -> str:
    """5 位港股代码 → yfinance ticker：去前导零、左补零到至少 4 位 + .HK。
    例如 '01245' → '1245.HK'，'00700' → '0700.HK'，'00005' → '0005.HK'。"""
    return f"{int(code):04d}.HK"


def _fetch_single_max_amount(code: str, lookback_days: int) -> tuple[str, float]:
    """
    用 yfinance 拉单只港股近 N 个【日历日】的 daily hist，返回 max(close*volume)。
    失败/无数据返回 (code, 0.0) —— 保守视为不满足流动性。

    注：yfinance 的 start/end 是日历日，N 个日历日大致覆盖 ⌈N*5/7⌉ 个交易日。
        为保证至少覆盖 lookback_days 个交易日，向前推 lookback_days * 2 个日历日。
    """
    try:
        import yfinance as yf
        from datetime import datetime, timedelta

        ticker = _code_to_yf_ticker(code)
        end = datetime.now() + timedelta(days=1)  # 含今日
        start = end - timedelta(days=max(lookback_days * 2, 10))

        t = yf.Ticker(ticker)
        hist = t.history(start=start.strftime("%Y-%m-%d"),
                         end=end.strftime("%Y-%m-%d"),
                         auto_adjust=False)
        if hist.empty:
            return code, 0.0

        # 取最近 lookback_days 个交易日（hist 已按日期升序）
        hist = hist.tail(lookback_days)
        amount = (hist["Close"].fillna(0) * hist["Volume"].fillna(0)).max()
        return code, float(amount)
    except Exception as e:
        logger.debug(f"yfinance 拉 {code} 失败：{e}")
        return code, 0.0


def get_hk_recent_amount_window(
    codes: list,
    lookback_days: int = 5,
    max_workers: int = 10,
    log_every: int = 100,
) -> dict:
    """
    批量拉取一组港股近 N 个交易日的 max(daily_amount=close*volume)。

    用途：rebuild-pool 时对"单日快照成交额=0"的票做窗口复核，
          避免仙股/冷门票因 rebuild 当天恰好停牌而被永久踢出。

    参数：
        codes: 5 位字符串 code 列表
        lookback_days: 回看交易日数（默认 5）
        max_workers: yfinance 并发线程数

    返回：{code: max_amount_in_window}，失败的 code 返回 0.0
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    if not codes:
        return {}

    logger.info(f"开始拉取 {len(codes)} 只票近 {lookback_days} 日 max(amount)，并发 {max_workers}...")
    results: dict = {}
    start_t = time.time()

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(_fetch_single_max_amount, c, lookback_days): c for c in codes}
        for i, fut in enumerate(as_completed(futures), 1):
            code, amt = fut.result()
            results[code] = amt
            if i % log_every == 0:
                elapsed = time.time() - start_t
                logger.info(f"  进度 [{i}/{len(codes)}] 已耗时 {elapsed:.1f}s")

    elapsed = time.time() - start_t
    nonzero = sum(1 for v in results.values() if v > 0)
    logger.info(f"窗口复核完成：{len(results)} 只，其中 {nonzero} 只有有效数据，耗时 {elapsed:.1f}s")
    return results


# ============================================================
# B46: 按 as_of_date 拉取真实历史行情（pool snapshot 流动性字段回填）
# ============================================================
import os as _os
import json as _json
import pickle as _pickle
from datetime import datetime as _datetime, timedelta as _timedelta

_HK_DAILY_CACHE_DIR = _os.path.join("data", "cache", "hk_daily")
_HK_DAILY_FAILED_PATH = _os.path.join("data", "cache", "hk_daily_failed.json")
_FAILED_MAX_ATTEMPTS = 5  # attempts 达到此值后跳过（视为死码：退市/长期停牌）


def _daily_cache_path(code: str) -> str:
    _os.makedirs(_HK_DAILY_CACHE_DIR, exist_ok=True)
    return _os.path.join(_HK_DAILY_CACHE_DIR, f"{code}.pkl")


# ---------- Failed sidecar (持久化重试状态) ----------
_failed_daily_cache: Optional[dict] = None  # 内存单例


def _load_failed_daily() -> dict:
    global _failed_daily_cache
    if _failed_daily_cache is not None:
        return _failed_daily_cache
    if not _os.path.exists(_HK_DAILY_FAILED_PATH):
        _failed_daily_cache = {}
        return _failed_daily_cache
    try:
        with open(_HK_DAILY_FAILED_PATH, "r", encoding="utf-8") as f:
            _failed_daily_cache = _json.load(f)
    except Exception:
        _failed_daily_cache = {}
    return _failed_daily_cache


def _save_failed_daily() -> None:
    if _failed_daily_cache is None:
        return
    try:
        _os.makedirs(_os.path.dirname(_HK_DAILY_FAILED_PATH), exist_ok=True)
        with open(_HK_DAILY_FAILED_PATH, "w", encoding="utf-8") as f:
            _json.dump(_failed_daily_cache, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.debug(f"保存 failed sidecar 失败: {e}")


def _mark_failed(code: str, err: str) -> None:
    """attempts+1 记录失败"""
    fd = _load_failed_daily()
    prev = fd.get(code, {})
    fd[code] = {
        "attempts": int(prev.get("attempts", 0)) + 1,
        "last_try": _datetime.now().isoformat(timespec="seconds"),
        "last_error": str(err)[:200],
    }


def _mark_dead(code: str, err: str) -> None:
    """立即标记为死码（跳过后续所有尝试）"""
    fd = _load_failed_daily()
    fd[code] = {
        "attempts": _FAILED_MAX_ATTEMPTS,
        "last_try": _datetime.now().isoformat(timespec="seconds"),
        "last_error": f"[dead] {str(err)[:200]}",
    }


def _clear_failed(code: str) -> None:
    fd = _load_failed_daily()
    if code in fd:
        fd.pop(code, None)


def _is_dead_code(code: str) -> bool:
    fd = _load_failed_daily()
    rec = fd.get(code)
    return bool(rec and int(rec.get("attempts", 0)) >= _FAILED_MAX_ATTEMPTS)


# ---------- B51: 月度价格 Failed sidecar（失败追踪 + 下次启动优先重试） ----------
_HK_MONTHLY_FAILED_PATH = _os.path.join("data", "cache", "hk_monthly_price_failed.json")

_failed_monthly_cache: Optional[dict] = None  # 内存单例


def _load_failed_monthly() -> dict:
    global _failed_monthly_cache
    if _failed_monthly_cache is not None:
        return _failed_monthly_cache
    if not _os.path.exists(_HK_MONTHLY_FAILED_PATH):
        _failed_monthly_cache = {}
        return _failed_monthly_cache
    try:
        with open(_HK_MONTHLY_FAILED_PATH, "r", encoding="utf-8") as f:
            _failed_monthly_cache = _json.load(f)
    except Exception:
        _failed_monthly_cache = {}
    return _failed_monthly_cache


def save_monthly_failed() -> None:
    """持久化月度价格失败 sidecar（供 main.py Step 2 尾部调用）。"""
    if _failed_monthly_cache is None:
        return
    try:
        _os.makedirs(_os.path.dirname(_HK_MONTHLY_FAILED_PATH), exist_ok=True)
        with open(_HK_MONTHLY_FAILED_PATH, "w", encoding="utf-8") as f:
            _json.dump(_failed_monthly_cache, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.debug(f"保存 monthly failed sidecar 失败: {e}")


def mark_monthly_failed(code: str, err: str) -> None:
    """attempts+1 记录月度价格拉取失败（仅网络失败，非数据为空）。"""
    fd = _load_failed_monthly()
    prev = fd.get(code, {})
    fd[code] = {
        "attempts": int(prev.get("attempts", 0)) + 1,
        "last_try": _datetime.now().isoformat(timespec="seconds"),
        "last_error": str(err)[:200],
    }


def clear_monthly_failed(code: str) -> None:
    """成功拉取后清除失败记录。"""
    fd = _load_failed_monthly()
    if code in fd:
        fd.pop(code, None)


def load_monthly_failed_codes(max_attempts: Optional[int] = None) -> list:
    """返回失败 code 列表（attempts < 死码阈值），供下次启动优先重试。"""
    fd = _load_failed_monthly()
    limit = max_attempts if max_attempts is not None else _FAILED_MAX_ATTEMPTS
    return [c for c, r in fd.items() if int(r.get("attempts", 0)) < limit]


# ---------- Cache (永久有效) ----------
def _load_daily_cache(code: str) -> Optional[pd.DataFrame]:
    """B47: 缓存永久有效（历史日线是不变量）。仅"缓存文件不存在"才 miss。"""
    p = _daily_cache_path(code)
    if not _os.path.exists(p):
        return None
    try:
        with open(p, "rb") as f:
            return _pickle.load(f)
    except Exception:
        return None


def _save_daily_cache(code: str, df: pd.DataFrame) -> None:
    try:
        with open(_daily_cache_path(code), "wb") as f:
            _pickle.dump(df, f)
    except Exception as e:
        logger.debug(f"缓存 {code} 失败: {e}")


def _fetch_single_daily(code: str, use_cache: bool = True, force: bool = False) -> Optional[pd.DataFrame]:
    """
    拉某 code 全量日线。
    B47 策略:
      - use_cache 且缓存存在 → 直接返回（永久有效）
      - force=True 强制重拉
      - 缓存缺失且未 dead code → API 拉取，成功即保存并清 failed，失败则 attempts+1
    """
    if use_cache and not force:
        cached = _load_daily_cache(code)
        if cached is not None:
            return cached
    if not force and _is_dead_code(code):
        return None
    for attempt in range(3):
        try:
            # B50-r3: ak.stock_hk_daily 内部 requests.get 无 timeout，
            # 新浪连续请求 ~250 只后会挂起连接（ESTABLISHED 但永不响应，实测卡 5 小时）。
            # 用 socket 默认超时兜底，超时后抛 socket.timeout → 走重试，不再无限挂起。
            _prev_sock_timeout = socket.getdefaulttimeout()
            socket.setdefaulttimeout(45)
            try:
                # stock_hk_daily(新浪源)已在 akshare 移除，改用东财 stock_hk_hist_em
                df = ak.stock_hk_hist_em(
                    symbol=code,
                    period="daily",
                    start_date="19700101",
                    end_date=pd.Timestamp.today().strftime("%Y%m%d"),
                    adjust="",
                )
            finally:
                socket.setdefaulttimeout(_prev_sock_timeout)
            if df is None or df.empty:
                _mark_failed(code, "empty result")
                return None
            # 东财源返回中文列名，统一 rename 为英文
            df = df.rename(columns={
                "日期": "date", "开盘": "open", "收盘": "close",
                "最高": "high", "最低": "low", "成交量": "volume", "成交额": "amount",
            })
            # B47.1: 检查数据结构。债券/衍生品/未上市新股 (40xxx/85xxx/89xxx 等)
            # 会返回不含 date 列的异常结构，直接标记死码不再重试
            if "date" not in df.columns:
                _mark_dead(code, f"unsupported symbol type (cols={list(df.columns)[:5]})")
                return None
            df["date"] = pd.to_datetime(df["date"])
            df = df[["date", "open", "close", "high", "low", "volume", "amount"]].reset_index(drop=True)
            _save_daily_cache(code, df)
            _clear_failed(code)
            time.sleep(AKSHARE_SLEEP)
            return df
        except KeyError as e:
            # KeyError('date') 属于符号类型不支持，直接死码
            if "'date'" in str(e) or str(e) == "'date'":
                _mark_dead(code, f"KeyError: {e}")
                return None
            if attempt == 2:
                logger.debug(f"{code} stock_hk_daily 失败: {e}")
                _mark_failed(code, str(e))
                return None
            time.sleep(1 + attempt)
        except Exception as e:
            if attempt == 2:
                logger.debug(f"{code} stock_hk_daily 失败: {e}")
                _mark_failed(code, str(e))
                return None
            time.sleep(1 + attempt)
    return None


def _pick_at_date(df: pd.DataFrame, as_of: _datetime, fallback_days: int = 5) -> Optional[dict]:
    """从日线里取 as_of 当天（或前 fallback_days 内最近交易日）的行情。"""
    if df is None or df.empty:
        return None
    as_of_ts = pd.Timestamp(as_of.date() if hasattr(as_of, "date") else as_of)
    lo = as_of_ts - pd.Timedelta(days=fallback_days)
    hit = df[(df["date"] >= lo) & (df["date"] <= as_of_ts)]
    if hit.empty:
        return None
    row = hit.sort_values("date").iloc[-1]
    return {
        "actual_date": row["date"].strftime("%Y-%m-%d"),
        "open": float(row["open"]) if pd.notna(row["open"]) else None,
        "close": float(row["close"]) if pd.notna(row["close"]) else None,
        "high": float(row["high"]) if pd.notna(row["high"]) else None,
        "low": float(row["low"]) if pd.notna(row["low"]) else None,
        "volume": float(row["volume"]) if pd.notna(row["volume"]) else None,
        "amount": float(row["amount"]) if pd.notna(row["amount"]) else None,
    }


def get_hk_prices_at_date(
    codes: list,
    as_of_date: _datetime,
    fallback_days: int = 5,
    max_workers: int = 8,
    use_cache: bool = True,
) -> dict:
    """
    B46+B47: 并发拉 codes 在 as_of_date 当天（若停牌，取前 fallback_days 内最近一天）的真实行情。

    B47 缓存策略:
      - 缓存永久有效（历史日线是不变量）；只重试 failed / 未缓存的 code
      - failed sidecar (data/cache/hk_daily_failed.json) 记录 attempts；attempts>=5 视为死码不再尝试

    Args:
        codes: 5位港股代码列表
        as_of_date: 基准日
        fallback_days: 停牌容忍窗口
        max_workers: 并发数（保留兼容，实际串行 API）
        use_cache: 使用磁盘缓存

    Returns:
        {code: {actual_date, open, close, high, low, volume, amount}}；
        无数据的 code 不出现在返回 dict 中
    """
    # NB: ak.stock_hk_daily 内部使用 mini_racer (V8) 执行 JS，多线程会导致
    #     "Check failed: !pool->IsInitialized()." fatal crash。这里强制串行。
    #     缓存命中的直接内存读取，仅缓存缺失走 API。
    codes = [str(c).zfill(5) for c in codes]
    results: dict = {}
    start_t = time.time()

    # 分离：缓存命中 / dead-code 跳过 / 需 API
    cached_dfs = {}
    to_fetch = []
    skipped_dead = []
    for c in codes:
        if use_cache:
            df = _load_daily_cache(c)
            if df is not None:
                cached_dfs[c] = df
                continue
        if _is_dead_code(c):
            skipped_dead.append(c)
            continue
        to_fetch.append(c)

    logger.info(
        f"拉取 {len(codes)} 只港股 as_of={as_of_date:%Y-%m-%d} 真实行情"
        f"（缓存命中 {len(cached_dfs)}，需 API {len(to_fetch)}, 跳过死码 {len(skipped_dead)}, fallback={fallback_days}d）"
    )

    # 缓存命中的 code 直接处理
    for code, df in cached_dfs.items():
        rec = _pick_at_date(df, as_of_date, fallback_days)
        if rec is not None:
            results[code] = rec

    # 串行 API 调用（V8 上下文冲突要求）
    for i, code in enumerate(to_fetch, 1):
        try:
            df = _fetch_single_daily(code, use_cache=False)  # _fetch_single_daily 内部会更新 failed/cache
            rec = _pick_at_date(df, as_of_date, fallback_days) if df is not None else None
            if rec is not None:
                results[code] = rec
        except Exception as e:
            logger.debug(f"{code} 失败: {e}")
        if i % 50 == 0:
            logger.info(f"  API 进度 [{i}/{len(to_fetch)}] 累计命中 {len(results)}, 耗时 {time.time()-start_t:.1f}s")

    # 持久化 failed sidecar
    _save_failed_daily()
    fd_size = len(_load_failed_daily())
    logger.info(
        f"完成：{len(results)}/{len(codes)} 命中 as_of={as_of_date:%Y-%m-%d}，"
        f"耗时 {time.time()-start_t:.1f}s；failed sidecar: {fd_size}"
    )
    return results
