"""Command-line interface for the workflow service."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Callable

from .service import WorkflowError, WorkflowService


class WorkflowArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise WorkflowError("USAGE_ERROR", message)


def build_parser() -> argparse.ArgumentParser:
    parser = WorkflowArgumentParser(prog="tickets", description="Manage the local ticket workflow")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument("--root", type=Path, help=argparse.SUPPRESS)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("init", help="initialize the workflow database")

    create = subparsers.add_parser("create", help="create a backlog ticket from a Markdown draft")
    create.add_argument("--file", type=Path, required=True)
    create.add_argument("--agent", required=True)
    create.add_argument("--priority", default="NORMAL")

    listing = subparsers.add_parser("list", help="list tickets")
    listing.add_argument("--status")
    listing.add_argument("--assigned-to")

    for command in ("show", "history"):
        child = subparsers.add_parser(command)
        child.add_argument("ticket_id")

    transitions: dict[str, tuple[bool, bool]] = {
        "ready": (False, True),
        "start": (False, True),
        "submit": (False, True),
        "test-pass": (False, True),
        "test-fail": (True, True),
        "approve": (False, True),
        "reject": (True, True),
        "block": (True, True),
        "unblock": (False, True),
    }
    for command, (message_required, agent_required) in transitions.items():
        child = subparsers.add_parser(command)
        child.add_argument("ticket_id")
        if agent_required:
            child.add_argument("--agent", required=True)
        child.add_argument("--message", required=message_required)
    return parser


def main(argv: list[str] | None = None, *, default_root: Path | None = None) -> int:
    arguments = list(argv) if argv is not None else sys.argv[1:]
    as_json = "--json" in arguments
    try:
        parser = build_parser()
        args = parser.parse_args(arguments)
    except WorkflowError as error:
        _print_error(error, as_json)
        return 2
    root = args.root or default_root or Path.cwd()
    service = WorkflowService(root)
    try:
        result = _dispatch(service, args)
        _print_success(args.command, result, args.json)
        return 0
    except WorkflowError as error:
        _print_error(error, args.json)
        return 1
    except (OSError, UnicodeError, sqlite3.Error) as error:
        wrapped = WorkflowError("INTERNAL_ERROR", str(error))
        _print_error(wrapped, args.json)
        return 1


def _dispatch(service: WorkflowService, args: argparse.Namespace) -> Any:
    if args.command == "init":
        service.init()
        return {"initialized": True, "database": str(service.database_path)}
    if args.command == "create":
        return service.create(args.file, args.agent, args.priority)
    if args.command == "list":
        return service.list(args.status, args.assigned_to)
    if args.command == "show":
        return service.get(args.ticket_id)
    if args.command == "history":
        return service.history(args.ticket_id)

    method_name = args.command.replace("-", "_")
    method: Callable[..., Any] = getattr(service, method_name)
    return method(args.ticket_id, args.agent, args.message)


def _print_success(command: str, result: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps({"ok": True, "result": result}, indent=2))
        return
    if command == "list":
        if not result:
            print("No tickets found.")
            return
        print("TICKET ID   STATUS        ASSIGNED TO       PRIORITY  ATTEMPTS")
        for ticket in result:
            print(
                f"{ticket['ticket_id']:<11} {ticket['status']:<13} "
                f"{(ticket['assigned_to'] or '-'):<17} {ticket['priority']:<9} {ticket['attempt_count']}"
            )
    elif command == "history":
        for event in result:
            message = f" - {event['message']}" if event["message"] else ""
            print(f"{event['created_at']} {event['agent']} {event['event']}{message}")
    elif command == "show":
        print(
            f"{result['ticket_id']} [{result['status']}] priority={result['priority']} "
            f"assigned_to={result['assigned_to'] or '-'} attempts={result['attempt_count']}"
        )
        print()
        print(result["content"], end="")
    elif command == "init":
        print(f"Initialized {result['database']}")
    else:
        print(f"{result['ticket_id']} -> {result['status']}")


def _print_error(error: WorkflowError, as_json: bool) -> None:
    if as_json:
        print(json.dumps({"ok": False, "error": error.as_dict()}, indent=2), file=sys.stderr)
    else:
        print(f"error [{error.code}]: {error.message}", file=sys.stderr)
