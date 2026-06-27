# 数据字典 — hk-stock-ranker

本文档说明项目内三大核心 CSV 数据集的字段语义、数据源与计算口径。
适用于：维护者、二次开发者、需要直接消费 CSV 的下游分析。

> 数据更新时间见每张表的 `updated_at` 列。  
> 港股代码统一为 **5 位 0 填充字符串**（例：`00700`）。

---

## 1. `data/csv/hk_financial.csv`

每只股票一行的「财务+派息」事实表。所有派息字段经过 `engine/dividend_attribution.py:compute_dividend_fields` 单一权威实现归属。

### 1.1 标识 / 元数据

| 字段 | 类型 | 含义 | 数据源 |
|---|---|---|---|
| `code` | str(5) | 港股代码（0 填充） | 主键 |
| `updated_at` | str | 该行最近更新时间 `YYYY-MM-DD HH:MM:SS` | 计算字段 |

### 1.2 派息字段（核心）

> 全部由 <kfile name="dividend_attribution.py" path="engine/dividend_attribution.py">engine/dividend_attribution.py</kfile> 输出；调用入口为 <ksymbol name="get_fiscal_year_dividends" filename="hk_financial.py" path="collectors/hk_financial.py" startline="497" type="function">get_fiscal_year_dividends</ksymbol>。

| 字段 | 类型 | 含义 | 数据源 |
|---|---|---|---|
| `fy_div_per_share` | float | 当前完整财年的 TTM 每股分红（HKD），含已除净 + 已宣派未除净 | AKShare 派息表 + HKEX 预告 |
| `prev_year_div_per_share` | float | 上一完整财年每股分红 | 同上 |
| `two_years_ago_div_per_share` | float | 再上一完整财年每股分红 | 同上 |
| `latest_period_div_per_share` | float | 最近一期财报（年报或中报）单期每股分红 | 同上 |
| `latest_period_yoy_div_per_share` | float | 最近一期同期上一年的每股分红 | 同上 |
| `forward_div_per_share` | float | 已宣派未除净的部分（含 HKEX override 增量），**不能与 fy_div 直接相加**（已包含在 fy_div 中） | 同上 |
| `latest_attributed_fy` | int | 当前完整财年的年份（如 2025） | 计算字段 |
| `fiscal_periods` | str | 当前财年涵盖的财报截止日，逗号分隔（如 `2025-12-31,2025-06-30`） | 计算字段 |
| `current_fy_end` | str | 当前财年的年报截止日（`YYYY-MM-DD`） | yfinance balance_sheet/income_stmt |
| `prev_fy_end` | str | 上一完整财年截止日 | 同上 |
| `latest_period_end` | str | 最近一期财报截止日（含中报） | 同上 |
| `latest_period_yoy_end` | str | 最近一期同期上一年的财报截止日 | 同上 |
| `has_dividend_history` | bool | 近 3 年是否有过派息记录（用于排名过滤） | yfinance.dividends |
| `forward_div_source` | str | forward_div 来源标记：`hkex` / `yfinance` / `none` | <kfile name="hk_forward_dividend.py" path="collectors/hk_forward_dividend.py">collectors/hk_forward_dividend.py</kfile> |

**重要陷阱**：
- `forward_div_per_share` **已计入** `fy_div_per_share`（B4 修复后语义）。回报率公式中两者只能取其一或差值。
- `ex_date == NaT` 的派息记录（董事会建议但未表决的预案）会被 R1 规则丢弃（B2 修复）。

### 1.3 回购 / 股本

| 字段 | 类型 | 含义 | 数据源 |
|---|---|---|---|
| `total_buyback` | float | 当前财年累计回购金额（HKD） | yfinance cashflow |
| `prev_total_buyback` | float | 上一财年累计回购金额 | 同上 |
| `buyback_per_share` | float | 当前财年每股回购 = `total_buyback / shares_outstanding` | 计算字段 |
| `prev_buyback_per_share` | float | 上一财年每股回购 | 计算字段 |
| `shares_outstanding` | float | 当前总股本 | yfinance balance_sheet |
| `prev_shares_outstanding` | float | 上一财年末总股本 | 同上 |
| `splits_ratio_in_window` | float | TTM 窗口内的拆股比（用于回购金额还原） | yfinance.splits |
| `splits_ratio_calculated` | bool | 是否已计算拆股调整 | 计算字段 |
| `annual_dilution_rate` | float | 年化稀释率：`(cur_shares - prev_shares) / prev_shares` 年化 | 计算字段 |

