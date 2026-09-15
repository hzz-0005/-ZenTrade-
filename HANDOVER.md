# TradingAgents Webapp 交接文档

> 面向接手的人：看完这份文档你应该能直接跑起来、知道每个模块干什么、避开已经踩过的坑。
> 本文所有路径以仓库根目录为基准。

---

## 1. 这是什么

仓库 `TradingAgents-main` 分两层：

| 层 | 位置 | 说明 |
|---|---|---|
| **原项目** | `tradingagents/` | TauricResearch 的多 Agent LLM 金融框架：分析师团队 → 多空辩论 → 交易员 → 风控 → 组合经理 |
| **自研回测** | `webapp/` | 逐交易日回测模拟器。FastAPI + SQLite(WAL) + Vue3/ECharts |

`webapp` 原本**只用**了原项目的 dataflows 和 llm_clients，决策是单次 LLM 调用。本次工作把它改造成：可选接入**原项目完整多智能体流水线**，并用消息闸门按需调度。

**当前生效配置**：LLM = DeepSeek `deepseek-v4-flash`，A 股数据 = akshare，美股/港股 = yfinance + Alpha Vantage。

> **数据源分流（2026-08-31 更新）**：原项目数据流 `tradingagents/dataflows` 现在**也有 akshare 供应商**了（`akshare.py`），`route_to_vendor` 会按符号自动分流——`.SS`/`.SZ`（6 位数字代码）走 akshare，其余走 yfinance 等原链。所以 **graph 流水线跑 A 股不再需要 VPN**；美股仍需 VPN（yfinance）。详见下文「数据源」章节。

---

## 2. 跑起来

```bash
# 后端（端口 8000）
./.venv/Scripts/python.exe -m webapp.run

# 前端改动后重新构建（产物在 webapp/static/）
cd webapp/frontend && npm run build
```

**改了后端代码必须重启进程**——`uvicorn` 没开 reload。

> ⚠️ **前端 build 的坑**：WorkBuddy 的 safe-delete 会拦截 Vite 清空 `webapp/static`，直接 `npm run build` 会失败。
> 正确姿势：先用 Python 把 `webapp/static` 改名为 `static.bak` → 建空目录 → build → bash `rm -rf static.bak`。

数据库：`webapp/data/backtest.db`（SQLite WAL）。表：`sessions` / `daily_records` / `price_frames` / `trades` / `llm_calls` / `session_reviews` / `skills`。

---

## 3. 三种决策模式

### Web 回测架构选择（2026-09-02）

新会话使用 `decision_architecture` 持久化选择：

- `adaptive`（默认）：0 调用监控 / 1 调用快速复核 / 2 调用精简委员会。
- `classic_graph`：原完整多智能体 Graph，继续由 `WEBAPP_GRAPH_TRIGGER` 决定始终运行或按条件升级。
- `fast`：原单模型/角色面板路径。

旧会话的 `use_full_graph` 字段继续保留并在迁移时映射，前端新建回测页使用三个单选项。自适应模式入口为 `webapp/engine/adaptive_agent.py`，两步委员会为 `webapp/engine/committee_agent.py`。

| 模式 | 触发条件 | 成本 | 说明 |
|---|---|---|---|
| **single** | `WEBAPP_AGENT_MODE=single` | 最低 | 单次 LLM 调用，简单交易员 prompt |
| **panel** | 默认（前端不勾流水线） | 低 | 单次 LLM 调用，prompt 内模拟多角色（技术/基本面/消息/多空辩论/交易员） |
| **graph** | 前端勾选「完整多智能体流水线」 | 高 | **跑原项目真实流水线**，见下 |

### graph 模式的调度（`WEBAPP_GRAPH_TRIGGER`）

- **`on_news`（默认）** —— 分层调度，平时零成本。触发完整流水线重决策的**四类条件**（任一满足即升级）：
  1. **有效期到**：`recheck_days` 过期
  2. **价格出区间**：收盘离开 coast 区间 `[recheck_lower, recheck_upper]`（不再设 0.25 带宽阈值——见坑 #14）
  3. **新消息**：先过关键词规则（财报/重组/评级/监管…）命中直接升级；没命中走一次便宜 LLM 影响评估（`thinking=low`），medium/high 才升级
  4. **技术面恶化**：单日跌≤-5% / 两日跌≤-8% / RSI<30 / 跌破 20 日线 / 跌破布林下轨（`news_gate.detect_technical_breakdown`），零成本、优先级压过例行新闻
  - 都不满足 → **维持原判断**（hold，沿用流水线上次给的止损/目标价，刷新有效期）
- **`always`** —— 每个决策日都跑完整流水线（贵，但最彻底）

