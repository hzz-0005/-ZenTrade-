"""Daily decision agent: one structured LLM call per day.

Robustness ladder (ordered for speed on thinking-mode models — a plain
JSON prompt is several times faster than function-calling structured
output on GLM, so it goes first):
  1. plain-prompt JSON + Pydantic parse, with up to 2 repair retries
  2. llm.with_structured_output(Decision)
  3. fallback: hold with a parse-failure flag — never fabricate a trade
"""
from __future__ import annotations

import json
import logging

from pydantic import ValidationError

from webapp.config import WebappSettings
from webapp.core.errors import LLMBudgetExceeded
from webapp.core.models import Decision
from webapp.engine.context_builder import DailyContext, closes_from_csv

logger = logging.getLogger(__name__)

_REPAIR_PROMPT = (
    "你上一次的输出不是符合要求的 JSON。错误信息：{error}\n"
    "请重新输出：严格只有一个 JSON 对象，字段为 action(buy|sell|hold)、"
    "position_pct(0~1)、confidence(0~1)、reasoning、key_signals(数组)、used_skills(数组)。"
    "还必须包含 recheck_days(1~10)、recheck_upper、recheck_lower、stop_loss、"
    "take_profit、target_position_pct；没有可靠止损/止盈时对应字段填 null。"
    "不要输出任何解释、markdown 代码块标记或其他文字。"
)


