#!/usr/bin/env bash
# Counts one platform error in CloudWatch (Pippa/Hackathon InfraErrors) with the assumed role.
set -euo pipefail

LOG_SCOPE="hack-publish/alert"

COMMON_DIR="${GITHUB_ACTION_PATH}/../../scripts/common"

# shellcheck source=../../../scripts/common/logger.sh
source "${COMMON_DIR}/logger.sh"
# shellcheck source=../../../scripts/common/error-handler.sh
source "${COMMON_DIR}/error-handler.sh"

METRIC_NAMESPACE="Pippa/Hackathon"
METRIC_NAME="InfraErrors"

if aws cloudwatch put-metric-data \
  --namespace "$METRIC_NAMESPACE" \
  --metric-name "$METRIC_NAME" \
  --value 1 \
  --unit Count; then
  log_info "Counted one ${METRIC_NAMESPACE}/${METRIC_NAME}"
else
  log_warn "Could not record ${METRIC_NAMESPACE}/${METRIC_NAME}"
fi