### 1.4 负债 / 估值

| 字段 | 类型 | 含义 | 数据源 |
|---|---|---|---|
| `net_debt_ratio` | float | 有息净负债率：`(有息负债 - 现金) / 总资产` | yfinance balance_sheet |
| `total_liability_ratio` | float | 总负债率：`总负债 / 总资产`（B3 新增双阈值） | 同上 |

> B3 双阈值过滤：仅保留 `net_debt_ratio <= 0.50` **且** `total_liability_ratio <= 0.80`；NaN 行不被过滤。

### 1.5 营运指标（TTM 同比）

| 字段 | 类型 | 含义 | 数据源 |
|---|---|---|---|
| `ttm_revenue` | float | 当前 TTM 营收（HKD） | yfinance income_stmt |
| `prev_ttm_revenue` | float | 上一财年 TTM 营收 | 同上 |
| `revenue_yoy` | float | 营收同比变化率 | 计算字段 |
| `ttm_operating_income` | float | 当前 TTM 营业利润 | 同上 |
| `prev_ttm_operating_income` | float | 上一财年 TTM 营业利润 | 同上 |
| `operating_income_yoy` | float | 营业利润同比变化率 | 计算字段 |
| `ttm_operating_cashflow` | float | 当前 TTM 经营现金流 | yfinance cashflow |
| `prev_ttm_operating_cashflow` | float | 上一财年 TTM 经营现金流 | 同上 |
| `operating_cashflow_yoy` | float | 经营现金流同比变化率 | 计算字段 |

### 1.6 行情

| 字段 | 类型 | 含义 | 数据源 |
|---|---|---|---|
| `current_price` | float | 当前价格（HKD），用于计算 return_rate | AKShare |

---

## 2. `data/csv/hk_industry.csv`

行业归属独立缓存表。`engine/ranker.py` 通过 left-join 把 industry 列拼到 ranking 输出。

| 字段 | 类型 | 含义 | 数据源 |
|---|---|---|---|
| `code` | str(5) | 港股代码 | 主键 |
| `industry` | str | 所属行业（中文，如「软件服务」「工业工程」） | AKShare `stock_hk_company_profile_em` |
| `updated_at` | str | 行业字段更新时间 | 计算字段 |

> 行业相对静态。增量采集策略：默认仅补 `industry` 缺失的代码；`scripts/refresh_industry_only.py --all` 强制全量刷新。

---

## 3. `data/csv/hk_ranking_YYYY-MM.csv`

按月生成的排名输出表。每月一份，行为 ranking 时刻的 TOP-N 候选。
所有字段由 <kfile name="ranker.py" path="engine/ranker.py">engine/ranker.py:rank_stocks</kfile> 计算输出，列顺序见 `OUTPUT_COLUMNS`。

### 3.1 排名核心

| 字段 | 类型 | 含义 |
|---|---|---|
| `rank` | int | 综合得分降序排名（1 = 最优） |
| `code` | str(5) | 港股代码 |
| `name` | str | 公司名称 |
| `industry` | str | 所属行业（来自 `hk_industry.csv` left-join，**仅 2026-05+ 输出包含此列**） |
| `current_price` | float | 排名时刻的当前价格（HKD） |
| `composite_score` | float | 综合得分 = `1.0 × return_zscore + 0.2 × growth_factor` |
| `return_zscore` | float | 回报率的 winsorized Z-score（截尾后标准化） |
| `growth_factor` | float | 增长因子（4 个增长 Z 加权平均，TTM 回报权重 2.0） |
| `return_rate` | float | 名义回报率（%）= `(fy_div_per_share + real_buyback_per_share) / current_price × 100 − annual_dilution_rate × 100`（forward 已计入 fy_div，不重复加） |

### 3.2 派息组件

> 直接 left-join 自 `hk_financial.csv` 的同名字段。语义见 §1.2。

| 字段 |
|---|
| `fy_div_per_share` |
| `prev_year_div_per_share` |
| `latest_period_div_per_share` |
| `latest_period_yoy_div_per_share` |
| `forward_div_per_share` |

### 3.3 TTM 回报体系

