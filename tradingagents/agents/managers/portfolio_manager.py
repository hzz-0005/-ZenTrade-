"""Portfolio Manager: synthesises the risk-analyst debate into the final decision.

Uses LangChain's ``with_structured_output`` so the LLM produces a typed
``PortfolioDecision`` directly, in a single call.  The result is rendered
back to markdown for storage in ``final_trade_decision`` so memory log,
CLI display, and saved reports continue to consume the same shape they do
today.  When a provider does not expose structured output, the agent falls
back gracefully to free-text generation.
"""

from __future__ import annotations

from langchain_core.messages import AIMessage, SystemMessage, ToolMessage

from tradingagents.agents.schemas import PortfolioDecision, render_pm_decision
from tradingagents.agents.utils.agent_utils import (
    get_instrument_context_from_state,
    get_language_instruction,
)
from tradingagents.agents.utils.structured import (
    NO_EXTERNAL_TOOLS,
    bind_structured,
    invoke_structured_or_freetext,
)


def create_portfolio_manager(llm):
    structured_llm = bind_structured(llm, PortfolioDecision, "Portfolio Manager")

    def portfolio_manager_node(state) -> dict:
        instrument_context = get_instrument_context_from_state(state)

        history = state["risk_debate_state"]["history"]
        risk_debate_state = state["risk_debate_state"]
        research_plan = state["investment_plan"]
        trader_plan = state["trader_investment_plan"]

        past_context = state.get("past_context", "")
        lessons_line = (
            f"- Lessons from prior decisions and outcomes:\n{past_context}\n"
            if past_context
            else ""
        )
        account_state = state.get("account_state", "")
        account_line = (
            "**Current Account State + Raw Market Snapshot** — the caller's REAL account "
            "AND the raw tape (close, % move, volume, moving-average positions), delivered "
            "as the result of the `get_account_state` tool call in the message right after "
            "this prompt. Treat that tool result as the mandatory starting point of your "
            "decision. Every statement about holding / trimming / taking profit / adding "
            "must be grounded in it: your target_position_pct is the exposure to move *to* "
            "from this actual starting point, not an abstract ideal. Narratives that "
            "contradict the account (e.g. 'take profit on the existing position' while the "
            "tool result says FLAT) are hallucinations — the execution engine discards them "
            "and logs the day as a no-op, wasting the decision entirely. If flat, your only "
            "choices are: stay flat (Hold), or re-enter (Buy/Overweight with "
            "target_position_pct > 0). Re-entry does NOT require a perfect multi-signal "
            "confluence (price to X AND RSI<Y AND volume dry-up for Z days) — any ONE "
            "stabilization trigger (放量站回均线 / 缩量止跌 / 回踩支撑企稳 / 超卖反弹 / 突破回踩确认) "
            "is enough to buy back in. A stabilization trigger is an ALREADY-HAPPENED price "
            "action visible in the raw market snapshot below (e.g. 放量长阳 +10% 站回20日线): "
            "when the snapshot shows it, your right-side confirmation is COMPLETE — buy now, "
            "do not re-derive a new 'wait for more confirmation'. Only an extended multi-day "
            "run far above the MA with RSI overbought counts as 'chasing'; a first-day 放量 "
            "reclaim off a base is the entry, not a chase. If you choose Hold, state ONE "
            "concrete re-entry price/signal, not a checklist:\n"
            if account_state
            else ""
        )

        prompt = f"""As the Portfolio Manager, synthesize the risk analysts' debate and deliver the final trading decision.

{instrument_context}

{account_line}---

**Rating Scale** (use exactly one — anchored to the Current Account State, not a standalone stock view):
- **Buy**: strong conviction — build a meaningful position NOW (from flat) or add substantially (from a holding).
- **Overweight**: favorable — increase exposure (from flat: build a position; from a holding: add).
- **Hold**: keep the position as-is (or stay flat). No trade today.
- **Underweight**: reduce exposure toward a lower target (only valid while holding).
- **Sell**: exit the position (only valid while holding).
If the account is FLAT, Underweight/Sell are INVALID — your only choices are Buy (build) or Hold (stay flat).

**Execution Levels** (fill every field — the decision must be executable, not just directional):
- **target_position_pct**: the FINAL exposure (fraction of total equity, 0~1) you want THIS account to hold after today's action, anchored to the current position. It must agree with the rating: > current weight for Buy/Overweight, < current weight for Underweight/Sell, ≈ current weight for Hold. Write a single-stock exposure (e.g. 0.5~1.0 for a conviction entry), NOT a diversified-portfolio slice like 0.03 or 0.06. This is a fraction of TOTAL EQUITY only — the engine does the cash-vs-shares conversion for you (a Buy spends remaining CASH, a Sell sells held SHARES), so never express the target as a cash or share fraction.
- **stop_loss**: protective stop price — trim/exit if price breaks below it.
- **take_profit**: first take-profit target — take partial or full profits at this level.
- **risk_level**: low / medium / high, reflecting the current position/market risk.

**Behavioral & structure check** (weigh these explicitly alongside the debate — do not decide on indicators and fundamentals alone):
- Where is price relative to overhead supply (trapped longs) and support? A 放量长阳 that reclaims a moving average has already absorbed the nearby trapped longs — do NOT veto a stabilization entry with "上方套牢盘/追高" unless the price is actually still inside the pre-drop dense zone.
- Is the tape oversold-but-stabilizing, or still falling? "Stabilization" is an ALREADY-HAPPENED single price action: 放量站上均线 / 超跌后放量反包 / 长下影缩量止跌 / 站回5日10日线. The moment the raw market snapshot shows ONE of these, your right-side confirmation is COMPLETE — buy now. Stacking a second/third "confirmation" is how you miss the move: by the time it all lines up, a V-reversal is already +15%. A falling knife is a decline with NO such signal — that is the only thing "stabilization" is meant to filter out.
- Does the crowd read as fear or greed? Lean contrarian when one side is extreme, and name the crowd bias you are (or are not) fading.
- **Swing position, not single-day reaction**: place today's move within the recent trend. If the name is extended above its 20-day highs after a multi-day run, that is a trim/take-profit zone (sell into strength) — not a reason to panic-sell a normal pullback. If it has pulled back from the highs but the trend is intact, hold and watch; if the pullback stabilizes (缩量止跌 / reclaimed the moving average / rebounded off the low), that is an add-the-dip zone. Sell into strength, buy stabilised weakness — never "sell the dip and sell the rip" as a knee-jerk to each day.
- **Follow-through beats fresh opinions**: if a trigger from the standing plan (re-entry price, add level, stop, take-profit) fired today, EXECUTE it. Answering a fired trigger with a new "wait for confirmation" is analysis paralysis — "观望/待确认" may be used at most once per setup.
- **Protect open profits**: with a large unrealized gain, the plan must name a trailing take-profit line (e.g. highest close minus 8~10%); if the gain retraces by a third, trim. Riding a big gain back to zero (坐电梯) is the worst outcome on the menu.
- **Time stop**: a thesis that has not played out within its stated horizon loses money to opportunity cost — reduce or exit instead of re-rolling "wait" indefinitely. Every "再等等" needs a deadline.
- **Flat-book duty**: if the account is flat, the only real question is the re-entry — at what price/signal and what tranche size. When that setup appears, buy it; a good name never re-entered is a missed trade, not prudence. One stabilization signal (as defined above) is enough — do not demand a checklist, and do not re-label a fired entry as "chasing": a first-day 放量 reclaim off a base is the entry, not a chase. Only an extended multi-day run far above the MA with RSI overbought is 追高.

**Context:**
- Research Manager's investment plan: **{research_plan}**
- Trader's transaction proposal: **{trader_plan}**
{lessons_line}
**Risk Analysts Debate History:**
{history}

---

Be decisive and ground every conclusion in specific evidence from the analysts.

{NO_EXTERNAL_TOOLS}{get_language_instruction()}"""

        # Deliver the account snapshot as the RESULT of a get_account_state tool
        # call the engine already executed (the account is deterministic — no need
        # to burn an extra LLM round trip asking the model to query it). A tool
        # result is read as queried ground truth, unlike a prose block inside a
        # long prompt, which the model reliably skimmed past — the root of the
        # "flat book still treated as a full position" hallucination.
        if account_state:
            prompt = [
                SystemMessage(content=prompt),
                AIMessage(
                    content="",
                    tool_calls=[{
                        "name": "get_account_state",
                        "args": {},
                        "id": "call_get_account_state",
                        "type": "tool_call",
                    }],
                    # DeepSeek thinking mode requires a prior assistant turn's
                    # reasoning_content to be echoed back on the next request,
                    # otherwise it 400s ("The `reasoning_content` in the
                    # thinking mode must be passed back to the API"). The
                    # engine's get_account_state call is deterministic — no
                    # model thought went into it — so we attach a truthful
                    # one-liner for the query. DeepSeekChatOpenAI re-attaches
                    # this to the wire; other providers ignore the extra kwarg.
                    additional_kwargs={
                        "reasoning_content": (
                            "Query the live account snapshot (position, cash, "
                            "cost basis, weight) before sizing the trade."
                        )
                    },
                ),
                ToolMessage(content=account_state, tool_call_id="call_get_account_state"),
            ]

        final_trade_decision = invoke_structured_or_freetext(
            structured_llm,
            llm,
            prompt,
            render_pm_decision,
            "Portfolio Manager",
        )

        new_risk_debate_state = {
            "judge_decision": final_trade_decision,
            "history": risk_debate_state["history"],
            "aggressive_history": risk_debate_state["aggressive_history"],
            "conservative_history": risk_debate_state["conservative_history"],
            "neutral_history": risk_debate_state["neutral_history"],
            "latest_speaker": "Judge",
            "current_aggressive_response": risk_debate_state["current_aggressive_response"],
            "current_conservative_response": risk_debate_state["current_conservative_response"],
            "current_neutral_response": risk_debate_state["current_neutral_response"],
            "count": risk_debate_state["count"],
        }

        return {
            "risk_debate_state": new_risk_debate_state,
            "final_trade_decision": final_trade_decision,
        }

    return portfolio_manager_node