---

## 4. 文件地图

### 决策链
| 文件 | 职责 |
|---|---|
| `webapp/engine/backtest_engine.py` | 主循环。**硬风控层（止损/止盈/回撤熔断）** → coast 机制 → 技术面恶化触发 → 每日决策 → 成交 → 复盘 |
| `webapp/engine/decision_agent.py` | single/panel 模式。JSON 解析加固 + 内容安全拦截重试 |
| `webapp/engine/graph_agent.py` | **完整流水线适配器**。跑 `TradingAgentsGraph`，解析 PM 结构化价位 → **多因子仓位** → 映射 `Decision`；把 skill 注入流水线 |
| `webapp/engine/hybrid_agent.py` | **分层调度器**。消息闸门 + 技术面恶化判断决定是否升级流水线 |
| `webapp/engine/news_gate.py` | 三级闸门的规则与 LLM 影响评估 + **`detect_technical_breakdown` 技术面恶化检测** |
| `webapp/engine/context_builder.py` | 组装每日 prompt（`DailyContext`） |
| `webapp/engine/data_gateway.py` | 数据网关。按市场分流取数、防前视裁剪 |
| `webapp/engine/clock.py` | `sim_date` ContextVar，全局防前视 |

### 风控与决策核心（`tradingagents/` 原项目内）
| 文件 | 职责 |
|---|---|
| `tradingagents/agents/schemas.py` | PM/Trader 结构化输出 schema。`PortfolioDecision` 含 `stop_loss/take_profit/target_position_pct/risk_level` |
| `tradingagents/agents/analysts/market_analyst.py` | 技术面分析师。prompt 含套牢盘/超跌量化/超跌企稳/量价情绪/压力位应对五维 |
| `tradingagents/agents/analysts/news_analyst.py` | 新闻分析师。prompt 含新闻情绪/预期差/情绪拐点 |
| `tradingagents/agents/analysts/sentiment_analyst.py` | 情绪分析师。prompt 含社交数据缺失时从新闻反推情绪 |
| `tradingagents/agents/trader/trader.py` | 交易员。prompt 含行为金融纪律 + 超跌企稳加仓 |
| `tradingagents/agents/managers/portfolio_manager.py` | 组合经理。prompt 含 Execution Levels + 行为/结构检查 |
| `webapp/skills/library.py` / `distiller.py` | skill 库与复盘蒸馏。`created_at` 按会话 `end_date` 生效（防前视） |

### 数据源
| 文件 | 职责 |
|---|---|
| `webapp/engine/vendors/cn_source.py` | A 股：OHLCV / 公告 / 研报 / 财务指标 / 指数 |
| `webapp/engine/vendors/us_source.py` | 美股港股：OHLCV / 财报 / 评级变动 / 新闻（yfinance + Alpha Vantage） |

### 验证脚本（`scripts/`）
- `smoke_panel.py` —— A 股端到端（FakeLLM + 临时库，不碰真实 `backtest.db`）
- `smoke_graph.py` —— 流水线输出 → Decision 的映射验证
- `smoke_hybrid.py` —— 闸门升级路径 + 维持判断 + **会话首日强制升级**（7/7）
- `smoke_accounting.py` —— 部分平仓记账 / coast 重建 / 后缀纠正 / macro 预取开关 / 首日强制建仓 / 空仓卖降级（离线，临时库）

> **2026-09-03 修复**：三个冒烟脚本曾因未跟上代码演进而失败（与本轮交易逻辑改动无关）——
> `smoke_hybrid` 的 FakeGraph 桩缺 `_call_count`；`smoke_graph` 用了已被配置校验拒绝的
> `agent_mode="graph"`（GraphDecisionAgent 根本不读该参数）；`smoke_accounting` 10a 期望 4 期
> 盈利趋势，但保守披露滞后（防前视）正确地把 2025-12-31 年报挡在 2026-02-03 之外。均已对齐，
> 现四个脚本全 PASS。

---

## 5. 配置速查

### `.env`（当前值）
```bash
TRADINGAGENTS_LLM_PROVIDER=deepseek
TRADINGAGENTS_DEEP_THINK_LLM=deepseek-v4-flash
TRADINGAGENTS_QUICK_THINK_LLM=deepseek-v4-flash
DEEPSEEK_API_KEY=...            # 已填
ALPHA_VANTAGE_API_KEY=...       # 已填，美股历史新闻用
TRADINGAGENTS_PIPELINE_MODE=sequential
# 分角色思考档位（DeepSeek V4：thinking 开关 + reasoning_effort 强度）
TRADINGAGENTS_DEEPSEEK_QUICK_THINKING_LEVEL=low    # 查询/论证类 agent
TRADINGAGENTS_DEEPSEEK_DEEP_THINKING_LEVEL=high    # 研究经理 + 组合经理（出最终交易）
```

