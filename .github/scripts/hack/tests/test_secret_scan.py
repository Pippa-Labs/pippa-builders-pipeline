"""Unit tests for secret_scan.py: a stub gitleaks, a faked download; plus the real gitleaks when it is installed.

Every key-shaped value here is built at run time from pieces and is not a real key, so this
file itself never trips a secret scanner. The real-binary tests run when HACK_TEST_GITLEAKS
names a gitleaks 8.30.1 binary (or `gitleaks` is on PATH); they are skipped otherwise.
"""

import hashlib
import importlib.util
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tomllib
import urllib.error
from pathlib import Path

import pytest

sys.dont_write_bytecode = True
MODULE = Path(__file__).resolve().parent.parent / "secret_scan.py"
spec = importlib.util.spec_from_file_location("hack_secret_scan", MODULE)
ss = importlib.util.module_from_spec(spec)
sys.modules["hack_secret_scan"] = ss
spec.loader.exec_module(ss)

#: Shaped like a GitHub personal access token; invented, and never written into this file whole.
FAKE_TOKEN = "gh" + "p_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
#: A high-entropy value assigned to a key-like name: gitleaks' generic-api-key rule.
FAKE_KEY = "Zx81" + "Qp0vLm7T" + "nR4wYc2K" + "bH9sJd6F"


def git(root, *args):
    env = {**os.environ, "GIT_AUTHOR_NAME": "Tide Watcher", "GIT_COMMITTER_NAME": "Tide Watcher",
           "GIT_AUTHOR_EMAIL": "1234567+tide-watcher@users.noreply.github.com",
           "GIT_COMMITTER_EMAIL": "1234567+tide-watcher@users.noreply.github.com",
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull}
    return subprocess.run(["git", "-C", str(root), *args], env=env, capture_output=True, text=True,
                          check=True).stdout.strip()


def commit_all(root, message="Change the app"):
    if not (root / ".git").exists():
        git(root, "init", "-q")
    git(root, "add", "-A")
    git(root, "commit", "-q", "--allow-empty", "-m", message)
    return git(root, "rev-parse", "HEAD")


@pytest.fixture
def app(tmp_path):
    root = tmp_path / "app"
    root.mkdir()
    (root / "app.py").write_text("class App:\n    pass\n")
    return root


def report_item(**extra):
    item = {"RuleID": "generic-api-key", "StartLine": 3, "File": "app.py", "Commit": "",
            "Secret": "REDACTED", "Match": "REDACTED", "Author": "Tide Watcher",
            "Email": "1234567+tide-watcher@users.noreply.github.com", "Fingerprint": "app.py:generic-api-key:3"}
    item.update(extra)
    return item


