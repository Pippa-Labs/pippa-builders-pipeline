"""Shared fixtures: stubbed gh, aws, gitleaks and python on PATH, and the Actions environment."""

import os
import shutil
import stat
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[4]
CHECK_ACTION_DIR = REPO / ".github" / "actions" / "hack-check"
PUBLISH_ACTION_DIR = REPO / ".github" / "actions" / "hack-publish"

MANIFEST = "manifest_version: 1\napp:\n  id: hack.smoke\n  name: Smoke\n  version: 0.1.0\n"
PLACEHOLDER = "app:\n  id: hack.your_app_name_here\n"

PIP_STUB = """#!/usr/bin/env bash
flags=""
if [ "${1:-}" = "-I" ]; then flags="-I"; fi
if [ "${1:-}" = "-I" ] && [ "${2:-}" = "-m" ] && [ "${3:-}" = "pip" ] && [ -n "${STUB_PIP:-}" ]; then shift; fi
if [ "${1:-}" = "-m" ] && [ "${2:-}" = "pip" ] && [ -n "${STUB_PIP:-}" ]; then
  {
    echo "pip ${*:3}"
    echo "FLAGS=${flags}"
    echo "CWD=${PWD}"
    echo "PIP_CONFIG_FILE=${PIP_CONFIG_FILE-unset}"
    echo "PIP_EXTRA_INDEX_URL=${PIP_EXTRA_INDEX_URL-unset}"
    prev=""
    for a in "$@"; do
      if [ "$prev" = "-r" ]; then echo "REQUIREMENTS $(cat "$a")"; fi
      if [ "$prev" = "--dest" ]; then dest="$a"; fi
      prev="$a"
    done
  } >> "$CALLS"
  if [ -n "${dest:-}" ] && [ "${STUB_PIP_EXIT:-0}" = "0" ] && [ -z "${STUB_PIP_NO_WHEEL:-}" ]; then
    printf 'PK' > "${dest}/pippa_agentapp_sdk-0.1.3-py3-none-any.whl"
  fi
  exit "${STUB_PIP_EXIT:-0}"
fi
exec PYTHON "$@"
"""

AWS_STUB = """#!/usr/bin/env bash
echo "aws $*" >> "$CALLS"
[ -n "${STUB_AWS_FAIL:-}" ] && case "$*" in *"$STUB_AWS_FAIL"*) echo "AccessDenied" >&2; exit 254;; esac
case "$1 $2" in
  "ssm get-parameter") printf '%s\\n' "${STUB_PARAMETER:-}" ;;
  "codeartifact get-repository-endpoint") echo "${STUB_ENDPOINT:-https://pippa-780886633355.d.codeartifact.eu-west-2.amazonaws.com/pypi/pippa-prod-codeartifact-euw2-hack-sdk/}" ;;
  "codeartifact get-authorization-token") echo "${STUB_TOKEN-tok-secret-value}" ;;
esac
"""

#: Writes STUB_GITLEAKS_REPORT (default: no findings) as the report and exits STUB_GITLEAKS_EXIT.
GITLEAKS_STUB = """#!/usr/bin/env bash
echo "gitleaks $*" >> "$CALLS"
for a in "$@"; do case "$a" in --report-path=*) report="${a#--report-path=}";; esac; done
printf '%s' "${STUB_GITLEAKS_REPORT:-[]}" > "$report"
exit "${STUB_GITLEAKS_EXIT:-0}"
"""


def write_exec(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def bin_dir(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    calls = tmp_path / "calls.log"
    write_exec(bindir / "python", PIP_STUB.replace("PYTHON", sys.executable))
    if not shutil.which("timeout"):
        write_exec(bindir / "timeout", "#!/usr/bin/env bash\nshift\nexec \"$@\"\n")
    write_exec(bindir / "gh", (
        "#!/usr/bin/env bash\n"
        f"echo \"gh $*\" >> {calls}\n"
        "for a in \"$@\"; do case \"$a\" in *.json) cat \"$a\" >> " + str(calls) + "; echo >> " + str(calls) + ";; esac; done\n"
    ))
    write_exec(bindir / "aws", AWS_STUB)
    write_exec(bindir / "gitleaks", GITLEAKS_STUB)
    return bindir


def base_env(tmp_path, bindir):
    runner_temp = tmp_path / "runner"
    runner_temp.mkdir(exist_ok=True)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {
        "PATH": f"{bindir}:{os.environ['PATH']}",
        "HOME": str(home),
        "CALLS": str(tmp_path / "calls.log"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PIP_CONFIG_FILE": os.devnull,
        "RUNNER_TEMP": str(runner_temp),
        "GITHUB_OUTPUT": str(tmp_path / "output.txt"),
        "GITHUB_ENV": str(tmp_path / "env.txt"),
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"),
        "GITHUB_REPOSITORY": "Pippa-Labs/pippa-hack-smoke",
        "GITHUB_SHA": "abc123",
        "GITHUB_RUN_ID": "99",
        "GITHUB_RUN_ATTEMPT": "1",
        "GITHUB_SERVER_URL": "https://github.com",
        "GITHUB_ACTIONS": "false",
        "HACK_GITLEAKS_BIN": str(bindir / "gitleaks"),
    }
    for name in ("output.txt", "env.txt", "summary.md"):
        (tmp_path / name).write_text("")
    return env


ROUTING_CLEAN = "print('Nothing here will stop your app being chosen.')"


def make_app(tmp_path, manifest=MANIFEST, check_body="print('Ready to push.')", requirements="",
             routing_body=ROUTING_CLEAN):
    app = tmp_path / "app"
    (app / "scripts").mkdir(parents=True, exist_ok=True)
    (app / "manifest.yaml").write_text(manifest)
    (app / "app.py").write_text("class App:\n    def handle(self, call):\n        return None\n")
    (app / "requirements-dev.txt").write_text(requirements)
    (app / "scripts" / "check.py").write_text(f"import sys\n{check_body}\n")
    if routing_body is not None:
        (app / "scripts" / "routing_check.py").write_text(f"import sys\n{routing_body}\n")
    return app


def read(path: Path) -> str:
    return path.read_text() if path.exists() else ""