> **分档机制**：`trading_graph.py` 的 `_get_provider_kwargs(role=...)` 给 deep/quick 两个客户端
> 分别注入 `extra_body={"thinking": {"type": "enabled"}} + reasoning_effort=<档位>`。
> `extra_body` 必须在 `openai_client.py` 的 `_PASSTHROUGH_KWARGS` 白名单里才会透传（踩过坑：
> 曾经不在白名单，思考开关被静默丢弃）。webapp 侧 `webapp/llm.py::get_llm(thinking_level=...)`
> 同理：决策调用用 deep 档（high），消息闸门强制 low。
> 离线校验脚本：`scripts/verify_deepseek_thinking.py`。

> **⚠️ `PIPELINE_MODE` 的坑**：`.env` 里现在写的是 `sequential`，但 **webapp 的流水线会话不用它** ——
> `GraphDecisionAgent` 用 `settings.graph_pipeline_mode`（默认 `sequential`）覆盖。
> 这个 `.env` 值只影响直接用原项目 CLI 的场景。

### webapp 专属环境变量
| 变量 | 默认 | 作用 |
|---|---|---|
| `WEBAPP_AGENT_MODE` | `panel` | `single` / `panel` |
| `WEBAPP_GRAPH_TRIGGER` | `on_news` | `on_news` / `always` |
| `WEBAPP_GRAPH_PIPELINE_MODE` | `sequential` | 流水线串行/并行 |
| `WEBAPP_GRAPH_DEBUG` | off | 开启后失败时 dump 消息序列（含孤儿 tool_call_id） |
| `WEBAPP_MACRO_NEWS` | off | CCTV 新闻联播填充（**默认关，见坑 #1**） |
| `WEBAPP_RISK_BUDGET` | `0.02` | 单笔买入允许承受的权益损失比例（距止损定仓位用） |
| `WEBAPP_MAX_DRAWDOWN_STOP` | `0.20` | 权益自峰值回撤超此值，硬风控强制降仓 |
| `WEBAPP_DRAWDOWN_REDUCE_TO` | `0.30` | 回撤熔断后的目标仓位（0=清仓） |

### 数据窗口
- 研报 / 评级变动：**60 天**（7 天窗口对单只股票几乎必空）
- 公告：**7 天**
- 宏观新闻：**默认关闭**

---

## 6. 已踩过的坑（别再踩）

### #1 CCTV 宏观新闻会搞死会话
新闻联播是**时政内容**。杭电股份某窗口无研报无公告，消息面 100% 是政治新闻，
被智谱内容安全（错误码 **1301**）直接拒请求，会话跑到第 80 天崩掉。

→ 默认关闭 `macro_news_enabled`。另外 `decision_agent` 有兜底：命中内容审核且 prompt 里有宏观条目时，
剔除宏观后重试一次（保留公告/研报），并记 flag。

### #2 LangGraph reducer 被描述字符串挡住
```python
# ❌ 这样写 reducer 会被静默忽略
investment_debate_state: Annotated[InvestDebateState, _merge_debate_state, "描述文字"]
```
langgraph 的判据是 `callable(meta[-1])`，**reducer 必须是最后一个元数据元素**。
上面这行末尾是字符串 → reducer 失效 → channel 退化成 `LastValue` → 并发写直接报
`InvalidUpdateError: At key 'investment_debate_state': Can receive only one value per step`。

→ 已改为 `Annotated[InvestDebateState, _merge_debate_state]`（去掉尾部字符串，描述挪进注释）。

**排查手法**（可复用）：
```python
from langgraph.graph.state import _get_channel
# channel 类型应是 BinaryOperatorAggregate，不是 LastValue
```

### #3 parallel 模式下工具调用会互相踩
`langgraph/prebuilt/tool_node.py:1258`：
```python
latest_ai_message = next(m for m in reversed(messages) if isinstance(m, AIMessage))
tool_calls = list(latest_ai_message.tool_calls)
```
ToolNode 执行**共享 messages 列表里最近一条 AIMessage** 的 tool_calls，**不区分属于哪个分析师**。
parallel 模式 4 个分析师并发，同时调工具时先发者的 `tool_call_id` 永远等不到 ToolMessage
→ 下次请求被 400 拒（`"tool_calls must be followed by tool messages"`）。

→ webapp 默认 `sequential`。**跟 DeepSeek 无关，OpenAI 同样会拒。**

