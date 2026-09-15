# TradingAgents 回测模拟器（webapp）

在 TradingAgents 多智能体框架之上构建的网页版逐日回测交易系统。

## 功能

- 给定初始资金 + 一只股票 + 日期区间，agent **逐日**分析并自主决策：买入（自定仓位比例）/ 卖出（可部分）/ 持有
- **历史可得性保护**：行情/新闻经模拟时钟截断；财报在没有真实披露时间时按保守披露期限延后可见；每日完整 prompt 落库可审计
- **市场自适应数据源**：`.SS/.SZ` → akshare（东财行情/新闻/财报），其余 → yfinance
- 多个回测可同时运行（并发上限 3，可暂停/停止）
- 区间结束自动复盘：逐笔交易点评 + 总结反思 + 提炼**分类经验（skill）**
- **经验闭环**：skill 库按日期钳制自动注入后续回测的每日决策 prompt，按成功率加权
- **A 股执行适配**：主板/创业板按 100 股整手、科创板按最低 200 股且可逐股递增，并计入卖方证券交易印花税
- 网页端：K线（含买卖标记）+ 权益曲线 + 每日操作表 + 决策抽屉（看完整 prompt）+ 复盘报告 + 经验库管理

## 决策架构

新建回测时可选择三种架构：

- **自适应精简（默认）**：普通日沿用有效观点，不调用 LLM；一般新增信息调用一次快速决策；首日、重大消息、技术状态切换或价格区间失效时使用两步投资委员会。
- **经典完整**：保留分析师、多空辩论、交易员、三方风控和组合经理的完整 Graph，用于对照实验和完整审计。
- **快速单模型**：需要重新决策时只运行一次角色面板，适合快速试跑。

自适应模式可通过 `WEBAPP_ADAPTIVE_FAST_STREAK_LIMIT`（默认 5）控制连续快速决策后的全面复核，通过 `WEBAPP_ADAPTIVE_MAX_CALLS_PER_DAY`（默认 4）限制每日直接调用预算。硬止损、止盈、回撤熔断和账户核对在三种架构中均由代码执行。

## 启动

```powershell
cd C:\Users\Lenovo\Desktop\TradingAgents-main
.\.venv\Scripts\Activate.ps1
pip install -r webapp\requirements-webapp.txt
python -m webapp.run
# 浏览器打开 http://127.0.0.1:8000
```

LLM 配置复用项目根目录 `.env`（DeepSeek `deepseek-v4-flash`，查询 agent 用 low 思考、决策用 high）。

## 目录

```
webapp/
├── core/       组合账务（Portfolio/ExecutionModel）、领域模型、错误
├── engine/     模拟时钟、数据网关（市场路由+截断）、决策agent、每日循环
│   └── vendors/cn_source.py   A股数据源（akshare）
├── skills/     经验库（注入/归因）与复盘提炼（distiller）
├── store/      SQLite（WAL）会话/每日记录/交易/经验
├── server/     FastAPI 路由 + 静态前端（CDN Vue3 + ECharts，无需 Node）
└── prompts/    决策/复盘/去重提示词
```

## 数据时序与回测边界

1. 每个交易日 T 开始时设置 `sim_date` ContextVar
2. 行情帧一次性拉取后**只读切片 `Date <= T`**；akshare 返回统一二次截断
3. StockTwits/Reddit/Polymarket/`ticker.info`/内部人交易等实时源在回测中**不调用**，缺失记入 data_flags
4. 财务报表不能用报告期末冒充披露日：A 股按季报/中报/三季报/年报期限保守延后，US 季度表按 45 天、年表按 90 天延后
5. skill 从来源会话结束的下一日起生效，不能反向影响产生它的会话
6. 外部新闻、研报和 skill 在提示词中均标记为不可信证据，并有字段/总量上限
7. 每日完整 prompt 持久化，可用脚本审计（不得出现 T 日之后的日期）

已知边界：财务供应商提供的是当前修订快照，不是逐日保存的 point-in-time 数据库；保守披露滞后能避免“报告期当天可见”，但不能还原后续重述前的原始值。当前成交仍按 T 日收盘价做研究近似，会高估实盘可实现性；提示词已明确实盘只能把结论作为下一交易时段计划。需要严格执行回测时，应进一步采用“前收盘生成信号、次日开盘成交”的账务模式。
