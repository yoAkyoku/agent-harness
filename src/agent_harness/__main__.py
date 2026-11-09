import argparse
import os
import re
import shutil
from pathlib import Path
import secrets

import uvicorn

from .local import MIN_TOKEN_LENGTH, TOKEN_ENV
from .web import create_app
from .model import ModelSettings


def parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the Agent Harness workbench on 127.0.0.1.",
        epilog="Without --model and with the default provider, a clearly labelled scripted fixture runs instead of a model.",
    )
    parser.add_argument("--store", type=Path, default=Path(".private/workbench"))
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--host", choices=("127.0.0.1", "0.0.0.0"), default="127.0.0.1",
                        help="Bind address. Use 0.0.0.0 only inside a container whose port is published to the host's "
                             "loopback (docker run -p 127.0.0.1:8787:8787); requests still need a localhost Host header "
                             "and the access token.")
    parser.add_argument("--provider", choices=("ollama", "openai", "anthropic"), default="ollama",
                        help="ollama: a local Ollama model; openai: any OpenAI-compatible Chat Completions endpoint; "
                             "anthropic: Claude through the official SDK.")
    parser.add_argument("--model", help="Model name. Ollama: an already-installed model (omit for the fixture). "
                                        "Anthropic: defaults to claude-opus-5-5.")
    parser.add_argument("--base-url", help="OpenAI-compatible base URL, for example https://api.openai.com/v1.")
    parser.add_argument("--api-key-env", help="Environment variable holding the API key "
                                              "(default OPENAI_API_KEY or ANTHROPIC_API_KEY). The key itself is never a flag.")
    parser.add_argument("--alt-model", dest="alt_models", action="append", default=[],
                        help="Another model of the same remote provider that a Run may choose; repeat for more.")
    parser.add_argument("--price", dest="prices", action="append", default=[], metavar="MODEL=INPUT,OUTPUT",
                        help="US dollars per million input and output tokens for a configured model, for cost estimates; "
                             "repeat for more. Without it, Runs show token counts only.")
    parser.add_argument("--context-window", type=int, help="Context budget in tokens for remote providers.")
    parser.add_argument("--effort", choices=("low", "medium", "high", "xhigh", "max"), default="low",
                        help="Claude effort level (thinking depth and token spend).")
    parser.add_argument("--thinking", action="store_true", help="Opt in to the verified local boolean-thinking profile; defaults off.")
    parser.add_argument("--model-authored-initial-plan", action="store_true",
                        help="Allow a free-form model-written initial plan; by default real models use harness-owned action stages.")
    parser.add_argument("--decision-protocol", choices=("native", "json_tools"), default="native",
                        help="Ollama decision wire protocol; JSON tools is experimental and requires --model.")
    parser.add_argument("--exact-tokenizer-library", type=Path, help="Explicit pinned local Windows libllama DLL; requires matching model asset.")
    parser.add_argument("--exact-tokenizer-model", type=Path, help="Explicit pinned local Qwen35 9B GGUF blob; vocab-only, no inference.")
    parser.add_argument("--mcp-config", type=Path,
                        help="JSON file with mcpServers (command, args, allowedTools); their allowed read-only tools can be selected by Runs.")
    parser.add_argument("--skills-dir", type=Path,
                        help="Folder of skills (each a folder with SKILL.md); Runs can select them and load instructions on demand.")
    parser.add_argument("--hooks-config", type=Path,
                        help="JSON file with HTTP webhooks called before tool calls (allow/deny) and after them (notify); "
                             "command hooks need --allow-command-hooks.")
    parser.add_argument("--tesseract", type=Path,
                        help="A local Tesseract program; PDFs without a text layer are then read through OCR.")
    parser.add_argument("--ocr-languages", help="Tesseract language codes joined by +, such as eng+chi_tra (default eng).")
    parser.add_argument("--allow-command-hooks", action="store_true",
                        help="Allow hooks that start local programs (argument lists, never a shell) from --hooks-config.")
    parser.add_argument("--fault-once-source-read", action="store_true", help="Controlled validation: fail each Run's first source_read before filesystem access.")
    parser.add_argument("--hold-first-tool", choices=["source_read", "artifact_write"], help="Controlled validation: hold the service's first matching read before access, or publish before result acknowledgement.")
    return parser


def build_ocr(arguments: argparse.Namespace) -> list[str] | None:
    """The OCR command from --tesseract; raises ValueError with a user-facing reason."""
    if arguments.tesseract is None:
        if arguments.ocr_languages is not None:
            raise ValueError("--ocr-languages needs --tesseract.")
        return None
    program = arguments.tesseract if arguments.tesseract.is_file() else shutil.which(str(arguments.tesseract))
    if program is None:
        raise ValueError(f"--tesseract {arguments.tesseract} is not a program on this computer.")
    if arguments.ocr_languages is not None and not re.fullmatch(r"[A-Za-z_]{2,20}(\+[A-Za-z_]{2,20}){0,7}", arguments.ocr_languages):
        raise ValueError("--ocr-languages takes Tesseract language codes joined by +, such as eng+chi_tra.")
    return [str(program)]


