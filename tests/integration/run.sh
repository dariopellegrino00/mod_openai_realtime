#!/bin/sh
set -eu

mock_log=/tmp/mod-openai-mock-events.jsonl
mock_ready=/tmp/mod-openai-mock-ready
freeswitch_log=/tmp/mod-openai-freeswitch.log
project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)

start_freeswitch() {
    if [ "${ENABLE_INTEGRATION_SANITIZERS:-0}" = "1" ]; then
        asan_runtime=$(gcc -print-file-name=libasan.so)
        if [ ! -f "${asan_runtime}" ]; then
            echo "AddressSanitizer runtime not found: ${asan_runtime}" >&2
            return 1
        fi

        LD_PRELOAD="${asan_runtime}" \
            ASAN_OPTIONS="detect_leaks=0:halt_on_error=1" \
            UBSAN_OPTIONS="halt_on_error=1:print_stacktrace=1" \
            freeswitch -nonat -ncwait
    else
        freeswitch -nonat -ncwait
    fi
}

cleanup() {
    fs_cli -x shutdown >/dev/null 2>&1 || true
    if [ -n "${mock_pid:-}" ]; then
        kill "${mock_pid}" >/dev/null 2>&1 || true
        wait "${mock_pid}" >/dev/null 2>&1 || true
    fi
}
trap cleanup EXIT INT TERM

rm -f "${mock_log}" "${mock_ready}" "${freeswitch_log}"
python3 "${project_dir}/tests/integration/mock_openai_server.py" \
    --event-log "${mock_log}" \
    --ready-file "${mock_ready}" &
mock_pid=$!

attempt=0
while [ ! -e "${mock_ready}" ]; do
    if ! kill -0 "${mock_pid}" 2>/dev/null; then
        wait "${mock_pid}" || true
        echo "mock WebSocket server exited before becoming ready" >&2
        exit 1
    fi
    attempt=$((attempt + 1))
    if [ "${attempt}" -ge 100 ]; then
        echo "mock WebSocket server did not start" >&2
        exit 1
    fi
    sleep 0.05
done

if ! start_freeswitch >"${freeswitch_log}" 2>&1; then
    echo "FreeSWITCH failed to start" >&2
    cat "${freeswitch_log}" >&2
    exit 1
fi

attempt=0
until fs_cli -x status >/dev/null 2>&1; do
    attempt=$((attempt + 1))
    if [ "${attempt}" -ge 100 ]; then
        echo "FreeSWITCH did not start" >&2
        cat "${freeswitch_log}" >&2
        exit 1
    fi
    sleep 0.1
done

if ! load_result=$(fs_cli -x "load mod_openai_audio_stream"); then
    echo "fs_cli failed while loading mod_openai_audio_stream: ${load_result}" >&2
    exit 1
fi
case "${load_result}" in
    *"+OK"*|*"already loaded"*) ;;
    *)
        echo "could not load mod_openai_audio_stream: ${load_result}" >&2
        exit 1
        ;;
esac

if ! python3 "${project_dir}/tests/integration/test_module.py"; then
    echo "FreeSWITCH log after integration-test failure:" >&2
    tail -200 "${freeswitch_log}" >&2
    for log in /usr/var/log/freeswitch/freeswitch.log /usr/local/freeswitch/log/freeswitch.log; do
        if [ -f "${log}" ]; then
            tail -200 "${log}" >&2
        fi
    done
    exit 1
fi
