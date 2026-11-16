"""Unit tests for personal_data.py, with real git repositories in tmp_path.

Every address here is invented. The ones that must be refused are at `college.local`
(reserved for local networks, never a real mailbox) or, for commits, at example.com:
a commit email is refused unless it is a GitHub no-reply address.
"""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.dont_write_bytecode = True
MODULE = Path(__file__).resolve().parent.parent / "personal_data.py"
spec = importlib.util.spec_from_file_location("personal_data", MODULE)
pd = importlib.util.module_from_spec(spec)
sys.modules["personal_data"] = pd
spec.loader.exec_module(pd)

PERSONAL = "ada.lovelace" + "@" + "college.local"
OTHER = "grace" + "@" + "college.local"
EXAMPLE = "builder@example.com"
NOREPLY = "1234567+tide-watcher@users.noreply.github.com"
BOT = "41898282+github-actions[bot]@users.noreply.github.com"
WEB_FLOW = "noreply@github.com"
ZERO = "0" * 40


# ---- helpers ----

def git(repo: Path, *args: str, author: str = NOREPLY, committer: str | None = None) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Tide Watcher", "GIT_AUTHOR_EMAIL": author,
        "GIT_COMMITTER_NAME": "Tide Watcher", "GIT_COMMITTER_EMAIL": committer or author,
        "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull,
    }
    result = subprocess.run(["git", "-C", str(repo), *args], env=env, capture_output=True, text=True, check=True)
    return result.stdout.strip()


def new_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "app"
    repo.mkdir()
    git(repo, "init", "-q")
    git(repo, "checkout", "-q", "-b", "main")
    return repo


def commit(repo: Path, name: str, text: str = "x = 1\n", author: str = NOREPLY, committer: str | None = None) -> str:
    (repo / name).write_text(text)
    git(repo, "add", name)
    git(repo, "commit", "-q", "-m", f"Add {name}", author=author, committer=committer)
    return git(repo, "rev-parse", "HEAD")


def event_file(tmp_path: Path, private: bool | None = False, **fields) -> Path:
    event = dict(fields)
    if private is not None:
        event["repository"] = {"full_name": "Pippa-Labs/pippa-hack-tide-times", "private": private}
    path = tmp_path / "event.json"
    path.write_text(json.dumps(event))
    return path


def scan(repo: Path, tmp_path: Path, event_name: str = "", event: Path | None = None, sha: str = "",
         capsys=None) -> tuple[int, str, str]:
    warnings = tmp_path / "warnings.txt"
    args = ["--app-dir", str(repo), "--warnings-file", str(warnings), f"--event-name={event_name}",
            f"--event-path={event or ''}", f"--sha={sha}"]
    code = pd.main(args)
    out = capsys.readouterr().out if capsys else ""
    return code, out, warnings.read_text() if warnings.exists() else ""


# ---- addresses, masking and the allow-list ----

@pytest.mark.parametrize("text", [
    f"Contact {PERSONAL} for help",
    f"<{PERSONAL}>",
    f"mailto:{PERSONAL}",
    f'owner_email: "{PERSONAL}"',
    f"https://user:pw{'@'}college.local/x",
])
def test_addresses_are_found(text):
    assert len(list(pd.addresses(text))) == 1


@pytest.mark.parametrize("text", [
    "owner_email: you@example.com",
    "a@example.org b@example.net c@mail.example.com",
    "student@college.example x@site.test y@thing.invalid z@box.localhost",
    f"git config user.email {NOREPLY}",
    f"{BOT}",
    "<img src='icon@2x.png'> and logo@3x.webp",
    "    @app.route('/x')\n@pytest.fixture",
    "npm install left-pad@1.3.0 and pip install requests==2.0",
    "version 0.1.0@main and a@b.c",
])
def test_allowed_or_not_an_address(text):
    assert list(pd.addresses(text)) == []


