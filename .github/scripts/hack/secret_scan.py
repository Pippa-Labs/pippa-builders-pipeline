#!/usr/bin/env python3
"""Refuses a push that commits a value that looks like a key. Runs before any app code.

gitleaks, a pinned release whose SHA-256 is checked before it ever runs, scans:

    every run                            -> the files of the checkout: what would be published
    push with a `before` this clone has  -> also the pushed commits (before..after)

The files are scanned every time, so a key that is still in the app fails every run until it
is taken out: pushing again, or a run cancelled by a newer push, never lets it through, and
neither does a key committed before this check existed. The pushed commits are scanned too,
because a key that was added and removed again within one push is still in the history.

The scans use gitleaks' own rules and the allowlists in gitleaks.toml next to this file
(.env.example, provider demo keys such as NASA's DEMO_KEY, obvious placeholders,
lines marked `pragma: allowlist secret`). The app adds its own allowlists in
.gitleaks.toml at the top of its repository: only its [allowlist] and
[[allowlists]] tables are used, never rules, and an allowlist with an entry that
would skip nearly every key (a path pattern that matches app.py, a pattern that
matches an ordinary key, a stopword shorter than 4 characters, or a pattern that
cannot be checked) is left out with a note. So an app can skip a false positive but
not switch the check off. gitleaks also skips the fingerprints listed in the app's
.gitleaksignore and every line that ends in a `gitleaks:allow` comment.

A finding never shows the value: gitleaks runs with --redact, and only the file,
the line, the commit and the rule's name are read from its report (never the
match, the secret or the author). Findings go to stdout as FAIL lines, the
format of the app's scripts/check.py.

Exit codes:
    0  nothing found
    1  refused
    2  the scan could not run

Usage:
    secret_scan.py --app-dir DIR --work-dir DIR [--gitleaks FILE]
                   [--event-name NAME] [--event-path FILE] [--sha SHA]
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import time
import tomllib
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

GITLEAKS_VERSION = "8.30.1"
GITLEAKS_URL = (
    "https://github.com/gitleaks/gitleaks/releases/download/"
    "v8.30.1/gitleaks_8.30.1_linux_x64.tar.gz"
)
#: From the release's gitleaks_8.30.1_checksums.txt, checked against a download on 2026-11-17.
GITLEAKS_SHA256 = "551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb"
GITLEAKS_MEMBER = "gitleaks"
MAX_DOWNLOAD_BYTES = 64 * 1024 * 1024
DOWNLOAD_TIMEOUT_SECONDS = 60
DOWNLOAD_ATTEMPTS = 3
DOWNLOAD_BACKOFF_SECONDS = 2.0

BASE_CONFIG = Path(__file__).resolve().with_name("gitleaks.toml")
APP_CONFIG = ".gitleaks.toml"
APP_IGNORE = ".gitleaksignore"
MAX_APP_CONFIG_BYTES = 64 * 1024
MAX_APP_ALLOWLISTS = 20
MAX_APP_LIST_ITEMS = 100
MAX_APP_VALUE_CHARS = 500
#: The parts of an allowlist an app may set; lists of strings unless named in APP_ALLOWLIST_CHOICES.
APP_ALLOWLIST_LISTS = ("paths", "regexes", "stopwords", "commits", "targetRules")
APP_ALLOWLIST_CHOICES = {
    "condition": ("AND", "OR"),
    "regexTarget": ("secret", "match", "line"),
}
#: An allowlist with none of these allows nothing; gitleaks refuses such a table.
APP_ALLOWLIST_CHECKS = ("paths", "regexes", "stopwords", "commits")
#: Files every app has, or could hide a key in: a `paths` pattern that matches one skips too much.
BROAD_PATH_SAMPLES = ("x", "app.py", "manifest.yaml", "config.py", "src/app/settings.py", "data/keys.json", ".env")
#: Ordinary keys, and lines that hold one: a `regexes` pattern or a stopword that matches one skips
#: too much, whatever its regexTarget. Built from pieces so that this file is not a finding itself.
_SAMPLE_KEY = "Zx81" + "Qp0vLm7T" + "nR4wYc2K" + "bH9sJd6F"
_SAMPLE_TOKEN = "gh" + "p_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
_SAMPLE_PREFIXED = "s" + "k-" + "proj-" + "Tq7Wm2Xc9Lb4Vn8Rf1Hz6Kd3"
BROAD_SECRET_SAMPLES = (
    "x", _SAMPLE_KEY, _SAMPLE_TOKEN, _SAMPLE_PREFIXED,
    f'api_key = "{_SAMPLE_KEY}"', f'TOKEN = "{_SAMPLE_TOKEN}"', f"OPENAI_API_KEY={_SAMPLE_PREFIXED}",
)
#: A stopword skips every finding that contains it, so a short one skips nearly all of them.
MIN_STOPWORD_CHARS = 4

SCAN_TIMEOUT_SECONDS = 240
#: gitleaks' exit code when it found something (--exit-code); any other non-zero code is an error.
LEAKS_EXIT = 3
#: gitleaks skips a larger file without a word. The publisher refuses an app whose files add up
#: to more than this (its MAX_UNPACKED_BYTES), so a file too large to scan is never published.
MAX_TARGET_MEGABYTES = 32
GENERIC_RULE = "generic-api-key"

SHA = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
ZERO_SHA = re.compile(r"^0+$")
SHORT_SHA = 7
MAX_LISTED = 40
MAX_SHOWN_CHARS = 200

FAIL = " FAIL"
FIX_INDENT = "       "
FINDING = "a value that looks like a key was committed in {location}; remove it, rotate the key, use app_secrets"
FIX = (
    "Rotate the key at its provider first (revoke it and create a new one): deleting the line does not "
    "take it out of the history, and everybody who can read this repository can read its history.",
    "Then take it out of the code, declare its name under app_secrets in manifest.yaml, read it with "
    "os.environ[\"NAME\"] and add the value in Pippa: Created by me, your app, Keys. For local runs, "
    "put it in .env, which is never committed.",
    "Not a key? End the line with the comment `gitleaks:allow`, or add an allowlist to .gitleaks.toml.",
)


class ScanError(Exception):
    """The scan could not run. The message never contains a scanned value."""


def shown(value: Any) -> str:
    """Printable, single-line and short: a file name or a rule name from the report."""
    text = "".join(char for char in str(value) if char.isprintable())
    return text[:MAX_SHOWN_CHARS] or "?"


# ---- the gitleaks binary ----

class Downloader(ABC):
    @abstractmethod
    def fetch(self) -> bytes:
        """Returns the release archive."""


class ReleaseDownloader(Downloader):
    """Downloads the pinned release from GitHub, retrying a failed attempt."""

    def __init__(
        self,
        opener: Callable[..., Any] = urllib.request.urlopen,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.opener = opener
        self.sleep = sleep

    def fetch(self) -> bytes:
        for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
            try:
                request = urllib.request.Request(GITLEAKS_URL, headers={"User-Agent": "pippa-hack-publish"})
                with self.opener(request, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
                    data = response.read(MAX_DOWNLOAD_BYTES + 1)
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                if attempt == DOWNLOAD_ATTEMPTS:
                    raise ScanError(f"gitleaks could not be downloaded: {type(exc).__name__}") from exc
                self.sleep(DOWNLOAD_BACKOFF_SECONDS * 2 ** (attempt - 1))
                continue
            if len(data) > MAX_DOWNLOAD_BYTES:
                raise ScanError("the gitleaks download is larger than any release")
            return data
        raise ScanError("gitleaks could not be downloaded")


class GitleaksInstaller:
    """Puts the gitleaks binary from the pinned release into the work directory.

    The archive's SHA-256 is compared with the pinned one before anything in it is
    read; only the one regular file named `gitleaks` is taken from it.
    """

    def __init__(self, downloader: Downloader, sha256: str = GITLEAKS_SHA256) -> None:
        self.downloader = downloader
        self.sha256 = sha256

    def install(self, work_dir: Path) -> Path:
        data = self.downloader.fetch()
        digest = hashlib.sha256(data).hexdigest()
        if digest != self.sha256:
            raise ScanError(f"the gitleaks {GITLEAKS_VERSION} download has SHA-256 {digest}, not the pinned one")
        binary = work_dir / GITLEAKS_MEMBER
        binary.unlink(missing_ok=True)
        try:
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
                member = next((item for item in archive.getmembers()
                               if item.name == GITLEAKS_MEMBER and item.isfile()), None)
                if member is None:
                    raise ScanError("the gitleaks archive has no gitleaks binary")
                source = archive.extractfile(member)
                if source is None:
                    raise ScanError("the gitleaks binary could not be read from the archive")
                content = source.read()
        except (tarfile.TarError, OSError, EOFError) as exc:
            raise ScanError(f"the gitleaks archive could not be read: {type(exc).__name__}") from exc
        descriptor = os.open(binary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700)
        with os.fdopen(descriptor, "wb") as target:
            target.write(content)
        return binary


# ---- the configuration ----

def toml_string(value: str) -> str:
    """A TOML basic string. Control characters are refused before this is called."""
    return json.dumps(value, ensure_ascii=False)


def has_control_characters(value: str) -> bool:
    return any(ord(char) < 0x20 or ord(char) == 0x7F for char in value)


@dataclass
class AppAllowlists:
    """The allowlists taken from the app's .gitleaks.toml, as TOML, and what was left out."""

    tables: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


