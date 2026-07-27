# Tests

The test suite has two layers:

- `unit`: fast tests for production code that has no FreeSWITCH dependency;
- `integration`: tests that build and load the module in a real FreeSWITCH process and connect it to a local mock
  WebSocket server.

The test suite has its own CMake project under `tests/`, so configuring the module itself behaves exactly as before.

## Unit tests

Run all unit tests:

```sh
./tests/run-unit.sh
```

Run them with AddressSanitizer and UndefinedBehaviorSanitizer when the compiler runtimes are installed:

```sh
BUILD_DIR=build/tests-sanitized ./tests/run-unit.sh --sanitizers
```

The unit executables are registered with CTest, so the equivalent manual commands are:

```sh
cmake -S tests -B build/tests
cmake --build build/tests --parallel
ctest --test-dir build/tests --output-on-failure
```

These tests compile the same `stream_protocol.cpp` and `base64.cpp` files linked into the FreeSWITCH module. No copied
implementations or fake FreeSWITCH headers are used.

## Integration tests

The integration image is built from the `integration` target in `Dockerfile.ci`. It extends the CI SDK with the
pinned FreeSWITCH runtime, its test configuration, and Python. The module and tests are not embedded in the image:
they are built from the current checkout and executed every time the container starts.

The mock server sends a known PCM16 tone which is recorded from the FreeSWITCH channel and checked for audible
duration and dominant frequency, exercising the playback queue and resampling path end to end.

```sh
./tests/run-integration.sh
```

By default, the script pulls the integration base image published by this repository. If it is not available yet, it
uses a cached copy or builds the `integration` target locally. `tests/Dockerfile` then adds the current checkout
without embedding it in the base image, and `tests/run-integration.sh` starts a fresh test container.

A cold local fallback compiles FreeSWITCH and can take up to one hour; subsequent unchanged builds reuse Docker's
layer cache. To validate changes to `Dockerfile.ci`, explicitly build and select a local base image:

```sh
docker build --file Dockerfile.ci --target integration \
  --tag mod-openai-realtime-integration:local .
INTEGRATION_BASE_IMAGE=mod-openai-realtime-integration:local ./tests/run-integration.sh
```

`INTEGRATION_BASE_IMAGE` can also select another published tag or digest.
`TEST_IMAGE` controls the local runner image name. The previous `INTEGRATION_IMAGE` override remains supported as an
alias for `TEST_IMAGE`.

`tests/run-ci.sh` is the entry point shared by local Docker runs and GitHub Actions. It runs the sanitized unit suite,
builds and installs the module, and then starts the real FreeSWITCH integration suite.
