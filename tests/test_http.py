import base64
import copy
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection
from http.cookies import SimpleCookie
import io
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server
from backend.domain import DomainError, Store
from test_providers import FakeGateway, ready_result, test_wav, work_body
from backend.providers import TokenHubPreviewGateway


class HTTPTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        network = patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("禁止外部网络访问"))
        network.start()
        self.addCleanup(network.stop)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.gateway = FakeGateway()
        self.server = server.make_server(0, self.directory.name, self.gateway)
        self.app = self.server.app
        self.futures = []
        submit = self.app.pool.submit
        def record_submit(*args, **kwargs):
            future = submit(*args, **kwargs)
            self.futures.append(future)
            return future
        capture = patch.object(self.app.pool, "submit", side_effect=record_submit)
        capture.start()
        self.addCleanup(capture.stop)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.session = self.new_session()

    def close_server(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        # 兼容修复前版本，失败测试也不能泄漏执行器线程。
        self.app.close()
        self.assertFalse(self.thread.is_alive())

    def request(self, method, path, body=None, auth=None, headers=None, raw=None):
        auth = self.session if auth is None and hasattr(self, "session") else auth
        supplied = {"Origin": "http://127.0.0.1:" + str(self.server.server_port)}
        if auth:
            supplied.update({"Cookie": auth["cookie"], "X-CSRF-Token": auth["csrf"]})
        if body is not None:
            raw = json.dumps(body).encode()
        if raw is not None:
            supplied["Content-Type"] = "application/json"
        supplied.update(headers or {})
        supplied = {key: value for key, value in supplied.items() if value is not None}
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            connection.request(method, path, body=raw, headers=supplied)
            response = connection.getresponse()
            data = response.read()
            metadata = dict(response.getheaders())
            if data and metadata.get("Content-Type", "").startswith("application/json"):
                data = json.loads(data)
            return response.status, metadata, data
        finally:
            connection.close()

    def ok(self, status, method, path, body=None, **kwargs):
        actual, headers, data = self.request(method, path, body, **kwargs)
        self.assertEqual(actual, status, data)
        return data

    def error(self, status, code, method, path, body=None, **kwargs):
        actual, headers, data = self.request(method, path, body, **kwargs)
        self.assertEqual(actual, status, data)
        self.assertEqual(data["error"]["code"], code)
        self.assertEqual(headers["X-Request-ID"], data["request_id"])
        self.assertEqual(headers["Cache-Control"], "no-store")
        return data

    def new_session(self):
        status, headers, body = self.request("POST", "/api/session", {}, auth=False)
        self.assertEqual(status, 200)
        cookie = SimpleCookie(headers["Set-Cookie"])["syp_session"].value
        return {"cookie": "syp_session=" + cookie, "token": cookie, "csrf": body["csrf"], "headers": headers}

    def work(self, **changes):
        return self.ok(201, "POST", "/api/works", work_body(**changes))

    def lyrics(self, work=None, **changes):
        work = work or self.work()
        body = {"base_version": work["version"], "source": "manual",
                "lines": [{"text": "测试第一句。"}, {"text": "测试第二句。"}]}
        body.update(changes)
        return self.ok(201, "POST", f'/api/works/{work["id"]}/lyrics', body)

    def lines(self, work):
        return copy.deepcopy(work["lyrics"][-1]["lines"])

    def job_body(self, work, key="test-job"):
        return {"base_version": work["version"], "lyric_id": work["current_lyric_id"],
                "idempotency_key": key, "confirmed": True}

    def job(self, work, key="test-job"):
        return self.ok(202, "POST", f'/api/works/{work["id"]}/jobs', self.job_body(work, key))

    def finish(self):
        for future in self.futures:
            future.result(timeout=5)
        self.assertEqual(self.app.jobs, set())

    def audio(self, work=None):
        work = work or self.lyrics()
        job = self.job(work)
        self.finish()
        done = self.ok(200, "GET", f'/api/jobs/{job["id"]}')
        self.assertEqual(done["status"], "ready", done)
        detail = self.ok(200, "GET", f'/api/works/{work["id"]}')
        return detail, detail["audios"][-1], done

    def share(self, work, audio, **changes):
        body = {"confirm": True, "audio_id": audio["id"], "show_lyrics": False}
        body.update(changes)
        return self.ok(201, "POST", f'/api/works/{work["id"]}/shares', body)

    def clip_body(self, data=None, **changes):
        body = {"role": "story", "source": "upload", "mime": "audio/wav",
                "audio_base64": base64.b64encode(data if data is not None else test_wav(duration=1)).decode(),
                "confirm": True}
        body.update(changes)
        return body

    def candidate(self, work, **changes):
        body = {"base_version": work["version"], "instruction": "仅用于测试"}
        body.update(changes)
        return f'/api/works/{work["id"]}/lyrics/generate', body

    def test_session_cookie_security_and_reuse(self):
        cookie = SimpleCookie(self.session["headers"]["Set-Cookie"])["syp_session"]
        self.assertTrue(cookie["httponly"])
        self.assertEqual(cookie["samesite"], "Strict")
        self.assertEqual(cookie["path"], "/")
        self.assertEqual(cookie["max-age"], "2592000")
        status, headers, body = self.request("POST", "/api/session", {})
        self.assertEqual(status, 200)
        self.assertEqual(body["csrf"], self.session["csrf"])
        self.assertNotIn("Set-Cookie", headers)
        self.assertNotIn(self.session["token"], json.dumps(body))
        self.assertNotEqual(self.new_session()["csrf"], self.session["csrf"])

    def test_session_required_for_private_endpoints(self):
        for cookie in (None, "syp_session=invalid", "syp_session=" + "a" * 43):
            self.error(401, "SESSION_REQUIRED", "GET", "/api/works", auth=False, headers={"Cookie": cookie})
        self.error(401, "SESSION_REQUIRED", "POST", "/api/works", work_body(), auth=False)

    def test_csrf_required_and_scoped_to_session(self):
        other = self.new_session()
        for token in (None, "wrong", other["csrf"], "é"):
            with self.subTest(token=token):
                self.error(403, "CSRF_FAILED", "POST", "/api/works", work_body(), headers={"X-CSRF-Token": token})
        self.assertEqual(self.ok(200, "GET", "/api/works"), [])

    def test_host_and_origin_reject_cross_site(self):
        for host in ("example.invalid", "127.0.0.1", "localhost:1", "127.0.0.1:1"):
            self.error(403, "HOST_DENIED", "GET", "/api/health", headers={"Host": host})
        for origin in ("null", "https://example.invalid", "http://localhost:1"):
            self.error(403, "ORIGIN_DENIED", "POST", "/api/session", {}, headers={"Origin": origin})
        self.error(403, "ORIGIN_DENIED", "POST", "/api/session", {}, headers={"Sec-Fetch-Site": "cross-site"})
        self.assertTrue(self.ok(200, "GET", "/api/health", headers={"Host": f"localhost:{self.server.server_port}"})["ok"])

    def test_duplicate_host_or_origin_headers_are_rejected(self):
        for header, value, code in (("Host", "example.invalid", "HOST_DENIED"),
                                    ("Origin", "https://example.invalid", "ORIGIN_DENIED")):
            with self.subTest(header=header):
                connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
                try:
                    connection.putrequest("POST", "/api/session")
                    connection.putheader("Origin", f"http://127.0.0.1:{self.server.server_port}")
                    connection.putheader(header, value)
                    connection.putheader("Content-Length", "0")
                    connection.endheaders()
                    response = connection.getresponse()
                    body = json.loads(response.read())
                    self.assertEqual(response.status, 403, body)
                    self.assertEqual(body["error"]["code"], code)
                finally:
                    connection.close()

    def test_health_capabilities_and_security_headers(self):
        status, headers, body = self.request("GET", "/api/health", auth=False)
        self.assertEqual(status, 200)
        self.assertTrue(body["local_only"])
        for key in ("Content-Security-Policy", "X-Content-Type-Options", "Referrer-Policy", "Permissions-Policy"):
            self.assertIn(key, headers)
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["Referrer-Policy"], "no-referrer")
        self.assertNotIn("Access-Control-Allow-Origin", headers)
        capabilities = self.ok(200, "GET", "/api/capabilities", auth=False)
        self.assertTrue(capabilities["manual_lyrics"])
        self.assertFalse(capabilities["production_ready"])
        self.assertTrue(capabilities["same_device_only"])
        self.assertFalse(capabilities["local_daily_song_limit"])
        self.assertNotIn("daily_song_quota", capabilities)

    def test_head_returns_headers_without_body(self):
        _, get_headers, _ = self.request("GET", "/api/health")
        status, headers, data = self.request("HEAD", "/api/health")
        self.assertEqual((status, data), (200, b""))
        self.assertEqual(headers["Content-Length"], get_headers["Content-Length"])

    def test_work_crud_and_version_conflict(self):
        work = self.work()
        path = f'/api/works/{work["id"]}'
        self.assertEqual(self.ok(200, "GET", path), work)
        changed = self.ok(200, "PATCH", path, {"base_version": 1, "title": "修改标题"})
        self.assertEqual(changed["version"], 2)
        self.error(409, "VERSION_CONFLICT", "PATCH", path, {"base_version": 1, "title": "旧版本"})
        self.assertEqual(self.ok(200, "GET", "/api/works")[0]["title"], "修改标题")
        self.error(422, "CONFIRM_REQUIRED", "DELETE", path, {"base_version": 2})
        self.ok(200, "DELETE", path, {"base_version": 2, "confirm": True})
        self.error(404, "NOT_FOUND", "GET", path)
        self.assertEqual(self.ok(200, "GET", "/api/works"), [])

    def test_owner_isolation_for_crud_lyrics_jobs_audio_and_shares(self):
        work, audio, job = self.audio()
        share = self.share(work, audio)
        other = self.new_session()
        path = f'/api/works/{work["id"]}'
        calls = [("GET", path, None), ("PATCH", path, {"base_version": work["version"], "title": "越权"}),
                 ("DELETE", path, {"base_version": work["version"], "confirm": True}),
                 ("POST", path + "/lyrics", {"base_version": work["version"], "lines": self.lines(work)}),
                 ("POST", path + "/lyrics/restore", {"base_version": work["version"], "lyric_id": work["current_lyric_id"]}),
                 ("POST", *self.candidate(work)), ("POST", path + "/jobs", self.job_body(work, "other")),
                 ("GET", f'/api/jobs/{job["id"]}', None), ("POST", f'/api/jobs/{job["id"]}/cancel', {}),
                 ("GET", f'/api/audio/{audio["id"]}', None), ("POST", path + f'/audios/{audio["id"]}/keep', {}),
                 ("POST", path + "/shares", {"confirm": True, "audio_id": audio["id"]}),
                 ("POST", path + f'/shares/{share["id"]}/revoke', {})]
        for method, endpoint, body in calls:
            with self.subTest(endpoint=endpoint, method=method):
                self.error(404, "NOT_FOUND", method, endpoint, body, auth=other)
        self.assertEqual(self.ok(200, "GET", "/api/works", auth=other), [])
        self.assertEqual(self.ok(200, "GET", path)["title"], work["title"])

    def test_locked_line_and_span_cannot_be_changed_or_unlocked_with_edit(self):
        work = self.lyrics(lines=[{"text": "测试原句。", "locked": True},
                                  {"text": "保留片段和其它文字。", "locked_spans": ["保留片段"]}])
        for change in ({"text": "测试原句！"}, {"locked": False, "text": "改句。"}):
            lines = self.lines(work)
            lines[0].update(change)
            self.error(422, "LOCK_CONFLICT", "POST", f'/api/works/{work["id"]}/lyrics',
                       {"base_version": work["version"], "lines": lines})
        lines = self.lines(work)
        lines[1]["text"] = "删除了片段。"
        self.error(422, "LOCK_CONFLICT", "POST", f'/api/works/{work["id"]}/lyrics',
                   {"base_version": work["version"], "lines": lines})
        self.assertEqual(self.ok(200, "GET", f'/api/works/{work["id"]}'), work)

    def test_sample_save_is_not_ai_and_does_not_generate_audio(self):
        self.gateway.features.clear()
        work = self.lyrics(source="sample")
        self.assertEqual(work["lyrics"][-1]["source"], "sample")
        self.assertEqual(work["status"], "lyrics_ready")
        self.assertEqual(work["jobs"], [])
        self.assertEqual(work["audios"], [])
        self.assertEqual(self.gateway.calls, [])
        self.assertEqual(list(self.app.assets.iterdir()), [])

    def test_restore_keeps_history_and_source(self):
        first = self.lyrics(source="sample")
        lines = self.lines(first)
        lines[0]["text"] = "测试改写。"
        changed = self.lyrics(first, lines=lines)
        restored = self.ok(200, "POST", f'/api/works/{first["id"]}/lyrics/restore',
                           {"base_version": changed["version"], "lyric_id": first["current_lyric_id"]})
        self.assertEqual(len(restored["lyrics"]), 3)
        self.assertEqual(self.lines(restored), self.lines(first))
        self.assertEqual(restored["lyrics"][-1]["source"], "sample")

    def test_unconfigured_returns_503_without_jobs_quota_or_provider_calls(self):
        self.gateway.features.clear()
        work = self.lyrics()
        for method, path, body in (("POST", f'/api/works/{work["id"]}/jobs', self.job_body(work)),
                                   ("POST", *self.candidate(work)), ("POST", "/api/transcribe", {})):
            self.error(503, "PROVIDER_NOT_CONFIGURED", method, path, body)
        self.assertEqual(self.ok(200, "GET", f'/api/works/{work["id"]}'), work)
        with self.app.store._transaction() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM quota_usage").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0)
        self.assertEqual(self.gateway.calls, [])

    def test_worker_receives_owner_and_idempotent_request_runs_once(self):
        work = self.lyrics()
        owner = self.app.store.session(self.session["token"])["owner_id"]
        with patch.object(self.gateway, "run_song", wraps=self.gateway.run_song) as worker:
            first = self.job(work)
            self.finish()
            second = self.job(work)
            self.finish()
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(worker.call_count, 1)
        self.assertEqual(worker.call_args.args[1], owner)
        self.assertEqual(second["status"], "ready")

    def test_status_tracks_worker_progress_and_ready_without_content_revision(self):
        work = self.lyrics()
        arrived, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        def respond(method, path, payload, timeout):
            if method == "POST":
                arrived.set()
                if not release.wait(5):
                    raise TimeoutError("测试未释放网关")
                return {"job_id": "remote"}
            return ready_result()
        self.gateway.handler = respond
        job = self.job(work)
        self.assertEqual(job["status"], "queued")
        self.assertTrue(arrived.wait(5))
        for path in (f'/api/works/{work["id"]}', "/api/works"):
            state = self.ok(200, "GET", path)
            state = state[0] if isinstance(state, list) else state
            self.assertEqual(state["status"], "generating")
            self.assertEqual(state["version"], work["version"])
        release.set()
        self.finish()
        self.assertEqual(self.ok(200, "GET", f'/api/jobs/{job["id"]}')["status"], "ready")
        self.assertEqual(self.ok(200, "GET", "/api/works")[0]["status"], "ready")

    def test_checking_status_is_visible_before_audio_commit(self):
        work = self.lyrics()
        checking, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        complete = self.app.store.complete_job
        def paused_complete(*args):
            checking.set()
            if not release.wait(5):
                raise TimeoutError("检查阶段测试未释放")
            return complete(*args)
        with patch.object(self.app.store, "complete_job", side_effect=paused_complete):
            job = self.job(work)
            self.assertTrue(checking.wait(5))
            self.assertEqual(self.ok(200, "GET", f'/api/jobs/{job["id"]}')["status"], "checking")
            self.assertEqual(self.ok(200, "GET", "/api/works")[0]["status"], "checking")
            detail = self.ok(200, "GET", f'/api/works/{work["id"]}')
            self.assertEqual((detail["status"], detail["audios"]), ("checking", []))
            release.set()
            self.finish()
        self.assertEqual(self.ok(200, "GET", f'/api/jobs/{job["id"]}')["status"], "ready")

    def test_worker_invalid_audio_exposes_failure_code_and_preserves_lyrics(self):
        self.gateway.song_result = ready_result(wav_base64="invalid")
        work = self.lyrics()
        job = self.job(work)
        self.finish()
        done = self.ok(200, "GET", f'/api/jobs/{job["id"]}')
        self.assertEqual(done["status"], "failed")
        self.assertEqual(done["error"]["code"], "AUDIO_INVALID")
        detail = self.ok(200, "GET", f'/api/works/{work["id"]}')
        self.assertEqual(detail["status"], "failed")
        self.assertEqual(detail["lyrics"], work["lyrics"])
        self.assertEqual(detail["audios"], [])
        self.assertEqual(list(self.app.assets.iterdir()), [])

    def test_cancel_and_delete_while_worker_waits_never_revive(self):
        for action in ("cancel", "delete"):
            with self.subTest(action=action):
                work = self.lyrics()
                arrived, release = threading.Event(), threading.Event()
                self.addCleanup(release.set)
                def respond(method, path, payload, timeout):
                    if method == "POST":
                        return {"job_id": "remote"}
                    arrived.set()
                    if not release.wait(5):
                        raise TimeoutError("测试未释放网关")
                    return ready_result()
                self.gateway.handler = respond
                job = self.job(work, action)
                self.assertTrue(arrived.wait(5))
                if action == "cancel":
                    self.ok(200, "POST", f'/api/jobs/{job["id"]}/cancel', {})
                else:
                    self.ok(200, "DELETE", f'/api/works/{work["id"]}', {"base_version": work["version"], "confirm": True})
                release.set()
                self.finish()
                if action == "cancel":
                    self.assertEqual(self.ok(200, "GET", f'/api/jobs/{job["id"]}')["status"], "cancelled")
                    self.assertEqual(self.ok(200, "GET", f'/api/works/{work["id"]}')["audios"], [])
                else:
                    self.error(404, "NOT_FOUND", "GET", f'/api/jobs/{job["id"]}')
                    self.error(404, "NOT_FOUND", "GET", f'/api/works/{work["id"]}')
                self.assertEqual(list(self.app.assets.iterdir()), [])

    def test_share_title_and_lyrics_snapshot_are_private_by_default_and_revocable(self):
        work, audio, _ = self.audio()
        share = self.share(work, audio)
        path = f'/api/public/{share["token"]}'
        original = self.ok(200, "GET", path, auth=False)
        self.assertEqual(set(original["audio"]), {"id", "mime", "duration"})
        encoded = json.dumps(original, ensure_ascii=False)
        for private in (work["story"], audio["asset_name"], work["id"], "locked_spans", "intent"):
            self.assertNotIn(private, encoded)
        renamed = self.ok(200, "PATCH", f'/api/works/{work["id"]}',
                          {"base_version": work["version"], "title": "分享之后的私密标题"})
        self.assertEqual(self.ok(200, "GET", path, auth=False), original)
        self.assertEqual(Store(self.app.store.db_path).public_share(share["token"]), original)
        show = self.share(renamed, audio, show_lyrics=True)
        public = self.ok(200, "GET", f'/api/public/{show["token"]}', auth=False)
        self.assertEqual(public["audio"]["lyrics"], [{"text": line["text"]} for line in self.lines(work)])
        self.assertEqual(self.ok(200, "GET", path + "/audio", auth=False), test_wav())
        self.ok(200, "POST", f'/api/works/{work["id"]}/shares/{share["id"]}/revoke', {})
        self.error(404, "NOT_FOUND", "GET", path, auth=False)
        self.error(404, "NOT_FOUND", "GET", path + "/audio", auth=False)

    def test_media_requires_owner_and_export_permission(self):
        work, audio, _ = self.audio()
        path = f'/api/audio/{audio["id"]}'
        self.error(401, "SESSION_REQUIRED", "GET", path, auth=False)
        self.error(404, "NOT_FOUND", "GET", path, auth=self.new_session())
        self.error(403, "EXPORT_NOT_LICENSED", "GET", path + "?download=1")
        self.assertEqual(self.ok(200, "GET", path), test_wav())
        kept = self.ok(200, "POST", f'/api/works/{work["id"]}/audios/{audio["id"]}/keep', {})
        self.assertTrue(kept["audios"][0]["kept"])

    def test_input_clip_create_replace_delete_and_private_range_preview(self):
        work = self.work()
        endpoint = f'/api/works/{work["id"]}/input-clips'
        original = test_wav(duration=1)
        clip = self.ok(201, "POST", endpoint, self.clip_body(original))
        self.assertEqual(set(clip), {"id", "role", "source", "mime", "duration", "created_at"})
        self.assertEqual((clip["role"], clip["source"], clip["mime"], clip["duration"]),
                         ("story", "upload", "audio/wav", 1))
        preview = f'/api/input-clips/{clip["id"]}/audio'
        status, headers, data = self.request("GET", preview, headers={"Range": "bytes=0-9"})
        self.assertEqual((status, data, headers["Content-Range"]), (206, original[:10], f"bytes 0-9/{len(original)}"))
        self.assertEqual((headers["Content-Type"], headers["Accept-Ranges"]), ("audio/wav", "bytes"))
        self.assertEqual(self.request("HEAD", preview, headers={"Range": "bytes=-5"})[0], 206)
        self.assertEqual(self.ok(200, "GET", preview), original)
        status, headers, error = self.request("GET", preview, headers={"Range": "bytes=999999-"})
        self.assertEqual((status, error["error"]["code"], headers["Content-Range"]),
                         (416, "INVALID_RANGE", f"bytes */{len(original)}"))
        recorded = self.ok(201, "POST", endpoint, self.clip_body(role="instruction", source="record"))
        replacement = self.ok(201, "POST", endpoint, self.clip_body(test_wav(duration=2), source="record"))
        self.error(404, "NOT_FOUND", "GET", preview)
        detail = self.ok(200, "GET", f'/api/works/{work["id"]}')
        self.assertEqual({item["id"] for item in detail["input_clips"]}, {recorded["id"], replacement["id"]})
        self.assertEqual((detail["version"], detail["updated_at"], detail["audios"], detail["shares"]),
                         (work["version"], work["updated_at"], [], []))
        self.assertNotIn("audio_base64", json.dumps(detail))
        self.assertEqual(self.ok(200, "DELETE", f'/api/input-clips/{replacement["id"]}', {"confirm":True}), {"deleted": True})
        self.error(404, "NOT_FOUND", "GET", f'/api/input-clips/{replacement["id"]}/audio')
        self.assertEqual(len(self.ok(200, "GET", f'/api/works/{work["id"]}')["input_clips"]), 1)
        self.assertEqual(list(self.app.assets.iterdir()), [])
        self.assertEqual(self.gateway.calls, [])

    def test_input_clips_require_owner_and_csrf_and_work_deletion_purges_blob(self):
        work = self.work()
        endpoint = f'/api/works/{work["id"]}/input-clips'
        other = self.new_session()
        body = self.clip_body()
        self.error(404, "NOT_FOUND", "POST", endpoint, body, auth=other)
        for token in (None, other["csrf"]):
            self.error(403, "CSRF_FAILED", "POST", endpoint, body,
                       headers={"X-CSRF-Token": token})
        clip = self.ok(201, "POST", endpoint, body)
        preview = f'/api/input-clips/{clip["id"]}/audio'
        self.error(401, "SESSION_REQUIRED", "GET", preview, auth=False)
        self.error(404, "NOT_FOUND", "GET", preview, auth=other)
        self.error(404, "NOT_FOUND", "DELETE", f'/api/input-clips/{clip["id"]}', auth=other)
        self.error(403, "CSRF_FAILED", "DELETE", f'/api/input-clips/{clip["id"]}',
                   headers={"X-CSRF-Token": None})
        self.error(404, "NOT_FOUND", "POST", "/api/works/missing/input-clips", body)
        self.ok(200, "DELETE", f'/api/works/{work["id"]}',
                {"base_version": work["version"], "confirm": True})
        self.error(404, "NOT_FOUND", "GET", preview)
        with self.app.store._transaction() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM input_clips").fetchone()[0], 0)

    def test_input_clip_rejects_fake_mime_oversize_and_long_audio(self):
        work = self.work()
        endpoint = f'/api/works/{work["id"]}/input-clips'
        for body, status, code in (
            (self.clip_body(mime="audio/mp4"), 422, "AUDIO_INVALID"),
            (self.clip_body(mime="audio/mpeg"), 422, "AUDIO_INVALID"),
            (self.clip_body(mime="video/mp4"), 422, "VALIDATION_ERROR"),
            (self.clip_body(confirm="true"), 422, "CONFIRM_REQUIRED"),
            (self.clip_body(test_wav(duration=61)), 422, "AUDIO_DURATION"),
            (self.clip_body(b"x" * 99), 413, "AUDIO_SIZE"),
        ):
            with self.subTest(status=status, code=code):
                self.error(status, code, "POST", endpoint, body)
        self.error(413, "BODY_TOO_LARGE", "POST", endpoint, raw=b"{}",
                   headers={"Content-Length": str(9 * 1024 * 1024 + 1)})
        with self.app.store._transaction() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM input_clips").fetchone()[0], 0)

    def test_input_clip_accepts_exact_six_mib_with_json_under_nine_mib(self):
        work = self.work()
        output = io.BytesIO()
        with wave.open(output, "wb") as sample:
            sample.setnchannels(2)
            sample.setsampwidth(2)
            sample.setframerate(48000)
            sample.writeframes(b"\0" * (6 * 1024 * 1024 - 44))
        audio = output.getvalue()
        self.assertEqual(len(audio), 6 * 1024 * 1024)
        clip = self.ok(201, "POST", f'/api/works/{work["id"]}/input-clips', self.clip_body(audio))
        self.assertTrue(0 < clip["duration"] < 60)
        self.assertEqual(self.ok(200, "GET", f'/api/input-clips/{clip["id"]}/audio'), audio)

    def test_licensed_download_has_attachment_header(self):
        self.gateway.song_result["export_allowed"] = True
        _, audio, _ = self.audio()
        status, headers, data = self.request("GET", f'/api/audio/{audio["id"]}?download=1')
        self.assertEqual((status, data), (200, test_wav()))
        self.assertIn("attachment", headers["Content-Disposition"])
        self.assertEqual(headers["Content-Type"], "audio/wav")
        self.assertEqual(self.ok(200,'GET','/api/internal/diagnostics')['behavior']['exports_sent'],1)
        self.request('HEAD',f'/api/audio/{audio["id"]}?download=1')
        self.assertEqual(self.ok(200,'GET','/api/internal/diagnostics')['behavior']['exports_sent'],1)

    def test_media_ranges_closed_open_suffix_and_head(self):
        _, audio, _ = self.audio()
        data = test_wav()
        path = f'/api/audio/{audio["id"]}'
        for value, start, end in (("bytes=0-9", 0, 9), ("bytes=10-", 10, len(data) - 1),
                                  ("bytes=-10", len(data) - 10, len(data) - 1),
                                  ("bytes=0-99999999", 0, len(data) - 1)):
            with self.subTest(value=value):
                status, headers, content = self.request("GET", path, headers={"Range": value})
                self.assertEqual((status, content), (206, data[start:end + 1]))
                self.assertEqual(headers["Content-Range"], f"bytes {start}-{end}/{len(data)}")
                self.assertEqual(headers["Accept-Ranges"], "bytes")
        status, headers, content = self.request("HEAD", path, headers={"Range": "bytes=0-9"})
        self.assertEqual((status, content, headers["Content-Length"]), (206, b"", "10"))

    def test_invalid_ranges_return_416_with_total_size(self):
        _, audio, _ = self.audio()
        for value in ("bytes=", "bytes=-", "bytes=-0", "bytes=8-2", "bytes=99999999-", "items=0-1", "bytes=0-1,3-4"):
            with self.subTest(value=value):
                status, headers, content = self.request("GET", f'/api/audio/{audio["id"]}', headers={"Range": value})
                self.assertEqual(status, 416, content)
                self.assertEqual(content["error"]["code"], "INVALID_RANGE")
                self.assertEqual(headers.get("Content-Range"), f"bytes */{len(test_wav())}")

    def test_private_files_and_path_traversal_are_not_static_routes(self):
        _, audio, _ = self.audio()
        for path in ("/server.py", "/backend/domain.py", "/data/shuoyipai.sqlite3", "/shuoyipai.sqlite3",
                     "/assets/" + audio["asset_name"], "/data/assets/" + audio["asset_name"],
                     "/../server.py", "/%2e%2e/server.py", "/web/../server.py", "/.env"):
            with self.subTest(path=path):
                self.error(404, "NOT_FOUND", "GET", path, auth=False)
        # 当前没有前端文件时，静态首页404是预期行为。
        if not (server.ROOT / "web" / "index.html").exists():
            self.error(404, "NOT_FOUND", "GET", "/", auth=False)

    def test_media_symlink_cannot_escape_private_assets_directory(self):
        _, audio, _ = self.audio()
        asset = self.app.assets / audio["asset_name"]
        asset.unlink()
        private = Path(self.directory.name) / "private.bin"
        private.write_bytes(b"private-test-data")
        asset.symlink_to(private)
        self.error(404, "ASSET_MISSING", "GET", f'/api/audio/{audio["id"]}')

    def test_delete_revokes_share_and_removes_owned_media_only(self):
        work, audio, _ = self.audio()
        share = self.share(work, audio)
        other = self.app.assets / "unrelated-test.bin"
        other.write_bytes(b"not-owned-by-work")
        self.ok(200, "DELETE", f'/api/works/{work["id"]}', {"base_version": work["version"], "confirm": True})
        self.assertFalse((self.app.assets / audio["asset_name"]).exists())
        self.assertTrue(other.exists())
        self.error(404, "NOT_FOUND", "GET", f'/api/public/{share["token"]}', auth=False)
        self.error(404, "NOT_FOUND", "GET", f'/api/audio/{audio["id"]}')

    def test_delete_cleans_audio_completed_after_initial_work_read(self):
        work = self.lyrics()
        owner = self.app.store.session(self.session["token"])["owner_id"]
        job = self.app.store.create_job(owner, work["id"], self.job_body(work), True)
        delete = self.app.store.delete_work
        def finish_then_delete(*args):
            self.gateway.run_song(self.app.store, owner, job, self.app.assets)
            return delete(*args)
        with patch.object(self.app.store, "delete_work", side_effect=finish_then_delete):
            self.ok(200, "DELETE", f'/api/works/{work["id"]}', {"base_version": work["version"], "confirm": True})
        self.assertEqual(list(self.app.assets.iterdir()), [])

    def test_delete_serializes_new_job_between_snapshot_and_tombstone(self):
        work = self.lyrics()
        owner = self.app.store.session(self.session["token"])["owner_id"]
        snapshot_read, release = threading.Event(), threading.Event()
        attempted, completed = threading.Event(), threading.Event()
        delete = self.app.store.delete_work
        def paused_delete(*args):
            snapshot_read.set()
            if not release.wait(5):
                raise TimeoutError("删除测试未释放")
            return delete(*args)
        def create_and_finish():
            attempted.set()
            try:
                job = self.app.song(owner, work["id"], self.job_body(work))
                self.finish()
                return job
            except DomainError as exc:
                return exc
            finally:
                completed.set()
        with patch.object(self.app.store, "delete_work", side_effect=paused_delete), ThreadPoolExecutor(2) as pool:
            deletion = pool.submit(self.request, "DELETE", f'/api/works/{work["id"]}',
                                   {"base_version": work["version"], "confirm": True})
            try:
                self.assertTrue(snapshot_read.wait(5))
                creation = pool.submit(create_and_finish)
                self.assertTrue(attempted.wait(5))
                self.assertFalse(completed.wait(0.05), "删除事务期间不能新建并完成任务")
            finally:
                release.set()
            self.assertEqual(deletion.result(5)[0], 200)
            result = creation.result(5)
            self.assertIsInstance(result, DomainError)
            self.assertEqual(result.status, 404)
        self.assertEqual(list(self.app.assets.iterdir()), [])
        self.assertEqual(self.gateway.calls, [])

    def test_validation_errors_never_create_partial_works(self):
        for changes, code in (({"story": ""}, "VALIDATION_ERROR"), ({"story": "字" * 501}, "VALIDATION_ERROR"),
                              ({"story": "\ud800"}, "VALIDATION_ERROR"), ({"intent": []}, "VALIDATION_ERROR"),
                              ({"style": []}, "VALIDATION_ERROR"), ({"service_consent": 1}, "CONSENT_REQUIRED")):
            self.error(422, code, "POST", "/api/works", work_body(**changes))
        self.assertEqual(self.ok(200, "GET", "/api/works"), [])

    def test_bad_json_and_non_object_json_are_400(self):
        for raw in (b"{", b"[]", b"null", b"true", b"\xff", b'{"x": NaN}', b'{"x": Infinity}'):
            with self.subTest(raw=raw):
                self.error(400, "INVALID_JSON", "POST", "/api/session", raw=raw)

    def test_invalid_content_type_length_transfer_encoding_and_oversize(self):
        cases = [({"Content-Type": "text/plain"}, 415, "CONTENT_TYPE"),
                 ({"Content-Length": "bad"}, 400, "INVALID_BODY"),
                 ({"Transfer-Encoding": "chunked"}, 400, "INVALID_BODY"),
                 ({"Content-Length": str(64 * 1024 + 1)}, 413, "BODY_TOO_LARGE")]
        for headers, status, code in cases:
            self.error(status, code, "POST", "/api/session", raw=b"{}", headers=headers)

    def test_duplicate_content_length_is_rejected_before_reading(self):
        connection = HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            connection.putrequest("POST", "/api/session")
            connection.putheader("Content-Type", "application/json")
            connection.putheader("Content-Length", "0")
            connection.putheader("Content-Length", "2")
            connection.endheaders(b"{}")
            response = connection.getresponse()
            self.assertEqual(response.status, 400)
            self.assertEqual(json.loads(response.read())["error"]["code"], "INVALID_BODY")
        finally:
            connection.close()

    def test_rejected_body_does_not_become_keepalive_request(self):
        port = self.server.server_port
        trailing = f"GET /api/health HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode()
        request = (f"POST /api/works HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
                   f"Cookie: {self.session['cookie']}\r\nContent-Length: {len(trailing)}\r\n"
                   "Content-Type: application/json\r\n\r\n").encode() + trailing
        with socket.create_connection(("127.0.0.1", port), timeout=5) as connection:
            connection.sendall(request)
            chunks = []
            while data := connection.recv(65536):
                chunks.append(data)
        response = b"".join(chunks)
        self.assertEqual(response.count(b"HTTP/1.1"), 1)
        self.assertIn(b"403", response)

    def test_ai_candidate_requires_confirmation_and_does_not_save(self):
        work = self.work()
        result = self.ok(200, "POST", *self.candidate(work))
        self.assertTrue(result["requires_confirmation"])
        self.assertEqual(result["source"], "ai")
        self.assertTrue(all("id" not in line for line in result["lines"]))
        self.assertEqual(self.ok(200, "GET", f'/api/works/{work["id"]}'), work)
        saved = self.lyrics(work, lines=result["lines"], source="ai")
        self.assertEqual(saved["lyrics"][-1]["source"], "ai")

    def test_two_independent_ai_candidates_can_be_combined_by_stable_line_id(self):
        work = self.lyrics()
        lines = self.lines(work)
        a = copy.deepcopy(lines); a[0]["text"] = "候选A第一句。"
        b = copy.deepcopy(lines); b[1]["text"] = "候选B第二句。"
        path, body = self.candidate(work)
        self.gateway.lyric_result = {"lines": a}
        candidate_a = self.ok(200, "POST", path, body)
        self.gateway.lyric_result = {"lines": b}
        candidate_b = self.ok(200, "POST", path, body)
        self.assertEqual(self.ok(200, "GET", f'/api/works/{work["id"]}')["version"], work["version"])
        chosen = [candidate_a["lines"][0], candidate_b["lines"][1]]
        saved = self.ok(201, "POST", f'/api/works/{work["id"]}/lyrics',
            {"base_version": work["version"], "source": "ai", "lines": chosen})
        self.assertEqual([line["text"] for line in saved["lyrics"][-1]["lines"]],
                         ["候选A第一句。", "候选B第二句。"])
        self.assertEqual(saved["lyrics"][-1]["parent_id"], work["current_lyric_id"])
        self.error(409, "VERSION_CONFLICT", "POST", f'/api/works/{work["id"]}/lyrics',
                   {"base_version": work["version"], "source": "ai", "lines": chosen})

    def test_ai_candidate_cannot_bypass_updated_version_on_save(self):
        work = self.work()
        candidate = self.ok(200, "POST", *self.candidate(work))
        changed = self.ok(200, "PATCH", f'/api/works/{work["id"]}', {"base_version": 1, "title": "并发更新"})
        self.error(409, "VERSION_CONFLICT", "POST", f'/api/works/{work["id"]}/lyrics', candidate)
        self.assertEqual(self.ok(200, "GET", f'/api/works/{work["id"]}'), changed)

    def test_ai_candidate_enforces_preserve_and_avoid_intent(self):
        for intent in (dict(work_body()["intent"], preserve="必须保留"),
                       dict(work_body()["intent"], avoid="测试第一句")):
            work = self.work(intent=intent)
            self.error(422, "INTENT_CONFLICT", "POST", *self.candidate(work))
            self.assertEqual(self.ok(200, "GET", f'/api/works/{work["id"]}')["lyrics"], [])

    def test_ai_cannot_change_lock_state_even_without_text_changes(self):
        work = self.lyrics(lines=[{"text": "测试第一句。", "locked": True},
                                  {"text": "测试第二句。", "locked_spans": ["测试"]}])
        for index, change in ((0, {"locked": False}), (1, {"locked_spans": []}), (1, {"locked": True}),
                               (0, {"locked": 1}), (1, {"locked": 0})):
            with self.subTest(change=change):
                self.gateway.lyric_result = {"lines": self.lines(work)}
                self.gateway.lyric_result["lines"][index].update(change)
                self.error(422, "LOCK_CONFLICT", "POST", *self.candidate(work))
        self.assertEqual(self.ok(200, "GET", f'/api/works/{work["id"]}'), work)

    def test_ai_cannot_introduce_locks_on_new_lines(self):
        work = self.work()
        self.gateway.lyric_result["lines"][0]["locked"] = True
        self.error(422, "LOCK_CONFLICT", "POST", *self.candidate(work))

    def test_ai_preserves_omitted_locks_and_locked_text(self):
        work = self.lyrics(lines=[{"text": "测试第一句。", "locked": True}, {"text": "测试第二句。"}])
        self.gateway.lyric_result = {"lines": [{"id": x["id"], "text": x["text"]} for x in self.lines(work)]}
        result = self.ok(200, "POST", *self.candidate(work))
        self.assertTrue(result["lines"][0]["locked"])
        self.gateway.lyric_result["lines"][0]["text"] = "测试第一句！"
        self.error(422, "LOCK_CONFLICT", "POST", *self.candidate(work))

    def test_ai_partial_scope_rejects_other_lines_order_and_count_changes(self):
        work = self.lyrics()
        original = self.lines(work)
        variants = [list(reversed(original)), original + [{"text": "越界新增。"}]]
        changed = self.lines(work)
        changed[1]["text"] = "越界改动。"
        variants.append(changed)
        for lines in variants:
            self.gateway.lyric_result = {"lines": lines}
            self.error(422, "SCOPE_CONFLICT", "POST", *self.candidate(work, line_id=original[0]["id"]))
        original[0]["text"] = "选中句合法修改。"
        self.gateway.lyric_result = {"lines": original}
        result = self.ok(200, "POST", *self.candidate(work, line_id=original[0]["id"]))
        self.assertEqual(result["lines"], original)

    def test_ai_input_types_and_version_are_validated_before_gateway(self):
        work = self.work()
        for changes, code in (({"base_version": True}, "VALIDATION_ERROR"),
                              ({"instruction": " "}, "VALIDATION_ERROR"),
                              ({"instruction": "\ud800"}, "VALIDATION_ERROR"),
                              ({"instruction": []}, "VALIDATION_ERROR"),
                              ({"line_id": {}}, "INVALID_LINE_ID"), ({"line_id": []}, "INVALID_LINE_ID"),
                              ({"line_id": ""}, "INVALID_LINE_ID"), ({"line_id": "unknown"}, "INVALID_LINE_ID")):
            with self.subTest(changes=changes):
                self.error(422, code, "POST", *self.candidate(work, **changes))
        self.error(409, "VERSION_CONFLICT", "POST", *self.candidate(work, base_version=2))
        self.assertEqual(self.gateway.calls, [])

    def test_ai_malformed_response_returns_controlled_error(self):
        work = self.lyrics()
        for result in (None, [], {"lines": None}, {"lines": [None, {}]},
                       {"lines": [{"id": [], "text": "测试"}, {"text": "第二句"}]}):
            with self.subTest(result=result):
                self.gateway.lyric_result = result
                self.error(502, "PROVIDER_INVALID", "POST", *self.candidate(work))

    def test_ai_semaphore_released_on_failure_and_busy_returns_429(self):
        work = self.work()
        with patch.object(self.gateway, "lyrics", side_effect=DomainError(502, "PROVIDER_UNAVAILABLE", "测试失败")):
            self.error(502, "PROVIDER_UNAVAILABLE", "POST", *self.candidate(work))
        self.assertTrue(self.app.ai_slots.acquire(blocking=False))
        self.assertTrue(self.app.ai_slots.acquire(blocking=False))
        try:
            self.error(429, "AI_BUSY", "POST", *self.candidate(work))
        finally:
            self.app.ai_slots.release()
            self.app.ai_slots.release()
        self.ok(200, "POST", *self.candidate(work))

    def test_transcribe_requires_confirmation_and_rejects_bad_mime(self):
        import base64
        body = {"consent": True, "mime": "audio/webm", "audio_base64": base64.b64encode(b"x" * 101).decode()}
        result = self.ok(200, "POST", "/api/transcribe", body)
        self.assertTrue(result["requires_confirmation"])
        self.error(422, "AUDIO_FORMAT", "POST", "/api/transcribe", dict(body, mime=[]))
        self.assertEqual(self.ok(200, "GET", "/api/works"), [])

    def test_events_reject_private_text_and_do_not_claim_verified_metrics(self):
        result = self.ok(200, "POST", "/api/events", {"event_id": "test-event", "name": "create_entry_view"})
        self.assertFalse(result["metrics_verified"])
        self.error(422, "VALIDATION_ERROR", "POST", "/api/events", {"event_id": "private", "name": "create_entry_view",
                                                                  "properties": {"story": "不应保存"}})

    def test_server_close_shuts_executor_and_rejects_new_jobs_without_creation(self):
        work = self.lyrics()
        owner = self.app.store.session(self.session["token"])["owner_id"]
        self.server.shutdown()
        self.thread.join(5)
        self.server.server_close()
        self.assertTrue(self.app.pool._shutdown)
        with self.assertRaises(DomainError) as raised:
            self.app.song(owner, work["id"], self.job_body(work))
        self.assertEqual((raised.exception.status, raised.exception.code), (503, "SERVER_CLOSED"))
        self.assertEqual(self.app.store.get_work(owner, work["id"])["jobs"], [])
        self.app.close()

    def test_submit_failure_does_not_leave_active_job_or_queue_slot(self):
        work = self.lyrics()
        with patch.object(self.app.pool, "submit", side_effect=RuntimeError("executor unavailable")):
            self.error(503, "WORKER_UNAVAILABLE", "POST", f'/api/works/{work["id"]}/jobs', self.job_body(work))
        self.assertEqual(self.app.jobs, set())
        jobs = self.ok(200, "GET", f'/api/works/{work["id"]}')["jobs"]
        self.assertEqual(jobs[0]["status"], "failed")
        self.assertEqual(jobs[0]["error"]["code"], "WORKER_UNAVAILABLE")

    def test_unexpected_worker_exception_is_terminal_and_releases_slot(self):
        work = self.lyrics()
        with patch.object(self.gateway, "run_song", side_effect=RuntimeError("private internal exception")):
            job = self.job(work)
            self.finish()
        done = self.ok(200, "GET", f'/api/jobs/{job["id"]}')
        self.assertEqual(done["status"], "failed")
        self.assertEqual(done["error"]["code"], "GENERATION_FAILED")
        self.assertNotIn("private internal", json.dumps(done))


    def test_diagnostics_context_consent_and_cost_are_private(self):
        self.ok(200,'POST','/api/analytics/context',{'traffic':'test','device':'desktop'})
        self.error(422,'CONSENT_REQUIRED','POST','/api/studies',{'consent':False})
        work=self.work()
        study=self.ok(201,'POST','/api/studies',{'consent':True})
        other=self.new_session()
        self.error(404,'NOT_FOUND','DELETE','/api/studies/'+study['id'],{},auth=other)
        flags=dict(independent_completion=False,recognized_original=False,wanted_revision=False,needed_help=True,work_id=work['id'])
        self.ok(200,'POST','/api/studies/'+study['id'],flags)
        diag=self.ok(200,'GET','/api/internal/diagnostics?traffic=test')
        self.assertEqual(diag['study_summary']['completed'],1)
        self.assertEqual(self.ok(200,'GET','/api/internal/diagnostics')['study_summary']['completed'],0)
        self.assertNotIn(work['story'],json.dumps(diag,ensure_ascii=False))
        self.ok(200,'DELETE','/api/studies/'+study['id'],{})
        self.assertEqual(self.ok(200,'GET','/api/internal/diagnostics?traffic=test')['studies'],[])
        self.error(422,'VALIDATION_ERROR','GET','/api/internal/diagnostics?days=banana')
        self.error(422,'VALIDATION_ERROR','GET','/api/internal/diagnostics?traffic=bad')

    def test_unsupported_stem_edit_never_calls_gateway(self):
        work=self.lyrics();self.gateway.lyrics=Mock()
        self.error(422,'UNSUPPORTED_STEM_EDIT','POST',f"/api/works/{work['id']}/lyrics/generate",{'base_version':work['version'],'instruction':'只改贝斯'})
        self.gateway.lyrics.assert_not_called()
        self.assertEqual(self.ok(200,'GET','/api/internal/diagnostics')['cost']['attempts'],0)

    def test_lyric_success_failure_ledger_and_actual_bill(self):
        work=self.lyrics()
        self.gateway.lyrics=Mock(return_value={'lines':self.lines(work)})
        self.ok(200,'POST',f"/api/works/{work['id']}/lyrics/generate",{'base_version':work['version'],'instruction':'改得自然'})
        self.gateway.lyrics.side_effect=DomainError(502,'PROVIDER_ERROR','供应商错误')
        self.error(502,'PROVIDER_ERROR','POST',f"/api/works/{work['id']}/lyrics/generate",{'base_version':work['version'],'instruction':'再自然一点'})
        diag=self.ok(200,'GET','/api/internal/diagnostics')
        self.assertEqual({c['status'] for c in diag['calls']},{'success','failed'})
        self.assertEqual(diag['cost']['unknown_attempts'],2)
        call=diag['calls'][0]['id'];other=self.new_session()
        body={'confirmed':True,'cost_micros':500,'reference':'测试账单'}
        self.error(404,'NOT_FOUND','POST','/api/internal/calls/'+call+'/cost',body,auth=other)
        self.ok(200,'POST','/api/internal/calls/'+call+'/cost',body)
        self.assertEqual(self.ok(200,'GET','/api/internal/diagnostics')['cost']['known_cost_micros'],500)

    def test_unconfirmed_voice_delete_is_rejected(self):
        work=self.work();clip=self.ok(201,'POST',f"/api/works/{work['id']}/input-clips",{'confirm':True,'role':'story','source':'upload','mime':'audio/wav','audio_base64':base64.b64encode(test_wav()).decode()})
        self.error(422,'CONFIRM_REQUIRED','DELETE','/api/input-clips/'+clip['id'],{})
        self.ok(200,'DELETE','/api/input-clips/'+clip['id'],{'confirm':True})


    def test_original_quote_and_comparison_routes_are_owner_private(self):
        work=self.lyrics(self.work(story='故事原话：测试第一句。'))
        lid=work['lyrics'][0]['lines'][0]['id'];other=self.new_session()
        self.error(404,'NOT_FOUND','GET',f"/api/works/{work['id']}/comparison?line_id={lid}",auth=other)
        self.error(422,'QUOTE_NOT_IN_STORY','POST',f"/api/works/{work['id']}/line-origins",{'base_version':work['version'],'line_id':lid,'quote':'编造原话'})
        updated=self.ok(200,'POST',f"/api/works/{work['id']}/line-origins",{'base_version':work['version'],'line_id':lid,'quote':'测试第一句。'})
        data=self.ok(200,'GET',f"/api/works/{work['id']}/comparison?line_id={lid}")
        self.assertEqual(data['origin']['quote'],'测试第一句。');self.assertEqual(data['first']['number'],1)
        self.assertGreater(updated['version'],work['version'])

    def intro_fixture(self):
        from test_creative import wav
        from test_providers import ready_result
        self.gateway.song_result=ready_result(wav_base64=base64.b64encode(wav(15)).decode(),export_allowed=True)
        work,audio,_=self.audio()
        clip=self.ok(201,'POST',f"/api/works/{work['id']}/input-clips",{'confirm':True,'role':'story','source':'upload','mime':'audio/wav','audio_base64':base64.b64encode(wav(3)).decode()})
        body={'base_version':work['version'],'clip_id':clip['id'],'phrase':'测试第一句','start_seconds':0,'end_seconds':2,'confirm':True,'heard_phrase_confirmed':True,'voice_rights':True,'processing_rights':True,'share_voice':True,'export_voice':True}
        intro=self.ok(201,'POST',f"/api/works/{work['id']}/audios/{audio['id']}/intro",body)
        return work,audio,clip,intro

    def test_diary_http_put_monthly_creation_and_owner_isolation(self):
        self.assertEqual((self.app.data_dir/"shuoyipai.sqlite3").stat().st_mode & 0o777,0o600)
        self.assertEqual(self.app.data_dir.stat().st_mode & 0o777,0o700)
        a=self.ok(200,'PUT','/api/diary/2026-01-01',{'phrase':'别急我在呢','base_version':0})
        b=self.ok(200,'PUT','/api/diary/2026-01-02',{'phrase':'晚风陪我回家','base_version':0})
        self.error(409,'VERSION_CONFLICT','PUT','/api/diary/2026-01-01',{'phrase':'覆盖原话','base_version':0})
        self.error(403,'CSRF_FAILED','PUT','/api/diary/2026-01-03',{'phrase':'没有授权','base_version':0},headers={'X-CSRF-Token':None})
        body={'month':'2026-01','service_consent':True,'entries':[{'day':e['day'],'base_version':e['version']} for e in (a,b)]}
        work=self.ok(201,'POST','/api/diary/monthly',body)
        self.assertEqual(self.ok(201,'POST','/api/diary/monthly',body)['id'],work['id'])
        other=self.new_session()
        self.assertEqual(self.ok(200,'GET','/api/diary?month=2026-01',auth=other)['entries'],[])
        self.error(404,'NOT_FOUND','POST','/api/diary/monthly',body,auth=other)
        self.ok(200,'DELETE','/api/diary/2026-01-02',{'base_version':b['version'],'confirm':True})
        self.assertEqual(len(self.ok(200,'GET','/api/works/'+work['id'])['monthly_diary']['sources']),2)

    def test_delete_intro_source_revokes_share_and_removes_rendered_file(self):
        work,audio,clip,intro=self.intro_fixture();file=self.app.assets/intro['asset_name'];self.assertTrue(file.is_file())
        share=self.share(work,intro);public=self.ok(200,'GET','/api/public/'+share['token'],auth=False)
        self.assertTrue(public['audio']['contains_original_voice']);self.assertNotIn('clip_id',public['audio']);self.assertNotIn('lyrics',public['audio'])
        self.ok(200,'DELETE','/api/input-clips/'+clip['id'],{'confirm':True});self.assertFalse(file.exists())
        self.error(404,'NOT_FOUND','GET','/api/audio/'+intro['id']);self.error(404,'NOT_FOUND','GET','/api/public/'+share['token'],auth=False)
        self.ok(200,'GET','/api/audio/'+audio['id'])

    def test_replacing_intro_source_removes_dependent_audio_and_file(self):
        from test_creative import wav
        work,audio,clip,intro=self.intro_fixture();file=self.app.assets/intro['asset_name']
        self.ok(201,'POST',f"/api/works/{work['id']}/input-clips",{'confirm':True,'role':'story','source':'upload','mime':'audio/wav','audio_base64':base64.b64encode(wav(3,frequency=600)).decode()})
        self.assertFalse(file.exists());detail=self.ok(200,'GET',f"/api/works/{work['id']}")
        self.assertEqual([a['id'] for a in detail['audios']],[audio['id']]);self.assertNotEqual(detail['input_clips'][0]['id'],clip['id'])


