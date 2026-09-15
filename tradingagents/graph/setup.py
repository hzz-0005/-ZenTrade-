# TradingAgents/graph/setup.py

from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.prebuilt import ToolNode

from tradingagents.agents import (
    create_aggressive_debator,
    create_bear_researcher,
    create_bull_researcher,
    create_conservative_debator,
    create_fundamentals_analyst,
    create_market_analyst,
    create_msg_delete,
    create_neutral_debator,
    create_news_analyst,
    create_portfolio_manager,
    create_research_manager,
    create_sentiment_analyst,
    create_trader,
)
from tradingagents.agents.utils.agent_states import AgentState
from tradingagents.agents.utils.agent_utils import bind_analyst_messages
from tradingagents.dataflows.config import get_config

from .analyst_execution import build_analyst_execution_plan
from .conditional_logic import ConditionalLogic

# Analyst report -> state key, for the report-cap wrapper below.
_REPORT_KEYS = {
    "market": "market_report",
    "social": "sentiment_report",
    "news": "news_report",
    "fundamentals": "fundamentals_report",
}

# Per-analyst message channel, matching AgentState and ConditionalLogic. Each
# analyst's tool loop lives in its own channel so parallel-mode analysts don't
# orphan each other's tool_calls in a shared messages list.
_MESSAGE_KEYS = {
    "market": "market_messages",
    "social": "social_messages",
    "news": "news_messages",
    "fundamentals": "fundamentals_messages",
}


def _cap_analyst_report(node, report_key: str, max_chars: int | None):
    """Wrap an analyst node so its report is truncated to ``max_chars``.

    The four analyst reports are re-embedded verbatim in every downstream
    prompt (bull/bear debate, research manager, trader, the three risk
    debaters, portfolio manager) — an unconstrained report multiplies
    through ~9 requests per pipeline run and has produced read timeouts on
    reasoning models with very large inputs. Capped at generation time, the
    truncated report is what downstream nodes see AND what lands in the
    saved state/audit; the truncation marker says so explicitly.
    """
    if not max_chars or max_chars <= 0:
        return node

    def wrapped(state) -> dict:
        out = node(state)
        report = out.get(report_key)
        if isinstance(report, str) and len(report) > max_chars:
            out = {
                **out,
                report_key: report[:max_chars] + "\n…[报告已截断以控制上下文长度]",
            }
        return out

    return wrapped

# Every target a shared conditional router can return. Each edge driven by the
# router maps all of them, so a fall-through return (e.g. under prompt/i18n/
# refactor drift in the speaker labels) can never hit a missing path_map entry
# and crash LangGraph mid-run (#1088).
DEBATE_PATH_MAP = {
    "Bull Researcher": "Bull Researcher",
    "Bear Researcher": "Bear Researcher",
    "Research Manager": "Research Manager",
}
RISK_ANALYSIS_PATH_MAP = {
    "Aggressive Analyst": "Aggressive Analyst",
    "Conservative Analyst": "Conservative Analyst",
    "Neutral Analyst": "Neutral Analyst",
    "Portfolio Manager": "Portfolio Manager",
}