def test_example_lookalikes_are_not_allowed():
    for domain in ("example.co", "myexample.com", "example.com.college.local", "notexample.org"):
        assert list(pd.addresses(f"x@{domain}")) == [f"x@{domain}"], domain


@pytest.mark.parametrize("address, masked", [
    (PERSONAL, "a***@college.local"),
    ("first.last", "f***"),
    ("", "?***"),
    ("x@lan\x00host\t.local", "x***@lanhost.local"),
])
def test_mask_shows_only_the_first_character_and_the_domain(address, masked):
    assert pd.mask(address) == masked


@pytest.mark.parametrize("email, noreply", [
    (NOREPLY, True),
    ("tide-watcher@users.noreply.github.com", True),
    (BOT, True),
    (WEB_FLOW, True),
    ("1234567+Tide@Users.NoReply.GitHub.com", True),
    (EXAMPLE, False),
    (PERSONAL, False),
    ("noreply@github.com.college.local", False),
    ("x@users.noreply.github.com.college.local", False),
    ("first.last", False),
])
def test_github_noreply(email, noreply):
    assert pd.is_github_noreply(email) is noreply


# ---- files ----

def test_file_findings_name_file_and_line_and_mask_the_address(tmp_path, capsys):
    repo = tmp_path / "app"
    (repo / "docs").mkdir(parents=True)
    (repo / "docs" / "notes.md").write_text(f"# Notes\n\nAsk {PERSONAL} or {PERSONAL}, then {OTHER}.\n")
    (repo / "manifest.yaml").write_text("owner_email: you@example.com\n")
    code, out, warnings = scan(repo, tmp_path, capsys=capsys)
    assert code == 1
    assert out.splitlines() == [
        " FAIL docs/notes.md:3 contains the email address a***@college.local",
        " FAIL docs/notes.md:3 contains the email address g***@college.local",
        f"       {pd.FILE_FIX}",
    ]
    assert PERSONAL not in out and "lovelace" not in out
    assert warnings == ""


def test_clean_files_pass(tmp_path, capsys):
    repo = tmp_path / "app"
    repo.mkdir()
    (repo / "app.py").write_text(f"# by {NOREPLY}\nAUTHOR = 'student@college.example'\n")
    assert scan(repo, tmp_path, capsys=capsys)[:2] == (0, "")


def test_binary_and_large_files_are_skipped(tmp_path, capsys):
    repo = tmp_path / "app"
    repo.mkdir()
    (repo / "photo.png").write_bytes(b"\x89PNG\r\n\x1a\n\0\0" + PERSONAL.encode())
    (repo / "big.txt").write_text(PERSONAL + "\n" + "x" * (pd.MAX_FILE_BYTES + 1))
    (repo / "small.txt").write_text("x" * 10)
    assert scan(repo, tmp_path, capsys=capsys)[0] == 0


def test_symlinks_are_not_followed(tmp_path, capsys):
    outside = tmp_path / "outside.txt"
    outside.write_text(PERSONAL)
    repo = tmp_path / "app"
    repo.mkdir()
    try:
        os.symlink(outside, repo / "link.txt")
    except OSError:
        pytest.skip("this system cannot make symlinks")
    assert scan(repo, tmp_path, capsys=capsys)[0] == 0


def test_only_tracked_files_of_a_repository_are_read(tmp_path, capsys):
    repo = new_repo(tmp_path)
    commit(repo, "app.py")
    (repo / "untracked.txt").write_text(PERSONAL)
    (repo / ".git" / "description").write_text(PERSONAL)
    assert scan(repo, tmp_path, capsys=capsys)[0] == 0
    git(repo, "add", "untracked.txt")
    assert scan(repo, tmp_path, capsys=capsys)[0] == 1


