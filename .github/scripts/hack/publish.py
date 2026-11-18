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
import stat
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
APPS_LINK = f"[app.getpippa.ai/apps]({APP_URL}/apps)"
#: What this publish did to the app, as the runtime says in the reply's `change`. A publisher or
#: runtime that predates the field sends none: the message then covers both a first install and
#: an update.
CHANGE_FIRST = "first"  # the app's first version
CHANGE_NEW_VERSION = "new_version"  # a version number this app had not published before
CHANGE_SAME_VERSION = "same_version"  # a draft pushed again under its own number: replaced in place
CHANGES = (CHANGE_FIRST, CHANGE_NEW_VERSION, CHANGE_SAME_VERSION)
#: The reply's `review_status` of a version everybody gets at once (review switched off).
APPROVED = "approved"
#: The guide in every app repository that explains versions, testers and review.
REVIEW_GUIDE = "docs/REVIEW_AND_SHARING.md"
PHRASE_LIMIT = 120
NAME_LIMIT = 80
VERSION_LIMIT = 40
#: A version number whose next one Pippa can suggest: 0.2.0 -> 0.2.1.
PATCH_VERSION = re.compile(r"(\d{1,6})\.(\d{1,6})\.(\d{1,6})")
#: Characters that could make text from manifest.yaml, in the middle of a sentence, into Markdown
#: emphasis, a link, an image, HTML, an entity or a table cell. (GitHub's plain-text email shows
#: any backslash, so nothing else is escaped.)
MARKDOWN_SPECIAL = re.compile(r"([\\`*_\[\]<>!~|&])")
#: What makes the start of a line a heading or a list item ("# ", "- ", "+ ", "1. ", "1) "), when
#: a space follows (and one always does, after the text): a name starts the line under "First,
#: add its API keys". Its last character is escaped.
MARKDOWN_LINE_START = re.compile(r"^(#{1,6}|[+-]|\d{1,9}[.)])(?=\s|$)")
#: What GitHub turns into a link or a mention AFTER the Markdown is rendered, so that no backslash
#: stops it: an "@" (a mention notifies that account; also an email address), a web address
#: ("://", or "www." starting a word) and an issue or pull request number ("#12", "GH-12"). Each
#: goes in a code span of its own, which GitHub never links.
GITHUB_REFERENCE = re.compile(r"(?:@|://|(?<![a-z0-9])www\.|#(?=\d)|(?<![a-z0-9])gh-(?=\d))+", re.IGNORECASE)
#: The server of a run, on github.com or GitHub Enterprise: https and a host name, nothing else.
GITHUB_SERVER = re.compile(r"https://[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*")
#: A repository as GITHUB_REPOSITORY names it, owner/name, and never "." or "..".
GITHUB_REPOSITORY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}/(?!\.\.?$)[A-Za-z0-9_.-]{1,100}")
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


#: A check that failed without a verdict in the workflow made before the check was split into the
#: prepare, SDK and check jobs (--old-workflow). A re-run of single jobs of one of its runs replays
#: it for 30 days with the actions of @main, and hack-check refuses its `sdk` mode, so its check job
#: can no longer pass: a refusal that asks for a new push, not a platform error. The public
#: pipeline's hack-publish passes --old-workflow unless the workflow says otherwise.
#: Remove after 2026-12-20.
OLD_WORKFLOW_REASON = "old_workflow"

