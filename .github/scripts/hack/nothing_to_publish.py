#!/usr/bin/env python3
"""Says whether a push has nothing to publish yet, from manifest.yaml alone.

A push publishes nothing while manifest.yaml still has

    - the placeholder app id `hack.your_app_name_here`, or
    - an `app_author_name` key (top level) whose value is blank: empty, "", '',
      quotes around spaces only, ~ or null. A manifest without the key is not
      held back: it predates the key, and Pippa says what to add.

Reads the file as text with no YAML library, because it runs before anything
is installed. Prints one paragraph per reason, or nothing when the push can
publish. A missing or unreadable manifest.yaml prints nothing: the check that
follows reports it.

Exit codes:
    0  decided (whatever was printed)
    2  bad usage

Usage:
    nothing_to_publish.py --manifest FILE
"""

from __future__ import annotations

import argparse
import re
import sys
from abc import ABC, abstractmethod
from pathlib import Path

PLACEHOLDER_ID = re.compile(r"^[ \t]*id:[ \t]*hack\.your_app_name_here[ \t]*$", re.MULTILINE)
AUTHOR_KEY = re.compile(r"""^(?:app_author_name|"app_author_name"|'app_author_name')[ \t]*:(?P<rest>.*)$""")
NULL_VALUES = frozenset({"", "~", "null", "Null", "NULL"})
BLOCK_INDICATOR = re.compile(r"^[|>][-+0-9]*$")

PLACEHOLDER_REASON = (
    "manifest.yaml still has the placeholder app id hack.your_app_name_here. "
    "The next push with a real app id publishes."
)
AUTHOR_REASON = (
    "Nothing to publish yet: choose your public author name (app_author_name in manifest.yaml). "
    "Claude will ask you for it."
)


def strip_comment(text: str) -> str:
    """A plain YAML value without its comment: `#` at the start or after a space."""
    return re.sub(r"(^|[ \t])#.*$", "", text).strip()


def quoted_value(text: str) -> str | None:
    """The content of a quoted scalar at the start of text, or None when it is not quoted."""
    text = text.strip()
    if text[:1] == "'":
        match = re.match(r"'((?:[^']|'')*)'", text)
        return match.group(1).replace("''", "'") if match else None
    if text[:1] == '"':
        match = re.match(r'"((?:[^"\\]|\\.)*)"', text)
        return match.group(1) if match else None
    return None


def is_indented(line: str) -> bool:
    return line[:1] in (" ", "\t")


def is_blank_or_comment(line: str) -> bool:
    stripped = line.strip()
    return not stripped or stripped.startswith("#")


class Reason(ABC):
    """One reason a push has nothing to publish yet."""

    text: str

    @abstractmethod
    def applies(self, manifest: str) -> bool:
        """True when the manifest is held back for this reason."""


class PlaceholderAppId(Reason):
    text = PLACEHOLDER_REASON

    def applies(self, manifest: str) -> bool:
        return bool(PLACEHOLDER_ID.search(manifest))


class BlankAuthorName(Reason):
    """The top-level `app_author_name` is present and blank; the last one counts, as in YAML."""

    text = AUTHOR_REASON

    def applies(self, manifest: str) -> bool:
        lines = manifest.splitlines()
        blank: bool | None = None
        for index, line in enumerate(lines):
            match = AUTHOR_KEY.match(line)
            if match:
                blank = self._blank(match.group("rest"), lines[index + 1:])
        return bool(blank)

    def _blank(self, rest: str, following: list[str]) -> bool:
        quoted = quoted_value(rest)
        if quoted is not None:
            return not quoted.strip()
        value = strip_comment(rest)
        if BLOCK_INDICATOR.match(value):
            return not any(line.strip() for line in self._continuation(following))
        if value == "":
            # `app_author_name:` with the value on the next, indented line(s).
            return not any(not is_blank_or_comment(line) for line in self._continuation(following))
        return value in NULL_VALUES

    @staticmethod
    def _continuation(following: list[str]) -> list[str]:
        """The indented lines (and blank lines between them) right after the key."""
        found: list[str] = []
        for line in following:
            if line.strip() and not is_indented(line):
                break
            found.append(line)
        return found


REASONS: tuple[Reason, ...] = (PlaceholderAppId(), BlankAuthorName())


def reasons_for(manifest: str) -> list[str]:
    return [reason.text for reason in REASONS if reason.applies(manifest)]


def read_manifest(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Say whether a push has nothing to publish yet")
    parser.add_argument("--manifest", required=True, type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    reasons = reasons_for(read_manifest(args.manifest))
    if reasons:
        print("\n\n".join(reasons))
    return 0


if __name__ == "__main__":
    sys.exit(main())
