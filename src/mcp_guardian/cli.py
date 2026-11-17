"""Command-line entry points for MCP Guardian."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections.abc import Sequence

from mcp_guardian.approval.gate import ApprovalGate
from mcp_guardian.demo.client import run_interactive_demo
from mcp_guardian.proxy.http import run_http_server
from mcp_guardian.proxy.stdio import run_stdio_proxy


def _configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        stream=sys.stderr,
    )


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument(
        "--timeout",
        type=float,
        default=120.0,
        help="Seconds to wait for user approval before auto-blocking (default: 120)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mcp-guardian",
        description="Monitor MCP tool calls and require Allow/Block confirmation.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    wrap = sub.add_parser("wrap", help="Stdio proxy for Claude Desktop / local clients")
    _add_common(wrap)
    wrap.add_argument(
        "--name",
        default="mcp-server",
        help="Logical MCP server name shown in the approval popup",
    )
    wrap.add_argument(
        "upstream",
        nargs=argparse.REMAINDER,
        help="Upstream MCP server command (use -- before the command)",
    )

    http = sub.add_parser(
        "http", help="Streamable HTTP proxy for Claude Web custom connectors"
    )
    _add_common(http)
    http.add_argument("--listen", default="127.0.0.1:8765", help="host:port to bind")
    http.add_argument(
        "--name",
        default="mcp-server",
        help="Logical MCP server name shown in the approval popup",
    )
    http.add_argument(
        "upstream",
        nargs=argparse.REMAINDER,
        help="Upstream MCP server command (use -- before the command)",
    )

    demo = sub.add_parser(
        "demo",
        help="Run an interactive demo (popup Allow/Block) without Claude",
    )
    _add_common(demo)
    demo.add_argument(
        "--name", default="demo-server", help="Server name shown in popups"
    )

    ui = sub.add_parser(
        "ui",
        help="Run the always-on-top approval UI daemon (needed for Claude Desktop)",
    )
    ui.add_argument("-v", "--verbose", action="store_true")

    return parser


def _strip_separator(args: list[str]) -> list[str]:
    if args and args[0] == "--":
        return args[1:]
    return args


def _require_upstream(args: list[str]) -> list[str]:
    command = _strip_separator(args)
    if not command:
        raise SystemExit("upstream command required after --")
    return command


def main(argv: Sequence[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    _configure_logging(getattr(args, "verbose", False))

    if args.command == "ui":
        from mcp_guardian.approval.display import ensure_display_env
        from mcp_guardian.approval.ipc import ApprovalUIServer

        ensure_display_env()
        server = ApprovalUIServer()
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            server.stop()
        raise SystemExit(0)

    gate = ApprovalGate(timeout_seconds=args.timeout)

    if args.command == "wrap":
        command = _require_upstream(args.upstream)
        raise SystemExit(
            asyncio.run(run_stdio_proxy(command, server_name=args.name, gate=gate))
        )

    if args.command == "http":
        command = _require_upstream(args.upstream)
        host, _, port_s = args.listen.partition(":")
        port = int(port_s or "8765")
        try:
            asyncio.run(
                run_http_server(
                    command,
                    host=host or "127.0.0.1",
                    port=port,
                    server_name=args.name,
                    gate=gate,
                )
            )
        except KeyboardInterrupt:
            raise SystemExit(0) from None

    if args.command == "demo":
        # Ensure UI daemon is up before scripted popups.
        from mcp_guardian.approval import ipc

        ipc.ensure_ui_daemon()
        guardian_cmd = [
            sys.executable,
            "-m",
            "mcp_guardian",
            "wrap",
            "--name",
            args.name,
            "--timeout",
            str(args.timeout),
            "--",
            sys.executable,
            "-m",
            "mcp_guardian.demo.server",
        ]
        raise SystemExit(asyncio.run(run_interactive_demo(guardian_cmd)))

    parser.error(f"unknown command: {args.command}")


if __name__ == "__main__":
    main()
