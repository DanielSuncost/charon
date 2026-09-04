#!/usr/bin/env python3
"""Launch and inspect the local Charon Graph Studio control plane."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Sequence


_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC_ROOT = _REPO_ROOT / "src"
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from charon.graph_studio_server import (  # noqa: E402
    GraphStudioController,
    create_server,
    validate_demo_assets,
)


def _state_default() -> str:
    return os.environ.get("CHARON_STATE_DIR", str(_REPO_ROOT / ".charon_state"))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="charon_graph",
        description="Run and inspect the local graph orchestration studio.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    serve = commands.add_parser("serve", help="serve the UI and control API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=4317)
    serve.add_argument("--state-dir", default=_state_default())
    serve.add_argument("--repo-root", default=str(_REPO_ROOT))

    snapshot = commands.add_parser("snapshot", help="print the current snapshot")
    snapshot.add_argument("--state-dir", default=_state_default())
    snapshot.add_argument("--repo-root", default=str(_REPO_ROOT))
    snapshot.add_argument("--compact", action="store_true")

    validate = commands.add_parser("validate", help="validate bundled graph assets")
    validate.add_argument("--repo-root", default=str(_REPO_ROOT))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    repo_root = Path(args.repo_root).expanduser().resolve()

    if args.command == "validate":
        try:
            result = validate_demo_assets(repo_root)
            generator = repo_root / "scripts" / "generate_routing_fixture.py"
            if generator.is_file():
                checked = subprocess.run(
                    [sys.executable, str(generator), "--check"],
                    cwd=repo_root,
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                if checked.returncode != 0:
                    detail = (checked.stderr or checked.stdout).strip()
                    raise RuntimeError(
                        f"routing fixture regeneration check failed: {detail}"
                    )
                result["fixture_regeneration"] = "verified"
        except (OSError, RuntimeError, ValueError) as exc:
            print(f"validation failed: {exc}", file=sys.stderr)
            return 1
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0

    state_dir = Path(args.state_dir).expanduser().resolve()
    controller = GraphStudioController(state_dir, repo_root=repo_root)
    if args.command == "snapshot":
        indent = None if args.compact else 2
        print(
            json.dumps(
                controller.snapshot(),
                indent=indent,
                ensure_ascii=False,
                separators=(",", ":") if args.compact else None,
            )
        )
        return 0

    server = create_server(
        controller,
        host=str(args.host),
        port=int(args.port),
    )
    host, port = server.server_address[:2]
    print(f"Graph Studio: http://{host}:{port}", flush=True)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