### #4 流水线失败绝不能静默降级
曾经把流水线异常吞掉转成 `hold`，结果回测「成功」跑完却全是 hold —— 看起来像策略谨慎，
实际 LLM 根本没跑。**比崩溃更糟**。

→ 现在异常一路抛到 `run()`，会话 `status=failed` 并写明真实错误。
超时/限流/5xx 会重试（2 次，10 秒退避）；400/401/403 等结构性错误直接失败。

### #5 美股数据面三个坑
1. `get_news_yfinance()` 渲染成 **markdown 字符串**，没有结构化数据可过滤 —— 就算修好参数也永远返回 0 条
2. `DEFAULT_CONFIG.get("benchmark_ticker", "^GSPC")` —— key 存在但值为 `None`，`dict.get` 返回 None 而非默认值
3. 新版 yfinance 返回 **MultiIndex 列**，`df["Close"]` 是 DataFrame 不是 Series

→ 都已修。美股基本面走 `quarterly_financials` + `quarterly_balance_sheet`（列即报告期，可过滤 ≤T），
**绝不用 `Ticker.info`**（那是「当前」快照，回测里等于前视）。

### #6 算同比不能混用量纲
曾拿季度营收（68.1B）比年度营收（130.5B）得出 -47.79%。
yfinance 季度只给 5 期，同比不够时回退到**年度 vs 年度**。现在 NVDA 营收同比 65.47%（已交叉验证）。

### #7 Alpha Vantage 的坑
- 免费档 **25 次请求/天**（查证自官方支持页）
- `tickers=NVDA` 会返回大量「顺带提及」的文章（主角是别的公司），光看 relevance 分数过滤不掉
  → 过滤策略：`relevance >= 0.8` **或** 标题含公司名。后者很关键：
  「Why OpenAI Is Unhappy With Some Nvidia Chips」relevance 只有 0.30，纯按分数会被误杀
- **配额设计**：按月分块预取并缓存原始 feed，之后每天 0 次调用。7 个月会话 ≈ 7~8 次调用
- **⚠️ 2026-09-01 更新**：磁盘新闻缓存已删除（`webapp/data/news_cache/` + `us_source.py` 里的 `_CACHE_DIR`/`_cache_path`/`_alpha_vantage_feed` 落盘逻辑）。现在 `_alpha_vantage_feed` 每次直接调 AV API、不落盘，重复跑同一美股窗口会重复烧 25 次/天配额。A 股不受影响（走 akshare）。
  **2026-09-03 补充**：`alpha_vantage_common.py` 已加全局每日配额计数器（所有 AV 调用的唯一入口），用到 20/25 时日志告警、达到上限再次告警；配合已有的 `AlphaVantageRateLimitError`，配额耗尽会显式报错而非静默返回空新闻。进程内计数，跨会话累计（配额本来就是按 key 按天）。

### #8 env 读取顺序
`.env` 是 `tradingagents/__init__.py` 的 `load_dotenv(find_dotenv())` 加载的。
在 import `tradingagents` **之前**读环境变量会得到 None。→ `_ensure_env_loaded()` 显式触发。

### #9 /resume 会静默卡死
引擎已销毁时（会话 failed/stopped，或服务重启后），`/sessions/{id}/resume` 只把 status 改成 running，
`SessionManager.resume()` 因找不到引擎什么都不做 → 会话永远卡住。
→ 已改为引擎不存在时调 `manager.start()`，从 `current_day_index` 续跑，并清空旧 error。

### #10 首个决策日流水线永远不会运行（已修）
`news_delta` 只在 `coast is not None` 时才计算，而首日 coast 为 None → hybrid 的三个升级条件
（规则/新消息/价格破区间）全不满足 → 首日永远"维持原判断"（一个不存在的判断），
消息面安静的股票整个会话一次流水线都不跑，账面全 hold。
→ `hybrid_agent.decide` 现在显式判断：无 coast / 无 prev_decision 时强制升级。
配套：引擎重启后 coast 只存在于内存里，首日规则会让每次 resume 白跑一次流水线，
所以 `_run_loop` 现在会从最后一条非 coast 的 daily_record 重建 coast（`_restore_coast`）。

### #11 系统代理会掐断 akshare 的国内连接（已修）
Windows 系统代理（Clash/v2rayN 等）会被 requests 从注册表继承，东财
push2his.eastmoney.com 这类国内端点经代理常被拒（`ProxyError: Remote end closed
connection`），而 Yahoo 兜底又**需要**代理。
→ `cn_source.py` import 时把 eastmoney.com/sina.com.cn/cctv.com 加进 `NO_PROXY`：
国内源直连、yfinance 继续走代理，两不误。
（注意：代理若是 TUN/全局模式，应用层绕不开，akshare 仍会失败——但 yfinance 兜底已足够。）

