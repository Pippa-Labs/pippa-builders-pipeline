#!/usr/bin/env bash
# Global logger library for pipeline scripts.
#
# Usage:
#   LOG_LEVEL="INFO"   # DEBUG|INFO|WARN|ERROR (default INFO)
#   LOG_SCOPE="my-step" # optional logical scope
#   source ".github/scripts/common/logger.sh"
#   log_info "message"
#
# Notes:
# - This is a sourced library; it does not modify shell options.
# - Logs are printed to stderr with timestamp and scope.
# - In GitHub Actions, WARN/ERROR are also emitted as annotations.

: "${LOG_LEVEL:=INFO}"
: "${LOG_SCOPE:=pipeline}"

LOGGER_LEVEL_DEBUG=10
LOGGER_LEVEL_INFO=20
LOGGER_LEVEL_WARN=30
LOGGER_LEVEL_ERROR=40

logger_to_upper() {
  printf '%s' "$1" | tr '[:lower:]' '[:upper:]'
}

logger_level_value() {
  case "$(logger_to_upper "$1")" in
    DEBUG) echo "$LOGGER_LEVEL_DEBUG" ;;
    INFO) echo "$LOGGER_LEVEL_INFO" ;;
    WARN) echo "$LOGGER_LEVEL_WARN" ;;
    ERROR) echo "$LOGGER_LEVEL_ERROR" ;;
    *) echo "$LOGGER_LEVEL_INFO" ;;
  esac
}

logger_should_log() {
  local message_level current_level
  message_level="$(logger_level_value "$1")"
  current_level="$(logger_level_value "$LOG_LEVEL")"
  [ "$message_level" -ge "$current_level" ]
}

logger_gh_escape() {
  local text
  text="$1"
  text="${text//'%'/'%25'}"
  text="${text//$'\r'/'%0D'}"
  text="${text//$'\n'/'%0A'}"
  printf '%s' "$text"
}

logger_emit_annotation() {
  local level message escaped
  level="$(logger_to_upper "$1")"
  message="$2"

  if [ "${GITHUB_ACTIONS:-}" != "true" ]; then
    return 0
  fi

  escaped="$(logger_gh_escape "$message")"

  case "$level" in
    DEBUG) echo "::debug::${escaped}" ;;
    WARN) echo "::warning::${escaped}" ;;
    ERROR) echo "::error::${escaped}" ;;
    *) ;;
  esac
}

logger_log() {
  local level message normalized timestamp
  level="$1"
  shift
  message="$*"
  normalized="$(logger_to_upper "$level")"

  if ! logger_should_log "$normalized"; then
    return 0
  fi

  timestamp="$(date -u +'%Y-%m-%dT%H:%M:%SZ')"
  printf '%s [%s] [%s] %s\n' "$timestamp" "$normalized" "$LOG_SCOPE" "$message" >&2
  logger_emit_annotation "$normalized" "$message"
}

log_debug() { logger_log DEBUG "$@"; }
log_info() { logger_log INFO "$@"; }
log_warn() { logger_log WARN "$@"; }
log_error() { logger_log ERROR "$@"; }

# Detail variants: same formatting as log_error/log_warn but no GitHub Actions annotation.
# Use for individual finding lines under a gate failure — prevents annotation quota exhaustion
# when there are many findings, and removes duplicate output in GitHub Actions logs.
log_error_detail() {
  if ! logger_should_log "ERROR"; then return 0; fi
  printf '%s [ERROR] [%s] %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$LOG_SCOPE" "$*" >&2
}
log_warn_detail() {
  if ! logger_should_log "WARN"; then return 0; fi
  printf '%s [WARN] [%s] %s\n' "$(date -u +'%Y-%m-%dT%H:%M:%SZ')" "$LOG_SCOPE" "$*" >&2
}
