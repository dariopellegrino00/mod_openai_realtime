#!/bin/sh
set -eu

project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
build_root=${BUILD_ROOT:-/tmp/mod-openai-realtime-build}
parallel_jobs=${CMAKE_BUILD_PARALLEL_LEVEL:-$(nproc)}

case "${parallel_jobs}" in
    ''|*[!0-9]*|0)
        echo "CMAKE_BUILD_PARALLEL_LEVEL must be a positive integer" >&2
        exit 2
        ;;
esac
export CMAKE_BUILD_PARALLEL_LEVEL="${parallel_jobs}"

BUILD_DIR="${build_root}/unit-sanitized" "${project_dir}/tests/run-unit.sh" --sanitizers

cmake -S "${project_dir}" -B "${build_root}/module" \
    -DCMAKE_BUILD_TYPE=RelWithDebInfo \
    -DCMAKE_C_COMPILER=gcc \
    -DCMAKE_CXX_COMPILER=g++ \
    -DENABLE_SANITIZERS=ON
cmake --build "${build_root}/module" --parallel "${parallel_jobs}"
cmake --install "${build_root}/module"
ldconfig

module_path=$(pkg-config --variable=modulesdir freeswitch)/mod_openai_audio_stream.so
module_dependencies=$(ldd "${module_path}")
case "${module_dependencies}" in
    *libasan*) ;;
    *)
        echo "sanitized module does not depend on the AddressSanitizer runtime" >&2
        exit 1
        ;;
esac
case "${module_dependencies}" in
    *libubsan*) ;;
    *)
        echo "sanitized module does not depend on the UndefinedBehaviorSanitizer runtime" >&2
        exit 1
        ;;
esac

ENABLE_INTEGRATION_SANITIZERS=1 exec "${project_dir}/tests/integration/run.sh"
