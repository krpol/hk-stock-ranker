#!/bin/bash
# B51: yfinance 被 Yahoo 限流后，自动等待恢复并重跑财务采集 + 排名
# 用法: nohup bash scripts/retry_financial_update.sh > /tmp/upd0906/retry.log 2>&1 &
set -u
cd /Users/huangzhen/hk-stock-ranker

INTERVAL=1200  # 20 分钟检测一次

echo "[$(date '+%F %T')] 开始监控 yfinance 限流恢复状态，每 ${INTERVAL}s 检测一次"

while true; do
    if .venv/bin/python -c "
import yfinance as yf
try:
    d = yf.Ticker('0700.HK').dividends
    assert d is not None and len(d) > 0
    print('OK')
except Exception as e:
    raise SystemExit(1)
" >/dev/null 2>&1; then
        echo "[$(date '+%F %T')] yfinance 已恢复，开始重跑财务采集 + 排名"
        SKIP_PRICE=1 SKIP_HKEX_DIV=1 FIN_FUTURE_TIMEOUT=120 FIN_CHECKPOINT_EVERY=100 \
            .venv/bin/python main.py --update --force-refresh
        echo "[$(date '+%F %T')] 财务更新完成，退出监控"
        break
    else
        echo "[$(date '+%F %T')] yfinance 仍限流，${INTERVAL}s 后重试"
        sleep $INTERVAL
    fi
done
