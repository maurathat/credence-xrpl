#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SPIKE_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
IMAGE="credence-xrpl-spike:3.4.0"
CONTAINER="credence-xrpl-spike-ledger"
VOLUME="credence-xrpl-spike-data"

# These names are dedicated to the disposable spike. Recreating both proves
# that --start and the checked-in amendment set produce a fresh genesis.
docker rm --force "${CONTAINER}" >/dev/null 2>&1 || true
docker volume rm "${VOLUME}" >/dev/null 2>&1 || true
docker build --platform linux/amd64 --tag "${IMAGE}" "${SPIKE_DIR}/config"
docker volume create "${VOLUME}" >/dev/null
docker run --detach \
  --name "${CONTAINER}" \
  --platform linux/amd64 \
  --publish 127.0.0.1:5005:5005 \
  --mount "type=volume,source=${VOLUME},target=/var/lib/xrpld" \
  "${IMAGE}" >/dev/null

for _attempt in $(seq 1 60); do
  if curl --silent --fail \
    --header 'Content-Type: application/json' \
    --data '{"method":"server_info","params":[{}]}' \
    http://127.0.0.1:5005 >/dev/null; then
    echo "Pinned XRPL spike ledger is ready at http://127.0.0.1:5005"
    exit 0
  fi
  sleep 1
done

docker logs "${CONTAINER}" >&2
echo "Pinned XRPL spike ledger did not become ready" >&2
exit 1
