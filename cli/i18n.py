"""CLI 界面中文映射。

默认把 CLI 的英文提示/面板标题映射为中文。设置环境变量
TRADINGAGENTS_CLI_LANGUAGE=en 可切回英文原文。

用法：
    from cli.i18n import t
    console.print(t("Step 1: Ticker Symbol"))
    # 带占位符的字符串：t("Analyzing {ticker}...", ticker="NVDA")

注意：agent 英文名（如 "Market Analyst"）在内部被当作字典键使用，
只在「展示层」调用 t() 翻译，内部逻辑一律保持英文。
"""
import os

from dotenv import load_dotenv

load_dotenv()


def _lang() -> str:
    return os.environ.get("TRADINGAGENTS_CLI_LANGUAGE", "zh").lower()


_ZH = {
    # ===== 欢迎页 =====
    "TradingAgents: Multi-Agents LLM Financial Trading Framework - CLI":
        "TradingAgents：多智能体 LLM 金融交易框架 - 命令行界面",
    "Workflow Steps:": "工作流程：",
    "I. Analyst Team → II. Research Team → III. Trader → IV. Risk Management → V. Portfolio Management":
        "I. 分析师团队 → II. 研究团队 → III. 交易员 → IV. 风险管理 → V. 投资组合管理",
    "Welcome to TradingAgents": "欢迎使用 TradingAgents",
    "Welcome to TradingAgents CLI": "欢迎使用 TradingAgents 命令行界面",
    "Multi-Agents LLM Financial Trading Framework": "多智能体 LLM 金融交易框架",

    # ===== 问题步骤 =====
    "Step 1: Ticker Symbol": "第 1 步：股票代码",
    "Enter the ticker, with exchange suffix when needed (e.g. SPY, 0700.HK, BTC-USD)":
        "输入股票代码，需要时带上交易所后缀（如 SPY、0700.HK、BTC-USD）",
    "Step 2: Analysis Date": "第 2 步：分析日期",
    "Enter the analysis date (YYYY-MM-DD)": "输入分析日期（YYYY-MM-DD）",
    "Step 3: Output Language": "第 3 步：输出语言",
    "Select the language for analyst reports and final decision":
        "选择分析报告和最终决策的输出语言",
    "Step 4: Analysts Team": "第 4 步：分析师团队",
    "Select your LLM analyst agents for the analysis": "选择参与分析的 LLM 分析师智能体",
    "Step 5: Research Depth": "第 5 步：研究深度",
    "Select your research depth level": "选择研究深度等级",
    "Step 6: LLM Provider": "第 6 步：LLM 提供商",
    "Select your LLM provider": "选择你的 LLM 提供商",
    "Step 7: Thinking Agents": "第 7 步：思考模型",
    "Select your thinking agents for analysis": "选择用于分析的思考模型",
    "✓ Output language from environment:": "✓ 输出语言（来自环境变量）：",
    "✓ Research depth from environment:": "✓ 研究深度（来自环境变量）：",
    "✓ LLM provider from environment:": "✓ LLM 提供商（来自环境变量）：",
    "✓ Backend URL:": "✓ 接口地址：",
    "✓ Thinking agents from environment:": "✓ 思考模型（来自环境变量）：",
    "debate /": "轮辩论 /",
    "risk rounds": "轮风险讨论",
    "Detected asset type:": "检测到资产类型：",

    # ===== 通用提示 =====
    "Default:": "默认：",
    "Selected analysts:": "已选择的分析师：",
    "Selected ticker:": "已选择股票：",
    "Analysis date:": "分析日期：",
    "Error: Analysis date cannot be in the future":
        "错误：分析日期不能是未来日期",
    "Error: Invalid date format. Please use YYYY-MM-DD":
        "错误：日期格式无效，请使用 YYYY-MM-DD 格式",

    # ===== 仪表盘面板 =====
    "Team": "团队",
    "Agent": "智能体",
    "Status": "状态",
    "Time": "时间",
    "Type": "类型",
    "Content": "内容",
    "Progress": "进度",
    "Messages & Tools": "消息与工具",
    "Current Report": "当前报告",
    "Waiting for analysis report...": "等待分析报告……",
    "pending": "等待中",
    "in_progress": "进行中",
    "completed": "已完成",
    "error": "错误",
    "Tool": "工具",
    "Agents:": "智能体：",
    "Reports:": "报告：",
    "Analyzing {ticker} on {date}...": "正在分析 {ticker}（{date}）……",
    "Completed analysis for {date}": "{date} 分析完成",

    # ===== 团队名（展示用） =====
    "Analyst Team": "分析师团队",
    "Research Team": "研究团队",
    "Trading Team": "交易团队",
    "Risk Management": "风险管理",
    "Portfolio Management": "投资组合管理",

    # ===== 智能体名（展示用） =====
    "Market Analyst": "行情分析师",
    "Sentiment Analyst": "舆情分析师",
    "News Analyst": "新闻分析师",
    "Fundamentals Analyst": "基本面分析师",
    "Bull Researcher": "多方研究员",
    "Bear Researcher": "空方研究员",
    "Research Manager": "研究经理",
    "Trader": "交易员",
    "Aggressive Analyst": "激进型分析师",
    "Neutral Analyst": "中立型分析师",
    "Conservative Analyst": "保守型分析师",
    "Portfolio Manager": "投资组合经理",

    # ===== 报告章节标题 =====
    "Market Analysis": "行情分析",
    "Social Sentiment": "社交舆情",
    "News Analysis": "新闻分析",
    "Fundamentals Analysis": "基本面分析",
    "Research Team Decision": "研究团队决策",
    "Trading Team Plan": "交易团队计划",
    "Portfolio Management Decision": "投资组合管理决策",
    "## Analyst Team Reports": "## 分析师团队报告",
    "## Research Team Decision": "## 研究团队决策",
    "## Trading Team Plan": "## 交易团队计划",
    "## Portfolio Management Decision": "## 投资组合管理决策",
    "### Market Analysis": "### 行情分析",
    "### Social Sentiment": "### 社交舆情",
    "### News Analysis": "### 新闻分析",
    "### Fundamentals Analysis": "### 基本面分析",
    "### Bull Researcher Analysis": "### 多方研究员分析",
    "### Bear Researcher Analysis": "### 空方研究员分析",
    "### Research Manager Decision": "### 研究经理决策",
    "### Aggressive Analyst Analysis": "### 激进型分析师分析",
    "### Conservative Analyst Analysis": "### 保守型分析师分析",
    "### Neutral Analyst Analysis": "### 中立型分析师分析",
    "### Portfolio Manager Decision": "### 投资组合经理决策",

    # ===== 最终报告展示 =====
    "Complete Analysis Report": "完整分析报告",
    "I. Analyst Team Reports": "I. 分析师团队报告",
    "II. Research Team Decision": "II. 研究团队决策",
    "III. Trading Team Plan": "III. 交易团队计划",
    "IV. Risk Management Team Decision": "IV. 风险管理团队决策",
    "V. Portfolio Manager Decision": "V. 投资组合经理决策",

    # ===== 分析后交互 =====
    "Analysis Complete!": "分析完成！",
    "Save report?": "是否保存报告？",
    "Save path (press Enter for default)": "保存路径（回车使用默认路径）",
    "✓ Report saved to:": "✓ 报告已保存至：",
    "Complete report:": "完整报告：",
    "Error saving report:": "保存报告出错：",
    "Display full report on screen?": "是否在屏幕上显示完整报告？",
    "Cleared {n} checkpoint(s).": "已清除 {n} 个检查点。",

    # ===== utils.py 交互提示 =====
    "Enter ticker symbol (e.g. {examples}):": "输入股票代码（如 {examples}）：",
    "Please enter a valid ticker symbol, e.g. AAPL, 000404.SZ, 0700.HK, GC=F.":
        "请输入有效的股票代码（不支持中文名称）。A股：沪市 600519.SS / 深市 002485.SZ；港股：0700.HK；美股：NVDA。",
    "No ticker symbol provided. Exiting...": "未输入股票代码，退出……",
    "Please enter a valid date in YYYY-MM-DD format.":
        "请输入有效的 YYYY-MM-DD 格式日期。",
    "No date provided. Exiting...": "未输入日期，退出……",
    "Select Your [Analysts Team]:": "选择你的[分析师团队]：",
    "\n- Press Space to select/unselect analysts\n- Press 'a' to select/unselect all\n- Press Enter when done":
        "\n- 按空格键选中/取消分析师\n- 按 'a' 全选/全不选\n- 选好后按回车确认",
    "You must select at least one analyst.": "至少需要选择一名分析师。",
    "No analysts selected. Exiting...": "未选择分析师，退出……",
    "Shallow - Quick research, few debate and strategy discussion rounds":
        "浅层 - 快速研究，少量辩论与策略讨论轮次",
    "Medium - Middle ground, moderate debate rounds and strategy discussion":
        "中层 - 适中，中等辩论轮次与策略讨论",
    "Deep - Comprehensive research, in depth debate and strategy discussion":
        "深层 - 全面研究，深入辩论与策略讨论",
    "Select Your [Research Depth]:": "选择你的[研究深度]：",
    "\n- Use arrow keys to navigate\n- Press Enter to select":
        "\n- 用方向键上下移动\n- 按回车键选择",
    "No research depth selected. Exiting...": "未选择研究深度，退出……",
    "No model selected. Exiting...": "未选择模型，退出……",
    "Select Your [{mode}-Thinking LLM Engine]:": "选择你的[{mode}思考]模型：",
    "Quick": "快速",
    "Deep": "深度",
    "No {mode} thinking llm engine selected. Exiting...": "未选择{mode}思考模型，退出……",
    "Custom model ID": "自定义模型 ID",
    "Enter model ID:": "输入模型 ID：",
    "Please enter a model ID.": "请输入模型 ID。",
    "Enter Azure deployment name ({mode}-thinking):": "输入 Azure 部署名称（{mode}思考）：",
    "Please enter a deployment name.": "请输入部署名称。",
    "Select Your [{mode}-Thinking] OpenRouter Model (latest available):":
        "选择你的[{mode}思考] OpenRouter 模型（最新可用）：",
    "Enter OpenRouter model ID (e.g. google/gemma-4-26b-a4b-it):":
        "输入 OpenRouter 模型 ID（如 google/gemma-4-26b-a4b-it）：",
    "No LLM provider selected. Exiting...": "未选择 LLM 提供商，退出……",
    "Cancelled. Exiting...": "已取消，退出……",
    "Select Reasoning Effort:": "选择推理力度：",
    "Medium (Default)": "中等（默认）",
    "High (More thorough)": "高（更彻底）",
    "Low (Faster)": "低（更快）",
    "Select Effort Level:": "选择力度等级：",
    "High (recommended)": "高（推荐）",
    "Medium (balanced)": "中（均衡）",
    "Low (faster, cheaper)": "低（更快、更省）",
    "Select Thinking Mode:": "选择思考模式：",
    "Enable Thinking (recommended)": "开启思考（推荐）",
    "Minimal/Disable Thinking": "最少思考/关闭思考",
    "Select GLM platform:": "选择 GLM 平台：",
    "Z.AI — api.z.ai (international, uses ZHIPU_API_KEY)":
        "Z.AI — api.z.ai（国际站，使用 ZHIPU_API_KEY）",
    "BigModel — open.bigmodel.cn (China, uses ZHIPU_CN_API_KEY)":
        "BigModel — open.bigmodel.cn（国内站，使用 ZHIPU_CN_API_KEY）",
    "Select Qwen region:": "选择通义千问区域：",
    "International — dashscope-intl.aliyuncs.com (uses DASHSCOPE_API_KEY)":
        "国际站 — dashscope-intl.aliyuncs.com（使用 DASHSCOPE_API_KEY）",
    "China — dashscope.aliyuncs.com (uses DASHSCOPE_CN_API_KEY)":
        "中国站 — dashscope.aliyuncs.com（使用 DASHSCOPE_CN_API_KEY）",
    "Select MiniMax region:": "选择 MiniMax 区域：",
    "Global — api.minimax.io (uses MINIMAX_API_KEY)":
        "全球站 — api.minimax.io（使用 MINIMAX_API_KEY）",
    "China — api.minimaxi.com (uses MINIMAX_CN_API_KEY)":
        "中国站 — api.minimaxi.com（使用 MINIMAX_CN_API_KEY）",
    "{env_var} is not set in your environment.": "环境中未设置 {env_var}。",
    "Paste your {env_var} (will be saved to .env):": "粘贴你的 {env_var}（将保存到 .env）：",
    "Skipped. API calls will fail until {env_var} is set.":
        "已跳过。在设置 {env_var} 之前，API 调用会失败。",
    "Saved {env_var} to {env_path}": "已将 {env_var} 保存到 {env_path}",
    "Select Output Language:": "选择输出语言：",
    "English (default)": "英语（默认）",
    "Chinese (中文)": "中文",
    "Japanese (日本語)": "日语（日本語）",
    "Korean (한국어)": "韩语（한국어）",
    "Hindi (हिन्दी)": "印地语（हिन्दी）",
    "Spanish (Español)": "西班牙语（Español）",
    "Portuguese (Português)": "葡萄牙语（Português）",
    "French (Français)": "法语（Français）",
    "German (Deutsch)": "德语（Deutsch）",
    "Arabic (العربية)": "阿拉伯语（العربية）",
    "Russian (Русский)": "俄语（Русский）",
    "Custom language": "自定义语言",
    "Enter language name (e.g. Turkish, Vietnamese, Thai, Indonesian):":
        "输入语言名称（如 Turkish、Vietnamese、Thai、Indonesian）：",
    "Please enter a language name.": "请输入语言名称。",
    "Enter the OpenAI-compatible base URL "
    "(e.g. http://localhost:8000/v1 for vLLM, http://localhost:1234/v1 for LM Studio):":
        "输入 OpenAI 兼容接口地址 "
        "（如 vLLM 用 http://localhost:8000/v1，LM Studio 用 http://localhost:1234/v1）：",
    "Enter a URL starting with http:// or https://": "请输入以 http:// 或 https:// 开头的地址",
    "No endpoint URL provided. Exiting...": "未输入接口地址，退出……",
    "OpenAI-compatible (vLLM, LM Studio, llama.cpp, custom relay)":
        "OpenAI 兼容接口（vLLM、LM Studio、llama.cpp、自定义中转）",
    "✓ {label} from environment:": "✓ {label}（来自环境变量）：",
    "Gemini thinking mode": "Gemini 思考模式",
    "Reasoning effort": "推理力度",
    "Claude effort": "Claude 力度等级",
    "Step 8: Thinking Mode": "第 8 步：思考模式",
    "Configure Gemini thinking mode": "配置 Gemini 思考模式",
    "Step 8: Reasoning Effort": "第 8 步：推理力度",
    "Configure OpenAI reasoning effort level": "配置 OpenAI 推理力度等级",
    "Step 8: Effort Level": "第 8 步：力度等级",
    "Configure Claude effort level": "配置 Claude 力度等级",
    "✓ Using Ollama at {url}{origin}": "✓ 使用 Ollama：{url}{origin}",
    " (from OLLAMA_BASE_URL)": "（来自 OLLAMA_BASE_URL）",

    # ===== 消息类型（展示用） =====
    "User": "用户",
    "Data": "数据",
    "Control": "控制",
    "System": "系统",

    # ===== 底部统计 =====
    "LLM:": "LLM 调用：",
    "Tools:": "工具调用：",
    "Tokens:": "Token：",
}


def t(s: str, **kwargs) -> str:
    """把英文界面文案映射为中文；带 kwargs 时先映射再做 str.format。"""
    out = s
    if _lang() == "zh":
        out = _ZH.get(s, s)
    if kwargs:
        out = out.format(**kwargs)
    return out
