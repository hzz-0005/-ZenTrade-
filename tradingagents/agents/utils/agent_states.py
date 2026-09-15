from typing import Annotated

from langgraph.graph import MessagesState
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict


def _merge_debate_state(
    a: dict | None, b: dict | None
) -> dict:
    """Merge two debate-state updates (LangGraph reducer).

    Kept semantically identical to last-write-wins for the sequential
    debate flow (each researcher builds its dict from the current state,
    so the newer dict already contains the older's fields). The extra
    handling only matters when researchers run in parallel
    (``pipeline_mode="parallel"``): each writes its own ``*_history`` key
    plus colliding ``history``/``count`` keys, which are joined/merged
    instead of raising an invalid-parallel-write error.
    """
    if a is None:
        return dict(b or {})
    if b is None:
        return dict(a)
    merged = {**a, **b}
    # Parallel researchers write the full dict from the same base state, so
    # fields they don't own come through as empty strings and would clobber
    # the other writer's content. Prefer the non-empty value for any key
    # where exactly one side contributed content (last-wins is unchanged
    # when both sides agree or both are non-empty).
    for key in merged:
        if not merged.get(key):
            av, bv = a.get(key), b.get(key)
            if av or bv:
                merged[key] = av or bv
    ha, hb = a.get("history"), b.get("history")
    if ha and hb and hb != ha:
        # containment => the newer update extends the older (sequential);
        # disjoint => two researchers spoke in the same step (parallel)
        if hb.startswith(ha):
            merged["history"] = hb
        elif ha.startswith(hb):
            merged["history"] = ha
        else:
            merged["history"] = ha.rstrip() + "\n\n" + hb
    if a.get("count") is not None and b.get("count") is not None:
        merged["count"] = max(a["count"], b["count"])
    return merged


# Researcher team state
class InvestDebateState(TypedDict):
    bull_history: Annotated[
        str, "Bullish Conversation history"
    ]  # Bullish Conversation history
    bear_history: Annotated[
        str, "Bearish Conversation history"
    ]  # Bullish Conversation history
    history: Annotated[str, "Conversation history"]  # Conversation history
    current_response: Annotated[str, "Latest response"]  # Last response
    judge_decision: Annotated[str, "Final judge decision"]  # Last response
    count: Annotated[int, "Length of the current conversation"]  # Conversation length


# Risk management team state
class RiskDebateState(TypedDict):
    aggressive_history: Annotated[
        str, "Aggressive Agent's Conversation history"
    ]  # Conversation history
    conservative_history: Annotated[
        str, "Conservative Agent's Conversation history"
    ]  # Conversation history
    neutral_history: Annotated[
        str, "Neutral Agent's Conversation history"
    ]  # Conversation history
    history: Annotated[str, "Conversation history"]  # Conversation history
    latest_speaker: Annotated[str, "Analyst that spoke last"]
    current_aggressive_response: Annotated[
        str, "Latest response by the aggressive analyst"
    ]  # Last response
    current_conservative_response: Annotated[
        str, "Latest response by the conservative analyst"
    ]  # Last response
    current_neutral_response: Annotated[
        str, "Latest response by the neutral analyst"
    ]  # Last response
    judge_decision: Annotated[str, "Judge's decision"]
    count: Annotated[int, "Length of the current conversation"]  # Conversation length


class AgentState(MessagesState):
    company_of_interest: Annotated[str, "Company that we are interested in trading"]
    asset_type: Annotated[str, "Asset type under analysis such as stock or crypto"]
    instrument_context: Annotated[str, "Deterministic ticker identity resolved at run start"]
    trade_date: Annotated[str, "What date we are trading at"]

    sender: Annotated[str, "Agent that sent this message"]

    # Per-analyst message channels. In ``pipeline_mode="parallel"`` the four
    # tool-using analysts run concurrently; if they shared the global
    # ``messages`` list, LangGraph's ToolNode (which executes the tool_calls of
    # the *last* AIMessage in the shared list) would orphan each other's
    # tool_call_id and the next request would be rejected with 400. Each
    # analyst therefore gets its own add_messages channel, and its ToolNode is
    # pointed at that channel via ``messages_key``. Downstream researchers read
    # the ``*_report`` fields, never these messages, so isolation is free.
    market_messages: Annotated[list, add_messages]
    social_messages: Annotated[list, add_messages]
    news_messages: Annotated[list, add_messages]
    fundamentals_messages: Annotated[list, add_messages]

    # research step
    market_report: Annotated[str, "Report from the Market Analyst"]
    sentiment_report: Annotated[str, "Report from the Sentiment Analyst"]
    news_report: Annotated[
        str, "Report from the News Researcher of current world affairs"
    ]
    fundamentals_report: Annotated[str, "Report from the Fundamentals Researcher"]

    # researcher team discussion step
    # NOTE: the reducer must be the ONLY Annotated metadata item. LangGraph
    # detects a reducer via `callable(meta[-1])`, so a trailing description
    # string silently disables it — the channel then degrades to LastValue and
    # two researchers writing in the same super-step raise InvalidUpdateError
    # ("At key 'investment_debate_state': Can receive only one value per step").
    # Keep the prose in this comment instead.
    investment_debate_state: Annotated[InvestDebateState, _merge_debate_state]

    investment_plan: Annotated[str, "Plan generated by the Analyst"]

    trader_investment_plan: Annotated[str, "Plan generated by the Trader"]

    # risk management team discussion step (same reducer caveat as above)
    risk_debate_state: Annotated[RiskDebateState, _merge_debate_state]
    final_trade_decision: Annotated[str, "Final decision made by the Risk Analysts"]
    past_context: Annotated[str, "Memory log context injected at run start (same-ticker decisions + cross-ticker lessons)"]
    # Caller's live account snapshot (position / cash / cost basis / weight),
    # injected by the webapp backtest so the Portfolio Manager sizes against the
    # REAL position. MUST be declared here: LangGraph drops any input key that
    # is not a schema channel, so without this field the injected account_state
    # never reaches the PM node (it silently reads "" and hallucinates a full
    # position every day).
    account_state: Annotated[str, "Live account snapshot injected by the caller"]
