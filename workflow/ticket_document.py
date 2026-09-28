"""Parsing and rendering for the deliberately small Markdown ticket format."""

from __future__ import annotations

import re
from dataclasses import dataclass


TICKET_ID_RE = re.compile(r"^TICKET-[0-9]{3,}$")
HEADING_RE = re.compile(r"^## (Objective|Requirements|Acceptance Criteria|Constraints|Dependencies)\s*$")
CANONICAL_TITLE_RE = re.compile(r"^# (TICKET-[0-9]{3,}): (.+?)\s*$")
DRAFT_TITLE_RE = re.compile(r"^# (.+?)\s*$")
SECTIONS = ("Objective", "Requirements", "Acceptance Criteria", "Constraints", "Dependencies")


class TicketDocumentError(ValueError):
    pass


@dataclass(frozen=True)
class TicketDocument:
    title: str
    sections: dict[str, str]
    dependencies: tuple[str, ...]
    ticket_id: str | None = None


def validate_ticket_id(ticket_id: str) -> None:
    if not TICKET_ID_RE.fullmatch(ticket_id):
        raise TicketDocumentError(
            f"Invalid ticket ID {ticket_id!r}; expected TICKET- followed by at least three digits"
        )


def parse_document(text: str, *, expect_canonical: bool) -> TicketDocument:
    lines = text.splitlines()
    if not lines:
        raise TicketDocumentError("Ticket document is empty")

    ticket_id = None
    if expect_canonical:
        title_match = CANONICAL_TITLE_RE.fullmatch(lines[0])
        if not title_match:
            raise TicketDocumentError("Canonical ticket title must be '# TICKET-NNN: Title'")
        ticket_id, title = title_match.groups()
        validate_ticket_id(ticket_id)
    else:
        title_match = DRAFT_TITLE_RE.fullmatch(lines[0])
        if not title_match or CANONICAL_TITLE_RE.fullmatch(lines[0]):
            raise TicketDocumentError("Draft title must be '# Title' and must not contain a ticket ID")
        title = title_match.group(1)
    title = title.strip()
    if not title:
        raise TicketDocumentError("Ticket title must not be empty")

    found: dict[str, list[str]] = {}
    current: str | None = None
    fence: str | None = None
    for line in lines[1:]:
        stripped = line.lstrip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            marker = stripped[:3]
            if fence is None:
                fence = marker
            elif fence == marker:
                fence = None
            if current is not None:
                found[current].append(line)
            else:
                raise TicketDocumentError("Content before the first required section is not allowed")
            continue
        heading = HEADING_RE.fullmatch(line) if fence is None else None
        if heading:
            current = heading.group(1)
            if current in found:
                raise TicketDocumentError(f"Duplicate section: {current}")
            found[current] = []
        elif current is not None:
            found[current].append(line)
        elif line.strip():
            raise TicketDocumentError("Content before the first required section is not allowed")

    missing = [section for section in SECTIONS if section not in found]
    if missing:
        raise TicketDocumentError(f"Missing required section(s): {', '.join(missing)}")

    sections = {name: "\n".join(found[name]).strip() for name in SECTIONS}
    for required in ("Objective", "Requirements", "Acceptance Criteria"):
        if not sections[required]:
            raise TicketDocumentError(f"Section must not be empty: {required}")

    dependencies = _parse_dependencies(sections["Dependencies"])
    return TicketDocument(title=title, sections=sections, dependencies=dependencies, ticket_id=ticket_id)


def _parse_dependencies(value: str) -> tuple[str, ...]:
    if not value or value.casefold() == "none":
        return ()

    dependencies: list[str] = []
    for line in value.splitlines():
        match = re.fullmatch(r"\s*-\s+(TICKET-[0-9]+)\s*", line)
        if not match:
            raise TicketDocumentError(
                "Dependencies must be 'None' or a bullet list containing only ticket IDs"
            )
        dependency = match.group(1)
        validate_ticket_id(dependency)
        if dependency in dependencies:
            raise TicketDocumentError(f"Duplicate dependency: {dependency}")
        dependencies.append(dependency)
    return tuple(dependencies)


def render_document(ticket_id: str, document: TicketDocument) -> str:
    validate_ticket_id(ticket_id)
    parts = [f"# {ticket_id}: {document.title}"]
    for section in SECTIONS:
        value = document.sections[section]
        if section in ("Constraints", "Dependencies") and not value:
            value = "None"
        parts.extend(("", f"## {section}", "", value))
    return "\n".join(parts).rstrip() + "\n"
