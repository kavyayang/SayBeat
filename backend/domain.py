"""短歌共创领域层：仅使用标准库；压缩音频借助本机 afinfo 临时探测。"""

import base64
import binascii
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import ntpath
import os
import re
import secrets
import sqlite3
import struct
import subprocess
import tempfile
import threading


class DomainError(Exception):
    def __init__(self, status, code, message):
        self.status = status
        self.code = code
        self.message = message
        super().__init__(message)


STYLES = frozenset({"light", "slow", "groove"})
SOURCES = frozenset({"manual", "ai", "sample"})
ACTIVE_STATUSES = frozenset({"queued", "generating", "checking"})
TERMINAL_STATUSES = frozenset({"ready", "failed", "timed_out", "cancelled"})
INTENT_KEYS = ("theme", "attitude", "preserve", "avoid", "recipient")
EVENT_NAMES = frozenset({
    "create_entry_view", "input_confirmed", "lyric_revision_applied",
    "lyric_line_locked", "lyric_confirmed", "song_job_created",
    "song_job_terminal", "audio_play_started", "audio_listen_qualified",
    "version_kept", "revision_requested", "share_created_or_revoked",
    "export_result", "feedback_submitted", "lyric_generate_result", "transcription_result", "song_generate_result",
})
EVENT_PROPERTIES = frozenset({
    "work_id", "job_id", "audio_id", "status", "category", "covered_ms",
    "duration_ms", "input_mode", "source", "error_code",
})
EVENT_ENUMS = {
    "status": ACTIVE_STATUSES | TERMINAL_STATUSES | {
        "success", "error", "started", "completed", "created", "revoked",
        "allowed", "denied", "blocked", "locked", "unlocked", "kept",
    },
    "category": {
        "lyrics", "music", "voice", "quality", "safety", "other", "like",
        "dislike", "good", "bad", "positive", "negative", "neutral",
        "lyrics_mismatch", "not_singing", "audio_quality", "too_short",
        "too_long", "style_mismatch", "pronunciation", "copyright",
        "privacy", "inappropriate", "technical", "generation_failed",
    },
    "input_mode": {"text", "voice", "sample", "typed", "speech"},
    "source": SOURCES | {"user", "system", "provider", "client", "server"},
}
AUDIO_MIMES = frozenset({
    "audio/mpeg", "audio/mp3", "audio/wav", "audio/x-wav", "audio/ogg",
    "audio/mp4", "audio/webm", "audio/flac", "audio/aac",
})
INPUT_ROLES = frozenset({"story", "instruction"})
INPUT_SOURCES = frozenset({"upload", "record"})
INPUT_MIMES = frozenset({"audio/wav", "audio/mpeg", "audio/mp3", "audio/mp4"})
MAX_INPUT_BYTES = 6 * 1024 * 1024


def _mp4_audio_only(data):
    def boxes(start, end):
        position = start
        while position + 8 <= end:
            size, kind = struct.unpack_from(">I4s", data, position)
            header = 8
            if size == 1:
                if position + 16 > end:
                    raise ValueError("invalid box")
                size = struct.unpack_from(">Q", data, position + 8)[0]
                header = 16
            elif size == 0:
                size = end - position
            if size < header or position + size > end:
                raise ValueError("invalid box")
            yield kind, position + header, position + size
            position += size
        if position != end:
            raise ValueError("trailing bytes")

    try:
        top = list(boxes(0, len(data)))
        if not top or top[0][0] != b"ftyp":
            return False
        moovs = [(start, end) for kind, start, end in top if kind == b"moov"]
        if len(moovs) != 1:
            return False
        tracks = []
        for kind, start, end in boxes(*moovs[0]):
            if kind != b"trak":
                continue
            handlers = []
            for child, lo, hi in boxes(start, end):
                if child == b"mdia":
                    for field, at, stop in boxes(lo, hi):
                        if field == b"hdlr" and stop - at >= 12:
                            handlers.append(data[at + 8:at + 12])
            tracks.append(handlers)
        return len(tracks) == 1 and tracks[0] == [b"soun"]
    except ValueError:
        return False


def _input_duration(data, mime):
    if mime == "audio/wav":
        if len(data) < 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE" or struct.unpack_from("<I", data, 4)[0] != len(data) - 8:
            _fail(422, "AUDIO_INVALID", "WAV文件结构无效")
        pos, fmt, frames = 12, None, None
        while pos + 8 <= len(data):
            size, = struct.unpack_from("<I", data, pos + 4)
            end = pos + 8 + size
            if end + (size % 2) > len(data):
                _fail(422, "AUDIO_INVALID", "WAV数据长度无效")
            kind = data[pos:pos + 4]
            if kind == b"fmt " and fmt is None and size == 16:
                fmt = struct.unpack_from("<HHIIHH", data, pos + 8)
            elif kind == b"data" and frames is None:
                frames = size
            else:
                _fail(422, "AUDIO_INVALID", "仅支持标准PCM16 WAV音频")
            pos = end + (size % 2)
        if pos != len(data) or fmt is None or frames is None:
            _fail(422, "AUDIO_INVALID", "WAV音频不完整")
        encoding, channels, rate, byte_rate, block, bits = fmt
        if (encoding != 1 or channels not in (1, 2) or not 1 <= rate <= 192000
                or bits != 16 or block != channels * 2 or byte_rate != rate * block
                or frames == 0 or frames % block):
            _fail(422, "AUDIO_INVALID", "仅支持PCM16单声道或双声道WAV音频")
        duration = frames / byte_rate
    else:
        if mime in {"audio/mpeg", "audio/mp3"}:
            valid_signature = (data[:3] == b"ID3" or
                               (len(data) >= 2 and data[0] == 0xff and data[1] & 0xe6 == 0xe2))
            expected = {"MPG3"}
            suffix = ".mp3"
        else:
            valid_signature = _mp4_audio_only(data)
            expected = {"m4af", "mp4f", "m4bf"}
            suffix = ".mp4"
        if not valid_signature:
            _fail(422, "AUDIO_INVALID", "音频内容与格式不符或包含视频")
        try:
            with tempfile.NamedTemporaryFile(suffix=suffix) as sample:
                sample.write(data)
                sample.flush()
                probe = subprocess.run(["/usr/bin/afinfo", sample.name], capture_output=True,
                                       text=True, timeout=10, check=False)
        except (OSError, subprocess.TimeoutExpired):
            _fail(422, "AUDIO_INVALID", "无法校验音频内容")
        if probe.returncode:
            _fail(422, "AUDIO_INVALID", "无法解析音频内容")
        file_type = re.search(r"^File type ID:\s*(\S+)", probe.stdout, re.MULTILINE)
        track_count = re.search(r"^Num Tracks:\s*(\d+)", probe.stdout, re.MULTILINE)
        measured = re.search(r"^estimated duration:\s*([\d.]+) sec$", probe.stdout, re.MULTILINE)
        if not file_type or file_type[1] not in expected or not track_count or track_count[1] != "1" or not measured:
            _fail(422, "AUDIO_INVALID", "音频轨道或格式无效")
        duration = float(measured[1])
    if not math.isfinite(duration) or not 0 < duration <= 60:
        _fail(422, "AUDIO_DURATION", "音频时长须大于0且不超过60秒")
    return duration


def _now():
    return datetime.now(timezone.utc)


def _stamp(value=None):
    return (value or _now()).isoformat(timespec="microseconds")


def _dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _id(prefix):
    return prefix + "_" + secrets.token_hex(16)


def _fail(status, code, message):
    raise DomainError(status, code, message)