def build_prices(arguments: argparse.Namespace, settings: ModelSettings) -> dict[str, dict[str, float]]:
    """Parse --price MODEL=INPUT,OUTPUT entries for configured models; raises ValueError with a user-facing reason."""
    configured = {settings.name, *settings.alternatives}
    prices: dict[str, dict[str, float]] = {}
    for entry in arguments.prices:
        name, separator, values = entry.partition("=")
        parts = values.split(",")
        try:
            if not separator or len(parts) != 2:
                raise ValueError
            numbers = [float(part) for part in parts]
        except ValueError:
            raise ValueError(f"--price {entry!r} must look like MODEL=INPUT,OUTPUT, in US dollars per million tokens.") from None
        if any(not 0 <= number < 10_000 for number in numbers):  # NaN fails this comparison too
            raise ValueError(f"--price {entry!r} needs prices from 0 to 10000 dollars per million tokens.")
        if name not in configured:
            raise ValueError(f"--price names {name!r}, which is not a configured model ({', '.join(sorted(configured))}).")
        if name in prices:
            raise ValueError(f"--price is given twice for {name}.")
        prices[name] = {"input": numbers[0], "output": numbers[1]}
    return prices


def build_settings(arguments: argparse.Namespace) -> ModelSettings:
    """Turn parsed flags into model settings; raises ValueError with a user-facing reason."""
    plan = not arguments.model_authored_initial_plan
    local_only = {"--thinking": arguments.thinking, "--decision-protocol json_tools": arguments.decision_protocol != "native",
                  "--exact-tokenizer-*": bool(arguments.exact_tokenizer_library or arguments.exact_tokenizer_model)}
    if arguments.provider != "ollama":
        used = [flag for flag, value in local_only.items() if value]
        if used:
            raise ValueError(f"{', '.join(used)} only apply to --provider ollama.")
    if arguments.provider == "openai":
        if not arguments.model or not arguments.base_url:
            raise ValueError("--provider openai needs --model and --base-url.")
        return ModelSettings.openai_compatible(arguments.model, base_url=arguments.base_url,
                                               api_key_env=arguments.api_key_env or "OPENAI_API_KEY",
                                               context_window_tokens=arguments.context_window or 128_000,
                                               server_controlled_initial_plan=plan,
                                               alternatives=tuple(arguments.alt_models))
    if arguments.provider == "anthropic":
        if arguments.base_url:
            raise ValueError("--base-url does not apply to --provider anthropic.")
        return ModelSettings.anthropic(arguments.model or "claude-opus-5-5",
                                       api_key_env=arguments.api_key_env or "ANTHROPIC_API_KEY",
                                       context_window_tokens=arguments.context_window or 200_000,
                                       effort=arguments.effort, server_controlled_initial_plan=plan,
                                       alternatives=tuple(arguments.alt_models))
    if arguments.base_url or arguments.api_key_env or arguments.context_window or arguments.alt_models:
        raise ValueError("--base-url, --api-key-env, --context-window and --alt-model apply to remote providers.")
    if not arguments.model:
        used = [flag for flag, value in local_only.items() if value]
        if arguments.model_authored_initial_plan:
            used.append("--model-authored-initial-plan")
        if used:
            raise ValueError(f"{', '.join(used)} requires an explicit already-installed local --model.")
        return ModelSettings()
    return ModelSettings.local(arguments.model, thinking=arguments.thinking, decision_protocol=arguments.decision_protocol,
                               server_controlled_initial_plan=plan,
                               exact_tokenizer_library=arguments.exact_tokenizer_library,
                               exact_tokenizer_model=arguments.exact_tokenizer_model)


def main() -> None:
    cli = parser()
    arguments = cli.parse_args()
    if not 1 <= arguments.port <= 65535:
        cli.error("--port must be between 1 and 65535")
    try:
        settings = build_settings(arguments)
        prices = build_prices(arguments, settings)
        ocr_command = build_ocr(arguments)
    except ValueError as error:
        cli.error(str(error))
    configured = os.environ.get(TOKEN_ENV)
    if configured is not None and len(configured) < MIN_TOKEN_LENGTH:
        cli.error(f"{TOKEN_ENV} must have at least {MIN_TOKEN_LENGTH} characters.")
    token = configured or secrets.token_urlsafe(32)
    try:
        app = create_app(arguments.store.resolve(), model_settings=settings, fault_once_source_read=arguments.fault_once_source_read,
                         hold_first_tool=arguments.hold_first_tool, access_token=token, mcp_config=arguments.mcp_config,
                         skills_dir=arguments.skills_dir, prices=prices, hooks_config=arguments.hooks_config,
                         allow_command_hooks=arguments.allow_command_hooks,
                         ocr_command=ocr_command, ocr_languages=arguments.ocr_languages or "eng")
    except ValueError as error:
        cli.error(str(error))
    shown = "<the value of " + TOKEN_ENV + ">" if configured else token
    print(f"Open the workbench at http://127.0.0.1:{arguments.port}/?token={shown}", flush=True)
    print("Scripts: send the same token as 'Authorization: Bearer <token>'.", flush=True)
    uvicorn.run(app, host=arguments.host, port=arguments.port, access_log=False)


if __name__ == "__main__":
    main()
