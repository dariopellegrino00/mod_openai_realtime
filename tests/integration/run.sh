#!/bin/sh
set -eu

mock_log=/tmp/mod-openai-mock-events.jsonl
mock_ready=/tmp/mod-openai-mock-ready
freeswitch_log=/tmp/mod-openai-freeswitch.log

cleanup() {
    fs_cli -x shutdown >/dev/null 2>&1 || true
    if [ -n "${mock_pid:-}" ]; then
        kill "${mock_pid}" >/dev/null 2>&1 || true
        wait "${mock_pid}" >/dev/null 2>&1 || true
    fi
}
trap cleanup EXIT INT TERM

rm -f "${mock_log}" "${mock_ready}" "${freeswitch_log}"
python3 /work/tests/integration/mock_openai_server.py \
    --event-log "${mock_log}" \
    --ready-file "${mock_ready}" &
mock_pid=$!

attempt=0
while [ ! -e "${mock_ready}" ]; do
    attempt=$((attempt + 1))
    if [ "${attempt}" -ge 100 ]; then
        echo "mock WebSocket server did not start" >&2
        exit 1
    fi
    sleep 0.05
done

freeswitch -nonat -ncwait >"${freeswitch_log}" 2>&1

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

load_result=$(fs_cli -x "load mod_openai_audio_stream")
case "${load_result}" in
    *"+OK"*|*"already loaded"*) ;;
    *)
        echo "could not load mod_openai_audio_stream: ${load_result}" >&2
        exit 1
        ;;
esac

if ! python3 /work/tests/integration/test_module.py; then
    echo "FreeSWITCH log after integration-test failure:" >&2
    tail -200 "${freeswitch_log}" >&2
    for log in /usr/var/log/freeswitch/freeswitch.log /usr/local/freeswitch/log/freeswitch.log; do
        if [ -f "${log}" ]; then
            tail -200 "${log}" >&2
        fi
    done
    exit 1
fi
