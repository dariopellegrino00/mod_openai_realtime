#!/bin/sh
set -eu

project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
build_dir=${BUILD_DIR:-"${project_dir}/build/tests"}

sanitizer_args=
if [ "${1:-}" = "--sanitizers" ]; then
    sanitizer_args="-DENABLE_TEST_SANITIZERS=ON"
elif [ "$#" -ne 0 ]; then
    echo "usage: $0 [--sanitizers]" >&2
    exit 2
fi

cmake -S "${project_dir}/tests" -B "${build_dir}" \
    -DCMAKE_BUILD_TYPE=Debug \
    ${sanitizer_args}
cmake --build "${build_dir}" --parallel
ctest --test-dir "${build_dir}" --output-on-failure