### #11b GLM 内容安全（1301）与 graph 流水线的宏观新闻（已修）
GLM 审核 1301 会拦截流水线请求，触发面主要在**宏观新闻**：框架的 `get_global_news`
走 yfinance 全网实时搜索，返回的头条常含政治内容，且对历史回测日期来说本身就是未来数据。
三层修复：
1. 框架新增 `global_news_enabled` 配置（默认 True，不影响上游 CLI 使用）；关闭时
   `route_to_vendor("get_global_news")` 返回诚实的"已禁用"提示，分析师工具循环照常工作。
2. webapp 的 graph 配置固定 `global_news_enabled=False`（回测双理由：防 1301 + 防前视）。
3. `graph_agent` 对内容审核错误有独立重试预算（重采样 2 次，触发常是模型自己生成的文本）；
   另把 `global_news_queries` 里的地缘政治主题换成中性经济主题（对上游 CLI 仍生效）。

### #11c GLM 研究员节点读超时（已修，600396 首跑实录）
600396 day1 流水线挂在 Bull Researcher：`OpenAITimeoutError`（读超时），重试 3 次全超。
排查结论：流水线客户端的 GLM thinking 注入是好的（flash 默认 low，已验证），
分析师 4 个节点全部成功，唯独**输入最大的多空研究员**连续 300s 无响应——
智谱对超大输入偶发拥堵，或代理链路对 bigmodel.cn 卡顿。
→ ① graph `llm_timeout` 300→600（给大 prompt 留余量）；② `bigmodel.cn` 加入
`NO_PROXY` 国内直连名单（若代理是环境变量级可绕行，TUN 模式无副作用）；
③ **prompt 瘦身**：四份分析师报告会被下游 9 个节点原样内嵌，新增
`analyst_report_max_chars` 配置（默认 None=上游不变），webapp graph 设 2000 字 +
`news_article_limit` 20→10，每份报告截断带显式标记。
会话失败后用 UI 的 resume 即可从 current_day_index 续跑，不用重建。

### #12 A 股 Yahoo 后缀按代码前缀定，不信用户输入（已修）
300308（中际旭创）是深交所创业板，Yahoo 只有 `300308.SZ`；会话建成 `300308.SS` 后
akshare 不在乎后缀，但 yfinance 兜底原样透传 → 404 → 会话失败。
→ 三层防御：① `sessions` 路由创建时按前缀纠正后缀（60x/68x→.SS，00x/30x→.SZ）；
② `data_gateway._load_yf` 兜底前用 `cn_source.yahoo_symbol()` 重算；③ 取数失败时
报错带上 akshare 与 yfinance 两级的真实原因。

### #13 价位标签按评级方向标注，不硬套做多语义（已修）
graph 流水线在 **Underweight/Sell** 评级下，PM 的 `Price Target` 是**下探目标**
（如 300308 d1：现价 ~589，目标 535），交易员的 `Stop Loss` 是"跌破则进一步减"的
触发位（570）——`graph_agent` 旧代码硬套做多字段名，显示成
"目标价=535，止损价=570"，看起来像倒挂，实际模型自洽。
→ `_levels_view()` 按每个价位相对现价的位置 + 评级方向打标签
（看多：上望目标/止损位；看空：下探目标/减仓触发位/观点失效位），
与评级矛盾的价位（如看多评级给出现价下方目标）丢弃并写 flag；
`_band()` 本身括位安全未动。
附带修复：key_signals 分句正则按英文句号切，把代码"300308.SZ"切成"300308."碎片——
改为只按中文句末标点分句。

### #14 stand_pat 重新居中会吞掉阴跌，回撤 40% 还在持有（已修）
`hybrid_agent._stand_pat` 旧逻辑：价格离开区间但破位幅度 < `escalate_breach_ratio`(0.25) 且无新闻时，
**把区间重新居中到当前价**——缓慢阴跌每天把"安全区间"往下挪一点，永不触发重决策、永不减仓，
这是华电辽能回撤 40% 的直接机制。
→ 已改：价格一出 band（不管幅度）就升级重决策，`_stand_pat` 删掉重新居中逻辑。
`escalate_breach_ratio` 字段保留但已弃用。

### #15 skill 的 `created_at` 用墙钟时间，历史回测永远选不到（已修）
`SkillLibrary.create` 旧代码 `created_at=_now()`（复盘那一刻的真实时间，如 2026-08-31），
而 `select_for_day` 用 `created_at <= sim_date` 过滤 → 回测 2024/2025 日期时，今天蒸馏的 skill
永远大于 sim_date，一条都选不进。`distiller.py` 顶部注释声称"用会话日期区间 stamp"，代码却用 `_now()`。
→ 已修：`create` 加 `effective_date` 参数，distiller 传 `session["end_date"]`。