class GraphSetup:
    """Handles the setup and configuration of the agent graph."""

    def __init__(
        self,
        quick_thinking_llm: Any,
        deep_thinking_llm: Any,
        tool_nodes: dict[str, ToolNode],
        conditional_logic: ConditionalLogic,
        pipeline_mode: str = "sequential",
    ):
        """Initialize with required components.

        ``pipeline_mode``: "sequential" runs analysts and debate rounds in the
        classic chained order; "parallel" fans the analysts out concurrently and
        replaces the multi-round debates with a single parallel pass (bull/bear —
        or the three risk analysts — speak once each, then the manager decides
        directly from both/all positions).
        """
        self.quick_thinking_llm = quick_thinking_llm
        self.deep_thinking_llm = deep_thinking_llm
        self.tool_nodes = tool_nodes
        self.conditional_logic = conditional_logic
        self.pipeline_mode = pipeline_mode

    def setup_graph(
        self, selected_analysts=("market", "social", "news", "fundamentals")
    ):
        """Set up and compile the agent workflow graph.

        Args:
            selected_analysts (list): List of analyst types to include. Options are:
                - "market": Market analyst
                - "social": Social media analyst
                - "news": News analyst
                - "fundamentals": Fundamentals analyst
        """
        plan = build_analyst_execution_plan(selected_analysts)

        analyst_factories = {
            "market": lambda: create_market_analyst(self.quick_thinking_llm),
            "social": lambda: create_sentiment_analyst(self.quick_thinking_llm),
            "news": lambda: create_news_analyst(self.quick_thinking_llm),
            "fundamentals": lambda: create_fundamentals_analyst(self.quick_thinking_llm),
        }

        # Create researcher and manager nodes
        bull_researcher_node = create_bull_researcher(self.quick_thinking_llm)
        bear_researcher_node = create_bear_researcher(self.quick_thinking_llm)
        research_manager_node = create_research_manager(self.deep_thinking_llm)
        trader_node = create_trader(self.quick_thinking_llm)

        # Create risk analysis nodes
        aggressive_analyst = create_aggressive_debator(self.quick_thinking_llm)
        neutral_analyst = create_neutral_debator(self.quick_thinking_llm)
        conservative_analyst = create_conservative_debator(self.quick_thinking_llm)
        portfolio_manager_node = create_portfolio_manager(self.deep_thinking_llm)

        # Create workflow
        workflow = StateGraph(AgentState)

        # Add analyst nodes to the graph. When analyst_report_max_chars is
        # set (config), wrap each analyst so its report is capped before it
        # enters state — the report is re-embedded verbatim by every
        # downstream prompt (debate/manager/trader/risk/PM).
        max_report_chars = get_config().get("analyst_report_max_chars")
        for spec in plan.specs:
            analyst_node = analyst_factories[spec.key]()
            # Bind the analyst to its own message channel. In parallel mode this
            # is what keeps the four concurrent tool loops isolated (each
            # ToolNode is already pointed at the same channel via messages_key).
            # In sequential mode it is harmless — each analyst just owns its
            # messages, and downstream nodes read the *_report fields instead.
            analyst_node = bind_analyst_messages(analyst_node, _MESSAGE_KEYS[spec.key])
            if spec.key in _REPORT_KEYS:
                analyst_node = _cap_analyst_report(
                    analyst_node, _REPORT_KEYS[spec.key], max_report_chars
                )
            workflow.add_node(spec.agent_node, analyst_node)
            workflow.add_node(
                spec.clear_node, create_msg_delete(messages_key=_MESSAGE_KEYS[spec.key])
            )
            workflow.add_node(spec.tool_node, self.tool_nodes[spec.key])

        # Add other nodes
        workflow.add_node("Bull Researcher", bull_researcher_node)
        workflow.add_node("Bear Researcher", bear_researcher_node)
        workflow.add_node("Research Manager", research_manager_node)
        workflow.add_node("Trader", trader_node)
        workflow.add_node("Aggressive Analyst", aggressive_analyst)
        workflow.add_node("Neutral Analyst", neutral_analyst)
        workflow.add_node("Conservative Analyst", conservative_analyst)
        workflow.add_node("Portfolio Manager", portfolio_manager_node)

        # Define edges
        parallel = self.pipeline_mode == "parallel"

        # Fan out: parallel mode starts every analyst in the same super-step
        # (they share nothing but the messages reducer, so concurrent writes
        # are conflict-free); sequential mode chains them as before.
        if parallel:
            for spec in plan.specs:
                workflow.add_edge(START, spec.agent_node)
        else:
            workflow.add_edge(START, plan.specs[0].agent_node)

        # Connect analysts in sequence
        for i, spec in enumerate(plan.specs):
            current_analyst = spec.agent_node
            current_tools = spec.tool_node
            current_clear = spec.clear_node

            # Add conditional edges for current analyst
            workflow.add_conditional_edges(
                current_analyst,
                getattr(self.conditional_logic, f"should_continue_{spec.key}"),
                [current_tools, current_clear],
            )
            workflow.add_edge(current_tools, current_analyst)

            # Connect to next analyst or to Bull Researcher if this is the last analyst
            if parallel:
                # Every analyst joins straight into the (parallel) researchers;
                # LangGraph waits for all incoming edges before starting them.
                workflow.add_edge(current_clear, "Bull Researcher")
                workflow.add_edge(current_clear, "Bear Researcher")
            elif i < len(plan.specs) - 1:
                workflow.add_edge(current_clear, plan.specs[i + 1].agent_node)
            else:
                workflow.add_edge(current_clear, "Bull Researcher")

        if parallel:
            # No debate rounds: bull and bear spoke once (concurrently, above);
            # the Research Manager decides directly from both positions, and
            # likewise the three risk analysts hand straight to the PM.
            workflow.add_edge("Bull Researcher", "Research Manager")
            workflow.add_edge("Bear Researcher", "Research Manager")
            workflow.add_edge("Research Manager", "Trader")
            workflow.add_edge("Trader", "Aggressive Analyst")
            workflow.add_edge("Trader", "Neutral Analyst")
            workflow.add_edge("Trader", "Conservative Analyst")
            workflow.add_edge("Aggressive Analyst", "Portfolio Manager")
            workflow.add_edge("Neutral Analyst", "Portfolio Manager")
            workflow.add_edge("Conservative Analyst", "Portfolio Manager")
        else:
            # Both research-debate edges share the complete DEBATE_PATH_MAP (#1088).
            for debate_node in ("Bull Researcher", "Bear Researcher"):
                workflow.add_conditional_edges(
                    debate_node,
                    self.conditional_logic.should_continue_debate,
                    DEBATE_PATH_MAP,
                )
            workflow.add_edge("Research Manager", "Trader")
            workflow.add_edge("Trader", "Aggressive Analyst")
            # All three risk edges share the complete RISK_ANALYSIS_PATH_MAP (#1088).
            for risk_node in ("Aggressive Analyst", "Conservative Analyst", "Neutral Analyst"):
                workflow.add_conditional_edges(
                    risk_node,
                    self.conditional_logic.should_continue_risk_analysis,
                    RISK_ANALYSIS_PATH_MAP,
                )

        workflow.add_edge("Portfolio Manager", END)

        return workflow
