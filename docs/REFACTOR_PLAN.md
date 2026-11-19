# 港股红利选股工具 · 重构方案

- 版本：v0.1
- 日期：2026-11-19
- 状态：待评审（未动任何代码）
- 范围：`/Users/kaka/work/hk-stock-ranker`

## 1. 目标与原则

**目标**：在不改变外部行为的前提下（排名结果、数据文件格式、CLI 参数、Web 接口均不变），
消除历史包袱、降低维护成本，为后续策略迭代铺路。

**原则**：

1. 增量式，不推倒重来。核心引擎（`engine/ranker.py`、`engine/dividend_attribution.py`、
   `engine/real_fcf.py`）已收口，逻辑不动，最多做薄封装。
2. 每步都有回归护栏：现有 18 个测试文件 + badcase 库（20 坏例）必须全绿；
   排名结果与当前版本 byte 级可对比。
3. 先拆"地雷"（P0），再做结构整理（P1），最后存储/工程化（P2/P3）。
4. 拆分只搬代码、不改逻辑；一次只动一个文件，每个独立 commit。

---

## 2. 现状盘点（关键依赖，拆分边界依据）

### 2.1 已知问题清单

| 级别 | 问题 | 位置 | 风险 |
|---|---|---|---|
| 🔴 P0 | 重复且承重的 import 块 | `main.py` L1–100 vs L102–146 | 删第一段会 NameError |
| 🟠 P1 | 编排逻辑堆在入口 | `main.py` 951 行 | 难改、难测 |
| 🟠 P1 | 6 个 domain 挤一文件 | `collectors/hk_financial.py` 1879 行 / 33 函数 | 难定位 |
| 🟠 P1 | 因子/诊断逻辑内聚 Web 层 | `web/app.py` 1255 行 | 业务逻辑与路由耦合 |
| 🟡 P2 | 39 个一次性脚本无索引 | `scripts/` | 无法判断哪些已退休 |
| 🟡 P2 | 硬编码相对路径 | `hk_price.py`、`web/app.py` | 依赖 CWD |
| 🟡 P2 | CSV 全量重写、无 schema 版本、无原子写 | `storage/store.py` | 随数据增长变慢 |
| 🟡 P2 | 备份文件堆积 | `data/csv/*.bak*`、`.broken_*`、`.20260522` | 占空间 |
| 🟢 P3 | 测试覆盖不均 | `collectors/`、`web/` | 重构缺护栏 |

### 2.2 `main.py` 重复 import 的精确结论

`main.py` 前 100 行是某次合并残留的旧 docstring + import + `logging.basicConfig`。
**危险点**：第二段 import（L124–135）漏掉了下面这些名字，它们靠第一段"死代码"的 import 才不报错：

- `save_monthly_failed`, `load_monthly_failed_codes`（来自 `collectors.hk_price`）
- `has_share_dilution`, `has_recent_dividend_or_forward`（来自 `collectors.hk_financial`）
- `load_latest_close_map`, `save_industry_data`, `load_industry_data`, `upsert_industry_data`（来自 `storage.store`）

修复方式：合并为**一份** import 并集，删掉重复 docstring 和第二次 `logging.basicConfig`。
`_run_stale_scan_and_refresh`（L49）是全文件唯一定义、后续被调用，保留。

### 2.3 模块依赖关系（归档/拆分依据）

| 模块 | 被谁依赖 | 结论 |
|---|---|---|
| `engine/real_fcf.py` | `web/app.py` L213、`tests/test_real_fcf.py` | 保留（非死代码） |
| `web/momentum.py` | `web/app.py` L710 | 保留 |
| `scripts/backfill_pools.py` | `main.py` L848、`tests/test_pool_snapshots.py` | **常驻**，不可归档 |
| `scripts/backfill_rankings.py` | `main.py` L884、`tests/test_backfill_rankings.py` | **常驻**，不可归档 |
| `scripts/refresh_dividend_only.py` | `tests/test_dividend_attribution.py` L257/L304 | **常驻**（待核查是否可退役，见 §4 P0-3） |
| `scripts/data_quality_check.py` | `tests/test_data_quality.py`、运维 | **常驻** |
| 其余 33 个 `scripts/*.py` | 仅运维一次性 | 可归档 |

### 2.4 FCF 双口径提示（重构时需留意，不要动逻辑）

- `engine/rank.py` 内联计算 B33 的 `real_fcf_ttm / fcf_to_equity / fcf_momentum`（成长因子）。
- `engine/real_fcf.py` 独立实现 3y 口径 `real_fcf` + 覆盖率 + 评级（Web 层可持续回报率用）。
两套口径并存，本次重构**只做物理搬移，不合并口径**，避免改变排名结果。

