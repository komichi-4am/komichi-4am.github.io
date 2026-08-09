#!/usr/bin/env python3
"""Create or validate the persistent Bilibili session used by Komichi."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from bilibili_session import (
    DEFAULT_COOKIE_PATH,
    BilibiliSession,
    BilibiliSessionError,
    generate_qr_challenge,
    poll_qr_challenge,
)


SCRIPT_DIR = Path(__file__).resolve().parent
NATIVE_QR_RENDERER = SCRIPT_DIR / "render_qr.m"
DEFAULT_QR_OUTPUT = Path(tempfile.gettempdir()) / "komichi-bilibili-login.png"


def render_qr_png(value: str, output: Path) -> str:
    output = output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        import qrcode  # type: ignore

        image = qrcode.make(value)
        image.save(output)
        return "python-qrcode"
    except ImportError:
        pass

    qrencode = shutil.which("qrencode")
    if qrencode:
        subprocess.run(
            [qrencode, "-o", str(output), "-s", "10", "-m", "4", value],
            check=True,
            capture_output=True,
            text=True,
        )
        return "qrencode"

    clang = shutil.which("clang")
    if clang and NATIVE_QR_RENDERER.is_file():
        executable = Path(tempfile.gettempdir()) / "komichi-render-qr"
        subprocess.run(
            [
                clang,
                "-fobjc-arc",
                "-fno-modules",
                "-framework",
                "Foundation",
                "-framework",
                "CoreImage",
                "-framework",
                "AppKit",
                str(NATIVE_QR_RENDERER),
                "-o",
                str(executable),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            [str(executable), value, str(output)],
            check=True,
            capture_output=True,
            text=True,
        )
        return "macos-coreimage"

    raise BilibiliSessionError(
        "qr_renderer_missing",
        "No QR renderer is available. Install qrcode[pil] or qrencode.",
    )


def command_login(args: argparse.Namespace) -> None:
    session = BilibiliSession.empty()
    login_url, key = generate_qr_challenge(session)
    renderer = render_qr_png(login_url, args.qr_output)
    qr_path = args.qr_output.expanduser().resolve()
    print(
        json.dumps(
            {
                "status": "waiting_for_scan",
                "qrImagePath": str(qr_path),
                "renderer": renderer,
                "instruction": "Open the QR image and scan it with the Bilibili mobile app.",
            },
            ensure_ascii=False,
            indent=2,
        ),
        flush=True,
    )

    deadline = time.monotonic() + args.timeout
    previous_status: str | None = None
    while time.monotonic() < deadline:
        status, message = poll_qr_challenge(session, key)
        if status != previous_status:
            print(
                json.dumps({"status": status, "message": message}, ensure_ascii=False),
                flush=True,
            )
            previous_status = status
        if status == "success":
            account = session.check_login()
            session.refresh_home_session()
            session.save(args.cookie_file)
            print(
                json.dumps(
                    {
                        "status": "saved",
                        "account": account,
                        "cookieFile": str(args.cookie_file.expanduser()),
                        "cookieValuesPrinted": False,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return
        if status in {"expired", "failed"}:
            raise BilibiliSessionError("bilibili_qr_login_failed", message)
        time.sleep(args.poll_interval)
    raise BilibiliSessionError(
        "bilibili_qr_login_timeout", "Bilibili QR login timed out; run the command again"
    )


def command_status(args: argparse.Namespace) -> None:
    session = BilibiliSession.load(args.cookie_file)
    account = session.check_login()
    session.save(args.cookie_file)
    print(
        json.dumps(
            {
                "status": "valid",
                "account": account,
                "cookieFile": str(args.cookie_file.expanduser()),
                "cookieValuesPrinted": False,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=("login", "status"), help="Create a login or validate it"
    )
    parser.add_argument(
        "--cookie-file", type=Path, default=DEFAULT_COOKIE_PATH, help="Local secret file"
    )
    parser.add_argument(
        "--qr-output", type=Path, default=DEFAULT_QR_OUTPUT, help="Temporary QR PNG path"
    )
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--poll-interval", type=float, default=2.0)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        if args.command == "login":
            command_login(args)
        else:
            command_status(args)
    except BilibiliSessionError as exc:
        print(
            json.dumps({"ok": False, "code": exc.code, "error": str(exc)}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 2
    except subprocess.CalledProcessError as exc:
        print(
            json.dumps(
                {"ok": False, "code": "qr_renderer_failed", "error": "QR rendering failed"},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
