"""Verify DeepSeek V4 per-role thinking depth end-to-end (offline, no API call).

1. Loads .env (import tradingagents triggers dotenv with usecwd=True).
2. Calls the REAL TradingAgentsGraph._deepseek_thinking_kwargs for both roles.
3. Builds clients through the real factory (tests the extra_body passthrough).
4. Also checks webapp get_llm: default = deep (high), gate override = low.
"""
from types import SimpleNamespace

from tradingagents.default_config import DEFAULT_CONFIG
from tradingagents.graph.trading_graph import TradingAgentsGraph
from tradingagents.llm_clients.factory import create_llm_client


def check(label, llm, want_effort, want_switch="enabled"):
    eb = getattr(llm, "extra_body", None) or {}
    effort = getattr(llm, "reasoning_effort", None)
    switch = (eb.get("thinking") or {}).get("type")
    print(f"[{label}] extra_body={eb} reasoning_effort={effort!r}")
    assert switch == want_switch, f"{label}: thinking switch {switch!r} != {want_switch!r}"
    if want_effort is not None:
        assert effort == want_effort, f"{label}: effort {effort!r} != {want_effort!r}"
    print(f"[{label}] OK")


def main():
    # --- graph path: real method, real factory ---
    stub = SimpleNamespace(config=DEFAULT_CONFIG)
    for role, want in (("deep", "high"), ("quick", "low")):
        kw = TradingAgentsGraph._deepseek_thinking_kwargs(stub, role)
        print(f"[graph/{role}] kwargs = {kw}")
        client = create_llm_client(
            "deepseek", DEFAULT_CONFIG["quick_think_llm"], None, **kw
        )
        check(f"graph/{role}", client.get_llm(), want)

    # --- webapp path ---
    from webapp.config import load_settings
    from webapp.llm import get_llm

    settings = load_settings()
    print(f"[webapp] provider={settings.llm_provider} model={settings.model}")
    check("webapp/decision", get_llm(settings), "high")
    check("webapp/gate", get_llm(settings, thinking_level="low"), "low")

    # --- off path: thinking disabled, no reasoning_effort ---
    kw = TradingAgentsGraph._deepseek_thinking_kwargs(
        SimpleNamespace(config={**DEFAULT_CONFIG,
                                "deepseek_quick_thinking_level": "off"}), "quick")
    client = create_llm_client("deepseek", "deepseek-v4-flash", None, **kw)
    check("graph/quick-off", client.get_llm(), None, want_switch="disabled")

    print("\nALL CHECKS PASSED")


if __name__ == "__main__":
    main()