class DecisionAgent:
    def __init__(self, llm, settings: WebappSettings):
        self.llm = llm
        self.settings = settings
        self._call_count = 0

    def reset_day(self) -> None:
        self._call_count = 0

    def _spend(self) -> None:
        self._call_count += 1
        if self._call_count > self.settings.max_llm_calls_per_day:
            raise LLMBudgetExceeded(
                f"exceeded {self.settings.max_llm_calls_per_day} LLM calls for this day"
            )

    def decide(self, ctx: DailyContext) -> tuple[Decision, str, str, list[str]]:
        """Returns (decision, prompt_text, response_text, flags).

        flags carries degradation markers like 'decision: parse-failure fallback'.
        """
        from webapp.prompts import loader

        if getattr(self.settings, "agent_mode", "panel") == "single":
            system_text = loader.decision_system()
        else:
            system_text = loader.decision_system_panel()
        flags: list[str] = []

        try:
            decision, prompt_text, response_text, flags = self._ladder(
                ctx, system_text, drop_macro=False, flags=flags
            )
        except Exception as exc:
            if not (_is_content_filter(exc) and _has_macro(ctx.news)):
                raise
            # Providers (GLM: error 1301) reject prompts carrying political
            # text. CCTV headlines are the usual trigger — drop just those and
            # retry once instead of losing the session.
            logger.warning("content filter rejected the prompt; retrying without macro news")
            flags.append("decision: 内容安全拦截，已剔除宏观新闻后重试")
            decision, prompt_text, response_text, flags = self._ladder(
                ctx, system_text, drop_macro=True, flags=flags
            )
        return self.reconcile(decision, ctx, flags), prompt_text, response_text, flags

    @staticmethod
    def reconcile(decision: Decision, ctx: DailyContext, flags: list[str]) -> Decision:
        """Engine-side position reconciliation: the account is ground truth.

        The model decides from text and can hallucinate a holding it no longer
        has (or cash it already spent). Fix decisions that contradict the real
        account, and stamp every decision with the actual position it was made
        from — a "hold" must say *which* position is being held.
        """
        p = ctx.portfolio or {}
        cash = float(p.get("cash") or 0.0)
        shares = float(p.get("shares") or 0.0)
        closes = closes_from_csv(ctx.ohlcv_tail_csv)
        close = closes[-1] if closes else None
        if close:
            pos_value = shares * close
            equity = cash + pos_value
        else:
            equity = float(p.get("equity") or 0.0)
            pos_value = max(0.0, equity - cash)
        weight = pos_value / equity if equity > 0 else 0.0

        # LLM output is untrusted even after schema validation. Keep only skill
        # ids that were actually supplied, bound persisted text, and reject
        # price levels that contradict the observed close.
        allowed_skills = {str(s.get("id")) for s in ctx.skills if s.get("id")}
        requested_skills = [str(s) for s in decision.used_skills]
        decision.used_skills = [s for s in requested_skills if s in allowed_skills]
        if len(decision.used_skills) != len(requested_skills):
            flags.append("decision: 丢弃未提供的 skill id")
        decision.reasoning = str(decision.reasoning or "")[:2000]
        decision.key_signals = [str(s)[:240] for s in decision.key_signals[:8]]

        if close:
            if decision.stop_loss is not None and decision.stop_loss >= close:
                decision.stop_loss = None
                flags.append("decision: 止损价不低于现价，已丢弃")
            if decision.take_profit is not None and decision.take_profit <= close:
                decision.take_profit = None
                flags.append("decision: 止盈价不高于现价，已丢弃")
            lower, upper = decision.recheck_lower, decision.recheck_upper
            if ((lower is not None or upper is not None)
                    and not (lower and upper and lower > 0 and lower <= close <= upper)):
                decision.recheck_lower = None
                decision.recheck_upper = None
                flags.append("decision: 复查区间未包含现价，改用引擎默认区间")

        apply_market_trade_guard(decision, ctx, flags)

        # target_position_pct and position_pct are two views of one action.
        # Derive the former from the executable fraction so contradictory LLM
        # fields cannot poison subsequent-day plans.
        if equity > 0:
            if decision.action == "buy":
                expected_target = min(1.0, (pos_value + cash * decision.position_pct) / equity)
            elif decision.action == "sell":
                expected_target = max(0.0, pos_value * (1 - decision.position_pct) / equity)
            else:
                expected_target = weight
            if (decision.target_position_pct is not None
                    and abs(decision.target_position_pct - expected_target) > 0.02):
                flags.append("decision: 目标仓位与动作比例冲突，已按账户重新计算")
            decision.target_position_pct = round(expected_target, 4)

        prefix = ""
        if decision.action == "sell" and weight <= 0.005:
            flags.append("decision: 当前空仓，sell 无持仓可执行，降级为 hold")
            prefix = ("【仓位核对】当前空仓（无持仓），卖出指令无持仓可执行，"
                      "实际执行=维持空仓。\n")
            decision.action = "hold"
            decision.position_pct = 0.0
        elif decision.action == "buy" and cash <= 0:
            flags.append("decision: 可用现金为 0，buy 无法执行，降级为 hold")
            prefix = ("【仓位核对】当前无可用现金，买入指令无法执行，"
                      "实际执行=维持仓位。\n")
            decision.action = "hold"
            decision.position_pct = 0.0

        basis = (
            f"【仓位基准】决策前实际仓位 {weight:.0%}（市值 ¥{pos_value:,.0f}），"
            f"现金 ¥{cash:,.0f}"
        )
        decision.reasoning = prefix + basis + "\n" + (decision.reasoning or "")
        return decision

    # Compatibility for callers/tests that used the former private name.
    _reconcile = reconcile

    def _ladder(self, ctx: DailyContext, system_text: str, drop_macro: bool,
                flags: list[str]) -> tuple[Decision, str, str, list[str]]:
        """JSON-repair ladder -> structured output -> hold fallback."""
        user_text = ctx.to_user_message(drop_macro=drop_macro)
        prompt_text = f"<system>\n{system_text}\n</system>\n<user>\n{user_text}\n</user>"

        # 1) plain-prompt JSON ladder (fast path)
        # Each ladder run gets its own call budget: a retry after a content
        # filter rejection is a fresh attempt, not a continuation.
        self._call_count = 0
        messages = [("system", system_text), ("human", user_text)]
        last_error = ""
        for attempt in range(3):  # first try + 2 repairs
            self._spend()
            if attempt > 0:
                messages = messages + [("human", _REPAIR_PROMPT.format(error=last_error))]
            response = self.llm.invoke(messages)
            text = _extract_text(response)
            decision_or_err = _parse_decision(text)
            if isinstance(decision_or_err, Decision):
                return decision_or_err, prompt_text, text, flags
            last_error = decision_or_err

        # 2) structured output fallback
        try:
            self._spend()
            logger.info("json ladder failed (%s); trying with_structured_output", last_error)
            chain = self.llm.with_structured_output(Decision)
            result = chain.invoke([
                ("system", system_text),
                ("human", user_text + "\n\n（前几次 JSON 输出无效，请严格只输出合法 JSON。）"),
            ])
            if isinstance(result, Decision):
                return result, prompt_text, result.model_dump_json(), flags
            decision = Decision.model_validate(result)
            return decision, prompt_text, json.dumps(decision.model_dump(), ensure_ascii=False), flags
        except LLMBudgetExceeded:
            raise
        except Exception as exc:
            logger.info("structured_output fallback failed: %s", exc)
            last_error = f"{last_error}; structured fallback: {exc}"

        # 3) fallback hold
        flags.append("decision: parse-failure fallback (hold)")
        return (
            Decision(action="hold", confidence=0.0, reasoning=f"模型输出解析失败：{last_error}"),
            prompt_text,
            last_error,
            flags,
        )


def _has_macro(news: list[dict] | None) -> bool:
    return any(n.get("kind") == "macro" for n in (news or []))


