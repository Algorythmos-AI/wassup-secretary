"""wassup — operator CLI for voice-agent configuration.

    wassup voice bindings                         show what every number routes to
    wassup voice export --out DIR                 save numbers + bound agent/LLM versions (backup)
    wassup voice rebind NUMBER --agent A --version N [--apply]
    wassup voice rollback NUMBER [--apply]        restore the binding before the last rebind

Safety rules (the rebind is how a clinic is cut over, and how a cutover is rolled back):
- Everything that writes is a dry run unless ``--apply`` is given.
- A number is only ever bound to an explicit, *published* version: never a draft, never "latest"
  (a draft binding means any edit in the provider's dashboard goes live on the next call).
- After writing, the number is read back and must show exactly the new binding.
- Every applied rebind appends the previous binding to ``<state-dir>/<number>.jsonl``, so
  ``rollback`` needs no memory of what was there before.
- The API key comes only from ``RETELL_API_KEY`` and is never printed.
- Exports contain prompts (trade secrets): files are written owner-read-only, into a directory
  you choose; never commit them.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

from wassup_cli.retell import Retell, RetellError, binding

DEFAULT_STATE_DIR = Path.home() / ".wassup" / "rebind"


class CliError(Exception):
    """A refusal or failure explained to the operator; exit status 1."""


def _describe(routes: list[tuple[str, int | None, float]]) -> str:
    if not routes:
        return "(unbound)"
    return ", ".join(
        f"{agent} v{'LATEST' if version is None else version}"
        + (f" (weight {weight:g})" if weight != 1 else "")
        for agent, version, weight in routes
    )


def cmd_bindings(api: Retell, out: TextIO) -> None:
    for number in sorted(api.list_phone_numbers(), key=lambda n: str(n.get("phone_number"))):
        routes = binding(number)
        warnings = [f"{a} is on LATEST (floating)" for a, v, _ in routes if v is None]
        for agent, version, _ in routes:
            if version is not None:
                detail = api.get_agent_version(agent, version)
                if detail is None or not detail.get("is_published"):
                    warnings.append(f"{agent} v{version} is NOT published")
        line = f"{number.get('phone_number')}: {_describe(routes)}"
        out.write(line + ("  !! " + "; ".join(warnings) if warnings else "") + "\n")


def _write_private(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    path.chmod(0o600)


def cmd_export(api: Retell, out_dir: Path, out: TextIO) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    out_dir.chmod(0o700)
    numbers = api.list_phone_numbers()
    _write_private(out_dir / "phone-numbers.json", numbers)
    pairs = sorted({(a, v) for n in numbers for a, v, _ in binding(n) if v is not None})
    for agent_id, version in pairs:
        agent = api.get_agent_version(agent_id, version)
        if agent is None:
            raise CliError(f"{agent_id} v{version} could not be read")
        _write_private(out_dir / f"{agent_id}.v{version}.agent.json", agent)
        engine = agent.get("response_engine") or {}
        if engine.get("type") == "retell-llm" and engine.get("llm_id"):
            llm_version = engine.get("version", version)
            llm = api.get_llm_version(engine["llm_id"], int(llm_version))
            if llm is not None:
                _write_private(out_dir / f"{agent_id}.v{version}.llm.json", llm)
    out.write(f"exported {len(numbers)} numbers and {len(pairs)} agent versions to {out_dir}\n")


def _state_file(state_dir: Path, e164: str) -> Path:
    return state_dir / f"{e164.lstrip('+')}.jsonl"


def _rebind(
    api: Retell,
    e164: str,
    agent_id: str,
    version: int,
    *,
    apply: bool,
    state_dir: Path,
    out: TextIO,
    reason: str,
) -> None:
    number = api.get_phone_number(e164)
    if number is None:
        raise CliError(f"{e164} is not a number on this account")
    current = binding(number)
    target = api.get_agent_version(agent_id, version)
    if target is None:
        raise CliError(f"{agent_id} v{version} does not exist")
    if not target.get("is_published"):
        raise CliError(f"{agent_id} v{version} is a draft: publish it first, then bind")
    out.write(f"{e164}\n  now:  {_describe(current)}\n  new:  {agent_id} v{version}\n")
    if current == [(agent_id, version, 1.0)]:
        out.write("  already bound; nothing to do\n")
        return
    if not apply:
        out.write("  dry run: add --apply to make this change\n")
        return
    state_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "at": datetime.now(UTC).isoformat(),
        "number": e164,
        "previous": [{"agent_id": a, "agent_version": v, "weight": w} for a, v, w in current],
        "new": {"agent_id": agent_id, "agent_version": version},
        "reason": reason,
    }
    with _state_file(state_dir, e164).open("a") as log:  # written before the change
        log.write(json.dumps(record) + "\n")
    api.bind_number(e164, agent_id, version)
    after = api.get_phone_number(e164)
    if after is None or binding(after) != [(agent_id, version, 1.0)]:
        raise CliError(
            f"verification FAILED: {e164} now shows {_describe(binding(after or {}))}; "
            f"roll back with: wassup voice rollback {e164} --apply"
        )
    out.write(f"  applied and verified. Roll back with: wassup voice rollback {e164} --apply\n")


def cmd_rollback(api: Retell, e164: str, *, apply: bool, state_dir: Path, out: TextIO) -> None:
    path = _state_file(state_dir, e164)
    lines = path.read_text().splitlines() if path.exists() else []
    if not lines:
        raise CliError(f"no rebind of {e164} recorded in {state_dir}")
    previous = json.loads(lines[-1])["previous"]
    if len(previous) != 1 or previous[0].get("agent_version") is None:
        raise CliError(
            f"the previous binding of {e164} was {previous}: not a single published version, "
            "so it can't be restored automatically; rebind explicitly"
        )
    _rebind(
        api,
        e164,
        previous[0]["agent_id"],
        int(previous[0]["agent_version"]),
        apply=apply,
        state_dir=state_dir,
        out=out,
        reason="rollback",
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wassup", description=__doc__.split("\n\n")[0])
    groups = parser.add_subparsers(dest="group", required=True)
    voice = groups.add_parser("voice", help="voice-agent configuration")
    commands = voice.add_subparsers(dest="command", required=True)
    commands.add_parser("bindings", help="show what every number routes to")
    export = commands.add_parser("export", help="save numbers and bound agent versions")
    export.add_argument("--out", type=Path, required=True)
    for name in ("rebind", "rollback"):
        cmd = commands.add_parser(name)
        cmd.add_argument("number", help="E.164, e.g. +61238211140")
        cmd.add_argument("--apply", action="store_true", help="make the change (default: dry run)")
        cmd.add_argument("--state-dir", type=Path, default=DEFAULT_STATE_DIR)
        if name == "rebind":
            cmd.add_argument("--agent", required=True)
            cmd.add_argument("--version", type=int, required=True)
            cmd.add_argument("--reason", default="manual")
    return parser


def run(argv: list[str], api: Retell, out: TextIO) -> None:
    _dispatch(_parser().parse_args(argv), api, out)


def _dispatch(args: argparse.Namespace, api: Retell, out: TextIO) -> None:
    if args.command == "bindings":
        cmd_bindings(api, out)
    elif args.command == "export":
        cmd_export(api, args.out, out)
    elif args.command == "rebind":
        _rebind(
            api,
            args.number,
            args.agent,
            args.version,
            apply=args.apply,
            state_dir=args.state_dir,
            out=out,
            reason=args.reason,
        )
    else:
        cmd_rollback(api, args.number, apply=args.apply, state_dir=args.state_dir, out=out)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(sys.argv[1:] if argv is None else argv)  # --help needs no key
    key = os.environ.get("RETELL_API_KEY")
    if not key:
        sys.stderr.write("RETELL_API_KEY is not set\n")
        return 2
    try:
        _dispatch(args, Retell(key), sys.stdout)
    except (CliError, RetellError) as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