def _object(value):
    if not isinstance(value, dict):
        _fail(422, "VALIDATION_ERROR", "请求内容必须为对象")
    return value


def _text(value, field, minimum=1, maximum=None):
    if (not isinstance(value, str) or len(value) < minimum
            or (maximum is not None and len(value) > maximum)
            or (minimum and not value.strip())
            or any(0xD800 <= ord(c) <= 0xDFFF for c in value)):
        _fail(422, "VALIDATION_ERROR", field + "格式或长度不正确")
    return value


def _enum(value, allowed, field):
    if not isinstance(value, str) or value not in allowed:
        _fail(422, "VALIDATION_ERROR", field + "不在允许范围内")
    return value


def _intent(value):
    _object(value)
    if set(value) != set(INTENT_KEYS):
        _fail(422, "VALIDATION_ERROR", "意图必须包含且仅包含五个规定字段")
    return {key: _text(value[key], key, minimum=0, maximum=500) for key in INTENT_KEYS}


def _base(body):
    value = body.get("base_version")
    if type(value) is not int or value < 1:
        _fail(422, "VALIDATION_ERROR", "base_version必须为正整数")
    return value


def _check_version(work, body):
    if _base(body) != work["version"]:
        _fail(409, "VERSION_CONFLICT", "作品已变更，请刷新后重试")


def _current(work):
    return next((lyric for lyric in work["lyrics"]
                 if lyric["id"] == work["current_lyric_id"]), None)


def _check_intent(lines, intent):
    text = "\n".join(line["text"] for line in lines)
    preserve = [word.strip() for word in intent["preserve"].splitlines() if word.strip()]
    avoid = [word.strip() for word in intent["avoid"].splitlines() if word.strip()]
    if any(word not in text for word in preserve):
        _fail(422, "INTENT_CONFLICT", "歌词未包含全部必须保留的原文")
    if any(word in text for word in avoid):
        _fail(422, "INTENT_CONFLICT", "歌词包含明确要求避免的词条")


def _protect_lines(previous, lines):
    if not previous:
        return
    old = {line["id"]: line for line in previous}
    new = {line["id"]: line for line in lines}
    for line_id, line in old.items():
        target = new.get(line_id)
        if target is None:
            if line["locked"] or line["locked_spans"]:
                _fail(422, "LOCK_CONFLICT", "不能删除锁定行或含锁定片段的行")
            continue
        if line["locked"] and target["text"] != line["text"]:
            _fail(422, "LOCK_CONFLICT", "锁定行必须逐字保留，包括标点")
        if any(span not in target["text"] for span in line["locked_spans"]):
            _fail(422, "LOCK_CONFLICT", "必须保留当前锁定的片段")
    text_changed = (set(old) != set(new)
                    or any(new[key]["text"] != line["text"]
                           for key, line in old.items() if key in new))
    locks_changed = any(
        line["locked"] != old[key]["locked"]
        or line["locked_spans"] != old[key]["locked_spans"]
        for key, line in new.items() if key in old
    ) or any(line["locked"] or line["locked_spans"]
             for key, line in new.items() if key not in old)
    if text_changed and locks_changed:
        _fail(422, "LOCK_CONFLICT", "请将锁状态变更与文本修改分开提交")


def _parse_lines(raw, previous):
    if not isinstance(raw, list) or not 2 <= len(raw) <= 4:
        _fail(422, "VALIDATION_ERROR", "歌词必须为2至4行")
    old = {line["id"]: line for line in previous}
    lines = []
    seen = set()
    supplied = set()
    has_new = False
    for item in raw:
        _object(item)
        text = _text(item.get("text"), "单行歌词", maximum=160)
        if any(char in text for char in "\r\n\v\f\x85\u2028\u2029"):
            _fail(422, "VALIDATION_ERROR", "单行歌词不能包含换行")
        line_id = item.get("id")
        if line_id is not None:
            _text(line_id, "行ID")
            if line_id not in old:
                _fail(422, "INVALID_LINE_ID", "已有行必须使用当前版本中的稳定ID")
            supplied.add(line_id)
        else:
            line_id = _id("line")
            has_new = True
        if line_id in seen:
            _fail(422, "INVALID_LINE_ID", "行ID不能重复")
        seen.add(line_id)
        prior = old.get(line_id, {"locked": False, "locked_spans": []})
        locked = item.get("locked", prior["locked"])
        spans = item.get("locked_spans", prior["locked_spans"])
        if type(locked) is not bool or not isinstance(spans, list):
            _fail(422, "VALIDATION_ERROR", "锁状态或锁定片段格式错误")
        for span in spans:
            _text(span, "锁定片段", maximum=160)
            if span not in text:
                _fail(422, "LOCK_CONFLICT", "锁定片段必须存在于所在行")
        lines.append({"id": line_id, "text": text, "locked": locked,
                      "locked_spans": list(spans)})
    _protect_lines(previous, lines)
    if old and has_new and not supplied:
        _fail(422, "LINE_ID_REQUIRED", "修改已有歌词必须携带稳定行ID")
    return lines


_SCHEMA = (
    "CREATE TABLE schema_versions(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)",
    """CREATE TABLE sessions(
        token_hash TEXT PRIMARY KEY, owner_id TEXT NOT NULL, csrf TEXT NOT NULL,
        created_at TEXT NOT NULL, expires_at TEXT NOT NULL)""",
    "CREATE INDEX sessions_expiry ON sessions(expires_at)",
    """CREATE TABLE works(
        id TEXT PRIMARY KEY, owner_id TEXT, document TEXT, deleted_at TEXT)""",
    "CREATE INDEX works_owner ON works(owner_id)",
    """CREATE TABLE work_revisions(
        work_id TEXT NOT NULL REFERENCES works(id), version INTEGER NOT NULL,
        snapshot TEXT NOT NULL, created_at TEXT NOT NULL,
        PRIMARY KEY(work_id, version))""",
    """CREATE TABLE jobs(
        id TEXT PRIMARY KEY, work_id TEXT NOT NULL REFERENCES works(id),
        owner_id TEXT, lyric_id TEXT, style TEXT, base_version INTEGER,
        idempotency_key TEXT, request_hash TEXT, snapshot TEXT,
        status TEXT NOT NULL CHECK(status IN
            ('queued','generating','checking','ready','failed','timed_out','cancelled')),
        created_at TEXT, error TEXT, audio_id TEXT,
        UNIQUE(owner_id, idempotency_key))""",
    "CREATE INDEX jobs_work ON jobs(work_id, created_at)",
    """CREATE UNIQUE INDEX jobs_one_active ON jobs(work_id)
        WHERE status IN ('queued','generating','checking')""",
    """CREATE TABLE audios(
        id TEXT PRIMARY KEY, work_id TEXT NOT NULL REFERENCES works(id),
        owner_id TEXT NOT NULL, document TEXT NOT NULL, created_at TEXT NOT NULL)""",
    "CREATE INDEX audios_work ON audios(work_id, created_at)",
    """CREATE TABLE shares(
        id TEXT PRIMARY KEY, work_id TEXT NOT NULL REFERENCES works(id),
        audio_id TEXT NOT NULL REFERENCES audios(id), token_hash TEXT NOT NULL UNIQUE,
        show_lyrics INTEGER NOT NULL, expires_at TEXT NOT NULL,
        revoked INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL)""",
    "CREATE INDEX shares_work ON shares(work_id)",
    # Legacy table retained for compatibility with existing SQLite databases;
    # no new usage is recorded and it never blocks song creation.
    """CREATE TABLE quota_usage(
        owner_id TEXT NOT NULL, day TEXT NOT NULL, used INTEGER NOT NULL,
        PRIMARY KEY(owner_id, day))""",
    """CREATE TABLE events(
        owner_id TEXT NOT NULL, event_id TEXT NOT NULL, name TEXT NOT NULL,
        properties TEXT NOT NULL, request_hash TEXT NOT NULL, work_id TEXT REFERENCES works(id),
        created_at TEXT NOT NULL, PRIMARY KEY(owner_id, event_id))""",
    "CREATE INDEX events_work ON events(work_id)",
    "CREATE INDEX events_name_time ON events(name, created_at)",
)


