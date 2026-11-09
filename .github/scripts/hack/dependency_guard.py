#!/usr/bin/env python3
"""Rejects app repositories whose dependencies could come from anywhere but PyPI.

Reads every requirements file of the app (requirements*.txt and the files they
include with -r or -c) and the whole tree, and refuses:

    - a requirement that names the Pippa SDK (the build installs it itself)
    - a package index or download location (-i, --index-url, --extra-index-url,
      -f, --find-links)
    - a wheel file, a local file or a folder as a requirement
    - a direct URL or VCS requirement (name @ https://..., git+https://...)
    - an included requirements file outside the repository or behind a URL
    - a wheel file committed anywhere in the repository

Each finding is printed as a FAIL line with the fix indented below it, the
format of the app's scripts/check.py, so the check job reports it as a code error.

Exit codes:
    0  nothing found
    1  findings
    2  the scan could not run

Usage:
    dependency_guard.py --app-dir DIR [--package pippa-agentapp-sdk]
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator

SDK_PACKAGE = "pippa-agentapp-sdk"
ROOT_PATTERN = re.compile(r"^requirements.*\.txt$", re.IGNORECASE)
SKIP_DIRS = frozenset({".git"})

INDEX_OPTIONS = ("index-url", "extra-index-url", "find-links")
INDEX_SHORT_OPTIONS = ("-i", "-f")
INCLUDE_OPTIONS = ("requirement", "constraint")
INCLUDE_SHORT_OPTIONS = ("-r", "-c")
EDITABLE_OPTION = "editable"
EDITABLE_SHORT_OPTION = "-e"

ARCHIVE_SUFFIXES = (".whl", ".tar.gz", ".tgz", ".tar.bz2", ".tar.xz", ".zip")
LOCAL_PREFIXES = ("./", "../", ".\\", "..\\", "/", "\\", "~", "file:")
WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")
URL_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
VCS_PREFIX = re.compile(r"^(git|hg|svn|bzr)\+", re.IGNORECASE)

FAIL = " FAIL"
FIX_INDENT = "       "


class GuardError(Exception):
    """The scan could not run."""


@dataclass(frozen=True)
class Finding:
    location: str
    problem: str
    fix: str

    def render(self) -> str:
        return f"{FAIL} {self.location} {self.problem}\n{FIX_INDENT}{self.fix}"


@dataclass(frozen=True)
class RequirementLine:
    path: str
    number: int
    text: str

    @property
    def location(self) -> str:
        return f"{self.path} line {self.number}"

    def tokens(self) -> list[str]:
        lexer = shlex.shlex(self.text, posix=True)
        lexer.whitespace_split = True
        lexer.escape = ""
        lexer.commenters = ""
        try:
            return list(lexer)
        except ValueError:
            return self.text.split()


def normalise(name: str) -> str:
    """PEP 503 normalised form, applied to a whole string."""
    return re.sub(r"[-_.]+", "-", name.lower())


def long_option(token: str) -> str | None:
    """Name of a long option (without dashes and value), or None."""
    if not token.startswith("--") or len(token) == 2:
        return None
    return token[2:].split("=", 1)[0]


def matches_long(name: str | None, options: Iterable[str]) -> bool:
    """pip accepts any unambiguous prefix of a long option, so a prefix counts."""
    return bool(name) and any(option.startswith(name) for option in options)


def matches_short(token: str, options: Iterable[str]) -> bool:
    return not token.startswith("--") and any(token.startswith(option) for option in options)


def option_value(tokens: list[str], index: int, short_options: Iterable[str]) -> str:
    """Value of the option at tokens[index]: attached (--x=v, -xv) or the next token."""
    token = tokens[index]
    if token.startswith("--") and "=" in token:
        return token.split("=", 1)[1]
    for short in short_options:
        if not token.startswith("--") and token.startswith(short) and len(token) > len(short):
            return token[len(short):]
    return tokens[index + 1] if index + 1 < len(tokens) else ""


def option_width(tokens: list[str], index: int, short_options: Iterable[str]) -> int:
    """Number of tokens the option at tokens[index] takes: 1 with an attached value, else 2."""
    token = tokens[index]
    if token.startswith("--"):
        return 1 if "=" in token else 2
    attached = any(token.startswith(short) and len(token) > len(short) for short in short_options)
    return 1 if attached else 2


def is_url(token: str) -> bool:
    """True for any URL or VCS reference (https://, git+https://, git+ssh://, ...)."""
    return bool(URL_SCHEME.match(token) or VCS_PREFIX.match(token))


def is_local_source(token: str) -> bool:
    """True for a wheel, an archive file, a local path or a file: URL."""
    bare = token.split("#", 1)[0].split("?", 1)[0]
    lowered = bare.lower()
    if lowered.endswith(".whl"):
        return True
    if URL_SCHEME.match(bare):
        return lowered.startswith("file:")
    if bare in (".", "..") or bare.startswith(LOCAL_PREFIXES) or WINDOWS_DRIVE.match(bare):
        return True
    if lowered.endswith(ARCHIVE_SUFFIXES):
        return True
    return "/" in bare or "\\" in bare


class LineRule(ABC):
    """A check on one logical line of a requirements file."""

    @abstractmethod
    def check(self, line: RequirementLine) -> Finding | None:
        """Returns a finding, or None when the line passes."""


class SdkPackageRule(LineRule):
    def __init__(self, package: str) -> None:
        self.package = normalise(package)

    def check(self, line: RequirementLine) -> Finding | None:
        if self.package not in normalise(line.text):
            return None
        return Finding(
            line.location,
            "installs the Pippa SDK",
            "The build installs the Pippa SDK itself, at the version your app runs with. "
            "Delete this line.",
        )


class IndexOptionRule(LineRule):
    def check(self, line: RequirementLine) -> Finding | None:
        for token in line.tokens():
            if matches_long(long_option(token), INDEX_OPTIONS) or matches_short(token, INDEX_SHORT_OPTIONS):
                return Finding(
                    line.location,
                    f"sets a package index or download location ({token.split('=', 1)[0][:40]})",
                    "Packages come from PyPI only. Delete this line.",
                )
        return None


def source_tokens(tokens: list[str]) -> Iterator[str]:
    """The tokens of a line that name what to install: requirements and -e values.

    Values of -r/-c are skipped (IncludeResolver follows them) and so are other options.
    """
    index = 0
    while index < len(tokens):
        token = tokens[index]
        name = long_option(token)
        if matches_long(name, INCLUDE_OPTIONS) or matches_short(token, INCLUDE_SHORT_OPTIONS):
            index += option_width(tokens, index, INCLUDE_SHORT_OPTIONS)
            continue
        if matches_long(name, (EDITABLE_OPTION,)) or matches_short(token, (EDITABLE_SHORT_OPTION,)):
            value = option_value(tokens, index, (EDITABLE_SHORT_OPTION,))
            if value:
                yield value
            index += option_width(tokens, index, (EDITABLE_SHORT_OPTION,))
            continue
        if not token.startswith("-"):
            yield token
        index += 1


class SourceRule(LineRule):
    """Refuses a requirement whose source matches `matches`."""

    problem: str

    @abstractmethod
    def matches(self, part: str) -> bool:
        """True when this part of a source token is refused."""

    def check(self, line: RequirementLine) -> Finding | None:
        for token in source_tokens(line.tokens()):
            parts = [token] if is_url(token) else token.split("@")
            if any(part and self.matches(part) for part in parts):
                return Finding(
                    line.location,
                    f"{self.problem} ({token[:80]})",
                    "Name the package as it is published on PyPI, or delete this line.",
                )
        return None


class LocalSourceRule(SourceRule):
    problem = "installs from a file or folder"

    def matches(self, part: str) -> bool:
        return is_local_source(part)


class DirectUrlRule(SourceRule):
    problem = "installs from a URL"

    def matches(self, part: str) -> bool:
        return is_url(part)


class RequirementsReader:
    """Reads a requirements file into logical lines the way pip does."""

    def read(self, path: Path, label: str) -> list[RequirementLine]:
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            raise GuardError(f"{label} could not be read: {exc}") from exc
        lines: list[RequirementLine] = []
        buffer = ""
        start = 0
        for number, physical in enumerate(raw.splitlines(), start=1):
            if not buffer:
                start = number
            stripped = self._strip_comment(physical)
            if stripped.endswith("\\"):
                buffer += stripped[:-1] + " "
                continue
            text = (buffer + stripped).strip()
            buffer = ""
            if text:
                lines.append(RequirementLine(label, start, text))
        if buffer.strip():
            lines.append(RequirementLine(label, start, buffer.strip()))
        return lines

    @staticmethod
    def _strip_comment(line: str) -> str:
        if line.lstrip().startswith("#"):
            return ""
        return re.sub(r"(^|\s+)#.*$", "", line)


class IncludeResolver:
    """Follows -r and -c references inside the repository."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def references(self, line: RequirementLine) -> list[str]:
        tokens = line.tokens()
        found: list[str] = []
        for index, token in enumerate(tokens):
            if matches_long(long_option(token), INCLUDE_OPTIONS) or matches_short(token, INCLUDE_SHORT_OPTIONS):
                value = option_value(tokens, index, INCLUDE_SHORT_OPTIONS)
                if value:
                    found.append(value)
        return found

    def resolve(self, line: RequirementLine, reference: str) -> tuple[Path | None, Finding | None]:
        if URL_SCHEME.match(reference):
            return None, Finding(
                line.location,
                f"includes requirements from a URL ({reference[:80]})",
                "Requirements must be in this repository. Delete this line.",
            )
        base = (self.root / line.path).parent
        target = (base / os.path.expanduser(reference)).resolve()
        if target != self.root and self.root not in target.parents:
            return None, Finding(
                line.location,
                f"includes a file outside the repository ({reference[:80]})",
                "Requirements must be in this repository. Delete this line.",
            )
        return target, None


class TreeRule(ABC):
    """A check on the files of the repository."""

    @abstractmethod
    def scan(self, root: Path) -> list[Finding]:
        """Returns the findings for the tree."""


class WheelFileRule(TreeRule):
    def scan(self, root: Path) -> list[Finding]:
        findings: list[Finding] = []
        for relative in walk_files(root):
            if relative.lower().endswith(".whl"):
                findings.append(Finding(
                    relative,
                    "is a wheel file in the repository",
                    "Wheel files may not be committed, and the build installs the Pippa SDK "
                    f'itself. Delete it (git rm "{relative}") and push again.',
                ))
        return findings


def walk_files(root: Path) -> Iterator[str]:
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(name for name in dirnames if name not in SKIP_DIRS)
        for name in sorted(filenames):
            yield (Path(directory) / name).relative_to(root).as_posix()


class DependencyGuard:
    def __init__(
        self,
        root: Path,
        line_rules: list[LineRule],
        tree_rules: list[TreeRule],
        reader: RequirementsReader,
        resolver: IncludeResolver,
    ) -> None:
        self.root = root
        self.line_rules = line_rules
        self.tree_rules = tree_rules
        self.reader = reader
        self.resolver = resolver

    def scan(self) -> list[Finding]:
        if not self.root.is_dir():
            raise GuardError(f"{self.root} is not a directory")
        findings: list[Finding] = []
        for rule in self.tree_rules:
            findings.extend(rule.scan(self.root))
        findings.extend(self._scan_requirements())
        return findings

    def _roots(self) -> list[str]:
        return [relative for relative in walk_files(self.root)
                if ROOT_PATTERN.match(Path(relative).name) and not (self.root / relative).is_symlink()]

    def _scan_requirements(self) -> list[Finding]:
        findings: list[Finding] = []
        pending = self._roots()
        seen: set[str] = set()
        while pending:
            label = pending.pop(0)
            if label in seen:
                continue
            seen.add(label)
            for line in self.reader.read(self.root / label, label):
                finding = self._check_line(line)
                if finding is not None:
                    findings.append(finding)
                    continue
                for reference in self.resolver.references(line):
                    target, problem = self.resolver.resolve(line, reference)
                    if problem is not None:
                        findings.append(problem)
                    elif target is not None and target.is_file():
                        pending.append(target.relative_to(self.root.resolve()).as_posix())
        return findings

    def _check_line(self, line: RequirementLine) -> Finding | None:
        for rule in self.line_rules:
            finding = rule.check(line)
            if finding is not None:
                return finding
        return None


class GuardFactory:
    @staticmethod
    def create(root: Path, package: str = SDK_PACKAGE) -> DependencyGuard:
        return DependencyGuard(
            root=root,
            line_rules=[SdkPackageRule(package), IndexOptionRule(), LocalSourceRule(), DirectUrlRule()],
            tree_rules=[WheelFileRule()],
            reader=RequirementsReader(),
            resolver=IncludeResolver(root),
        )


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Reject dependencies that do not come from PyPI")
    parser.add_argument("--app-dir", required=True, type=Path)
    parser.add_argument("--package", default=SDK_PACKAGE, help="Name of the Pippa SDK distribution")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        findings = GuardFactory.create(args.app_dir, args.package).scan()
    except GuardError as exc:
        print(f"dependency check failed: {exc}", file=sys.stderr)
        return 2
    for finding in findings:
        print(finding.render())
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