class AppConfigReader:
    """Reads only the allowlists from the app's .gitleaks.toml.

    Rules, `extend` and anything else are left out, so the app can skip a false
    positive but never replace or drop a rule. A file that cannot be used is left
    out whole, with a note; the check then runs with the built-in allowlists only.
    """

    def __init__(self, app_dir: Path) -> None:
        self.path = app_dir / APP_CONFIG

    def read(self) -> AppAllowlists:
        result = AppAllowlists()
        if not self.path.exists() and not self.path.is_symlink():
            return result
        if self.path.is_symlink() or not self.path.is_file():
            result.notes.append(f"{APP_CONFIG} is not a regular file, so it was not used.")
            return result
        try:
            if self.path.stat().st_size > MAX_APP_CONFIG_BYTES:
                result.notes.append(f"{APP_CONFIG} is larger than {MAX_APP_CONFIG_BYTES // 1024} KB, "
                                    "so it was not used.")
                return result
            document = tomllib.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            result.notes.append(f"{APP_CONFIG} could not be read ({type(exc).__name__}), so it was not used.")
            return result

        ignored = sorted(key for key in document if key not in ("allowlist", "allowlists", "title", "description"))
        if ignored:
            result.notes.append(f"Only the allowlists in {APP_CONFIG} are used; ignored: {', '.join(ignored)}.")
        for table in self._tables(document, result):
            rendered = self._render(table, result)
            if rendered:
                result.tables.append(rendered)
        if len(result.tables) > MAX_APP_ALLOWLISTS:
            result.notes.append(f"Only the first {MAX_APP_ALLOWLISTS} allowlists in {APP_CONFIG} are used.")
            del result.tables[MAX_APP_ALLOWLISTS:]
        return result

    @staticmethod
    def _tables(document: dict[str, Any], result: AppAllowlists) -> list[Any]:
        tables: list[Any] = []
        single = document.get("allowlist")
        if single is not None:
            tables.append(single)
        many = document.get("allowlists")
        if many is not None:
            if isinstance(many, list):
                tables.extend(many)
            else:
                result.notes.append(f"allowlists in {APP_CONFIG} is not a list of tables, so it was not used.")
        return tables

    def _render(self, table: Any, result: AppAllowlists) -> str:
        if not isinstance(table, dict):
            result.notes.append(f"An allowlist in {APP_CONFIG} is not a table, so it was not used.")
            return ""
        lines = ["[[allowlists]]"]
        description = table.get("description")
        text = description if isinstance(description, str) and not has_control_characters(description) else ""
        lines.append(f"description = {toml_string(('From the app: ' + text)[:MAX_APP_VALUE_CHARS])}")
        checks = 0
        for key, value in table.items():
            if key == "description":
                continue
            if key in APP_ALLOWLIST_CHOICES:
                if value not in APP_ALLOWLIST_CHOICES[key]:
                    result.notes.append(f"{key} = {shown(value)!r} in {APP_CONFIG} is not one of "
                                        f"{', '.join(APP_ALLOWLIST_CHOICES[key])}; that allowlist was not used.")
                    return ""
                lines.append(f"{key} = {toml_string(value)}")
                continue
            if key not in APP_ALLOWLIST_LISTS:
                result.notes.append(f"{shown(key)} in an allowlist of {APP_CONFIG} is not used.")
                continue
            items = self._strings(value)
            if items is None:
                result.notes.append(f"{key} in {APP_CONFIG} must be a list of at most {MAX_APP_LIST_ITEMS} "
                                    f"one-line strings; that allowlist was not used.")
                return ""
            broad = next((item for item in items if allows_too_much(key, item)), None)
            if broad is not None:
                result.notes.append(f"{key} entry {shown(broad)!r} in {APP_CONFIG} would skip nearly every "
                                    "key, or could not be checked; that allowlist was not used. Mark a false "
                                    "positive with a `gitleaks:allow` comment at the end of its line instead.")
                return ""
            if items:
                lines.append(f"{key} = [{', '.join(toml_string(item) for item in items)}]")
                checks += key in APP_ALLOWLIST_CHECKS
        if not checks:
            result.notes.append(f"An allowlist in {APP_CONFIG} lists no paths, regexes, stopwords or "
                                "commits, so it was not used.")
            return ""
        return "\n".join(lines)

    @staticmethod
    def _strings(value: Any) -> list[str] | None:
        if not isinstance(value, list) or len(value) > MAX_APP_LIST_ITEMS:
            return None
        if not all(isinstance(item, str) and item and len(item) <= MAX_APP_VALUE_CHARS
                   and not has_control_characters(item) for item in value):
            return None
        return list(value)


