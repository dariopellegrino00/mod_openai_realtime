# Tests

Run the fast suite with `./tests/run-unit.sh`; run the complete suite with `./tests/run-integration.sh`.
Both entry points are POSIX `sh` scripts and use the same commands locally and in GitHub Actions.

## Responsibilities

| Entry point | Runs where | Responsibility |
| --- | --- | --- |
| `run-unit.sh` | Host or container | Configure the standalone test CMake project, build, and run CTest. |
| `run-integration.sh` | Host | Select the FreeSWITCH base image, add this checkout, and start a fresh container. |
| `run-ci.sh` | Container | Run sanitized unit tests, build the lifecycle probe and sanitized module, install it, and invoke integration. |
| `integration/run.sh` | Container | Start the mock and FreeSWITCH, check readiness, run Python assertions, unload, shut down, and collect diagnostics. |

CMake/CTest owns test discovery and failure reporting for the fast suite. Python `unittest` owns integration
assertions and discovers `integration/test_*.py`; the shell scripts manage processes. `integration/esl.py` handles event-socket framing and
`integration/audio.py` measures recordings. Neither helper needs FreeSWITCH to import.

## Fast suite

Requirements: a C/C++ toolchain, CMake 3.18 or newer, and Python 3.9 or newer. FreeSWITCH is not required.

```sh
./tests/run-unit.sh
./tests/run-unit.sh --sanitizers
```

CTest runs three C++ executables with 18 cases, five Python ESL cases, and two mock event-log cases. The C++ tests
compile the production `stream_protocol.cpp`, `base64.cpp`, and `playback_queue.cpp`; there are no copied
implementations or fake FreeSWITCH headers. Their small `CHECK` harness remains active in Release builds. The ESL
tests use local socket pairs to check fragmentation, timeout recovery, packet deadlines, coalesced packets,
filtering, and connection closure.

Normal and sanitized runs use separate directories, `build/tests` and `build/tests-sanitized`. Set `BUILD_DIR` to
override the directory and `CMAKE_BUILD_PARALLEL_LEVEL` to limit parallel builds. GCC sanitizer runtimes may need
separate system packages. To run one registered test after building:

```sh
ctest --test-dir build/tests --output-on-failure -R '^python$'
```

## FreeSWITCH integration

```sh
TEST_ARTIFACT_DIR=/tmp/mod-openai-test-artifacts ./tests/run-integration.sh
```

The suite loads the real module in the pinned FreeSWITCH runtime and connects it to a local WebSocket mock. Calls
use a `null/` endpoint with an active media source. Tests observe public APIs, custom ESL events, backend messages,
module logs, and recorded PCM16 audio.

| Contract | Evidence |
| --- | --- |
| Playback, resampling, underruns, interruption | Recorded tone frequencies, audible durations, ordered segments, and speech events. |
| Pause versus mute | Pause preserves every tone through completion; mute consumes the held interval and never replays it after unmute. |
| Capture and reconnection | Packet sizes, stereo channel separation, mute silence, and gated reconnects that reject stale audio. |
| Protocol validation and compatibility | Malformed JSON/Base64/UTF-8, odd PCM boundaries, raw transport, opaque reused/missing `response_id`, and preserved valid payloads. |
| API errors | One actionable error line, unchanged success responses, private payloads, and explicit stop/mute side effects. |
| Lifecycle and diagnostics | Restart, terminal close while paused, overlapping stop/hangup, private debug WAVs, header precedence, and log suppression. |

`lifecycle_probe.c` is a test-only shared library preloaded into FreeSWITCH. File barriers hold the API stop just
before removal to verify that capture cannot follow the final payload, including buffered residue. They also let
hangup enter CLOSE under the FreeSWITCH media-bug lock: stop must finish, the channel must disappear, and the
WebSocket must disconnect. The suite also checks unload refusal with an active stream and successful unload/reload
after stop. This exercises the real lock ordering without relying on repeated scheduling races. The probe is built
only with `BUILD_INTEGRATION_TESTS=ON` in the standalone test project; it is never linked into or shipped with the module.

ASan and UBSan cover the module and its compiled IXWebSocket code. FreeSWITCH and SpeexDSP in the base are not fully
sanitized. LeakSanitizer is disabled inside FreeSWITCH; TSan is not run. The suite does not certify SIP/RTP network
behavior, runtime WSS certificate verification, stereo playback codecs, mid-call codec changes, or slow-peer
backpressure. Add those scenarios when changing their contracts.

The host script defaults to the verified GHCR `integration` alias. Authenticate with `docker login ghcr.io` when the
package requires access; otherwise the script uses a cached image or builds the base locally. A cold fallback can
take up to one hour because it compiles FreeSWITCH. To test a changed base explicitly:

```sh
docker build --file Dockerfile.ci --target integration \
  --tag mod-openai-realtime-integration:local .
INTEGRATION_BASE_IMAGE=mod-openai-realtime-integration:local ./tests/run-integration.sh
```

`INTEGRATION_BASE_IMAGE` accepts a local tag or published digest. `TEST_IMAGE` names the checkout runner, defaulting
to `mod-openai-realtime-tests:local`; the previous `INTEGRATION_IMAGE` name remains an alias for this override.
Reuse these tags while iterating. Remove only obsolete images belonging to this project, keeping the main base
and current runner. `run-ci.sh` installs into the container and must not be invoked directly on the host.

## CI and failure diagnostics

Build checks compile Release with TLS enabled and disabled. Static Checks runs clang-format, clang-tidy, cppcheck,
ShellCheck, actionlint, and Ruff. Tests runs the sanitized fast and integration suites. The image-check and
image-publishing workflows run the same suite. Every integration workflow retains logs, mock events, and WAVs as
artifacts for seven days on failure; `TEST_ARTIFACT_DIR` enables the same collection locally.

To match CI locally, you can install the Ruff version pinned in `ruff.toml` in a `.venv`.

SDK and integration environments in the consumer workflows are pinned to immutable digests. Ordinary pull requests
build the module and checkout runner without rebuilding FreeSWITCH. `CI Image Checks` builds and tests base-image
changes before merge. `CI Images` publishes commit tags, tests the published integration digest, and only then
promotes the `sdk` and `integration` aliases. Update the SDK digests in `build.yml` and `code-checks.yml` together with
the integration digest in `tests.yml`, using the same verified publisher run.

Keep referenced image digests and one previous known-good version for rollback. Dependabot checks action SHAs
monthly; the Debian digest in `DEBIAN_IMAGE` needs a manual update validated by `CI Image Checks`. Requiring the
workflow results before merge is a repository ruleset setting, separate from these YAML files.
