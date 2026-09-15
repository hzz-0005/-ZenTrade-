import os
from pathlib import Path

import questionary
from dotenv import find_dotenv, set_key
from rich.console import Console

from cli.models import AnalystType, AssetType
from cli.i18n import t
from tradingagents.llm_clients.api_key_env import get_api_key_env
from tradingagents.llm_clients.model_catalog import get_model_options

console = Console()

TICKER_INPUT_EXAMPLES = "SPY, 0700.HK, BTC-USD"

ANALYST_ORDER = [
    ("Market Analyst", AnalystType.MARKET),
    ("Sentiment Analyst", AnalystType.SOCIAL),
    ("News Analyst", AnalystType.NEWS),
    ("Fundamentals Analyst", AnalystType.FUNDAMENTALS),
]

CRYPTO_SUFFIXES = ("-USD", "-USDT", "-USDC", "-BTC", "-ETH")


def is_valid_ticker_input(value: str) -> bool:
    """Whether a ticker entry is acceptable (charset + length).

    Allows the characters Yahoo symbols use, including ``=`` for futures/forex
    like ``GC=F`` and ``EURUSD=X`` (#980), and ``^`` for indices. Empty input is
    allowed (it defaults to SPY downstream).

    ASCII-only: ``str.isalnum()`` accepts CJK characters, so a Chinese company
    name like ``茅台`` used to pass validation and later crash the data layer
    (non-ASCII in a request URL raises UnicodeEncodeError). Data sources are
    Yahoo-symbol based, so non-ASCII input can never be priced anyway.
    """
    v = value.strip()
    return not v or (
        v.isascii()
        and all(ch.isalnum() or ch in "._-^=" for ch in v)
        and len(v) <= 32
    )


def get_ticker() -> str:
    """Prompt the user to enter a ticker symbol, preserving exchange suffixes.

    Uses questionary.text (not typer.prompt, which strips trailing dot-suffixes
    like ``000404.SH`` on some shells) and validates the symbol charset so an
    obvious typo is caught before the run starts.
    """
    ticker = questionary.text(
        t("Enter ticker symbol (e.g. {examples}):", examples=TICKER_INPUT_EXAMPLES),
        validate=lambda x: (
            is_valid_ticker_input(x)
            or t("Please enter a valid ticker symbol, e.g. AAPL, 000404.SZ, 0700.HK, GC=F.")
        ),
        style=questionary.Style(
            [
                ("text", "fg:green"),
                ("highlighted", "noinherit"),
            ]
        ),
    ).ask()

    if ticker is None:
        console.print(f"\n[red]{t('No ticker symbol provided. Exiting...')}[/red]")
        exit(1)

    return normalize_ticker_symbol(ticker) if ticker.strip() else "SPY"


def normalize_ticker_symbol(ticker: str) -> str:
    """Resolve user input to its canonical Yahoo symbol (single source of truth).

    Delegates to the data layer's ``normalize_symbol`` so the symbol the CLI
    passes through the pipeline is exactly the one the data path will price
    (e.g. ``BTCUSD`` -> ``BTC-USD``, ``XAUUSD`` -> ``GC=F``). Falls back to the
    plain upper-case if the data layer is unavailable.
    """
    try:
        from tradingagents.dataflows.symbol_utils import normalize_symbol

        return normalize_symbol(ticker)
    except Exception:
        return ticker.strip().upper()


def detect_asset_type(ticker: str) -> AssetType:
    """Classify on the canonical symbol so e.g. BTCUSD and BTC-USDT both read as
    crypto (#981/#982), matching what the data path will actually fetch."""
    canonical = normalize_ticker_symbol(ticker)
    if canonical.endswith(CRYPTO_SUFFIXES):
        return AssetType.CRYPTO
    return AssetType.STOCK


def filter_analysts_for_asset_type(
    analysts: list[AnalystType], asset_type: AssetType
) -> list[AnalystType]:
    if asset_type != AssetType.CRYPTO:
        return analysts
    return [
        analyst
        for analyst in analysts
        if analyst != AnalystType.FUNDAMENTALS
    ]