def test_findings_are_listed_up_to_a_limit_with_the_fix_once(tmp_path, capsys):
    repo = tmp_path / "app"
    repo.mkdir()
    (repo / "list.txt").write_text("\n".join(f"n{n}@college.local" for n in range(pd.MAX_LISTED + 5)))
    code, out, _ = scan(repo, tmp_path, capsys=capsys)
    lines = out.splitlines()
    assert code == 1
    assert len([line for line in lines if line.startswith(" FAIL list.txt:")]) == pd.MAX_LISTED
    assert lines[-2] == " FAIL 5 more not listed"
    assert lines[-1] == f"       {pd.FILE_FIX}"


# ---- which commits a run checks ----

def pushed(repo, tmp_path, event_name, sha, **event) -> list[str]:
    git_ = pd.Git(repo)
    return [c.sha for c in pd.PushedCommits(git_, event_name, event, sha).commits()]


def test_push_checks_the_commits_after_before(tmp_path):
    repo = new_repo(tmp_path)
    first = commit(repo, "a.py")
    second = commit(repo, "b.py")
    third = commit(repo, "c.py")
    assert pushed(repo, tmp_path, "push", third, before=first, after=third) == [third, second]


def test_force_push_over_a_known_commit_checks_only_the_new_ones(tmp_path):
    repo = new_repo(tmp_path)
    base = commit(repo, "a.py")
    old_tip = commit(repo, "b.py")
    git(repo, "branch", "old", old_tip)
    git(repo, "reset", "-q", "--hard", base)
    new_tip = commit(repo, "c.py")
    assert pushed(repo, tmp_path, "push", new_tip, before=old_tip, after=new_tip, forced=True) == [new_tip]


def test_force_push_over_an_unknown_commit_uses_the_listed_commits(tmp_path):
    repo = new_repo(tmp_path)
    commit(repo, "a.py")
    second = commit(repo, "b.py")
    third = commit(repo, "c.py")
    listed = [{"id": second, "author": {"email": NOREPLY}, "committer": {"email": NOREPLY}},
              {"id": "f" * 40, "author": {"email": EXAMPLE}, "committer": {"email": WEB_FLOW}}]
    commits = pd.PushedCommits(pd.Git(repo), "push", {"before": "e" * 40, "after": third, "commits": listed},
                               third).commits()
    assert [c.sha for c in commits] == [second, third, "f" * 40]
    assert commits[-1] == pd.Commit("f" * 40, EXAMPLE, WEB_FLOW)


def test_force_push_over_an_unknown_commit_without_a_list_checks_the_pushed_commit(tmp_path):
    repo = new_repo(tmp_path)
    commit(repo, "a.py")
    tip = commit(repo, "b.py")
    assert pushed(repo, tmp_path, "push", tip, before="e" * 40, after=tip, commits=[]) == [tip]


def test_first_push_checks_the_whole_history(tmp_path):
    repo = new_repo(tmp_path)
    first = commit(repo, "a.py")
    second = commit(repo, "b.py")
    assert pushed(repo, tmp_path, "push", second, before=ZERO, after=second, created=True) == [second, first]


def test_first_push_with_listed_commits_checks_those(tmp_path):
    repo = new_repo(tmp_path)
    commit(repo, "a.py")
    second = commit(repo, "b.py")
    listed = [{"id": second, "author": {"email": NOREPLY}, "committer": {"email": NOREPLY}}]
    assert pushed(repo, tmp_path, "push", second, before=ZERO, after=second, commits=listed) == [second]


def test_workflow_dispatch_checks_the_published_commit(tmp_path):
    repo = new_repo(tmp_path)
    commit(repo, "a.py", author=EXAMPLE)
    tip = commit(repo, "b.py")
    assert pushed(repo, tmp_path, "workflow_dispatch", tip) == [tip]


def test_history_is_capped(tmp_path, monkeypatch):
    monkeypatch.setattr(pd, "MAX_COMMITS", 2)
    repo = new_repo(tmp_path)
    for name in ("a.py", "b.py", "c.py"):
        tip = commit(repo, name)
    assert len(pushed(repo, tmp_path, "push", tip, before=ZERO, after=tip)) == 2


