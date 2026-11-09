"""Unit tests for check_warnings.py."""

import importlib.util
import sys
from pathlib import Path

sys.dont_write_bytecode = True
MODULE = Path(__file__).resolve().parent.parent / "check_warnings.py"
spec = importlib.util.spec_from_file_location("check_warnings", MODULE)
cw = importlib.util.module_from_spec(spec)
sys.modules["check_warnings"] = cw
spec.loader.exec_module(cw)

CHECK_LOG = """\
Collecting PyYAML
Checking your app before you push

\x1b[33m WARN\x1b[0m owner_email is still the template's placeholder (you@example.com)
       Ask the person for the address they sign in to Pippa with.

  OK  app.py is present
....                                                                     [100%]
       this indented pytest line is not a fix

 WARN owner_email is still the template's placeholder (you@example.com)

Ready to push.
"""

ROUTING_LOG = """\
Will Pippa choose your app?

  OK  summary leads with something specific (54 chars seen)
       chooser sees: 'Works out how many days until a date'
 FAIL days.until: "how long" reduces to one ordinary word
       Carry the subject in the phrase.
 WARN days.until shares "date" with dates.between
TRY THESE ONCE IT IS LIVE
    - when is christmas

1 problem(s) that will stop your app being chosen.
1 warning(s) as well.
"""


def render(tmp_path, check=CHECK_LOG, routing=ROUTING_LOG, status=1):
    check_log = tmp_path / "check.log"
    routing_log = tmp_path / "routing.log"
    check_log.write_text(check)
    routing_log.write_text(routing)
    args = cw.parse_args([f"--check-log={check_log}", f"--routing-log={routing_log}",
                          f"--routing-status={status}"])
    return cw.report_for(args).render()


def test_summary_of_check_and_routing(tmp_path):
    assert render(tmp_path) == (
        "scripts/check.py\n"
        " WARN owner_email is still the template's placeholder (you@example.com)\n"
        "       Ask the person for the address they sign in to Pippa with.\n"
        "\n"
        "scripts/routing_check.py\n"
        ' FAIL days.until: "how long" reduces to one ordinary word\n'
        "       Carry the subject in the phrase.\n"
        ' WARN days.until shares "date" with dates.between\n'
        "1 warning(s) as well."
    )


def test_nothing_to_say_is_empty(tmp_path):
    routing = "  OK  fine\n\nNothing here will stop your app being chosen.\n"
    assert render(tmp_path, check="Ready to push.\n", routing=routing, status=0) == ""


def test_routing_warnings_only(tmp_path):
    routing = " WARN a phrase is long\n1 warning(s). Worth a look, none fatal.\n"
    text = render(tmp_path, check="", routing=routing, status=0)
    assert text == "scripts/routing_check.py\n WARN a phrase is long\n1 warning(s). Worth a look, none fatal."


def test_routing_crash_is_one_warning(tmp_path):
    routing = "Will Pippa choose your app?\nTraceback (most recent call last):\n  File \"x\"\nNameError: name 'y' is not defined\n"
    text = render(tmp_path, check="", routing=routing, status=1)
    assert text == (
        "scripts/routing_check.py\n"
        " WARN the routing check could not run (exit 1); routing was not checked.\n"
        "       NameError: name 'y' is not defined"
    )


def test_routing_timeout_is_one_warning(tmp_path):
    text = render(tmp_path, check="", routing="Will Pippa choose your app?\n", status=124)
    assert text == "scripts/routing_check.py\n WARN the routing check did not finish in time; routing was not checked."


def test_unexpected_exit_status_is_a_crash(tmp_path):
    text = render(tmp_path, check="", routing="Nothing here will stop your app being chosen.\n", status=2)
    assert "could not run (exit 2)" in text


def test_missing_logs_are_empty(tmp_path):
    args = cw.parse_args([f"--check-log={tmp_path / 'none'}", f"--routing-log={tmp_path / 'none2'}",
                          "--routing-status=2"])
    assert "could not run (exit 2)" in cw.report_for(args).render()


def test_summary_is_cut(tmp_path):
    check = "".join(f" WARN warning number {i} {'x' * 80}\n" for i in range(200))
    routing = "Nothing here will stop your app being chosen.\n"
    text = render(tmp_path, check=check, routing=routing, status=0)
    assert len(text) <= cw.SUMMARY_LIMIT and text.endswith("\n...")


def test_main_prints_nothing_when_clean(tmp_path, capsys):
    (tmp_path / "r.log").write_text("Nothing here will stop your app being chosen.\n")
    assert cw.main([f"--routing-log={tmp_path / 'r.log'}", "--routing-status=0"]) == 0
    assert capsys.readouterr().out == ""
