#!/usr/bin/env bash
# Global error handler library for pipeline scripts.
#
# Usage:
#   source ".github/scripts/common/logger.sh"
#   source ".github/scripts/common/error-handler.sh"
#
# Notes:
# - Must be sourced AFTER logger.sh (uses log_error).
# - Sets ERR and EXIT traps automatically on source.
# - Provides stack trace on unexpected errors.
# - Supports registering cleanup functions via error_register_cleanup.
# - In GitHub Actions, emits ::error:: annotation with context.

if [ "${ERROR_HANDLER_INITIALIZED:-}" = "1" ]; then
  return 0
fi
ERROR_HANDLER_INITIALIZED=1

_ERROR_CLEANUP_FUNCTIONS=()

error_register_cleanup() {
  _ERROR_CLEANUP_FUNCTIONS+=("$1")
}

_error_run_cleanups() {
  local fn
  for fn in "${_ERROR_CLEANUP_FUNCTIONS[@]}"; do
    if declare -f "$fn" > /dev/null 2>&1; then
      "$fn" || true
    fi
  done
}

_error_stack_trace() {
  local frame=1
  log_error "--- Stack trace ---"
  while caller "$frame" > /dev/null 2>&1; do
    local info
    info="$(caller "$frame")"
    local lineno func file
    lineno="$(echo "$info" | awk '{print $1}')"
    func="$(echo "$info" | awk '{print $2}')"
    file="$(echo "$info" | awk '{print $3}')"
    log_error "  at ${func}() in ${file}:${lineno}"
    frame=$((frame + 1))
  done
  log_error "-------------------"
}

_error_trap_handler() {
  local exit_code=$?
  local failed_command="${BASH_COMMAND:-unknown}"
  local lineno="${1:-unknown}"

  log_error "Command failed: '${failed_command}' (exit code ${exit_code}) at line ${lineno}"
  _error_stack_trace

  if [ "${GITHUB_ACTIONS:-}" = "true" ]; then
    local file="${BASH_SOURCE[1]:-unknown}"
    echo "::error file=${file},line=${lineno}::Command failed: '${failed_command}' (exit ${exit_code})"
  fi

  _error_run_cleanups
  exit "$exit_code"
}

_error_exit_handler() {
  local exit_code=$?
  if [ "$exit_code" -ne 0 ]; then
    _error_run_cleanups
  fi
}

trap '_error_trap_handler ${LINENO}' ERR
trap '_error_exit_handler' EXIT
