"""Unit tests for nothing_to_publish.py: the placeholder app id and a blank app_author_name."""

import importlib.util
import sys
from pathlib import Path

import pytest

sys.dont_write_bytecode = True
MODULE = Path(__file__).resolve().parent.parent / "nothing_to_publish.py"
spec = importlib.util.spec_from_file_location("nothing_to_publish", MODULE)
ntp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ntp)

APP = "app:\n  id: hack.days_until\n  name: Days Until\n  version: 0.1.0\n"
PLACEHOLDER_APP = "app:\n  id: hack.your_app_name_here\n  name: Days Until\n"


def reasons(text: str) -> list[str]:
    return ntp.reasons_for(text)


def test_a_real_app_id_without_the_author_key_publishes():
    assert reasons(APP) == []


def test_placeholder_app_id_holds_the_push_back():
    assert reasons(PLACEHOLDER_APP) == [ntp.PLACEHOLDER_REASON]


def test_template_manifest_with_both_placeholders_gives_both_reasons():
    text = '# Your public author name\napp_author_name: ""\n\n' + PLACEHOLDER_APP
    assert reasons(text) == [ntp.PLACEHOLDER_REASON, ntp.AUTHOR_REASON]


@pytest.mark.parametrize("line", [
    'app_author_name: ""',
    "app_author_name: ''",
    'app_author_name: "   "',
    "app_author_name:",
    "app_author_name:   ",
    "app_author_name: ~",
    "app_author_name: null",
    "app_author_name: NULL",
    'app_author_name: ""   # Claude asks you for it',
    "app_author_name:   # Claude asks you for it",
    '"app_author_name": ""',
    "app_author_name : ''",
])
def test_blank_author_name_holds_the_push_back(line):
    assert reasons(f"{line}\n{APP}") == [ntp.AUTHOR_REASON]


@pytest.mark.parametrize("line", [
    "app_author_name: Tide Watcher",
    'app_author_name: "Tide Watcher"',
    "app_author_name: 'o''hare'",
    "app_author_name: x # a comment",
    "app_author_name: '#hash'",
    "app_author_name: nullish",
])
def test_a_filled_author_name_publishes(line):
    assert reasons(f"{line}\n{APP}") == []


def test_the_author_name_message_is_the_contract_sentence():
    assert ntp.AUTHOR_REASON == (
        "Nothing to publish yet: choose your public author name (app_author_name in manifest.yaml). "
        "Claude will ask you for it."
    )


def test_value_on_the_next_indented_line_is_not_blank():
    assert reasons(f"app_author_name:\n  Tide Watcher\n{APP}") == []


def test_key_followed_only_by_comments_is_blank():
    assert reasons(f"app_author_name:\n  # still empty\n\n{APP}") == [ntp.AUTHOR_REASON]


@pytest.mark.parametrize("block, blank", [("|\n  Tide Watcher\n", False), (">-\n\n", True)])
def test_block_scalars(block, blank):
    found = reasons(f"app_author_name: {block}{APP}")
    assert found == ([ntp.AUTHOR_REASON] if blank else [])


def test_a_nested_author_key_is_not_the_top_level_key():
    assert reasons('app:\n  id: hack.days_until\n  app_author_name: ""\n') == []


def test_the_last_author_key_counts_as_in_yaml():
    assert reasons(f'app_author_name: ""\napp_author_name: Tide Watcher\n{APP}') == []
    assert reasons(f'app_author_name: Tide Watcher\napp_author_name: ""\n{APP}') == [ntp.AUTHOR_REASON]


def test_windows_line_endings(tmp_path):
    manifest = tmp_path / "manifest.yaml"
    manifest.write_bytes(b'app_author_name: ""\r\napp:\r\n  id: hack.your_app_name_here\r\n')
    assert reasons(ntp.read_manifest(manifest)) == [ntp.PLACEHOLDER_REASON, ntp.AUTHOR_REASON]


def test_main_prints_one_paragraph_per_reason(tmp_path, capsys):
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text('app_author_name: ""\n' + PLACEHOLDER_APP)
    assert ntp.main(["--manifest", str(manifest)]) == 0
    assert capsys.readouterr().out == f"{ntp.PLACEHOLDER_REASON}\n\n{ntp.AUTHOR_REASON}\n"


def test_main_prints_nothing_for_a_missing_manifest(tmp_path, capsys):
    assert ntp.main(["--manifest", str(tmp_path / "manifest.yaml")]) == 0
    assert capsys.readouterr().out == ""
