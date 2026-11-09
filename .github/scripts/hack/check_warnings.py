#!/usr/bin/env python3
"""Summarises the non-blocking findings of a passed check for the commit comment.

Reads the log of the app's scripts/check.py (its WARN lines and the fix under
each) and the log of scripts/routing_check.py (its FAIL and WARN lines, the fix
under each and its closing count). Nothing here changes the verdict: a routing
check that crashed, timed out or printed nothing recognisable becomes one more
warning. Prints the summary, or nothing when there is nothing to say.

Usage:
    check_warnings.py --check-log FILE --routing-log FILE --routing-status N
"""

from __future__ import annotations

import argparse
import re
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

ANSI = re.compile(r"\x1b\[[0-9;]*m")
FIX_PREFIX = "       "
TIMEOUT_STATUS = 124
SUMMARY_LIMIT = 3000
ROUTING_SCRIPT = "scripts/routing_check.py"
CHECK_SCRIPT = "scripts/check.py"
ROUTING_CLOSING = re.compile(
    r"^(\d+ problem\(s\) that will stop your app being chosen\.|\d+ warning\(s\)[ .].*"
    r"|Nothing here will stop your app being chosen\.)$"
)


class SummaryError(Exception):
    """A log could not be read."""


@dataclass
class Section:
    title: str
    lines: list[str] = field(default_factory=list)

    def render(self) -> str:
        return "\n".join([self.title, *self.lines])


def read_log(path: Path | None) -> list[str]:
    if path is None:
        return []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise SummaryError(f"{path} could not be read: {exc}") from exc
    return [ANSI.sub("", line).rstrip() for line in text.splitlines()]


def blocks(lines: list[str], markers: tuple[str, ...]) -> list[list[str]]:
    """Lines starting with one of the markers, each with the fix lines indented under it."""
    found: list[list[str]] = []
    current: list[str] | None = None
    for line in lines:
        if line.startswith(markers):
            current = [line]
            found.append(current)
        elif current is not None and line.startswith(FIX_PREFIX) and line.strip():
            current.append(line)
        else:
            current = None
    return found


class WarningSource(ABC):
    @abstractmethod
    def section(self) -> Section | None:
        """The section for the comment, or None when the source has nothing to say."""


class CheckLogSource(WarningSource):
    """WARN blocks of scripts/check.py; it prints each warning twice, the first time with its fix."""

    def __init__(self, lines: list[str]) -> None:
        self.lines = lines

    def section(self) -> Section | None:
        seen: set[str] = set()
        section = Section(CHECK_SCRIPT)
        for block in blocks(self.lines, (" WARN ",)):
            if block[0] in seen:
                continue
            seen.add(block[0])
            section.lines.extend(block)
        return section if section.lines else None


class RoutingLogSource(WarningSource):
    def __init__(self, lines: list[str], status: int) -> None:
        self.lines = lines
        self.status = status

    def section(self) -> Section | None:
        section = Section(ROUTING_SCRIPT)
        closing = [line.strip() for line in self.lines if ROUTING_CLOSING.match(line.strip())]
        if self.status == TIMEOUT_STATUS:
            section.lines.append(" WARN the routing check did not finish in time; routing was not checked.")
            return section
        if self.status not in (0, 1) or not closing:
            section.lines.append(
                f" WARN the routing check could not run (exit {self.status}); routing was not checked."
            )
            last = next((line.strip() for line in reversed(self.lines) if line.strip()), "")
            if last:
                section.lines.append(f"{FIX_PREFIX}{last[:200]}")
            return section
        for block in blocks(self.lines, (" FAIL ", " WARN ")):
            section.lines.extend(block)
        if not section.lines:
            return None
        section.lines.append(closing[-1])
        return section


class WarningsReport:
    def __init__(self, sources: list[WarningSource], limit: int = SUMMARY_LIMIT) -> None:
        self.sources = sources
        self.limit = limit

    def render(self) -> str:
        sections = [section for source in self.sources if (section := source.section()) is not None]
        text = "\n\n".join(section.render() for section in sections)
        if len(text) > self.limit:
            text = text[: self.limit - 4].rstrip() + "\n..."
        return text


def report_for(args: argparse.Namespace) -> WarningsReport:
    return WarningsReport([
        CheckLogSource(read_log(args.check_log)),
        RoutingLogSource(read_log(args.routing_log), args.routing_status),
    ])


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Summarise the non-blocking findings of a passed check")
    parser.add_argument("--check-log", type=Path, default=None)
    parser.add_argument("--routing-log", type=Path, default=None)
    parser.add_argument("--routing-status", type=int, required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        text = report_for(args).render()
    except SummaryError as exc:
        print(f"warnings could not be summarised: {exc}", file=sys.stderr)
        return 2
    if text:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
