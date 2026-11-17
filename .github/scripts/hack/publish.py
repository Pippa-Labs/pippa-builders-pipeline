#!/usr/bin/env python3
"""Publishes a hackathon Agent App through the Pippa publisher and builds the report.

Packages the app, requests a GitHub OIDC token for the publisher, invokes the
publisher Lambda and classifies the result as success, code error, refusal or
platform error. A refusal is the publisher saying no for a reason that is not
in the app's code (`error_kind: "refused"`): the repository is not linked to a
Pippa account, or manifest.yaml has something Pippa does not accept, or the run
itself may not publish (a `reason` of IDENTITY_REFUSALS: pushed by somebody who
is not the repository's builder, a token used twice, not a push). Every invoke
carries a token requested for that attempt: the publisher accepts a token once.
A published app whose API keys are not all added yet says so first, apart from
the warnings that a push fixes.

A run the account that pushed did not start (GITHUB_TRIGGERING_ACTOR is not
GITHUB_ACTOR: somebody else's re-run) stops before anything else, with exit
status RERUN_REFUSED_EXIT: no token is requested, nothing is sent to Pippa and
no report is written, so nothing is posted on the commit. Writes the report
files the action posts:

    outcome       success, code, refused or infra
    status.json   body for POST /repos/{owner}/{repo}/statuses/{sha}
    comment.json  body for POST /repos/{owner}/{repo}/commits/{sha}/comments
    summary.md    step summary

Usage:
    publish.py --app-dir app --run-url URL --reference REF --out-dir DIR
               [--check-result success] [--check-verdict passed]
               [--check-details-file FILE] [--check-warnings-file FILE]
               [--credentials success]
               [--setup success]
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

LOG = logging.getLogger("hack-publish")

STATUS_CONTEXT = "pippa/publish"
STATUS_DESCRIPTION_LIMIT = 140
DETAILS_LIMIT = 6000
WARNINGS_LIMIT = 3000
#: Pippa's own warnings on a published app: at most this many, each cut to this many characters.
RUNTIME_WARNINGS_COUNT = 10
RUNTIME_WARNING_LIMIT = 500
RUNTIME_WARNINGS_TITLE = "Pippa"

PUBLISH_FUNCTION = "pippa-hack-publish"
PUBLISH_REGION = "eu-west-2"
TOKEN_AUDIENCE = "pippa-hackathon"
TOKEN_TIMEOUT_SECONDS = 15
READ_TIMEOUT_SECONDS = 360
CONNECT_TIMEOUT_SECONDS = 15
THROTTLE_ATTEMPTS = 3
THROTTLE_BACKOFF_SECONDS = 2.0
THROTTLE_CODE = "TooManyRequestsException"

APP_URL = "https://app.getpippa.ai"
CODE_MARKER = "CODE_ERROR"
REFUSED_MARKER = "REFUSED"
PLATFORM_MARKER = "PLATFORM_ERROR"
#: The publisher's `error_kind` of a refusal that is about the repository, not its code.
#: It comes with `error_class: "code"`, so an older pipeline still shows the sentence.
REFUSED_KIND = "refused"
REFUSED_WITHOUT_REASON = "Pippa refused to publish this app without giving a reason. Ask an organiser."
REFUSED_NEXT_STEP = (
    "This is not a problem in the app's code: do what the message says, then push again. If it says to "
    "ask an organiser, changing the code will not help."
)


@dataclass(frozen=True)
class IdentityRefusal:
    """What Pippa says when the run itself may not publish, and what the builder does about it."""

    sentence: str
    next_step: str


#: The `reason` of a refusal about who or what started the run rather than the app (the runtime's
#: pre-check, relayed by the publisher). The sentence stands in when Pippa sends none.
IDENTITY_REFUSALS = {
    "actor_not_builder": IdentityRefusal(
        "Only the builder this repository belongs to can publish it.",
        "Push from the GitHub account Pippa Labs invited to this repository. If somebody else pushed to "
        "it, tell an organiser. Changing the code will not help.",
    ),
    "token_replayed": IdentityRefusal(
        "This run's one-time permission to publish had already been used.",
        "Push again to publish. If it happens again, tell an organiser and give them the run log link.",
    ),
    "not_a_push": IdentityRefusal(
        "Only a push to main publishes; this run was started another way.",
        "Push a commit to main to publish.",
    ),
    "builder_identity_unknown": IdentityRefusal(
        "Pippa does not know yet which GitHub account this repository belongs to, so it cannot check who pushed.",
        "Ask an organiser to finish setting up this repository. Changing the code will not help.",
    ),
}
#: How each identity refusal's sentence starts. The publisher relays the runtime's sentence as
#: `error` and does not send the `reason`, so this is how a refusal is recognised today. The
#: runtime's sentences live in pippa_agentapp_runtime/deploys.py; the event refusal is also
#: the publisher's own. Compared after curly apostrophes are made straight.
IDENTITY_SENTENCES = (
    ("Only the builder this repository belongs to can publish it", "actor_not_builder"),
    ("Only a push to main publishes", "not_a_push"),
    ("This publish used a GitHub sign-in token that had already been used", "token_replayed"),
    ("Pippa doesn't know which GitHub account this repository belongs to", "builder_identity_unknown"),
)
CURLY_APOSTROPHE = "\N{RIGHT SINGLE QUOTATION MARK}"
#: A runtime warning about an API key the app declares but its builder has not added yet, e.g.
#: "Published, but it can't run until you add OPENWEATHER_API_KEY in Created by me."
KEYS_MISSING = re.compile(r"\b(?:can[\u2019']?t|cannot|can not)\s+run\s+until\s+you\s+add\b", re.IGNORECASE)

MAX_BUNDLE_BYTES = 3 * 1024 * 1024
SKIP_DIRS = frozenset({
    ".git", ".github", "vendor", "scripts", "__pycache__", ".pytest_cache",
    ".venv", "venv", "docs", ".ruff_cache", ".idea",
})
SKIP_SUFFIXES = (".pyc", ".pyo", ".whl", ".tar.gz", ".zip", ".log")
SKIP_NAMES = frozenset({
    "requirements-dev.txt", "CLAUDE.md", "README.md", ".gitignore", "pytest.ini",
})
REQUIRED_FILES = ("app.py", "manifest.yaml")
SUCCESS_FIELDS = ("app_id", "version", "name")

#: Exit status when somebody other than the account that pushed started the run (sysexits'
#: EX_NOPERM). hack-publish.sh then fails the job and posts and counts nothing.
RERUN_REFUSED_EXIT = 77
#: Characters an account name keeps in the log; anything else becomes "?".
ACCOUNT_UNSAFE = re.compile(r"[^A-Za-z0-9._\[\]-]")
ACCOUNT_LIMIT = 100


class PublishError(Exception):
    """Base error of a publish run."""


class CodeError(PublishError):
    """The app itself must change before it can be published."""


class PlatformError(PublishError):
    """The pipeline or the publisher failed; the app is not at fault.

    `detail` goes to the job log only, never to the comment or the status.
    """

    def __init__(self, detail: str, reference: str | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.reference = reference


def fence(text: str) -> str:
    """Wraps text in a Markdown code fence longer than any backtick run inside it."""
    longest = run = 0
    for char in text:
        run = run + 1 if char == "`" else 0
        longest = max(longest, run)
    marker = "`" * max(3, longest + 1)
    return f"{marker}text\n{text}\n{marker}"


def truncate(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ""


def plain_account(name: str) -> str:
    """An account name as plain text for the log, so it can never start a workflow command."""
    return ACCOUNT_UNSAFE.sub("?", name)[:ACCOUNT_LIMIT]


def started_by_another_account(environ: Mapping[str, str]) -> str:
    """Why the run may not publish because the account that pushed did not start it; "" when it did.

    A re-run keeps the pusher as GITHUB_ACTOR (and so in the token) and names whoever started it in
    GITHUB_TRIGGERING_ACTOR. The workflow skips every job of such a re-run; this check also stops
    one whose workflow has no such condition (a re-run of single jobs replays the first attempt's
    workflow, while this file comes from the action's current ref). The names must match exactly,
    and a run that does not give both is refused too.
    """
    actor = environ.get("GITHUB_ACTOR", "")
    triggering = environ.get("GITHUB_TRIGGERING_ACTOR", "")
    if not actor or not triggering:
        return ("Not published: the run does not say both who pushed (GITHUB_ACTOR) and who started it "
                "(GITHUB_TRIGGERING_ACTOR), so it cannot show that the account that pushed started it.")
    if actor != triggering:
        return (f"Not published: this re-run was started by {plain_account(triggering)}, not by "
                f"{plain_account(actor)}, who pushed; only the account that pushed can re-run a publish. "
                "Push again instead.")
    return ""


def runtime_warnings(reply: dict[str, Any]) -> list[str]:
    """The sentences in the publisher's `warnings`, one line each; [] from a publisher that sends none."""
    value = reply.get("warnings")
    if not isinstance(value, list):
        return []
    sentences = [" ".join(item.split()) for item in value if isinstance(item, str)]
    return [truncate(item, RUNTIME_WARNING_LIMIT) for item in sentences if item][:RUNTIME_WARNINGS_COUNT]


def identity_reason(reply: dict[str, Any]) -> str:
    """The IDENTITY_REFUSALS code of a refusal: its `reason` (or `error_reason`), else its sentence; or ""."""
    for name in ("reason", "error_reason"):
        value = reply.get(name)
        if isinstance(value, str) and value.strip() in IDENTITY_REFUSALS:
            return value.strip()
    sentence = str(reply.get("error") or "").strip().replace(CURLY_APOSTROPHE, "'")
    return next((reason for start, reason in IDENTITY_SENTENCES if sentence.startswith(start)), "")


class Outcome(ABC):
    """Result of a publish run, rendered as commit status, commit comment and summary."""

    kind: str
    state: str

    def __init__(self, run_url: str) -> None:
        self.run_url = run_url

    @property
    @abstractmethod
    def description(self) -> str:
        """Short commit status description."""

    @property
    @abstractmethod
    def message(self) -> str:
        """Commit comment body."""

    @property
    def summary(self) -> str:
        return f"### Publish to Pippa\n\n{self.message}\n"

    def status_payload(self) -> dict[str, str]:
        return {
            "state": self.state,
            "context": STATUS_CONTEXT,
            "description": truncate(self.description, STATUS_DESCRIPTION_LIMIT),
            "target_url": self.run_url,
        }

    def comment_payload(self) -> dict[str, str]:
        return {"body": self.message}


class Published(Outcome):
    kind = "success"
    state = "success"

    def __init__(self, run_url: str, reply: dict[str, Any], warnings: str = "") -> None:
        super().__init__(run_url)
        self.warnings = truncate(warnings, WARNINGS_LIMIT)
        self.runtime_warnings = runtime_warnings(reply)
        #: Pippa's warnings that the app cannot run until its builder adds an API key; no push fixes them.
        self.keys_missing = [sentence for sentence in self.runtime_warnings if KEYS_MISSING.search(sentence)]
        self.app_id = str(reply["app_id"])
        self.version = str(reply["version"])
        self.name = str(reply["name"])
        self.outcomes = [str(item) for item in reply.get("outcomes") or []]
        self.function = str(reply.get("function") or "")

    @property
    def description(self) -> str:
        if self.keys_missing:
            return f"Live: {self.app_id} v{self.version}; add its API keys in Pippa"
        return f"Live: {self.app_id} v{self.version}"

    @property
    def all_warnings(self) -> str:
        """Pippa's own warnings first, as WARN lines under one title, then those of the build.

        The missing API keys are not among them: the message lists those on their own.
        """
        sections = []
        others = [sentence for sentence in self.runtime_warnings if sentence not in self.keys_missing]
        if others:
            lines = [f" WARN {sentence}" for sentence in others]
            sections.append("\n".join([RUNTIME_WARNINGS_TITLE, *lines]))
        if self.warnings:
            sections.append(self.warnings)
        return "\n\n".join(sections)

    @property
    def message(self) -> str:
        if self.keys_missing:
            keys = "\n".join(f" WARN {sentence}" for sentence in self.keys_missing)
            message = (
                f"Your app {self.name} ({self.app_id} v{self.version}) is published, but it can't run "
                f"until you add its API keys. Open {APP_URL}, go to Created by me, open the app and add "
                "them under Keys. No push is needed: the keys stay in Pippa, never in GitHub.\n\n"
                f"{fence(keys)}"
            )
        else:
            message = (
                f"Your app {self.name} ({self.app_id} v{self.version}) is live. "
                f"Open {APP_URL}, go to Apps, install it and try it."
            )
        if self.all_warnings:
            message += (
                "\n\nWarnings (they did not stop the publish; fix them and push again):\n\n"
                f"{fence(self.all_warnings)}"
            )
        return message

    @property
    def summary(self) -> str:
        outcomes = ", ".join(f"`{item}`" for item in self.outcomes) or "-"
        return f"{super().summary}\nOutcomes: {outcomes}\n"


class CodeFailure(Outcome):
    kind = "code"
    state = "failure"

    def __init__(self, run_url: str, error: str) -> None:
        super().__init__(run_url)
        self.error = truncate(error, DETAILS_LIMIT) or "The check failed without a message."

    @property
    def description(self) -> str:
        return f"Code error: {first_line(self.error)}"

    @property
    def message(self) -> str:
        return (
            f"{CODE_MARKER}: the app was not published because of a problem in its code.\n\n"
            f"{fence(self.error)}\n\n"
            f"Fix the code and push again.\n\n"
            f"Run log: {self.run_url}"
        )


class Refusal(Outcome):
    """Pippa said no before anything was deployed, for a reason outside the app's code.

    The sentence comes from the runtime and says what to do. It is fenced like a code
    error's text: it can quote names from manifest.yaml, which must not become Markdown.
    """

    kind = "refused"
    state = "failure"

    def __init__(self, run_url: str, detail: str, reason: str = "") -> None:
        super().__init__(run_url)
        #: A code of IDENTITY_REFUSALS, or "" for a refusal about the repository or the manifest.
        self.reason = reason if reason in IDENTITY_REFUSALS else ""
        fallback = IDENTITY_REFUSALS[self.reason].sentence if self.reason else REFUSED_WITHOUT_REASON
        self.detail = truncate(detail, DETAILS_LIMIT) or fallback

    @property
    def description(self) -> str:
        return f"Refused: {first_line(self.detail)}"

    @property
    def message(self) -> str:
        if self.reason:
            next_step = (
                f"{IDENTITY_REFUSALS[self.reason].next_step} This is not a problem in the app's code. "
                f"(Reason: {self.reason})"
            )
        else:
            next_step = REFUSED_NEXT_STEP
        return (
            f"{REFUSED_MARKER}: the app was not published:\n\n"
            f"{fence(self.detail)}\n\n"
            f"Nothing was published, and every version published before is unchanged. {next_step}\n\n"
            f"Run log: {self.run_url}"
        )


class PlatformFailure(Outcome):
    kind = "infra"
    state = "failure"

    def __init__(self, run_url: str, reference: str) -> None:
        super().__init__(run_url)
        self.reference = reference

    @property
    def description(self) -> str:
        return f"Platform error, reference {self.reference}"

    @property
    def message(self) -> str:
        return (
            f"{PLATFORM_MARKER}: the app was not published because of a problem on the "
            f"Pippa platform, not in the app. Reference: {self.reference}\n\n"
            f"Do not change your code. Tell an organiser and give them the reference."
        )


class OutcomeFactory:
    """Builds the outcome from the check verdict, a publisher reply or an error."""

    def __init__(self, run_url: str, reference: str, warnings: str = "") -> None:
        self.run_url = run_url
        self.reference = reference
        self.warnings = warnings

    def code(self, error: str) -> CodeFailure:
        return CodeFailure(self.run_url, error)

    def refused(self, detail: str, reason: str = "") -> Refusal:
        return Refusal(self.run_url, detail, reason)

    def platform(self, reference: str | None = None) -> PlatformFailure:
        return PlatformFailure(self.run_url, reference or self.reference)

    def from_check(self, result: str, verdict: str, details: str) -> Outcome | None:
        """Returns the outcome decided by the check job, or None when publishing may go on."""
        if result == "success":
            return None
        if result == "failure" and verdict == "failed":
            return self.code(details)
        LOG.error("Check job ended with result=%r verdict=%r", result, verdict)
        return self.platform()

    def from_reply(self, reply: Any) -> Outcome:
        if not isinstance(reply, dict):
            raise PlatformError(f"Publisher reply is not an object: {reply!r:.400}")
        if reply.get("ok") is True:
            missing = [name for name in SUCCESS_FIELDS if not reply.get(name)]
            if missing:
                raise PlatformError(f"Publisher success reply lacks {missing}: {reply!r:.400}")
            published = Published(self.run_url, reply, self.warnings)
            notes = "".join(f"  Warning  {sentence}\n" for sentence in published.runtime_warnings)
            print(
                f"\n{'=' * 68}\n  YOUR APP IS LIVE IN PIPPA\n\n"
                f"  App      {published.name}  ({published.app_id} v{published.version})\n"
                f"  Can do   {', '.join(published.outcomes) or '-'}\n{notes}{'=' * 68}"
            )
            return published
        error_class = reply.get("error_class")
        reason = identity_reason(reply) if error_class in ("code", REFUSED_KIND) else ""
        if reason or error_class == REFUSED_KIND or (
            error_class == "code" and reply.get("error_kind") == REFUSED_KIND
        ):
            refusal = self.refused(str(reply.get("error") or ""), reason)
            print(f"\n{'=' * 68}\n{REFUSED_MARKER}: the app was not published:\n\n  {refusal.detail}\n{'=' * 68}")
            return refusal
        if error_class == "code":
            error = str(reply.get("error") or "")
            print(f"\n{'=' * 68}\nPippa refused to publish this app:\n\n  {error}\n{'=' * 68}")
            return self.code(error)
        LOG.error(
            "Publisher refused with statusCode=%s error_class=%r error=%r",
            reply.get("statusCode"), error_class, reply.get("error"),
        )
        return self.platform(str(reply.get("reference") or "") or None)


@dataclass(frozen=True)
class Package:
    data: bytes
    files: list[str] = field(default_factory=list)


class Packager:
    """Zips the files the app needs at run time; development files stay out."""

    def __init__(self, root: Path, limit: int = MAX_BUNDLE_BYTES) -> None:
        self.root = root
        self.limit = limit

    def _candidates(self) -> list[Path]:
        found: list[Path] = []
        for directory, dirnames, filenames in os.walk(self.root, followlinks=False):
            dirnames[:] = sorted(name for name in dirnames if name not in SKIP_DIRS)
            for name in sorted(filenames):
                path = Path(directory) / name
                if path.is_symlink() or not path.is_file():
                    LOG.warning("Skipping %s: not a regular file", path.relative_to(self.root))
                    continue
                if self._excluded(name):
                    continue
                found.append(path)
        return found

    @staticmethod
    def _excluded(name: str) -> bool:
        if name in SKIP_NAMES or name.endswith(SKIP_SUFFIXES):
            return True
        return name.startswith("test_") or name.endswith("_test.py")

    def build(self) -> Package:
        out = io.BytesIO()
        included: list[str] = []
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in self._candidates():
                relative = path.relative_to(self.root).as_posix()
                archive.write(path, relative)
                included.append(relative)

        size = out.tell()
        print(f"Packaged {len(included)} file(s), {size // 1024} KB:")
        for name in included:
            print(f"  {name}")
        if size > self.limit:
            raise CodeError(self._too_large(size, included))
        for required in REQUIRED_FILES:
            if required not in included:
                if required == "app.py":
                    raise CodeError("ERROR: app.py is not in the package. It must be at the top level.")
                raise CodeError(f"ERROR: {required} is not in the package.")
        return Package(out.getvalue(), included)

    def _too_large(self, size: int, included: list[str]) -> str:
        biggest = sorted(((self.root / name).stat().st_size, name) for name in included)[-5:]
        listing = "\n".join(f"    {length // 1024:>6} KB  {name}" for length, name in reversed(biggest))
        return (
            f"ERROR: the package is {size // 1024} KB; the limit is {self.limit // 1024} KB.\n\n"
            f"  Largest files:\n{listing}\n\n"
            "  You do not need to commit dependencies - the Pippa toolkit is\n"
            "  already available to your app. Check for a committed virtualenv,\n"
            "  a data file or a folder of images."
        )


def load_manifest(root: Path) -> dict[str, Any]:
    import yaml

    try:
        manifest = yaml.safe_load((root / "manifest.yaml").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as exc:
        raise CodeError(f"ERROR: manifest.yaml could not be read: {exc}") from exc
    if not isinstance(manifest, dict):
        raise CodeError("ERROR: manifest.yaml does not contain a mapping.")
    return manifest


class TokenProvider(ABC):
    @abstractmethod
    def token(self, audience: str) -> str:
        """Returns a GitHub OIDC token for the audience."""


class ActionsTokenProvider(TokenProvider):
    """Requests the token from the Actions runtime; needs `id-token: write`."""

    def __init__(self, environ: dict[str, str] | None = None) -> None:
        self.environ = os.environ if environ is None else environ

    def token(self, audience: str) -> str:
        url = self.environ.get("ACTIONS_ID_TOKEN_REQUEST_URL")
        bearer = self.environ.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN")
        if not url or not bearer:
            raise PlatformError("No OIDC token request variables; the job lacks id-token: write")
        separator = "&" if "?" in url else "?"
        request = urllib.request.Request(
            f"{url}{separator}{urllib.parse.urlencode({'audience': audience})}",
            headers={"Authorization": f"bearer {bearer}"},
        )
        try:
            with urllib.request.urlopen(request, timeout=TOKEN_TIMEOUT_SECONDS) as response:  # noqa: S310
                value = json.loads(response.read())["value"]
        except (urllib.error.URLError, TimeoutError, ValueError, KeyError, TypeError) as exc:
            raise PlatformError(f"OIDC token request failed: {type(exc).__name__}: {exc}") from exc
        if not isinstance(value, str) or not value:
            raise PlatformError("OIDC token response has no value")
        return value


EventFactory = Callable[[], dict[str, Any]]


class Publisher(ABC):
    @abstractmethod
    def publish(self, make_event: EventFactory) -> Any:
        """Sends an event built by make_event and returns the decoded reply.

        make_event is called again for every attempt, so each attempt carries its own OIDC
        token: the publisher accepts a token once and refuses a reused one as a replay.
        """


class LambdaPublisher(Publisher):
    """Invokes the publisher function synchronously.

    Only a throttled invoke is retried: Lambda refuses it before the function runs. The
    retry is still a new event with a new token.
    """

    def __init__(
        self,
        client: Any = None,
        function: str = PUBLISH_FUNCTION,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.client = client if client is not None else self._client()
        self.function = function
        self.sleep = sleep

    @staticmethod
    def _client() -> Any:
        import boto3
        from botocore.config import Config

        return boto3.client(
            "lambda",
            region_name=PUBLISH_REGION,
            config=Config(
                read_timeout=READ_TIMEOUT_SECONDS,
                connect_timeout=CONNECT_TIMEOUT_SECONDS,
                retries={"total_max_attempts": 1},
            ),
        )

    def publish(self, make_event: EventFactory) -> Any:
        response = self._invoke(make_event)
        request_id = (response.get("ResponseMetadata") or {}).get("RequestId")
        try:
            raw = response["Payload"].read()
        except Exception as exc:
            raise PlatformError(f"Reading the reply failed: {type(exc).__name__}: {exc}") from exc

        if response.get("FunctionError"):
            raise PlatformError(
                f"Publisher raised {response['FunctionError']} (request {request_id})",
                reference=request_id,
            )
        try:
            return json.loads(raw)
        except (ValueError, TypeError) as exc:
            raise PlatformError(
                f"Publisher reply is not JSON ({len(raw)} bytes, request {request_id})",
                reference=request_id,
            ) from exc

    def _invoke(self, make_event: EventFactory) -> dict[str, Any]:
        for attempt in range(1, THROTTLE_ATTEMPTS + 1):
            payload = json.dumps(make_event()).encode()
            print(f"Publishing to {self.function} ({len(payload) // 1024} KB, attempt {attempt})")
            try:
                return self.client.invoke(
                    FunctionName=self.function,
                    InvocationType="RequestResponse",
                    Payload=payload,
                )
            except Exception as exc:
                error = getattr(exc, "response", None)
                code = (error.get("Error") or {}).get("Code") if isinstance(error, dict) else None
                if code == THROTTLE_CODE and attempt < THROTTLE_ATTEMPTS:
                    delay = THROTTLE_BACKOFF_SECONDS * 2 ** (attempt - 1)
                    LOG.warning("Publisher throttled, attempt %d of %d; retrying in %.0f s",
                                attempt, THROTTLE_ATTEMPTS, delay)
                    self.sleep(delay)
                    continue
                raise PlatformError(f"Invoke failed: {type(exc).__name__}: {exc}") from exc
        raise PlatformError("Invoke failed: no attempt left")


@dataclass(frozen=True)
class CheckState:
    result: str = "success"
    verdict: str = "passed"
    details: str = ""
    credentials: str = "success"
    setup: str = "success"


class PublishRun:
    """Decides the outcome of one publish attempt."""

    def __init__(
        self,
        app_dir: Path,
        factory: OutcomeFactory,
        token_provider: TokenProvider,
        publisher_factory: Callable[[], Publisher],
    ) -> None:
        self.app_dir = app_dir
        self.factory = factory
        self.token_provider = token_provider
        self.publisher_factory = publisher_factory

    def execute(self, state: CheckState) -> Outcome:
        decided = self.factory.from_check(state.result, state.verdict, state.details)
        if decided is not None:
            return decided
        try:
            if state.setup != "success":
                raise PlatformError("Publish job setup failed (checkout, Python or dependencies)")
            if state.credentials != "success":
                raise PlatformError("AWS role could not be assumed")
            return self._publish()
        except CodeError as exc:
            return self.factory.code(str(exc))
        except PlatformError as exc:
            LOG.error("Platform error: %s", exc.detail)
            return self.factory.platform(exc.reference)
        except Exception as exc:
            LOG.exception("Unexpected failure: %s", type(exc).__name__)
            return self.factory.platform()

    def _publish(self) -> Outcome:
        package = Packager(self.app_dir).build()
        manifest = load_manifest(self.app_dir)
        code = base64.b64encode(package.data).decode()

        def make_event() -> dict[str, Any]:
            # A new token for every attempt: the publisher refuses a token it has seen.
            return {"token": self.token_provider.token(TOKEN_AUDIENCE), "manifest": manifest, "code_zip_b64": code}

        reply = self.publisher_factory().publish(make_event)
        return self.factory.from_reply(reply)


class ReportWriter:
    def __init__(self, out_dir: Path) -> None:
        self.out_dir = out_dir

    def write(self, outcome: Outcome) -> None:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        (self.out_dir / "outcome").write_text(outcome.kind, encoding="utf-8")
        (self.out_dir / "status.json").write_text(
            json.dumps(outcome.status_payload()), encoding="utf-8"
        )
        (self.out_dir / "comment.json").write_text(
            json.dumps(outcome.comment_payload()), encoding="utf-8"
        )
        (self.out_dir / "summary.md").write_text(outcome.summary, encoding="utf-8")


def read_details(path: str | None) -> str:
    if not path:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Publish a hackathon app and build the report")
    parser.add_argument("--app-dir", required=True, type=Path)
    parser.add_argument("--run-url", required=True)
    parser.add_argument("--reference", required=True, help="Reference shown on platform errors")
    parser.add_argument("--out-dir", required=True, type=Path)
    parser.add_argument("--check-result", default="success")
    parser.add_argument("--check-verdict", default="passed")
    parser.add_argument("--check-details-file", default="")
    parser.add_argument("--check-warnings-file", default="", help="Non-blocking warnings of a passed check")
    parser.add_argument("--credentials", default="success")
    parser.add_argument("--setup", default="success", help="Aggregated outcome of checkout, Python and dependencies")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] [%(name)s] %(message)s")
    args = parse_args(argv)
    refusal = started_by_another_account(os.environ)
    if refusal:
        LOG.error("%s", refusal)
        return RERUN_REFUSED_EXIT
    state = CheckState(
        result=args.check_result,
        verdict=args.check_verdict,
        details=read_details(args.check_details_file),
        credentials=args.credentials,
        setup=args.setup,
    )
    run = PublishRun(
        app_dir=args.app_dir,
        factory=OutcomeFactory(args.run_url, args.reference, read_details(args.check_warnings_file)),
        token_provider=ActionsTokenProvider(),
        publisher_factory=LambdaPublisher,
    )
    outcome = run.execute(state)
    ReportWriter(args.out_dir).write(outcome)
    print(f"\n{outcome.message}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
