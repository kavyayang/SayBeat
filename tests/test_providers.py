import base64
import copy
from http.client import IncompleteRead
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.domain import DomainError, Store
from backend.providers import Gateway, NoRedirect, TokenHubCandidate, TokenHubPreviewGateway, load_tokenhub_key


def work_body(**changes):
    body = {"story": "仅供自动化测试的私密故事", "title": "测试标题快照",
            "intent": dict(theme="感谢", attitude="温暖", preserve="", avoid="", recipient="朋友"),
            "service_consent": True}
    body.update(changes)
    return body


def test_wav(duration=15, channels=1, width=2, silent=False):
    # 只有测试使用的恒定PCM值，不是歌曲，绝不能放进产品素材目录。
    stream = io.BytesIO()
    with wave.open(stream, "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(width)
        wav.setframerate(8000)
        frame = bytes(width) if silent else b"\x01" + bytes(width - 1)
        wav.writeframes(frame * channels * int(8000 * duration))
    return stream.getvalue()


def ready_result(**changes):
    result = {"status": "ready", "wav_base64": base64.b64encode(test_wav()).decode(),
              "verification": {"singing": True, "lyrics_match": True,
                               "rights_cleared": True, "verifier": "test-only-verifier"},
              "export_allowed": False}
    result.update(changes)
    return result


class FakeGateway(Gateway):
    def __init__(self, features=("lyrics", "speech", "singing")):
        # 不读取环境凭据、不创建网络客户端；所有响应均由测试提供。
        self.features = set(features)
        self.calls = []
        self.lyric_result = {"lines": [{"text": "测试第一句。"}, {"text": "测试第二句。"}]}
        self.song_result = ready_result()
        self.handler = None

    def enabled(self, feature):
        return feature in self.features

    def request(self, method, path, payload=None, timeout=25):
        self.calls.append((method, path, copy.deepcopy(payload), timeout))
        if self.handler:
            return self.handler(method, path, payload, timeout)
        if path == "/lyrics":
            return copy.deepcopy(self.lyric_result)
        if path == "/transcribe":
            return {"text": "测试转写文本"}
        if method == "POST":
            return {"job_id": "test-remote-id"}
        return copy.deepcopy(self.song_result)


class Clock:
    def __init__(self):
        self.now = 0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(patch.stopall)
        patch.dict(os.environ, {}, clear=True).start()
        # 适配器测试只能使用Mock；任何意外外联直接失败。
        patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("禁止网络访问")).start()
        self.gateway = Gateway()

    def error(self, status, code, function, *args, **kwargs):
        with self.assertRaises(DomainError) as raised:
            function(*args, **kwargs)
        self.assertEqual((raised.exception.status, raised.exception.code), (status, code))

    def configured(self, **changes):
        env = {"AI_GATEWAY_URL": "http://127.0.0.1:9876/adapter/", "AI_GATEWAY_KEY": "test-only-not-a-real-key",
               "AI_GATEWAY_FEATURES": "lyrics,speech,singing", "AI_RIGHTS_CONFIRMED": "true"}
        env.update(changes)
        with patch.dict(os.environ, env, clear=True):
            return Gateway()

    def response(self, raw):
        response = Mock()
        response.read.return_value = raw
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        self.gateway.opener = Mock()
        self.gateway.opener.open.return_value = response
        return response

    def test_default_capabilities_are_disabled(self):
        self.assertEqual(self.gateway.capabilities(), dict(lyrics=False, speech=False, singing=False))

    def test_configuration_requires_url_key_rights_and_feature(self):
        self.assertTrue(all(self.configured().capabilities().values()))
        for field in ("AI_GATEWAY_URL", "AI_GATEWAY_KEY", "AI_RIGHTS_CONFIRMED", "AI_GATEWAY_FEATURES"):
            with self.subTest(field=field):
                self.assertFalse(any(self.configured(**{field: ""}).capabilities().values()))
        self.assertEqual(self.configured(AI_GATEWAY_FEATURES="lyrics").capabilities(),
                         dict(lyrics=True, speech=False, singing=False))
        self.assertFalse(self.configured(AI_RIGHTS_CONFIRMED="TRUE").enabled("singing"))

    def test_configuration_rejects_insecure_or_malformed_urls(self):
        for url in ("http://example.invalid", "file:///tmp/test", "https:///missing-host",
                    "https://127.0.0.1:bad", "https://user:pass@127.0.0.1",
                    "https://127.0.0.1/?key=value", "https://127.0.0.1/#fragment"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.configured(AI_GATEWAY_URL=url)

    def test_unconfigured_lyrics_and_speech_never_call_request(self):
        with patch.object(self.gateway, "request") as request:
            self.error(503, "PROVIDER_NOT_CONFIGURED", self.gateway.lyrics, {}, "测试", None)
            self.error(503, "PROVIDER_NOT_CONFIGURED", self.gateway.speech, {})
            request.assert_not_called()

    def test_request_contract_and_json_response(self):
        self.gateway = self.configured()
        response = self.response('{"text":"测试"}'.encode())
        self.assertEqual(self.gateway.request("POST", "/lyrics", {"story": "测试"}, timeout=3), {"text": "测试"})
        request = self.gateway.opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:9876/adapter/lyrics")
        self.assertEqual(request.get_header("Authorization"), "Bearer test-only-not-a-real-key")
        self.assertEqual(json.loads(request.data), {"story": "测试"})
        self.assertEqual(self.gateway.opener.open.call_args.kwargs, {"timeout": 3})
        response.read.assert_called_once_with(24 * 1024 * 1024 + 1)
        response.__exit__.assert_called_once()

    def test_request_rejects_bad_json_non_object_and_invalid_encoding(self):
        for raw in (b"not-json", b"[]", b"null", b"\xff", b"{"):
            with self.subTest(raw=raw):
                self.gateway = self.configured()
                self.response(raw)
                self.error(502, "PROVIDER_UNAVAILABLE", self.gateway.request, "GET", "/songs/test")

    def test_truncated_http_response_is_provider_error(self):
        self.gateway = self.configured()
        response = self.response(b"")
        response.read.side_effect = IncompleteRead(b"partial")
        self.error(502, "PROVIDER_UNAVAILABLE", self.gateway.request, "GET", "/songs/test")
        response.__exit__.assert_called_once()

    def test_request_limits_response_size(self):
        self.gateway = self.configured()
        self.response(b"x" * (24 * 1024 * 1024 + 1))
        self.error(502, "PROVIDER_INVALID", self.gateway.request, "GET", "/songs/test")

    def test_request_network_failures_are_sanitized(self):
        for exc in (urllib.error.URLError("secret"), TimeoutError("secret"), OSError("secret"),
                    urllib.error.HTTPError("http://127.0.0.1/test", 401, "secret", {}, None)):
            with self.subTest(exc=type(exc).__name__):
                self.gateway = self.configured()
                self.gateway.opener = Mock()
                self.gateway.opener.open.side_effect = exc
                self.error(502, "PROVIDER_UNAVAILABLE", self.gateway.request, "GET", "/songs/test")

    def test_redirect_is_not_followed(self):
        self.error(502, "PROVIDER_REDIRECT", NoRedirect().redirect_request,
                   None, None, 302, "redirect", {}, "http://127.0.0.1/elsewhere")
        self.gateway = self.configured()
        self.gateway.opener = Mock()
        self.gateway.opener.open.side_effect = DomainError(502, "PROVIDER_REDIRECT", "拒绝重定向")
        self.error(502, "PROVIDER_REDIRECT", self.gateway.request, "GET", "/songs/test")

    def test_lyrics_payload_preserves_selected_version_and_rules(self):
        gateway = FakeGateway()
        work = dict(work_body(), current_lyric_id="v1", lyrics=[{"id": "v1", "lines": []}, {"id": "v2"}])
        gateway.lyrics(work, "只改一句", "line1")
        payload = gateway.calls[0][2]
        self.assertEqual(payload["current_lyrics"], work["lyrics"][0])
        self.assertEqual(payload["target_line_id"], "line1")
        self.assertTrue(payload["rules"]["preserve_locks_exactly"])
        self.assertTrue(payload["rules"]["unselected_lines_immutable"])
        self.assertEqual(payload["story"], work["story"])

    def speech_body(self, **changes):
        body = {"consent": True, "mime": "audio/webm", "audio_base64": base64.b64encode(b"t" * 101).decode()}
        body.update(changes)
        return body

    def test_speech_requires_explicit_consent(self):
        gateway = FakeGateway()
        for consent in (None, False, 1, "true"):
            self.error(422, "CONSENT_REQUIRED", gateway.speech, self.speech_body(consent=consent))
        self.assertEqual(gateway.calls, [])

    def test_speech_rejects_formats_including_non_string_types(self):
        gateway = FakeGateway()
        for mime in (None, "audio/wav", "text/plain", [], {}):
            with self.subTest(mime=mime):
                self.error(422, "AUDIO_FORMAT", gateway.speech, self.speech_body(mime=mime))
        self.assertEqual(gateway.calls, [])

    def test_speech_rejects_invalid_base64_and_size(self):
        gateway = FakeGateway()
        for audio in (None, [], "!", "", base64.b64encode(b"x" * 100).decode(),
                      base64.b64encode(b"x" * (6 * 1024 * 1024 + 1)).decode()):
            with self.subTest(size=len(audio) if isinstance(audio, str) else None):
                self.error(422, "AUDIO_INVALID", gateway.speech, self.speech_body(audio_base64=audio))
        self.error(422, "AUDIO_INVALID", gateway.speech, {"consent": True, "mime": "audio/webm"})
        self.assertEqual(gateway.calls, [])

    def test_speech_success_requires_confirmation_and_accepts_supported_formats(self):
        for mime in ("audio/webm", "audio/webm;codecs=opus", "audio/mp4", "audio/ogg;codecs=opus"):
            gateway = FakeGateway()
            result = gateway.speech(self.speech_body(mime=mime))
            self.assertEqual(result, {"text": "测试转写文本", "requires_confirmation": True})
            self.assertEqual(gateway.calls[0][2]["max_duration"], 60)
            self.assertNotIn("consent", gateway.calls[0][2])

    def test_speech_rejects_empty_long_and_malformed_transcriptions(self):
        gateway = FakeGateway()
        for text in (None, "", "   ", "字" * 501, 1, [], "\ud800"):
            with self.subTest(text=repr(text)), patch.object(gateway, "request", return_value={"text": text}):
                self.error(502, "TRANSCRIPTION_INVALID", gateway.speech, self.speech_body())


class WorkerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.assets = self.root / "assets"
        self.assets.mkdir()
        self.store = Store(self.root / "test.sqlite3")
        self.owner = self.store.create_session()["owner_id"]
        self.gateway = FakeGateway()
        self.counter = 0
        self.clock = Clock()
        clock_patch = patch("backend.providers.time", self.clock)
        clock_patch.start()
        self.addCleanup(clock_patch.stop)
        network = patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("禁止网络访问"))
        network.start()
        self.addCleanup(network.stop)

    def job(self):
        work = self.store.create_work(self.owner, work_body())
        work = self.store.save_lyrics(self.owner, work["id"], {"base_version": 1,
                "source": "manual", "lines": [{"text": "测试第一句。"}, {"text": "测试第二句。"}]})
        self.counter += 1
        job = self.store.create_job(self.owner, work["id"], {"base_version": work["version"],
                    "lyric_id": work["current_lyric_id"], "idempotency_key": str(self.counter), "confirmed": True}, True)
        return work, job

    def run_job(self, job, **kwargs):
        self.gateway.run_song(self.store, self.owner, job, self.assets, **kwargs)
        return self.store.get_job(self.owner, job["id"])

    def assert_failed(self, job, code):
        result = self.store.get_job(self.owner, job["id"])
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error"]["code"], code)
        self.assertIsNone(result["audio_id"])
        self.assertEqual(list(self.assets.iterdir()), [])
        self.assertEqual(self.store.get_work(self.owner, job["work_id"])["audios"], [])

    def test_success_uses_owner_snapshot_and_checking_state(self):
        work, job = self.job()
        with patch.object(self.store, "get_job", wraps=self.store.get_job) as reads, \
                patch.object(self.store, "transition_job", wraps=self.store.transition_job) as transitions:
            done = self.run_job(job)
        self.assertEqual(done["status"], "ready")
        self.assertTrue(all(call.args[0] == self.owner for call in reads.call_args_list))
        self.assertEqual([call.args[1] for call in transitions.call_args_list], ["generating", "checking"])
        self.assertEqual(self.gateway.calls[0][2]["snapshot"], job["snapshot"])
        audio = self.store.media_asset(self.owner, done["audio_id"])
        self.assertEqual((self.assets / audio["asset_name"]).read_bytes(), test_wav())
        self.assertEqual(audio["lyrics"], work["lyrics"][-1]["lines"])
        self.assertFalse(audio["export_allowed"])

    def test_unconfigured_worker_fails_without_request(self):
        _, job = self.job()
        self.gateway.features.clear()
        self.run_job(job)
        self.assert_failed(job, "PROVIDER_NOT_CONFIGURED")
        self.assertEqual(self.gateway.calls, [])

    def test_remote_job_ids_are_validated_before_polling(self):
        for remote in (None, [], "", "../escape", "x?secret=1", "x" * 161):
            with self.subTest(remote=remote):
                _, job = self.job()
                with patch.object(self.gateway, "request", return_value={"job_id": remote}) as request:
                    self.run_job(job)
                self.assertEqual(request.call_count, 1)
                self.assert_failed(job, "PROVIDER_INVALID")

    def test_remote_failures_and_unknown_states_fail_closed(self):
        for state, code in (("failed", "GENERATION_FAILED"), ("unknown", "PROVIDER_INVALID"),
                            (None, "PROVIDER_INVALID"), ([], "PROVIDER_INVALID")):
            with self.subTest(state=state):
                _, job = self.job()
                self.gateway.song_result = {"status": state}
                self.run_job(job)
                self.assert_failed(job, code)

    def test_verification_requires_all_true_and_named_verifier(self):
        original = ready_result()
        variants = [{}, None, [], {**original["verification"], "verifier": " "},
                    {**original["verification"], "verifier": True}]
        variants += [{**original["verification"], key: value}
                     for key in ("singing", "lyrics_match", "rights_cleared") for value in (False, 1, None)]
        for verification in variants:
            with self.subTest(verification=verification):
                _, job = self.job()
                self.gateway.song_result = dict(original, verification=verification)
                self.run_job(job)
                self.assert_failed(job, "AUDIO_UNVERIFIED")

    def test_invalid_base64_non_wav_silence_and_truncation_never_succeed(self):
        invalid = [None, "!", base64.b64encode(b"not-a-wav").decode(),
                   base64.b64encode(test_wav(silent=True)).decode(),
                   base64.b64encode(test_wav()[:-10]).decode()]
        for data in invalid:
            with self.subTest(kind=type(data).__name__, size=len(data) if isinstance(data, str) else 0):
                _, job = self.job()
                self.gateway.song_result = ready_result(wav_base64=data)
                self.run_job(job)
                self.assert_failed(job, "AUDIO_INVALID")

    def test_wav_duration_and_pcm_format_bounds(self):
        for kwargs in ({"duration": 14.99}, {"duration": 30.01}, {"channels": 3}, {"width": 1}):
            with self.subTest(kwargs=kwargs):
                _, job = self.job()
                self.gateway.song_result = ready_result(wav_base64=base64.b64encode(test_wav(**kwargs)).decode())
                self.run_job(job)
                self.assert_failed(job, "AUDIO_INVALID")

    def test_wav_upper_duration_bound_and_export_boolean(self):
        _, job = self.job()
        self.gateway.song_result = ready_result(wav_base64=base64.b64encode(test_wav(duration=30, channels=2)).decode(),
                                               export_allowed=True)
        done = self.run_job(job)
        audio = self.store.media_asset(self.owner, done["audio_id"])
        self.assertEqual(audio["duration"], 30)
        self.assertTrue(audio["export_allowed"])

    def test_cancelled_before_start_does_not_submit(self):
        _, job = self.job()
        self.store.cancel_job(self.owner, job["id"])
        self.assertEqual(self.run_job(job)["status"], "cancelled")
        self.assertEqual(self.gateway.calls, [])

    def test_cancel_between_active_check_and_transition_does_not_submit(self):
        _, job = self.job()
        transition = self.store.transition_job
        def cancel_first(jid, status, *args):
            self.store.cancel_job(self.owner, jid)
            return transition(jid, status, *args)
        with patch.object(self.store, "transition_job", side_effect=cancel_first):
            self.assertEqual(self.run_job(job)["status"], "cancelled")
        self.assertEqual(self.gateway.calls, [])

    def test_cancel_during_poll_ignores_late_ready(self):
        _, job = self.job()
        def respond(method, path, payload, timeout):
            if method == "POST":
                return {"job_id": "remote"}
            self.store.cancel_job(self.owner, job["id"])
            return ready_result()
        self.gateway.handler = respond
        self.assertEqual(self.run_job(job)["status"], "cancelled")
        self.assertEqual(list(self.assets.iterdir()), [])

    def test_delete_during_poll_does_not_restore_private_state(self):
        work, job = self.job()
        def respond(method, path, payload, timeout):
            if method == "POST":
                return {"job_id": "remote"}
            self.store.delete_work(self.owner, work["id"], {"base_version": work["version"], "confirm": True})
            return ready_result()
        self.gateway.handler = respond
        self.gateway.run_song(self.store, self.owner, job, self.assets)
        terminal = self.store.transition_job(job["id"], "failed")
        self.assertEqual(terminal["status"], "cancelled")
        self.assertIsNone(terminal["snapshot"])
        self.assertEqual(self.store.list_works(self.owner), [])
        self.assertEqual(list(self.assets.iterdir()), [])

    def test_cancel_or_delete_during_file_write_removes_orphan(self):
        write = Path.write_bytes
        for action in ("cancel", "delete"):
            with self.subTest(action=action):
                work, job = self.job()
                def write_then_cancel(path, data):
                    result = write(path, data)
                    if action == "cancel":
                        self.store.cancel_job(self.owner, job["id"])
                    else:
                        self.store.delete_work(self.owner, work["id"], {"base_version": work["version"], "confirm": True})
                    return result
                with patch.object(Path, "write_bytes", write_then_cancel):
                    self.gateway.run_song(self.store, self.owner, job, self.assets)
                self.assertEqual(self.store.transition_job(job["id"], "failed")["status"], "cancelled")
                self.assertEqual(list(self.assets.iterdir()), [])

    def test_file_write_or_completion_failure_removes_partial_asset(self):
        write = Path.write_bytes
        _, job = self.job()
        def partial_write(path, data):
            write(path, data[:20])
            raise OSError("仅测试内部错误，不得回传")
        with patch.object(Path, "write_bytes", partial_write):
            self.run_job(job)
        self.assert_failed(job, "GENERATION_FAILED")
        _, job = self.job()
        with patch.object(self.store, "complete_job", side_effect=DomainError(503, "STORE_UNAVAILABLE", "内部错误")):
            self.run_job(job)
        self.assert_failed(job, "STORE_UNAVAILABLE")

    def test_deadline_zero_never_submits_and_sets_timeout_code(self):
        _, job = self.job()
        done = self.run_job(job, deadline_seconds=0)
        self.assertEqual(done["status"], "timed_out")
        self.assertEqual(done["error"]["code"], "GENERATION_TIMEOUT")
        self.assertEqual(self.gateway.calls, [])

    def test_deadline_bounds_submit_and_poll_timeouts(self):
        _, job = self.job()
        def pending(method, path, payload, timeout):
            if method == "POST":
                self.clock.now += 0.02
                return {"job_id": "remote"}
            return {"status": "generating"}
        self.gateway.handler = pending
        done = self.run_job(job, deadline_seconds=0.05)
        self.assertEqual(done["status"], "timed_out")
        self.assertLessEqual(self.gateway.calls[0][3], 0.05)
        self.assertLessEqual(self.gateway.calls[1][3], 0.031)
        self.assertEqual(self.clock.now, 0.05)

    def test_deadline_late_ready_is_not_committed(self):
        _, job = self.job()
        def late(method, path, payload, timeout):
            if method == "POST":
                return {"job_id": "remote"}
            self.clock.now = 5
            return ready_result()
        self.gateway.handler = late
        done = self.run_job(job, deadline_seconds=5)
        self.assertEqual(done["status"], "timed_out")
        self.assertIsNone(done["audio_id"])
        self.assertEqual(list(self.assets.iterdir()), [])

    def test_deadline_during_file_write_cleans_asset(self):
        _, job = self.job()
        write = Path.write_bytes
        def slow_write(path, data):
            result = write(path, data)
            self.clock.now = 5
            return result
        with patch.object(Path, "write_bytes", slow_write):
            done = self.run_job(job, deadline_seconds=5)
        self.assertEqual(done["status"], "timed_out")
        self.assertIsNone(done["audio_id"])
        self.assertEqual(list(self.assets.iterdir()), [])

    def test_timeout_exception_at_deadline_is_timeout_not_generic_failure(self):
        _, job = self.job()
        def timeout(method, path, payload, timeout):
            self.clock.now = 5
            raise DomainError(502, "PROVIDER_UNAVAILABLE", "测试超时")
        self.gateway.handler = timeout
        done = self.run_job(job, deadline_seconds=5)
        self.assertEqual(done["status"], "timed_out")
        self.assertEqual(done["error"]["code"], "GENERATION_TIMEOUT")


class LocalTokenHubKeyTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / ".env"
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def test_missing_file_and_environment_override(self):
        self.assertEqual(load_tokenhub_key(self.path), "")
        with patch.dict(os.environ, {"TENCENT_TOKENHUB_API_KEY": "test-env-only"}):
            self.assertEqual(load_tokenhub_key(self.path), "test-env-only")

    def test_reads_only_private_data_not_shell_statements(self):
        self.path.write_text("# comment\nOTHER_KEY=ignore\nTENCENT_TOKENHUB_API_KEY=test-file-only\n", encoding="utf-8")
        self.path.chmod(0o600)
        self.assertEqual(load_tokenhub_key(self.path), "test-file-only")

    def test_rejects_world_readable_or_symlink_or_duplicate_key(self):
        self.path.write_text("TENCENT_TOKENHUB_API_KEY=test-file-only\n", encoding="utf-8")
        self.path.chmod(0o644)
        with self.assertRaises(DomainError) as raised:
            load_tokenhub_key(self.path)
        self.assertEqual(raised.exception.code, "CONFIG_INSECURE")
        self.path.chmod(0o600)
        link = self.path.parent / "linked.env"
        link.symlink_to(self.path)
        with self.assertRaises(DomainError) as raised:
            load_tokenhub_key(link)
        self.assertEqual(raised.exception.code, "CONFIG_INSECURE")
        self.path.write_text("TENCENT_TOKENHUB_API_KEY=one\nTENCENT_TOKENHUB_API_KEY=two\n", encoding="utf-8")
        with self.assertRaises(DomainError) as raised:
            load_tokenhub_key(self.path)
        self.assertEqual(raised.exception.code, "CONFIG_INVALID")


class TokenHubCandidateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "private-candidate.mp3"
        self.opener = Mock()
        probe = patch("backend.providers.subprocess.run", return_value=Mock(
            returncode=0, stdout="File type ID: MPG3\nestimated duration: 23.0 sec\n"))
        probe.start()
        self.addCleanup(probe.stop)
        self.client = TokenHubCandidate(api_key="test-only-key", opener=self.opener)
        decoder = patch('backend.providers.inspect_mp3', return_value={'duration': 23.0})
        decoder.start()
        self.addCleanup(decoder.stop)
        self.audio = b"ID3" + b"not-a-real-song" * 100  # format marker only; never product audio
        self.response = {"base_resp": {"status_code": 0},
                         "data": {"status": 2, "audio": self.audio.hex()},
                         "extra_info": {"music_duration": 23000}}

    def feed(self, data=None):
        response = Mock()
        response.read.return_value = json.dumps(data if data is not None else self.response).encode()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        self.opener.open.return_value = response
        return response

    def generate(self, **changes):
        params = {"confirm_paid_call": True, "confirm_input_rights": True}
        params.update(changes)
        return self.client.generate("第一句\n第二句", "温暖轻快", self.path, **params)

    def test_needs_both_confirmations_and_server_side_key_before_network(self):
        for flags in ({"confirm_paid_call": False}, {"confirm_input_rights": False},
                      {"confirm_paid_call": "false"}, {"confirm_input_rights": 1}):
            with self.subTest(flags=flags), self.assertRaises(DomainError) as raised:
                self.generate(**flags)
            self.assertEqual(raised.exception.code, "CONFIRMATION_REQUIRED")
        self.client.api_key = ""
        with self.assertRaises(DomainError) as raised:
            self.generate()
        self.assertEqual(raised.exception.code, "PROVIDER_NOT_CONFIGURED")
        self.opener.open.assert_not_called()

    def test_documented_request_and_private_unverified_candidate(self):
        response = self.feed()
        result = self.generate()
        req = self.opener.open.call_args.args[0]
        self.assertEqual(req.full_url, TokenHubCandidate.URL)
        self.assertEqual(req.get_header("Authorization"), "Bearer test-only-key")
        self.assertEqual(self.opener.open.call_args.kwargs["timeout"], 180)
        body = json.loads(req.data)
        self.assertEqual(body["lyrics"], "第一句\n第二句")
        self.assertEqual(body["model"], "minimax-music-v3.0")
        self.assertEqual(body["audio_setting"], {"format": "mp3"})
        self.assertEqual(body["output_format"], "url")
        self.assertFalse(body["is_instrumental"])
        self.assertFalse(body["lyrics_optimizer"])
        self.assertEqual(self.path.read_bytes(), self.audio)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(result["status"], "private_candidate_unverified")
        self.assertFalse(result["singing_verified"])
        self.assertFalse(result["lyrics_match_verified"])
        self.assertFalse(result["output_rights_verified"])
        self.assertTrue(result["in_15_30_second_target"])
        self.assertEqual(result["duration_seconds_probed"], 23)
        response.read.assert_called_once_with(32 * 1024 * 1024 + 1)

    def test_audio_without_assumed_id3_or_frame_prefix_uses_format_probe(self):
        audio = b"metadata-before-frames" + b"candidate-bytes" * 100
        self.feed({**self.response, "data": {"status": 2, "audio": audio.hex()}})
        result = self.generate()
        self.assertEqual(self.path.read_bytes(), audio)
        self.assertEqual(result["status"], "private_candidate_unverified")
        self.assertFalse(result["singing_verified"])

    def test_non_hex_audio_gives_bounded_diagnosis_and_no_candidate(self):
        cases = (("https://example.invalid/private?token=secret", "HTTPS 链接"),
                 ("cHJpdmF0ZS1hdWRpbz==", "疑似 Base64"),
                 ("abc", "十六进制字符数为奇数"), ("<secret>", "其他非十六进制"))
        for audio, shape in cases:
            with self.subTest(shape=shape):
                self.feed({**self.response, "data": {"status": 2, "audio": audio},
                           "request_id": "31a58927-074e-4ee7-8dfe-829a1063e0f9",
                           "trace_id": "4-WandAudio-12345678"})
                with patch('backend.providers.download_music',side_effect=DomainError(502,'AUDIO_INVALID','HTTPS 链接不安全')), self.assertRaises(DomainError) as raised:
                    self.generate()
                self.assertEqual(raised.exception.code, "AUDIO_INVALID")
                self.assertIn(shape, raised.exception.message)
                self.assertIn("请求ID 31a58927-074e-4ee7-8dfe-829a1063e0f9", raised.exception.message)
                self.assertNotIn(audio, raised.exception.message)
                self.assertNotIn("secret", raised.exception.message)
                self.assertFalse(self.path.exists())

    def test_music_diagnosis_rejects_untrusted_provider_ids(self):
        self.feed({**self.response, "data": {"status": 2, "audio": "not-hex"},
                   "request_id": "https://private.invalid/token=secret", "trace_id": "secret?audio=private"})
        with self.assertRaises(DomainError) as raised:
            self.generate()
        self.assertEqual(raised.exception.code, "AUDIO_INVALID")
        self.assertNotIn("token=", raised.exception.message)
        self.assertNotIn("audio=", raised.exception.message)
        self.assertFalse(self.path.exists())

    def test_documented_url_response_downloads_and_saves_mp3_not_url_text(self):
        url = 'https://example.com/music.mp3?private-signature=test'
        self.feed({**self.response, 'data': {'status': 2, 'audio': url}})
        with patch('backend.providers.download_music', return_value=self.audio) as fetch:
            result = self.generate()
        fetch.assert_called_once_with(url)
        self.assertEqual(self.path.read_bytes(), self.audio)
        self.assertEqual(result['response_format'], 'url')
        self.assertNotIn(url, json.dumps(result))

    def test_failed_full_decode_removes_candidate(self):
        self.feed()
        with patch('backend.providers.inspect_mp3', side_effect=DomainError(422,'AUDIO_INVALID','损坏')):
            with self.assertRaises(DomainError):
                self.generate()
        self.assertFalse(self.path.exists())

    def test_decoded_duration_mismatch_never_saves_truncated_audio(self):
        self.feed()
        with patch('backend.providers.inspect_mp3',return_value={'duration':15}):
            with self.assertRaises(DomainError) as raised:
                self.generate()
        self.assertEqual(raised.exception.code,'AUDIO_INVALID')
        self.assertFalse(self.path.exists())

    def test_probe_must_confirm_mp3_file_type(self):
        self.feed()
        with patch("backend.providers.subprocess.run", return_value=Mock(
                returncode=0, stdout="File type ID: WAVE\nestimated duration: 23.0 sec\n")):
            with self.assertRaises(DomainError) as raised:
                self.generate()
        self.assertEqual(raised.exception.code, "AUDIO_INVALID")
        self.assertFalse(self.path.exists())

    def test_rotated_local_key_is_used_before_each_confirmed_request(self):
        self.feed()
        with patch("backend.providers.load_tokenhub_key", side_effect=["old-test-key", "rotated-test-key"]):
            client = TokenHubCandidate(opener=self.opener)
            client.generate("第一句\n第二句", "温暖轻快", self.path,
                            confirm_paid_call=True, confirm_input_rights=True)
        req = self.opener.open.call_args.args[0]
        self.assertEqual(req.get_header("Authorization"), "Bearer rotated-test-key")

    def test_preview_capability_rechecks_local_key_after_rotation(self):
        with patch("backend.providers.load_tokenhub_key", side_effect=["old-test-key", "", "new-test-key", "new-test-key", "new-test-key"]):
            gateway = TokenHubPreviewGateway()
            self.assertFalse(gateway.enabled("singing"))
            self.assertTrue(gateway.enabled("singing"))
            self.assertTrue(gateway.enabled("lyrics"))
            self.assertTrue(gateway.enabled("speech"))

    def test_bad_inputs_rejected_without_network(self):
        for kwargs in ({"model": "music-3.0"}, {"prompt": "x" * 2001},
                       {"lyrics": ""}, {"lyrics": "字" * 3501}):
            with self.subTest(kwargs=tuple(kwargs)), self.assertRaises(DomainError):
                self.client.generate(kwargs.get("lyrics", "两句歌词"), kwargs.get("prompt", "民谣"),
                                     self.path, model=kwargs.get("model", "minimax-music-v3.0"),
                                     confirm_paid_call=True, confirm_input_rights=True)
        self.opener.open.assert_not_called()

    def test_response_never_claims_singing_or_rights_and_rejects_failures(self):
        for data in ({"base_resp": {"status_code": 1001}, "data": self.response["data"]},
                     {**self.response, "base_resp": {"status_code": False}},
                     {**self.response, "data": {"status": 1, "audio": self.audio.hex()}},
                     {**self.response, "data": {"status": 2.0, "audio": self.audio.hex()}},
                     {**self.response, "data": {"status": 2, "audio": b"fake".hex()}},
                     {**self.response, "extra_info": {"music_duration": None}},
                     {**self.response, "extra_info": {"music_duration": 10 ** 1000}}):
            with self.subTest(data=str(data)[:80]):
                self.feed(data)
                with self.assertRaises(DomainError):
                    self.generate()
                self.assertFalse(self.path.exists())

    def test_failed_audio_probe_removes_reserved_file(self):
        self.feed()
        with patch("backend.providers.subprocess.run", return_value=Mock(returncode=1, stdout="")):
            with self.assertRaises(DomainError) as raised:
                self.generate()
        self.assertEqual(raised.exception.code, "AUDIO_INVALID")
        self.assertFalse(self.path.exists())

    def test_http_errors_distinguish_auth_billing_and_rate_limit_without_leaking_details(self):
        for status, code in ((401, "PROVIDER_AUTH"), (403, "PROVIDER_AUTH"),
                             (402, "PROVIDER_BILLING"), (429, "PROVIDER_RATE_LIMIT"),
                             (500, "PROVIDER_HTTP")):
            with self.subTest(status=status):
                self.opener.open.side_effect = urllib.error.HTTPError(
                    "https://example.invalid/secret", status, "secret-error", {}, None)
                with self.assertRaises(DomainError) as raised:
                    self.generate()
                self.assertEqual(raised.exception.code, code)
                self.assertIn(f"HTTP {status}", raised.exception.message)
                self.assertNotIn("secret", raised.exception.message)
                self.assertFalse(self.path.exists())

    def test_structured_billing_error_has_safe_business_code_and_request_id(self):
        for status, business, code, guidance in ((402, "401007", "PROVIDER_BILLING", "开启后付费"),
                                                 (402, "401008", "PROVIDER_BILLING", "开启后付费"),
                                                 (402, "403004", "PROVIDER_BILLING", "欠费"),
                                                 (403, "403004", "PROVIDER_BILLING", "欠费"),
                                                 (429, 429005, "PROVIDER_RATE_LIMIT", "限流")):
            with self.subTest(status=status, business=business):
                body = json.dumps({"error": {"code": business, "request_id": "req-12345",
                         "message_zh": "secret-user-lyrics-and-token"}}).encode()
                self.opener.open.side_effect = urllib.error.HTTPError(
                    "https://example.invalid/secret", status, "secret", {}, io.BytesIO(body))
                with self.assertRaises(DomainError) as raised:
                    self.generate()
                self.assertEqual(raised.exception.code, code)
                self.assertIn(f"业务码 {business}", raised.exception.message)
                self.assertIn("供应商请求ID req-12345", raised.exception.message)
                self.assertIn(guidance, raised.exception.message)
                self.assertNotIn("secret", raised.exception.message)
                self.assertFalse(self.path.exists())

    def test_untrusted_provider_error_text_is_not_exposed(self):
        body = json.dumps({"error": {"code": "bad-secret", "request_id": "token=secret",
                                 "message": "secret-story"}}).encode()
        self.opener.open.side_effect = urllib.error.HTTPError(
            "https://example.invalid/secret", 402, "secret", {}, io.BytesIO(body))
        with self.assertRaises(DomainError) as raised:
            self.generate()
        self.assertEqual(raised.exception.code, "PROVIDER_BILLING")
        self.assertNotIn("secret", raised.exception.message)
        self.assertNotIn("业务码", raised.exception.message)
        self.assertNotIn("供应商请求ID", raised.exception.message)
        self.assertFalse(self.path.exists())

    def test_provider_network_failure_removes_reserved_file(self):
        self.opener.open.side_effect = urllib.error.URLError("secret")
        with self.assertRaises(DomainError) as raised:
            self.generate()
        self.assertEqual(raised.exception.code, "PROVIDER_UNAVAILABLE")
        self.assertFalse(self.path.exists())

    def test_existing_output_is_not_overwritten_or_billed(self):
        self.path.write_bytes(b"existing")
        with self.assertRaises(DomainError) as raised:
            self.generate()
        self.assertEqual(raised.exception.code, "OUTPUT_EXISTS")
        self.assertEqual(self.path.read_bytes(), b"existing")
        self.opener.open.assert_not_called()