def apply_market_trade_guard(decision: Decision, ctx: DailyContext, flags: list[str]) -> Decision:
    """Block discretionary A-share chasing/panic-selling at the engine boundary.

    ``DecisionAgent`` applies this while reconciling its own output, but the
    full graph and the graph-backed hybrid agent return ``Decision`` objects
    directly.  The backtest engine calls this same idempotent guard immediately
    before execution, making the rule architecture-independent.  Deterministic
    hard risk exits bypass this function by design.
    """
    cn_move = _cn_session_extreme_move(ctx.ohlcv_tail_csv) if ctx.market == "cn" else None
    blocked = False
    if cn_move is not None and decision.action == "buy" and cn_move >= 0.07:
        decision.action = "hold"
        decision.position_pct = 0.0
        flags.append("decision: A股当日涨幅/盘中冲高≥7%，禁止追涨，改为 hold")
        blocked = True
    elif (cn_move is not None and decision.action == "sell"
          and 0 < decision.position_pct < 1.0 and cn_move <= -0.07):
        decision.action = "hold"
        decision.position_pct = 0.0
        flags.append("decision: A股当日跌幅/盘中急跌≤-7%，禁止杀跌式部分卖出，改为 hold")
        blocked = True

    if blocked:
        closes = closes_from_csv(ctx.ohlcv_tail_csv)
        close = closes[-1] if closes else None
        portfolio = ctx.portfolio or {}
        cash = float(portfolio.get("cash") or 0.0)
        shares = float(portfolio.get("shares") or 0.0)
        equity = cash + shares * close if close else float(portfolio.get("equity") or 0.0)
        decision.target_position_pct = round(shares * close / equity, 4) if close and equity > 0 else 0.0
    return decision


def _cn_session_extreme_move(csv: str) -> float | None:
    """Return the largest absolute intraday/gap move versus yesterday's close.

    CSV is generated by the engine, but parsing defensively keeps this guard
    safe for API/tests that construct ``DailyContext`` themselves.
    """
    bars: list[tuple[float, float, float, float]] = []
    for line in (csv or "").splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 5:
            continue
        try:
            bars.append((float(parts[1]), float(parts[2]), float(parts[3]), float(parts[4])))
        except ValueError:
            continue
    if len(bars) < 2 or bars[-2][3] <= 0:
        return None
    previous_close = bars[-2][3]
    open_, high, low, close = bars[-1]
    if close >= previous_close:
        return max(open_, high, close) / previous_close - 1
    return min(open_, low, close) / previous_close - 1


def _is_content_filter(exc: Exception) -> bool:
    """True for provider-side moderation rejections (GLM error 1301).

    These abort the request before any token is generated, so they are worth
    distinguishing from transport/parse errors: the prompt can be salvaged by
    removing the offending content.
    """
    text = f"{type(exc).__name__}: {exc}".lower().replace(" ", "")
    return any(
        marker in text
        for marker in ("contentfilter", "content_filter", "1301", "不安全或敏感", "敏感内容")
    )


def _extract_text(response) -> str:
    content = getattr(response, "content", response)
    if isinstance(content, list):
        return "\n".join(
            item.get("text", "") if isinstance(item, dict) and item.get("type") == "text"
            else item if isinstance(item, str) else ""
            for item in content
        )
    return str(content or "")


def _parse_decision(text: str) -> Decision | str:
    """Parse a Decision out of LLM text; returns an error string on failure.

    Panel mode asks the model to reason through several analyst roles before
    emitting one final JSON. Thinking models sometimes leak per-role JSON
    blocks, so slicing "first { to last }" would glue them together and fail.
    Instead every balanced object is decoded and the *last* one that validates
    is used — the trader's synthesis is the final block by construction.
    """
    if not text:
        return "empty response"
    cleaned = text.strip()
    # tolerate markdown fences
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()
    if "{" not in cleaned:
        return f"no JSON object found in: {text[:200]}"

    decoder = json.JSONDecoder()
    errors: list[str] = []
    starts = [i for i, ch in enumerate(cleaned) if ch == "{"]
    for start in reversed(starts):  # last valid object wins
        try:
            payload, _ = decoder.raw_decode(cleaned[start:])
        except json.JSONDecodeError as exc:
            errors.append(f"@{start}: {exc}")
            continue
        if not isinstance(payload, dict):
            errors.append(f"@{start}: not an object")
            continue
        try:
            return Decision.model_validate(payload)
        except ValidationError as exc:
            errors.append(f"@{start}: {exc}")
            continue
    detail = errors[-1] if errors else "unknown"
    return f"invalid decision JSON: {detail}"