def get_analysis_date() -> str:
    """Prompt the user to enter a date in YYYY-MM-DD format."""
    import re
    from datetime import datetime

    def validate_date(date_str: str) -> bool:
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", date_str):
            return False
        try:
            datetime.strptime(date_str, "%Y-%m-%d")
            return True
        except ValueError:
            return False

    date = questionary.text(
        t("Enter the analysis date (YYYY-MM-DD):"),
        validate=lambda x: validate_date(x.strip())
        or t("Please enter a valid date in YYYY-MM-DD format."),
        style=questionary.Style(
            [
                ("text", "fg:green"),
                ("highlighted", "noinherit"),
            ]
        ),
    ).ask()

    if not date:
        console.print(f"\n[red]{t('No date provided. Exiting...')}[/red]")
        exit(1)

    return date.strip()


def select_analysts(asset_type: AssetType = AssetType.STOCK) -> list[AnalystType]:
    """Select analysts using an interactive checkbox."""
    available_analysts = filter_analysts_for_asset_type(
        [value for _, value in ANALYST_ORDER],
        asset_type,
    )
    choices = questionary.checkbox(
        t("Select Your [Analysts Team]:"),
        choices=[
            questionary.Choice(t(display), value=value)
            for display, value in ANALYST_ORDER
            if value in available_analysts
        ],
        instruction=t("\n- Press Space to select/unselect analysts\n- Press 'a' to select/unselect all\n- Press Enter when done"),
        validate=lambda x: len(x) > 0 or t("You must select at least one analyst."),
        style=questionary.Style(
            [
                ("checkbox-selected", "fg:green"),
                ("selected", "fg:green noinherit"),
                ("highlighted", "noinherit"),
                ("pointer", "noinherit"),
            ]
        ),
    ).ask()

    if not choices:
        console.print(f"\n[red]{t('No analysts selected. Exiting...')}[/red]")
        exit(1)

    return choices


def select_research_depth() -> int:
    """Select research depth using an interactive selection."""

    # Define research depth options with their corresponding values
    DEPTH_OPTIONS = [
        ("Shallow - Quick research, few debate and strategy discussion rounds", 1),
        ("Medium - Middle ground, moderate debate rounds and strategy discussion", 3),
        ("Deep - Comprehensive research, in depth debate and strategy discussion", 5),
    ]

    choice = questionary.select(
        t("Select Your [Research Depth]:"),
        choices=[
            questionary.Choice(t(display), value=value) for display, value in DEPTH_OPTIONS
        ],
        instruction=t("\n- Use arrow keys to navigate\n- Press Enter to select"),
        style=questionary.Style(
            [
                ("selected", "fg:yellow noinherit"),
                ("highlighted", "fg:yellow noinherit"),
                ("pointer", "fg:yellow noinherit"),
            ]
        ),
    ).ask()

    if choice is None:
        console.print(f"\n[red]{t('No research depth selected. Exiting...')}[/red]")
        exit(1)

    return choice


# Mainstream OpenRouter chat-LLM provider namespaces. We surface the newest
# models from these rather than the universal-newest, which is dominated by
# niche/experimental releases. These are the general-purpose chat providers;
# more enterprise/specialised namespaces (nvidia, cohere, amazon, ...) tend to
# ship research/safety variants as their newest, so they're left out of the
# shortlist. Provider names are stable (unlike model IDs), so this rarely needs
# touching; anything not here is still reachable via Custom ID.
_OPENROUTER_MAINSTREAM = {
    "openai", "anthropic", "google", "deepseek", "qwen", "mistralai",
    "meta-llama", "x-ai", "z-ai", "minimax", "moonshotai",
}


def _fetch_openrouter_models() -> list[tuple[str, str]]:
    """Fetch available models from the OpenRouter API."""
    import requests
    try:
        resp = requests.get("https://openrouter.ai/api/v1/models", timeout=10)
        resp.raise_for_status()
        models = resp.json().get("data", [])
        # Newest first so the top-N shown really is the latest available — the
        # API currently returns this order, but sort explicitly so the prompt's
        # "latest available" label holds regardless of response ordering.
        models.sort(key=lambda m: m.get("created") or 0, reverse=True)
        return [(m.get("name") or m["id"], m["id"]) for m in models]
    except Exception as e:
        console.print(f"\n[yellow]Could not fetch OpenRouter models: {e}[/yellow]")
        return []


def _require_text(message: str, hint: str) -> str:
    """Prompt for a required value; exit cleanly if the user cancels.

    ``questionary.text(...).ask()`` returns None on Ctrl-C/Esc; mirror the
    exit-on-cancel behavior of the other required selections so a cancelled
    prompt never returns an empty model/deployment that would fail downstream.
    """
    response = questionary.text(
        message,
        validate=lambda x: len(x.strip()) > 0 or hint,
    ).ask()
    if response is None:
        console.print(f"\n[red]{t('Cancelled. Exiting...')}[/red]")
        exit(1)
    return response.strip()


