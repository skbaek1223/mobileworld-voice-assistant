"""`mw assistant`: run the in-app voice assistant against a phone/emulator over ADB."""

import argparse
import os
import subprocess

from loguru import logger
from rich.console import Console

from mobile_world.assistant.speech import STT_ENGINES, TTS_ENGINES


def configure_parser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "assistant",
        help="Run a voice assistant that operates only inside one app",
    )
    app = p.add_argument_group("app")
    app.add_argument("--package", required=True, help="Host app package, e.g. com.example.app")
    app.add_argument("--app-name", "--app_name", dest="app_name", help="Display name of the app")
    app.add_argument(
        "--launch-activity",
        dest="launch_activity",
        default=None,
        help="Activity used to relaunch the app (default: launcher activity)",
    )
    app.add_argument(
        "--extra-package",
        dest="extra_packages",
        action="append",
        default=[],
        help="Additional package the assistant may stay in (repeatable)",
    )
    app.add_argument(
        "--tools-file",
        dest="tools_file",
        default=None,
        help="JSON file with app tools (see docs/assistant_tools.example.json)",
    )

    llm = p.add_argument_group("agent model (OpenAI-compatible)")
    llm.add_argument("--model-name", "--model_name", dest="model_name", required=True)
    llm.add_argument("--llm-base-url", "--llm_base_url", dest="llm_base_url", required=True)
    llm.add_argument(
        "--api-key", "--api_key", dest="api_key", default=None, help="Defaults to $API_KEY"
    )
    llm.add_argument("--max-steps", dest="max_steps", type=int, default=20)

    sp = p.add_argument_group("speech")
    sp.add_argument("--language", default="ko", help="Spoken language code (default: ko)")
    sp.add_argument("--stt", choices=STT_ENGINES, default="console")
    sp.add_argument("--tts", choices=TTS_ENGINES, default="console")
    sp.add_argument(
        "--stt-model",
        dest="stt_model",
        default=None,
        help="whisper size (small, medium, large-v3) or cloud model name",
    )
    sp.add_argument("--tts-model", dest="tts_model", default=None)
    sp.add_argument("--tts-voice", dest="tts_voice", default=None)
    sp.add_argument(
        "--speech-base-url",
        dest="speech_base_url",
        default=None,
        help="Base URL for the cloud STT/TTS (default: OpenAI)",
    )
    sp.add_argument(
        "--speech-api-key",
        dest="speech_api_key",
        default=None,
        help="Defaults to $SPEECH_API_KEY, then $OPENAI_API_KEY",
    )

    dev = p.add_argument_group("device")
    dev.add_argument(
        "--aw-host",
        "--aw_host",
        dest="aw_host",
        default=None,
        help="MobileWorld server URL (started automatically when omitted)",
    )
    dev.add_argument("--device", default=None, help="ADB device ID (default: the only device)")
    dev.add_argument("--step-wait-time", dest="step_wait_time", type=float, default=1.0)

    p.add_argument(
        "--once",
        default=None,
        help="Handle a single request given as text, then exit (no listening)",
    )
    p.add_argument("--log-verbose", dest="log_verbose", action="store_true")


def _detect_device() -> str:
    out = subprocess.check_output(["adb", "devices"]).decode().splitlines()
    devices = [line.split("\t")[0] for line in out if "\tdevice" in line]
    if len(devices) != 1:
        raise SystemExit(f"Expected exactly one adb device, found {devices}. Use --device.")
    return devices[0]


async def execute(args: argparse.Namespace) -> None:
    from mobile_world.assistant import (
        AppScopeGuard,
        ToolRegistry,
        VoiceAssistantSession,
        build_stt,
        build_tts,
        load_tools_file,
    )
    from mobile_world.assistant.agent import InAppAssistantAgent
    from mobile_world.assistant.device import MobileWorldDevice
    from mobile_world.core.user_task_runner.prerequisite import env_cleanup, env_validation

    console = Console()
    if not args.log_verbose:
        logger.remove()
        logger.add(lambda m: console.print(m, end="", markup=False), level="INFO")

    device_id = args.device or _detect_device()
    aw_url = env_validation(args.aw_host, device_id)
    app_name = args.app_name or args.package

    tools = load_tools_file(args.tools_file, args.package) if args.tools_file else ToolRegistry()
    speech_key = args.speech_api_key or os.getenv("SPEECH_API_KEY") or os.getenv("OPENAI_API_KEY")
    stt = build_stt(
        args.stt,
        model=args.stt_model,
        language=args.language,
        base_url=args.speech_base_url,
        api_key=speech_key,
    )
    tts = build_tts(
        args.tts,
        model=args.tts_model,
        voice=args.tts_voice,
        base_url=args.speech_base_url,
        api_key=speech_key,
    )

    device = MobileWorldDevice(aw_url, device_id, step_wait_time=args.step_wait_time)
    guard = AppScopeGuard(
        package=args.package,
        app_name=app_name,
        extra_packages=args.extra_packages,
        tool_names=tools.names,
        launch_activity=args.launch_activity,
    )
    agent = InAppAssistantAgent(
        model_name=args.model_name,
        llm_base_url=args.llm_base_url,
        api_key=args.api_key or os.getenv("API_KEY") or "empty",
        app_name=app_name,
        language=args.language,
        tools=tools.schemas(),
    )
    session = VoiceAssistantSession(
        agent,
        device,
        stt,
        tts,
        guard,
        tools=tools,
        app_name=app_name,
        language=args.language,
        max_steps=args.max_steps,
    )

    console.print(
        f"[bold cyan]🎙  {app_name} assistant[/bold cyan] "
        f"(stt={args.stt}, tts={args.tts}, tools={len(tools)}, device={device_id})"
    )
    try:
        if not guard.is_in_scope(device.current_package()):
            device.launch(args.package, args.launch_activity)
        if args.once:
            result = session.handle_request(args.once)
            console.print(
                f"[green]status={result.status} steps={len(result.steps)} "
                f"({result.duration:.1f}s)[/green]"
            )
        else:
            session.run_forever()
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted[/yellow]")
    finally:
        env_cleanup(device_id)