#: The `reason` of a refusal about who or what started the run rather than the app (the runtime's
#: pre-check, relayed by the publisher, or the publisher's own: an old run, a re-run Pippa cannot
#: date, the wrong pipeline for the repository, two publishes at once). The sentence stands in
#: when Pippa sends none; the publisher's own are its sentences in hackathon/publisher/handler.py.
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
    "stale_run": IdentityRefusal(
        "This workflow run is older than the one that published the live version of this app, so it cannot "
        "replace it. Re-running an old run never publishes; push a new commit to main instead.",
        "Push a new commit to main to publish your latest code. Re-running this run, or any older one, will "
        "not publish it.",
    ),
    "rerun_untagged": IdentityRefusal(
        "This is a re-run of an earlier workflow run, and Pippa cannot tell whether it is older than the live "
        "version of this app, so it cannot replace it. Push a new commit to main instead.",
        "Push a new commit to main to publish. Re-running this run will not help; once a push has published, "
        "re-running that push's run works again.",
    ),
    "visibility_not_allowed": IdentityRefusal(
        "this repository is not private, so it cannot publish through the private pipeline; restore "
        ".github/workflows/publish.yml from the public template",
        "Restore .github/workflows/publish.yml from the public app template (pippa-hack-template-public), "
        "then push again. If you did not change that file, tell an organiser.",
    ),
    "concurrent_publish": IdentityRefusal(
        "Another publish of this app is running at the same time, so this one stopped and published nothing. "
        "Wait for that run to finish, then push again.",
        "Wait for the other publish of this app to finish (see the Actions tab of this repository). If it did "
        "not publish your latest commit, push again.",
    ),
    # Decided here, never sent by the publisher. Remove after 2026-12-20, with --old-workflow.
    OLD_WORKFLOW_REASON: IdentityRefusal(
        "This run was made by an older version of the Pippa publish workflow, which can no longer publish.",
        "Push a new commit to main to publish your app. Re-running this run, or any run made before the "
        "update, will not help.",
    ),
}
#: How each identity refusal's sentence starts, for a publisher that sends the sentence as `error`
#: without the `reason` (it sends `reason` for its identity refusals, `rerun_untagged` and
#: `concurrent_publish` since they were added; before that, the runtime's refusals and an
#: overlapping publish came without one). The runtime's sentences live in
#: pippa_agentapp_runtime/deploys.py; the event refusal and the last four are the publisher's own,
#: in hackathon/publisher/handler.py. Compared after curly apostrophes are made straight.
IDENTITY_SENTENCES = (
    ("Only the builder this repository belongs to can publish it", "actor_not_builder"),
    ("Only a push to main publishes", "not_a_push"),
    ("This publish used a GitHub sign-in token that had already been used", "token_replayed"),
    ("Pippa doesn't know which GitHub account this repository belongs to", "builder_identity_unknown"),
    ("This workflow run is older than the one that published the live version of this app", "stale_run"),
    ("This is a re-run of an earlier workflow run, and Pippa cannot tell whether it is older", "rerun_untagged"),
    ("this repository is not private, so it cannot publish through the private pipeline", "visibility_not_allowed"),
    ("Another publish of this app is running at the same time", "concurrent_publish"),
)
CURLY_APOSTROPHE = "\N{RIGHT SINGLE QUOTATION MARK}"
#: A runtime warning about an API key the app declares but its builder has not added yet, e.g.
#: "Published, but it can't run until you add OPENWEATHER_API_KEY in Created by me."
KEYS_MISSING = re.compile(r"\b(?:can[\u2019']?t|cannot|can not)\s+run\s+until\s+you\s+add\b", re.IGNORECASE)

