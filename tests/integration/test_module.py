#!/usr/bin/env python3
import base64
import json
import math
import os
import socket
import subprocess
import sys
import time
import unittest
import wave
from array import array
from pathlib import Path


MOCK_URL = "ws://127.0.0.1:18080"
EVENT_LOG = Path("/tmp/mod-openai-mock-events.jsonl")
PLAYBACK_DRAIN_MARGIN_SECONDS = 0.4
PLAYBACK_DURATION_TOLERANCE_SECONDS = 0.1
FREESWITCH_LOG = Path(os.environ.get("FREESWITCH_RUNTIME_LOG", "/tmp/mod-openai-freeswitch.log"))
ARTIFACT_DIR = Path(os.environ["TEST_ARTIFACT_DIR"]) if os.environ.get("TEST_ARTIFACT_DIR") else None
PCM16_BYTES_PER_SAMPLE = 2
AUDIBLE_SAMPLE_THRESHOLD = 500
MODULE_LOG_SOURCES = ("mod_openai_audio_stream.c:", "openai_audio_streamer_glue.cpp:")
MEDIA_BUG_NAME = "audio_stream"  # Keep in sync with MY_BUG_NAME in mod_openai_audio_stream.h.
SPEECH_START_EVENT = "mod_openai_audio_stream::openai_speech_start"
SPEECH_STOP_EVENT = "mod_openai_audio_stream::openai_speech_stop"


class FreeSwitchEventSocket:
    """Minimal inbound ESL client used to observe the module's public custom events."""

    def __init__(self, unique_id):
        self._unique_id = unique_id
        self.seen_events = []
        self._socket = socket.create_connection(("127.0.0.1", 8021), timeout=5)
        self._buffer = bytearray()
        try:
            headers, _ = self._receive_packet(5)
            if headers.get("content-type") != "auth/request":
                raise RuntimeError(f"unexpected FreeSWITCH event socket greeting: {headers}")
            self._command("auth ClueCon")
            self._command(f"event json CUSTOM {SPEECH_START_EVENT} {SPEECH_STOP_EVENT}")
        except Exception:
            self.close()
            raise

    def close(self):
        self._socket.close()

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc_value, _traceback):
        self.close()

    def _header_boundary(self):
        boundaries = []
        for marker in (b"\r\n\r\n", b"\n\n"):
            index = self._buffer.find(marker)
            if index >= 0:
                boundaries.append((index, len(marker)))
        return min(boundaries) if boundaries else None

    def _receive_packet(self, timeout):
        self._socket.settimeout(timeout)
        boundary = self._header_boundary()
        while boundary is None:
            chunk = self._socket.recv(4096)
            if not chunk:
                raise RuntimeError("FreeSWITCH event socket closed unexpectedly")
            self._buffer.extend(chunk)
            boundary = self._header_boundary()

        header_end, marker_length = boundary
        raw_headers = bytes(self._buffer[:header_end]).decode("utf-8", errors="replace")
        del self._buffer[: header_end + marker_length]
        headers = {}
        for line in raw_headers.replace("\r\n", "\n").split("\n"):
            if ":" in line:
                name, value = line.split(":", 1)
                headers[name.strip().lower()] = value.strip()

        content_length = int(headers.get("content-length", "0"))
        while len(self._buffer) < content_length:
            chunk = self._socket.recv(4096)
            if not chunk:
                raise RuntimeError("FreeSWITCH event socket closed in a packet body")
            self._buffer.extend(chunk)
        body = bytes(self._buffer[:content_length])
        del self._buffer[:content_length]
        return headers, body

    def _command(self, command):
        self._socket.sendall(f"{command}\n\n".encode())
        headers, _ = self._receive_packet(5)
        if headers.get("content-type") != "command/reply" or not headers.get("reply-text", "").startswith("+OK"):
            raise RuntimeError(f"FreeSWITCH event socket command failed: {command}: {headers}")

    def wait_for(self, event_subclass, timeout=5):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            try:
                headers, body = self._receive_packet(remaining)
            except socket.timeout:
                return None
            if headers.get("content-type") != "text/event-json":
                continue
            event = json.loads(body)
            self.seen_events.append((event.get("Unique-ID"), event.get("Event-Subclass")))
            if event.get("Unique-ID") == self._unique_id and event.get("Event-Subclass") == event_subclass:
                return event
        return None