| 字段 | 含义 |
|---|---|
| `ttm_return_rate` | 名义 TTM 回报 = `fy_div + buyback_per_share`（HKD） |
| `prev_ttm_return_rate` | 上一财年同口径 |
| `ttm_return_yoy` | TTM 回报金额同比 |
| `real_ttm_return_rate` | 真实 TTM 回报 = `fy_div + real_buyback_per_share`（剔除虚假回购） |
| `real_buyback_per_share` | 真实每股回购：`min(股本变化折算, 现金回购)`，剔除股本异常变化（>30% 阈值） |

### 3.4 回购 / 股本

| 字段 | 含义 |
|---|---|
| `buyback_per_share` | 当前财年每股回购 |
| `prev_buyback_per_share` | 上一财年每股回购 |
| `net_share_change_rate` | 股本净变化率 = `(prev_shares - cur_shares) / prev_shares × 100` |
| `annual_dilution_rate` | 年化稀释率 |
| `shares_outstanding` | 当前总股本 |
| `prev_shares_outstanding` | 上一财年末总股本 |
| `total_buyback` | 当前财年累计回购金额 |

### 3.5 增长因子分量

| 字段 | 含义 |
|---|---|
| `revenue_yoy` | TTM 营收同比 |
| `operating_income_yoy` | TTM 营业利润同比 |
| `operating_cashflow_yoy` | TTM 经营现金流同比 |
| `growth_zscore_revenue` | 营收同比的 winsorized Z |
| `growth_zscore_operating_income` | 营业利润同比 Z |
| `growth_zscore_operating_cashflow` | 经营现金流同比 Z |
| `growth_zscore_ttm_return` | TTM 回报金额同比 Z |

> `growth_factor` = 上述 4 个 Z 的加权平均（TTM 回报权重 2，其余 1）；NaN 自动跳过。

### 3.6 财报期 / 风险 / 市值

| 字段 | 含义 |
|---|---|
| `current_fy_end` | 当前财年截止日 |
| `prev_fy_end` | 上一财年截止日 |
| `latest_period_end` | 最近一期财报截止日 |
| `latest_period_yoy_end` | 最近一期同期上一年的财报截止日 |
| `net_debt_ratio` | 有息净负债率（B3 过滤阈值 ≤ 0.50） |
| `total_liability_ratio` | 总负债率（B3 过滤阈值 ≤ 0.80，仅 2026-05+ 输出包含此列） |
| `market_cap` | 市值（HKD） |

---

## 4. 其他辅助 CSV

| 文件 | 主要字段 | 说明 |
|---|---|---|
| `hk_stock_list.csv` | `code, name, market_cap, pe_ttm, pb` | AKShare 拉取的港股全集合（采集源） |
| `hk_stock_pool.csv` | `code, name, market_cap` 等 | 当前生效的股票池 |
| `hk_stock_pool_<YYYY-12-31>.csv` (在 `pools/` 子目录) | 同上 | 年度池快照（A 阶段产出，回测使用，消除幸存者偏差） |
| `hk_monthly_price.csv` | `code, date, close` | 月末后复权收盘价（5y） |
| `hk_benchmark_hsi.csv` | `period, return` | 恒生指数月度收益（基准） |
| `hk_forward_dividend.csv` | `code, forward_div` 等 | HKEX 预告分红爬虫产出 |

---

## 5. 字段历史变更（Bug 修复）

| 时间 | 字段/规则 | 变更 |
|---|---|---|
| B1 | `latest_attributed_fy` 归属 | 改用 AKShare「财政年度」精确归属，回退 8 月窗口启发式 |
| B2 | `ex_date == NaT` 处理 | 跳过未表决预案（02355 宝业 5.5 港元虚高） |
| B3 | `total_liability_ratio` | 新增；与 `net_debt_ratio` 双阈值过滤 |
| B4 | `forward_div_per_share` | 修复 HKEX override 与 AKShare 已收录派息双计 bug |
| C1 | 派息归属逻辑 | 抽出 `engine/dividend_attribution.py` 单一权威实现，两条调用路径成为薄包装器 |
| B5 | `latest_period_end` / `fiscal_periods` | 修复闰年漂移 bug：fy_anchor=(8,31) 时中报期 (2,28) 被错误过滤。改为「月份匹配 + 月末容差 (⋥ 28)」。影响 9 只股票的展示字段，不影响 composite_score 排名 |
| B6 | 验证集 | 新增 `tests/badcases/known_badcases.yaml` + `tests/test_known_badcases.py`。沉淀 B2/B3/B4/B5 的真实股票 case，未来重构造成老 bug 复现会被 pytest 捕获 |
| B7 | `prev_year_div_per_share`（02602 万物云） | AKShare 2024 中期方案「相当于港币0.622」末尾缺「元」字，`parse_dividend_amount_hkd` 正则强制要末尾「元」导致该笔 0.622 HKD 丢失，prev_year_div 少算（1.457 而非 2.079）。修复：4 条 pattern 末尾「元」改为「元?」可选 |
| B9 | 流动性过滤采样偏差（01245 NIRAKU） | `--rebuild-pool` 只看当日快照 amount，冷门票遇上停牌日全年被踢出。修复：单日不过的票用 yfinance 拉近 N=5 交易日 max(close*volume) 复核，任一天达门槛即入池。新增配置 `LIQUIDITY_WINDOW_DAYS=5` 与 CLI `--liquidity-window N`；Step 2 产出复核明细 CSV `data/csv/liquidity_window_YYYYMMDD.csv` |