def test_a_pushed_commit_missing_from_the_checkout_cannot_be_checked(tmp_path, capsys):
    repo = new_repo(tmp_path)
    commit(repo, "a.py")
    event = event_file(tmp_path, before=ZERO, after="d" * 40)
    code = pd.main(["--app-dir", str(repo), "--event-name=push", f"--event-path={event}", f"--sha={'d' * 40}"])
    captured = capsys.readouterr()
    assert code == 2 and captured.out == ""
    assert "the pushed commit is not in the checkout" in captured.err


# ---- commit emails ----

def test_public_push_with_a_personal_commit_email_is_refused(tmp_path, capsys):
    repo = new_repo(tmp_path)
    base = commit(repo, "a.py")
    tip = commit(repo, "b.py", author=PERSONAL)
    event = event_file(tmp_path, private=False, before=base, after=tip)
    code, out, warnings = scan(repo, tmp_path, "push", event, tip, capsys=capsys)
    assert code == 1
    assert out == (
        f" FAIL commit {tip[:7]} (author and committer): Your commit shows the email address "
        "a***@college.local publicly. Set `git config user.email` to your GitHub no-reply address "
        "and amend the commit.\n"
    )
    assert PERSONAL not in out
    assert warnings == ""


def test_private_push_with_a_personal_commit_email_is_a_warning(tmp_path, capsys):
    repo = new_repo(tmp_path)
    base = commit(repo, "a.py")
    tip = commit(repo, "b.py", author=EXAMPLE, committer=WEB_FLOW)
    event = event_file(tmp_path, private=True, before=base, after=tip)
    code, out, warnings = scan(repo, tmp_path, "push", event, tip, capsys=capsys)
    assert code == 0 and out == ""
    assert warnings == (
        f" WARN commit {tip[:7]} (author): This commit shows the email address b***@example.com to "
        "everybody who can read this repository.\n"
        f"       {pd.PRIVATE_COMMIT_FIX}\n"
    )


def test_noreply_bot_and_web_flow_commits_pass(tmp_path, capsys):
    repo = new_repo(tmp_path)
    first = commit(repo, "a.py", author=BOT, committer=WEB_FLOW)
    tip = commit(repo, "b.py", author=NOREPLY, committer=WEB_FLOW)
    event = event_file(tmp_path, private=False, before=ZERO, after=tip, commits=[])
    assert scan(repo, tmp_path, "push", event, tip, capsys=capsys)[:2] == (0, "")
    assert first


def test_commits_before_the_push_are_not_checked_again(tmp_path, capsys):
    repo = new_repo(tmp_path)
    old = commit(repo, "a.py", author=PERSONAL)
    tip = commit(repo, "b.py")
    event = event_file(tmp_path, private=False, before=old, after=tip)
    assert scan(repo, tmp_path, "push", event, tip, capsys=capsys)[0] == 0


def test_one_line_per_address_with_its_commits(tmp_path, capsys):
    repo = new_repo(tmp_path)
    base = commit(repo, "a.py")
    shas = [commit(repo, f"f{n}.py", author=PERSONAL, committer=NOREPLY) for n in range(5)]
    shas.append(commit(repo, "g.py", author=NOREPLY, committer=OTHER))
    event = event_file(tmp_path, private=False, before=base, after=shas[-1])
    code, out, _ = scan(repo, tmp_path, "push", event, shas[-1], capsys=capsys)
    assert code == 1 and len(out.splitlines()) == 2
    author_line = next(line for line in out.splitlines() if "a***@college.local" in line)
    committer_line = next(line for line in out.splitlines() if "g***@college.local" in line)
    newest_first = [sha[:7] for sha in reversed(shas[:5])]
    assert author_line.startswith(f" FAIL commits {', '.join(newest_first[:3])} and 2 more (author): ")
    assert committer_line.startswith(f" FAIL commit {shas[-1][:7]} (committer): ")


