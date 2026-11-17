"""The expression evaluator the workflow tests use behaves like GitHub's on the shapes the workflow uses."""

import pytest

from workflow_expressions import Status, evaluate, runs, truthy


def test_strings_compare_without_regard_to_case():
    assert evaluate("'Pippa-Labs' == 'pippa-labs'", {}) is True
    assert evaluate("github.actor == github.triggering_actor", {"github.actor": "Tide",
                                                                "github.triggering_actor": "tide"}) is True
    assert evaluate("github.actor != github.triggering_actor", {"github.actor": "tide",
                                                                "github.triggering_actor": "org-writer"}) is True


def test_a_missing_context_value_is_null():
    assert evaluate("needs.check.outputs.verdict", {}) is None
    assert evaluate("needs.prepare.outputs.ready != 'false'", {}) is True
    assert evaluate("needs.prepare.outputs.ready == ''", {}) is True, "null and '' are both 0"


def test_and_and_or_return_one_of_their_operands():
    context = {"needs.prepare.result": "success", "needs.check.result": "failure"}
    assert evaluate("needs.prepare.result == 'success' && needs.check.result || 'failure'", context) == "failure"
    assert evaluate("needs.prepare.result == 'failure' && needs.check.result || 'failure'", context) == "failure"
    assert evaluate("needs.prepare.outputs.verdict || 'none'", context) == "none"
    assert evaluate("'a' && 'b'", {}) == "b"


def test_and_binds_tighter_than_or_and_not_tighter_than_both():
    assert evaluate("true || false && false", {}) is True
    assert evaluate("!false && false", {}) is False
    assert evaluate("!(false && false)", {}) is True


def test_different_types_compare_as_numbers():
    assert evaluate("true == 1", {}) is True
    assert evaluate("'' == false", {}) is True
    assert evaluate("'false' == false", {}) is False


def test_not_of_a_boolean_context_value():
    assert evaluate("!github.event.repository.fork", {"github.event.repository.fork": False}) is True
    assert evaluate("!github.event.repository.fork", {"github.event.repository.fork": True}) is False


def test_starts_with_ignores_case():
    assert evaluate("startsWith(github.event.repository.name, 'pippa-hack-')",
                    {"github.event.repository.name": "Pippa-Hack-Tides"}) is True
    assert evaluate("startsWith(github.event.repository.name, 'pippa-hack-')",
                    {"github.event.repository.name": "tides"}) is False


def test_the_dollar_braces_are_optional():
    assert evaluate("${{ !cancelled() && 'x' }}", {}) == "x"


def test_an_if_without_a_status_function_needs_every_needed_job_to_have_succeeded():
    assert runs("true", {}, Status.of("success", "success")) is True
    assert runs("true", {}, Status.of("success", "failure")) is False
    assert runs("true", {}, Status.of("success", "skipped")) is False


def test_an_if_with_a_status_function_decides_alone():
    assert runs("${{ !cancelled() && true }}", {}, Status.of("failure")) is True
    assert runs("${{ !cancelled() && true }}", {}, Status(cancelled=True)) is False
    assert runs("always()", {}, Status.of("failure")) is True


def test_truthiness():
    assert [truthy(value) for value in (None, False, 0.0, "", "0", "false", True, 1.0)] == [
        False, False, False, False, True, True, True, True]


@pytest.mark.parametrize("expression", ["a ==", "unknown()", "'open", "a $ b", "(a"])
def test_anything_it_cannot_read_is_an_error(expression):
    with pytest.raises(ValueError):
        evaluate(expression, {})
