#!/bin/sh
set -eu

project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
build_root=${BUILD_ROOT:-/tmp/mod-openai-realtime-build}

BUILD_DIR="${build_root}/unit" "${project_dir}/tests/run-unit.sh" --sanitizers

cmake -S "${project_dir}" -B "${build_root}/module" \
    -DCMAKE_BUILD_TYPE=RelWithDebInfo
cmake --build "${build_root}/module" --parallel
cmake --install "${build_root}/module"
ldconfig

exec "${project_dir}/tests/integration/run.sh"