详细方案见 `plan.md` §10。

---

## 6. 维护提示

- 修改任何派息口径，**只改** <kfile name="dividend_attribution.py" path="engine/dividend_attribution.py">engine/dividend_attribution.py</kfile> 一处即可
- 增量补采：
  - 仅刷新分红：`python scripts/refresh_dividend_only.py`
  - 仅刷新行业：`python scripts/refresh_industry_only.py`
  - 仅刷新负债：`python scripts/refresh_liability_only.py`
- 端到端校验：`pytest tests/`（141 测试）
  - 单元测试 D1-D13（mock）、5 套集成测试（S1-S6）
  - `tests/test_dividend_parse.py`：`parse_dividend_amount_hkd` 17 个单元测试（含 B7 回归）
  - `tests/test_web_div_yoy_filter.py`：Web 过滤层 12 个集成测试（frg/fdg/frb + B7/B8 联合验证）
  - `tests/test_liquidity_window.py`：B9 流动性窗口复核 11 个单元测试（mock yfinance）
  - **真实股票 badcase 验证集**：`tests/test_known_badcases.py`（读取 `data/csv/hk_financial.csv` 对照 `tests/badcases/known_badcases.yaml`）。支持 `require_present: true` 字段标记「股票必须在 universe 中」（如 B9-01245 验证流动性补回成功）
- **发现新 bug 后的流程**：修复 → 在 `known_badcases.yaml` 追加一条 case（含 issue/code/desc/expect）→ 跑 `pytest tests/test_known_badcases.py` 验证通过，避免未来重构复现老 bug

---

## 7. Web UI 交互项

| 参数名 | UI 标签 | 语义 | 备注 |
|---|---|---|---|
| `fdg=1` | 分红同比不下降 | `fy_div_per_share >= prev_year_div_per_share` 且 `latest_period_div >= latest_period_yoy_div` | 展示层过滤，不重算 zscore，排名跳号 |
| `frg=1` | 回报同比不下降 | `ttm_return_rate >= prev_ttm_return_rate`（即 TTM 总回报含分红+回购） | 同上；NaN 视为 0 |
| `frb=1` | 真实回购 | `net_share_change_rate >= 0`（股本净变化不负，排除被 SBC 稀释的伪回购） | 同上 |

所有 checkbox 默认 unchecked，不破坏现有 URL/行为。
过滤时机：展示层（加载 ranking csv 后立即 filter），不影响 composite_score 排名顺序。

---

## 8. Badcase 维护强制流程

```
用户/自测发现某只股票字段异常
    ↓
1. 定位根因（collector / engine / 正则 / 字段归属逻辑）
    ↓
2. 修复代码
    ↓
3. 在 tests/badcases/known_badcases.yaml 末尾追加一条 case：
   - issue: BN（自增编号）
   - code: "XXXXX"（5 位字符串）
   - name: 股票名称
   - desc: bug 来源 + 修复原理（一段话）
   - expect: { field_name: { approx/range/exact: value } }
    ↓
4. pytest tests/test_known_badcases.py → 新 case PASS
    ↓
5. pytest（全量）→ 118+ 全 PASS
    ↓
6. 提交：badcase yaml + 修复代码放同一 commit
```

**禁止行为**：修复 bug 但不追加 badcase。Badcase 是 bug 存在的证明，也是未来重构的护盾。
