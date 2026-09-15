"""Two-call investment committee: balanced analysis, then executable decision."""
from __future__ import annotations

import json

from webapp.config import WebappSettings
from webapp.core.errors import LLMBudgetExceeded
from webapp.engine.context_builder import DailyContext
from webapp.engine.decision_agent import DecisionAgent, _extract_text, _parse_decision

_ANALYSIS_SYSTEM = """你是精简投资委员会的综合分析员。不要扮演多个轮流发言的角色；
请一次性审视技术面、基本面、消息面、当前仓位和历史经验，只输出一个 JSON 对象：
{"bull_case":["看多证据"],"bear_case":["看空证据"],"risks":["主要风险"],
 "uncertainties":["尚不确定的信息"],"key_levels":{"support":数字或null,"resistance":数字或null}}。
分析 A 股时，高PE只能影响仓位与置信度，不能单独否决买入；亏损或刚扭亏时禁用
绝对PE，优先检查扣非利润、经营现金流和盈利持续性。技术企稳出现一个可观察信号
即可支持试探仓，不得为了显得谨慎而无限叠加确认条件。
证据必须来自用户提供的数据，不得补造行情或新闻。"""

_DECISION_SYSTEM = """你是投资委员会的最终决策人。根据综合分析和真实账户输出严格 JSON：
action(buy|sell|hold)、position_pct(0~1)、confidence(0~1)、reasoning、
key_signals(数组)、used_skills(数组)、recheck_days(1~10)，以及可选的
recheck_upper、recheck_lower、stop_loss、take_profit、target_position_pct。
position_pct 对 buy 表示动用可用现金的比例，对 sell 表示卖出现有持仓的比例。
分析 A 股时，高PE只能影响仓位与置信度，不能单独否决买入；将盈利拐点视为催化剂，
用目标仓位分层表达不确定性，而不是一律等待低PE。
禁止输出 markdown 或 JSON 之外的文字。"""


class CommitteeDecisionAgent:
    def __init__(self, settings: WebappSettings, analyst_llm=None, decision_llm=None):
        self.settings = settings
        self.analyst_llm = analyst_llm or self._make_llm("low")
        self.decision_llm = decision_llm or self._make_llm("high")
        self._call_count = 0

    def _make_llm(self, thinking_level: str):
        from webapp.llm import get_llm

        return get_llm(self.settings, timeout=300, thinking_level=thinking_level)

    def reset_day(self) -> None:
        self._call_count = 0

    def _invoke(self, llm, messages):
        if self._call_count >= self.settings.adaptive_max_calls_per_day:
            raise LLMBudgetExceeded(
                f"adaptive committee exceeded {self.settings.adaptive_max_calls_per_day} calls"
            )
        self._call_count += 1
        return llm.invoke(messages)

    def decide(self, ctx: DailyContext):
        context = ctx.to_user_message()
        analysis_response = self._invoke(
            self.analyst_llm,
            [("system", _ANALYSIS_SYSTEM), ("human", context)],
        )
        analysis_text = _extract_text(analysis_response)
        analysis = self._parse_analysis(analysis_text)

        account = ctx.portfolio or {}
        decision_user = (
            f"综合分析：\n{json.dumps(analysis, ensure_ascii=False)}\n\n"
            f"真实账户：现金={account.get('cash')}，持仓股数={account.get('shares')}，"
            f"总资产={account.get('equity')}，持仓成本={account.get('avg_cost')}。\n\n"
            f"原始数据：\n{context}"
        )
        decision_response = self._invoke(
            self.decision_llm,
            [("system", _DECISION_SYSTEM), ("human", decision_user)],
        )
        decision_text = _extract_text(decision_response)
        parsed = _parse_decision(decision_text)
        if not hasattr(parsed, "action"):
            raise ValueError(str(parsed))

        flags = ["adaptive:path=committee"]
        decision = DecisionAgent.reconcile(parsed, ctx, flags)
        audit = self._audit(ctx, analysis, decision_text)
        return decision, audit, decision_text, flags

    @staticmethod
    def _parse_analysis(text: str) -> dict:
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.strip("`")
            if cleaned.lower().startswith("json"):
                cleaned = cleaned[4:].strip()
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("committee analysis returned no JSON object")
        payload = json.loads(cleaned[start : end + 1])
        required = {"bull_case", "bear_case", "risks", "uncertainties", "key_levels"}
        if not isinstance(payload, dict) or not required.issubset(payload):
            raise ValueError("committee analysis JSON is missing required fields")
        return payload

    @staticmethod
    def _audit(ctx: DailyContext, analysis: dict, decision_text: str) -> str:
        def lines(key):
            values = analysis.get(key) or []
            return "；".join(str(value) for value in values) or "无"

        return "\n".join(
            [
                f"交易日: {ctx.sim_date}（{ctx.symbol}）",
                "模式: 自适应精简架构 / 两步投资委员会",
                f"多头证据: {lines('bull_case')}",
                f"空头证据: {lines('bear_case')}",
                f"主要风险: {lines('risks')}",
                f"不确定性: {lines('uncertainties')}",
                f"关键价位: {json.dumps(analysis.get('key_levels'), ensure_ascii=False)}",
                "",
                "最终结构化决策:",
                decision_text,
            ]
        )
