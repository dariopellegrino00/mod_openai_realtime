"""Named streams share a channel, but must not share audio, controls or teardown."""

import json
import time
from pathlib import Path

from audio import AUDIBLE_SAMPLE_THRESHOLD, audible_duration, tone_durations
from esl import (
    CONNECT_EVENT,
    CONNECTION_ERROR_EVENT,
    DISCONNECT_EVENT,
    JSON_EVENT,
    PLAY_EVENT,
    SPEECH_START_EVENT,
    SPEECH_STOP_EVENT,
    FreeSwitchEventSocket,
)
from test_module import (
    MOCK_URL,
    ModuleIntegrationBase,
    api,
    assert_error,
    assert_ok,
    encode_json,
    mock_events,
    wait_for_event,
    wait_for_realtime_playback,
    wait_until,
)


class StreamInstancesTest(ModuleIntegrationBase):
    def command(self, name, action, expected_error=None):
        command = f"uuid_openai_audio_stream {self.uuid} {action} stream={name}"
        if expected_error is not None:
            return assert_error(self, command, f"{expected_error} [stream={name}]")
        return assert_ok(self, command)

    def assert_capture(self, path, start_index):
        event = wait_for_event(
            lambda item: item.get("event") == "audio-received"
            and item.get("path") == path
            and item["peak_amplitude"] > AUDIBLE_SAMPLE_THRESHOLD,
            start_index,
        )
        self.assertIsNotNone(event, f"no audible caller audio reached {path}")
        return event

    def assert_no_capture(self, path, start_index):
        captured = [
            event
            for event in mock_events()[start_index:]
            if event.get("path") == path and event.get("event") in {"audio-received", "binary"}
        ]
        self.assertEqual(captured, [], f"disabled capture reached {path}")

    def wait_for_stream_event(self, socket, event_type, name):
        event = socket.wait_for(event_type, predicate=lambda item: item.get("Stream-Name") == name)
        self.assertIsNotNone(event, f"no {event_type} event for stream {name}: {socket.seen_events}")
        return event

    def test_three_backends_receive_audio_and_controls_are_isolated(self):
        self.start_read_tone()
        self.start_stream(f"{MOCK_URL}/bot", start_muted=False)  # Legacy default still supports playback.
        self.start_stream(
            f"{MOCK_URL}/transcription",
            stream="transcription",
            direction="send",
            start_muted=False,
        )
        self.start_stream(
            f"{MOCK_URL}/analysis",
            stream="analysis",
            direction="send",
            start_muted=False,
            send_rate="16k",
        )
        for path in ("/bot", "/transcription", "/analysis"):
            self.assert_capture(path, self.event_start)

        for action, inverse in (("pause", "resume"), ("mute all", "unmute all")):
            with self.subTest(action=action):
                self.command("transcription", action)
                time.sleep(0.1)  # Allow audio already accepted by the socket to reach the mock.
                before = len(mock_events())
                time.sleep(0.25)
                self.assert_no_capture("/transcription", before)
                self.assert_capture("/bot", before)
                self.assert_capture("/analysis", before)
                self.command("transcription", inverse)
                self.assert_capture("/transcription", len(mock_events()))

        self.stop_stream(
            stream="transcription",
            final_payload={"type": "integration.final", "stream": "transcription"},
        )
        before = len(mock_events())
        self.assert_capture("/bot", before)
        self.assert_capture("/analysis", before)
        self.assert_no_capture("/transcription", before)
        self.stop_stream()  # No selector targets only default.
        self.assert_capture("/analysis", len(mock_events()))
        self.stop_stream(stream="analysis")

    def test_recv_plays_audio_and_never_captures_even_after_unmute(self):
        self.start_stream(f"{MOCK_URL}/receiver", stream="speaker", direction="recv")
        self.start_recording("recv-only")
        sent = self.trigger_response("audio-response-sent", stream="speaker")
        wait_for_realtime_playback(sent["duration_seconds"])
        self.stop_recording()
        self.assert_recording_contains_tone(sent["frequency"], sent["duration_seconds"])
        self.start_read_tone()
        for action in ("mute all", "unmute all", "pause", "resume"):
            self.command("speaker", action)
        self.expect_module_errors("stream has no send audio capability")
        self.command("speaker", "unmute user", expected_error="Send audio is disabled for this stream")
        time.sleep(0.3)
        self.stop_stream(stream="speaker", final_payload={"type": "integration.final"})
        self.assert_no_capture("/receiver", self.event_start)

    def test_mute_aliases_target_the_same_direction_in_both_apis(self):
        self.start_read_tone()
        self.start_stream(f"{MOCK_URL}/observer", stream="observer", direction="send", start_muted=False)
        self.start_stream(stream="speaker", direction="recv")
        for stream_api in ("uuid_openai_audio_stream", "uuid_raw_audio_stream"):
            for target, inverse in (("send", "user"), ("user", "send")):
                assert_ok(self, f"{stream_api} {self.uuid} mute {target} stream=observer")
                time.sleep(0.1)  # Drain capture and the one-time mute silence already sent to the socket.
                before = len(mock_events())
                time.sleep(0.2)
                self.assert_no_capture("/observer", before)
                assert_ok(self, f"{stream_api} {self.uuid} unmute {inverse} stream=observer")
                self.assert_capture("/observer", len(mock_events()))
            for target, inverse in (("recv", "openai"), ("openai", "recv")):
                assert_ok(self, f"{stream_api} {self.uuid} mute {target} stream=speaker")
                assert_ok(self, f"{stream_api} {self.uuid} unmute {inverse} stream=speaker")
            self.expect_module_errors("stream has no receive audio capability", "stream has no send audio capability")
            assert_error(
                self,
                f"{stream_api} {self.uuid} mute recv stream=observer",
                "Receive audio is disabled for this stream [stream=observer]",
            )
            assert_error(
                self,
                f"{stream_api} {self.uuid} unmute send stream=speaker",
                "Send audio is disabled for this stream [stream=speaker]",
            )
        self.stop_stream(stream="observer")
        self.stop_stream(stream="speaker")

    def test_raw_recv_uses_playback_rate_without_capture_parameters(self):
        stream_api = "uuid_raw_audio_stream"
        with FreeSwitchEventSocket(self.uuid) as socket:
            self.start_stream(
                f"{MOCK_URL}/raw-audio",
                stream="speaker",
                direction="recv",
                playback_rate="8k",
                stream_api=stream_api,
            )
            self.start_recording("raw-recv")
            sent = self.trigger_response("raw-audio-response-sent", stream="speaker", stream_api=stream_api)
            self.wait_for_stream_event(socket, SPEECH_STOP_EVENT, "speaker")
            rate, samples = self.stop_recording()
        durations = tone_durations(samples, rate, (sent["split_frequency"], sent["regular_frequency"]))
        self.assertGreater(durations[sent["split_frequency"]], sent["split_duration"] / 2)
        self.assertGreater(durations[sent["regular_frequency"]], sent["regular_duration"] * 0.8)
        self.stop_stream(stream="speaker", stream_api=stream_api)
        self.assert_no_capture("/raw-audio", self.event_start)

    def test_send_discards_backend_audio_in_json_mode(self):
        self.assert_send_discards_backend_audio("uuid_openai_audio_stream", "/send-only", "audio-response-sent")

    def test_send_discards_backend_audio_in_raw_mode(self):
        self.assert_send_discards_backend_audio("uuid_raw_audio_stream", "/raw-audio", "raw-audio-response-sent")

    def assert_send_discards_backend_audio(self, stream_api, path, response_event):
        assert_ok(self, f"uuid_setvar {self.uuid} STREAM_DISABLE_AUDIOFILES false")
        with FreeSwitchEventSocket(self.uuid) as socket:
            self.start_stream(
                f"{MOCK_URL}{path}",
                stream="observer",
                direction="send",
                stream_api=stream_api,
            )
            self.start_recording("send-only")
            self.expect_module_errors("stream has no receive audio capability")
            self.command("observer", "unmute openai", expected_error="Receive audio is disabled for this stream")
            sent = self.trigger_response(response_event, stream="observer", stream_api=stream_api)
            duration = (
                sent["duration_seconds"] if path == "/send-only" else sent["split_duration"] + sent["regular_duration"]
            )
            wait_for_realtime_playback(duration)
            rate, samples = self.stop_recording()
            self.assertEqual(
                audible_duration(samples, rate),
                0,
                "send-only stream played backend audio",
            )
            self.assertEqual(list(Path("/tmp").glob(f"{self.uuid}_*.tmp.wav")), [])
            audio_event = socket.wait_for(
                JSON_EVENT,
                timeout=0.1,
                predicate=lambda event: "delta" in json.loads(event.get("_body", "{}")),
            )
            self.assertIsNone(audio_event, "send stream forwarded an audio payload to ESL")
            self.assertNotIn((self.uuid, PLAY_EVENT), socket.seen_events)
            self.assertNotIn('"delta":', "\n".join(self.module_log_lines()))
            # JSON control is still available over either transport.
            payload = {
                "type": "session.update",
                "session": {"instructions": stream_api},
            }
            self.command("observer", f"send_json {encode_json(payload)}")
            echoed = self.wait_for_stream_event(socket, JSON_EVENT, "observer")
            self.assertEqual(json.loads(echoed["_body"])["session"], payload["session"])
            self.stop_stream(stream="observer", stream_api=stream_api)

    def test_playback_reservation_survives_pause_and_mute_and_is_released_on_stop(self):
        self.start_stream(stream="observer", direction="send")
        for owner in ("default", "bot"):
            with self.subTest(owner=owner):
                self.start_stream(stream=owner)  # Omitted direction reserves playback for either name.
                self.expect_module_errors("bug already attached")
                assert_error(
                    self,
                    f"uuid_raw_audio_stream {self.uuid} start {MOCK_URL} mono send stream={owner}",
                    f"Stream already exists [stream={owner}]",
                )
                for action in ("pause", "mute openai"):
                    self.command(owner, action)
                    for stream_api, arguments in (
                        ("uuid_raw_audio_stream", "recv"),
                        ("uuid_openai_audio_stream", "mono both"),
                    ):
                        self.expect_module_errors("channel already has a stream with playback enabled")
                        assert_error(
                            self,
                            f"{stream_api} {self.uuid} start {MOCK_URL} {arguments} stream=second",
                            f"Playback is already enabled by stream '{owner}' [stream=second]",
                        )
                self.stop_stream(stream=owner)
                self.start_stream(stream="second", direction="recv")
                self.command("observer", "resume")
                self.stop_stream(stream="second")
        self.start_stream(direction="both")
        self.stop_stream()
        self.stop_stream(stream="observer")

    def test_selectors_validate_names_and_default_is_an_alias(self):
        self.start_stream()
        self.expect_module_errors("bug already attached!")
        self.command("default", f"start {MOCK_URL} mono send", expected_error="Stream already exists")
        for selector in (
            "stream=",
            "stream=Uppercase",
            "stream=../bad",
            "stream=" + "x" * 65,
        ):
            assert_error(self, f"uuid_openai_audio_stream {self.uuid} pause {selector}", "Invalid stream name")
        for selector in ("stream=x stream=y", "stream=x stream=x"):
            assert_error(
                self, f"uuid_openai_audio_stream {self.uuid} pause {selector}", "Specify only one stream selector"
            )
        for direction in ("send recv", "both send", "recv recv"):
            self.command(
                "invalid",
                f"start {MOCK_URL} mono {direction}",
                expected_error="Specify only one audio direction: send, recv or both",
            )
        for action, fragment in (
            ("pause", "stream_session_pauseresume failed: no media bug found"),
            ("stop", "stream_session_cleanup failed: no media bug found"),
            ("send_json e30=", "no bug, failed sending json"),
        ):
            self.expect_module_errors(fragment)
            self.command("missing", action, expected_error="Stream not found")
        long_name = "a" * 62 + "_-"
        self.start_stream(stream=long_name, direction="send")
        self.stop_stream(stream=long_name)
        self.command("default", "resume")
        self.stop_stream(stream="default")

    def test_named_events_and_debug_files_follow_their_instance(self):
        assert_ok(self, f"uuid_setvar {self.uuid} STREAM_DISABLE_AUDIOFILES false")
        self.start_stream(stream="observer", direction="send")
        for name in ("speaker", "next-speaker", "speaker"):
            with self.subTest(stream=name), FreeSwitchEventSocket(self.uuid) as socket:
                self.start_stream(f"{MOCK_URL}/debug-audio", stream=name, direction="recv")
                self.wait_for_stream_event(socket, CONNECT_EVENT, name)
                self.wait_for_stream_event(socket, JSON_EVENT, name)
                self.trigger_response("debug-audio-response-sent", stream=name)
                played = self.wait_for_stream_event(socket, PLAY_EVENT, name)
                path = Path(json.loads(played["_body"])["file"])
                self.assertTrue(path.name.startswith(f"{self.uuid}_{name}_"), path)
                self.assertTrue(path.exists())
                self.wait_for_stream_event(socket, SPEECH_START_EVENT, name)
                self.wait_for_stream_event(socket, SPEECH_STOP_EVENT, name)
                self.stop_stream(stream=name)
                self.wait_for_stream_event(socket, DISCONNECT_EVENT, name)
                self.assertFalse(path.exists(), "stopped instance left its audio file behind")
                self.command("observer", "resume")
        self.stop_stream(stream="observer")

    def test_send_json_and_channel_setting_snapshots_are_per_stream(self):
        assert_ok(self, f"uuid_setvar {self.uuid} STREAM_NO_RECONNECT false")
        self.start_stream(f"{MOCK_URL}/close-on-command", stream="first", direction="send")
        assert_ok(self, f"uuid_setvar {self.uuid} STREAM_NO_RECONNECT true")
        assert_ok(self, f"uuid_setvar {self.uuid} STREAM_OPENAI_API_KEY second-key")
        assert_ok(
            self,
            f'uuid_setvar {self.uuid} STREAM_EXTRA_HEADERS {{"X-Integration-Test":"second"}}',
        )
        second = self.start_stream(f"{MOCK_URL}/second", stream="second", direction="send")
        self.assertEqual(second["authorization_headers"], ["Bearer second-key"])
        self.assertEqual(second["integration_headers"], ["second"])
        before = len(mock_events())
        for name in ("first", "second"):
            payload = {"type": "session.update", "session": {"instructions": name}}
            with FreeSwitchEventSocket(self.uuid) as socket:
                self.command(name, f"send_json {encode_json(payload)}")
                echoed = self.wait_for_stream_event(socket, JSON_EVENT, name)
                self.assertEqual(json.loads(echoed["_body"])["session"], payload["session"])
        messages = [event for event in mock_events()[before:] if event.get("event") == "message"]
        self.assertEqual(
            [(event["path"], event["payload"]["session"]["instructions"]) for event in messages],
            [("/close-on-command", "first"), ("/second", "second")],
        )
        before = len(mock_events())
        self.command("first", f"send_json {encode_json({'type': 'integration.close'})}")
        reconnected = wait_for_event(
            lambda event: event.get("event") == "connected" and event.get("path") == "/close-on-command",
            before,
        )
        self.assertIsNotNone(reconnected, "the second stream's no-reconnect setting affected the first")
        self.assertEqual(reconnected["authorization_headers"], ["Bearer integration-test-key"])
        self.assertEqual(reconnected["integration_headers"], [])
        self.stop_stream(stream="first")
        self.stop_stream(stream="second")

    def test_log_suppression_uses_the_selected_stream_snapshot(self):
        secret = "named-suppression-marker"
        assert_ok(self, f"uuid_setvar {self.uuid} STREAM_SUPPRESS_LOG true")
        self.start_stream(f"{MOCK_URL}/suppress-log-named", stream="private", direction="send")
        assert_ok(self, f"uuid_setvar {self.uuid} STREAM_SUPPRESS_LOG false")
        self.start_stream(stream="public", direction="send")
        self.expect_module_errors("WebSocket error response received (payload suppressed)")
        with FreeSwitchEventSocket(self.uuid) as socket:
            self.trigger_response(
                "error-response-sent",
                {"type": "response.create", "metadata": {"marker": secret}},
                stream="private",
            )
            event = socket.wait_for(JSON_EVENT, predicate=lambda item: secret in item.get("_body", ""))
            self.assertIsNotNone(event)
            self.assertEqual(event["Stream-Name"], "private")
        self.stop_stream(
            stream="private",
            final_payload={"type": "integration.final", "marker": secret},
        )
        self.assertNotIn(secret, "\n".join(self.module_log_lines()))
        self.stop_stream(stream="public")

    def test_terminal_close_of_paused_stream_cleans_up_only_that_instance(self):
        self.start_stream(
            f"{MOCK_URL}/survivor",
            stream="survivor",
            direction="send",
            start_muted=False,
        )
        self.start_read_tone()
        for direction in ("send", "recv", "both"):
            with self.subTest(direction=direction):
                self.start_stream(
                    f"{MOCK_URL}/close-on-command",
                    stream="temporary",
                    direction=direction,
                )
                self.command("temporary", "pause")
                self.command(
                    "temporary",
                    f"send_json {encode_json({'type': 'integration.close'})}",
                )
                self.assertTrue(wait_until(lambda: "audio_stream:temporary" not in api(f"uuid_buglist {self.uuid}")))
                self.assert_capture("/survivor", len(mock_events()))
                self.start_stream(stream="temporary", direction=direction)
                self.stop_stream(stream="temporary")
        self.stop_stream(stream="survivor")

    def test_connection_failure_releases_named_receiver_without_closing_observer(self):
        self.start_stream(stream="observer", direction="send")
        self.expect_module_errors("WebSocket connection error")
        with FreeSwitchEventSocket(self.uuid) as socket:
            self.command("speaker", "start ws://127.0.0.1:1 recv")
            self.wait_for_stream_event(socket, CONNECTION_ERROR_EVENT, "speaker")
            self.assertTrue(wait_until(lambda: "audio_stream:speaker" not in api(f"uuid_buglist {self.uuid}")))
        self.start_stream(stream="speaker", direction="recv")
        self.command("observer", "resume")
        self.stop_stream(stream="speaker")
        self.stop_stream(stream="observer")

    def test_hangup_closes_every_instance(self):
        for name, direction in (
            ("bot", "both"),
            ("transcription", "send"),
            ("analysis", "send"),
        ):
            self.start_stream(f"{MOCK_URL}/{name}", stream=name, direction=direction)
        before = len(mock_events())
        assert_ok(self, f"uuid_kill {self.uuid}")
        self.uuid = None
        for path in ("/bot", "/transcription", "/analysis"):
            disconnected = wait_for_event(
                lambda event: event.get("event") == "disconnected" and event["path"] == path,
                before,
            )
            self.assertIsNotNone(disconnected, f"{path} survived channel hangup")