STUB = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "{calls}"
for a in "$@"; do case "$a" in --report-path=*) report="${{a#--report-path=}}";; --config=*) config="${{a#--config=}}";; esac; done
[ -n "${{STUB_CONFIG_COPY:-}}" ] && cp "$config" "$STUB_CONFIG_COPY"
if [ -n "${{STUB_FAIL_WITH_APP_CONFIG:-}}" ] && grep -q "From the app" "$config"; then echo "bad regex" >&2; exit 1; fi
printf '%s' "${{STUB_REPORT:-[]}}" > "$report"
exit "${{STUB_EXIT:-0}}"
"""


@pytest.fixture
def stub(tmp_path):
    path = tmp_path / "bin" / "gitleaks"
    path.parent.mkdir()
    path.write_text(STUB.format(calls=tmp_path / "gitleaks-calls.log"))
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def run_main(app, tmp_path, binary, monkeypatch=None, *extra, **env):
    for name, value in env.items():
        os.environ[name] = value
    try:
        out = io.StringIO()
        err = io.StringIO()
        old = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = out, err
        try:
            code = ss.main([f"--app-dir={app}", f"--work-dir={tmp_path / 'work'}", f"--gitleaks={binary}",
                            "--event-name=", *extra])
        finally:
            sys.stdout, sys.stderr = old
    finally:
        for name in env:
            os.environ.pop(name, None)
    return code, out.getvalue(), err.getvalue()


def calls(tmp_path):
    path = tmp_path / "gitleaks-calls.log"
    return path.read_text() if path.exists() else ""


# ---- what is scanned ----

class FakeGit(ss.Git):
    def __init__(self, known):
        self.known = set(known)

    def has_commit(self, sha):
        return sha in self.known


BEFORE = "a" * 40
AFTER = "b" * 40


def test_a_push_with_a_known_before_scans_the_pushed_range():
    target = ss.choose_target(FakeGit({BEFORE, AFTER}), "push", {"before": BEFORE, "after": AFTER}, AFTER)
    assert target == ss.Target(BEFORE, AFTER)
    assert target.is_range and target.describe() == "the pushed commits aaaaaaa..bbbbbbb"


@pytest.mark.parametrize("event_name, event, known", [
    ("push", {"before": "0" * 40, "after": AFTER}, {AFTER}),            # first push of the branch
    ("push", {"before": BEFORE, "after": AFTER}, {AFTER}),              # force push over a lost commit
    ("push", {"before": "", "after": AFTER}, {AFTER}),
    ("push", {"before": "HEAD~1", "after": AFTER}, {AFTER, "HEAD~1"}),  # never a revision expression
    ("push", {"before": "--all", "after": AFTER}, {AFTER, "--all"}),
    ("workflow_dispatch", {"before": BEFORE, "after": AFTER}, {BEFORE, AFTER}),
    ("", {}, set()),
])
def test_anything_but_a_known_range_scans_the_tree(event_name, event, known):
    target = ss.choose_target(FakeGit(known), event_name, event, AFTER)
    assert not target.is_range
    assert target.describe() == "the files of the checkout"


def test_the_range_is_passed_as_log_options_and_the_tree_as_a_directory(app, tmp_path, stub):
    base = commit_all(app)
    (app / "helper.py").write_text("X = 1\n")
    tip = commit_all(app)
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"before": base, "after": tip}))
    code, _, err = run_main(app, tmp_path, stub, None, "--event-name=push", f"--event-path={event}", f"--sha={tip}")
    assert code == 0, err
    line = calls(tmp_path).splitlines()[0]
    assert line.startswith("git ") and f"--log-opts={base}..{tip}" in line
    for flag in ("--redact", "--no-banner", "--report-format=json", "--exit-code=3"):
        assert flag in line
    assert f"the pushed commits {base[:7]}..{tip[:7]}: 0 finding(s)" in err

    (tmp_path / "gitleaks-calls.log").unlink()
    code, _, _ = run_main(app, tmp_path, stub)
    line = calls(tmp_path).splitlines()[0]
    assert line.startswith("dir ") and "--log-opts" not in line and line.endswith(" .")


# ---- findings: where, never what ----

def test_a_finding_names_file_line_commit_and_rule_never_the_value(app, tmp_path, stub):
    sha = "c" * 40
    report = [report_item(File="app.py", StartLine=38, Commit=sha, Secret=FAKE_KEY, Match=f'key = "{FAKE_KEY}"')]
    code, out, err = run_main(app, tmp_path, stub, STUB_REPORT=json.dumps(report), STUB_EXIT="3")
    assert code == 1
    assert out.startswith(" FAIL a value that looks like a key was committed in app.py:38 (commit ccccccc, "
                          "gitleaks rule generic-api-key); remove it, rotate the key, use app_secrets\n")
    assert "       Rotate the key at its provider first" in out
    assert "app_secrets in manifest.yaml" in out and "Created by me" in out
    for value in (FAKE_KEY, "tide-watcher", "Tide Watcher"):
        assert value not in out + err


def test_findings_are_deduplicated_sorted_and_listed_up_to_the_limit(app, tmp_path, stub):
    report = [report_item(File="b.py", StartLine=2), report_item(File="a.py", StartLine=9, RuleID="github-pat"),
              report_item(File="b.py", StartLine=2, RuleID="slack-bot-token"),
              report_item(File="a.py", StartLine=9), report_item(File="c.py", StartLine=1)]
    code, out, _ = run_main(app, tmp_path, stub, STUB_REPORT=json.dumps(report), STUB_EXIT="3")
    assert code == 1
    fails = [line for line in out.splitlines() if line.startswith(" FAIL")]
    assert fails == [
        f" FAIL a value that looks like a key was committed in {place} (gitleaks rule {rule}); remove it, "
        "rotate the key, use app_secrets"
        for place, rule in (("a.py:9", "github-pat"), ("b.py:2", "slack-bot-token"), ("c.py:1", "generic-api-key"))
    ]

    many = [report_item(File=f"f{n:02}.py") for n in range(ss.MAX_LISTED + 5)]
    _, out, _ = run_main(app, tmp_path, stub, STUB_REPORT=json.dumps(many), STUB_EXIT="3")
    assert out.count("committed in") == ss.MAX_LISTED
    assert " FAIL 5 more not listed" in out


def test_odd_report_fields_are_shown_safely(app, tmp_path, stub):
    report = [report_item(File="a\nb\x1b[31m.py", StartLine="7", Commit="not-a-sha", RuleID=None), "junk"]
    code, out, _ = run_main(app, tmp_path, stub, STUB_REPORT=json.dumps(report), STUB_EXIT="3")
    assert code == 1
    assert " FAIL a value that looks like a key was committed in ab[31m.py:0 (gitleaks rule ?);" in out


def test_a_clean_scan_prints_nothing(app, tmp_path, stub):
    code, out, err = run_main(app, tmp_path, stub)
    assert (code, out) == (0, "")
    assert "the files of the checkout: 0 finding(s)" in err


@pytest.mark.parametrize("exit_code, report", [("1", "[]"), ("0", "not json"), ("0", "{}")])
def test_a_gitleaks_failure_or_unreadable_report_is_a_scan_error(app, tmp_path, stub, exit_code, report):
    code, out, err = run_main(app, tmp_path, stub, STUB_EXIT=exit_code, STUB_REPORT=report)
    assert code == 2 and out == ""
    assert "secret check failed:" in err


def test_a_missing_app_dir_is_a_scan_error(tmp_path, stub):
    code, _, err = run_main(tmp_path / "missing", tmp_path, stub)
    assert code == 2 and "is not a directory" in err


# ---- the app's allowlists ----

BASE = ss.BASE_CONFIG.read_text()


def merged_config(app, tmp_path, stub, **env):
    copy = tmp_path / "config-copy.toml"
    code, out, err = run_main(app, tmp_path, stub, STUB_CONFIG_COPY=str(copy), **env)
    return code, out, err, copy.read_text()


def test_the_built_in_config_extends_gitleaks_and_allows_demo_keys_and_env_example():
    config = tomllib.loads(BASE)
    assert config["extend"] == {"useDefault": True}
    descriptions = [entry["description"] for entry in config["allowlists"]]
    assert len(descriptions) == 3
    regexes = [regex for entry in config["allowlists"] for regex in entry.get("regexes", [])]
    paths = [path for entry in config["allowlists"] for path in entry.get("paths", [])]
    assert r"(?i)^demo_key$" in regexes
    assert r"(^|/)\.env\.example$" in paths
    assert r"pragma:\s*allowlist\s+secret" in regexes
    assert "rules" not in config


def test_without_an_app_config_the_built_in_one_is_used_as_is(app, tmp_path, stub):
    code, _, _, config = merged_config(app, tmp_path, stub)
    assert code == 0
    assert config == BASE.rstrip("\n") + "\n"


def test_the_app_allowlists_are_added_and_its_rules_ignored(app, tmp_path, stub):
    (app / ".gitleaks.toml").write_text(
        'title = "mine"\n'
        "[extend]\nuseDefault = false\n"
        "[[rules]]\nid = \"nothing\"\nregex = '''x^'''\n"
        "[allowlist]\ndescription = \"fixtures\"\npaths = ['''^tests/fixtures/''']\n"
        "[[allowlists]]\nregexTarget = \"line\"\ncondition = \"AND\"\nregexes = ['''FAKE-\\d+''']\n"
        "stopwords = [\"sample\"]\ntargetRules = [\"generic-api-key\"]\nflavour = \"x\"\n"
    )
    code, _, err, config = merged_config(app, tmp_path, stub)
    assert code == 0
    parsed = tomllib.loads(config)
    assert parsed["extend"] == {"useDefault": True}
    assert "rules" not in parsed
    added = parsed["allowlists"][3:]
    assert added == [
        {"description": "From the app: fixtures", "paths": ["^tests/fixtures/"]},
        {"description": "From the app: ", "regexTarget": "line", "condition": "AND", "regexes": [r"FAKE-\d+"],
         "stopwords": ["sample"], "targetRules": ["generic-api-key"]},
    ]
    assert "Only the allowlists in .gitleaks.toml are used; ignored: extend, rules." in err
    assert "flavour in an allowlist of .gitleaks.toml is not used." in err


@pytest.mark.parametrize("text, note", [
    ("[allowlist\n", "could not be read (TOMLDecodeError)"),
    ("[allowlist]\npaths = 'x'\n", "must be a list of at most 100 one-line strings"),
    ('[allowlist]\npaths = ["a\\u0007b"]\n', "must be a list of at most 100 one-line strings"),
    ('[allowlist]\npaths = ["a\\nb"]\n', "must be a list of at most 100 one-line strings"),
    ("[allowlist]\nregexTarget = 'everything'\npaths = ['x']\n", "is not one of secret, match, line"),
    ("[allowlist]\ndescription = 'nothing to allow'\n", "lists no paths, regexes, stopwords or commits"),
    ("allowlists = 'x'\n", "is not a list of tables"),
])
def test_an_unusable_app_allowlist_is_left_out_with_a_note(app, tmp_path, stub, text, note):
    (app / ".gitleaks.toml").write_text(text)
    code, _, err, config = merged_config(app, tmp_path, stub)
    assert code == 0
    assert note in err
    assert "From the app" not in config


def test_a_huge_or_linked_app_config_is_not_used(app, tmp_path, stub):
    (app / ".gitleaks.toml").write_text("#" * (ss.MAX_APP_CONFIG_BYTES + 1))
    _, _, err, _ = merged_config(app, tmp_path, stub)
    assert "larger than 64 KB" in err
    (app / ".gitleaks.toml").unlink()
    target = tmp_path / "elsewhere.toml"
    target.write_text("[allowlist]\npaths = ['.*']\n")
    try:
        (app / ".gitleaks.toml").symlink_to(target)
    except OSError:
        pytest.skip("symlinks are not available")
    _, _, err, config = merged_config(app, tmp_path, stub)
    assert "is not a regular file" in err and "From the app" not in config


def test_allowlists_gitleaks_refuses_are_dropped_and_the_scan_runs_again(app, tmp_path, stub):
    (app / ".gitleaks.toml").write_text("[allowlist]\nregexes = ['''(?<=x)y''']\n")
    report = json.dumps([report_item()])
    code, out, err = run_main(app, tmp_path, stub, STUB_FAIL_WITH_APP_CONFIG="1", STUB_REPORT=report, STUB_EXIT="3")
    assert code == 1
    assert len(calls(tmp_path).splitlines()) == 2
    assert "       gitleaks could not use the allowlists in .gitleaks.toml, so they were not used." in out


def test_the_app_gitleaksignore_is_passed_only_when_it_is_a_file(app, tmp_path, stub):
    run_main(app, tmp_path, stub)
    assert f"--gitleaks-ignore-path={tmp_path / 'work' / 'no-ignore'}" in calls(tmp_path)
    (tmp_path / "gitleaks-calls.log").unlink()
    (app / ".gitleaksignore").write_text("app.py:generic-api-key:3\n")
    run_main(app, tmp_path, stub)
    assert f"--gitleaks-ignore-path={app.resolve() / '.gitleaksignore'}" in calls(tmp_path)


def test_gitleaks_environment_overrides_are_not_passed_on(app, tmp_path, stub):
    env_dump = tmp_path / "env.txt"
    stub.write_text(stub.read_text().replace('exit "${STUB_EXIT', f'env > {env_dump}\nexit "${{STUB_EXIT'))
    run_main(app, tmp_path, stub, GITLEAKS_CONFIG="/tmp/evil.toml")
    assert "GITLEAKS_CONFIG" not in env_dump.read_text()


# ---- the pinned download ----

def tarball(members):
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as archive:
        for name, data, kind in members:
            info = tarfile.TarInfo(name)
            info.type = kind
            info.size = len(data) if kind == tarfile.REGTYPE else 0
            archive.addfile(info, io.BytesIO(data) if kind == tarfile.REGTYPE else None)
    return out.getvalue()


class FakeDownloader(ss.Downloader):
    def __init__(self, data):
        self.data = data

    def fetch(self):
        return self.data


def test_the_download_is_refused_unless_its_sha256_is_the_pinned_one(tmp_path):
    data = tarball([("gitleaks", b"#!/bin/sh\n", tarfile.REGTYPE)])
    with pytest.raises(ss.ScanError, match="not the pinned one"):
        ss.GitleaksInstaller(FakeDownloader(data)).install(tmp_path)
    assert not (tmp_path / "gitleaks").exists()


def test_only_the_gitleaks_file_is_taken_from_the_archive(tmp_path):
    data = tarball([("../escape", b"x", tarfile.REGTYPE), ("README.md", b"x", tarfile.REGTYPE),
                    ("gitleaks", b"binary", tarfile.REGTYPE)])
    binary = ss.GitleaksInstaller(FakeDownloader(data), hashlib.sha256(data).hexdigest()).install(tmp_path)
    assert binary == tmp_path / "gitleaks" and binary.read_bytes() == b"binary"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["gitleaks"]
    assert not (tmp_path.parent / "escape").exists()


def test_an_archive_without_a_regular_gitleaks_file_is_refused(tmp_path):
    data = tarball([("gitleaks", b"", tarfile.SYMTYPE)])
    with pytest.raises(ss.ScanError, match="no gitleaks binary"):
        ss.GitleaksInstaller(FakeDownloader(data), hashlib.sha256(data).hexdigest()).install(tmp_path)


def test_the_pin_is_the_linux_x64_release():
    assert ss.GITLEAKS_URL.endswith(f"/v{ss.GITLEAKS_VERSION}/gitleaks_{ss.GITLEAKS_VERSION}_linux_x64.tar.gz")
    assert ss.GITLEAKS_URL.startswith("https://github.com/gitleaks/gitleaks/releases/download/")
    assert len(ss.GITLEAKS_SHA256) == 64


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_the_download_retries_then_gives_up(tmp_path):
    attempts, delays = [], []

    def opener(request, timeout):
        attempts.append((request.full_url, timeout))
        raise urllib.error.URLError("down")

    with pytest.raises(ss.ScanError, match="could not be downloaded"):
        ss.ReleaseDownloader(opener=opener, sleep=delays.append).fetch()
    assert [url for url, _ in attempts] == [ss.GITLEAKS_URL] * ss.DOWNLOAD_ATTEMPTS
    assert delays == [2.0, 4.0]


def test_the_download_succeeds_after_a_failure():
    results = [urllib.error.URLError("down"), Response(b"archive")]

    def opener(request, timeout):
        result = results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result

    assert ss.ReleaseDownloader(opener=opener, sleep=lambda _: None).fetch() == b"archive"


def test_main_downloads_when_no_binary_is_given_and_fails_closed(app, tmp_path):
    code = ss.main([f"--app-dir={app}", f"--work-dir={tmp_path / 'work'}", "--event-name="],
                   downloader=FakeDownloader(b"not the release"))
    assert code == 2


# ---- the real gitleaks ----

def real_gitleaks():
    candidate = os.environ.get("HACK_TEST_GITLEAKS") or shutil.which("gitleaks")
    if not candidate:
        pytest.skip("no gitleaks binary (set HACK_TEST_GITLEAKS)")
    version = subprocess.run([candidate, "version"], capture_output=True, text=True).stdout.strip()
    if version != ss.GITLEAKS_VERSION:
        pytest.skip(f"gitleaks {version} is not the pinned {ss.GITLEAKS_VERSION}")
    return Path(candidate)


def test_real_gitleaks_refuses_a_pushed_key_without_printing_it(app, tmp_path):
    binary = real_gitleaks()
    base = commit_all(app)
    (app / "weather.py").write_text(f'import os\n\nTOKEN = "{FAKE_TOKEN}"\n')
    (app / "config.py").write_text(f'\n\napi_key = "{FAKE_KEY}"\nNASA_API_KEY = "DEMO_KEY"\n')
    (app / ".env.example").write_text(f"OPENWEATHER_API_KEY=\nAPI_KEY={FAKE_KEY}\n")
    (app / "fixture.py").write_text(f'api_key = "{FAKE_KEY}"  # pragma: allowlist secret\n')
    (app / "marked.py").write_text(f'api_key = "{FAKE_KEY}"  # gitleaks:allow\n')
    tip = commit_all(app)
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"before": base, "after": tip}))
    code, out, err = run_main(app, tmp_path, binary, None, "--event-name=push", f"--event-path={event}",
                              f"--sha={tip}")
    assert code == 1, err
    fails = [line for line in out.splitlines() if line.startswith(" FAIL")]
    assert [line.split(" committed in ", 1)[1].split(" (", 1)[0] for line in fails] == ["config.py:3", "weather.py:3"]
    assert f"(commit {tip[:7]}, gitleaks rule github-pat)" in out
    for value in (FAKE_TOKEN, FAKE_KEY, "tide-watcher"):
        assert value not in out + err
    report = (tmp_path / "work" / "report.json").read_text()
    assert FAKE_TOKEN not in report and FAKE_KEY not in report


def test_real_gitleaks_scans_only_the_pushed_range(app, tmp_path):
    binary = real_gitleaks()
    (app / "old.py").write_text(f'api_key = "{FAKE_KEY}"\n')
    base = commit_all(app)
    (app / "helper.py").write_text("X = 1\n")
    tip = commit_all(app)
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"before": base, "after": tip}))
    code, out, err = run_main(app, tmp_path, binary, None, "--event-name=push", f"--event-path={event}",
                              f"--sha={tip}")
    assert (code, out) == (0, ""), err

    code, out, _ = run_main(app, tmp_path, binary)
    assert code == 1 and "old.py:1 (gitleaks rule generic-api-key)" in out


def test_real_gitleaks_honours_the_app_allowlist_and_gitleaksignore(app, tmp_path):
    binary = real_gitleaks()
    (app / "tests").mkdir()
    (app / "tests" / "data.py").write_text(f'api_key = "{FAKE_KEY}"\n')
    (app / "weather.py").write_text(f'TOKEN = "{FAKE_TOKEN}"\n')
    (app / ".gitleaks.toml").write_text("[allowlist]\npaths = ['''^tests/''']\n[[rules]]\nid = 'x'\nregex = 'y'\n")
    code, out, err = run_main(app, tmp_path, binary)
    assert code == 1, err
    assert "tests/data.py" not in out and "weather.py:1" in out
    (app / ".gitleaksignore").write_text("weather.py:github-pat:1\n")
    code, out, err = run_main(app, tmp_path, binary)
    assert (code, out) == (0, ""), err


def test_real_gitleaks_with_a_broken_app_regex_still_scans(app, tmp_path):
    binary = real_gitleaks()
    (app / "weather.py").write_text(f'TOKEN = "{FAKE_TOKEN}"\n')
    (app / ".gitleaks.toml").write_text("[allowlist]\nregexes = ['''(?<=lookbehind)x''']\n")
    code, out, err = run_main(app, tmp_path, binary)
    assert code == 1, err
    assert "weather.py:1" in out
    assert "gitleaks could not use the allowlists in .gitleaks.toml" in out