def allows_too_much(key: str, item: str) -> bool:
    """Whether one entry of an app allowlist would skip (nearly) every finding.

    gitleaks matches Go regular expressions, unanchored. Python's `re` reads the same syntax
    for what an allowlist needs; a pattern it cannot read cannot be checked, so is not trusted.
    """
    if key == "stopwords":
        word = item.lower()
        return len(item) < MIN_STOPWORD_CHARS or any(word in sample.lower() for sample in BROAD_SECRET_SAMPLES)
    if key not in ("paths", "regexes"):
        return False
    try:
        pattern = re.compile(item)
    except (re.error, OverflowError, RecursionError):
        return True
    samples = BROAD_PATH_SAMPLES if key == "paths" else BROAD_SECRET_SAMPLES
    return any(pattern.search(sample) for sample in samples)


def write_config(path: Path, base: str, app_tables: list[str]) -> Path:
    text = base.rstrip("\n") + "\n"
    if app_tables:
        text += "\n# From the app's .gitleaks.toml (allowlists only)\n\n" + "\n\n".join(app_tables) + "\n"
    path.write_text(text, encoding="utf-8")
    return path


# ---- what to scan ----

class Git:
    def __init__(self, root: Path) -> None:
        self.root = root

    def has_commit(self, sha: str) -> bool:
        if not SHA.match(sha):
            return False
        try:
            result = subprocess.run(
                ["git", "-C", str(self.root), "cat-file", "-e", f"{sha}^{{commit}}"],
                capture_output=True, check=False,
            )
        except OSError as exc:
            raise ScanError(f"git could not run: {exc.strerror}") from exc
        return result.returncode == 0


