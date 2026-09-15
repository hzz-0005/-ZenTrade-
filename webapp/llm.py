"""Central LLM construction for the webapp.

Thinking-depth notes:
- DeepSeek V4 takes both a switch (``thinking: enabled/disabled``) and a
  strength (``reasoning_effort: low/high/max``). Thinking is ON by default, so
  we always send both explicitly. The webapp's own call IS the final decision,
  so it uses the *deep* level (high) — the cheap news-impact gate overrides it
  to low. See api-docs.deepseek.com/guides/thinking_mode.
- Zhipu's GLM-4.5+ models default to deep-thinking enabled, which adds tens of
  seconds per call; there the level comes from the GLM knob instead.
- A request timeout bounds the worst case: a hung call fails fast into the
  JSON repair ladder instead of stalling a session for minutes.
"""
from __future__ import annotations

from webapp.config import WebappSettings

_GLM_PROVIDERS = ("glm", "glm-cn")
_DEEPSEEK_PROVIDERS = ("deepseek",)


def get_llm(
    settings: WebappSettings,
    *,
    timeout: int = 60,
    temperature: float = 0.0,
    thinking_level: str | None = None,
):
    """Build a chat client for one provider.

    ``thinking_level`` overrides the provider's default thinking depth for this
    one call (e.g. the cheap news gate asks for "low" while the trade decision
    uses the configured "deep" level). When ``None`` the role default applies:
    the "deep" level for DeepSeek, the GLM knob for Zhipu.
    """
    from tradingagents.default_config import DEFAULT_CONFIG
    from tradingagents.llm_clients.factory import create_llm_client

    provider = settings.llm_provider.lower()
    kwargs: dict = {"timeout": timeout, "temperature": temperature}

    if provider in _DEEPSEEK_PROVIDERS:
        # This is DeepSeek V4: thinking is a switch (enabled/disabled) plus a
        # strength (reasoning_effort low/high/max). Both are sent explicitly so
        # we never inherit the provider default. See
        # api-docs.deepseek.com/guides/thinking_mode.
        if thinking_level is None:
            thinking_level = str(
                DEFAULT_CONFIG.get("deepseek_deep_thinking_level") or "high"
            ).strip().lower()
        level = thinking_level
        if level in ("off", "disabled", "none"):
            kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
        else:
            kwargs["extra_body"] = {"thinking": {"type": "enabled"}}
            kwargs["reasoning_effort"] = level

    client = create_llm_client(
        settings.llm_provider, settings.model, settings.backend_url, **kwargs
    )
    llm = client.get_llm()

    if provider in _GLM_PROVIDERS:
        try:
            import os

            # Deep thinking by default for GLM-4.5+ reasoning models; flash
            # variants default to low to keep latency reasonable. Override via
            # TRADINGAGENTS_GLM_THINKING_LEVEL=low/max/off/high, or the
            # thinking_level argument for a one-off (e.g. the news gate).
            default_level = "low" if "flash" in settings.model.lower() else "high"
            if thinking_level is not None:
                level = thinking_level
            else:
                level = os.environ.get(
                    "TRADINGAGENTS_GLM_THINKING_LEVEL", default_level
                ).strip().lower()
            extra = dict(getattr(llm, "extra_body", None) or {})
            if level in ("off", "disabled"):
                # Explicitly disable thinking — omitting the param would leave
                # the provider default, which for GLM-4.5+ is thinking ON.
                extra["thinking"] = {"type": "disabled"}
            elif level in ("low", "high", "max"):
                extra["thinking"] = {"type": "enabled", "level": level}
            else:
                extra["thinking"] = {"type": level}
            if extra:
                llm.extra_body = extra
        except Exception:
            pass  # non-fatal: slower calls, same correctness
    return llm


def _get_llm(settings: WebappSettings):
    return get_llm(settings)