def select_openrouter_model(mode: str) -> str:
    """Select an OpenRouter model from the newest available, or enter a custom ID.

    ``mode`` ("quick"/"deep") labels the prompt so the two consecutive
    OpenRouter selections are distinguishable, like the other providers (#1000).
    """
    models = _fetch_openrouter_models()  # newest first
    # Prefer the newest from mainstream providers so the shortlist isn't crowded
    # out by niche/experimental releases; fall back to all if none match.
    mainstream = [
        (name, mid) for name, mid in models
        if not mid.startswith("~")  # skip variant/alias duplicate routes
        and mid.split("/", 1)[0] in _OPENROUTER_MAINSTREAM
    ]
    top = (mainstream or models)[:5]

    choices = [questionary.Choice(name, value=mid) for name, mid in top]
    choices.append(questionary.Choice(t("Custom model ID"), value="custom"))

    localized_prompt = t(
        "Select Your [{mode}-Thinking] OpenRouter Model (latest available):",
        mode=t(mode.title()),
    )
    choice = questionary.select(
        f"{localized_prompt} ({mode.title()}-Thinking)",
        choices=choices,
        instruction=t("\n- Use arrow keys to navigate\n- Press Enter to select"),
        style=questionary.Style([
            ("selected", "fg:magenta noinherit"),
            ("highlighted", "fg:magenta noinherit"),
            ("pointer", "fg:magenta noinherit"),
        ]),
    ).ask()

    if choice is None:
        console.print(f"\n[red]{t('No model selected. Exiting...')}[/red]")
        exit(1)
    if choice == "custom":
        return _require_text(
            t("Enter OpenRouter model ID (e.g. google/gemma-4-26b-a4b-it):"),
            t("Please enter a model ID."),
        )
    return choice


def _prompt_custom_model_id() -> str:
    """Prompt user to type a custom model ID."""
    return _require_text(t("Enter model ID:"), t("Please enter a model ID."))


def _select_model(provider: str, mode: str) -> str:
    """Select a model for the given provider and mode (quick/deep)."""
    if provider.lower() == "openrouter":
        return select_openrouter_model(mode)

    if provider.lower() == "azure":
        return _require_text(
            t("Enter Azure deployment name ({mode}-thinking):", mode=t(mode.title())),
            t("Please enter a deployment name."),
        )

    choice = questionary.select(
        t("Select Your [{mode}-Thinking LLM Engine]:", mode=t(mode.title())),
        choices=[
            questionary.Choice(t(display), value=value)
            for display, value in get_model_options(provider, mode)
        ],
        instruction=t("\n- Use arrow keys to navigate\n- Press Enter to select"),
        style=questionary.Style(
            [
                ("selected", "fg:magenta noinherit"),
                ("highlighted", "fg:magenta noinherit"),
                ("pointer", "fg:magenta noinherit"),
            ]
        ),
    ).ask()

    if choice is None:
        console.print(f"\n[red]{t('No {mode} thinking llm engine selected. Exiting...', mode=t(mode.title()))}[/red]")
        exit(1)

    if choice == "custom":
        return _prompt_custom_model_id()

    return choice


def select_shallow_thinking_agent(provider) -> str:
    """Select shallow thinking llm engine using an interactive selection."""
    return _select_model(provider, "quick")


def select_deep_thinking_agent(provider) -> str:
    """Select deep thinking llm engine using an interactive selection."""
    return _select_model(provider, "deep")

