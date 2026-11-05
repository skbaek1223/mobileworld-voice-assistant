"""Run the 늘밥 voice web app.

    NEULBAB_APP_DIR=.../agent-company/product/app \\
    python -m mobile_world.assistant.webapp --env-file path/to/.env --self-signed

Phones need HTTPS for the microphone: use --self-signed (accept the warning once on the
phone) or your own --ssl-certfile/--ssl-keyfile, or put the app behind an HTTPS proxy.
"""

from __future__ import annotations

import argparse
import os
import socket
import subprocess
from pathlib import Path

from loguru import logger


def _key_from_env_file(path: str, name: str = "OPENAI_API_KEY") -> str:
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("export "):
            line = line[7:].strip()
        if line.startswith(f"{name}=") or line.startswith(f"{name} ="):
            return line.split("=", 1)[1].strip().strip("'\"")
    raise SystemExit(f"{name} not found in {path}")


def _local_ip() -> str:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def self_signed_cert(folder: Path, ip: str) -> tuple[str, str]:
    """Create (once) a certificate valid for this machine's IP and localhost."""
    folder.mkdir(parents=True, exist_ok=True)
    cert, key = folder / "cert.pem", folder / "key.pem"
    if not cert.exists() or not key.exists():
        subprocess.run(
            [
                "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "365",
                "-keyout", str(key), "-out", str(cert), "-subj", "/CN=neulbab-voice",
                "-addext", f"subjectAltName=IP:{ip},IP:127.0.0.1,DNS:localhost",
            ],
            check=True,
            capture_output=True,
        )  # fmt: skip
        key.chmod(0o600)
    return str(cert), str(key)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="늘밥 voice web app (Android + iPhone browsers)")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8443)
    p.add_argument("--model-name", default="gpt-5")
    p.add_argument(
        "--reasoning-effort", default="minimal", choices=["minimal", "low", "medium", "high"]
    )
    p.add_argument("--llm-base-url", default="https://api.openai.com/v1")
    p.add_argument("--env-file", help="file with OPENAI_API_KEY=... (default: $OPENAI_API_KEY)")
    p.add_argument("--stt-model", default="gpt-4o-mini-transcribe")
    p.add_argument("--tts-model", default="gpt-4o-mini-tts")
    p.add_argument("--tts-voice", default="coral")
    p.add_argument("--neulbab-app-dir", default=None, help="default: $NEULBAB_APP_DIR")
    p.add_argument("--state-file", default=None, help="keep the family's orders/settings here")
    p.add_argument("--access-code", default=None, help="default: a random 6-digit code")
    p.add_argument(
        "--no-access-code", action="store_true", help="anyone who can reach it may use it"
    )
    p.add_argument("--filler-after", type=float, default=1.0)
    p.add_argument(
        "--self-signed", action="store_true", help="serve HTTPS with a generated certificate"
    )
    p.add_argument("--ssl-certfile")
    p.add_argument("--ssl-keyfile")
    p.add_argument("--log-level", default="INFO", help="DEBUG shows agent steps")
    p.add_argument(
        "--standalone",
        action="store_true",
        help="serve only the voice page at / (default: the 늘밥 screens with a voice widget)",
    )
    args = p.parse_args(argv)

    import sys

    import uvicorn

    # concise server log: no prompts / model I/O (they contain everything the user said)
    logger.remove()
    logger.add(
        sys.stderr,
        level=args.log_level,
        filter=lambda r: not r["name"].startswith(("mobile_world.agents", "mobile_world.runtime")),
    )

    from mobile_world.assistant.neulbab import MockNeulbabApp, load_data
    from mobile_world.assistant.neulbab.data import resolve_app_dir
    from mobile_world.assistant.webapp.server import Family, WebConfig, create_app
    from mobile_world.assistant.webapp.voice import OpenAIVoice, VoiceConfig

    api_key = (
        _key_from_env_file(args.env_file) if args.env_file else os.getenv("OPENAI_API_KEY", "")
    )
    if not api_key:
        raise SystemExit("No OpenAI key: pass --env-file or set OPENAI_API_KEY")

    cfg = WebConfig(
        model_name=args.model_name,
        llm_base_url=args.llm_base_url,
        api_key=api_key,
        reasoning_effort=args.reasoning_effort,
        filler_after_sec=None if args.filler_after < 0 else args.filler_after,
    )
    if args.no_access_code:
        cfg.access_code = None
    elif args.access_code:
        cfg.access_code = args.access_code

    family = Family(MockNeulbabApp(load_data(args.neulbab_app_dir), state_file=args.state_file))
    voice = OpenAIVoice(
        api_key,
        config=VoiceConfig(
            stt_model=args.stt_model, tts_model=args.tts_model, tts_voice=args.tts_voice
        ),
    )
    for phrase in ("잠시만요.", "네, 또는 아니요로 말씀해 주세요."):  # instant fillers
        voice.synthesize(phrase)
    app_dir = None if args.standalone else resolve_app_dir(args.neulbab_app_dir)
    app = create_app(family, cfg, voice, neulbab_app_dir=app_dir)

    ip = _local_ip()
    cert = key = None
    if args.self_signed:
        cert, key = self_signed_cert(Path.home() / ".cache" / "neulbab-voice", ip)
    elif args.ssl_certfile:
        cert, key = args.ssl_certfile, args.ssl_keyfile
    scheme = "https" if cert else "http"
    url = f"{scheme}://{ip}:{args.port}/"
    logger.info(f"늘밥 음성 비서: {url}   접속 코드: {cfg.access_code or '(없음)'}")
    if cfg.access_code and app_dir:
        logger.info(f"접속 번호를 미리 넣은 주소: {url}?code={cfg.access_code}")
    if not cert:
        logger.warning("HTTP only: phones will not allow the microphone (use --self-signed)")
    uvicorn.run(
        app, host=args.host, port=args.port, ssl_certfile=cert, ssl_keyfile=key, log_level="warning"
    )


if __name__ == "__main__":
    main()
