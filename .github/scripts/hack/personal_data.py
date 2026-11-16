#!/usr/bin/env python3
"""Refuses personal data in an app repository. Runs before any app code.

Two checks:

    files     every file of the repository except binary files and files over
              1 MB. An email address is refused, unless it is a GitHub
              no-reply address (...@users.noreply.github.com) or at a domain
              reserved for examples: example.com, example.org, example.net
              (and their subdomains), *.example, *.test, *.invalid, localhost.
    commits   the commits being pushed (from the push event; see
              PushedCommits). An author or committer email that is not a
              GitHub no-reply address is refused in a public repository,
              where everybody can read it for good, and is a warning in a
              private one. The commits are checked only when the run has an
              event to read (GITHUB_EVENT_NAME), that is in GitHub Actions.

An address is never printed, only its first character and its domain
(a***@example.org). Refusals go to stdout as FAIL lines, the format of the
app's scripts/check.py; warnings go to --warnings-file as WARN lines (stdout
without it).

Exit codes:
    0  nothing refused (there may be warnings)
    1  refused
    2  the scan could not run

Usage:
    personal_data.py --app-dir DIR [--warnings-file FILE]
                     [--event-name NAME] [--event-path FILE] [--sha SHA]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

MAX_FILE_BYTES = 1024 * 1024
BINARY_SNIFF_BYTES = 8000
MAX_COMMITS = 500
MAX_LISTED = 40
SHORT_SHA = 7
SKIP_DIRS = frozenset({".git"})

EMAIL = re.compile(
    r"(?<![A-Za-z0-9._%+-])"
    r"(?P<local>[A-Za-z0-9._%+-]+)@"
    r"(?P<domain>(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+(?P<tld>[A-Za-z]{2,63}))"
    r"(?![A-Za-z0-9-])"
)
#: File name endings that look like a top-level domain after an @, as in icon@2x.png.
FILE_SUFFIXES = frozenset({
    "png", "jpg", "jpeg", "gif", "svg", "webp", "ico", "bmp", "tif", "tiff", "avif", "heic",
    "css", "scss", "js", "mjs", "cjs", "ts", "tsx", "jsx", "json", "map", "lock", "whl",
    "yaml", "yml", "toml", "txt", "html", "htm", "xml", "csv",
})
NOREPLY_DOMAIN = "users.noreply.github.com"
#: The committer of commits made on github.com (web-flow).
GITHUB_NOREPLY = "noreply@github.com"
EXAMPLE_DOMAINS = ("example.com", "example.org", "example.net")
RESERVED_TLDS = ("example", "test", "invalid", "localhost")
ZERO_SHA = re.compile(r"^0+$")

FAIL = " FAIL"
WARN = " WARN"
FIX_INDENT = "       "

FILE_FIX = (
    "Remove it: nothing personal belongs in this repository. "
    "For an example or a test, use an address at example.com."
)
PUBLIC_COMMIT = (
    "Your commit shows the email address {address} publicly. Set `git config user.email` "
    "to your GitHub no-reply address and amend the commit."
)
PRIVATE_COMMIT = (
    "This commit shows the email address {address} to everybody who can read this repository."
)
PRIVATE_COMMIT_FIX = (
    "Set `git config user.email` to your GitHub no-reply address for your next commits."
)


class ScanError(Exception):
    """The scan could not run. The message never contains an address."""


def mask(address: str) -> str:
    """a***@domain, or a*** for a value without @. The rest of the local part never shows."""
    local, at, domain = address.strip().partition("@")
    first = local[:1] if local[:1].isprintable() and local[:1] else "?"
    if not at:
        return f"{first}***"
    shown = "".join(char for char in domain if char.isprintable() and not char.isspace())[:100]
    return f"{first}***@{shown}"


def allowed_in_files(domain: str) -> bool:
    domain = domain.lower().rstrip(".")
    if domain == NOREPLY_DOMAIN:
        return True
    if any(domain == name or domain.endswith(f".{name}") for name in EXAMPLE_DOMAINS):
        return True
    return any(domain == tld or domain.endswith(f".{tld}") for tld in RESERVED_TLDS)


def is_github_noreply(email: str) -> bool:
    email = email.strip().lower()
    return email.endswith(f"@{NOREPLY_DOMAIN}") or email == GITHUB_NOREPLY


def addresses(text: str) -> Iterator[str]:
    """The email addresses in text that are not allowed in a file."""
    for match in EMAIL.finditer(text):
        if match.group("tld").lower() in FILE_SUFFIXES:
            continue
        if allowed_in_files(match.group("domain")):
            continue
        yield match.group(0)


@dataclass(frozen=True)
class Finding:
    location: str
    problem: str
    fix: str = ""

    def render(self, marker: str) -> str:
        line = f"{marker} {self.location} {self.problem}"
        return f"{line}\n{FIX_INDENT}{self.fix}" if self.fix else line


@dataclass
class Report:
    failures: list[Finding] = field(default_factory=list)
    warnings: list[Finding] = field(default_factory=list)


# ---- files ----

def walk_files(root: Path) -> Iterator[str]:
    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(name for name in dirnames if name not in SKIP_DIRS)
        for name in sorted(filenames):
            yield (Path(directory) / name).relative_to(root).as_posix()


class FileScan:
    """Email addresses in the repository's text files."""

    def __init__(self, root: Path, files: list[str]) -> None:
        self.root = root
        self.files = files

    def findings(self) -> list[Finding]:
        found: list[Finding] = []
        for relative in self.files:
            text = self._text(relative)
            if text is None:
                continue
            for number, line in enumerate(text.split("\n"), start=1):
                for shown in dict.fromkeys(mask(address) for address in addresses(line)):
                    found.append(Finding(f"{relative}:{number}", f"contains the email address {shown}"))
        return found

    def _text(self, relative: str) -> str | None:
        """The file as text, or None for a link, a binary file or one over the size limit."""
        path = self.root / relative
        if path.is_symlink() or not path.is_file():
            return None
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                return None
            data = path.read_bytes()
        except OSError as exc:
            raise ScanError(f"{relative} could not be read: {exc.strerror}") from exc
        if b"\0" in data[:BINARY_SNIFF_BYTES]:
            return None
        return data.decode("utf-8", errors="replace")