MAX_BUNDLE_BYTES = 3 * 1024 * 1024
#: Folders never packaged, at any depth. `.claude` holds the builder's Claude Code settings (the
#: templates ship `.claude/settings.json`): for building the app, never part of it.
SKIP_DIRS = frozenset({
    ".git", ".github", "vendor", "scripts", "__pycache__", ".pytest_cache",
    ".venv", "venv", "docs", ".ruff_cache", ".idea", ".claude",
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


def code_span(text: str) -> str:
    """Text as an inline Markdown code span: never a link, a mention or HTML, whatever it holds."""
    text = " ".join(text.split())
    longest = run = 0
    for char in text:
        run = run + 1 if char == "`" else 0
        longest = max(longest, run)
    marker = "`" * (longest + 1)
    pad = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{marker}{pad}{text}{pad}{marker}"


def md_text(text: str, limit: int = NAME_LIMIT) -> str:
    """Text from manifest.yaml (a name, a version) as plain words in a Markdown comment.

    Markdown punctuation is escaped, so it never becomes a link, an image or HTML, and neither
    does a heading or a list marker at the start. What GitHub links after rendering, where
    escaping does not help (GITHUB_REFERENCE: "@someone" would notify that account, a web
    address, "#12"), goes in a code span of its own. A name without any of it reads as written.
    """
    text = truncate(" ".join(text.split()), limit)
    pieces, start = [], 0
    for found in GITHUB_REFERENCE.finditer(text):
        pieces += [MARKDOWN_SPECIAL.sub(r"\\\1", text[start:found.start()]), code_span(found.group())]
        start = found.end()
    pieces.append(MARKDOWN_SPECIAL.sub(r"\\\1", text[start:]))
    marker = MARKDOWN_LINE_START.match(pieces[0])
    if marker:
        last = marker.end() - 1
        pieces[0] = f"{pieces[0][:last]}\\{pieces[0][last:]}"
    return "".join(pieces)


def example_phrase(manifest: Mapping[str, Any] | None) -> str:
    """The manifest's first example phrase, to suggest what to ask Pippa; "" when it has none."""
    outcomes = (manifest or {}).get("outcomes")
    for outcome in outcomes if isinstance(outcomes, list) else []:
        phrases = outcome.get("example_phrases") if isinstance(outcome, dict) else None
        for phrase in phrases if isinstance(phrases, list) else []:
            if isinstance(phrase, str) and phrase.strip():
                return truncate(" ".join(phrase.split()), PHRASE_LIMIT)
    return ""


def next_version(version: str) -> str:
    """The patch release after version (0.2.0 -> 0.2.1), or "" for a number of another shape."""
    match = PATCH_VERSION.fullmatch(version)
    if not match:
        return ""
    major, minor, patch = (int(part) for part in match.groups())
    return f"{major}.{minor}.{patch + 1}"


def review_guide_url(environ: Mapping[str, str]) -> str:
    """Link to the review guide in this repository, or "" when the run does not say which one."""
    server = environ.get("GITHUB_SERVER_URL", "").rstrip("/")
    repository = environ.get("GITHUB_REPOSITORY", "")
    if not GITHUB_SERVER.fullmatch(server) or not GITHUB_REPOSITORY.fullmatch(repository):
        return ""
    return f"{server}/{repository}/blob/HEAD/{REVIEW_GUIDE}"


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
    """A published version, and the comment that tells its builder how to reach it in Pippa.

    GitHub emails the comment to whoever pushed, so it is written as that email: what happened,
    how to try this version in Pippa, and what to do when it is ready. Which steps apply depends
    on `change` (the app's first version, a new version number, or a draft pushed again) and on
    whether the version is public at once (`review_status` approved: review switched off).
    """

    kind = "success"
    state = "success"

    def __init__(
        self,
        run_url: str,
        reply: dict[str, Any],
        warnings: str = "",
        manifest: Mapping[str, Any] | None = None,
        guide_url: str = "",
    ) -> None:
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
        change = reply.get("change")
        #: One of CHANGES, or "" from a publisher or runtime that does not say.
        self.change = change if change in CHANGES else ""
        live = reply.get("live_version")
        #: The approved version everybody else gets, or "" when the app is not public.
        self.live_version = live.strip() if isinstance(live, str) else ""
        self.public = reply.get("review_status") == APPROVED
        self.phrase = example_phrase(manifest)
        self.guide_url = guide_url

    @property
    def description(self) -> str:
        state = "Live" if self.public else "Published as a private draft"
        if self.keys_missing:
            return f"{state}: {self.app_id} v{self.version}; add its API keys in Pippa"
        return f"{state}: {self.app_id} v{self.version}"

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
        sections = [self._headline()]
        if self.keys_missing:
            sections.append(self._keys())
        sections.append(self._try_it())
        if not self.public:
            sections.append(self._when_ready())
        sections.append(self._footer())
        if self.all_warnings:
            sections.append(
                "#### Warnings\n\nThese didn't stop the publish. Fix them and push again.\n\n"
                f"{fence(self.all_warnings)}"
            )
        return "\n\n".join(section for section in sections if section)

    # The pieces of the message. Names and numbers from manifest.yaml go through md_text, the
    # example phrase through code_span: none of them can become a link, HTML or a mention.

    @property
    def _name(self) -> str:
        return md_text(self.name)

    @property
    def _v(self) -> str:
        return md_text(self.version, VERSION_LIMIT)

    @property
    def _badge(self) -> str:
        return f"**via {self._name} {self._v}**"

    @property
    def _ask(self) -> str:
        if self.phrase:
            return f"Ask Pippa something your app does, for example {code_span(self.phrase)}."
        return "Ask Pippa something your app does."

    def _reinstall(self, lead: str) -> str:
        """`lead` ends where the instruction starts: "..., press" or "...? Press"."""
        return (
            f"{lead} **Uninstall** on the card, then **Install v{self._v}**: Pippa picks up that wording "
            "when the app is installed."
        )

    def _headline(self) -> str:
        name, v = self._name, self._v
        live = md_text(self.live_version, VERSION_LIMIT)
        if self.change == CHANGE_SAME_VERSION:
            return (
                f"**{name} {v} is updated.** You pushed version {v} again while it's still a draft, so "
                f"its code was replaced. Anyone on {v} gets the new code straight away, you included if "
                "it's installed for you."
            )
        if self.public:
            return f"**{name} {v} is published, and it's live:** anyone in Pippa can install it."
        if self.change == CHANGE_FIRST:
            return (
                f"**{name} is published.** Version {v} is in Pippa now as a private draft: only you can "
                "see it until Pippa Labs approves it."
            )
        headline = f"**{name} {v} is published** as a private draft: only you and your testers can get it."
        if self.change == CHANGE_NEW_VERSION:
            if self.live_version:
                return f"{headline} Everyone else keeps using {live}, your live version, until {v} is approved."
            return f"{headline} Your app isn't public yet."
        return f"{headline[:-1]} until Pippa Labs approves it."

    def _keys(self) -> str:
        keys = "\n".join(f" WARN {sentence}" for sentence in self.keys_missing)
        return (
            "#### First, add its API keys\n\n"
            f"{self._name} can't run until you add them. Open {APPS_LINK}, choose **Created by me**, "
            "and press **Add** next to each key under **Keys** on the app's card. No push is needed: "
            "the keys stay in Pippa, never in GitHub.\n\n"
            f"{fence(keys)}"
        )

    def _try_it(self) -> str:
        v = self._v
        if self.change == CHANGE_FIRST:
            return (
                "#### Try it in Pippa\n\n"
                f"1. Open {APPS_LINK}, signed in with the Pippa account your invitation was for.\n"
                "2. Choose **Created by me**. Your app has a card there.\n"
                f"3. Press **Install v{v}**.\n"
                f"4. {self._ask}\n\n"
                f"When your app answers, the reply carries the badge {self._badge}, so you know it was "
                "your app.\n\n"
                "While you're building, switch on **Always use my latest version** on the card: then "
                "every push is what answers you in chat."
            )
        if self.change == CHANGE_NEW_VERSION:
            return (
                "#### Try the new version\n\n"
                "You stay on the version you installed until you switch:\n\n"
                f"1. Open {APPS_LINK} and choose **Created by me**.\n"
                "2. On your app's card, press **Update to latest**. If **Always use my latest version** "
                f"is on, you have {v} already.\n"
                f"3. {self._ask} The reply should carry the badge {self._badge}.\n\n"
                + ("" if self.public else
                   f"Your testers get {v} too, unless you chose a particular version for them under "
                   "**Testers get**.\n\n")
                + self._reinstall(
                    "If **Always use my latest version** is on and you changed the app's name, summary "
                    "or example phrases, press"
                )
            )
        if self.change == CHANGE_SAME_VERSION:
            return (
                "#### Try it\n\n"
                f"{self._ask} The reply should carry the badge {self._badge}. Not installed yet? Open "
                f"{APPS_LINK}, choose **Created by me** and press **Install v{v}**.\n\n"
                + self._reinstall("Changed the app's name, summary or example phrases? Press")
            )
        return (
            "#### Try it in Pippa\n\n"
            f"1. Open {APPS_LINK} and choose **Created by me**.\n"
            f"2. Press **Install v{v}** if your app isn't installed yet, or **Update to latest** if an "
            "older version is. With **Always use my latest version** on, you have it already.\n"
            f"3. {self._ask} The reply should carry the badge {self._badge}."
        )

    def _when_ready(self) -> str:
        v = self._v
        submit = f"Under **Versions** on the card, press **Submit for review** next to {v}."
        if self.change == CHANGE_SAME_VERSION:
            following = next_version(self.version)
            example = f" (to {following}, say)" if following else ""
            return (
                f"#### When it's ready\n\n{submit} From then on {v} can't change: for anything after "
                f"that, raise `version:` in `manifest.yaml`{example} and push."
            )
        if self.change == CHANGE_NEW_VERSION and self.live_version:
            live = md_text(self.live_version, VERSION_LIMIT)
            return (
                f"#### When it's ready\n\n{submit} Once it's approved, it replaces {live} for everyone: "
                "people who have your app move to it the next time they ask Pippa. If "
                f"{v} adds something new your app can do, they get an **Update** button instead, and "
                "choose."
            )
        if self.change == CHANGE_NEW_VERSION:
            return (
                f"#### When it's ready\n\n{submit} Pippa Labs reviews it, and once it's approved anyone can "
                "install it."
            )
        return (
            "#### When it's ready\n\n"
            "- **Let someone try it:** press **Share for testing** on the card and add the email "
            "address they use for Pippa.\n"
            f"- **Make it public:** under **Versions** on the card, press **Submit for review** next to "
            f"{v}. Pippa Labs reviews it, and once it's approved anyone can install it."
        )

    def _footer(self) -> str:
        lines = []
        if self.change in (CHANGE_FIRST, ""):
            lines.append("Can't see your app? Reload the Apps page: it loads the list only once.")
        if self.guide_url:
            lines.append(
                f"More about versions, testers and review: [{REVIEW_GUIDE.rsplit('/', 1)[-1]} in your "
                f"repository]({self.guide_url})"
            )
        return "\n\n".join(lines)

    @property
    def summary(self) -> str:
        outcomes = ", ".join(code_span(item) for item in self.outcomes) or "-"
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

    def __init__(self, run_url: str, reference: str, warnings: str = "", guide_url: str = "") -> None:
        self.run_url = run_url
        self.reference = reference
        self.warnings = warnings
        self.guide_url = guide_url

    def code(self, error: str) -> CodeFailure:
        return CodeFailure(self.run_url, error)

    def refused(self, detail: str, reason: str = "") -> Refusal:
        return Refusal(self.run_url, detail, reason)

    def platform(self, reference: str | None = None) -> PlatformFailure:
        return PlatformFailure(self.run_url, reference or self.reference)

    def from_check(self, result: str, verdict: str, details: str, old_workflow: bool = False) -> Outcome | None:
        """Returns the outcome decided by the check job, or None when publishing may go on.

        With `old_workflow`, a check that failed without a verdict is the old workflow's check job
        that can no longer pass (OLD_WORKFLOW_REASON): refused, not a platform error.
        """
        if result == "success":
            return None
        if result == "failure" and verdict == "failed":
            return self.code(details)
        if result == "failure" and old_workflow:
            LOG.warning("Check job of the workflow made before the split failed (verdict=%r); push a new commit",
                        verdict)
            return self.refused("", OLD_WORKFLOW_REASON)
        LOG.error("Check job ended with result=%r verdict=%r", result, verdict)
        return self.platform()

    def from_reply(self, reply: Any, manifest: Mapping[str, Any] | None = None) -> Outcome:
        if not isinstance(reply, dict):
            raise PlatformError(f"Publisher reply is not an object: {reply!r:.400}")
        if reply.get("ok") is True:
            missing = [name for name in SUCCESS_FIELDS if not reply.get(name)]
            if missing:
                raise PlatformError(f"Publisher success reply lacks {missing}: {reply!r:.400}")
            published = Published(self.run_url, reply, self.warnings, manifest, self.guide_url)
            notes = "".join(f"  Warning  {sentence}\n" for sentence in published.runtime_warnings)
            banner = "YOUR APP IS LIVE IN PIPPA" if published.public else "YOUR APP IS PUBLISHED IN PIPPA"
            # One line each: a line break in a name from manifest.yaml could otherwise start a line
            # of the log with "::", which the runner reads as a workflow command.
            name, app_id, version, can_do = (
                " ".join(text.split())
                for text in (published.name, published.app_id, published.version, ", ".join(published.outcomes))
            )
            print(
                f"\n{'=' * 68}\n  {banner}\n\n"
                f"  App      {name}  ({app_id} v{version})\n"
                f"  Can do   {can_do or '-'}\n{notes}{'=' * 68}"
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


MANIFEST_NOT_A_FILE = "ERROR: manifest.yaml must be a regular file in the repository, not a link or a folder."


def read_regular_file(path: Path) -> str:
    """The UTF-8 text of path, opened without following a link and only if it is a regular file.

    The last check is made on the opened file itself, so a link or a folder put in its place
    after an earlier look is still never read.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    with os.fdopen(os.open(path, flags), "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise CodeError(MANIFEST_NOT_A_FILE)
        return handle.read().decode("utf-8")


def load_manifest(root: Path) -> dict[str, Any]:
    """Reads manifest.yaml, refusing a link or anything else that is not a regular file.

    It runs in the job that holds the publish role's credentials, and a YAML error quotes the
    line it stopped at in the commit comment, so it never reads what a link points at (the
    Packager leaves links out as well).
    """
    import yaml

    path = root / "manifest.yaml"
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise CodeError(MANIFEST_NOT_A_FILE)
    try:
        manifest = yaml.safe_load(read_regular_file(path))
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
    #: The calling workflow is the one made before the check was split (remove after 2026-12-20).
    old_workflow: bool = False


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
        decided = self.factory.from_check(state.result, state.verdict, state.details, state.old_workflow)
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
        return self.factory.from_reply(reply, manifest)


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
    parser.add_argument("--old-workflow", action="store_true",
                        help="The workflow made before the check was split (remove after 2026-12-20)")
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
        old_workflow=args.old_workflow,
    )
    run = PublishRun(
        app_dir=args.app_dir,
        factory=OutcomeFactory(args.run_url, args.reference, read_details(args.check_warnings_file),
                               review_guide_url(os.environ)),
        token_provider=ActionsTokenProvider(),
        publisher_factory=LambdaPublisher,
    )
    outcome = run.execute(state)
    ReportWriter(args.out_dir).write(outcome)
    print(f"\n{outcome.message}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
