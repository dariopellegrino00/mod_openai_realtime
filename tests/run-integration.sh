#!/bin/sh
set -eu

project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
image=${INTEGRATION_IMAGE:-mod-openai-audio-stream-integration:local}

docker build \
    --target integration-test \
    --file "${project_dir}/Dockerfile.ci" \
    --tag "${image}" \
    "${project_dir}"
docker run --rm "${image}"