@dataclass(frozen=True)
class Target:
    """The files of the checkout, and also the pushed commits (`before`..`after`) when `before` is set."""

    before: str = ""
    after: str = ""

    @property
    def is_range(self) -> bool:
        return bool(self.before)

    def describe(self) -> str:
        if self.is_range:
            return (f"the files of the checkout and the pushed commits "
                    f"{self.before[:SHORT_SHA]}..{self.after[:SHORT_SHA]}")
        return "the files of the checkout"


def choose_target(git: Git, event_name: str, event: dict[str, Any], sha: str) -> Target:
    if event_name != "push":
        return Target()
    before = str(event.get("before") or "").lower()
    after = (sha or str(event.get("after") or "")).lower()
    if not SHA.match(before) or ZERO_SHA.match(before) or not SHA.match(after):
        return Target()
    if not (git.has_commit(before) and git.has_commit(after)):
        return Target()
    return Target(before, after)


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


# ---- running gitleaks ----

@dataclass(frozen=True)
class Finding:
    file: str
    line: int
    commit: str
    rule: str

    @property
    def location(self) -> str:
        where = f"{self.file}:{self.line}"
        if self.commit:
            return f"{where} (commit {self.commit[:SHORT_SHA]}, gitleaks rule {self.rule})"
        return f"{where} (gitleaks rule {self.rule})"

    def render(self) -> str:
        return f"{FAIL} {FINDING.format(location=self.location)}"