def _llm_provider_table() -> list[tuple[str, str, str | None]]:
    """(display_name, provider_key, base_url) for every supported provider.

    Shared by the interactive picker and by env-driven configuration so an
    env-set provider resolves to the same default endpoint the menu uses.
    Ollama users can point at a remote ollama-serve via OLLAMA_BASE_URL
    (convention from the broader Ollama ecosystem); falls back to the
    localhost default when unset.
    """
    ollama_url = os.environ.get("OLLAMA_BASE_URL") or "http://localhost:11434/v1"
    return [
        ("OpenAI", "openai", "https://api.openai.com/v1"),
        ("Google", "google", None),
        ("Anthropic", "anthropic", "https://api.anthropic.com/"),
        ("xAI", "xai", "https://api.x.ai/v1"),
        ("DeepSeek", "deepseek", "https://api.deepseek.com"),
        ("Qwen", "qwen", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"),
        ("GLM", "glm", "https://open.bigmodel.cn/api/paas/v4/"),
        ("MiniMax", "minimax", "https://api.minimax.io/v1"),
        ("OpenRouter", "openrouter", "https://openrouter.ai/api/v1"),
        ("Mistral", "mistral", "https://api.mistral.ai/v1"),
        ("Kimi (Moonshot)", "kimi", "https://api.moonshot.ai/v1"),
        ("Groq", "groq", "https://api.groq.com/openai/v1"),
        ("NVIDIA NIM", "nvidia", "https://integrate.api.nvidia.com/v1"),
        ("Azure OpenAI", "azure", None),
        ("Amazon Bedrock", "bedrock", None),
        ("Ollama", "ollama", ollama_url),
        ("OpenAI-compatible (vLLM, LM Studio, llama.cpp, custom relay)", "openai_compatible", None),
    ]


def provider_default_url(provider_key: str) -> str | None:
    """Return the default backend URL for a provider key, or None if unknown."""
    key = provider_key.lower()
    for _, pk, url in _llm_provider_table():
        if pk == key:
            return url
    return None


def resolve_backend_url(
    provider: str, menu_url: str | None = None, env_url: str | None = None
) -> str | None:
    """Resolve the backend URL with the correct precedence.

    An explicit env override (``env_url``, from ``TRADINGAGENTS_LLM_BACKEND_URL``
    via ``DEFAULT_CONFIG['backend_url']``) is honored regardless of how the
    provider was chosen — interactively or from the environment (#978).
    Otherwise the menu/region URL, then the provider's default.
    """
    return env_url or menu_url or provider_default_url(provider)


def prompt_openai_compatible_url() -> str:
    """Prompt for a custom OpenAI-compatible endpoint base URL."""
    url = questionary.text(
        t("Enter the OpenAI-compatible base URL "
        "(e.g. http://localhost:8000/v1 for vLLM, http://localhost:1234/v1 for LM Studio):"),
        validate=lambda x: x.strip().startswith(("http://", "https://"))
        or t("Enter a URL starting with http:// or https://"),
    ).ask()
    if not url:
        console.print(f"\n[red]{t('No endpoint URL provided. Exiting...')}[/red]")
        exit(1)
    return url.strip()


def select_llm_provider() -> tuple[str, str | None]:
    """Select the LLM provider and its API endpoint."""
    PROVIDERS = _llm_provider_table()

    choice = questionary.select(
        t("Select your LLM provider"),
        choices=[
            questionary.Choice(t(display), value=(provider_key, url))
            for display, provider_key, url in PROVIDERS
        ],
        instruction=t("\n- Use arrow keys to navigate\n- Press Enter to select"),
        style=questionary.Style(
            [
                ("selected", "fg:magenta noinherit"),
                ("highlighted", "fg:magenta noinherit"),
                ("pointer", "fg:magenta noinherit"),
            ]
        ),
    ).ask()

    if choice is None:
        console.print(f"\n[red]{t('No LLM provider selected. Exiting...')}[/red]")
        exit(1)

    provider, url = choice
    return provider, url


def ask_openai_reasoning_effort() -> str:
    """Ask for OpenAI reasoning effort level."""
    choices = [
        questionary.Choice(t("Medium (Default)"), "medium"),
        questionary.Choice(t("High (More thorough)"), "high"),
        questionary.Choice(t("Low (Faster)"), "low"),
    ]
    return questionary.select(
        t("Select Reasoning Effort:"),
        choices=choices,
        style=questionary.Style([
            ("selected", "fg:cyan noinherit"),
            ("highlighted", "fg:cyan noinherit"),
            ("pointer", "fg:cyan noinherit"),
        ]),
    ).ask()


def ask_anthropic_effort() -> str | None:
    """Ask for Anthropic effort level.

    Controls token usage and response thoroughness on Claude 4.5 / 4.6 / 4.7
    models. The API also accepts "max"; we expose low/medium/high as the
    common selection range.
    """
    return questionary.select(
        t("Select Effort Level:"),
        choices=[
            questionary.Choice(t("High (recommended)"), "high"),
            questionary.Choice(t("Medium (balanced)"), "medium"),
            questionary.Choice(t("Low (faster, cheaper)"), "low"),
        ],
        style=questionary.Style([
            ("selected", "fg:cyan noinherit"),
            ("highlighted", "fg:cyan noinherit"),
            ("pointer", "fg:cyan noinherit"),
        ]),
    ).ask()


def ask_gemini_thinking_config() -> str | None:
    """Ask for Gemini thinking configuration.

    Returns thinking_level: "high" or "minimal".
    Client maps to appropriate API param based on model series.
    """
    return questionary.select(
        t("Select Thinking Mode:"),
        choices=[
            questionary.Choice(t("Enable Thinking (recommended)"), "high"),
            questionary.Choice(t("Minimal/Disable Thinking"), "minimal"),
        ],
        style=questionary.Style([
            ("selected", "fg:green noinherit"),
            ("highlighted", "fg:green noinherit"),
            ("pointer", "fg:green noinherit"),
        ]),
    ).ask()


def ask_glm_region() -> tuple[str, str]:
    """Ask which GLM platform (Z.AI international vs BigModel China) to use.

    Zhipu serves the same GLM models under two brands with separate
    accounts; keys aren't interchangeable. Returns (provider_key, backend_url).
    """
    return questionary.select(
        t("Select GLM platform:"),
        choices=[
            questionary.Choice(
                t("Z.AI — api.z.ai (international, uses ZHIPU_API_KEY)"),
                value=("glm", "https://api.z.ai/api/paas/v4/"),
            ),
            questionary.Choice(
                t("BigModel — open.bigmodel.cn (China, uses ZHIPU_CN_API_KEY)"),
                value=("glm-cn", "https://open.bigmodel.cn/api/paas/v4/"),
            ),
        ],
        style=questionary.Style([
            ("selected", "fg:cyan noinherit"),
            ("highlighted", "fg:cyan noinherit"),
            ("pointer", "fg:cyan noinherit"),
        ]),
    ).ask()


def ask_qwen_region() -> tuple[str, str]:
    """Ask which Qwen region (international vs China) to use.

    Alibaba DashScope exposes two endpoints with separate accounts —
    a key from one region does NOT authenticate against the other
    (fixes #758). Returns (provider_key, backend_url).
    """
    return questionary.select(
        t("Select Qwen region:"),
        choices=[
            questionary.Choice(
                t("International — dashscope-intl.aliyuncs.com (uses DASHSCOPE_API_KEY)"),
                value=("qwen", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"),
            ),
            questionary.Choice(
                t("China — dashscope.aliyuncs.com (uses DASHSCOPE_CN_API_KEY)"),
                value=("qwen-cn", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
            ),
        ],
        style=questionary.Style([
            ("selected", "fg:cyan noinherit"),
            ("highlighted", "fg:cyan noinherit"),
            ("pointer", "fg:cyan noinherit"),
        ]),
    ).ask()


def ask_minimax_region() -> tuple[str, str]:
    """Ask which MiniMax region (global vs China) to use.

    MiniMax exposes two endpoints with separate accounts — a key from
    one region does NOT authenticate against the other. Returns
    (provider_key, backend_url).
    """
    return questionary.select(
        t("Select MiniMax region:"),
        choices=[
            questionary.Choice(
                t("Global — api.minimax.io (uses MINIMAX_API_KEY)"),
                value=("minimax", "https://api.minimax.io/v1"),
            ),
            questionary.Choice(
                t("China — api.minimaxi.com (uses MINIMAX_CN_API_KEY)"),
                value=("minimax-cn", "https://api.minimaxi.com/v1"),
            ),
        ],
        style=questionary.Style([
            ("selected", "fg:cyan noinherit"),
            ("highlighted", "fg:cyan noinherit"),
            ("pointer", "fg:cyan noinherit"),
        ]),
    ).ask()


def confirm_ollama_endpoint(url: str) -> None:
    """Show the resolved Ollama endpoint after provider selection.

    Surfaces three things the user benefits from seeing before model
    selection: which URL we'll actually hit, where it came from
    (`OLLAMA_BASE_URL` vs default), and a soft warning if the URL is
    missing the scheme/port that ollama-serve expects. The warning is
    advisory only — we don't reject malformed input, since the user may
    be doing something deliberately unusual (e.g. a reverse-proxy path).
    """
    from_env = os.environ.get("OLLAMA_BASE_URL")
    origin = t(" (from OLLAMA_BASE_URL)") if from_env and from_env == url else ""
    console.print(f"[green]{t('✓ Using Ollama at {url}{origin}', url=url, origin=origin)}[/green]")

    if not url.startswith(("http://", "https://")):
        console.print(
            f"[yellow]Note: {url!r} is missing a scheme. "
            f"Ollama-serve typically expects a URL like "
            f"http://<host>:11434/v1.[/yellow]"
        )
    elif ":11434" not in url and "://localhost" not in url and "://127.0.0.1" not in url:
        # Soft hint when the port differs from the ollama-serve default
        # and the host isn't local (where users sometimes proxy on :80).
        console.print(
            f"[yellow]Note: {url!r} doesn't include port 11434. "
            f"Make sure your remote ollama-serve listens on the port "
            f"shown above.[/yellow]"
        )


def ensure_api_key(provider: str) -> str | None:
    """Make sure the API key for `provider` is available in the environment.

    If the env var is already set, returns its value untouched. Otherwise
    interactively prompts the user, persists the value to the project's
    .env file via python-dotenv's set_key (creating .env if needed), and
    exports it into os.environ so the current process picks it up.

    Returns None for providers that do not require a key (e.g. ollama)
    and for providers not found in the canonical mapping.
    """
    env_var = get_api_key_env(provider)
    if env_var is None:
        return None  # ollama / unknown — no key check possible

    # Key-optional providers (generic OpenAI-compatible / local servers) read the
    # key when present but must never force an interactive prompt.
    from tradingagents.llm_clients.openai_client import OPENAI_COMPATIBLE_PROVIDERS
    spec = OPENAI_COMPATIBLE_PROVIDERS.get(provider.lower())
    if spec is not None and spec.key_optional:
        return os.environ.get(env_var)

    existing = os.environ.get(env_var)
    if existing:
        return existing

    console.print(
        f"\n[yellow]{t('{env_var} is not set in your environment.', env_var=env_var)}[/yellow]"
    )
    key = questionary.password(
        t("Paste your {env_var} (will be saved to .env):", env_var=env_var),
        style=questionary.Style([
            ("text", "fg:cyan"),
            ("highlighted", "noinherit"),
        ]),
    ).ask()
    if not key:
        console.print(
            f"[red]{t('Skipped. API calls will fail until {env_var} is set.', env_var=env_var)}[/red]"
        )
        return None

    # Never walk upward when *writing* a secret. A test run, notebook or nested
    # project may sit below an unrelated parent .env; mutating the first file
    # found by find_dotenv can overwrite a real credential outside the caller's
    # intended directory. Reads may search parents, writes stay in cwd.
    env_path = str(Path.cwd() / ".env")
    Path(env_path).touch(exist_ok=True)
    set_key(env_path, env_var, key)
    os.environ[env_var] = key
    console.print(f"[green]{t('Saved {env_var} to {env_path}', env_var=env_var, env_path=env_path)}[/green]")
    return key


def ask_output_language() -> str:
    """Ask for report output language."""
    choice = questionary.select(
        t("Select Output Language:"),
        choices=[
            questionary.Choice(t("English (default)"), "English"),
            questionary.Choice(t("Chinese (中文)"), "Chinese"),
            questionary.Choice(t("Japanese (日本語)"), "Japanese"),
            questionary.Choice(t("Korean (한국어)"), "Korean"),
            questionary.Choice(t("Hindi (हिन्दी)"), "Hindi"),
            questionary.Choice(t("Spanish (Español)"), "Spanish"),
            questionary.Choice(t("Portuguese (Português)"), "Portuguese"),
            questionary.Choice(t("French (Français)"), "French"),
            questionary.Choice(t("German (Deutsch)"), "German"),
            questionary.Choice(t("Arabic (العربية)"), "Arabic"),
            questionary.Choice(t("Russian (Русский)"), "Russian"),
            questionary.Choice(t("Custom language"), "custom"),
        ],
        style=questionary.Style([
            ("selected", "fg:yellow noinherit"),
            ("highlighted", "fg:yellow noinherit"),
            ("pointer", "fg:yellow noinherit"),
        ]),
    ).ask()

    # Output language has a sensible default, so a cancel falls back to English
    # rather than exiting the run (unlike the required model/provider prompts).
    if choice is None:
        return "English"
    if choice == "custom":
        return (questionary.text(
            t("Enter language name (e.g. Turkish, Vietnamese, Thai, Indonesian):"),
            validate=lambda x: len(x.strip()) > 0 or t("Please enter a language name."),
        ).ask() or "").strip() or "English"

    return choice
