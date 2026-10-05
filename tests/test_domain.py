import base64
import copy
from concurrent.futures import ThreadPoolExecutor
import io
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import wave


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.domain import DomainError, Store


class DomainTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "domain.sqlite3"
        self.store = Store(self.path)
        self.owner = self.store.create_session()["owner_id"]
        self.other = self.store.create_session()["owner_id"]

    def intent(self, **changes):
        intent = dict(theme="感谢", attitude="温暖", preserve="", avoid="", recipient="朋友")
        intent.update(changes)
        return intent

    def work(self, owner=None, **changes):
        body = {"story": "谢谢你在下雨时等我", "intent": self.intent(),
                "service_consent": True, "title": "雨中的感谢"}
        body.update(changes)
        return self.store.create_work(owner or self.owner, body)

    def lyrics(self, work=None, owner=None, **changes):
        work = work or self.work(owner)
        body = {"base_version": work["version"], "source": "manual",
                "lines": [{"text": "你在雨里等我。"}, {"text": "把晴天装进口袋。"}]}
        body.update(changes)
        return self.store.save_lyrics(owner or self.owner, work["id"], body)

    def lines(self, work):
        return copy.deepcopy(work["lyrics"][-1]["lines"])

    def save(self, work, lines, **changes):
        body = {"base_version": work["version"], "lines": lines, "source": "manual"}
        body.update(changes)
        return self.store.save_lyrics(self.owner, work["id"], body)

    def job_body(self, work, key="job-key"):
        return {"base_version": work["version"], "lyric_id": work["current_lyric_id"],
                "idempotency_key": key, "confirmed": True}

    def job(self, work=None, key="job-key", owner=None):
        work = work or self.lyrics(owner=owner)
        return self.store.create_job(owner or self.owner, work["id"], self.job_body(work, key), True)

    def checking(self, work=None, key="job-key"):
        job = self.job(work, key)
        self.store.transition_job(job["id"], "generating")
        return self.store.transition_job(job["id"], "checking")

    def asset(self, **changes):
        asset = {"verified_singing": True, "lyrics_match": True, "duration": 22.5,
                 "asset_name": "song-01.mp3", "mime": "audio/mpeg", "export_allowed": False}
        asset.update(changes)
        return asset

    def audio(self, work=None, key="job-key"):
        work = work or self.lyrics()
        job = self.checking(work, key)
        done = self.store.complete_job(job["id"], self.asset())
        return self.store.media_asset(self.owner, done["audio_id"])

    def share(self, work, audio, **changes):
        body = {"confirm": True, "audio_id": audio["id"], "show_lyrics": False}
        body.update(changes)
        return self.store.create_share(self.owner, work["id"], body)

    def clip_bytes(self, seconds=1, channels=1, width=2):
        stream = io.BytesIO()
        with wave.open(stream, "wb") as sample:
            sample.setnchannels(channels)
            sample.setsampwidth(width)
            sample.setframerate(8000)
            sample.writeframes(b"\x01" * (int(8000 * seconds) * channels * width))
        return stream.getvalue()

    def clip_body(self, audio=None, **changes):
        body = {"role": "story", "source": "upload", "mime": "audio/wav",
                "audio_base64": base64.b64encode(audio if audio is not None else self.clip_bytes()).decode(),
                "confirm": True}
        body.update(changes)
        return body

    def rows(self, query, params=()):
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        try:
            return [dict(row) for row in connection.execute(query, params)]
        finally:
            connection.close()

    def error(self, status, function, *args, code=None, **kwargs):
        with self.assertRaises(DomainError) as raised:
            function(*args, **kwargs)
        self.assertEqual(raised.exception.status, status)
        self.assertIsInstance(raised.exception.message, str)
        self.assertTrue(raised.exception.message)
        if code:
            self.assertEqual(raised.exception.code, code)
        return raised.exception

    def parallel(self, functions):
        barrier = threading.Barrier(len(functions))

        def execute(function):
            barrier.wait(timeout=10)
            try:
                return function()
            except DomainError as exc:
                return exc

        with ThreadPoolExecutor(max_workers=len(functions)) as executor:
            return list(executor.map(execute, functions))

    def assert_work_status(self, work, expected):
        detail = self.store.get_work(self.owner, work["id"])
        summary = next(item for item in self.store.list_works(self.owner) if item["id"] == work["id"])
        for result in (detail, summary):
            self.assertEqual(result["status"], expected)
            self.assertEqual(result["version"], work["version"])
            self.assertEqual(result["updated_at"], work["updated_at"])

    def test_schema_wal_foreign_keys_and_reopen(self):
        self.assertEqual(self.rows("PRAGMA user_version")[0]["user_version"], 6)
        self.assertEqual(self.rows("PRAGMA journal_mode")[0]["journal_mode"], "wal")
        self.assertEqual(self.rows("SELECT version FROM schema_versions ORDER BY version"),
                         [{"version": 1}, {"version": 2}, {"version": 3}, {"version": 4}, {"version": 5}, {"version": 6}])
        connection = self.store._connect()
        try:
            self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
        finally:
            connection.close()
        work = self.work()
        reopened = Store(self.path)
        self.assertEqual(reopened.get_work(self.owner, work["id"]), work)

    def test_v2_database_migrates_to_private_blob_table_without_losing_works(self):
        work = self.work()
        connection = sqlite3.connect(self.path)
        try:
            connection.execute("DROP TABLE input_clips")
            connection.execute("ALTER TABLE shares DROP COLUMN card_theme")
            connection.execute("DROP INDEX jobs_one_active")
            connection.execute("""CREATE UNIQUE INDEX jobs_one_active ON jobs(work_id)
                WHERE status IN ('queued','generating','checking')""")
            connection.execute("DELETE FROM schema_versions WHERE version IN (3, 4)")
            connection.execute("PRAGMA user_version=2")
            connection.commit()
        finally:
            connection.close()
        upgraded = Store(self.path)
        self.assertEqual(upgraded.get_work(self.owner, work["id"]), work)
        self.assertEqual(self.rows("PRAGMA user_version")[0]["user_version"], 6)
        self.assertEqual(self.rows("PRAGMA foreign_key_check"), [])
        clip = upgraded.create_input_clip(self.owner, work["id"], self.clip_body())
        self.assertEqual(upgraded.get_work(self.owner, work["id"])["input_clips"], [clip])
        self.assertEqual(Store(self.path).get_work(self.owner, work["id"])["input_clips"], [clip])

    def test_input_clip_replaces_only_same_role_and_is_not_song_or_revision(self):
        work = self.work()
        original = self.clip_bytes()
        first = self.store.create_input_clip(self.owner, work["id"], self.clip_body(original))
        instruction = self.store.create_input_clip(self.owner, work["id"], self.clip_body(
            role="instruction", source="record"))
        replaced = self.store.create_input_clip(self.owner, work["id"], self.clip_body(
            self.clip_bytes(seconds=2), source="record"))
        self.assertNotEqual(first["id"], replaced["id"])
        self.assertEqual(set(first), {"id", "role", "source", "mime", "duration", "created_at"})
        self.assertEqual((first["duration"], replaced["duration"]), (1, 2))
        self.error(404, self.store.input_clip_audio, self.owner, first["id"])
        self.assertEqual(self.store.input_clip_audio(self.owner, instruction["id"]), ("audio/wav", original))
        detail = self.store.get_work(self.owner, work["id"])
        self.assertEqual({clip["id"] for clip in detail["input_clips"]}, {instruction["id"], replaced["id"]})
        self.assertEqual((detail["audios"], detail["jobs"], detail["shares"]), ([], [], []))
        self.assertEqual((detail["version"], detail["updated_at"], detail["status"]),
                         (work["version"], work["updated_at"], "draft"))
        self.assertEqual(len(self.rows("SELECT * FROM work_revisions")), 1)
        self.assertNotIn("audio_base64", json.dumps(detail))
        self.assertEqual(self.store.delete_input_clip(self.owner, instruction["id"]), {"deleted": True})
        self.error(404, self.store.delete_input_clip, self.owner, instruction["id"])
        self.assertEqual(len(self.rows("SELECT * FROM input_clips")), 1)

    def test_input_clip_validation_rejects_forgery_duration_size_and_unconfirmed(self):
        work = self.work()
        valid = self.clip_body()
        cases = [
            (dict(confirm=1), 422), (dict(role="song"), 422),
            (dict(source="generated"), 422), (dict(mime="video/mp4"), 422),
            (dict(mime="audio/webm"), 422), (dict(audio_base64="%%%"), 422),
            (self.clip_body(b"x" * 99), 413), (self.clip_body(b"x" * (6 * 1024 * 1024 + 1)), 413),
            (self.clip_body(self.clip_bytes(), mime="audio/mpeg"), 422),
            (self.clip_body(self.clip_bytes(), mime="audio/mp4"), 422),
            (self.clip_body(self.clip_bytes(width=1)), 422),
            (self.clip_body(self.clip_bytes(channels=3)), 422),
            (self.clip_body(self.clip_bytes(seconds=60.01)), 422),
        ]
        for changes, status in cases:
            with self.subTest(case=str(changes)[:80]):
                body = dict(valid)
                body.update(changes)
                self.error(status, self.store.create_input_clip, self.owner, work["id"], body)
                self.assertEqual(self.rows("SELECT * FROM input_clips"), [])
        exact = self.store.create_input_clip(self.owner, work["id"], self.clip_body(self.clip_bytes(seconds=60)))
        self.assertEqual(exact["duration"], 60)
        self.error(404, self.store.create_input_clip, self.other, work["id"], valid)
        self.error(404, self.store.input_clip_audio, self.other, exact["id"])
        self.error(404, self.store.delete_input_clip, self.other, exact["id"])

    def test_mp4_audio_is_probed_and_video_track_is_rejected(self):
        work = self.work()
        source = Path(self.directory.name) / "synthetic.wav"
        encoded = Path(self.directory.name) / "synthetic.mp4"
        source.write_bytes(self.clip_bytes())
        subprocess.run(["/usr/bin/afconvert", "-f", "m4af", "-d", "aac",
                        str(source), str(encoded)], check=True, capture_output=True)
        audio = encoded.read_bytes()
        self.assertIn(b"soun", audio)
        clip = self.store.create_input_clip(self.owner, work["id"], self.clip_body(audio, mime="audio/mp4"))
        self.assertEqual(clip["duration"], 1)
        self.assertEqual(self.store.input_clip_audio(self.owner, clip["id"]), ("audio/mp4", audio))
        self.error(422, self.store.create_input_clip, self.owner, work["id"],
                   self.clip_body(audio.replace(b"soun", b"vide", 1), mime="audio/mp4"), code="AUDIO_INVALID")
        self.assertEqual(self.store.input_clip_audio(self.owner, clip["id"]), ("audio/mp4", audio))

    def test_mp3_probe_must_match_mime_and_report_valid_audio_duration(self):
        work = self.work()
        candidate = b"ID3" + b"\0" * 197
        output = "File type ID:   MPG3\nNum Tracks:     1\nestimated duration: 2.250000 sec\n"
        with patch("backend.domain.subprocess.run", return_value=Mock(returncode=0, stdout=output)) as probe:
            clip = self.store.create_input_clip(self.owner, work["id"],
                                                self.clip_body(candidate, mime="audio/mp3"))
        self.assertEqual(clip["duration"], 2.25)
        self.assertEqual(probe.call_args.args[0][0], "/usr/bin/afinfo")
        self.assertEqual(self.store.input_clip_audio(self.owner, clip["id"]), ("audio/mp3", candidate))
        with patch("backend.domain.subprocess.run", return_value=Mock(
                returncode=0, stdout=output.replace("MPG3", "mp4f"))):
            self.error(422, self.store.create_input_clip, self.owner, work["id"],
                       self.clip_body(candidate, mime="audio/mpeg"), code="AUDIO_INVALID")

    def test_input_clip_blob_removed_on_work_delete_and_never_publicly_shared(self):
        work = self.lyrics()
        clip = self.store.create_input_clip(self.owner, work["id"], self.clip_body())
        audio = self.audio(work)
        share = self.share(work, audio)
        self.assertNotIn("input_clips", self.store.public_share(share["token"]))
        self.error(404, self.store.create_share, self.owner, work["id"],
                   {"confirm": True, "audio_id": clip["id"]})
        self.store.delete_work(self.owner, work["id"], {"base_version": work["version"], "confirm": True})
        self.assertEqual(self.rows("SELECT * FROM input_clips"), [])
        self.error(404, self.store.input_clip_audio, self.owner, clip["id"])

    def test_sessions_are_random_and_only_token_hash_is_stored(self):
        session = self.store.create_session()
        second = self.store.create_session()
        self.assertGreaterEqual(len(session["token"]), 43)
        self.assertNotEqual(session["token"], second["token"])
        self.assertNotEqual(session["csrf"], session["token"])
        self.assertEqual(self.store.session(session["token"]),
                         {"owner_id": session["owner_id"], "csrf": session["csrf"]})
        stored = self.rows("SELECT * FROM sessions WHERE owner_id=?", (session["owner_id"],))[0]
        self.assertEqual(stored["token_hash"], hashlib.sha256(session["token"].encode()).hexdigest())
        self.assertNotIn(session["token"], json.dumps(stored))
        self.assertIsNone(self.store.session("not-a-token"))
        self.assertIsNone(self.store.session(None))

    def test_session_expires_at_exactly_thirty_days(self):
        now = datetime(2026, 9, 1, tzinfo=timezone.utc)
        with patch("backend.domain._now", return_value=now):
            session = self.store.create_session()
        with patch("backend.domain._now", return_value=now + timedelta(days=30, microseconds=-1)):
            self.assertIsNotNone(self.store.session(session["token"]))
        with patch("backend.domain._now", return_value=now + timedelta(days=30)):
            self.assertIsNone(self.store.session(session["token"]))

    def test_work_detail_and_summary_contract(self):
        work = self.work()
        self.assertEqual(set(work), {"id", "title", "story", "intent", "style", "tempo", "version",
                                    "current_lyric_id", "lyrics", "audios", "jobs", "shares",
                                    "input_clips", "created_at", "updated_at", "status"})
        self.assertEqual(work["style"], "light")
        self.assertEqual(work["version"], 1)
        self.assertEqual(self.store.get_work(self.owner, work["id"]), work)
        summary = self.store.list_works(self.owner)[0]
        self.assertTrue({"id", "title", "story", "status", "updated_at", "version"} <= set(summary))
        self.assertEqual(self.store.list_works(self.other), [])

    def test_work_input_and_explicit_service_consent(self):
        for story in ("", " " * 10, "我" * 501, 12, None, "\ud800"):
            with self.subTest(story=repr(story)):
                self.error(422, self.work, story=story)
        for consent in (False, 1, "true", None):
            self.error(422, self.work, service_consent=consent, code="CONSENT_REQUIRED")
        for intent in ({}, self.intent(theme=1), {**self.intent(), "extra": "正文"}):
            self.error(422, self.work, intent=intent)
        self.assertEqual(len(self.work(story="我" * 500)["story"]), 500)
        self.assertEqual(len(self.work(story="𝄞" * 500)["story"]), 500)

    def test_story_intent_style_changes_are_revisions_with_optimistic_lock(self):
        work = self.work()
        for field, value in (("story", "新的故事"), ("intent", self.intent(attitude="轻快")),
                             ("style", "groove"), ("title", "新的标题")):
            old = copy.deepcopy(work)
            work = self.store.update_work(self.owner, work["id"],
                                          {"base_version": work["version"], field: value})
            self.assertEqual(work["version"], old["version"] + 1)
            self.error(409, self.store.update_work, self.owner, work["id"],
                       {"base_version": old["version"], "story": "过期编辑"}, code="VERSION_CONFLICT")
        revisions = self.rows("SELECT * FROM work_revisions WHERE work_id=? ORDER BY version", (work["id"],))
        self.assertEqual(len(revisions), 5)
        self.assertEqual(json.loads(revisions[0]["snapshot"])["story"], "谢谢你在下雨时等我")
        self.error(422, self.store.update_work, self.owner, work["id"],
                   {"base_version": work["version"], "style": "rock"})
        self.error(422, self.store.update_work, self.owner, work["id"],
                   {"base_version": True, "story": "故事"})

    def test_unchanged_work_confirmation_is_noop(self):
        work = self.work()
        result = self.store.update_work(self.owner, work["id"],
                                        {"base_version": work["version"], "story": work["story"]})
        self.assertEqual(result, work)
        self.assertEqual(len(self.rows("SELECT * FROM work_revisions")), 1)

    def test_lyric_line_validation_and_sample_provenance(self):
        work = self.work()
        for lines in ([], [{"text": "一行"}], [{"text": "一行"}] * 5,
                      [{"text": ""}, {"text": "第二行"}],
                      [{"text": "字" * 161}, {"text": "第二行"}],
                      [{"text": "第一行\n越界行"}, {"text": "第二行"}]):
            with self.subTest(lines=lines):
                self.error(422, self.lyrics, work, lines=lines)
        self.error(422, self.lyrics, work, source="fake-ai")
        result = self.lyrics(work, source="sample")
        lyric = result["lyrics"][0]
        self.assertEqual(lyric["source"], "sample")
        self.assertIsNone(lyric["parent_id"])
        self.assertEqual(lyric["number"], 1)
        self.assertEqual(len({line["id"] for line in lyric["lines"]}), 2)
        self.assertEqual(set(lyric["lines"][0]), {"id", "text", "locked", "locked_spans"})
        self.assertFalse(lyric["lines"][0]["locked"])
        self.assertEqual(lyric["lines"][0]["locked_spans"], [])

    def test_lyric_versions_immutable_and_existing_ids_stable(self):
        work = self.lyrics()
        first = copy.deepcopy(work["lyrics"][0])
        lines = self.lines(work)
        lines[1]["text"] = "把星光装进口袋。"
        revised = self.save(work, lines)
        self.assertEqual(revised["lyrics"][0], first)
        self.assertEqual(revised["lyrics"][-1]["parent_id"], first["id"])
        self.assertEqual(revised["lyrics"][-1]["number"], 2)
        self.assertNotEqual(revised["current_lyric_id"], first["id"])
        self.assertEqual([line["id"] for line in lines], [line["id"] for line in self.lines(revised)])
        self.error(422, self.lyrics, revised, code="LINE_ID_REQUIRED")
        invalid = self.lines(revised)
        invalid[0]["id"] = "invented-id"
        self.error(422, self.save, revised, invalid, code="INVALID_LINE_ID")
        self.error(409, self.save, work, lines, code="VERSION_CONFLICT")

    def test_can_add_then_remove_unlocked_line(self):
        work = self.lyrics()
        lines = self.lines(work) + [{"text": "新增的一行。"}]
        added = self.save(work, lines)
        self.assertEqual(len(self.lines(added)), 3)
        removed = self.save(added, self.lines(added)[:2])
        self.assertEqual([line["id"] for line in self.lines(removed)],
                         [line["id"] for line in self.lines(work)])

    def test_locked_line_protects_exact_punctuation_and_cannot_be_removed(self):
        work = self.lyrics(lines=[{"text": "你在雨里等我。", "locked": True},
                                  {"text": "第二行。"}, {"text": "第三行。"}])
        lines = self.lines(work)
        lines[0]["text"] = "你在雨里等我！"
        self.error(422, self.save, work, lines, code="LOCK_CONFLICT")
        self.error(422, self.save, work, self.lines(work)[1:], code="LOCK_CONFLICT")
        reordered = self.save(work, list(reversed(self.lines(work))))
        self.assertEqual(self.lines(reordered)[-1]["text"], "你在雨里等我。")

    def test_unlock_and_edit_must_be_separate_revisions(self):
        work = self.lyrics(lines=[{"text": "原句。", "locked": True}, {"text": "第二句。"}])
        lines = self.lines(work)
        lines[0].update(locked=False, text="修改句。")
        self.error(422, self.save, work, lines, code="LOCK_CONFLICT")
        lines[0]["text"] = "原句。"
        unlocked = self.save(work, lines)
        lines = self.lines(unlocked)
        lines[0]["text"] = "修改句。"
        edited = self.save(unlocked, lines)
        self.assertEqual(self.lines(edited)[0]["text"], "修改句。")
        self.assertTrue(edited["lyrics"][0]["lines"][0]["locked"])

    def test_lock_change_cannot_mix_with_another_line_text_edit(self):
        work = self.lyrics()
        lines = self.lines(work)
        lines[0]["locked"] = True
        lines[1]["text"] = "另一句改字。"
        self.error(422, self.save, work, lines, code="LOCK_CONFLICT")

    def test_locked_spans_allow_surrounding_edits_but_not_deletion(self):
        work = self.lyrics(lines=[{"text": "谢谢你，陪我走过雨季。", "locked_spans": ["谢谢你，"]},
                                  {"text": "第二行。"}, {"text": "第三行。"}])
        lines = self.lines(work)
        lines[0]["text"] = "谢谢你，陪我走进晴天。"
        revised = self.save(work, lines)
        self.assertEqual(self.lines(revised)[0]["locked_spans"], ["谢谢你，"])
        lines = self.lines(revised)
        lines[0]["text"] = "谢谢你！陪我走进晴天。"
        self.error(422, self.save, revised, lines, code="LOCK_CONFLICT")
        self.error(422, self.save, revised, self.lines(revised)[1:], code="LOCK_CONFLICT")

    def test_span_unlock_and_change_are_separate_and_missing_flags_are_preserved(self):
        work = self.lyrics(lines=[{"text": "保留原文，走向晴天。", "locked_spans": ["保留原文，"]},
                                  {"text": "第二行。"}])
        lines = self.lines(work)
        lines[0].update(text="更换原文，走向晴天。", locked_spans=[])
        self.error(422, self.save, work, lines, code="LOCK_CONFLICT")
        lines[0]["text"] = "保留原文，走向晴天。"
        unlocked = self.save(work, lines)
        lines = self.lines(unlocked)
        lines[0]["text"] = "更换原文，走向晴天。"
        self.save(unlocked, lines)
        work = self.lyrics(lines=[{"text": "锁定原句。", "locked": True}, {"text": "第二行。"}])
        unchanged = [{"id": line["id"], "text": line["text"]} for line in self.lines(work)]
        self.assertTrue(self.lines(self.save(work, unchanged))[0]["locked"])

    def test_restore_creates_new_child_with_historical_stable_ids(self):
        first = self.lyrics()
        lines = self.lines(first)
        lines[0]["text"] = "改过的第一行。"
        second = self.save(first, lines)
        restored = self.store.restore_lyrics(self.owner, first["id"],
                                             {"base_version": second["version"],
                                              "lyric_id": first["current_lyric_id"]})
        self.assertEqual(self.lines(restored), self.lines(first))
        self.assertEqual(restored["lyrics"][-1]["parent_id"], second["current_lyric_id"])
        self.assertEqual(restored["lyrics"][-1]["number"], 3)
        self.assertEqual(restored["lyrics"][:2], second["lyrics"])
        self.assertNotEqual(restored["current_lyric_id"], first["current_lyric_id"])

    def test_restore_cannot_bypass_current_line_or_span_locks(self):
        for locking in ({"locked": True}, {"locked_spans": ["改后"]}):
            with self.subTest(locking=locking):
                first = self.lyrics()
                lines = self.lines(first)
                lines[0]["text"] = "改后文本。"
                second = self.save(first, lines)
                lines = self.lines(second)
                lines[0].update(locking)
                locked = self.save(second, lines)
                self.error(422, self.store.restore_lyrics, self.owner, first["id"],
                           {"base_version": locked["version"], "lyric_id": first["current_lyric_id"]},
                           code="LOCK_CONFLICT")

    def test_preserve_multiline_and_exact_avoid_entries(self):
        work = self.work(intent=self.intent(preserve="雨里\n口袋", avoid="告别\n再见"))
        work = self.lyrics(work)
        lines = self.lines(work)
        lines[0]["text"] = "你在晴天等我。"
        self.error(422, self.save, work, lines, code="INTENT_CONFLICT")
        lines = self.lines(work)
        lines[0]["text"] = "你在雨里等我，说再见。"
        self.error(422, self.save, work, lines, code="INTENT_CONFLICT")
        lines[0]["text"] = "你在雨里等我，不愿离别。"
        self.assertEqual(self.lines(self.save(work, lines))[0]["text"], lines[0]["text"])

    def test_conflicting_preserve_and_avoid_reject_without_revision(self):
        work = self.work(intent=self.intent(preserve="雨里", avoid="雨里"))
        self.error(422, self.lyrics, work, code="INTENT_CONFLICT")
        self.assertEqual(self.store.get_work(self.owner, work["id"])["lyrics"], [])
        self.assertEqual(len(self.rows("SELECT * FROM work_revisions")), 1)

    def test_job_and_restore_revalidate_updated_intent(self):
        first = self.lyrics()
        edited = self.store.update_work(self.owner, first["id"],
                                        {"base_version": first["version"],
                                         "intent": self.intent(preserve="必须出现")})
        self.error(422, self.job, edited, code="INTENT_CONFLICT")
        self.error(422, self.store.restore_lyrics, self.owner, edited["id"],
                   {"base_version": edited["version"], "lyric_id": first["current_lyric_id"]},
                   code="INTENT_CONFLICT")
        self.assertEqual(self.rows("SELECT * FROM quota_usage"), [])

    def test_unconfigured_provider_returns_503_without_job_or_quota(self):
        work = self.lyrics()
        self.error(503, self.store.create_job, self.owner, work["id"], self.job_body(work), False,
                   code="PROVIDER_NOT_CONFIGURED")
        self.assertEqual(self.rows("SELECT * FROM jobs"), [])
        self.assertEqual(self.rows("SELECT * FROM quota_usage"), [])
        self.assertEqual(self.store.get_work(self.owner, work["id"]), work)
        self.assertEqual(self.job(work)["status"], "queued")

    def test_job_requires_confirmation_current_lyrics_and_base(self):
        first = self.lyrics()
        second = self.save(first, self.lines(first))
        for changes, status, code in (({"confirmed": 1}, 422, "CONFIRM_REQUIRED"),
                                      ({"base_version": 1}, 409, "VERSION_CONFLICT"),
                                      ({"lyric_id": first["current_lyric_id"]}, 409, "LYRIC_VERSION_CONFLICT")):
            body = self.job_body(second)
            body.update(changes)
            self.error(status, self.store.create_job, self.owner, second["id"], body, True, code=code)
        self.assertEqual(self.rows("SELECT * FROM quota_usage"), [])

    def test_job_snapshot_and_completion_do_not_overwrite_current_work(self):
        work = self.lyrics()
        job = self.checking(work)
        changed = self.store.update_work(self.owner, work["id"],
                                         {"base_version": work["version"], "style": "slow", "story": "新的故事"})
        lines = self.lines(changed)
        lines[0]["text"] = "更新后的歌词。"
        changed = self.save(changed, lines)
        ready = self.store.complete_job(job["id"], self.asset())
        after = self.store.get_work(self.owner, work["id"])
        for key in ("current_lyric_id", "version", "style", "story", "status", "updated_at"):
            self.assertEqual(after[key], changed[key])
        self.assertEqual(ready["snapshot"]["lyrics"], self.lines(work))
        self.assertEqual(ready["snapshot"]["intent"], work["intent"])
        self.assertEqual(ready["snapshot"]["style"], "light")
        self.assertEqual(after["audios"][0]["lyrics"], self.lines(work))
        self.assertEqual(after["audios"][0]["lyric_id"], work["current_lyric_id"])

    def test_job_idempotency_returns_original_even_after_work_change(self):
        work = self.lyrics()
        body = self.job_body(work)
        original = self.job(work)
        self.store.update_work(self.owner, work["id"], {"base_version": work["version"], "style": "slow"})
        self.assertEqual(self.store.create_job(self.owner, work["id"], body, False), original)
        self.assertEqual(len(self.rows("SELECT * FROM jobs")), 1)
        self.assertEqual(self.rows("SELECT * FROM quota_usage"), [])
        for field, value in (("base_version", work["version"] + 1), ("lyric_id", "different-id"),
                             ("confirmed", False)):
            conflict = dict(body, **{field: value})
            self.error(409, self.store.create_job, self.owner, work["id"], conflict, True,
                       code="IDEMPOTENCY_CONFLICT")
        another = self.lyrics()
        self.error(409, self.job, another, code="IDEMPOTENCY_CONFLICT")

    def test_idempotency_keys_are_scoped_to_owner(self):
        first = self.job()
        other_work = self.lyrics(owner=self.other)
        second = self.job(other_work, owner=self.other)
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(len(self.rows("SELECT * FROM jobs")), 2)
        self.assertEqual(self.rows("SELECT * FROM quota_usage"), [])

    def test_one_active_job_per_work(self):
        work = self.lyrics()
        job = self.job(work)
        self.error(409, self.job, work, key="different", code="JOB_ACTIVE")
        self.assertEqual(len(self.rows("SELECT * FROM jobs")), 1)
        self.assertEqual(self.rows("SELECT * FROM quota_usage"), [])
        self.store.cancel_job(self.owner, job["id"])
        self.assertEqual(self.job(work, key="different")["status"], "queued")

    def test_no_daily_limit_after_failed_jobs_or_deleted_work(self):
        for index in range(4):
            work = self.lyrics()
            job = self.job(work, key=str(index))
            self.store.transition_job(job["id"], "failed", {"code": "PROVIDER_AUTH", "message": "测试拒绝"})
            self.store.delete_work(self.owner, work["id"], {"base_version": work["version"], "confirm": True})
        self.assertEqual(self.job(self.lyrics(), key="fifth")["status"], "queued")
        self.assertEqual(len(self.rows("SELECT * FROM jobs")), 5)
        self.assertEqual(self.rows("SELECT * FROM quota_usage"), [])

    def test_old_quota_records_do_not_block_song_creation(self):
        work = self.lyrics()
        with self.store._transaction() as connection:
            connection.execute("INSERT INTO quota_usage VALUES(?, ?, ?)", (self.owner, "2026-09-30", 3))
        self.assertEqual(self.job(work, key="after-old-limit")["status"], "queued")
        self.assertEqual(self.rows("SELECT used FROM quota_usage"), [{"used": 3}])
        self.store.cancel_job(self.owner, self.rows("SELECT id FROM jobs")[0]["id"])
        self.assertEqual(self.job(work, key="next-attempt")["status"], "queued")
        self.assertEqual(self.rows("SELECT used FROM quota_usage"), [{"used": 3}])

    def test_job_state_machine_is_strict_and_ready_is_completion_only(self):
        job = self.job()
        for status in ("checking", "ready", "queued"):
            self.error(409, self.store.transition_job, job["id"], status, code="INVALID_JOB_STATE")
        self.assertEqual(self.store.transition_job(job["id"], "generating")["status"], "generating")
        self.error(409, self.store.transition_job, job["id"], "queued")
        self.assertEqual(self.store.transition_job(job["id"], "checking")["status"], "checking")
        self.error(409, self.store.transition_job, job["id"], "ready")
        self.assertEqual(self.store.complete_job(job["id"], self.asset())["status"], "ready")

    def test_terminal_jobs_never_resurrect(self):
        for status in ("failed", "timed_out", "cancelled"):
            with self.subTest(status=status):
                job = self.job(key=status)
                terminal = self.store.transition_job(job["id"], status)
                self.assertEqual(self.store.transition_job(job["id"], "generating"), terminal)
                self.assertEqual(self.store.cancel_job(self.owner, job["id"]), terminal)
                self.assertEqual(self.store.complete_job(job["id"], self.asset()), terminal)
        self.assertEqual(self.rows("SELECT * FROM audios"), [])

    def test_complete_only_from_checking(self):
        job = self.job()
        self.error(409, self.store.complete_job, job["id"], self.asset())
        self.store.transition_job(job["id"], "generating")
        self.error(409, self.store.complete_job, job["id"], self.asset())
        self.assertEqual(self.rows("SELECT * FROM audios"), [])

    def test_complete_rejects_unverified_duration_and_unsafe_assets_atomically(self):
        job = self.checking()
        invalid = [
            {"verified_singing": False}, {"verified_singing": 1}, {"lyrics_match": False},
            {"duration": 14.99}, {"duration": 30.01}, {"duration": True},
            {"duration": float("nan")}, {"duration": float("inf")}, {"duration": 10 ** 1000},
            {"asset_name": "../secret.mp3"}, {"asset_name": "/tmp/secret.mp3"},
            {"asset_name": "..\\secret.mp3"}, {"asset_name": "C:secret.mp3"},
            {"asset_name": "bad\x00.mp3"}, {"asset_name": ""}, {"asset_name": ".."},
            {"mime": "text/html"}, {"export_allowed": "yes"},
        ]
        for changes in invalid:
            with self.subTest(changes=changes):
                self.error(422, self.store.complete_job, job["id"], self.asset(**changes))
                self.assertEqual(self.store.get_job(self.owner, job["id"])["status"], "checking")
                self.assertEqual(self.rows("SELECT * FROM audios"), [])

    def test_audio_duration_inclusive_bounds_and_repeated_completion(self):
        for duration in (15, 30):
            job = self.checking(key=str(duration))
            ready = self.store.complete_job(job["id"], self.asset(duration=duration))
            self.assertEqual(self.store.media_asset(self.owner, ready["audio_id"])["duration"], duration)
            self.assertEqual(self.store.complete_job(job["id"], self.asset(asset_name="different.mp3")), ready)
            self.assertEqual(self.store.cancel_job(self.owner, job["id"]), ready)
        self.assertEqual(len(self.rows("SELECT * FROM audios")), 2)

    def test_cancelled_job_ignores_late_completion_even_invalid_asset(self):
        work = self.lyrics()
        job = self.checking(work)
        cancelled = self.store.cancel_job(self.owner, job["id"])
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual(self.store.complete_job(job["id"], None), cancelled)
        self.assertEqual(self.store.get_work(self.owner, work["id"])["audios"], [])

    def test_deleted_work_ignores_late_worker_completion_and_owner_access(self):
        work = self.lyrics()
        job = self.checking(work)
        self.store.delete_work(self.owner, work["id"], {"base_version": work["version"], "confirm": True})
        late = self.store.complete_job(job["id"], self.asset())
        self.assertEqual(late["status"], "cancelled")
        self.assertIsNone(late["snapshot"])
        self.assertIsNone(late["audio_id"])
        self.assertEqual(self.store.transition_job(job["id"], "generating"), late)
        self.error(404, self.store.get_job, self.owner, job["id"])
        self.error(404, self.store.get_work, self.owner, work["id"])
        self.assertEqual(self.rows("SELECT * FROM audios"), [])

    def test_restart_marks_all_active_jobs_failed_without_reviving_terminal(self):
        self.store = Store(self.path)
        active = [self.job(key=str(index)) for index in range(3)]
        self.store.transition_job(active[1]["id"], "generating")
        self.store.transition_job(active[2]["id"], "generating")
        self.store.transition_job(active[2]["id"], "checking")
        cancelled = self.store.cancel_job(self.owner, self.job(key="cancelled")["id"])
        restarted = Store(self.path)
        result = restarted.abort_incomplete_jobs()
        self.assertEqual(len(result), 3)
        for job in result:
            self.assertEqual(job["status"], "failed")
            self.assertEqual(job["error"]["code"], "SERVER_RESTARTED")
            self.assertEqual(restarted.complete_job(job["id"], self.asset()), job)
        self.assertEqual(restarted.get_job(self.owner, cancelled["id"]), cancelled)
        self.assertEqual(restarted.abort_incomplete_jobs(), [])
        self.assertEqual(len(self.rows("SELECT * FROM jobs")), 4)
        self.assertEqual(self.rows("SELECT * FROM quota_usage"), [])

    def test_preview_compare_allows_three_distinct_styles_but_not_duplicate_or_parallel_generation(self):
        work = self.lyrics()
        first = self.store.create_job(self.owner, work["id"], self.job_body(work, "light"), True, True)
        self.error(409, self.store.create_job, self.owner, work["id"],
                   self.job_body(work, "parallel"), True, True, code="JOB_ACTIVE")
        self.store.transition_job(first["id"], "generating")
        self.store.transition_job(first["id"], "checking")
        self.error(409, self.store.create_job, self.owner, work["id"],
                   self.job_body(work, "same"), True, True, code="STYLE_EXISTS")
        self.error(409, self.store.create_job, self.owner, work["id"],
                   self.job_body(work, "internal"), True, code="JOB_ACTIVE")
        for key, style in (("slow", "slow"), ("groove", "groove")):
            work = self.store.update_work(self.owner, work["id"],
                {"base_version": work["version"], "style": style})
            job = self.store.create_job(self.owner, work["id"], self.job_body(work, key), True, True)
            self.store.transition_job(job["id"], "generating")
            self.store.transition_job(job["id"], "checking")
        self.assertEqual({job["style"] for job in self.store.get_work(self.owner, work["id"])["jobs"]},
                         {"light", "slow", "groove"})
        self.error(409, self.store.create_job, self.owner, work["id"],
                   self.job_body(work, "fourth"), True, True, code="COMPARE_FULL")
        self.store.cancel_job(self.owner, first["id"])
        self.error(409, self.store.create_job, self.owner, work["id"],
                   self.job_body(work, "same-groove"), True, True, code="STYLE_EXISTS")

    def test_keep_audio_owner_only_and_does_not_change_content_revision(self):
        work = self.lyrics()
        audio = self.audio(work)
        self.assertFalse(audio["kept"])
        result = self.store.keep_audio(self.owner, work["id"], audio["id"])
        self.assertTrue(result["audios"][0]["kept"])
        self.assertEqual(result["version"], work["version"])
        self.error(404, self.store.keep_audio, self.other, work["id"], audio["id"])
        self.error(404, self.store.keep_audio, self.owner, self.work()["id"], audio["id"])

    def test_shares_work_without_export_permission_and_do_not_leak_private_fields(self):
        work = self.lyrics(self.work(story="绝不能公开的故事", intent=self.intent(theme="私密主题")))
        audio = self.audio(work)
        self.assertFalse(audio["export_allowed"])
        share = self.share(work, audio)
        public = self.store.public_share(share["token"])
        self.assertEqual(set(public), {"title", "card_theme", "audio", "expires_at", "ai_generated"})
        self.assertEqual(set(public["audio"]), {"id", "duration", "mime"})
        encoded = json.dumps(public, ensure_ascii=False)
        for private in (work["story"], "私密主题", audio["asset_name"], self.owner,
                        work["current_lyric_id"], "你在雨里等我。"):
            self.assertNotIn(private, encoded)
        self.assertTrue(public["ai_generated"])
        self.assertEqual(self.store.share_asset(share["token"])["asset_name"], audio["asset_name"])
        listed = self.store.get_work(self.owner, work["id"])["shares"]
        self.assertNotIn("token", listed[0])
        self.assertNotIn("token_hash", listed[0])
        self.assertNotIn(share["token"], json.dumps(self.rows("SELECT * FROM shares")))

    def test_show_lyrics_uses_only_selected_audio_snapshot(self):
        work = self.lyrics()
        audio = self.audio(work)
        lines = self.lines(work)
        lines[0]["text"] = "后来修改的私密歌词。"
        self.save(work, lines)
        share = self.share(work, audio, show_lyrics=True)
        public = self.store.public_share(share["token"])
        self.assertEqual(public["audio"]["lyrics"], [{"text": line["text"]} for line in self.lines(work)])
        self.assertNotIn("后来修改的私密歌词", json.dumps(public, ensure_ascii=False))
        self.assertNotIn("locked_spans", json.dumps(public))

    def test_share_requires_confirmation_and_matching_work_audio(self):
        work = self.lyrics()
        audio = self.audio(work)
        self.error(422, self.share, work, audio, confirm=1, code="CONFIRM_REQUIRED")
        self.error(422, self.share, work, audio, show_lyrics="yes")
        self.error(404, self.share, self.work(), audio)
        self.assertEqual(self.rows("SELECT * FROM shares"), [])

    def test_controlled_share_card_validates_title_theme_expiry_and_never_exposes_story(self):
        work = self.lyrics(self.work(story="不公开的故事正文"))
        audio = self.audio(work)
        now = datetime(2026, 9, 1, tzinfo=timezone.utc)
        with patch("backend.domain._now", return_value=now):
            card = self.share(work, audio, card_title="送给朋友的一小段", card_theme="lavender",
                              expires_days=3, show_lyrics=False)
        self.assertEqual(card["card_theme"], "lavender")
        self.assertEqual(card["card_title"], "送给朋友的一小段")
        self.assertEqual(card["expires_at"], (now + timedelta(days=3)).isoformat(timespec="microseconds"))
        with patch("backend.domain._now", return_value=now):
            public = self.store.public_share(card["token"])
        self.assertEqual((public["title"], public["card_theme"]), ("送给朋友的一小段", "lavender"))
        self.assertNotIn("lyrics", public["audio"])
        self.assertNotIn("不公开的故事正文", json.dumps(public, ensure_ascii=False))
        for kwargs in ({"expires_days": 0}, {"expires_days": True}, {"expires_days": 8},
                       {"card_theme": "<script>"}, {"card_title": " "}, {"card_title": "字" * 81}):
            with self.subTest(kwargs=kwargs):
                self.error(422, self.share, work, audio, **kwargs)
        with patch("backend.domain._now", return_value=now + timedelta(days=3)):
            self.error(404, self.store.public_share, card["token"])
            self.error(404, self.store.share_asset, card["token"])

    def test_share_expiry_at_seven_days_and_revoke(self):
        work = self.lyrics()
        audio = self.audio(work)
        now = datetime(2026, 9, 1, tzinfo=timezone.utc)
        with patch("backend.domain._now", return_value=now):
            share = self.share(work, audio)
        with patch("backend.domain._now", return_value=now + timedelta(days=7, microseconds=-1)):
            self.assertTrue(self.store.public_share(share["token"])["ai_generated"])
        with patch("backend.domain._now", return_value=now + timedelta(days=7)):
            self.error(404, self.store.public_share, share["token"])
            self.error(404, self.store.share_asset, share["token"])
        current = self.share(work, audio)
        self.assertEqual(self.store.revoke_share(self.owner, work["id"], current["id"]), {"revoked": True})
        self.assertEqual(self.store.revoke_share(self.owner, work["id"], current["id"]), {"revoked": True})
        self.error(404, self.store.public_share, current["token"])
        self.error(404, self.store.share_asset, current["token"])
        self.error(404, self.store.public_share, "random-token")

    def test_owner_isolation_across_all_owner_endpoints(self):
        work = self.lyrics()
        audio = self.audio(work)
        share = self.share(work, audio)
        job = self.store.get_work(self.owner, work["id"])["jobs"][0]
        calls = [
            (self.store.get_work, (self.other, work["id"])),
            (self.store.update_work, (self.other, work["id"], {"base_version": work["version"]})),
            (self.store.save_lyrics, (self.other, work["id"], {"base_version": work["version"]})),
            (self.store.restore_lyrics, (self.other, work["id"], {"base_version": work["version"]})),
            (self.store.delete_work, (self.other, work["id"], {"base_version": work["version"], "confirm": True})),
            (self.store.create_job, (self.other, work["id"], self.job_body(work, "other"), True)),
            (self.store.get_job, (self.other, job["id"])),
            (self.store.cancel_job, (self.other, job["id"])),
            (self.store.media_asset, (self.other, audio["id"])),
            (self.store.keep_audio, (self.other, work["id"], audio["id"])),
            (self.store.create_share, (self.other, work["id"], {"confirm": True, "audio_id": audio["id"]})),
            (self.store.revoke_share, (self.other, work["id"], share["id"])),
        ]
        for function, arguments in calls:
            with self.subTest(endpoint=function.__name__):
                self.error(404, function, *arguments)
        self.assertEqual(self.store.list_works(self.other), [])
        self.assertTrue(self.store.public_share(share["token"])["ai_generated"])

    def test_events_idempotent_and_client_listen_metrics_not_claimed_verified(self):
        work = self.work()
        body = {"event_id": "event-001", "name": "audio_listen_qualified",
                "properties": {"work_id": work["id"], "covered_ms": 20000, "duration_ms": 25000}}
        first = self.store.add_event(self.owner, body)
        self.assertEqual(first, self.store.add_event(self.owner, body))
        self.assertTrue(first["accepted"])
        self.assertFalse(first["metrics_verified"])
        self.assertEqual(len(self.rows("SELECT e.* FROM events e JOIN event_context c ON e.owner_id=c.owner_id AND e.event_id=c.event_id WHERE c.origin='client'")), 1)
        body["properties"]["covered_ms"] = 21000
        self.error(409, self.store.add_event, self.owner, body, code="IDEMPOTENCY_CONFLICT")
        self.store.add_event(self.other, {"event_id": "event-001", "name": "create_entry_view"})
        self.assertEqual(len(self.rows("SELECT e.* FROM events e JOIN event_context c ON e.owner_id=c.owner_id AND e.event_id=c.event_id WHERE c.origin='client'")), 2)

    def test_event_whitelist_rejects_private_text_and_feedback_body(self):
        invalid = [
            {"name": "arbitrary"},
            {"properties": {"story": "私密正文"}},
            {"properties": {"category": "用户任意正文"}},
            {"properties": {"status": "这里藏着任意文字"}},
            {"properties": {"error_code": "用户私密信息"}},
            {"properties": {"covered_ms": True}},
            {"properties": {"duration_ms": -1}},
            {"text": "私密反馈正文"},
            {"name": "feedback_submitted", "properties": {}},
            {"name": "feedback_submitted", "properties": {"category": "lyrics", "text": "任意反馈"}},
        ]
        for changes in invalid:
            with self.subTest(changes=changes):
                body = {"event_id": "event-001", "name": "create_entry_view", "properties": {}}
                body.update(changes)
                self.error(422, self.store.add_event, self.owner, body)
        accepted = self.store.add_event(self.owner, {"event_id": "feedback-001", "name": "feedback_submitted",
                                                     "properties": {"category": "lyrics"}})
        self.assertTrue(accepted["accepted"])
        self.assertEqual(len(self.rows("SELECT e.* FROM events e JOIN event_context c ON e.owner_id=c.owner_id AND e.event_id=c.event_id WHERE c.origin='client'")), 1)

    def test_event_references_enforce_ownership_and_consistency(self):
        work = self.lyrics()
        audio = self.audio(work)
        job = self.store.get_work(self.owner, work["id"])["jobs"][0]
        for key, value in (("work_id", work["id"]), ("audio_id", audio["id"]), ("job_id", job["id"])):
            self.error(404, self.store.add_event, self.other,
                       {"event_id": "foreign", "name": "create_entry_view", "properties": {key: value}})
        another = self.work()
        self.error(422, self.store.add_event, self.owner,
                   {"event_id": "mixed", "name": "audio_play_started",
                    "properties": {"work_id": another["id"], "audio_id": audio["id"]}},
                   code="EVENT_REFERENCE_CONFLICT")
        self.assertEqual(self.rows("SELECT e.* FROM events e JOIN event_context c ON e.owner_id=c.owner_id AND e.event_id=c.event_id WHERE c.origin='client'"), [])

    def test_delete_clears_all_private_metadata_and_revokes_access_but_keeps_personal_file(self):
        private = "私密标记-不得残留"
        work = self.lyrics(self.work(story=private, intent=self.intent(theme=private)),
                           lines=[{"text": private}, {"text": "第二句。"}])
        audio = self.audio(work)
        share = self.share(work, audio, show_lyrics=True)
        active = self.job(work, key="second")
        for key, value in (("work_id", work["id"]), ("audio_id", audio["id"]), ("job_id", active["id"])):
            self.store.add_event(self.owner, {"event_id": key, "name": "create_entry_view", "properties": {key: value}})
        personal_file = Path(self.directory.name) / audio["asset_name"]
        personal_file.write_bytes(b"personal-file-must-remain")
        self.error(422, self.store.delete_work, self.owner, work["id"],
                   {"base_version": work["version"], "confirm": False})
        self.error(409, self.store.delete_work, self.owner, work["id"],
                   {"base_version": 1, "confirm": True})
        self.assertEqual(self.store.delete_work(self.owner, work["id"],
                                                {"base_version": work["version"], "confirm": True}), {"deleted": True})
        self.assertEqual(personal_file.read_bytes(), b"personal-file-must-remain")
        self.assertEqual(self.store.list_works(self.owner), [])
        for table in ("work_revisions", "audios", "input_clips", "shares", "events"):
            self.assertEqual(self.rows("SELECT * FROM " + table), [])
        tombstone = self.rows("SELECT * FROM works")[0]
        self.assertIsNone(tombstone["owner_id"])
        self.assertIsNone(tombstone["document"])
        self.assertIsNotNone(tombstone["deleted_at"])
        for row in self.rows("SELECT * FROM jobs"):
            for field in ("owner_id", "lyric_id", "style", "snapshot", "idempotency_key", "request_hash", "audio_id"):
                self.assertIsNone(row[field])
            self.assertNotIn(private, json.dumps(row, ensure_ascii=False))
        self.error(404, self.store.media_asset, self.owner, audio["id"])
        self.error(404, self.store.public_share, share["token"])
        self.error(404, self.store.share_asset, share["token"])
        self.error(404, self.store.get_job, self.owner, active["id"])
        self.assertEqual(self.store.complete_job(active["id"], self.asset())["status"], "cancelled")
        self.assertEqual(self.rows("SELECT * FROM quota_usage"), [])

    def test_returned_objects_are_detached_from_persisted_state(self):
        work = self.lyrics()
        work["intent"]["theme"] = "修改返回值"
        work["lyrics"][0]["lines"][0]["text"] = "修改返回值"
        stored = self.store.get_work(self.owner, work["id"])
        self.assertEqual(stored["intent"]["theme"], "感谢")
        self.assertEqual(self.lines(stored)[0]["text"], "你在雨里等我。")
        job = self.job(stored)
        job["snapshot"]["lyrics"][0]["text"] = "修改快照返回值"
        self.assertEqual(self.store.get_job(self.owner, job["id"])["snapshot"]["lyrics"], self.lines(stored))

    def test_concurrent_updates_across_stores_have_single_winner(self):
        work = self.work()
        second = Store(self.path)
        results = self.parallel([
            lambda: self.store.update_work(self.owner, work["id"], {"base_version": 1, "story": "来自线程一"}),
            lambda: second.update_work(self.owner, work["id"], {"base_version": 1, "story": "来自线程二"}),
        ])
        self.assertEqual(sum(isinstance(result, dict) for result in results), 1)
        self.assertEqual([result.code for result in results if isinstance(result, DomainError)], ["VERSION_CONFLICT"])
        self.assertEqual(self.store.get_work(self.owner, work["id"])["version"], 2)
        self.assertEqual(len(self.rows("SELECT * FROM work_revisions")), 2)

    def test_concurrent_idempotent_jobs_create_one_task(self):
        work = self.lyrics()
        stores = [self.store, Store(self.path)] * 3
        results = self.parallel([
            lambda store=store: store.create_job(self.owner, work["id"], self.job_body(work), True)
            for store in stores
        ])
        self.assertTrue(all(isinstance(result, dict) for result in results))
        self.assertEqual(len({result["id"] for result in results}), 1)
        self.assertEqual(len(self.rows("SELECT * FROM jobs")), 1)
        self.assertEqual(len(self.rows("SELECT * FROM jobs")), 1)
        self.assertEqual(self.rows("SELECT * FROM quota_usage"), [])

    def test_concurrent_distinct_jobs_across_stores_have_no_daily_limit(self):
        works = [self.lyrics() for _ in range(6)]
        second = Store(self.path)
        results = self.parallel([
            lambda index=index, work=work: (self.store if index % 2 else second).create_job(
                self.owner, work["id"], self.job_body(work, str(index)), True)
            for index, work in enumerate(works)
        ])
        self.assertTrue(all(isinstance(result, dict) for result in results))
        self.assertEqual(len({result["id"] for result in results}), 6)
        self.assertEqual(len(self.rows("SELECT * FROM jobs")), 6)
        self.assertEqual(self.rows("SELECT * FROM quota_usage"), [])

    def test_concurrent_different_keys_cannot_create_two_active_jobs_for_one_work(self):
        work = self.lyrics()
        second = Store(self.path)
        results = self.parallel([
            lambda: self.store.create_job(self.owner, work["id"], self.job_body(work, "one"), True),
            lambda: second.create_job(self.owner, work["id"], self.job_body(work, "two"), True),
        ])
        self.assertEqual(sum(isinstance(result, dict) for result in results), 1)
        self.assertEqual([result.code for result in results if isinstance(result, DomainError)], ["JOB_ACTIVE"])
        self.assertEqual(len(self.rows("SELECT * FROM jobs")), 1)
        self.assertEqual(self.rows("SELECT * FROM quota_usage"), [])

    def test_atomic_complete_vs_cancel_never_leaves_cancelled_job_with_audio(self):
        work = self.lyrics()
        job = self.checking(work)
        second = Store(self.path)
        results = self.parallel([
            lambda: self.store.complete_job(job["id"], self.asset()),
            lambda: second.cancel_job(self.owner, job["id"]),
        ])
        self.assertTrue(all(isinstance(result, dict) for result in results))
        final = self.store.get_job(self.owner, job["id"])
        self.assertIn(final["status"], {"ready", "cancelled"})
        audios = self.store.get_work(self.owner, work["id"])["audios"]
        self.assertEqual(len(audios), 1 if final["status"] == "ready" else 0)

    def test_default_title_does_not_implicitly_publish_private_story(self):
        work = self.store.create_work(self.owner, {"story": "不能泄露的故事原文", "intent": self.intent(),
                                                   "service_consent": True})
        self.assertEqual(work["title"], "未命名短歌")
        work = self.lyrics(work)
        share = self.share(work, self.audio(work))
        public = self.store.public_share(share["token"])
        self.assertNotIn(work["story"], json.dumps(public, ensure_ascii=False))

    def test_invalid_tokens_and_identifiers_fail_without_encoding_exceptions(self):
        for token in ("\ud800", "", None, [], "a" * 1000):
            with self.subTest(token=repr(token)):
                self.assertIsNone(self.store.session(token))
                self.error(404, self.store.public_share, token)
                self.error(404, self.store.share_asset, token)
        self.error(422, self.store.get_work, self.owner, "\ud800")
        self.error(422, self.store.get_job, self.owner, {"invalid": True})
        self.error(422, self.store.media_asset, self.owner, [])
        self.error(422, self.store.list_works, None)

    def test_database_connection_errors_are_domain_errors(self):
        self.error(503, Store, self.directory.name, code="STORE_UNAVAILABLE")

    def test_add_and_remove_unlocked_lines_preserve_remaining_ids(self):
        work = self.lyrics(lines=[{"text": "第一句。"}, {"text": "第二句。"}, {"text": "第三句。"}])
        lines = self.lines(work)
        changed = self.save(work, [lines[0], lines[2], {"text": "新的第四句。"}])
        self.assertEqual([line["id"] for line in self.lines(changed)[:2]], [lines[0]["id"], lines[2]["id"]])
        self.assertNotIn(self.lines(changed)[-1]["id"], {line["id"] for line in lines})

    def test_atomic_complete_vs_delete_always_ends_without_assets_or_private_content(self):
        work = self.lyrics()
        job = self.checking(work)
        second = Store(self.path)
        results = self.parallel([
            lambda: self.store.complete_job(job["id"], self.asset()),
            lambda: second.delete_work(self.owner, work["id"], {"base_version": work["version"], "confirm": True}),
        ])
        self.assertTrue(all(isinstance(result, dict) for result in results))
        self.assertEqual(self.rows("SELECT * FROM audios"), [])
        self.assertIsNone(self.rows("SELECT document FROM works WHERE id=?", (work["id"],))[0]["document"])
        self.assertIsNone(self.rows("SELECT snapshot FROM jobs WHERE id=?", (job["id"],))[0]["snapshot"])
        self.error(404, self.store.get_work, self.owner, work["id"])
        self.error(404, self.store.get_job, self.owner, job["id"])

    def test_share_title_is_immutable_after_work_renamed_and_store_reopened(self):
        work = self.lyrics()
        audio = self.audio(work)
        share = self.share(work, audio, show_lyrics=True)
        original = self.store.public_share(share["token"])
        changed = self.store.update_work(self.owner, work["id"],
                                         {"base_version": work["version"], "title": "改名后的作品"})
        self.assertEqual(self.store.get_work(self.owner, work["id"])["title"], "改名后的作品")
        self.assertEqual(self.store.public_share(share["token"]), original)
        reopened = Store(self.path)
        self.assertEqual(reopened.public_share(share["token"]), original)
        new_share = self.share(changed, audio)
        self.assertEqual(self.store.public_share(new_share["token"])["title"], changed["title"])
        self.assertEqual(self.rows("SELECT title_snapshot FROM shares WHERE id=?", (share["id"],)),
                         [{"title_snapshot": work["title"]}])

    def test_v1_share_migration_recovers_creation_title_from_revisions_once(self):
        now = datetime(2026, 9, 1, tzinfo=timezone.utc)
        with patch("backend.domain._now", return_value=now):
            work = self.lyrics()
            share = self.share(work, self.audio(work))
        with patch("backend.domain._now", return_value=now + timedelta(minutes=1)):
            renamed = self.store.update_work(self.owner, work["id"],
                                             {"base_version": work["version"], "title": "分享创建后才改的标题"})
        connection = sqlite3.connect(self.path)
        try:
            connection.execute("ALTER TABLE shares DROP COLUMN title_snapshot")
            connection.execute("ALTER TABLE shares DROP COLUMN card_theme")
            connection.execute("DROP TABLE input_clips")
            connection.execute("DROP INDEX jobs_one_active")
            connection.execute("""CREATE UNIQUE INDEX jobs_one_active ON jobs(work_id)
                WHERE status IN ('queued','generating','checking')""")
            connection.execute("DELETE FROM schema_versions WHERE version IN (2, 3, 4)")
            connection.execute("PRAGMA user_version=1")
            connection.commit()
        finally:
            connection.close()
        with patch("backend.domain._now", return_value=now + timedelta(minutes=2)):
            migrated = Store(self.path)
            self.assertEqual(migrated.public_share(share["token"])["title"], work["title"])
            self.assertEqual(migrated.get_work(self.owner, work["id"])["title"], renamed["title"])
            migrated.update_work(self.owner, work["id"],
                                 {"base_version": renamed["version"], "title": "迁移后再次修改"})
            self.assertEqual(Store(self.path).public_share(share["token"])["title"], work["title"])
        self.assertEqual(self.rows("SELECT version FROM schema_versions ORDER BY version"),
                         [{"version": 1}, {"version": 2}, {"version": 3}, {"version": 4}, {"version": 5}, {"version": 6}])
        self.assertEqual(self.rows("PRAGMA foreign_key_check"), [])

    def test_v1_share_without_historical_revision_uses_neutral_title(self):
        work = self.lyrics()
        share = self.share(work, self.audio(work))
        self.store.update_work(self.owner, work["id"],
                               {"base_version": work["version"], "title": "后来填写的私密标题"})
        connection = sqlite3.connect(self.path)
        try:
            connection.execute("ALTER TABLE shares DROP COLUMN title_snapshot")
            connection.execute("ALTER TABLE shares DROP COLUMN card_theme")
            connection.execute("DROP TABLE input_clips")
            connection.execute("DROP INDEX jobs_one_active")
            connection.execute("""CREATE UNIQUE INDEX jobs_one_active ON jobs(work_id)
                WHERE status IN ('queued','generating','checking')""")
            connection.execute("DELETE FROM schema_versions WHERE version IN (2, 3, 4)")
            connection.execute("DELETE FROM work_revisions WHERE work_id=?", (work["id"],))
            connection.execute("PRAGMA user_version=1")
            connection.commit()
        finally:
            connection.close()
        migrated = Store(self.path)
        self.assertEqual(migrated.public_share(share["token"])["title"], "未命名短歌")

    def test_idempotent_replay_across_midnight_does_not_create_another_job(self):
        before = datetime(2026, 9, 30, 15, 59, 59, 999999, tzinfo=timezone.utc)
        work = self.lyrics()
        with patch("backend.domain._now", return_value=before):
            original = self.job(work)
        with patch("backend.domain._now", return_value=before + timedelta(microseconds=1)):
            self.assertEqual(self.job(work), original)
            self.assertEqual(self.job(key="next-day")["status"], "queued")
        self.assertEqual(len(self.rows("SELECT * FROM jobs")), 2)
        self.assertEqual(self.rows("SELECT * FROM quota_usage"), [])

    def test_job_creation_uses_one_clock_sample(self):
        work = self.lyrics()
        before = datetime(2026, 9, 1, 15, 59, 59, 999999, tzinfo=timezone.utc)
        after = before + timedelta(microseconds=1)
        with patch("backend.domain._now", side_effect=[before, after]) as clock:
            job = self.job(work)
        self.assertEqual(clock.call_count, 1)
        self.assertEqual(job["created_at"], before.isoformat(timespec="microseconds"))
        self.assertEqual(self.rows("SELECT * FROM quota_usage"), [])

    def test_status_tracks_current_job_without_writing_work_or_revisions(self):
        work = self.work()
        self.assert_work_status(work, "draft")
        work = self.lyrics(work)
        self.assert_work_status(work, "lyrics_ready")
        stored = self.rows("SELECT document FROM works WHERE id=?", (work["id"],))
        revisions = self.rows("SELECT * FROM work_revisions WHERE work_id=?", (work["id"],))
        job = self.job(work)
        self.assert_work_status(work, "queued")
        for status in ("generating", "checking"):
            self.store.transition_job(job["id"], status)
            self.assert_work_status(work, status)
        self.store.complete_job(job["id"], self.asset())
        self.assert_work_status(work, "ready")
        self.assertEqual(self.rows("SELECT document FROM works WHERE id=?", (work["id"],)), stored)
        self.assertEqual(self.rows("SELECT * FROM work_revisions WHERE work_id=?", (work["id"],)), revisions)

    def test_status_tracks_failure_timeout_cancellation_and_retry(self):
        work = self.lyrics()
        for index, terminal in enumerate(("failed", "timed_out", "cancelled")):
            job = self.job(work, key=str(index))
            self.assert_work_status(work, "queued")
            if terminal == "cancelled":
                self.store.cancel_job(self.owner, job["id"])
            else:
                self.store.transition_job(job["id"], terminal)
            self.assert_work_status(work, terminal)

    def test_status_uses_latest_attempt_even_when_older_matching_audio_exists(self):
        work = self.lyrics()
        self.audio(work)
        self.assert_work_status(work, "ready")
        retry = self.job(work, key="retry")
        self.assert_work_status(work, "queued")
        self.store.transition_job(retry["id"], "failed")
        self.assert_work_status(work, "failed")
        self.assertEqual(len(self.store.get_work(self.owner, work["id"])["audios"]), 1)
        self.audio(work, key="recovered")
        self.assert_work_status(work, "ready")

    def test_new_lyric_revision_does_not_inherit_old_audio_ready_status(self):
        work = self.lyrics()
        old_audio = self.audio(work)
        self.assert_work_status(work, "ready")
        lines = self.lines(work)
        lines[0]["text"] = "全新版本的歌词。"
        changed = self.save(work, lines)
        self.assertEqual(changed["status"], "lyrics_ready")
        self.assert_work_status(changed, "lyrics_ready")
        self.assertEqual(changed["audios"][0]["id"], old_audio["id"])
        self.audio(changed, key="new-lyrics")
        self.assert_work_status(changed, "ready")
        restored = self.store.restore_lyrics(self.owner, work["id"],
                                             {"base_version": changed["version"],
                                              "lyric_id": work["current_lyric_id"]})
        self.assert_work_status(restored, "lyrics_ready")

    def test_status_requires_matching_style_and_survives_non_style_metadata_edits(self):
        work = self.lyrics()
        self.audio(work)
        renamed = self.store.update_work(self.owner, work["id"],
                                         {"base_version": work["version"], "title": "新名字", "story": "新故事"})
        self.assert_work_status(renamed, "ready")
        slow = self.store.update_work(self.owner, work["id"],
                                      {"base_version": renamed["version"], "style": "slow"})
        self.assertEqual(slow["current_lyric_id"], work["current_lyric_id"])
        self.assertEqual(slow["status"], "lyrics_ready")
        self.assert_work_status(slow, "lyrics_ready")
        self.audio(slow, key="slow-song")
        self.assert_work_status(slow, "ready")
        light = self.store.update_work(self.owner, work["id"],
                                       {"base_version": slow["version"], "style": "light"})
        self.assert_work_status(light, "ready")

    def test_old_lyric_tasks_do_not_set_new_lyric_status_before_or_after_completion(self):
        work = self.lyrics()
        job = self.checking(work)
        changed = self.save(work, self.lines(work))
        self.assert_work_status(changed, "lyrics_ready")
        self.store.complete_job(job["id"], self.asset())
        self.assert_work_status(changed, "lyrics_ready")

    def test_old_style_task_failure_does_not_set_current_style_status(self):
        work = self.lyrics()
        job = self.job(work)
        changed = self.store.update_work(self.owner, work["id"],
                                         {"base_version": work["version"], "style": "slow"})
        self.assert_work_status(changed, "lyrics_ready")
        self.store.transition_job(job["id"], "failed")
        self.assert_work_status(changed, "lyrics_ready")
        reverted = self.store.update_work(self.owner, work["id"],
                                          {"base_version": changed["version"], "style": "light"})
        self.assert_work_status(reverted, "failed")

    def test_restart_failure_is_visible_in_current_work_status(self):
        work = self.lyrics()
        self.checking(work)
        self.store = Store(self.path)
        self.assert_work_status(work, "checking")
        self.store.abort_incomplete_jobs()
        self.assert_work_status(work, "failed")

    def test_creation_enforces_title_and_each_intent_unicode_length_limit(self):
        limit_intent = {key: "𝄞" * 500 for key in self.intent()}
        work = self.work(title="𝄞" * 80, intent=limit_intent)
        self.assertEqual(work["title"], "𝄞" * 80)
        self.assertEqual(work["intent"], limit_intent)
        for key in self.intent():
            with self.subTest(field=key):
                self.error(422, self.work, intent=self.intent(**{key: "𝄞" * 501}), code="VALIDATION_ERROR")
        self.error(422, self.work, title="𝄞" * 81, code="VALIDATION_ERROR")
        self.assertEqual(len(self.rows("SELECT * FROM works")), 1)
        self.assertEqual(len(self.rows("SELECT * FROM work_revisions")), 1)

    def test_update_enforces_title_and_each_intent_limit_without_partial_revisions(self):
        work = self.work()
        for key in self.intent():
            with self.subTest(field=key):
                self.error(422, self.store.update_work, self.owner, work["id"],
                           {"base_version": work["version"], "story": "不应被写入",
                            "intent": self.intent(**{key: "界" * 501})}, code="VALIDATION_ERROR")
                self.assertEqual(self.store.get_work(self.owner, work["id"]), work)
        self.error(422, self.store.update_work, self.owner, work["id"],
                   {"base_version": work["version"], "story": "不应被写入", "title": "名" * 81},
                   code="VALIDATION_ERROR")
        self.assertEqual(len(self.rows("SELECT * FROM work_revisions")), 1)
        updated = self.store.update_work(self.owner, work["id"],
                                         {"base_version": work["version"], "title": "名" * 80,
                                          "intent": {key: "界" * 500 for key in self.intent()}})
        self.assertEqual(updated["title"], "名" * 80)
        self.assertEqual(updated["version"], work["version"] + 1)
        self.assertEqual(len(self.rows("SELECT * FROM work_revisions")), 2)
        empty = self.store.update_work(self.owner, work["id"],
                                       {"base_version": updated["version"],
                                        "intent": {key: "" for key in self.intent()}})
        self.assertTrue(all(value == "" for value in empty["intent"].values()))


if __name__ == "__main__":
    unittest.main()
