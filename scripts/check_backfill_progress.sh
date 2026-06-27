#!/usr/bin/env bash
# 查看 backfill_pools 后台任务进度
LOG=${1:-/tmp/backfill_pools.log}
if [ ! -f "$LOG" ]; then
  echo "日志文件不存在: $LOG"
  exit 1
fi

echo "=== 最近 5 条进度 ==="
grep "进度" "$LOG" | tail -5

echo ""
echo "=== 已完成的基准日 ==="
grep "完成: 入选" "$LOG" || echo "  （尚无完成的基准日）"

echo ""
echo "=== 已生成的 snapshot 文件 ==="
ls -la data/csv/pools/ 2>/dev/null | grep -v "^d" | grep -v "^total" || echo "  （还未生成）"
