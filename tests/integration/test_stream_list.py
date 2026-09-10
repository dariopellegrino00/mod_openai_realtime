"""Stream discovery exposes current control state without connection credentials."""

from test_module import (
    MOCK_URL,
    RECONNECT_GATE,
    ModuleIntegrationBase,
    assert_error,
    assert_ok,
    encode_json,
    mock_events,
    wait_for_event,
    wait_until,
)


class StreamListTest(ModuleIntegrationBase):
    def test_empty_list_and_invalid_requests(self):
        for stream_api in ("uuid_openai_audio_stream", "uuid_raw_audio_stream"):
            self.assertEqual(self.list_streams(stream_api=stream_api), [])
            assert_error(
                self,
                f"{stream_api} {self.uuid} list stream=default",
                "Stream not found [stream=default]",
            )
            self.assertEqual(
                assert_error(self, f"{stream_api} {self.uuid} list extra"),
                "-ERR List accepts only an optional stream selector",
            )
            assert_error(
                self,
                f"{stream_api} {self.uuid} list extra stream=bot",
                "List accepts only an optional stream selector [stream=bot]",
            )
            missing_uuid = "00000000-0000-0000-0000-000000000000"
            self.expect_module_errors(f"Error locating session {missing_uuid}")
            self.assertEqual(
                assert_error(self, f"{stream_api} {missing_uuid} list"),
                "-ERR Channel not found",
            )

    def test_list_tracks_controls_membership_and_name_reuse(self):
        secret = "stream-list-secret"
        assert_ok(self, f"uuid_setvar {self.uuid} STREAM_OPENAI_API_KEY {secret}")
        # A recorder is another module's media bug; even its XML-sensitive target must not affect discovery.
        self.start_recording("list-unrelated&recorder")
        self.start_stream(f"{MOCK_URL}/{secret}", start_muted=False)
        self.start_stream(stream="observer", direction="send", stream_api="uuid_raw_audio_stream")
        self.start_stream(stream="analysis", direction="send", start_muted=False)
        self.assertTrue(wait_until(lambda: all(item["connected"] for item in self.list_streams())))
        streams = {item["name"]: item for item in self.list_streams()}
        self.assertEqual(set(streams), {"default", "observer", "analysis"})
        self.assertEqual(
            streams["default"],
            {
                "name": "default",
                "direction": "both",
                "connected": True,
                "paused": False,
                "send_muted": False,
                "recv_muted": False,
            },
        )
        self.assertEqual(
            streams["observer"],
            {
                "name": "observer",
                "direction": "send",
                "connected": True,
                "paused": False,
                "send_muted": True,
                "recv_muted": False,
            },
        )
        self.assertEqual(self.list_streams(stream="default"), [streams["default"]])
        self.assertEqual(
            {item["name"]: item for item in self.list_streams(stream_api="uuid_raw_audio_stream")},
            streams,
        )
        self.assertNotIn(secret, str(streams))

        assert_ok(self, f"uuid_openai_audio_stream {self.uuid} pause")
        assert_ok(self, f"uuid_openai_audio_stream {self.uuid} mute recv")
        assert_ok(self, f"uuid_raw_audio_stream {self.uuid} unmute user stream=observer")
        default = self.list_streams(stream="default")[0]
        self.assertTrue(default["paused"])
        self.assertTrue(default["recv_muted"])
        self.assertFalse(default["send_muted"])
        observer = self.list_streams(stream="observer")[0]
        self.assertFalse(observer["paused"])
        self.assertFalse(observer["send_muted"])
        self.assertFalse(observer["recv_muted"])

        self.stop_stream(stream="observer")
        self.assertEqual({item["name"] for item in self.list_streams()}, {"default", "analysis"})
        assert_error(
            self,
            f"uuid_openai_audio_stream {self.uuid} list stream=observer",
            "Stream not found [stream=observer]",
        )
        self.stop_stream()
        self.start_stream(stream="observer", direction="recv")
        self.assertTrue(wait_until(lambda: self.list_streams(stream="observer")[0]["connected"]))
        self.assertEqual(
            self.list_streams(stream="observer"),
            [
                {
                    "name": "observer",
                    "direction": "recv",
                    "connected": True,
                    "paused": False,
                    "send_muted": False,
                    "recv_muted": False,
                }
            ],
        )
        self.stop_stream(stream="analysis")
        self.stop_stream(stream="observer")
        self.assertEqual(self.list_streams(), [])
        self.stop_recording()

    def test_list_reports_disconnected_and_reconnected_stream(self):
        path = "/gated-stream-list"
        RECONNECT_GATE.unlink(missing_ok=True)
        self.addCleanup(RECONNECT_GATE.unlink, missing_ok=True)
        assert_ok(self, f"uuid_setvar {self.uuid} STREAM_NO_RECONNECT false")
        self.start_stream(f"{MOCK_URL}{path}", stream="observer", direction="send")
        self.assertTrue(wait_until(lambda: self.list_streams(stream="observer")[0]["connected"]))
        before = len(mock_events())
        try:
            payload = encode_json({"type": "integration.close"})
            assert_ok(
                self,
                f"uuid_openai_audio_stream {self.uuid} send_json {payload} stream=observer",
            )
            self.assertIsNotNone(
                wait_for_event(
                    lambda event: event.get("event") == "reconnect-blocked" and event.get("path") == path,
                    before,
                )
            )
            self.assertFalse(self.list_streams(stream="observer")[0]["connected"])
        finally:
            RECONNECT_GATE.touch()
        self.assertIsNotNone(
            wait_for_event(
                lambda event: event.get("event") == "connected" and event.get("path") == path,
                before,
            )
        )
        self.assertTrue(wait_until(lambda: self.list_streams(stream="observer")[0]["connected"]))
        self.stop_stream(stream="observer")
        self.assertEqual(self.list_streams(), [])
