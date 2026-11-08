import pytest

from agent_harness_demo.__main__ import build_settings, parser


def settings(*flags: str):
    return build_settings(parser().parse_args(list(flags)))


def test_no_model_runs_the_labelled_fixture() -> None:
    assert settings().mode == "fixture"


def test_ollama_model_keeps_harness_owned_initial_plan() -> None:
    local = settings("--model", "qwen3.5:9b-q4_K_M", "--decision-protocol", "json_tools")
    assert local.mode == "local" and local.decision_protocol == "json_tools" and local.server_controlled_initial_plan


def test_openai_compatible_provider() -> None:
    remote = settings("--provider", "openai", "--model", "gpt-example", "--base-url", "https://api.example.test/v1/",
                      "--api-key-env", "EXAMPLE_KEY", "--context-window", "64000")
    assert (remote.mode, remote.base_url, remote.api_key_env, remote.context_window_tokens) == (
        "openai", "https://api.example.test/v1", "EXAMPLE_KEY", 64000)


def test_anthropic_provider_defaults() -> None:
    claude = settings("--provider", "anthropic")
    assert (claude.mode, claude.name, claude.api_key_env, claude.effort, claude.context_window_tokens) == (
        "anthropic", "claude-opus-5-5", "ANTHROPIC_API_KEY", "low", 200_000)


@pytest.mark.parametrize("flags", [
    ("--provider", "openai", "--model", "gpt-example"),
    ("--provider", "anthropic", "--base-url", "https://example.test"),
    ("--provider", "anthropic", "--thinking"),
    ("--provider", "openai", "--model", "m", "--base-url", "https://x.test", "--decision-protocol", "json_tools"),
    ("--base-url", "https://x.test"),
    ("--thinking",),
])
def test_inconsistent_flags_are_rejected(flags: tuple[str, ...]) -> None:
    with pytest.raises(ValueError):
        settings(*flags)