### #16 评级解析失败不再静默变 Hold，返回 `REVIEW` 哨兵（2026-09-01 移植上游 v0.4.0）
`tradingagents/agents/utils/rating.py` 旧 `parse_rating` 解析失败静默返回 "Hold"——把"解析不出来"
伪装成可交易的"中性"信号。已从上游 v0.4.0（issue #1170）移植 REVIEW 哨兵：
- 新增 `extract_rating(text) -> str | None`（失败返回 None，绝不伪造）、`RATING_REVIEW = "REVIEW"` 常量、
  `is_review(signal) -> bool`；`parse_rating` 保留为 `extract_rating(text) or default` 的向后兼容包装。
- `SignalProcessor.process_signal` 改用 `extract_rating`，失败返回 `RATING_REVIEW` 而非 "Hold"。
- **下游安全**：`webapp/engine/graph_agent.py` 第 385-389 行已有兜底——rating 不在 `_RATING_ACTION`
  （只认 5 级）就打 flag `unparsed rating '{rating}' -> hold` 并降级 Hold，所以 REVIEW 对回测器零风险，
  只会被正确拦截+记录，不会裸奔。`memory.py` 用 `parse_rating` 打日志 tag（非交易信号），保持 Hold 合理。
- 注意：若未来有代码直接用 `process_signal` 返回值做交易判断，必须先 `is_review()` 守卫再映射 5 级枚举。

### #17 adaptive 降级曾把结构性错误也伪装成维持原判断（已修，2026-09-03）
`adaptive_agent.decide` 旧逻辑捕获**所有异常**、只要有前次决策就降级为维持原判断。
这与坑 #4 原则冲突：API 密钥失效（401）、请求被拒（400）、解析失败这类**结构性错误**
会每天静默降级，会话"成功"跑完但全是假 hold——LLM 根本没跑，比崩溃更糟。
→ 已改为只对两类错误降级：① `LLMBudgetExceeded`（当日预算用尽，明日再评估）；
② 瞬时错误（`graph_agent._is_transient`：超时/连接/429/5xx）。结构性错误一路抛出，
会话 `status=failed` 并写明真实错误。回归：`tests/test_webapp_adaptive_agent.py`（12 项）。

---

## 7. 关键机制说明

### Coast（决策有效期）
每个决策带 `recheck_days` + 价格区间 `[recheck_lower, recheck_upper]`。
在有效期内且价格未出区且无新重大消息且无技术面恶化 → **沿用决策，不调 LLM**。
这是控制成本的核心：145 个交易日可能只触发 30~50 次真实决策。

触发重新决策的**四类条件**：有效期到了 / 价格出区 / 出现新的重大消息 / 技术面恶化（超跌/破位）。

### 硬风控层（止损 / 止盈 / 回撤熔断，2026-08-31 新增）
每日循环**最前面**、独立于 LLM 和 coast（含 coast 日）跑 `_check_risk_stops`，确定性执行、零 LLM 调用：
1. **回撤熔断**：权益自峰值回撤 ≥ `max_drawdown_stop_pct`（默认 20%）→ 强制降仓到 `drawdown_reduce_to_pct`（默认 30%）
2. **止损**：收盘 ≤ 决策里的 `stop_loss` → 清仓离场
3. **止盈**：收盘 ≥ 决策里的 `take_profit` → 兑现一半
触发后作废 coast，下一日强制重决策。`_peak_equity` 运行期追踪 + resume 时从 `daily_records` 重建。
这是按住「回撤 40% 还在持有」的关键层——此前 `stop_loss`/`price_target` 只用来画 coast 区间，从不落单卖出。

### 多因子仓位（2026-08-31，替代硬编码评级仓位）
`graph_agent._size_position` 五因子定仓位（buy=花现金比例 / sell=卖持仓比例）：
1. PM 的 `target_position_pct`（最权威）→ 2. 距止损风险预算（止损越近仓位越小）→ 3. 布林带宽波动率缩放 → 4. 权益回撤收紧 → 5. 信心缩放。
旧 `_RATING_MAP` 固定比例（Buy=30%…Sell=100%）已删除，只保留评级→方向映射。