def read_findings(report: Path) -> list[Finding]:
    """The file, line, commit and rule of each finding; nothing else of the report is read."""
    return merge_findings(parse_report(report))


def merge_findings(findings: list[Finding]) -> list[Finding]:
    """One finding per file and line, sorted.

    A provider's own rule says more than the generic one, and a finding that names the commit
    (the scan of the pushed commits) more than the same one in the files.
    """
    def rank(finding: Finding) -> tuple[bool, bool]:
        return finding.rule != GENERIC_RULE, bool(finding.commit)

    found: dict[tuple[str, int], Finding] = {}
    for finding in findings:
        key = (finding.file, finding.line)
        if key not in found or rank(finding) > rank(found[key]):
            found[key] = finding
    return sorted(found.values(), key=lambda finding: (finding.file, finding.line))


def parse_report(report: Path) -> list[Finding]:
    """Every finding of a gitleaks JSON report, unmerged."""
    try:
        items = json.loads(report.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ScanError(f"the gitleaks report could not be read: {type(exc).__name__}") from exc
    if not isinstance(items, list):
        raise ScanError("the gitleaks report is not a list")
    findings: list[Finding] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        line = item.get("StartLine")
        findings.append(Finding(
            file=shown(item.get("File") or "?"),
            line=line if isinstance(line, int) and line > 0 else 0,
            commit=str(item.get("Commit") or "") if SHA.match(str(item.get("Commit") or "")) else "",
            rule=shown(item.get("RuleID") or "?"),
        ))
    return findings


class Gitleaks:
    """Runs the binary on the app's checkout with a config, and reads its report."""

    def __init__(self, binary: Path, app_dir: Path, work_dir: Path) -> None:
        self.binary = binary
        self.app_dir = app_dir
        self.work_dir = work_dir

    def scan(self, config: Path, target: Target) -> list[Finding]:
        """The files of the checkout, then the pushed commits when there is a range."""
        findings = self._run(config, target, history=False)
        if target.is_range:
            findings += self._run(config, target, history=True)
        return merge_findings(findings)

    def _run(self, config: Path, target: Target, history: bool) -> list[Finding]:
        report = self.work_dir / ("report-commits.json" if history else "report-files.json")
        report.unlink(missing_ok=True)
        args = [
            str(self.binary), "git" if history else "dir",
            "--no-banner", "--redact", "--log-level=warn",
            f"--config={config}",
            "--report-format=json", f"--report-path={report}",
            f"--exit-code={LEAKS_EXIT}",
            f"--max-target-megabytes={MAX_TARGET_MEGABYTES}",
            f"--timeout={SCAN_TIMEOUT_SECONDS}",
            f"--gitleaks-ignore-path={self._ignore_path()}",
        ]
        if history:
            args.append(f"--log-opts={target.before}..{target.after}")
        args.append(".")
        environment = {name: value for name, value in os.environ.items() if not name.startswith("GITLEAKS_")}
        try:
            result = subprocess.run(
                args, cwd=self.app_dir, env=environment, capture_output=True, text=True,
                encoding="utf-8", errors="replace", timeout=SCAN_TIMEOUT_SECONDS + 30, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ScanError("gitleaks did not finish within its time limit") from exc
        except OSError as exc:
            raise ScanError(f"gitleaks could not run: {exc.strerror}") from exc
        if result.returncode not in (0, LEAKS_EXIT):
            tail = " | ".join(line.strip() for line in result.stderr.strip().splitlines()[-3:])
            raise ScanError(f"gitleaks failed (exit {result.returncode}): {tail[:400]}")
        return parse_report(report)

    def _ignore_path(self) -> Path:
        """The app's .gitleaksignore when it is a regular file, else an empty directory."""
        ignore = self.app_dir / APP_IGNORE
        if ignore.is_file() and not ignore.is_symlink():
            return ignore
        empty = self.work_dir / "no-ignore"
        empty.mkdir(exist_ok=True)
        return empty


# ---- the scan ----

@dataclass
class Result:
    findings: list[Finding] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


class SecretScan:
    def __init__(self, app_dir: Path, work_dir: Path, binary: Path, target: Target) -> None:
        self.app_dir = app_dir
        self.work_dir = work_dir
        self.gitleaks = Gitleaks(binary, app_dir, work_dir)
        self.target = target

    def run(self) -> Result:
        try:
            base = BASE_CONFIG.read_text(encoding="utf-8")
        except OSError as exc:
            raise ScanError(f"the built-in gitleaks configuration could not be read: {exc.strerror}") from exc
        app = AppConfigReader(self.app_dir).read()
        result = Result(notes=list(app.notes))
        config = write_config(self.work_dir / "config.toml", base, app.tables)
        try:
            result.findings = self.gitleaks.scan(config, self.target)
        except ScanError as exc:
            if not app.tables:
                raise
            # The app's allowlists can break the configuration (a regex gitleaks refuses):
            # scan again without them rather than blame the platform.
            print(f"gitleaks refused the configuration with the app's allowlists: {exc}", file=sys.stderr)
            result.notes.append(f"gitleaks could not use the allowlists in {APP_CONFIG}, so they were not used.")
            config = write_config(self.work_dir / "config.toml", base, [])
            result.findings = self.gitleaks.scan(config, self.target)
        return result


def render(result: Result) -> str:
    lines = [finding.render() for finding in result.findings[:MAX_LISTED]]
    if len(result.findings) > MAX_LISTED:
        lines.append(f"{FAIL} {len(result.findings) - MAX_LISTED} more not listed")
    lines.extend(f"{FIX_INDENT}{line}" for line in (*FIX, *result.notes))
    return "\n".join(lines)


def prepare_work_dir(path: Path) -> Path:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(mode=0o700, parents=True)
    return path


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Refuse a push that commits a value that looks like a key")
    parser.add_argument("--app-dir", required=True, type=Path)
    parser.add_argument("--work-dir", required=True, type=Path, help="Emptied, then holds gitleaks and its report")
    parser.add_argument("--gitleaks", type=Path, default=None,
                        help="A gitleaks binary to use instead of the pinned download (tests)")
    parser.add_argument("--event-name", default=os.environ.get("GITHUB_EVENT_NAME", ""))
    parser.add_argument("--event-path", default=os.environ.get("GITHUB_EVENT_PATH", ""))
    parser.add_argument("--sha", default=os.environ.get("GITHUB_SHA", ""))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None, downloader: Downloader | None = None) -> int:
    args = parse_args(argv)
    try:
        if not args.app_dir.is_dir():
            raise ScanError(f"{args.app_dir} is not a directory")
        app_dir = args.app_dir.resolve()
        work_dir = prepare_work_dir(args.work_dir.resolve())
        binary = args.gitleaks or GitleaksInstaller(downloader or ReleaseDownloader()).install(work_dir)
        event = load_event(args.event_path) if args.event_name else {}
        target = choose_target(Git(app_dir), args.event_name, event, args.sha)
        result = SecretScan(app_dir, work_dir, binary, target).run()
    except ScanError as exc:
        print(f"secret check failed: {exc}", file=sys.stderr)
        return 2
    for note in result.notes:
        print(f"Note: {note}", file=sys.stderr)
    print(f"Secret check (gitleaks {GITLEAKS_VERSION}) scanned {target.describe()}: "
          f"{len(result.findings)} finding(s)", file=sys.stderr)
    if result.findings:
        print(render(result))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
