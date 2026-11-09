"""Unit tests for dependency_guard.py."""

import importlib.util
import os
import sys
from pathlib import Path

import pytest

sys.dont_write_bytecode = True
MODULE = Path(__file__).resolve().parent.parent / "dependency_guard.py"
spec = importlib.util.spec_from_file_location("dependency_guard", MODULE)
dg = importlib.util.module_from_spec(spec)
sys.modules["dependency_guard"] = dg
spec.loader.exec_module(dg)


@pytest.fixture
def app(tmp_path):
    root = tmp_path / "app"
    root.mkdir()
    (root / "app.py").write_text("class App: pass\n")
    return root


def scan(root, requirements=None, name="requirements-dev.txt"):
    if requirements is not None:
        (root / name).write_text(requirements)
    return dg.GuardFactory.create(root).scan()


def problems(findings):
    return [f"{f.location} {f.problem}" for f in findings]


@pytest.mark.parametrize("text", [
    "",
    "# Tests\nPyYAML>=6.0\npytest>=8.0\n",
    "pytest==8.3.4 \\\n    --hash=sha256:abc\n",
    "requests[socks]>=2 ; python_version >= \"3.8\"\n",
    "--only-binary :all:\n--prefer-binary\npytest\n",
    "pytest  # the Pippa SDK pippa_agentapp_sdk is installed by the build\n",
])
def test_accepts_pypi_requirements(app, text):
    assert scan(app, text) == []


@pytest.mark.parametrize("line", [
    "pippa-agentapp-sdk",
    "pippa_agentapp_sdk==0.1.3",
    "Pippa.AgentApp-SDK>=0.1",
    "./vendor/pippa_agentapp_sdk-0.1.3-py3-none-any.whl",
    "pippa_agentapp_sdk @ https://example.org/x.whl",
    "-e git+https://example.org/x.git#egg=pippa_agentapp_sdk",
])
def test_refuses_the_sdk(app, line):
    findings = scan(app, f"pytest\n{line}\n")
    assert problems(findings) == ["requirements-dev.txt line 2 installs the Pippa SDK"]
    assert "Delete this line." in findings[0].fix


@pytest.mark.parametrize("line, option", [
    ("-i https://example.org/simple", "-i"),
    ("-ihttps://example.org/simple", "-ihttps://example.org/simple"),
    ("--index-url https://example.org/simple", "--index-url"),
    ("--index-url=https://example.org/simple", "--index-url"),
    ("--extra-index-url https://example.org/simple", "--extra-index-url"),
    ("--extra-index=https://example.org/simple", "--extra-index"),
    ("-f https://example.org/wheels/", "-f"),
    ("--find-links ./wheels", "--find-links"),
    ("--find-links=/tmp/wheels", "--find-links"),
])
def test_refuses_index_options(app, line, option):
    findings = scan(app, f"{line}\npytest\n")
    assert len(findings) == 1
    assert findings[0].location == "requirements-dev.txt line 1"
    assert findings[0].problem.startswith("sets a package index or download location (")
    assert option[:40] in findings[0].problem


@pytest.mark.parametrize("line", [
    "./vendor/tool-1.0-py3-none-any.whl",
    "tool-1.0-py3-none-any.whl",
    "https://example.org/tool-1.0-py3-none-any.whl",
    "vendor/tool",
    ".",
    "../tool",
    "/opt/tool",
    "~/tool",
    "tool.tar.gz",
    "file:///opt/tool",
    "tool @ file:///opt/tool",
    "-e ./tool",
    "--editable=../tool",
    "C:\\tools\\tool",
])
def test_refuses_local_sources(app, line):
    findings = scan(app, f"{line}\n")
    assert problems(findings) and problems(findings)[0].startswith(
        "requirements-dev.txt line 1 installs from a file or folder")