# ---- commits ----

@dataclass(frozen=True)
class Commit:
    sha: str
    author_email: str
    committer_email: str

    def exposed(self) -> dict[str, list[str]]:
        """Addresses that are not GitHub no-reply addresses, each with the roles it has here."""
        roles: dict[str, list[str]] = {}
        for role, email in (("author", self.author_email), ("committer", self.committer_email)):
            if email.strip() and not is_github_noreply(email):
                roles.setdefault(email.strip(), []).append(role)
        return roles


class Git:
    """The git commands the scan needs, run in the repository."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def _run(self, args: list[str], stdin: str | None = None) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                ["git", "-C", str(self.root), *args], input=stdin, capture_output=True,
                text=True, encoding="utf-8", errors="replace", check=False,
            )
        except OSError as exc:
            raise ScanError(f"git could not run: {exc.strerror}") from exc

    def output(self, args: list[str], stdin: str | None = None) -> str:
        result = self._run(args, stdin)
        if result.returncode != 0:
            raise ScanError(f"git {args[0]} failed (exit {result.returncode})")
        return result.stdout

    def is_repository(self) -> bool:
        return self._run(["rev-parse", "--is-inside-work-tree"]).returncode == 0

    def has_commit(self, sha: str) -> bool:
        return bool(sha) and self._run(["cat-file", "-e", f"{sha}^{{commit}}"]).returncode == 0

    def rev_list(self, head: str, exclude: str | None = None) -> list[str]:
        args = ["rev-list", f"--max-count={MAX_COMMITS}", head]
        if exclude:
            args.append(f"^{exclude}")
        return self.output(args).split()

    def commits(self, shas: list[str]) -> list[Commit]:
        if not shas:
            return []
        out = self.output(["log", "--no-walk=unsorted", "--stdin", "--format=%H%x1f%ae%x1f%ce"],
                          stdin="\n".join(shas) + "\n")
        found = []
        for line in out.splitlines():
            parts = line.split("\x1f")
            if len(parts) == 3:
                found.append(Commit(*parts))
        return found

    def tracked_files(self) -> list[str]:
        return [name for name in self.output(["ls-files", "-z"]).split("\0") if name]


def payload_commit(item: Any) -> Commit | None:
    """A commit as the push event lists it, for one this clone does not have."""
    if not isinstance(item, dict) or not isinstance(item.get("id"), str):
        return None
    people = [item.get(role) if isinstance(item.get(role), dict) else {} for role in ("author", "committer")]
    emails = [str(person.get("email") or "") for person in people]
    return Commit(item["id"], emails[0], emails[1])


class PushedCommits:
    """The commits a run publishes for the first time.

    push, with a `before` this clone has  -> the commits from `before` to the pushed
                                             commit: a normal push, and a force push
                                             whose old tip is still in the clone
    push, with a `before` it does not have -> the commits the event lists (a force
                                             push over a commit no branch keeps),
                                             else just the pushed commit
    push that created the branch           -> the commits the event lists, else the
                                             branch's whole history (first push)
    anything else (workflow_dispatch)      -> the commit being published
    At most MAX_COMMITS from git.
    """

    def __init__(self, git: Git, event_name: str, event: dict[str, Any], sha: str) -> None:
        self.git = git
        self.event_name = event_name
        self.event = event
        self.head = sha or str(event.get("after") or "") or "HEAD"

    def commits(self) -> list[Commit]:
        if not self.git.has_commit(self.head):
            raise ScanError("the pushed commit is not in the checkout")
        if self.event_name != "push":
            return self.git.commits([self.head])
        before = str(self.event.get("before") or "")
        created = not before or bool(ZERO_SHA.match(before))
        if not created and self.git.has_commit(before):
            return self.git.commits(self.git.rev_list(self.head, exclude=before))
        listed = self._listed()
        if listed:
            return listed
        if created:
            return self.git.commits(self.git.rev_list(self.head))
        return self.git.commits([self.head])

    def _listed(self) -> list[Commit]:
        items = self.event.get("commits")
        if not isinstance(items, list):
            return []
        local: list[str] = []
        remote: list[Commit] = []
        for item in items:
            commit = payload_commit(item)
            if commit is None:
                continue
            if self.git.has_commit(commit.sha):
                local.append(commit.sha)
            else:
                remote.append(commit)
        if not local and not remote:
            return []
        if self.head not in local:
            local.append(self.head)
        return self.git.commits(list(dict.fromkeys(local))) + remote


def repository_is_public(event: dict[str, Any]) -> bool:
    """From the event's repository; a repository the event does not describe counts as public."""
    repository = event.get("repository")
    if not isinstance(repository, dict):
        return True
    if isinstance(repository.get("private"), bool):
        return not repository["private"]
    return repository.get("visibility") not in ("private", "internal")


