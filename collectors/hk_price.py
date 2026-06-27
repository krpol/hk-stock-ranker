"""
港股价格与基本行情采集
来源：AKShare（东方财富）
"""
import time
import logging
from typing import Optional
import requests
import pandas as pd
import akshare as ak
from config.settings import AKSHARE_SLEEP

logger = logging.getLogger(__name__)

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
      2. AKShare stock_hk_spot（港交所数据，备用）
      3. curl 直接请求东方财富接口

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

    # ---- 方案2：AKShare stock_hk_spot（港交所）----
    if df.empty:
        logger.info("回退到 AKShare stock_hk_spot（港交所数据源）...")
        for attempt in range(1, max_retries + 1):
            try:
                df = ak.stock_hk_spot()
                rename_map = {
                    "代码": "code", "中文名称": "name", "最新价": "price",
                    "涨跌幅": "pct_change", "成交量": "volume", "成交额": "amount",
                }
                df = df.rename(columns={k: v for k, v in rename_map.items() if k in df.columns})
                df["code"] = df["code"].astype(str).str.zfill(5)
                logger.info(f"AKShare stock_hk_spot 成功，共 {len(df)} 只")
                break
            except Exception as e:
                wait = 3 * attempt
                logger.warning(f"stock_hk_spot 失败（第{attempt}/{max_retries}次）: {e}，{wait}秒后重试...")
                time.sleep(wait)

    # ---- 方案3：curl 直接请求东方财富 ----
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


def get_hk_monthly_price(
    code: str,
    start_date: str,
    end_date: str,
    adjust: str = "hfq",
    max_retries: int = 3,
) -> pd.DataFrame:
    """
    获取港股月度收盘价（复权），带重试
    
    Args:
        code: 港股代码（5位，如 "00700"）
        start_date: 开始日期 "YYYYMMDD"
        end_date: 结束日期 "YYYYMMDD"
        adjust: 复权方式 "hfq"=后复权 / "qfq"=前复权 / ""=不复权
    
    Returns:
        DataFrame with columns: date, open, close, high, low, volume, amount
    """
    for attempt in range(1, max_retries + 1):
        try:
            df = ak.stock_hk_hist(
                symbol=code,
                period="monthly",
                start_date=start_date,
                end_date=end_date,
                adjust=adjust,
            )
            df = df.rename(columns={
                "日期": "date",
                "开盘": "open",
                "收盘": "close",
                "最高": "high",
                "最低": "low",
                "成交量": "volume",
                "成交额": "amount",
                "涨跌幅": "pct_change",
                "涨跌额": "change",
            })
            df["date"] = pd.to_datetime(df["date"])
            df["code"] = code
            time.sleep(AKSHARE_SLEEP)
            return df[["date", "code", "open", "close", "high", "low", "volume", "amount"]]
        except Exception as e:
            err_str = str(e)
            if any(kw in err_str for kw in ("Connection aborted", "RemoteDisconnected",
                                             "Connection refused", "timed out",
                                             "curl: (6)", "curl: (7)", "curl: (28)")):
                wait = 5 + 3 * attempt
                logger.warning(f"[{code}] 月度价格网络异常，{wait}s后重试({attempt}/{max_retries}): {err_str[:60]}")
                time.sleep(wait)
            else:
                logger.warning(f"[{code}] 获取月度价格失败(非网络): {err_str[:80]}")
                return pd.DataFrame()
    
    logger.warning(f"[{code}] 月度价格达最大重试次数")
    return pd.DataFrame()


def get_hk_monthly_price_dual(
    code: str,
    start_date: str,
    end_date: str,
    max_retries: int = 3,
) -> pd.DataFrame:
    """
    获取港股月度价格的【原始价 + 前复权价】双价版本。

    返回列: date, code, open, close, high, low, volume, amount, close_qfq
        close      —— 原始（不复权）收盘价，用于 PE/PB/股息率/ranking 展示价
        close_qfq  —— 前复权收盘价，用于动量/价格变化率/回测
        OHL/V/amount 维持不复权口径

    实现：对 akshare 调两次，分别 adjust="" 与 adjust="qfq"，按 date 对齐 merge。
    任一侧空则返回空 DataFrame。
    """
    raw = get_hk_monthly_price(code, start_date, end_date, adjust="", max_retries=max_retries)
    if raw.empty:
        return pd.DataFrame()
    qfq = get_hk_monthly_price(code, start_date, end_date, adjust="qfq", max_retries=max_retries)
    if qfq.empty:
        logger.warning(f"[{code}] qfq 拉取为空，dual 返回空")
        return pd.DataFrame()
    # 仅取 qfq 的 close 列重命名 close_qfq，按 (date, code) join
    qfq_slim = qfq[["date", "code", "close"]].rename(columns={"close": "close_qfq"})
    merged = raw.merge(qfq_slim, on=["date", "code"], how="left")
    return merged[["date", "code", "open", "close", "high", "low", "volume", "amount", "close_qfq"]]


def get_hk_current_price(code: str) -> Optional[float]:
    """获取港股当前（最新）收盘价（不复权）"""
    try:
        df = ak.stock_hk_hist(
            symbol=code,
            period="daily",
            start_date="20200101",
            end_date=pd.Timestamp.today().strftime("%Y%m%d"),
            adjust="",
        )
        if df.empty:
            return None
        return float(df.iloc[-1]["收盘"])
    except Exception as e:
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
    """5 位港股代码 → yfinance ticker：去前导零 + .HK，例如 '01245' → '1245.HK'"""
    return f"{int(code)}.HK"


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