---

## 3. 目标架构（after）

```
hk-stock-ranker/
├── pyproject.toml                 # P3：替代 requirements.txt（含 ruff/mypy 配置）
├── config/
│   └── settings.py                # 统一所有路径/常量（消除硬编码）
├── collectors/
│   ├── hk_price.py
│   ├── hk_industry.py
│   ├── hk_dividend_history.py
│   ├── hk_forward_dividend.py
│   ├── incremental.py
│   └── financial/                 # P1：拆分 hk_financial.py
│       ├── __init__.py            #   导出 collect_financial_data / DOMAIN_FIELDS / ALL_DOMAINS
│       ├── _common.py             #   _hk_ticker / _get_ticker / DUAL_LISTED_EXEMPT / 报表读取
│       ├── _report.py             #   _get_hk_report_akshare / _ak_series / _yf_series / _combine_series
│       ├── dividends.py           #   get_fiscal_year_dates / get_fiscal_year_dividends /
│       │                          #   has_dividend_history / has_recent_dividend_or_forward
│       ├── buyback.py             #   get_fiscal_year_buyback / has_share_dilution /
│       │                          #   get_annual_dilution_rate / _bs_shares_history / splits
│       ├── debt.py                #   get_net_debt_ratio / get_debt_ratios（复用 _report）
│       ├── fcf.py                 #   get_fcf_components（复用 _report）
│       ├── growth.py              #   get_growth_metrics / _get_growth_from_akshare
│       └── collect.py             #   collect_financial_data（domain 编排入口）
├── engine/
│   ├── ranker.py                  # 不动
│   ├── dividend_attribution.py    # 不动
│   ├── real_fcf.py                # 不动
│   └── factors.py                 # P1：从 web/app.py 下沉的现金/覆盖/诊断因子
├── pipeline/                      # P1：从 main.py 抽出编排
│   ├── __init__.py
│   ├── rebuild_pool.py            #   run_rebuild_pool
│   ├── update.py                  #   run_update + 价格/行业/stale 子步骤
│   ├── rank.py                    #   run_rank_only
│   └── cli.py                     #   argparse（main.py 退化为薄壳，保持 CLI 参数不变）
├── storage/
│   ├── store.py                   # 保留（对外接口不变）
│   └── db.py                      # P2：SQLite/DuckDB 后端，sore.py 内部切换
├── backtest/                      # 不动
├── web/
│   ├── app.py                     # 只留路由，瘦身到 ~400 行
│   ├── momentum.py                # 不动
│   └── services.py                # P1：诊断/因子服务
├── scheduler/                     # 不动
├── scripts/
│   ├── README.md                  # P2：索引（常驻 vs 已归档）
│   └── archive/                   # P2：33 个退休脚本移入
├── tests/                         # 扩充 collectors/web 用例
└── docs/
    ├── REFACTOR_PLAN.md           # 本文档
    └── ...
```

---

## 4. 分阶段计划

### P0 · 消除地雷（约 0.5 天，低风险）

| 步骤 | 动作 | 验收 |
|---|---|---|
| P0-1 | `main.py`：合并双 import 为一份并集，删重复 docstring 与第二次 `logging.basicConfig` | `python -c "import main"` 通过；`python main.py --rank-only` 产出与改动前一致 |
| P0-2 | 补一个"import 完整性"冒烟测试：遍历 `main.py` 顶层引用的名字，断言全部可解析 | 防止再次漏 import |
| P0-3 | 核查 `scripts/refresh_dividend_only.py` 是否已被 `compute_dividend_fields` 完全替代；若 `recompute_dividends_for_code` 仅剩测试引用，改测试指向新入口并退役该脚本 | 不改变运行结果 |

> P0 完成后单独 commit：`refactor(main): 合并重复 import 块，消除承重死代码`。

### P1 · 拆分大文件（约 2–3 天，逐文件独立 commit）

#### P1-a：`collectors/hk_financial.py` → `collectors/financial/`

- 按 §3 拆成 9 个子模块。**关键耦合点**：`get_debt_ratios` 与 `get_fcf_components`
  共用报表读取（`_get_hk_report_akshare` 等），必须落到 `_report.py` 供两者复用。
- `__init__.py` 保持 `collect_financial_data / DOMAIN_FIELDS / ALL_DOMAINS` 三个导出不变，
  使 `incremental.py` 与 `main.py` 零改动。
