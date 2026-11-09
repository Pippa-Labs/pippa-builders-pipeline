#!/usr/bin/env bash
# Writes the check verdict to the step outputs. Sourced after logger.sh.
#
# Functions:
#   verdict_passed           verdict=passed
#   verdict_failed DETAILS   verdict=failed and the details, cut to VERDICT_DETAILS_LIMIT characters
#   verdict_warnings TEXT    warnings, cut to VERDICT_DETAILS_LIMIT characters

VERDICT_DETAILS_LIMIT=6000

verdict_passed() {
  echo "verdict=passed" >> "$GITHUB_OUTPUT"
}

verdict_multiline() {
  local name="$1" value="$2" delimiter
  delimiter="EOF_$(od -An -N12 -tx1 /dev/urandom | tr -d ' \n')"
  printf '%s<<%s\n%s\n%s\n' "$name" "$delimiter" "${value:0:${VERDICT_DETAILS_LIMIT}}" "$delimiter" \
    >> "$GITHUB_OUTPUT"
}

verdict_failed() {
  echo "verdict=failed" >> "$GITHUB_OUTPUT"
  verdict_multiline details "$1"
}

verdict_warnings() {
  verdict_multiline warnings "$1"
}
