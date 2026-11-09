#!/usr/bin/env bash
# Installs the Pippa SDK from the SDK-only package repository, at the version and hash
# of the SDK parameter. Uses the credentials of the SDK read role from the previous step;
# the repository token lives only in this script and nothing is written to pip config.
set -euo pipefail

LOG_SCOPE="hack-check/install-sdk"

ACTION_DIR="${GITHUB_ACTION_PATH}"
COMMON_DIR="${ACTION_DIR}/../../scripts/common"
HACK_DIR="${ACTION_DIR}/../../scripts/hack"

# shellcheck source=../../../scripts/common/logger.sh
source "${COMMON_DIR}/logger.sh"
# shellcheck source=../../../scripts/common/error-handler.sh
source "${COMMON_DIR}/error-handler.sh"

ACCOUNT_ID="780886633355"
ENVIRONMENT="prod"
REGION="eu-west-2"
REGION_SHORT="euw2"
COMPONENT="hack-sdk"
DOMAIN="pippa"
REPOSITORY="pippa-${ENVIRONMENT}-codeartifact-${REGION_SHORT}-${COMPONENT}"
PARAMETER="/pippa/${ENVIRONMENT}/hack/sdk"
TOKEN_SECONDS=900
INSTALL_TIMEOUT=120
WORK_DIR="${RUNNER_TEMP}/hack-sdk"

remove_work_dir() {
  rm -rf "$WORK_DIR"
}
error_register_cleanup remove_work_dir

read_pin() {
  aws ssm get-parameter --region "$REGION" --name "$PARAMETER" \
    --query Parameter.Value --output text > "${WORK_DIR}/parameter.json"
  python -I "${HACK_DIR}/sdk_pin.py" \
    "--parameter-file=${WORK_DIR}/parameter.json" \
    "--out=${WORK_DIR}/sdk.txt"
}

repository_endpoint() {
  aws codeartifact get-repository-endpoint --region "$REGION" \
    --domain "$DOMAIN" --domain-owner "$ACCOUNT_ID" --repository "$REPOSITORY" \
    --format pypi --query repositoryEndpoint --output text
}

repository_token() {
  aws codeartifact get-authorization-token --region "$REGION" \
    --domain "$DOMAIN" --domain-owner "$ACCOUNT_ID" \
    --duration-seconds "$TOKEN_SECONDS" --query authorizationToken --output text
}

index_url() {
  local endpoint="${1#https://}" token="$2"
  printf 'https://aws:%s@%s/simple/' "$token" "${endpoint%/}"
}

install_sdk() {
  local url="$1"
  env -u PIP_EXTRA_INDEX_URL -u PIP_INDEX_URL -u PIP_FIND_LINKS -u PIP_TRUSTED_HOST \
    PIP_CONFIG_FILE=/dev/null \
    timeout "$INSTALL_TIMEOUT" python -I -m pip install \
    --disable-pip-version-check --no-input --quiet --no-cache-dir \
    --no-deps --only-binary :all: --require-hashes \
    --index-url "$url" -r "${WORK_DIR}/sdk.txt"
}

main() {
  remove_work_dir
  mkdir -m 700 "$WORK_DIR"
  # Nothing from the app's checkout may be importable or readable as config while credentials exist.
  cd "$WORK_DIR"

  local requirement endpoint token
  requirement="$(read_pin)"
  log_info "Installing ${requirement}"

  endpoint="$(repository_endpoint)"
  token="$(repository_token)"
  if [ -z "$token" ] || [ -z "$endpoint" ]; then
    log_error "The package repository returned no endpoint or no token"
    exit 1
  fi
  echo "::add-mask::${token}"
  install_sdk "$(index_url "$endpoint" "$token")"
  unset token

  cd /
  remove_work_dir
  log_info "${requirement} installed"
}

main "$@"