class CommitScan:
    def __init__(self, commits: list[Commit], public: bool) -> None:
        self.commits = commits
        self.public = public

    def report(self) -> Report:
        grouped: dict[str, list[tuple[str, list[str]]]] = {}
        for commit in self.commits:
            for email, roles in commit.exposed().items():
                grouped.setdefault(email.lower(), []).append((commit.sha, roles))
        report = Report()
        for email, uses in grouped.items():
            location = self._location(uses)
            address = mask(email)
            if self.public:
                report.failures.append(Finding(location, PUBLIC_COMMIT.format(address=address)))
            else:
                report.warnings.append(Finding(location, PRIVATE_COMMIT.format(address=address), PRIVATE_COMMIT_FIX))
        return report

    @staticmethod
    def _location(uses: list[tuple[str, list[str]]]) -> str:
        roles = sorted({role for _, used in uses for role in used})
        shas = [sha[:SHORT_SHA] for sha, _ in uses]
        listed = ", ".join(shas[:3]) + (f" and {len(shas) - 3} more" if len(shas) > 3 else "")
        noun = "commit" if len(shas) == 1 else "commits"
        return f"{noun} {listed} ({' and '.join(roles)}):"


# ---- the scan ----

@dataclass(frozen=True)
class Push:
    event_name: str
    event: dict[str, Any]
    sha: str