class TokenHubTextSpeechTests(unittest.TestCase):
    def setUp(self):
        self.gateway = TokenHubPreviewGateway(api_key="test-only-key")
        self.audio = base64.b64encode(test_wav(duration=1)).decode("ascii")

    def test_speech_requires_explicit_billing_and_valid_audio_before_call(self):
        with patch.object(self.gateway, "_model_request") as request:
            for body in ({"mime": "audio/wav", "audio_base64": self.audio, "consent": True},
                         {"mime": "audio/wav", "audio_base64": self.audio, "paid_call_confirmed": True},
                         {"mime": "audio/wav", "audio_base64": "fake", "consent": True, "paid_call_confirmed": True}):
                with self.subTest(body=body), self.assertRaises(DomainError):
                    self.gateway.speech(body)
            request.assert_not_called()

    def test_speech_sends_documented_data_not_public_url(self):
        with patch.object(self.gateway, "_model_request", return_value={"status": "completed", "output": {"text": "把第二句改得轻快一点"}}) as request:
            result = self.gateway.speech({"mime": "audio/wav", "audio_base64": self.audio,
                                          "consent": True, "paid_call_confirmed": True})
        self.assertEqual(result, {"text": "把第二句改得轻快一点", "requires_confirmation": True})
        path, payload = request.call_args.args
        self.assertEqual(path, "/v1/wand/asrproxy/sync_transcribe")
        self.assertEqual(payload["voice_encode_format"], "wav")
        self.assertEqual(payload["model"], "hy-asr-3.0-preview")
        self.assertEqual(payload["data"], self.audio)
        self.assertNotIn("input_url", payload)

    def test_speech_rejects_incomplete_or_fabricated_response(self):
        for response in ({"status": "failed", "output": {"text": "假成功"}},
                         {"status": "completed", "output": {"text": ""}},
                         {"status": "completed", "text": "字段位置不符"}):
            with self.subTest(response=response), patch.object(self.gateway, "_model_request", return_value=response):
                with self.assertRaises(DomainError) as raised:
                    self.gateway.speech({"mime": "audio/wav", "audio_base64": self.audio,
                                         "consent": True, "paid_call_confirmed": True})
                self.assertEqual(raised.exception.code, "TRANSCRIPTION_INVALID")

    def test_lyrics_request_is_structured_and_response_strict(self):
        work = {"story": "今天下雨了", "intent": {"preserve": "别忘了雨", "avoid": "鸡汤"},
                "lyrics": [], "current_lyric_id": None}
        response = {"choices": [{"message": {"content": json.dumps({"lines": [{"text": "雨落在窗边"}, {"text": "别忘了雨"}]})}}]}
        with patch.object(self.gateway, "_model_request", return_value=response) as request:
            self.assertEqual(self.gateway.lyrics(work, "写两句", None)["lines"][0]["text"], "雨落在窗边")
        path, payload = request.call_args.args
        self.assertEqual(path, "/v1/chat/completions")
        self.assertFalse(payload["stream"])
        self.assertIn("别忘了雨", payload["messages"][1]["content"])
        for invalid in ({"choices": []}, {"choices": [{"message": {"content": "```json\n{}\n```"}}]}):
            with patch.object(self.gateway, "_model_request", return_value=invalid):
                with self.assertRaises(DomainError) as raised:
                    self.gateway.lyrics(work, "写两句", None)
                self.assertEqual(raised.exception.code, "PROVIDER_INVALID")


if __name__ == "__main__":
    unittest.main()
