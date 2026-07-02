# 每月开始流程（Monthly Routine）

> 标准化「每月初刷新港股排名系统」的操作手册。每月 1-3 号执行一次。

## 0. 触发时机

- **执行日**：每月 1-3 号（建议 2 号或 3 号，等 AKShare 月末价格数据稳定上线）
- **判断 AKShare 是否已收录上月末数据**：
  ```bash
  .venv/bin/python -c "
  import pandas as pd
  df = pd.read_csv('data/csv/hk_monthly_price.csv', dtype={'code': str})
  df['date'] = pd.to_datetime(df['date'])
  latest = sorted(df['date'].unique())[-3:]
  print('最新 3 个日期:', [str(d.date()) for d in latest])
  "
  ```
  - 若最新日期仍为上月末（如 6/30），说明已收录；若还停留在更早，等次日再跑。

## 1. 步骤顺序

### Step 1：拉月末价格

```bash
.venv/bin/python main.py --backfill-prices --months 2
```

- 拉最近 2 个月的日度价格 → 按月截取月末收盘 → 写入 `data/csv/hk_monthly_price.csv`
- 用 AKShare `stock_hk_hist`（前复权 + 原始价双价）
- **耗时**：~15-25 分钟（902 只，并发 4 worker）
- **常见失败**：AKShare 限流（`RemoteDisconnected`），失败率 30-60%

### Step 2：补刷失败 code

```bash
.venv/bin/python scripts/backfill_july_missing.py  # 脚本名按当月改
```

> **注**：脚本名 `backfill_july_missing.py` 是 B37 临时脚本，针对 7 月。长期应改为通用 `backfill_missing_month.py`（接受 `--period YYYY-MM` 参数）。当前实现会自动识别当前 CSV 中缺失当月的 code 并补刷。

- 自动识别缺失当月价格的 code，只针对缺失的重拉
- **耗时**：~15-30 分钟（视失败数量）
- **可重复跑**：每跑一次能补回 30-50% 失败 code（AKShare 间歇性限流）
- **建议跑 2-3 轮**，覆盖率到 90%+ 即可停止

**覆盖率检查**：
```bash
.venv/bin/python -c "
import pandas as pd, sys
sys.path.insert(0, '.')
from storage.store import load_stock_pool, load_monthly_price
mp = load_monthly_price()
mp['code'] = mp['code'].astype(str).str.zfill(5)
mp['date'] = pd.to_datetime(mp['date'])
mp['period'] = mp['date'].dt.strftime('%Y-%m')
pool = load_stock_pool()
pool['code'] = pool['code'].astype(str).str.zfill(5)
target = 'YYYY-MM'  # 改为目标月
july = set(mp[mp['period']==target]['code'])
all_codes = set(pool['code'])
print(f'{target} 覆盖: {len(july)}/{len(all_codes)} = {len(july)/len(all_codes)*100:.1f}%')
"
```

### Step 3：刷新财务数据 + 预分红 + 重新排名

```bash
.venv/bin/python main.py --update
```

**完整链路**（`main.py` `run_update` 函数）：

| Step | 内容 | 判断逻辑 |
|---|---|---|
| 2 | 月度价格增量 | 总是跑（已在 Step 1 做过，此处跳过即可，或重复无妨） |
| 2b | 5 年历史回填 | 池内月数 < `HISTORY_YEARS*12*0.8` 触发 |
| 3a | 预分红爬虫 | 走港交所披露易，180 天回看 |
| 3b | 财务数据增量 | TTL 30 天 OR 财年到期 120 天 |
| 5 | 重新排名 | 用最新财务+价格重算 |

**关键参数**（`config/settings.py`）：
- `FINANCIAL_REFRESH_TTL_DAYS = 30`：财务数据 30 天过期
- `FORCE_REFRESH_AFTER_FY_DAYS = 120`：财年结束 120 天后强制刷新
- `FY_DUE_MIN_AGE_DAYS = 7`：避免 7 天内重复刷同一只

**耗时**：~1-1.5 小时（902 只财务采集）

### Step 4：冒烟 web

```bash
# 启动 web（如未运行）
.venv/bin/python web/app.py &

# 冒烟目标月
curl -s "http://localhost:5001/?period=YYYY-MM&per_page=3" | grep -E "现金堡垒因子|增长因子"
```

**验证点**：
- 页面返回 HTTP 200
- 表头前 12 列含 `回报率(%) → 可持续回报率(%) → 增长因子 → 现金堡垒因子`（B37 列顺序）
- TOP 5 股票与上月对比，合理性检查

### Step 5：commit + push

```bash
git add data/csv/hk_monthly_price.csv \
        data/csv/hk_ranking_YYYY-MM.csv \
        data/excel/hk_ranking_YYYY-MM.xlsx \
        data/csv/hk_financial_data.csv
git commit -m "B37: YYYY-MM 月度刷新（价格+财务+预分红+排名）"
git push
```

## 2. 常见问题

### AKShare 限流

**症状**：`RemoteDisconnected('Remote end closed connection without response')`，失败率 30-60%。

**原因**：AKShare 东财接口对并发 + 高频请求敏感。

**处理**：
1. 串行单只测试可成功 → 确认是并发限流
2. 降低并发：`backfill_july_missing.py` 中 `max_workers=4` 改为 `2`
3. 多轮重试（每轮补回一部分）
4. yfinance fallback：当前 `get_hk_monthly_price_dual` 未集成 yfinance，未来可加（仅用 close 价就够排名）

### 财年到期判断

**何时触发强制刷新**：
- 财年结束 120 天后 + 最近一次刷新 > 7 天前

**港股财年常见日**：
- 12/31（大部分 H 股）：4 月底年报出炉 → 5 月刷新
- 3/31（部分金融股）：7 月底年报出炉 → 8 月刷新
- 6/30（部分零售）：10 月底年报出炉 → 11 月刷新
- 9/30（部分地产）：1 月底年报出炉 → 2 月刷新

每月初跑 `--update` 会自动识别到期股票刷新，无需手动判断。

### 预分红爬虫

**数据源**：港交所披露易（`scrape_forward_div_em_only.py`），非 AKShare。

**回看窗口**：180 天，覆盖半年内的所有预分红公告。

**与 `--update` 关系**：`--update` Step 3a 自动跑，也可单独 `python main.py --scrape-forward-div`。

## 3. 检查清单

执行前确认：
- [ ] AKShare 已收录上月末价格（Step 0）
- [ ] web 服务可正常访问（`curl http://localhost:5001/api/periods`）
- [ ] 磁盘空间足够（`data/csv/` ~50MB/月增量）

执行后确认：
- [ ] 价格覆盖率 ≥ 90%
- [ ] 排名 CSV 行数与上月接近（±10%）
- [ ] TOP 5 与上月对比合理（无完全洗牌）
- [ ] commit 已推到远端

## 4. 历史记录

| 月份 | 执行日 | 价格覆盖 | 排名行数 | 备注 |
|---|---|---:|---:|---|
| 2026-07 | 2026-07-02 | 89.9% | 567 | AKShare 限流严重，3 轮补刷 |