def api(command, timeout=10):
    result = subprocess.run(
        ["fs_cli", "-x", command],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError(f"fs_cli failed for {command!r}: {result.stderr.strip()}")
    return result.stdout.strip()


def assert_ok(test, command):
    result = api(command)
    test.assertTrue(result.startswith("+OK"), f"command failed: {command}\n{result}")
    return result


def assert_error(test, command):
    result = api(command)
    lines = result.splitlines()
    test.assertTrue(lines and lines[-1].startswith("-ERR"), f"command unexpectedly succeeded: {command}\n{result}")
    return result


def mock_events():
    if not EVENT_LOG.exists():
        return []
    events = []
    for line in EVENT_LOG.read_text(encoding="utf-8").splitlines():
        if line:
            events.append(json.loads(line))
    return events


def wait_for_event(predicate, start_index=0, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        events = mock_events()
        for event in events[start_index:]:
            if predicate(event):
                return event
        time.sleep(0.05)
    return None


def wait_until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


def encode_json(payload):
    compact = json.dumps(payload, separators=(",", ":")).encode()
    return base64.b64encode(compact).decode()


def read_mono_pcm16(path):
    with wave.open(str(path), "rb") as recording:
        channels = recording.getnchannels()
        sample_width = recording.getsampwidth()
        sample_rate = recording.getframerate()
        frames = recording.readframes(recording.getnframes())

    if sample_width != PCM16_BYTES_PER_SAMPLE:
        raise AssertionError(f"expected PCM16 recording, got {sample_width * 8}-bit samples")

    samples = array("h")
    samples.frombytes(frames)
    if sys.byteorder == "big":
        samples.byteswap()
    if channels > 1:
        samples = array(
            "h",
            (
                sum(samples[index : index + channels]) // channels
                for index in range(0, len(samples), channels)
            ),
        )
    return sample_rate, samples


def goertzel_power(samples, sample_rate, frequency):
    coefficient = 2 * math.cos(2 * math.pi * frequency / sample_rate)
    previous = 0.0
    previous_previous = 0.0
    for sample in samples:
        current = sample + coefficient * previous - previous_previous
        previous_previous = previous
        previous = current
    return previous_previous**2 + previous**2 - coefficient * previous * previous_previous


def dominant_frequency(samples, sample_rate):
    frequencies = range(300, min(3000, sample_rate // 2), 25)
    return max(frequencies, key=lambda frequency: goertzel_power(samples, sample_rate, frequency))


def audible_duration(samples, sample_rate):
    window_size = max(sample_rate // 50, 1)
    active_samples = 0
    for offset in range(0, len(samples) - window_size + 1, window_size):
        window = samples[offset : offset + window_size]
        mean_square = sum(sample * sample for sample in window) / len(window)
        if mean_square >= AUDIBLE_SAMPLE_THRESHOLD**2:
            active_samples += len(window)
    return active_samples / sample_rate


def wait_for_realtime_playback(duration_seconds):
    # The mock queues audio immediately, but FreeSWITCH consumes it at media rate.
    time.sleep(duration_seconds + PLAYBACK_DRAIN_MARGIN_SECONDS)


def active_runs(samples, sample_rate, window_ms=5, threshold=500):
    window_size = max(sample_rate * window_ms // 1000, 1)
    runs = 0
    active = False
    active_windows = 0
    for offset in range(0, len(samples) - window_size + 1, window_size):
        window = samples[offset : offset + window_size]
        mean_square = sum(sample * sample for sample in window) / len(window)
        window_active = mean_square >= threshold**2
        if window_active:
            active_windows += 1
            if not active:
                runs += 1
        active = window_active
    return runs, active_windows * window_size / sample_rate


def longest_silent_gap(samples, sample_rate, window_ms=20, threshold=500):
    window_size = max(sample_rate * window_ms // 1000, 1)
    activity = []
    for offset in range(0, len(samples) - window_size + 1, window_size):
        window = samples[offset : offset + window_size]
        mean_square = sum(sample * sample for sample in window) / len(window)
        activity.append(mean_square >= threshold**2)

    try:
        first_active = activity.index(True)
        last_active = len(activity) - 1 - activity[::-1].index(True)
    except ValueError:
        return 0.0

    longest = 0
    current = 0
    for active in activity[first_active : last_active + 1]:
        if active:
            longest = max(longest, current)
            current = 0
        else:
            current += 1
    return max(longest, current) * window_size / sample_rate


def tone_durations(samples, sample_rate, frequencies, window_ms=20, threshold=AUDIBLE_SAMPLE_THRESHOLD):
    window_size = max(sample_rate * window_ms // 1000, 1)
    windows = {frequency: 0 for frequency in frequencies}
    for offset in range(0, len(samples) - window_size + 1, window_size):
        window = samples[offset : offset + window_size]
        mean_square = sum(sample * sample for sample in window) / len(window)
        if mean_square < threshold**2:
            continue
        winner = max(frequencies, key=lambda frequency: goertzel_power(window, sample_rate, frequency))
        windows[winner] += 1
    return {frequency: count * window_size / sample_rate for frequency, count in windows.items()}


class ModuleIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.uuid = None
        self.recording = None
        self.event_start = len(mock_events())
        self.freeswitch_log_start = FREESWITCH_LOG.stat().st_size
        self.expected_module_error_fragments = []
        self.addCleanup(self.cleanup_resources)
        self.originate_call()
        assert_ok(self, f"uuid_setvar {self.uuid} STREAM_OPENAI_API_KEY integration-test-key")
        assert_ok(self, f"uuid_setvar {self.uuid} STREAM_DISABLE_AUDIOFILES true")
        assert_ok(self, f"uuid_setvar {self.uuid} STREAM_NO_RECONNECT true")

    def tearDown(self):
        self.cleanup_resources()
        self.assertTrue(api("status").startswith("UP"), "FreeSWITCH stopped during the test")
        events = mock_events()[self.event_start :]
        invalid_events = [
            event for event in events if event.get("event") in {"invalid-json", "invalid-audio-message"}
        ]
        self.assertEqual(invalid_events, [], "module sent malformed data to the mock WebSocket server")
        misaligned_audio = [
            event
            for event in events
            if event.get("event") in {"audio-received", "binary"} and event.get("sample_aligned") is not True
        ]
        self.assertEqual(misaligned_audio, [], "module sent a partial PCM16 sample")
        self.assert_expected_module_errors()

    def cleanup_resources(self):
        try:
            if self.uuid:
                uuid = self.uuid
                self.uuid = None
                api(f"uuid_kill {uuid}")
        finally:
            if self.recording and ARTIFACT_DIR is None:
                self.recording.unlink(missing_ok=True)

    def originate_call(self):
        result = api("originate null/+15555550100 &park()")
        self.assertTrue(result.startswith("+OK "), f"could not originate test channel: {result}")
        self.uuid = result.removeprefix("+OK ").strip()
        assert_ok(self, f"uuid_broadcast {self.uuid} silence_stream://-1 aleg")

    def start_read_tone(self, frequency=700):
        source = f"tone_stream://%(10000,0,{frequency});loops=-1"
        assert_ok(self, f"uuid_displace {self.uuid} start {source} 0 rm")
        return source

    def module_error_lines(self):
        with FREESWITCH_LOG.open("rb") as log:
            log.seek(self.freeswitch_log_start)
            lines = log.read().decode("utf-8", errors="replace").splitlines()
        return [
            line
            for line in lines
            if "[ERR]" in line and any(source in line for source in MODULE_LOG_SOURCES)
        ]

    def assert_expected_module_errors(self):
        unmatched = self.module_error_lines()
        for fragment in self.expected_module_error_fragments:
            match = next((index for index, line in enumerate(unmatched) if fragment in line), None)
            if match is None:
                self.fail(f"module did not log expected error {fragment!r}; observed {unmatched}")
            unmatched.pop(match)
        self.assertEqual(unmatched, [], "module logged unexpected errors:\n" + "\n".join(unmatched))

    def expect_module_errors(self, *fragments):
        self.expected_module_error_fragments.extend(fragments)

    def start_stream(
        self,
        url=MOCK_URL,
        start_muted=True,
        stream_api="uuid_openai_audio_stream",
        send_rate="24k",
        playback_rate=None,
        mix_type="mono",
    ):
        before = len(mock_events())
        command = f"{stream_api} {self.uuid} start {url} {mix_type} {send_rate}"
        if playback_rate is not None:
            command += f" {playback_rate}"
        if start_muted:
            command += " mute_user"
        assert_ok(self, command)
        connected = wait_for_event(lambda event: event.get("event") == "connected", before)
        self.assertIsNotNone(connected, "module did not connect to the mock WebSocket server")
        return connected

    def start_recording(self, scenario):
        recording_dir = ARTIFACT_DIR or Path("/tmp")
        recording_dir.mkdir(parents=True, exist_ok=True)
        self.recording = recording_dir / f"mod-openai-{scenario}-{self.uuid}.wav"
        self.recording.unlink(missing_ok=True)
        assert_ok(self, f"uuid_record {self.uuid} start {self.recording}")

    def stop_recording(self):
        assert_ok(self, f"uuid_record {self.uuid} stop {self.recording}")
        self.assertTrue(self.recording.exists(), "FreeSWITCH did not create the playback recording")
        return read_mono_pcm16(self.recording)

    def stop_stream(self, final_payload=None, stream_api="uuid_openai_audio_stream"):
        before = len(mock_events())
        command = f"{stream_api} {self.uuid} stop"
        if final_payload is not None:
            command += f" {encode_json(final_payload)}"
        assert_ok(self, command)

        if final_payload is not None:
            delivered = wait_for_event(
                lambda event: event.get("event") == "message" and event.get("payload") == final_payload,
                before,
            )
            self.assertIsNotNone(delivered, "final stop payload was not delivered before disconnect")

        disconnected = wait_for_event(lambda event: event.get("event") == "disconnected", before)
        self.assertIsNotNone(disconnected, "WebSocket did not disconnect after stop")

    def restart_after_automatic_cleanup(self, timeout=5):
        deadline = time.monotonic() + timeout
        last_bug_list = "no media-bug query completed"
        while time.monotonic() < deadline:
            last_bug_list = api(f"uuid_buglist {self.uuid}")
            self.assertFalse(last_bug_list.startswith("-ERR"), f"media-bug query failed: {last_bug_list}")
            if MEDIA_BUG_NAME not in last_bug_list:
                self.start_stream()
                return
            time.sleep(0.05)
        self.fail(f"stream context was not cleaned up after peer disconnect: {last_bug_list}")

    def trigger_response(
        self,
        expected_event,
        payload=None,
        stream_api="uuid_openai_audio_stream",
        timeout=5,
    ):
        before = len(mock_events())
        request = encode_json(payload if payload is not None else {"type": "response.create"})
        assert_ok(self, f"{stream_api} {self.uuid} send_json {request}")
        event = wait_for_event(lambda item: item.get("event") == expected_event, before, timeout=timeout)
        self.assertIsNotNone(event, f"mock did not emit {expected_event}")
        return event

    def assert_recording_contains_tone(self, expected_frequency, expected_duration_seconds):
        self.assertTrue(
            self.recording.exists(),
            "FreeSWITCH did not create the playback recording",
        )
        sample_rate, samples = read_mono_pcm16(self.recording)
        duration = audible_duration(samples, sample_rate)
        self.assertGreater(
            duration,
            expected_duration_seconds - PLAYBACK_DURATION_TOLERANCE_SECONDS,
            "recording contains less audible audio than the mock sent",
        )
        self.assertLess(
            duration,
            expected_duration_seconds + PLAYBACK_DURATION_TOLERANCE_SECONDS,
            "recording contains more audible audio than the mock sent",
        )
        frequency = dominant_frequency(samples, sample_rate)
        self.assertAlmostEqual(frequency, expected_frequency, delta=75)

    def test_final_stop_payload(self):
        self.start_stream()
        self.stop_stream({"type": "integration.final", "marker": "stop-payload"})

    def assert_playback_control_creates_silence(self, command, inverse_command, scenario):
        with FreeSwitchEventSocket(self.uuid) as event_socket:
            self.start_stream(f"{MOCK_URL}/flow-control")
            self.start_recording(scenario)
            sent = self.trigger_response("flow-control-response-sent")
            started = event_socket.wait_for(SPEECH_START_EVENT)
            self.assertIsNotNone(
                started,
                f"module did not emit the playback-start event; observed {event_socket.seen_events}",
            )

            assert_ok(self, f"uuid_openai_audio_stream {self.uuid} {command}")
            time.sleep(0.3)
            assert_ok(self, f"uuid_openai_audio_stream {self.uuid} {inverse_command}")
            time.sleep(0.35)
            sample_rate, samples = self.stop_recording()

        runs, _ = active_runs(samples, sample_rate, window_ms=20)
        self.assertGreaterEqual(runs, 2, f"{command} did not interrupt audible playback")
        self.assertGreater(
            longest_silent_gap(samples, sample_rate),
            0.15,
            f"{command} did not create the expected silent playback interval",
        )
        self.assertAlmostEqual(dominant_frequency(samples, sample_rate), sent["frequency"], delta=75)
        self.stop_stream()

    def test_pause_and_resume_control_playback(self):
        self.assert_playback_control_creates_silence("pause", "resume", "pause-resume")

    def test_openai_mute_controls_playback(self):
        self.assert_playback_control_creates_silence("mute openai", "unmute openai", "openai-mute")

    def test_playback_emits_speech_lifecycle_events(self):
        with FreeSwitchEventSocket(self.uuid) as event_socket:
            self.start_stream()
            self.trigger_response("audio-response-sent")
            started = event_socket.wait_for(SPEECH_START_EVENT)
            stopped = event_socket.wait_for(SPEECH_STOP_EVENT)
            self.assertIsNotNone(
                started,
                f"module did not emit the playback-start event; observed {event_socket.seen_events}",
            )
            self.assertIsNotNone(
                stopped,
                f"module did not emit the playback-stop event after AudioDone; observed {event_socket.seen_events}",
            )
        self.stop_stream()

    def test_invalid_audio_delta_does_not_reopen_completed_playback(self):
        self.expect_module_errors("response.output_audio.delta no audio data")
        with FreeSwitchEventSocket(self.uuid) as event_socket:
            self.start_stream(f"{MOCK_URL}/invalid-delta-after-done")
            sent = self.trigger_response("invalid-delta-after-done-sent")

            started = event_socket.wait_for(SPEECH_START_EVENT)
            stopped = event_socket.wait_for(SPEECH_STOP_EVENT, timeout=sent["duration_seconds"] + 3)
            self.assertIsNotNone(
                started,
                f"module did not emit playback-start; observed {event_socket.seen_events}",
            )
            self.assertIsNotNone(
                stopped,
                f"invalid audio delta suppressed playback-stop; observed {event_socket.seen_events}",
            )

        self.stop_stream()

    def test_caller_audio_reaches_websocket(self):
        before = len(mock_events())
        self.start_stream(start_muted=False)
        audio = wait_for_event(lambda event: event.get("event") == "audio-received", before)
        self.assertIsNotNone(audio, "module did not send caller audio to the WebSocket")
        self.assertGreater(audio["size"], 0, "module sent an empty audio payload")
        self.assertTrue(audio["sample_aligned"], "module sent a partial PCM16 sample")
        self.stop_stream()

    def test_capture_buffering_and_user_mute(self):
        capture_rate = 24000
        buffer_ms = 100
        aggregated_size = capture_rate * buffer_ms // 1000 * PCM16_BYTES_PER_SAMPLE
        mute_silence_size = capture_rate * PCM16_BYTES_PER_SAMPLE
        self.start_read_tone()
        assert_ok(self, f"uuid_setvar {self.uuid} STREAM_BUFFER_SIZE {buffer_ms}")

        before = len(mock_events())
        self.start_stream(start_muted=False)
        aggregated = wait_for_event(
            lambda event: event.get("event") == "audio-received" and event.get("size") == aggregated_size,
            before,
        )
        self.assertIsNotNone(aggregated, "capture audio was not aggregated to the configured duration")
        self.assertGreaterEqual(
            aggregated["peak_amplitude"],
            AUDIBLE_SAMPLE_THRESHOLD,
            "audible caller audio was not captured",
        )

        before = len(mock_events())
        assert_ok(self, f"uuid_openai_audio_stream {self.uuid} mute user")
        mute_silence = wait_for_event(
            lambda event: event.get("event") == "audio-received" and event.get("size") == mute_silence_size,
            before,
        )
        self.assertIsNotNone(mute_silence, "muting did not send one second of silence")
        self.assertTrue(mute_silence["all_zero"], "muting sent non-silent PCM data")

        muted_since = len(mock_events())
        time.sleep(0.25)
        audio_while_muted = [
            event for event in mock_events()[muted_since:] if event.get("event") == "audio-received"
        ]
        self.assertEqual(audio_while_muted, [], "caller audio continued while user mute was active")

        before = len(mock_events())
        assert_ok(self, f"uuid_openai_audio_stream {self.uuid} unmute user")
        resumed = wait_for_event(
            lambda event: event.get("event") == "audio-received" and event.get("size") == aggregated_size,
            before,
        )
        self.assertIsNotNone(resumed, "capture audio did not resume after unmute")
        self.assertGreaterEqual(
            resumed["peak_amplitude"],
            AUDIBLE_SAMPLE_THRESHOLD,
            "audible caller audio did not resume after unmute",
        )
        self.stop_stream()

    def test_stereo_capture_preserves_channel_separation(self):
        self.start_read_tone()
        path = "/stereo-capture"
        before = len(mock_events())
        self.start_stream(f"{MOCK_URL}{path}", start_muted=False, mix_type="stereo")
        captured = wait_for_event(
            lambda event: event.get("event") == "audio-received"
            and event.get("path") == path
            and len(event.get("channel_peak_amplitudes", [])) == 2,
            before,
        )
        self.assertIsNotNone(captured, "module did not send interleaved stereo capture")
        caller_peak, callee_peak = captured["channel_peak_amplitudes"]
        self.assertGreaterEqual(
            caller_peak,
            AUDIBLE_SAMPLE_THRESHOLD,
            "caller channel did not contain the test tone",
        )
        self.assertLess(callee_peak, AUDIBLE_SAMPLE_THRESHOLD, "caller tone leaked into the callee channel")
        self.stop_stream()

    def test_invalid_capture_buffer_size_uses_default(self):
        default_frame_bytes = 24000 * 20 // 1000 * PCM16_BYTES_PER_SAMPLE
        assert_ok(self, f"uuid_setvar {self.uuid} STREAM_BUFFER_SIZE 21")

        before = len(mock_events())
        self.start_stream(start_muted=False)
        audio = wait_for_event(
            lambda event: event.get("event") == "audio-received" and event.get("size") == default_frame_bytes,
            before,
        )
        self.assertIsNotNone(audio, "invalid capture buffer size did not fall back to 20 ms")
        self.stop_stream()

    def test_extra_headers_merge_with_authorization(self):
        extra_headers = json.dumps(
            {
                "Authorization": "Bearer must-be-replaced",
                "X-Integration-Test": "preserved",
            },
            separators=(",", ":"),
        )
        assert_ok(self, f"uuid_setvar {self.uuid} STREAM_EXTRA_HEADERS {extra_headers}")

        connected = self.start_stream()
        self.assertEqual(connected["authorization_headers"], ["Bearer integration-test-key"])
        self.assertEqual(connected["integration_headers"], ["preserved"])
        self.stop_stream()

    def test_playback_audio_reaches_channel(self):
        self.start_stream()
        self.start_recording("playback")

        sent = self.trigger_response("audio-response-sent")
        wait_for_realtime_playback(sent["duration_seconds"])
        self.stop_recording()

        self.assert_recording_contains_tone(sent["frequency"], sent["duration_seconds"])
        self.stop_stream()

    def test_reused_response_id_does_not_suppress_new_playback(self):
        self.start_stream()
        self.start_recording("response-id")

        sent = self.trigger_response(
            "reused-response-id-audio-sent",
            {"type": "integration.reused_response_id"},
        )
        wait_for_realtime_playback(sent["duration_seconds"])
        self.stop_recording()

        self.assert_recording_contains_tone(sent["frequency"], sent["duration_seconds"])
        self.stop_stream()

    def test_playback_recovers_from_repeated_underruns(self):
        self.start_stream(f"{MOCK_URL}/underrun")
        self.start_recording("underrun")

        sent = self.trigger_response("underrun-response-sent", timeout=10)
        time.sleep(0.2)
        sample_rate, samples = self.stop_recording()
        recording_duration = len(samples) / sample_rate
        self.assertGreater(
            recording_duration,
            sent["elapsed"] + 0.1,
            "short replacement frames compressed the recorded playback timeline",
        )

        # Use analysis windows shorter than each 5 ms burst: otherwise a burst crossing a
        # window boundary can be counted as 10 ms and make the upper bound timing-dependent.
        runs, active_time = active_runs(samples, sample_rate, window_ms=1)
        expected_active_time = sent["burst_count"] * sent["burst_duration"]
        self.assertGreaterEqual(runs, sent["burst_count"] - 4, "playback did not recover after repeated underruns")
        self.assertGreater(active_time, expected_active_time * 0.6)
        self.assertLess(active_time, expected_active_time * 1.5, "playback repeated or stretched underrun audio")
        self.assertAlmostEqual(dominant_frequency(samples, sample_rate), sent["frequency"], delta=75)
        self.stop_stream()

    def test_barge_in_discards_buffered_audio(self):
        self.start_stream(f"{MOCK_URL}/barge-in")
        self.start_recording("barge-in")

        sent = self.trigger_response("barge-in-response-sent")
        time.sleep(0.9)
        sample_rate, samples = self.stop_recording()
        durations = tone_durations(
            samples,
            sample_rate,
            (sent["interrupted_frequency"], sent["replacement_frequency"]),
        )
        self.assertLess(
            durations[sent["interrupted_frequency"]],
            0.5,
            "audio queued before barge-in was still played",
        )
        self.assertGreater(
            durations[sent["replacement_frequency"]],
            0.4,
            "replacement audio following barge-in did not reach the channel",
        )
        self.stop_stream()

    def test_debug_audio_file_is_private_and_removed_on_stop(self):
        assert_ok(self, f"uuid_setvar {self.uuid} STREAM_DISABLE_AUDIOFILES false")
        self.start_stream(f"{MOCK_URL}/debug-audio")

        temp_dir = Path(api("global_getvar temp_dir"))
        debug_file = temp_dir / f"{self.uuid}_0.tmp.wav"
        debug_file.unlink(missing_ok=True)
        sent = self.trigger_response("debug-audio-response-sent")

        def debug_wav_is_complete():
            try:
                with wave.open(str(debug_file), "rb") as recording:
                    return len(recording.readframes(recording.getnframes())) == sent["byte_count"]
            except (EOFError, FileNotFoundError, wave.Error):
                return False

        self.assertTrue(wait_until(debug_wav_is_complete), "module did not finish writing the debug WAV file")

        self.assertEqual(debug_file.stat().st_mode & 0o777, 0o600, "debug audio is not owner-only")
        with wave.open(str(debug_file), "rb") as recording:
            self.assertEqual(recording.getnchannels(), 1)
            self.assertEqual(recording.getsampwidth(), 2)
            self.assertEqual(recording.getframerate(), sent["sample_rate"])
            self.assertEqual(recording.getnframes() * recording.getsampwidth(), sent["byte_count"])

        self.stop_stream()
        self.assertTrue(wait_until(lambda: not debug_file.exists()), "debug WAV survived stream teardown")

    def test_raw_audio_streams_binary_pcm_in_both_directions(self):
        stream_api = "uuid_raw_audio_stream"
        before = len(mock_events())
        # setUp keeps debug WAVs disabled because the mock deliberately sends one frame per byte.
        # Requesting the mock's 8 kHz rate isolates PCM16 stitching; JSON playback covers resampling.
        self.start_stream(
            f"{MOCK_URL}/raw-audio",
            start_muted=False,
            stream_api=stream_api,
            playback_rate="8k",
        )
        caller_audio = wait_for_event(lambda event: event.get("event") == "binary", before)
        self.assertIsNotNone(caller_audio, "raw mode did not send caller audio as a binary WebSocket frame")
        self.assertGreater(caller_audio["size"], 0)
        self.assertTrue(caller_audio["sample_aligned"], "raw caller audio ended with a partial PCM16 sample")

        self.start_recording("raw-audio")
        sent = self.trigger_response("raw-audio-response-sent", stream_api=stream_api)
        self.assertGreater(sent["split_frame_count"], 1, "mock did not split the binary PCM stream")
        time.sleep(1)

        sample_rate, samples = self.stop_recording()
        durations = tone_durations(
            samples,
            sample_rate,
            (sent["split_frequency"], sent["regular_frequency"]),
        )
        self.assertGreater(
            durations[sent["split_frequency"]],
            sent["split_duration"] / 2,
            "split PCM16 samples were not reconstructed",
        )
        self.assertGreater(
            durations[sent["regular_frequency"]],
            sent["regular_duration"] * 0.8,
            "regular binary playback was truncated",
        )
        self.stop_stream(stream_api=stream_api)

    def test_raw_audio_resets_decoder_state_after_interruption(self):
        stream_api = "uuid_raw_audio_stream"
        self.start_stream(
            f"{MOCK_URL}/raw-audio-boundary",
            stream_api=stream_api,
            playback_rate="24k",
        )
        self.start_recording("raw-audio-boundary")
        sent = self.trigger_response("raw-audio-boundary-response-sent", stream_api=stream_api)
        time.sleep(0.65)
        sample_rate, samples = self.stop_recording()
        self.assertGreater(audible_duration(samples, sample_rate), 0.35)
        self.assertAlmostEqual(
            dominant_frequency(samples, sample_rate),
            sent["replacement_frequency"],
            delta=75,
            msg="PCM carry or resampler state leaked across the interrupted stream",
        )
        self.stop_stream(stream_api=stream_api)

    def test_raw_audio_resets_decoder_state_after_oversized_frame(self):
        self.expect_module_errors("Dropping oversized binary WebSocket message")
        stream_api = "uuid_raw_audio_stream"
        with FreeSwitchEventSocket(self.uuid) as event_socket:
            self.start_stream(
                f"{MOCK_URL}/raw-audio-oversized-boundary",
                stream_api=stream_api,
                playback_rate="24k",
            )
            self.start_recording("raw-audio-oversized-boundary")
            sent = self.trigger_response(
                "raw-audio-oversized-boundary-response-sent",
                stream_api=stream_api,
            )
            stopped = event_socket.wait_for(SPEECH_STOP_EVENT)
            self.assertIsNotNone(
                stopped,
                f"raw playback did not stop after the oversized frame; observed {event_socket.seen_events}",
            )
            sample_rate, samples = self.stop_recording()

        self.assertGreater(audible_duration(samples, sample_rate), 0.35)
        self.assertAlmostEqual(
            dominant_frequency(samples, sample_rate),
            sent["replacement_frequency"],
            delta=75,
            msg="PCM carry crossed an oversized raw frame",
        )
        self.stop_stream(stream_api=stream_api)

    def test_double_start_is_rejected(self):
        self.expect_module_errors("bug already attached")
        self.start_stream()
        assert_error(self, f"uuid_openai_audio_stream {self.uuid} start {MOCK_URL} mono 24k mute_user")
        self.stop_stream()

    def test_rapid_restart(self):
        for _ in range(10):
            self.start_stream()
            self.stop_stream()

    def test_hangup_while_streaming(self):
        self.start_stream()
        before = len(mock_events())
        assert_ok(self, f"uuid_kill {self.uuid}")
        self.uuid = None
        disconnected = wait_for_event(lambda event: event.get("event") == "disconnected", before)
        self.assertIsNotNone(disconnected, "WebSocket did not disconnect after channel hangup")

    def test_reconnects_after_transient_peer_failure(self):
        assert_ok(self, f"uuid_setvar {self.uuid} STREAM_NO_RECONNECT false")
        before = len(mock_events())
        first_connection = self.start_stream(f"{MOCK_URL}/reconnect")
        self.assertEqual(first_connection["connection_number"], 1)

        reconnected = wait_for_event(
            lambda event: event.get("event") == "connected"
            and event.get("path") == "/reconnect"
            and event.get("connection_number") == 2,
            before,
            timeout=10,
        )
        self.assertIsNotNone(reconnected, "module did not reconnect after a transient peer failure")

        self.trigger_response("audio-response-sent")
        self.stop_stream()

    def test_reconnect_preserves_completed_playback_lifecycle(self):
        with FreeSwitchEventSocket(self.uuid) as event_socket:
            assert_ok(self, f"uuid_setvar {self.uuid} STREAM_NO_RECONNECT false")
            before = len(mock_events())
            first_connection = self.start_stream(f"{MOCK_URL}/reconnect-during-playback")
            self.assertEqual(first_connection["connection_number"], 1)

            sent = self.trigger_response("reconnect-playback-response-sent")

            started = event_socket.wait_for(SPEECH_START_EVENT)
            self.assertIsNotNone(
                started,
                f"module did not emit playback-start before reconnect; observed {event_socket.seen_events}",
            )
            reconnected = wait_for_event(
                lambda event: event.get("event") == "connected"
                and event.get("path") == "/reconnect-during-playback"
                and event.get("connection_number") == 2,
                before,
                timeout=10,
            )
            self.assertIsNotNone(reconnected, "module did not reconnect while completed playback was draining")

            stopped = event_socket.wait_for(SPEECH_STOP_EVENT, timeout=sent["duration_seconds"] + 3)
            self.assertIsNotNone(
                stopped,
                f"module lost playback-stop state across reconnect; observed {event_socket.seen_events}",
            )

        self.stop_stream()

    def test_immediate_peer_close_without_reconnect(self):
        before = len(mock_events())
        assert_ok(
            self,
            f"uuid_openai_audio_stream {self.uuid} start {MOCK_URL}/close-immediately mono 24k mute_user",
        )
        closed = wait_for_event(lambda event: event.get("event") == "closed", before)
        self.assertIsNotNone(closed, "mock peer did not close the WebSocket")
        disconnected = wait_for_event(lambda event: event.get("event") == "disconnected", before)
        self.assertIsNotNone(disconnected, "mock WebSocket handler did not finish after peer close")

        self.restart_after_automatic_cleanup()
        self.stop_stream()

    def test_peer_close_while_paused_cleans_up_without_resume(self):
        before = len(mock_events())
        self.start_stream(f"{MOCK_URL}/close-on-command")
        assert_ok(self, f"uuid_openai_audio_stream {self.uuid} pause")
        close_message = encode_json({"type": "integration.close"})
        assert_ok(self, f"uuid_openai_audio_stream {self.uuid} send_json {close_message}")
        closed = wait_for_event(lambda event: event.get("event") == "closed", before)
        self.assertIsNotNone(closed, "mock peer did not close the paused WebSocket")
        disconnected = wait_for_event(lambda event: event.get("event") == "disconnected", before)
        self.assertIsNotNone(disconnected, "mock WebSocket handler did not finish after paused close")

        self.restart_after_automatic_cleanup()
        self.stop_stream()

    def test_send_json_rejects_invalid_payloads(self):
        self.expect_module_errors("base64 decode error", "invalid JSON")
        self.start_stream()

        assert_error(self, f"uuid_openai_audio_stream {self.uuid} send_json %%%=")
        invalid_json = base64.b64encode(b'{"type":').decode("ascii")
        assert_error(self, f"uuid_openai_audio_stream {self.uuid} send_json {invalid_json}")

        self.stop_stream()

    def test_all_mute_controls_both_audio_directions(self):
        capture_rate = 24000
        mute_silence_size = capture_rate * PCM16_BYTES_PER_SAMPLE
        tone_source = self.start_read_tone()

        before = len(mock_events())
        self.start_stream(start_muted=False)
        captured = wait_for_event(
            lambda event: event.get("event") == "audio-received"
            and event.get("peak_amplitude", 0) >= AUDIBLE_SAMPLE_THRESHOLD,
            before,
        )
        self.assertIsNotNone(captured, "caller audio did not reach the backend before mute all")

        before = len(mock_events())
        assert_ok(self, f"uuid_openai_audio_stream {self.uuid} mute all")
        silence = wait_for_event(
            lambda event: event.get("event") == "audio-received"
            and event.get("size") == mute_silence_size
            and event.get("all_zero"),
            before,
        )
        self.assertIsNotNone(silence, "mute all did not mute caller audio")

        before = len(mock_events())
        assert_ok(self, f"uuid_openai_audio_stream {self.uuid} unmute all")
        resumed = wait_for_event(
            lambda event: event.get("event") == "audio-received"
            and event.get("peak_amplitude", 0) >= AUDIBLE_SAMPLE_THRESHOLD,
            before,
        )
        self.assertIsNotNone(resumed, "unmute all did not resume caller audio")
        self.stop_stream()
        assert_ok(self, f"uuid_displace {self.uuid} stop {tone_source}")

        assert_ok(self, f"uuid_break {self.uuid} all")
        assert_ok(self, f"uuid_broadcast {self.uuid} silence_stream://-1 aleg")
        self.assert_playback_control_creates_silence("mute all", "unmute all", "mute-all")

    def test_invalid_mute_target_is_rejected(self):
        self.expect_module_errors("invalid mute target")
        self.start_stream()
        assert_error(self, f"uuid_openai_audio_stream {self.uuid} mute invalid")
        self.stop_stream()

    def test_invalid_start_arguments_are_rejected(self):
        self.expect_module_errors(
            "invalid websocket uri",
            "invalid mix type",
            "invalid send sample rate",
            "invalid playback sample rate",
            "unexpected argument",
        )
        assert_error(self, f"uuid_openai_audio_stream {self.uuid} start http://example.test mono 24k")
        assert_error(self, f"uuid_openai_audio_stream {self.uuid} start {MOCK_URL} invalid 24k")
        assert_error(self, f"uuid_openai_audio_stream {self.uuid} start {MOCK_URL} mono 12000")
        assert_error(self, f"uuid_openai_audio_stream {self.uuid} start {MOCK_URL} mono 24k 12000")
        assert_error(self, f"uuid_openai_audio_stream {self.uuid} start {MOCK_URL} mono 24k 24k unexpected")

    def test_decimal_sample_rate_upper_bound_is_accepted(self):
        self.start_stream(send_rate="48000", playback_rate="48000")
        self.stop_stream()


if __name__ == "__main__":
    unittest.main(verbosity=2)
