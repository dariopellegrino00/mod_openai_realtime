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

The integration image is built from the `integration-test` target in `Dockerfile.ci`. Its FreeSWITCH toolchain is the
same one used to create the CI SDK image, but it additionally contains the FreeSWITCH runtime and test configuration.
The mock server also sends a known PCM16 tone which is recorded from the FreeSWITCH channel and checked for audible
duration and dominant frequency, exercising the playback queue and resampling path end to end.

```sh
./tests/run-integration.sh
```

The first run compiles FreeSWITCH and its dependencies. Later runs reuse Docker's layer cache; only changes to the
module and tests rebuild the final layers. Set `INTEGRATION_IMAGE` to override the local image name.