@pytest.mark.parametrize("line", [
    "pkg @ https://example.org/pkg-1.0.tar.gz",
    "pkg@https://example.org/pkg-1.0.tar.gz",
    "git+https://github.com/example/tool.git@v1#egg=tool",
    "git+ssh://git@github.com/example/tool.git",
    "tool @ git+https://github.com/example/tool.git",
    "-e git+https://github.com/example/tool.git#egg=tool",
    "--editable=hg+https://example.org/tool",
    "https://example.org/tool-1.0.tar.gz",
])
def test_refuses_direct_urls(app, line):
    findings = scan(app, f"{line}\n")
    assert problems(findings) and problems(findings)[0].startswith(
        "requirements-dev.txt line 1 installs from a URL")


def test_follows_includes_and_constraints(app):
    (app / "base.txt").write_text("pytest\n--extra-index-url https://example.org/simple\n")
    (app / "constraints.txt").write_text("pippa-agentapp-sdk==0.1.3\n")
    findings = scan(app, "-r base.txt\n-c constraints.txt\nPyYAML\n")
    assert problems(findings) == [
        "base.txt line 2 sets a package index or download location (--extra-index-url)",
        "constraints.txt line 1 installs the Pippa SDK",
    ]


def test_include_cycle_is_read_once(app):
    (app / "a.txt").write_text("-r requirements-dev.txt\npytest\n")
    assert scan(app, "-r a.txt\n") == []


@pytest.mark.parametrize("line, problem", [
    ("-r https://example.org/req.txt", "includes requirements from a URL"),
    ("--requirement=../outside.txt", "includes a file outside the repository"),
    ("-c /etc/constraints.txt", "includes a file outside the repository"),
])
def test_refuses_includes_from_outside(app, line, problem):
    findings = scan(app, f"{line}\n")
    assert findings and findings[0].problem.startswith(problem)


def test_scans_every_requirements_file(app):
    (app / "tools").mkdir()
    (app / "tools" / "requirements.txt").write_text("-i https://example.org/simple\n")
    findings = scan(app, "pytest\n")
    assert problems(findings) == ["tools/requirements.txt line 1 sets a package index or download location (-i)"]


def test_continuation_lines_are_joined(app):
    findings = scan(app, "pytest\n--extra-index-url \\\n  https://example.org/simple\n")
    assert problems(findings) == ["requirements-dev.txt line 2 sets a package index or download location "
                                  "(--extra-index-url)"]


def test_refuses_wheel_files_anywhere(app):
    (app / "vendor").mkdir()
    (app / "vendor" / "pippa_agentapp_sdk-0.1.3-py3-none-any.whl").write_bytes(b"PK")
    (app / "deep" / "er").mkdir(parents=True)
    (app / "deep" / "er" / "x.WHL").write_bytes(b"PK")
    (app / ".git").mkdir()
    (app / ".git" / "ignored.whl").write_bytes(b"PK")
    findings = scan(app, "pytest\n")
    assert [f.location for f in findings] == [
        "deep/er/x.WHL", "vendor/pippa_agentapp_sdk-0.1.3-py3-none-any.whl",
    ]
    assert 'git rm "vendor/pippa_agentapp_sdk-0.1.3-py3-none-any.whl"' in findings[1].fix


def test_wheel_symlink_counts(app, tmp_path):
    target = tmp_path / "x.whl"
    target.write_bytes(b"PK")
    os.symlink(target, app / "link.whl")
    assert [f.location for f in scan(app)] == ["link.whl"]


def test_one_finding_per_line(app):
    findings = scan(app, "./vendor/pippa_agentapp_sdk-0.1.3-py3-none-any.whl\n")
    assert len(findings) == 1


def test_render_matches_check_format():
    finding = dg.Finding("requirements-dev.txt line 3", "installs the Pippa SDK", "Delete this line.")
    assert finding.render() == " FAIL requirements-dev.txt line 3 installs the Pippa SDK\n       Delete this line."


def test_main_exit_codes(app, capsys):
    assert dg.main([f"--app-dir={app}"]) == 0
    (app / "requirements-dev.txt").write_text("pippa-agentapp-sdk\n")
    assert dg.main([f"--app-dir={app}"]) == 1
    assert " FAIL requirements-dev.txt line 1 installs the Pippa SDK" in capsys.readouterr().out
    assert dg.main([f"--app-dir={app / 'missing'}"]) == 2
