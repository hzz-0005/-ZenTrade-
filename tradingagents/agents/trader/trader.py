"""Trader: turns the Research Manager's investment plan into a concrete transaction proposal."""

from __future__ import annotations

import functools

from langchain_core.messages import AIMessage

from tradingagents.agents.schemas import TraderProposal, render_trader_proposal
from tradingagents.agents.utils.agent_utils import (
    get_instrument_context_from_state,
    get_language_instruction,
)
from tradingagents.agents.utils.structured import (
    NO_EXTERNAL_TOOLS,
    bind_structured,
    invoke_structured_or_freetext,
)


def create_trader(llm):
    structured_llm = bind_structured(llm, TraderProposal, "Trader")

    def trader_node(state, name):
        company_name = state["company_of_interest"]
        instrument_context = get_instrument_context_from_state(state)
        investment_plan = state["investment_plan"]

        messages = [
            {
                "role": "system",
                "content": (
                    "You are a trading agent analyzing market data to make investment decisions. "
                    "Based on your analysis, provide a specific recommendation to buy, sell, or hold. "
                    "Anchor your reasoning in the analysts' reports and the research plan. "
                    "Apply behavioral-finance discipline: do not catch a falling knife — but treat a "
                    "confirmed oversold stabilization (缩量止跌、长下影、重新站回5日/10日线、MACD底背离) "
                    "as a positive ADD-the-dip signal and act on it, adding in tranches rather than "
                    "all at once; never add while the knife is still falling. Do not chase euphoric "
                    "rallies. Beware the disposition effect (selling winners too early / holding "
                    "losers too long) and anchoring to your own cost basis or to a prior high. Size "
                    "the position against the stop-loss distance and volatility, and state the exact "
                    "trigger that would invalidate the trade. "
                    "Time against the swing, not the single day: sell into strength (take profit as "
                    "the name stretches above its recent highs) and buy stabilised weakness. Buy "
                    "triggers are many — not just moving-average breaks (站稳20日线 is only one of "
                    "them): 超跌企稳/缩量止跌, 超卖反弹, 波段回调低吸(回踩支撑), 突破回踩确认, "
                    "利空出尽反转, 长下影/MACD底背离 — pick whichever matches the tape instead of "
                    "waiting on a single indicator — rather than selling every dip and every rip. "
                    "Every sell must carry its re-entry trigger "
                    "— the price or signal at which you would buy back — so a 'T' trade never becomes "
                    "an excuse for a trend exit. "
                    "\n\nProfessional habits you MUST keep (these are the failure modes of this desk):\n"
                    "1. **Follow-through beats fresh opinions.** Your own prior plan is your first "
                    "input: if a trigger you stated earlier (re-entry price, add-on level, stop, "
                    "take-profit) has been hit by today's data, EXECUTE it. Do not answer a fired "
                    "trigger with a brand-new 'wait for confirmation' — waiting for confirmation "
                    "after the confirmation arrived is analysis paralysis, and '观望/待确认' may be "
                    "used at most once per setup.\n"
                    "2. **Protect open profits.** With a large unrealized gain, trail it: name the "
                    "pullback level that banks the profit (e.g. highest close minus 8~10%). Letting "
                    "winners run does NOT mean riding a +50% gain back to zero (坐电梯) — if the gain "
                    "retraces by a third, trim without debate.\n"
                    "3. **Time stops.** If the entry thesis has not played out within its stated "
                    "window, reduce or exit — idle capital pays opportunity cost, and every '再等等' "
                    "must come with a deadline and a reason.\n"
                    "4. **Flat-book duty.** When the account holds no position, your only job is the "
                    "re-entry plan: the exact price/signal and the tranche size. When that setup "
                    "actually appears, you buy it — a good name you never re-enter is a missed trade, "
                    "not a good decision.\n"
                    "5. **A hold is a decision, not a default.** Every hold must re-validate the "
                    "thesis AND state the position it applies to (current weight -> target weight)."
                    + NO_EXTERNAL_TOOLS
                    + get_language_instruction()
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Based on a comprehensive analysis by a team of analysts, here is an investment "
                    f"plan tailored for {company_name}. {instrument_context} This plan incorporates "
                    f"insights from current technical market trends, macroeconomic indicators, and "
                    f"social media sentiment. Use this plan as a foundation for evaluating your next "
                    f"trading decision.\n\nProposed Investment Plan: {investment_plan}\n\n"
                    f"Leverage these insights to make an informed and strategic decision."
                ),
            },
        ]

        trader_plan = invoke_structured_or_freetext(
            structured_llm,
            llm,
            messages,
            render_trader_proposal,
            "Trader",
        )

        return {
            "messages": [AIMessage(content=trader_plan)],
            "trader_investment_plan": trader_plan,
            "sender": name,
        }

    return functools.partial(trader_node, name="Trader")