- 回归：`tests/test_incremental_financial.py`、`test_dividend_attribution.py`、`test_known_badcases.py` 全绿。

#### P1-b：`main.py` 编排 → `pipeline/`

- 抽出 `run_rebuild_pool` / `run_update` / `run_rank_only` 及其内联子步骤
  （价格回填、current_price 采集、行业补采、stale 扫描）。
- `main.py` 退化为 argparse 薄壳，**CLI 参数与打印输出保持完全一致**。
- 回归：跑 `--rank-only` 对比输出；`--update` 用 `SKIP_PRICE=1 SKIP_FORWARD_DIV=1` 空跑验证。

#### P1-c：`web/app.py` 因子/诊断 → `engine/factors.py` + `web/services.py`

- `_attach_cashflow_factors`、`_diagnose_stock`、`_load_low_map` 等纯函数下沉；
  路由只留参数解析与模板渲染。
- 回归：`tests/test_web_div_yoy_filter.py`；启动 `--web` 对比 `/api/ranking` 响应。

> 每拆完一个文件跑一次全量 `pytest`，绿了再 commit。

### P2 · 存储升级 + 脚本归档（约 2–3 天）

| 步骤 | 动作 | 验收 |
|---|---|---|
| P2-1 | 路径统一：`hk_price.py` 的 `"data/cache/hk_daily"`、`web/app.py` 的 `_PRICE_FILE` 收编到 `config/settings.py` | 任意 CWD 下运行正常 |
| P2-2 | `storage/store.py` 内部切换到 SQLite/DuckDB（对外 `load_*`/`save_*` 签名不变），带 schema 版本号 + 事务原子写；提供 CSV↔DB 一次性迁移脚本 | 排名结果不变；`load_monthly_price` 不再每次读全量 |
| P2-3 | 33 个一次性脚本移入 `scripts/archive/`，`scripts/README.md` 建立索引（常驻 4 个 + 归档 33 个，各注用途与状态） | 常驻脚本可被 `main.py`/测试正常 import |
| P2-4 | 数据备份清理：`data/csv/*.bak*`、`.broken_*`、`.20260522` 移入 `data/csv/archive/`（**只移动，不删除**） | 磁盘占用下降，原数据可回溯 |

### P3 · 工程化（约 1 天）

- `requirements.txt` → `pyproject.toml` + 锁版本（`pip-tools` 或 `uv`）。
- 引入 `ruff`（格式化 + lint）与 `mypy`（渐进式，先 `--check-untyped-defs` 温和模式）。
- 补 `collectors/`（mock 数据源）与 `web/`（Flask test_client）测试。
- CI（可选）：`pytest` + `ruff` 的 GitHub Actions。

---

## 5. 回归策略

1. **金标准对比**：P0/P1 每次改动后，用同一份股票池/财务数据跑 `--rank-only`，
   生成的 `hk_ranking_*.csv` 与改动前逐字段 diff（`rank/code/return_rate/composite_score` 必须一致）。
2. **测试护栏**：现有 18 个测试文件 + badcase 库（20 坏例）全绿，作为每个 commit 的准入门槛。
3. **分层验证**：P1 拆分是纯搬移，理想情况下 diff 后排名零差异；若出现差异立即回滚定位。

## 6. 风险与回滚

| 风险 | 缓解 |
|---|---|
| 拆分搬移引入逻辑漂移 | 只搬不改；金标准 diff；逐文件 commit |
| `hk_financial.py` 拆分时 domain 间耦合（`_report` 共享） | 先抽 `_report.py`/`_common.py`，再按 domain 分 |
| 存储切换导致字段/类型漂移 | DB 层做列级类型校验，迁移脚本 + 双跑对比 |
| 归档脚本被隐式依赖 | 归档前用 `grep` 全仓扫描 import，确认无引用 |

回滚：每阶段独立 commit，出问题直接 `git revert` 对应 commit，数据文件不受影响。

## 7. 待确认决策项

1. **存储后端选型**：SQLite（零依赖、单文件）还是 DuckDB（列存、聚合更快、但多一个重依赖）？
   建议 SQLite 起步，够用且最稳。
2. **是否引入 `src/` 布局**：建议暂不引入（平铺即可），降低迁移风险，后续需要再上。
3. **`refresh_dividend_only.py` 退役**：确认 `compute_dividend_fields` 已完全接管后是否可删。
4. **执行节奏**：P0 立即做；P1 是否一次全做，还是先只拆 `hk_financial.py` 一个验证收益？
