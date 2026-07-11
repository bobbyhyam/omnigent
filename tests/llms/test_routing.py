"""Tests for llms.routing — model string parsing and harness inference."""

import pytest

from omnigent.errors import OmnigentError
from omnigent.llms.routing import RoutedModel, infer_harness_from_model, parse_model_string


@pytest.mark.parametrize(
    ("model_string", "expected"),
    [
        (
            "anthropic/claude-sonnet-4-20250514",
            RoutedModel(provider="anthropic", model="claude-sonnet-4-20250514"),
        ),
        (
            "openai/gpt-5.4",
            RoutedModel(provider="openai", model="gpt-5.4"),
        ),
        (
            "groq/llama-3.1-70b",
            RoutedModel(provider="groq", model="llama-3.1-70b"),
        ),
        (
            "deepseek/deepseek-chat",
            RoutedModel(provider="deepseek", model="deepseek-chat"),
        ),
        (
            "xai/grok-2",
            RoutedModel(provider="xai", model="grok-2"),
        ),
        (
            "openrouter/meta-llama/llama-3.1-70b",
            RoutedModel(
                provider="openrouter",
                model="meta-llama/llama-3.1-70b",
            ),
        ),
        (
            "ollama/llama3",
            RoutedModel(provider="ollama", model="llama3"),
        ),
        (
            "gemini/gemini-2.5-pro",
            RoutedModel(provider="gemini", model="gemini-2.5-pro"),
        ),
        (
            "bedrock/anthropic.claude-3-sonnet",
            RoutedModel(provider="bedrock", model="anthropic.claude-3-sonnet"),
        ),
        (
            "vertex/gemini-2.5-pro",
            RoutedModel(provider="vertex", model="gemini-2.5-pro"),
        ),
        (
            "databricks/my-endpoint",
            RoutedModel(provider="databricks", model="my-endpoint"),
        ),
        (
            "moonshot/kimi-k2-instruct",
            RoutedModel(provider="moonshot", model="kimi-k2-instruct"),
        ),
    ],
)
def test_parse_with_provider_prefix(
    model_string: str,
    expected: RoutedModel,
) -> None:
    assert parse_model_string(model_string) == expected


def test_parse_without_prefix_defaults_to_openai() -> None:
    result = parse_model_string("gpt-5.4")
    assert result == RoutedModel(provider="openai", model="gpt-5.4")


@pytest.mark.parametrize(
    ("model_string", "expected_provider"),
    [
        # Unprefixed vendor aliases (the strings a subscription/gateway login
        # exposes) must infer their real provider, not fall through to openai.
        ("claude-opus-4-8", "anthropic"),
        ("claude-sonnet-5", "anthropic"),
        ("gemini-2.5-flash", "gemini"),
        ("grok-2", "xai"),
        ("deepseek-chat", "deepseek"),
        ("databricks-claude-sonnet-4", "databricks"),
        # A genuinely unprefixed gpt-* stays on openai (the backward-compatible
        # default is preserved for the one family it was actually correct for).
        ("gpt-5.4", "openai"),
    ],
)
def test_parse_without_prefix_infers_provider_from_model_prefix(
    model_string: str,
    expected_provider: str,
) -> None:
    """
    Regression for #2071: an unprefixed ``claude-*`` (and every other
    well-known vendor alias) must not be mis-inferred as provider ``openai``.
    """
    result = parse_model_string(model_string)
    assert result == RoutedModel(provider=expected_provider, model=model_string)


def test_unknown_provider_raises() -> None:
    with pytest.raises(OmnigentError, match="Unknown provider 'foobar'"):
        parse_model_string("foobar/some-model")


# ── infer_harness_from_model ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("model", "expected_harness"),
    [
        # Databricks-hosted Claude → claude-sdk harness; these are the
        # models that triggered the original routing bug (Responses API
        # passthrough 400s for Claude).
        ("databricks-claude-sonnet-4", "claude-sdk"),
        ("databricks-claude-sonnet-4-6", "claude-sdk"),
        # Anthropic-prefixed models also need claude-sdk.
        ("anthropic/claude-sonnet-4-20250514", "claude-sdk"),
        # Databricks-hosted GPT and plain OpenAI models → openai-agents.
        ("databricks-gpt-5-4", "openai-agents"),
        ("openai/gpt-5.4", "openai-agents"),
        ("gpt-5.4", "openai-agents"),
        # Unknown model — no prefix match — returns empty string so the
        # downstream validator can surface a "harness required" error.
        ("llama3-groq", ""),
        ("unknown-model-xyz", ""),
    ],
)
def test_infer_harness_from_model(model: str, expected_harness: str) -> None:
    """
    :func:`infer_harness_from_model` maps known model prefixes to their
    harness names and returns ``""`` for unrecognised models.

    A failure here means the prefix table has drifted — either a new
    model family was added without updating the table, or an existing
    prefix was renamed.
    """
    assert infer_harness_from_model(model) == expected_harness, (
        f"Model {model!r}: expected harness {expected_harness!r}, "
        f"got {infer_harness_from_model(model)!r}. "
        "Check _HARNESS_FOR_MODEL_PREFIX in omnigent/llms/routing.py."
    )
