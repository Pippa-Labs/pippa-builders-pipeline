"""Unit tests for sdk_pin.py."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

sys.dont_write_bytecode = True
MODULE = Path(__file__).resolve().parent.parent / "sdk_pin.py"
spec = importlib.util.spec_from_file_location("sdk_pin", MODULE)
sp = importlib.util.module_from_spec(spec)
sys.modules["sdk_pin"] = sp
spec.loader.exec_module(sp)

SHA = "0123456789abcdef" * 4
VALUE = {
    "version": "0.1.3",
    "filename": "pippa_agentapp_sdk-0.1.3-py3-none-any.whl",
    "sha256": SHA,
    "layer_arn": "arn:aws:lambda:eu-west-2:111122223333:layer:pippa-hack-sdk:5",
    "frozen": False,
    "updated_at": "2026-11-08T00:00:00Z",
}


def run(tmp_path, value):
    parameter = tmp_path / "parameter.json"
    parameter.write_text(value if isinstance(value, str) else json.dumps(value))
    out = tmp_path / "sdk.txt"
    return sp.main([f"--parameter-file={parameter}", f"--out={out}"]), out


def test_writes_hash_locked_pin(tmp_path, capsys):
    rc, out = run(tmp_path, VALUE)
    assert rc == 0
    assert out.read_text() == f"pippa-agentapp-sdk==0.1.3 --hash=sha256:{SHA}\n"
    assert capsys.readouterr().out.strip() == "pippa-agentapp-sdk==0.1.3"


def test_uppercase_hash_is_normalised(tmp_path):
    rc, out = run(tmp_path, {**VALUE, "sha256": SHA.upper()})
    assert rc == 0 and SHA in out.read_text()


@pytest.mark.parametrize("change", [
    {"version": ""},
    {"version": "0.1.3rc1"},
    {"version": "0.1.3 --index-url x"},
    {"version": "0.1.3\npippa-other"},
    {"sha256": "abc"},
    {"sha256": "g" * 64},
    {"filename": "pippa_agentapp_sdk-0.1.2-py3-none-any.whl"},
    {"filename": "other-0.1.3-py3-none-any.whl"},
    {"filename": "pippa_agentapp_sdk-0.1.3.tar.gz"},
    {"version": 13},
])
def test_rejects_inconsistent_parameter(tmp_path, change, capsys):
    rc, out = run(tmp_path, {**VALUE, **change})
    assert rc == 2
    assert not out.exists()
    assert "SDK parameter rejected" in capsys.readouterr().err


@pytest.mark.parametrize("raw", ["", "not json", "[1]", "null"])
def test_rejects_unreadable_parameter(tmp_path, raw):
    rc, out = run(tmp_path, raw)
    assert rc == 2 and not out.exists()


def test_missing_file(tmp_path):
    assert sp.main([f"--parameter-file={tmp_path / 'none'}", f"--out={tmp_path / 'o'}"]) == 2


@pytest.mark.parametrize("version", ["0.2", "0.1.4.post1", "1.2.3.4"])
def test_final_release_versions_are_accepted(tmp_path, version):
    value = {**VALUE, "version": version, "filename": f"pippa_agentapp_sdk-{version}-py3-none-any.whl"}
    rc, out = run(tmp_path, value)
    assert rc == 0
    assert out.read_text().startswith(f"pippa-agentapp-sdk=={version} --hash=sha256:")


@pytest.mark.parametrize("version", ["1!0.2", "v0.2", "0.2-1", "0.2rc1", "0.2.dev1", "0.2+local", "0.2a1",
                                     "1.2.3.4.5", "0.2.post", "\u0661.2"])
def test_other_versions_are_rejected(tmp_path, version):
    value = {**VALUE, "version": version, "filename": f"pippa_agentapp_sdk-{version}-py3-none-any.whl"}
    rc, out = run(tmp_path, value)
    assert rc == 2 and not out.exists()


def test_version_rule_is_the_shared_one():
    assert sp.VERSION_PATTERN.pattern == r"^\d+(\.\d+){0,3}(\.post\d+)?$"
    assert sp.VERSION_PATTERN.flags & sp.re.ASCII


def test_post_release_is_accepted():
    pin = sp.PinBuilder("pippa-agentapp-sdk").build(
        {**VALUE, "version": "0.1.3.post1", "filename": "pippa_agentapp_sdk-0.1.3.post1-py3-none-any.whl"})
    assert pin.requirement == "pippa-agentapp-sdk==0.1.3.post1"