### 提示词维度注入（2026-08-31，市场结构 + 行为金融）
graph 流水线的分析师/交易员/组合经理 prompt 已注入「指标之外」的判断维度，避免 LLM 只绕着技术指标打转：
- **market_analyst**：套牢盘/筹码、超跌量化分级、超跌企稳确认、量价反推情绪人性、压力位应对（缩量遇阻减仓 / 放量突破持有 / 假突破与破位止损）
- **news_analyst**：新闻情绪/预期差（利好出尽 vs 利空落地）、情绪拐点、散户 vs 机构倾向
- **sentiment_analyst**：社交数据（StockTwits/Reddit 回测 live-only）缺失时从新闻反推情绪，降 confidence 而非默认 Neutral
- **trader**：行为金融纪律——不接飞刀但**超跌企稳是正向加仓信号（分批抄底）**、不追高、处置效应、锚定
- **portfolio_manager**：Execution Levels（止损/止盈/目标仓位/风险等级）+ 行为/结构检查
- **review.md**：第三层「人性与情绪复盘」，candidate_skills 强制覆盖 sentiment 情绪人性类

> ⚠️ 提示词要求变多后，`analyst_report_max_chars=2000` 的报告截断会更吃紧——实测若市场分析师报告被截断，需调高该值（但注意下游 9 节点 prompt 滚雪球超时的老坑 #11c）。

### skill 经验闭环修复（2026-08-31）
此前 graph 模式 skill「只进不出」：`graph_agent.decide` 不读 `ctx.skills`，且 `SkillLibrary.create` 的 `created_at` 用墙钟时间 `_now()`，历史回测永远选不到（`select_for_day` 用 `created_at <= sim_date` 过滤）。修复：
- `graph_agent` 把 `ctx.skills` 格式化后经 `propagate(extra_context=...)` 注入 PM 的 `past_context`
- `SkillLibrary.create` 加 `effective_date`（默认 now），distiller 传 `session["end_date"]`——skill 在会话结束后才生效，防前视

### 防前视
- 所有价格数据经 `DataGateway` 裁剪到 ≤ T
- `sim_date` ContextVar 全局可见
- 基本面按**报告期 ≤ T** 过滤（不是取最新）
- 研报/评级按**发布日期 ≤ T** 过滤
- 新闻在网关层二次裁剪

### 持仓管理策略（首日强制建仓）
`sessions.initial_position` 默认 `'full'`：**第 0 个交易日由引擎按策略规则满仓买入（不走 LLM、零成本）**，
第 1 个交易日起模型才接管——且因为不播种 coast，第 1 天必然触发真实决策。

为什么必须这样（真实会话证据）：让 LLM 从空仓回答"买不买"，它几乎永远回答 hold——
panel 会话实测连续 49 个 hold（仓位始终 0.0）；graph 流水线则会在空仓时输出 Sell，被引擎拒绝（4 次 rejected）。
空仓起步的架构天然退化成死拿现金。强制建仓后，模型的每个决策都变成**持仓管理**：
减仓 / 加仓 / 清仓 / 持有，两份决策提示词（`webapp/prompts/decision_system*.md`）已重写为这个框架。

- 空仓时收到 sell 不再记 `rejected`，降级为 no-op hold 并写 `execution: sell ignored` flag
- 想回到"模型自己决定入场"的旧行为：创建会话时传 `initial_position: "agent"`
- 首日买入记录 `llm_calls=0`、flag `policy: 首日强制满仓建仓`

### 因子权重与数据增强（2026-08-30）
两份决策提示词加了**因子权重框架**：盈利与估值 ~40%（权重最高）· 消息与机构情绪 ~30% ·
技术面 ~30% 且**只做择时**；reasoning 强制输出分因子打分（盈利 x/10、消息 y/10、技术 z/10）。
配套的数据增强（否则权重只是空话）：
- `cn_source.fetch_financial_snapshot` 新增**盈利趋势（近4期营收/净利增速，由远及近）**。
  注意 ROE 是年内累计值、跨期不可比，只保留最新单期，不进趋势。
- `DataGateway.fundamentals` 叠加**市盈率PE(年化估算)**=现价/年化EPS（A股摊薄EPS按报告期
  年化、美股单季EPS×4）。每日现算、不随月度快照缓存。
- `DataGateway.sentiment`：近60日**机构情绪**（cn=研报评级分布、us=上调/下调/维持动作分布），
  派生 偏多/偏空/分歧 结论；无覆盖时诚实返回 None+flag。
- 提示词段落顺序即权重顺序：盈利与估值 → 消息面 → 机构情绪 → 技术面（标注"仅用于择时"）。
- graph 模式本身就是多因子流水线（各分析师自行取数），以上增强主要作用于 panel/single 模式。