def test_an_event_without_the_repository_counts_as_public(tmp_path, capsys):
    repo = new_repo(tmp_path)
    tip = commit(repo, "a.py", author=EXAMPLE)
    event = event_file(tmp_path, private=None)
    assert scan(repo, tmp_path, "workflow_dispatch", event, tip, capsys=capsys)[0] == 1


@pytest.mark.parametrize("repository, public", [
    ({"private": True}, False),
    ({"private": False}, True),
    ({"visibility": "private"}, False),
    ({"visibility": "internal"}, False),
    ({"visibility": "public"}, True),
    ({}, True),
    ("Pippa-Labs/x", True),
])
def test_visibility(repository, public):
    assert pd.repository_is_public({"repository": repository}) is public


def test_commit_and_file_findings_together_commits_first(tmp_path, capsys):
    repo = new_repo(tmp_path)
    base = commit(repo, "a.py")
    tip = commit(repo, "notes.md", text=f"ask {OTHER}\n", author=PERSONAL)
    event = event_file(tmp_path, private=False, before=base, after=tip)
    code, out, _ = scan(repo, tmp_path, "push", event, tip, capsys=capsys)
    lines = out.splitlines()
    assert code == 1
    assert lines[0].startswith(f" FAIL commit {tip[:7]}")
    assert lines[1] == " FAIL notes.md:1 contains the email address g***@college.local"
    assert lines[2] == f"       {pd.FILE_FIX}"


def test_warnings_go_to_stdout_without_a_warnings_file(tmp_path, capsys):
    repo = new_repo(tmp_path)
    tip = commit(repo, "a.py", author=EXAMPLE)
    event = event_file(tmp_path, private=True)
    assert pd.main(["--app-dir", str(repo), "--event-name=workflow_dispatch", f"--event-path={event}",
                    f"--sha={tip}"]) == 0
    assert capsys.readouterr().out.startswith(f" WARN commit {tip[:7]} (author and committer): ")


def test_event_from_the_actions_environment(tmp_path, capsys, monkeypatch):
    repo = new_repo(tmp_path)
    tip = commit(repo, "a.py", author=PERSONAL)
    event = event_file(tmp_path, private=False)
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_dispatch")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    monkeypatch.setenv("GITHUB_SHA", tip)
    assert pd.main(["--app-dir", str(repo)]) == 1
    assert "a***@college.local publicly" in capsys.readouterr().out


# ---- cannot run ----

def test_an_unreadable_event_cannot_be_checked(tmp_path, capsys):
    repo = new_repo(tmp_path)
    tip = commit(repo, "a.py")
    (tmp_path / "event.json").write_text("{not json")
    code = pd.main(["--app-dir", str(repo), "--event-name=push", f"--event-path={tmp_path / 'event.json'}",
                    f"--sha={tip}"])
    assert code == 2
    assert "the event file could not be read" in capsys.readouterr().err


def test_commits_cannot_be_checked_outside_a_repository(tmp_path, capsys):
    repo = tmp_path / "app"
    repo.mkdir()
    (repo / "app.py").write_text("x = 1\n")
    event = event_file(tmp_path)
    assert pd.main(["--app-dir", str(repo), "--event-name=push", f"--event-path={event}", "--sha=abc"]) == 2
    assert "is not a git repository" in capsys.readouterr().err


def test_a_missing_app_dir_cannot_be_checked(tmp_path, capsys):
    assert pd.main(["--app-dir", str(tmp_path / "nope")]) == 2


def test_errors_never_carry_an_address(tmp_path, capsys):
    repo = new_repo(tmp_path)
    commit(repo, "a.py", author=PERSONAL)
    event = event_file(tmp_path, before=ZERO, after="d" * 40)
    pd.main(["--app-dir", str(repo), "--event-name=push", f"--event-path={event}", f"--sha={'d' * 40}"])
    captured = capsys.readouterr()
    assert PERSONAL not in captured.out + captured.err
