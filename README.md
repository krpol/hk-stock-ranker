# 港股回报率排名工具

## 功能概述
- 采集港股月度收盘价（后复权，5年历史）
- 采集财务数据：分红（按财年）、预分红、回购金额、PE/PB、有息负债率
- 按回报率排序：`(财年分红 + 预分红 + 财年每股回购) / 当前收盘价`
- 输出：CSV + Excel + Web 页面

## 回报率公式说明

```
return_rate = (fy_div_per_share + real_buyback_per_share) / current_price × 100
              - annual_dilution_rate × 100

其中：
  fy_div_per_share         当前财年完整每股分红（HKD），= 已除净 + 已宣派未除净
                           （forward_div_per_share 已计入此项，不再单独相加）
  real_buyback_per_share   真实每股回购 = min(股本变化折算, 现金回购)，
                           剔除 |股本变化| > 30% 的异常情形
  annual_dilution_rate     年化稀释率（增发对回报的负向调整）
  current_price            当前收盘价（不复权）

注：forward_div_per_share 字段保留用于展示「已宣派未除净」部分，
    在 fy_div_per_share 中已包含，不可双计（详见 docs/DATA_DICTIONARY.md §1.2）。
```

## 字段速查
完整数据字典见 [docs/DATA_DICTIONARY.md](docs/DATA_DICTIONARY.md)。

## 安装

```bash
cd ~/hk-stock-ranker
pip install -r requirements.txt
```

## 使用

```bash
# 采集数据 + 计算排名（默认取市值前200只港股）
python main.py --update

# 自定义股票池大小
python main.py --update --pool-size 500

# 仅重新计算排名（无需重新采集）
python main.py --rank-only

# 启动 Web 服务（访问 http://localhost:5000）
python main.py --web

# 采集完后直接启动 Web
python main.py --update --web

# 启动月度定时调度（每月第5天 08:00 HKT 自动更新）
python main.py --schedule

# 年度股票池重建（默认带近 5 交易日 max(amount) 复核，避免冷门股因 rebuild 当天停牌被永久踢出）
python main.py --rebuild-pool

# 自定义流动性窗口大小（N 越大覆盖越宽；设 0 退回到旧的单日快照行为）
python main.py --rebuild-pool --liquidity-window 10
python main.py --rebuild-pool --liquidity-window 0
```

## 项目结构

```
hk-stock-ranker/
├── config/
│   └── settings.py          # 全局配置（市值门槛、调度时间、端口等）
├── collectors/
│   ├── hk_price.py          # 港股价格采集（AKShare）
│   └── hk_financial.py      # 财务数据（yfinance：分红/回购/负债率）
├── engine/
│   └── ranker.py            # 回报率计算 + 排序
├── storage/
│   └── store.py             # CSV / Excel 读写
├── web/
│   ├── app.py               # Flask Web 服务
│   └── templates/index.html # 排名展示页
├── scheduler/
│   └── tasks.py             # APScheduler 月度调度
├── data/
│   ├── csv/                 # 原始数据（价格、财务、排名）
│   └── excel/               # 排名 Excel 文件
├── main.py                  # 主入口
└── requirements.txt
```

## 数据来源

| 数据 | 来源 |
|------|------|
| 港股列表、月度价格 | AKShare（东方财富） |
| 分红历史（除息日） | yfinance（Yahoo Finance） |
| 预分红 | yfinance `fast_info` |
| 回购金额 | yfinance `cashflow`（RepurchaseOfCapitalStock） |
| PE/PB | AKShare 实时行情 |
| 有息负债率 | yfinance `balance_sheet` |

## 配置说明

编辑 `config/settings.py`：
- `HK_STOCK_FILTERS.min_market_cap`：最小市值门槛（港元），默认 20 亿
- `MONTHLY_UPDATE_DAY`：每月几号自动更新，默认第 5 天
- `WEB_PORT`：Web 服务端口，默认 5000
- `HISTORY_YEARS`：价格历史保留年数，默认 5 年

## 注意事项

1. **回购数据限制**：yfinance 的 `cashflow` 数据更新有延迟（通常为季度/年报后），且部分小市值港股覆盖不完整，`buyback_per_share` 可能为 0。
2. **预分红**：基于 `fast_info.last_dividend_value` 判断，若除息日在最新财报日之后则计入预分红，准确性依赖 Yahoo 数据更新时效。
3. **数据频率**：建议每月初运行一次更新，避免频繁请求导致 IP 被限速。