class TokenHubPreviewHTTPTests(unittest.TestCase):
    """Use fake audio bytes only; never call the external paid service."""
    close_server = HTTPTests.close_server
    request = HTTPTests.request
    ok = HTTPTests.ok
    error = HTTPTests.error
    new_session = HTTPTests.new_session
    work = HTTPTests.work
    lyrics = HTTPTests.lyrics
    lines = HTTPTests.lines
    job_body = HTTPTests.job_body
    finish = HTTPTests.finish

    def setUp(self):
        HTTPTests.setUp(self)
        self.calls = []
        self.client = Mock(api_key="test-only-not-real", _reload_local_key=False)
        def generate(lyrics, prompt, output, **flags):
            self.calls.append((lyrics, prompt, flags))
            Path(output).write_bytes(b"ID3" + b"test-only" * 160)
            return {"status": "private_candidate_unverified"}
        self.client.generate.side_effect = generate
        self.app.gateway = TokenHubPreviewGateway(candidate_client=self.client)

    def review_fixture(self, **scope):
        work = self.lyrics()
        job = self.ok(202,'POST',f'/api/works/{work["id"]}/jobs',
            dict(self.job_body(work),paid_call_confirmed=True))
        self.finish()
        job = self.ok(200,'GET',f'/api/jobs/{job["id"]}')
        body = {'confirm':True,'sha256':job['snapshot']['candidate']['sha256'],
                'heard_lyrics':'\n'.join(line['text'] for line in job['snapshot']['lyrics']),
                'rights_reference':'测试用授权说明'}
        for key in ('singing','no_missing_words','no_extra_words','quality','complete_ending',
                    'input_rights','output_rights','display_allowed','share_allowed','export_allowed'):
            body[key] = True
        body.update(scope)
        return work,job,body

    def test_review_promotes_real_asset_permissions_snapshot_and_replay(self):
        work,job,body = self.review_fixture(share_allowed=False,export_allowed=False)
        with patch('server.inspect_mp3',return_value={'duration':22,'sha256':body['sha256']}):
            result = self.ok(200,'POST',f'/api/jobs/{job["id"]}/review',body)
        self.assertTrue(result['approved'])
        aid=result['job']['audio_id']
        self.assertEqual(self.request('GET',f'/api/audio/{aid}')[0],200)
        self.error(403,'EXPORT_NOT_LICENSED','GET',f'/api/audio/{aid}?download=1')
        self.error(403,'SHARE_FORBIDDEN','POST',f'/api/works/{work["id"]}/shares',{'confirm':True,'audio_id':aid})
        self.assertTrue(self.ok(200,'POST',f'/api/jobs/{job["id"]}/review',body)['replayed'])
        self.error(404,'NOT_FOUND','GET',f'/api/jobs/{job["id"]}/preview')
        detail=self.ok(200,'GET',f'/api/works/{work["id"]}')
        self.assertEqual(len(detail['audios']),1)
        self.assertEqual(detail['audios'][0]['audit']['rights_reference'],'测试用授权说明')

    def test_review_wrong_lyrics_never_promotes_and_foreign_session_denied(self):
        work,job,body=self.review_fixture()
        body['heard_lyrics']+='额外歌词'
        with patch('server.inspect_mp3',return_value={'duration':22,'sha256':body['sha256']}):
            result=self.ok(200,'POST',f'/api/jobs/{job["id"]}/review',body)
        self.assertFalse(result['approved'])
        self.assertEqual(self.ok(200,'GET',f'/api/works/{work["id"]}')['audios'],[])
        previous=self.session
        self.session=self.new_session()
        try:
            self.error(404,'NOT_FOUND','POST',f'/api/jobs/{job["id"]}/review',body)
        finally:
            self.session=previous

    def test_saved_candidate_remains_reviewable_when_provider_key_removed(self):
        work,job,body=self.review_fixture()
        self.app.gateway=FakeGateway()
        self.assertTrue(self.ok(200,'GET',f'/api/jobs/{job["id"]}')['preview_ready'])
        self.assertEqual(self.request('GET',f'/api/jobs/{job["id"]}/preview')[0],200)
        with patch('server.inspect_mp3',return_value={'duration':22,'sha256':body['sha256']}):
            self.assertTrue(self.ok(200,'POST',f'/api/jobs/{job["id"]}/review',body)['approved'])

    def test_prepare_is_explicit_private_idempotent_and_removed_on_cancel(self):
        work,job,body=self.review_fixture()
        path=f'/api/jobs/{job["id"]}/prepare'
        with patch('server.prepare_short') as process:
            self.error(422,'CONFIRM_REQUIRED','POST',path,{'confirm':True})
            process.assert_not_called()
        def prepared(source,output):
            Path(output).write_bytes(b'unit-only-wav-fixture')
            return {'asset_name':Path(output).name,'mime':'audio/wav','duration':29.5,
                    'sha256':'unit-render-hash','source_sha256':body['sha256'],
                    'processing':'whole_song_pitch_preserving_tempo'}
        with patch('server.prepare_short',side_effect=prepared) as process:
            created=self.ok(200,'POST',path,{'confirm':True,'processing_rights':True})
            self.ok(200,'POST',path,{'confirm':True,'processing_rights':True})
            self.assertEqual(process.call_count,1)
        self.assertEqual(created['status'],'checking')
        self.assertEqual(self.request('GET',f'/api/jobs/{job["id"]}/preview?variant=short')[0],200)
        self.error(403,'EXPORT_NOT_LICENSED','GET',f'/api/jobs/{job["id"]}/preview?variant=short&download=1')
        self.assertEqual(self.request('GET',f'/api/jobs/{job["id"]}/preview')[0],200)
        self.ok(200,'POST',f'/api/jobs/{job["id"]}/cancel',{})
        self.assertFalse((self.app.assets/(job['id']+'.short.wav')).exists())
        self.error(404,'NOT_FOUND','GET',f'/api/jobs/{job["id"]}/preview?variant=short')

    def test_cancel_during_prepare_prevents_late_prepared_file(self):
        work,job,body=self.review_fixture()
        # Session helper exposes csrf/cookie, derive the owner from its cookie.
        token=self.session['cookie'].split('=',1)[1]
        owner=self.app.store.session(token)['owner_id']
        def cancelled(source,output):
            Path(output).write_bytes(b'unit-only-wav-fixture')
            self.app.store.cancel_job(owner,job['id'])
            return {'source_sha256':body['sha256'],'sha256':'unit-render-hash'}
        with patch('server.prepare_short',side_effect=cancelled):
            self.error(409,'INVALID_JOB_STATE','POST',f'/api/jobs/{job["id"]}/prepare',{'confirm':True,'processing_rights':True})
        self.assertFalse((self.app.assets/(job['id']+'.short.wav')).exists())

    def test_lyrics_and_speech_require_per_call_confirmation_and_never_generate_song(self):
        cap = self.ok(200, "GET", "/api/capabilities")
        self.assertTrue(cap["lyrics"] and cap["speech"])
        work = self.lyrics()
        path = f'/api/works/{work["id"]}/lyrics/generate'
        original = self.ok(200, "GET", f'/api/works/{work["id"]}')
        self.error(422, "PAID_CALL_CONFIRMATION_REQUIRED", "POST", path,
                   {"base_version": work["version"], "instruction": "只改第一句"})
        with patch.object(self.app.gateway, "lyrics", return_value={"lines": [
                 {"id": line["id"], "text": line["text"]} for line in self.lines(work)]}) as call:
            result = self.ok(200, "POST", path, {"base_version": work["version"],
                "instruction": "只改第一句", "paid_call_confirmed": True})
            call.assert_called_once()
        self.assertTrue(result["requires_confirmation"])
        self.assertEqual(self.ok(200, "GET", f'/api/works/{work["id"]}')["lyrics"], original["lyrics"])
        body = {"mime": "audio/wav", "audio_base64": base64.b64encode(test_wav(duration=1)).decode(), "consent": True}
        with patch.object(self.app.gateway, "speech", wraps=self.app.gateway.speech) as call:
            self.error(422, "CONFIRMATION_REQUIRED", "POST", "/api/transcribe", body)
            call.assert_called_once()
        with patch.object(self.app.gateway, "speech", return_value={"text": "把第一句改得轻松", "requires_confirmation": True}) as call:
            response = self.ok(200, "POST", "/api/transcribe", dict(body,paid_call_confirmed=True))
            self.assertTrue(response["requires_confirmation"])
            call.assert_called_once()
        self.assertEqual(self.calls, [])

    def test_button_capability_and_owner_private_preview_without_ready_or_share(self):
        cap = self.ok(200, "GET", "/api/capabilities")
        self.assertTrue(cap["singing"])
        self.assertTrue(cap["song_preview_only"])
        self.assertEqual(cap["audio_format"], "mp3")
        work = self.lyrics()
        path = f'/api/works/{work["id"]}/jobs'
        self.error(422, "PAID_CALL_CONFIRMATION_REQUIRED", "POST", path, self.job_body(work))
        self.assertEqual(self.ok(200, "GET", f'/api/works/{work["id"]}')["jobs"], [])
        body = dict(self.job_body(work), paid_call_confirmed=True)
        created = self.ok(202, "POST", path, body)
        self.finish()
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(self.calls[0][0].startswith("[Verse]\n"))
        self.assertTrue(self.calls[0][2]["confirm_paid_call"])
        idem = self.ok(202, "POST", path, body)
        self.assertEqual(idem["id"], created["id"])
        self.assertEqual(len(self.calls), 1)
        job = self.ok(200, "GET", f'/api/jobs/{created["id"]}')
        self.assertEqual(job["status"], "checking")
        self.assertTrue(job["preview_ready"])
        detail = self.ok(200, "GET", f'/api/works/{work["id"]}')
        self.assertEqual(detail["audios"], [])
        self.assertEqual(detail["shares"], [])
        self.assertEqual(detail["status"], "checking")
        self.error(401, "SESSION_REQUIRED", "GET", f'/api/jobs/{created["id"]}/preview', auth=False)
        other = self.new_session()
        self.error(404, "NOT_FOUND", "GET", f'/api/jobs/{created["id"]}/preview', auth=other)
        status, headers, audio = self.request("GET", f'/api/jobs/{created["id"]}/preview')
        self.assertEqual((status, headers["Content-Type"]), (200, "audio/mpeg"))
        self.assertTrue(audio.startswith(b"ID3"))
        self.error(403, "EXPORT_NOT_LICENSED", "GET", f'/api/jobs/{created["id"]}/preview?download=1')
        self.error(404, "NOT_FOUND", "POST", f'/api/works/{work["id"]}/shares',
                   {"confirm": True, "audio_id": created["id"]})
        cancelled = self.ok(200, "POST", f'/api/jobs/{created["id"]}/cancel', {})
        self.assertEqual(cancelled["status"], "cancelled")
        self.error(404, "NOT_FOUND", "GET", f'/api/jobs/{created["id"]}/preview')
        self.assertFalse((self.app.assets / (created["id"] + ".candidate.mp3")).exists())

    def test_same_lyrics_two_styles_have_separate_private_previews_and_revoke_only_one(self):
        work = self.lyrics()
        first = self.ok(202, "POST", f'/api/works/{work["id"]}/jobs',
            dict(self.job_body(work, key="light-a"), paid_call_confirmed=True))
        self.finish()
        changed = self.ok(200, "PATCH", f'/api/works/{work["id"]}',
            {"base_version": work["version"], "style": "slow"})
        self.assertEqual(changed["current_lyric_id"], work["current_lyric_id"])
        second = self.ok(202, "POST", f'/api/works/{work["id"]}/jobs',
            dict(self.job_body(changed, key="slow-b"), paid_call_confirmed=True))
        self.finish()
        detail = self.ok(200, "GET", f'/api/works/{work["id"]}')
        self.assertEqual({job["style"] for job in detail["jobs"]}, {"light", "slow"})
        self.assertTrue(all(job["preview_ready"] for job in detail["jobs"]))
        self.assertEqual(detail["audios"], [])
        self.error(404, "NOT_FOUND", "POST", f'/api/works/{work["id"]}/shares',
                   {"confirm": True, "audio_id": first["id"]})
        self.ok(200, "POST", f'/api/jobs/{first["id"]}/cancel', {})
        self.error(404, "NOT_FOUND", "GET", f'/api/jobs/{first["id"]}/preview')
        self.assertEqual(self.request("GET", f'/api/jobs/{second["id"]}/preview')[0], 200)
        self.assertEqual(len(self.calls), 2)

    def test_more_than_three_failed_attempts_without_daily_limit(self):
        self.client.generate.side_effect = DomainError(502, "PROVIDER_AUTH", "仅测试鉴权拒绝")
        self.assertFalse(self.ok(200, "GET", "/api/capabilities")["local_daily_song_limit"])
        for index in range(4):
            work = self.lyrics()
            body = dict(self.job_body(work, key=f"attempt-{index}"), paid_call_confirmed=True)
            created = self.ok(202, "POST", f'/api/works/{work["id"]}/jobs', body)
            self.finish()
            self.assertEqual(self.ok(200, "GET", f'/api/jobs/{created["id"]}')["error"]["code"], "PROVIDER_AUTH")
        self.assertEqual(self.client.generate.call_count, 4)
        with self.app.store._transaction() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM quota_usage").fetchone()[0], 0)

    def test_failed_provider_never_exposes_fake_audio_or_retries(self):
        self.client.generate.side_effect = DomainError(502, "PROVIDER_AUTH", "TokenHub 返回鉴权拒绝")
        work = self.lyrics()
        body = dict(self.job_body(work), paid_call_confirmed=True)
        created = self.ok(202, "POST", f'/api/works/{work["id"]}/jobs', body)
        self.finish()
        failed = self.ok(200, "GET", f'/api/jobs/{created["id"]}')
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["error"]["code"], "PROVIDER_AUTH")
        self.assertFalse(failed["preview_ready"])
        self.error(404, "NOT_FOUND", "GET", f'/api/jobs/{created["id"]}/preview')
        self.assertEqual(self.client.generate.call_count, 1)
        self.assertEqual(self.ok(200, "GET", f'/api/works/{work["id"]}')["audios"], [])

    def test_delete_after_candidate_and_restart_clean_up(self):
        work = self.lyrics()
        body = dict(self.job_body(work), paid_call_confirmed=True)
        created = self.ok(202, "POST", f'/api/works/{work["id"]}/jobs', body)
        self.finish()
        filename = self.app.assets / (created["id"] + ".candidate.mp3")
        self.assertTrue(filename.exists())
        self.ok(200, "DELETE", f'/api/works/{work["id"]}',
                {"base_version": work["version"], "confirm": True})
        self.assertFalse(filename.exists())
        self.error(404, "NOT_FOUND", "GET", f'/api/jobs/{created["id"]}/preview')


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {}, clear=True)
        env.start()
        self.addCleanup(env.stop)
        network = patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("禁止网络访问"))
        network.start()
        self.addCleanup(network.stop)

    def test_bind_failure_does_not_abort_existing_server_jobs(self):
        with tempfile.TemporaryDirectory() as directory:
            first = server.make_server(0, directory, FakeGateway())
            try:
                owner = first.app.store.create_session()["owner_id"]
                work = first.app.store.create_work(owner, work_body())
                work = first.app.store.save_lyrics(owner, work["id"], {"base_version": 1,
                        "lines": [{"text": "第一句"}, {"text": "第二句"}]})
                job = first.app.store.create_job(owner, work["id"], {"base_version": work["version"],
                        "lyric_id": work["current_lyric_id"], "confirmed": True, "idempotency_key": "test"}, True)
                with self.assertRaises(OSError):
                    server.make_server(first.server_port, directory, FakeGateway())
                self.assertEqual(first.app.store.get_job(owner, job["id"])["status"], "queued")
            finally:
                first.server_close()
                first.app.close()

    def test_close_with_active_and_queued_workers_does_not_deadlock(self):
        with tempfile.TemporaryDirectory() as directory:
            gateway = FakeGateway()
            app = server.Application(directory, gateway)
            entered = threading.Barrier(3)
            release = threading.Event()
            shutting_down = threading.Event()
            futures = []
            original_submit = app.pool.submit
            original_shutdown = app.pool.shutdown
            def shutdown(*args, **kwargs):
                shutting_down.set()
                return original_shutdown(*args, **kwargs)
            def submit(*args):
                future = original_submit(*args)
                futures.append(future)
                return future
            def block(store, owner, job, assets):
                if store.get_job(owner, job["id"])["status"] not in server.ACTIVE:
                    return
                entered.wait(5)
                release.wait(5)
            closed = threading.Event()
            errors = []
            def close():
                try:
                    app.close()
                except Exception as exc:
                    errors.append(exc)
                finally:
                    closed.set()
            with patch.object(gateway, "run_song", side_effect=block), patch.object(app.pool, "submit", side_effect=submit), \
                    patch.object(app.pool, "shutdown", side_effect=shutdown):
                owner = app.store.create_session()["owner_id"]
                jobs = []
                for index in range(3):
                    work = app.store.create_work(owner, work_body())
                    work = app.store.save_lyrics(owner, work["id"], {"base_version": 1,
                            "lines": [{"text": "第一句"}, {"text": "第二句"}]})
                    jobs.append(app.song(owner, work["id"], {"base_version": work["version"],
                        "lyric_id": work["current_lyric_id"], "confirmed": True, "idempotency_key": str(index)}))
                entered.wait(5)
                closing = threading.Thread(target=close, daemon=True)
                closing.start()
                self.assertTrue(shutting_down.wait(5))
                release.set()
                self.assertTrue(closed.wait(5), "关闭不能持有worker退出所需的锁")
                closing.join(5)
            self.assertEqual(errors, [])
            self.assertEqual(app.jobs, set())
            self.assertTrue(all(app.store.get_job(owner, job["id"])["status"] not in server.ACTIVE for job in jobs))
            self.assertTrue(all(future.done() for future in futures))
            app.close()


if __name__ == "__main__":
    unittest.main()