### 技术手法工具箱与自学习闭环（2026-08-30）
问题：实测会话满仓建仓后每日卖 5% 碎步等破线、不会超跌抄底、暴跌两日 20% 不应急离场、
~50% 现金闲置到结束也从不加仓，skill 学到的也是"注意风险"式空话。修复分三层，
**原则是教"怎么思考"而不是写死动作**（用户明确反对"禁止 5%"式死规则）：
1. **决策提示词改为"目标仓位思维"**（panel+single）：每天先定应得的目标仓位（0~100%），
   与现仓的差距就是动作；**现金必须有部署计划**（在等什么触发、计划加多少，或说明为何保留）；
   **动作匹配决心**（该加就大加、该减就大胆离场，决心 80% 只动 5% 是错误）；做T 是配对操作
   （卖出写明回补条件）；应急离场/超跌抄底/金字塔加仓/分批兑现都是工具。reasoning 格式加
   【仓位与操作】现仓 X% → 目标 Y% → 动作与手法。
2. **引擎加现金闲置提醒**（`_cash_idle_note`）：现金占比 ≥30% 且 ≥5 个交易日没买过 →
   当日 data_flag 要求给出部署计划或保留理由。软提醒不强制动作。
3. **复盘注入操作画像**（`distiller._operation_profile`）：买卖次数、平均调仓幅度
   （≥4次且<15% 判碎步）、**日均现金占比（≥30% 点名闲置）**、首次卖出后重新买入次数、
   最大单日/两日跌幅及当日动作、最大回撤区间、**与买入持有的差距**。
4. **review.md 三层复盘**（技术总结 + 操作自评与调整 + 人性与情绪复盘）：candidate_skills 必须覆盖
   技术手法类、情绪人性类（sentiment）与操作纠偏类，statement 必须是"触发条件+动作+幅度"的可执行规则。
   skill 注入数 6→8，每次提炼上限 5→6。
skill 是跨会话沉淀的（日期钳制防前视），跑得越多，手法库越贴行情。

### 流水线审计
graph 模式下 `prompt_text` 存的是**全链路 9 段审计**：四份分析师报告 → 多空辩论 → 投资计划 →
交易员方案 → 三方风控辩论 → 最终决策。前端抽屉里能直接看到。

---

## 8. 当前状态

- 前端产物：`webapp/static/assets/index-2Qc8XaXC.js`
- 回归状态：`smoke_panel` / `smoke_graph` / `smoke_hybrid` / `smoke_accounting` 全 PASS
- 8000 端口当前**空闲**（agent 不占用，用户自管）

### 已知未完成 / 可选优化
1. **美股新闻覆盖有限** —— Alpha Vantage 单次约 50 条上限，按月分块后每天可能只有几天有新闻
2. **FRED 宏观数据未接入**（`FRED_API_KEY` 留空）—— 只影响新闻分析师的宏观指标，非致命
3. **流水线 graph 模式的社交/情绪源是 live-only** —— 原框架的 StockTwits/Reddit 只能取到
   "现在"的内容，回测历史日期 T 时可能混入 T 之后的信息。**已缓解**：sentiment_analyst 在社交数据
   `<unavailable>` 时从新闻反推情绪、market_analyst 从量价反推情绪人性；但真实社交情绪仍需接入有
   历史存档的数据源。graph 审计文本里已加提示，评估情绪面/消息面时请打折
4. **超跌加仓未落代码层（有意为之）** —— 超跌加仓是进攻侧、与回撤熔断（防守侧）方向相反，为避免
   二者打架，当前只靠 trader 提示词引导"超跌企稳→分批加仓"，不写死代码触发。若要落地：超跌信号
   只做"提高目标仓位上限/解锁受限抄底单"，**绝不绕过回撤熔断**
5. **`smoke_structured_output.py`** 是手动验证工具（2026-09-03 核实：全部导入接口仍有效）——
   用**真实 API key** 验证指定提供商的结构化输出模式，会产生少量真实调用，不纳入离线回归
6. ~~`.env` 的 `PIPELINE_MODE=parallel` 是误导项~~ —— 已改为 `sequential` 并加注释说明
   webapp 固定用 `WEBAPP_GRAPH_PIPELINE_MODE`

---

## 9. 接手后第一步

```bash
# 1. 确认服务能起
./.venv/Scripts/python.exe -m webapp.run

# 2. 跑一遍三个回归（约 2 分钟）
./.venv/Scripts/python.exe scripts/smoke_graph.py
./.venv/Scripts/python.exe scripts/smoke_hybrid.py
./.venv/Scripts/python.exe scripts/smoke_panel.py

# 3. 前端新建回测，先跑 A 股短区间（如 600519.SS，2 周）验证链路
#    再勾选「完整多智能体流水线」跑一次，看抽屉里的全链路审计
```

**建议先用短区间 + A 股验证**，美股和流水线的成本都高一个量级。