def load_event(path: str) -> dict[str, Any]:
    if not path:
        return {}
    try:
        event = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ScanError(f"the event file could not be read: {type(exc).__name__}") from exc
    if not isinstance(event, dict):
        raise ScanError("the event file is not an object")
    return event


class PersonalDataScan:
    def __init__(self, root: Path, push: Push | None) -> None:
        self.root = root
        self.push = push
        self.git = Git(root)

    def run(self) -> Report:
        """Commit findings first: they are about what is already public; then the files."""
        if not self.root.is_dir():
            raise ScanError(f"{self.root} is not a directory")
        repository = self.git.is_repository()
        files = self.git.tracked_files() if repository else list(walk_files(self.root))
        report = Report()
        if self.push is not None:
            if not repository:
                raise ScanError(f"{self.root} is not a git repository, so the pushed commits cannot be checked")
            commits = PushedCommits(self.git, self.push.event_name, self.push.event, self.push.sha).commits()
            public = repository_is_public(self.push.event)
            report = CommitScan(commits, public).report()
            print(f"Checked {len(files)} file(s) and {len(commits)} commit(s) of a "
                  f"{'public' if public else 'private'} repository", file=sys.stderr)
        else:
            print(f"Checked {len(files)} file(s); no push event, so no commits", file=sys.stderr)
        in_files = FileScan(self.root, files).findings()
        if in_files:
            last = in_files[-1]
            in_files[-1] = Finding(last.location, last.problem, FILE_FIX)
        report.failures.extend(in_files)
        return report


def render(findings: list[Finding], marker: str) -> str:
    lines = [finding.render(marker) for finding in findings[:MAX_LISTED]]
    if len(findings) > MAX_LISTED:
        hidden = findings[MAX_LISTED:]
        lines.append(f"{marker} {len(hidden)} more not listed")
        fix = next((finding.fix for finding in reversed(hidden) if finding.fix), "")
        if fix and not any(finding.fix == fix for finding in findings[:MAX_LISTED]):
            lines.append(f"{FIX_INDENT}{fix}")
    return "\n".join(lines)


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Refuse personal data in an app repository")
    parser.add_argument("--app-dir", required=True, type=Path)
    parser.add_argument("--warnings-file", type=Path, default=None)
    parser.add_argument("--event-name", default=os.environ.get("GITHUB_EVENT_NAME", ""))
    parser.add_argument("--event-path", default=os.environ.get("GITHUB_EVENT_PATH", ""))
    parser.add_argument("--sha", default=os.environ.get("GITHUB_SHA", ""))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        push = Push(args.event_name, load_event(args.event_path), args.sha) if args.event_name else None
        report = PersonalDataScan(args.app_dir, push).run()
    except ScanError as exc:
        print(f"personal data check failed: {exc}", file=sys.stderr)
        return 2
    warnings = render(report.warnings, WARN)
    if warnings:
        if args.warnings_file is not None:
            args.warnings_file.write_text(warnings + "\n", encoding="utf-8")
        else:
            print(warnings)
    if report.failures:
        print(render(report.failures, FAIL))
    return 1 if report.failures else 0


if __name__ == "__main__":
    sys.exit(main())