from .analytics import AnalyticsMixin, SCHEMA as ANALYTICS_SCHEMA


from .creative import CreativeMixin, parse_tempo


from .diary import DiaryMixin, SCHEMA as DIARY_SCHEMA

class Store(AnalyticsMixin,CreativeMixin,DiaryMixin):
    def __init__(self, db_path):
        self._lock = threading.RLock()
        self.db_path = os.fspath(db_path)
        self._uri = self.db_path == ":memory:"
        self._memory_keeper = None
        if self._uri:
            self.db_path = "file:shuoyipai_" + secrets.token_hex(16) + "?mode=memory&cache=shared"
            self._memory_keeper = self._connect()
        with self._lock:
            connection = self._connect()
            try:
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("BEGIN IMMEDIATE")
                version = connection.execute("PRAGMA user_version").fetchone()[0]
                if version > 6:
                    _fail(500, "UNSUPPORTED_SCHEMA", "数据库版本高于当前程序支持的版本")
                if version == 0:
                    for statement in _SCHEMA:
                        connection.execute(statement)
                    connection.execute("INSERT INTO schema_versions VALUES(1, ?)", (_stamp(),))
                    connection.execute("PRAGMA user_version=1")
                    version = 1
                if version == 1:
                    connection.execute(
                        "ALTER TABLE shares ADD COLUMN title_snapshot TEXT NOT NULL DEFAULT '未命名短歌'")
                    # 旧分享按创建时的作品修订回填；没有历史记录时不公开后来修改的标题。
                    connection.execute(
                        """UPDATE shares SET title_snapshot=COALESCE(
                            (SELECT json_extract(snapshot, '$.title') FROM work_revisions
                             WHERE work_id=shares.work_id AND created_at<=shares.created_at
                             ORDER BY version DESC LIMIT 1), '未命名短歌')""")
                    connection.execute("INSERT INTO schema_versions VALUES(2, ?)", (_stamp(),))
                    connection.execute("PRAGMA user_version=2")
                    version = 2
                if version == 2:
                    connection.execute("""CREATE TABLE input_clips(
                        id TEXT PRIMARY KEY, work_id TEXT NOT NULL REFERENCES works(id),
                        owner_id TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN ('story','instruction')),
                        source TEXT NOT NULL CHECK(source IN ('upload','record')),
                        mime TEXT NOT NULL, duration REAL NOT NULL, created_at TEXT NOT NULL,
                        audio BLOB NOT NULL, UNIQUE(work_id, role))""")
                    connection.execute("CREATE INDEX input_clips_owner ON input_clips(owner_id, id)")
                    connection.execute("INSERT INTO schema_versions VALUES(3, ?)", (_stamp(),))
                    connection.execute("PRAGMA user_version=3")
                    version = 3
                if version == 3:
                    # A private TokenHub preview remains in checking until the
                    # owner discards it. Allow one generation in flight while
                    # keeping up to three distinct-style previews for comparison.
                    connection.execute("DROP INDEX jobs_one_active")
                    connection.execute("""CREATE UNIQUE INDEX jobs_one_active ON jobs(work_id)
                        WHERE status IN ('queued','generating')""")
                    connection.execute("ALTER TABLE shares ADD COLUMN card_theme TEXT NOT NULL DEFAULT 'mint'")
                    connection.execute("INSERT INTO schema_versions VALUES(4, ?)", (_stamp(),))
                    connection.execute("PRAGMA user_version=4")
                if version <= 4:
                    for statement in ANALYTICS_SCHEMA:
                        connection.execute(statement)
                    connection.execute("INSERT OR IGNORE INTO schema_versions VALUES(5, ?)", (_stamp(),))
                    connection.execute("PRAGMA user_version=5")
                if version <= 5:
                    for statement in DIARY_SCHEMA:
                        connection.execute(statement)
                    connection.execute("INSERT OR IGNORE INTO schema_versions VALUES(6, ?)", (_stamp(),))
                    connection.execute("PRAGMA user_version=6")
                connection.commit()
            except sqlite3.Error as exc:
                connection.rollback()
                raise DomainError(503, "STORE_UNAVAILABLE", "数据库初始化失败") from exc
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    def _connect(self):
        connection = None
        try:
            connection = sqlite3.connect(self.db_path, timeout=15, isolation_level=None,
                                         check_same_thread=False, uri=self._uri)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA secure_delete=ON")
            return connection
        except sqlite3.Error as exc:
            if connection is not None:
                connection.close()
            raise DomainError(503, "STORE_UNAVAILABLE", "数据库连接失败") from exc

    @contextmanager
    def _transaction(self):
        with self._lock:
            connection = self._connect()
            try:
                # 同一事务完成读、校验和写；不同Store实例也由SQLite串行化写入。
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.commit()
            except sqlite3.Error as exc:
                connection.rollback()
                raise DomainError(503, "STORE_UNAVAILABLE", "数据库暂时不可用") from exc
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    def _work(self, connection, owner, work_id):
        _text(owner, "所有者ID")
        _text(work_id, "作品ID")
        row = connection.execute(
            "SELECT document FROM works WHERE id=? AND owner_id=? AND deleted_at IS NULL",
            (work_id, owner),
        ).fetchone()
        if row is None:
            _fail(404, "NOT_FOUND", "作品不存在")
        return json.loads(row["document"])

    def _persist(self, connection, work, revision=False):
        connection.execute("UPDATE works SET document=? WHERE id=?", (_dump(work), work["id"]))
        if revision:
            snapshot = {key: work[key] for key in (
                "title", "story", "intent", "style", "version", "current_lyric_id",
            )}
            snapshot["lyrics"] = _current(work)
            connection.execute(
                "INSERT INTO work_revisions VALUES(?, ?, ?, ?)",
                (work["id"], work["version"], _dump(snapshot), work["updated_at"]),
            )

    def _job(self, connection, job_id, owner=None):
        _text(job_id, "任务ID")
        row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None or (owner is not None and row["owner_id"] != owner):
            _fail(404, "NOT_FOUND", "任务不存在")
        return row

    @staticmethod
    def _job_dict(row):
        return {
            "id": row["id"], "work_id": row["work_id"], "status": row["status"],
            "lyric_id": row["lyric_id"], "style": row["style"],
            "created_at": row["created_at"],
            "error": json.loads(row["error"]) if row["error"] else None,
            "snapshot": json.loads(row["snapshot"]) if row["snapshot"] else None,
            "audio_id": row["audio_id"],
        }

    @staticmethod
    def _share_dict(row):
        return {"id": row["id"], "audio_id": row["audio_id"],
                "show_lyrics": bool(row["show_lyrics"]), "expires_at": row["expires_at"],
                "card_theme": row["card_theme"], "card_title": row["title_snapshot"],
                "revoked": bool(row["revoked"])}

    def _work_status(self, connection, work):
        if work["current_lyric_id"] is None:
            return "draft"
        # 只反映当前歌词与风格；最新尝试的失败也不能被旧成品掩盖。
        latest = connection.execute(
            """SELECT status FROM jobs WHERE work_id=? AND lyric_id=? AND style=?
                AND COALESCE(json_extract(snapshot,'$.tempo'),'null')=? ORDER BY rowid DESC LIMIT 1""",
            (work["id"], work["current_lyric_id"], work["style"],_dump(work.get("tempo"))),
        ).fetchone()
        if latest is not None and latest["status"] != "ready":
            return latest["status"]
        ready = connection.execute(
            """SELECT 1 FROM audios WHERE work_id=?
                AND json_extract(document, '$.lyric_id')=?
                AND json_extract(document, '$.style')=? AND COALESCE(json_extract(document,'$.tempo'),'null')=? LIMIT 1""",
            (work["id"], work["current_lyric_id"], work["style"],_dump(work.get("tempo"))),
        ).fetchone()
        return "ready" if ready else "lyrics_ready"

    @staticmethod
    def _clip_dict(row):
        return {key: row[key] for key in ("id", "role", "source", "mime", "duration", "created_at")}

    def _detail(self, connection, work):
        work["input_clips"] = [self._clip_dict(row) for row in connection.execute(
            """SELECT id, role, source, mime, duration, created_at FROM input_clips
                WHERE work_id=? ORDER BY created_at, id""", (work["id"],))]
        work["audios"] = [json.loads(row["document"]) for row in connection.execute(
            "SELECT document FROM audios WHERE work_id=? ORDER BY created_at, id", (work["id"],))]
        work["jobs"] = [self._job_dict(row) for row in connection.execute(
            "SELECT * FROM jobs WHERE work_id=? ORDER BY created_at, id", (work["id"],))]
        work["shares"] = [self._share_dict(row) for row in connection.execute(
            "SELECT * FROM shares WHERE work_id=? ORDER BY created_at, id", (work["id"],))]
        work["status"] = self._work_status(connection, work)
        return work

    def create_session(self):
        with self._transaction() as connection:
            token, csrf, owner = secrets.token_urlsafe(32), secrets.token_urlsafe(32), _id("owner")
            now = _now()
            connection.execute("INSERT INTO sessions VALUES(?, ?, ?, ?, ?)",
                               (_hash(token), owner, csrf, _stamp(now), _stamp(now + timedelta(days=30))))
            return {"token": token, "csrf": csrf, "owner_id": owner}

    def session(self, token):
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
            return None
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT owner_id, csrf FROM sessions WHERE token_hash=? AND expires_at>?",
                (_hash(token), _stamp()),
            ).fetchone()
            return dict(row) if row else None

    def create_work(self, owner, body):
        _text(owner, "所有者ID")
        _object(body)
        if body.get("service_consent") is not True:
            _fail(422, "CONSENT_REQUIRED", "必须明确同意使用生成服务")
        story = _text(body.get("story"), "故事", maximum=500)
        intent = _intent(body.get("intent"))
        title = _text(body.get("title", "未命名短歌"), "标题", maximum=80)
        style = _enum(body.get("style", "light"), STYLES, "风格")
        tempo=parse_tempo(body.get("tempo"))
        with self._transaction() as connection:
            return self._create_work(connection, owner, title, story, intent, style, tempo)

    def _create_work(self, connection, owner, title, story, intent, style="light", tempo=None):
        now = _stamp()
        work = {
            "id": _id("work"), "title": title, "story": story, "intent": intent,
            "style": style, "tempo":tempo, "version": 1, "current_lyric_id": None, "lyrics": [],
            "audios": [], "jobs": [], "shares": [], "input_clips": [], "created_at": now,
            "updated_at": now, "status": "draft",
        }
        connection.execute("INSERT INTO works VALUES(?, ?, ?, NULL)",
                           (work["id"], owner, _dump(work)))
        self._persist(connection, work, revision=True)
        self._server_event(connection, owner, "input_confirmed", {"work_id":work["id"], "input_mode":"text"})
        return work

    def list_works(self, owner):
        _text(owner, "所有者ID")
        with self._transaction() as connection:
            works = [json.loads(row["document"]) for row in connection.execute(
                "SELECT document FROM works WHERE owner_id=? AND deleted_at IS NULL", (owner,))]
            works.sort(key=lambda work: (work["updated_at"], work["id"]), reverse=True)
            for work in works:
                work["status"] = self._work_status(connection, work)
            return [{key: work[key] for key in (
                "id", "title", "story", "status", "updated_at", "version",
            )} for work in works]

    def get_work(self, owner, id):
        with self._transaction() as connection:
            return self._detail(connection, self._work(connection, owner, id))

    def update_work(self, owner, id, body):
        _object(body)
        with self._transaction() as connection:
            work = self._work(connection, owner, id)
            _check_version(work, body)
            changes = {}
            if "tempo" in body: changes["tempo"]=parse_tempo(body["tempo"])
            if "story" in body:
                changes["story"] = _text(body["story"], "故事", maximum=500)
            if "intent" in body:
                changes["intent"] = _intent(body["intent"])
            if "style" in body:
                changes["style"] = _enum(body["style"], STYLES, "风格")
            if "title" in body:
                changes["title"] = _text(body["title"], "标题", maximum=80)
            if any(value != work.get(key) for key, value in changes.items()):
                work.update(changes)
                work["version"] += 1
                work["updated_at"] = _stamp()
                self._persist(connection, work, revision=True)
                if "story" in changes:
                    self._server_event(connection, owner, "input_confirmed", {"work_id":id,"input_mode":"text"})
            return self._detail(connection, work)

    def _append_lyrics(self, connection, work, lines, source):
        _check_intent(lines, work["intent"])
        lyric = {
            "id": _id("lyric"), "number": len(work["lyrics"]) + 1,
            "parent_id": work["current_lyric_id"], "source": source,
            "lines": lines, "created_at": _stamp(),
        }
        work["lyrics"].append(lyric)
        work["current_lyric_id"] = lyric["id"]
        work["version"] += 1
        work["updated_at"] = lyric["created_at"]
        work["status"] = "lyrics_ready"
        self._persist(connection, work, revision=True)
        owner=connection.execute("SELECT owner_id FROM works WHERE id=?",(work['id'],)).fetchone()['owner_id']
        before=next((v for v in work['lyrics'] if v['id']==lyric['parent_id']),None)
        text_changed=bool(before and [v['text'] for v in before['lines']] != [v['text'] for v in lines])
        self._server_event(connection,owner,'lyric_revision_applied',{'work_id':work['id'],'lyric_id':lyric['id'],'parent_id':lyric['parent_id'],'version':work['version'],'source':source,'is_change':text_changed})
        if text_changed:
            self._server_event(connection,owner,'revision_requested',{'work_id':work['id'],'category':'lyrics','source':source})
        old_map={line['id']:line for line in before['lines']} if before else {}
        for line in lines:
            previous=old_map.get(line['id'],{'locked':False,'locked_spans':[]})
            if (previous['locked'],previous['locked_spans']) != (line['locked'],line['locked_spans']):
                self._server_event(connection,owner,'lyric_line_locked',{'work_id':work['id'],'lyric_id':lyric['id'],'line_id':line['id'],'status':'locked' if line['locked'] or line['locked_spans'] else 'unlocked'})
        return self._detail(connection, work)

    def save_lyrics(self, owner, id, body):
        _object(body)
        with self._transaction() as connection:
            work = self._work(connection, owner, id)
            _check_version(work, body)
            source = _enum(body.get("source", "manual"), SOURCES, "歌词来源")
            current = _current(work)
            lines = _parse_lines(body.get("lines"), current["lines"] if current else [])
            return self._append_lyrics(connection, work, lines, source)

    def restore_lyrics(self, owner, id, body):
        _object(body)
        with self._transaction() as connection:
            work = self._work(connection, owner, id)
            _check_version(work, body)
            lyric_id = _text(body.get("lyric_id"), "歌词版本ID")
            target = next((lyric for lyric in work["lyrics"] if lyric["id"] == lyric_id), None)
            if target is None:
                _fail(404, "NOT_FOUND", "歌词版本不存在")
            lines = json.loads(_dump(target["lines"]))
            current = _current(work)
            _protect_lines(current["lines"] if current else [], lines)
            return self._append_lyrics(connection, work, lines, target["source"])

    def create_input_clip(self, owner, work_id, body):
        _object(body)
        with self._transaction() as connection:
            self._work(connection, owner, work_id)
        if body.get("confirm") is not True:
            _fail(422, "CONFIRM_REQUIRED", "保存音频素材需要明确确认")
        role = _enum(body.get("role"), INPUT_ROLES, "音频角色")
        source = _enum(body.get("source"), INPUT_SOURCES, "音频来源")
        mime = _enum(body.get("mime"), INPUT_MIMES, "音频类型")
        encoded = body.get("audio_base64")
        if not isinstance(encoded, str) or not encoded or len(encoded) > 8 * 1024 * 1024:
            _fail(413, "AUDIO_SIZE", "音频大小须在100字节至6MiB之间")
        try:
            audio = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            _fail(422, "AUDIO_INVALID", "音频Base64内容无效")
        if not 100 <= len(audio) <= MAX_INPUT_BYTES:
            _fail(413, "AUDIO_SIZE", "音频大小须在100字节至6MiB之间")
        duration = _input_duration(audio, mime)
        with self._transaction() as connection:
            self._work(connection, owner, work_id)
            clip = {"id": _id("clip"), "role": role, "source": source, "mime": mime,
                    "duration": duration, "created_at": _stamp()}
            previous=connection.execute('SELECT id FROM input_clips WHERE work_id=? AND role=?',(work_id,role)).fetchone()
            if previous:
                for row in connection.execute("SELECT id FROM audios WHERE owner_id=? AND json_extract(document,'$.intro.clip_id')=?",(owner,previous['id'])).fetchall():
                    connection.execute('DELETE FROM shares WHERE audio_id=?',(row['id'],))
                    connection.execute("DELETE FROM events WHERE owner_id=? AND json_extract(properties,'$.audio_id')=?",(owner,row['id']))
                    connection.execute('DELETE FROM audios WHERE id=?',(row['id'],))
            connection.execute("DELETE FROM input_clips WHERE work_id=? AND role=?", (work_id, role))
            connection.execute(
                """INSERT INTO input_clips(id, work_id, owner_id, role, source, mime,
                    duration, created_at, audio) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (clip["id"], work_id, owner, role, source, mime, duration, clip["created_at"], audio))
            return clip

    def _input_clip(self, connection, owner, clip_id):
        _text(owner, "所有者ID")
        _text(clip_id, "音频素材ID")
        row = connection.execute(
            """SELECT c.* FROM input_clips c JOIN works w ON w.id=c.work_id
                WHERE c.id=? AND c.owner_id=? AND w.owner_id=? AND w.deleted_at IS NULL""",
            (clip_id, owner, owner),
        ).fetchone()
        if row is None:
            _fail(404, "NOT_FOUND", "音频素材不存在")
        return row

    def input_clip_audio(self, owner, clip_id):
        with self._transaction() as connection:
            row = self._input_clip(connection, owner, clip_id)
            return row["mime"], row["audio"]

    def delete_input_clip(self, owner, clip_id):
        with self._transaction() as connection:
            self._input_clip(connection, owner, clip_id)
            for row in connection.execute("SELECT id FROM audios WHERE owner_id=? AND json_extract(document,'$.intro.clip_id')=?",(owner,clip_id)).fetchall():
                connection.execute('DELETE FROM shares WHERE audio_id=?',(row['id'],))
                connection.execute("DELETE FROM events WHERE owner_id=? AND json_extract(properties,'$.audio_id')=?",(owner,row['id']))
                connection.execute('DELETE FROM audios WHERE id=?',(row['id'],))
            connection.execute("DELETE FROM input_clips WHERE id=?", (clip_id,))
            return {"deleted": True}

    def delete_work(self, owner, id, body):
        _object(body)
        with self._transaction() as connection:
            work = self._work(connection, owner, id)
            _check_version(work, body)
            if body.get("confirm") is not True:
                _fail(422, "CONFIRM_REQUIRED", "删除作品需要明确确认")
            connection.execute("DELETE FROM shares WHERE work_id=?", (id,))
            connection.execute("DELETE FROM input_clips WHERE work_id=?", (id,))
            connection.execute("DELETE FROM audios WHERE work_id=?", (id,))
            connection.execute("DELETE FROM model_calls WHERE work_id=?", (id,))
            connection.execute("DELETE FROM play_studies WHERE work_id=?", (id,))
            connection.execute("DELETE FROM events WHERE work_id=?", (id,))
            connection.execute("DELETE FROM work_revisions WHERE work_id=?", (id,))
            # 留下无法归属个人的任务终态，供迟到的worker回调判断。
            connection.execute(
                """UPDATE jobs SET status=CASE
                    WHEN status IN ('queued','generating','checking') THEN 'cancelled' ELSE status END,
                    owner_id=NULL, lyric_id=NULL, style=NULL, base_version=NULL,
                    idempotency_key=NULL, request_hash=NULL, snapshot=NULL, created_at=NULL,
                    audio_id=NULL, error=? WHERE work_id=?""",
                (_dump({"code": "WORK_DELETED", "message": "作品已删除"}), id),
            )
            connection.execute(
                "UPDATE works SET owner_id=NULL, document=NULL, deleted_at=? WHERE id=?", (_stamp(), id))
            return {"deleted": True}

    def create_job(self, owner, id, body, configured, allow_preview_compare=False):
        _object(body)
        with self._transaction() as connection:
            work = self._work(connection, owner, id)
            base_version = _base(body)
            lyric_id = _text(body.get("lyric_id"), "歌词版本ID")
            key = _text(body.get("idempotency_key"), "幂等键", maximum=128)
            confirmed = body.get("confirmed") is True
            fingerprint = _hash(_dump({"work_id": id, "base_version": base_version,
                                       "lyric_id": lyric_id, "confirmed": confirmed}))
            existing = connection.execute(
                "SELECT * FROM jobs WHERE owner_id=? AND idempotency_key=?", (owner, key)).fetchone()
            if existing:
                if existing["request_hash"] != fingerprint:
                    _fail(409, "IDEMPOTENCY_CONFLICT", "幂等键已用于不同的请求参数")
                return self._job_dict(existing)
            if not confirmed:
                _fail(422, "CONFIRM_REQUIRED", "生成前必须确认歌词")
            if configured is not True:
                _fail(503, "PROVIDER_NOT_CONFIGURED", "尚未配置真实歌曲生成服务")
            _check_version(work, body)
            if work["current_lyric_id"] != lyric_id:
                _fail(409, "LYRIC_VERSION_CONFLICT", "只能对当前已确认的歌词生成歌曲")
            lyric = _current(work)
            if lyric is None:
                _fail(422, "LYRICS_REQUIRED", "请先保存歌词")
            _check_intent(lyric["lines"], work["intent"])
            if connection.execute(
                "SELECT 1 FROM jobs WHERE work_id=? AND status IN ('queued','generating')" +
                ("" if allow_preview_compare else " OR work_id=? AND status='checking'"),
                (id,) if allow_preview_compare else (id,id),
            ).fetchone():
                _fail(409, "JOB_ACTIVE", "此作品已有正在处理的任务")
            if allow_preview_compare:
                previews = connection.execute(
                    "SELECT style, lyric_id, snapshot FROM jobs WHERE work_id=? AND status='checking'", (id,)).fetchall()
                if len(previews) >= 3:
                    _fail(409, "COMPARE_FULL", "最多同时保留三种待试听感觉；请先丢弃一版")
                if any(row["lyric_id"] == lyric_id and row["style"] == work["style"] and json.loads(row["snapshot"]).get("tempo")==work.get("tempo") for row in previews):
                    _fail(409, "STYLE_EXISTS", "同一版歌词的这个感觉已有待试听候选；请先对比或丢弃")
            now = _now()
            snapshot = {"lyric_id": lyric_id, "lyrics": lyric["lines"],
                        "intent": work["intent"], "style": work["style"], "tempo":work.get("tempo"), "work_version": work["version"]}
            job_id = _id("job")
            connection.execute(
                """INSERT INTO jobs(id, work_id, owner_id, lyric_id, style, base_version,
                    idempotency_key, request_hash, snapshot, status, created_at)
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?)""",
                (job_id, id, owner, lyric_id, work["style"], base_version,
                 key, fingerprint, _dump(snapshot), _stamp(now)),
            )
            if work['audios'] or connection.execute('SELECT 1 FROM audios WHERE work_id=?',(id,)).fetchone():
                self._server_event(connection,owner,'revision_requested',{'work_id':id,'category':'music','job_id':job_id})
            self._server_event(connection,owner,'lyric_confirmed',{'work_id':id,'lyric_id':lyric_id,'version':base_version},'confirmed_'+job_id,now=now)
            self._server_event(connection,owner,'song_job_created',{'work_id':id,'job_id':job_id,'status':'queued'},'created_'+job_id,now=now)
            return self._job_dict(self._job(connection, job_id))

    def get_job(self, owner, jobid):
        _text(owner, "所有者ID")
        with self._transaction() as connection:
            return self._job_dict(self._job(connection, jobid, owner))

    def cancel_job(self, owner, jobid):
        _text(owner, "所有者ID")
        with self._transaction() as connection:
            job = self._job(connection, jobid, owner)
            if job["status"] in ACTIVE_STATUSES:
                connection.execute("UPDATE jobs SET status='cancelled', error=? WHERE id=?",
                                   (_dump({"code": "USER_CANCELLED", "message": "用户已取消"}), jobid))
                self._server_event(connection,owner,'song_job_terminal',{'work_id':job['work_id'],'job_id':jobid,'status':'cancelled'},'terminal_'+jobid)
            return self._job_dict(self._job(connection, jobid, owner))

    def transition_job(self, jobid, status, error=None):
        _enum(status, ACTIVE_STATUSES | TERMINAL_STATUSES, "任务状态")
        with self._transaction() as connection:
            job = self._job(connection, jobid)
            if job["status"] in TERMINAL_STATUSES:
                return self._job_dict(job)
            allowed = {"queued": "generating", "generating": "checking"}
            if status == "ready" or (status not in {"failed", "timed_out", "cancelled"}
                                     and allowed.get(job["status"]) != status):
                _fail(409, "INVALID_JOB_STATE", "任务不能执行此状态迁移")
            if error is not None and status in ACTIVE_STATUSES:
                _fail(422, "VALIDATION_ERROR", "非终态任务不能包含错误")
            if error is None and status in {"failed", "timed_out", "cancelled"}:
                error = {"code": status.upper(), "message": "任务未完成"}
            elif isinstance(error, str):
                error = {"code": status.upper(), "message": error}
            elif error is not None:
                _object(error)
                error = {"code": _text(error.get("code"), "错误代码", maximum=128),
                         "message": _text(error.get("message"), "错误说明", minimum=0)}
            connection.execute("UPDATE jobs SET status=?, error=? WHERE id=?",
                               (status, _dump(error) if error is not None else None, jobid))
            if status in TERMINAL_STATUSES:
                self._server_event(connection,job['owner_id'],'song_job_terminal',{'work_id':job['work_id'],'job_id':jobid,'status':status,'error_code':(error or {}).get('code')},'terminal_'+jobid)
            return self._job_dict(self._job(connection, jobid))

    def complete_job(self, jobid, asset):
        with self._transaction() as connection:
            return self._complete_job(connection, jobid, asset)

    def register_candidate(self, owner, jobid, metadata, path):
        with self._transaction() as connection:
            job = self._job(connection, jobid, owner)
            if job['status'] != 'generating':
                _fail(409, 'INVALID_JOB_STATE', '任务已经结束，不能接收候选')
            snapshot = json.loads(job['snapshot'])
            metadata = metadata if isinstance(metadata, dict) else {}
            snapshot['candidate'] = {key: metadata.get(key) for key in (
                'duration_seconds_reported', 'duration_seconds_probed', 'size_bytes', 'request_id', 'response_format')}
            with open(path, 'rb') as stream:
                snapshot['candidate']['sha256'] = hashlib.sha256(stream.read()).hexdigest()
            snapshot['candidate']['created_at'] = _stamp()
            snapshot['reviews'] = []
            connection.execute('UPDATE jobs SET snapshot=? WHERE id=?', (_dump(snapshot), jobid))

    def review_candidate(self, owner, jobid, body, inspection):
        from .audio_review import words
        _object(body)
        with self._transaction() as connection:
            job = self._job(connection, jobid, owner)
            self._work(connection, owner, job['work_id'])
            if job['status'] == 'ready':
                return {'approved': True, 'job': self._job_dict(job), 'replayed': True}
            if job['status'] != 'checking':
                _fail(409, 'INVALID_JOB_STATE', '候选不在待审核状态')
            snapshot = json.loads(job['snapshot'])
            variant = _enum(body.get('variant', 'original'), {'original','short'}, '试听版本')
            selected = snapshot.get('prepared' if variant == 'short' else 'candidate', {})
            digest = selected.get('sha256')
            if not digest or digest != body.get('sha256') or digest != inspection.get('sha256'):
                _fail(409, 'CANDIDATE_CHANGED', '候选已改变，请重新读取和试听')
            if body.get('confirm') is not True:
                _fail(422, 'CONFIRM_REQUIRED', '请明确提交此次人工审核')
            text = _text(body.get('heard_lyrics'), '实际听到的全部歌词', maximum=2000)
            licence = _text(body.get('rights_reference'), '使用权依据/记录', minimum=1, maximum=1000)
            flags = ('singing', 'no_missing_words', 'no_extra_words', 'quality', 'complete_ending',
                     'input_rights', 'output_rights', 'display_allowed', 'share_allowed', 'export_allowed')
            if any(type(body.get(key)) is not bool for key in flags):
                _fail(422, 'VALIDATION_ERROR', '请逐项明确审核结果和使用范围')
            expected = '\n'.join(line['text'] for line in snapshot['lyrics'])
            reasons = []
            if words(text) != words(expected):
                reasons.append('LYRICS_MISMATCH')
            for key in flags[:7]:
                if not body[key]:
                    reasons.append(key.upper() + '_FAILED')
            if not body['display_allowed']:
                reasons.append('DISPLAY_NOT_LICENSED')
            if variant == 'short' and body.get('processing_rights') is not True:
                reasons.append('PROCESSING_NOT_LICENSED')
            if not 15 <= inspection['duration'] <= 30:
                reasons.append('TOO_SHORT' if inspection['duration'] < 15 else 'TOO_LONG')
            audit = {key: body[key] for key in flags}
            audit.update(id=_id('review'), reviewed_at=_stamp(), reviewer_id=owner,
                method='owner_listening_attestation', heard_lyrics=text, expected_lyrics=expected,
                lyrics_match=words(text) == words(expected), rights_reference=licence,
                rights_method='owner_attestation', inspection=inspection, rejected_reasons=reasons)
            audit.update(variant=variant, processing=selected.get('processing'),
                processing_rights=body.get('processing_rights',False), source_sha256=selected.get('source_sha256',digest))
            snapshot['reviews'] = (snapshot.get('reviews', []) + [audit])[-20:]
            connection.execute('UPDATE jobs SET snapshot=? WHERE id=?', (_dump(snapshot), jobid))
            if reasons:
                return {'approved': False, 'reasons': reasons, 'job': self._job_dict(self._job(connection, jobid))}
            completed = self._complete_job(connection, jobid, {
                'verified_singing': True, 'lyrics_match': True, 'duration': inspection['duration'],
                'asset_name': selected['asset_name'] if variant == 'short' else jobid + '.candidate.mp3',
                'mime': selected['mime'] if variant == 'short' else 'audio/mpeg',
                'export_allowed': body['export_allowed'], 'audit': audit})
            return {'approved': True, 'job': completed}

    def register_prepared(self, owner, jobid, prepared):
        with self._transaction() as connection:
            job = self._job(connection, jobid, owner)
            if job['status'] != 'checking':
                _fail(409,'INVALID_JOB_STATE','任务已结束，不能接收调速候选')
            snapshot = json.loads(job['snapshot'])
            if snapshot.get('candidate',{}).get('sha256') != prepared['source_sha256']:
                _fail(409,'CANDIDATE_CHANGED','原始候选已经改变')
            if snapshot.get('prepared'):
                _fail(409,'OUTPUT_EXISTS','已有调速候选，请刷新后试听')
            snapshot['prepared'] = dict(prepared,created_at=_stamp(),processing_rights_attested=True)
            connection.execute('UPDATE jobs SET snapshot=? WHERE id=?',(_dump(snapshot),jobid))
            return self._job_dict(self._job(connection,jobid))

    def _complete_job(self, connection, jobid, asset):
        job = self._job(connection, jobid)
        if job["status"] in TERMINAL_STATUSES:
            return self._job_dict(job)
        if job["status"] != "checking":
            _fail(409, "INVALID_JOB_STATE", "仅检查中的任务可以完成")
        _object(asset)
        if asset.get("verified_singing") is not True or asset.get("lyrics_match") is not True:
            _fail(422, "INVALID_ASSET", "音频必须通过歌唱与歌词匹配检查")
        duration = asset.get("duration")
        if (type(duration) not in (int, float) or not 15 <= duration <= 30
                or not math.isfinite(duration)):
            _fail(422, "INVALID_ASSET", "歌曲时长必须为15至30秒")
        name = _text(asset.get("asset_name"), "音频文件名", maximum=255)
        if (name in {".", ".."} or name != name.strip() or name.startswith(".")
                or ntpath.basename(name) != name or os.path.basename(name) != name
                or any(char in name for char in "/\\:")
                or any(ord(char) < 32 or ord(char) == 127 for char in name)):
            _fail(422, "INVALID_ASSET", "音频文件名必须为安全的basename")
        mime = _enum(asset.get("mime"), AUDIO_MIMES, "音频类型")
        export_allowed = asset.get("export_allowed", False)
        if type(export_allowed) is not bool:
            _fail(422, "INVALID_ASSET", "导出授权必须为布尔值")
        snapshot = json.loads(job["snapshot"])
        audio = {"id": _id("audio"), "lyric_id": job["lyric_id"],
                 "lyrics": snapshot["lyrics"], "style": snapshot["style"],
                 "duration": duration, "asset_name": name, "mime": mime,
                 "tempo":json.loads(job["snapshot"]).get("tempo"), "export_allowed": export_allowed, "kept": False, "created_at": _stamp()}
        if asset.get("audit"):
            audio.update(audit=asset["audit"], share_allowed=asset["audit"]["share_allowed"],
                         display_allowed=asset["audit"]["display_allowed"])
        connection.execute("INSERT INTO audios VALUES(?, ?, ?, ?, ?)",
                           (audio["id"], job["work_id"], job["owner_id"], _dump(audio), audio["created_at"]))
        connection.execute("UPDATE jobs SET status='ready', error=NULL, audio_id=? WHERE id=?",
                           (audio["id"], jobid))
        self._server_event(connection,job['owner_id'],'song_job_terminal',{'work_id':job['work_id'],'job_id':jobid,'audio_id':audio['id'],'status':'ready'},'terminal_'+jobid)
        return self._job_dict(self._job(connection, jobid))

    def abort_incomplete_jobs(self, preserved_candidates=()):
        with self._transaction() as connection:
            rows = connection.execute(
                "SELECT id FROM jobs WHERE status IN ('queued','generating','checking') ORDER BY created_at, id"
            ).fetchall()
            aborted = []
            for row in rows:
                job = self._job(connection, row['id'])
                snapshot = json.loads(job['snapshot'])
                if (job['status'] == 'checking' and row['id'] in preserved_candidates
                        and snapshot.get('candidate', {}).get('sha256')):
                    continue
                connection.execute("UPDATE jobs SET status='failed', error=? WHERE id=?",
                    (_dump({"code": "SERVER_RESTARTED", "message": "服务已重启，请重新发起生成"}), row['id']))
                self._server_event(connection,job['owner_id'],'song_job_terminal',{'work_id':job['work_id'],'job_id':job['id'],'status':'failed','error_code':'SERVER_RESTARTED'},'terminal_'+job['id'])
                aborted.append(self._job_dict(self._job(connection, row['id'])))
            return aborted

    def _audio(self, connection, owner, audio_id, work_id=None):
        _text(owner, "所有者ID")
        _text(audio_id, "音频ID")
        row = connection.execute(
            """SELECT a.* FROM audios a JOIN works w ON w.id=a.work_id
                WHERE a.id=? AND a.owner_id=? AND w.owner_id=? AND w.deleted_at IS NULL""",
            (audio_id, owner, owner),
        ).fetchone()
        if row is None or (work_id is not None and row["work_id"] != work_id):
            _fail(404, "NOT_FOUND", "音频不存在")
        return row

    def keep_audio(self, owner, workid, audioid):
        with self._transaction() as connection:
            work = self._work(connection, owner, workid)
            row = self._audio(connection, owner, audioid, workid)
            audio = json.loads(row["document"])
            if not audio.get("kept"):
                self._server_event(connection,owner,"version_kept",{"work_id":workid,"audio_id":audioid,"status":"kept"},"kept_"+audioid)
            audio["kept"] = True
            connection.execute("UPDATE audios SET document=? WHERE id=?", (_dump(audio), audioid))
            return self._detail(connection, work)

    def create_share(self, owner, workid, body):
        _object(body)
        with self._transaction() as connection:
            work = self._work(connection, owner, workid)
            if body.get("confirm") is not True:
                _fail(422, "CONFIRM_REQUIRED", "创建分享需要明确确认")
            audio_id = _text(body.get("audio_id"), "音频ID")
            audio = json.loads(self._audio(connection, owner, audio_id, workid)['document'])
            if audio.get('share_allowed', True) is not True:
                _fail(403, 'SHARE_FORBIDDEN', '本音频的使用权记录未允许分享')
            show = body.get("show_lyrics", False)
            if type(show) is not bool:
                _fail(422, "VALIDATION_ERROR", "show_lyrics必须为布尔值")
            if show and audio.get('display_allowed', True) is not True:
                _fail(403, 'DISPLAY_FORBIDDEN', '使用权记录不允许展示歌词')
            days = body.get("expires_days", 7)
            if type(days) is not int or days not in (1, 3, 7):
                _fail(422, "VALIDATION_ERROR", "分享期限仅支持1、3或7天")
            theme = _enum(body.get("card_theme", "mint"), {"mint", "lavender", "cream"}, "分享卡配色")
            title = _text(body.get("card_title", work["title"]), "分享标题", maximum=80)
            token, share_id, now = secrets.token_urlsafe(32), _id("share"), _now()
            connection.execute(
                """INSERT INTO shares(id, work_id, audio_id, token_hash, show_lyrics,
                    expires_at, revoked, created_at, title_snapshot, card_theme)
                    VALUES(?, ?, ?, ?, ?, ?, 0, ?, ?, ?)""",
                (share_id, workid, audio_id, _hash(token), int(show),
                 _stamp(now + timedelta(days=days)), _stamp(now), title, theme))
            result = self._share_dict(connection.execute("SELECT * FROM shares WHERE id=?", (share_id,)).fetchone())
            self._server_event(connection,owner,"share_created_or_revoked",{"work_id":workid,"audio_id":audio_id,"status":"created","share_id":share_id})
            result["token"] = token
            return result

    def _valid_share(self, connection, token):
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
            _fail(404, "NOT_FOUND", "分享不存在或已失效")
        row = connection.execute(
            """SELECT s.*, a.document AS audio
                FROM shares s JOIN audios a ON a.id=s.audio_id JOIN works w ON w.id=s.work_id
                WHERE s.token_hash=? AND s.revoked=0 AND s.expires_at>? AND w.deleted_at IS NULL""",
            (_hash(token), _stamp()),
        ).fetchone()
        if row is None:
            _fail(404, "NOT_FOUND", "分享不存在或已失效")
        return row

    def public_share(self, token):
        with self._transaction() as connection:
            row = self._valid_share(connection, token)
            stored_audio = json.loads(row["audio"])
            audio = {key: stored_audio[key] for key in ("id", "duration", "mime")}
            if stored_audio.get('kind')=='voice_intro':
                audio.update(contains_original_voice=True,intro_duration=stored_audio['intro']['duration'],song_duration=stored_audio['song_duration'])
            if row["show_lyrics"]:
                # 对外只给可展示歌词，不暴露内部稳定ID或锁定片段。
                audio["lyrics"] = [{"text": line["text"]} for line in stored_audio["lyrics"]]
            return {"title": row["title_snapshot"], "card_theme": row["card_theme"],
                    "audio": audio, "expires_at": row["expires_at"], "ai_generated": True}

    def revoke_share(self, owner, workid, shareid):
        _text(shareid, "分享ID")
        with self._transaction() as connection:
            self._work(connection, owner, workid)
            changed = connection.execute("UPDATE shares SET revoked=1 WHERE id=? AND work_id=?",
                                         (shareid, workid)).rowcount
            if not changed:
                _fail(404, "NOT_FOUND", "分享不存在")
            self._server_event(connection,owner,"share_created_or_revoked",{"work_id":workid,"status":"revoked","share_id":shareid},"revoked_"+shareid)
            return {"revoked": True}

    def media_asset(self, owner, audioid):
        with self._transaction() as connection:
            row=self._audio(connection, owner, audioid)
            return dict(json.loads(row["document"]),work_id=row["work_id"])

    def share_asset(self, token):
        with self._transaction() as connection:
            return json.loads(self._valid_share(connection, token)["audio"])

    def add_event(self, owner, body):
        _text(owner, "所有者ID")
        _object(body)
        if set(body) - {"event_id", "name", "properties", "client_at"}:
            _fail(422, "VALIDATION_ERROR", "事件不能携带正文或其他字段")
        client_at=body.get('client_at')
        if client_at is not None:
            try:
                if not isinstance(client_at,str) or len(client_at)>40 or datetime.fromisoformat(client_at.replace('Z','+00:00')).tzinfo is None: raise ValueError()
            except (ValueError,TypeError): _fail(422,'VALIDATION_ERROR','客户端时间必须为带时区的 ISO 时间')
        event_id = _text(body.get("event_id"), "事件ID", maximum=128)
        if not re.fullmatch(r"[A-Za-z0-9_-]+", event_id):
            _fail(422, "VALIDATION_ERROR", "事件ID必须为不含文本的机器标识")
        name = _enum(body.get("name"), EVENT_NAMES, "事件名")
        properties = _object(body.get("properties", {}))
        if set(properties) - EVENT_PROPERTIES:
            _fail(422, "VALIDATION_ERROR", "事件包含不允许的属性")
        for key, value in properties.items():
            if key in EVENT_ENUMS:
                _enum(value, EVENT_ENUMS[key], key)
            elif key in {"covered_ms", "duration_ms"}:
                if type(value) is not int or not 0 <= value <= 86_400_000:
                    _fail(422, "VALIDATION_ERROR", "试听时长必须为非负毫秒数且不超过一天")
            elif key == "error_code":
                if not isinstance(value, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", value):
                    _fail(422, "VALIDATION_ERROR", "错误码必须为机器代码")
            else:
                _text(value, key, maximum=128)
        if name == "feedback_submitted" and "category" not in properties:
            _fail(422, "VALIDATION_ERROR", "反馈只能提交规定类别，且类别不能为空")
        fingerprint = _hash(_dump({"name": name, "properties": properties}))
        with self._transaction() as connection:
            work_ids = set()
            if "work_id" in properties:
                work_ids.add(self._work(connection, owner, properties["work_id"])["id"])
            job = None
            if "job_id" in properties:
                job = self._job(connection, properties["job_id"], owner)
                work_ids.add(job["work_id"])
            if "audio_id" in properties:
                audio = self._audio(connection, owner, properties["audio_id"])
                work_ids.add(audio["work_id"])
                if job is not None and job["audio_id"] != audio["id"]:
                    _fail(422, "EVENT_REFERENCE_CONFLICT", "任务与音频不匹配")
            if len(work_ids) > 1:
                _fail(422, "EVENT_REFERENCE_CONFLICT", "事件关联对象不属于同一作品")
            existing = connection.execute(
                "SELECT request_hash FROM events WHERE owner_id=? AND event_id=?", (owner, event_id)).fetchone()
            if existing and existing["request_hash"] != fingerprint:
                _fail(409, "IDEMPOTENCY_CONFLICT", "事件ID已用于不同的内容")
            if not existing:
                connection.execute("INSERT INTO events VALUES(?, ?, ?, ?, ?, ?, ?)",
                                   (owner, event_id, name, _dump(properties), fingerprint,
                                    next(iter(work_ids), None), _stamp()))
            self._event_context(connection,owner,event_id,origin="client",client_at=client_at)
            # 客户端上报只能说明收到指标，不代表服务端验证过实际试听行为。
            return {"event_id": event_id, "accepted": True, "metrics_verified": False}
