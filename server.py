#!/usr/bin/env python3
"""Local-only prototype server. Run: python3 server.py --port 8765.
No external packages. Put credentials in server environment, never browser code.
Use a production WSGI/ASGI stack + TLS/auth/rate limits before public deployment.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hmac
import hashlib
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import threading
import time
from urllib.parse import urlsplit, parse_qs, unquote
from backend.domain import Store, DomainError, _parse_lines, _check_intent, _base, _text
from backend.providers import Gateway, MurekaCandidate, TokenHubCandidate, TokenHubPreviewGateway, load_tokenhub_key
from backend.composition import compose_intro
from backend.audio_review import inspect_mp3, inspect_wav, prepare_short

ROOT = Path(__file__).resolve().parent
ACTIVE = {"queued", "generating", "checking"}


class Application:
    def __init__(self, data_dir=None, gateway=None):
        self.data_dir = Path(data_dir or ROOT / "data")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir.chmod(0o700)
        self.assets = self.data_dir / "assets"
        if not self.assets.is_dir():
            self.assets.mkdir(exist_ok=True)
        self.assets.chmod(0o700)
        database = self.data_dir / "shuoyipai.sqlite3"
        if not database.exists():
            database.touch(mode=0o600)
        database.chmod(0o600)
        self.store = Store(database)
        self.store.abort_pending_calls()
        preserved = {path.name.removesuffix('.candidate.mp3') for path in self.assets.glob('*.candidate.mp3')}
        aborted = self.store.abort_incomplete_jobs(preserved)
        for job in aborted:
            (self.assets / (job["id"] + ".candidate.mp3")).unlink(missing_ok=True)
            (self.assets / (job["id"] + ".short.wav")).unlink(missing_ok=True)
        if gateway is not None:
            self.gateway = gateway
        elif os.getenv("AI_GATEWAY_URL"):
            self.gateway = Gateway()
        else:
            key = load_tokenhub_key()
            # Do not pin the Key in the process: TokenHubCandidate re-reads the
            # private configuration immediately before each confirmed request.
            self.gateway = TokenHubPreviewGateway() if key else Gateway()
        self.pool = ThreadPoolExecutor(max_workers=2)
        self.lock = threading.Lock()
        self.jobs = set()
        self.closed = False
        self.limits = {}
        self.ai_slots = threading.BoundedSemaphore(2)

    def close(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            jobs = tuple(self.jobs)
        # 等待线程退出时不能持有_run清理队列所需的锁。
        try:
            for jid in jobs:
                self.store.transition_job(jid, "failed", {"code": "SERVER_SHUTDOWN", "message": "服务已关闭，请重新发起生成"})
        finally:
            self.pool.shutdown(wait=True, cancel_futures=True)
            with self.lock:
                self.jobs.clear()

    def rate_limit(self, key, count=90):
        now = time.monotonic()
        with self.lock:
            bucket = [t for t in self.limits.get(key, []) if now - t < 60]
            if len(bucket) >= count:
                raise DomainError(429, "RATE_LIMIT", "操作太频繁，请稍后再试")
            self.limits[key] = bucket + [now]
            if len(self.limits) > 2000:
                self.limits = {k: v for k, v in self.limits.items() if v and now - v[-1] < 60}

    def song(self, owner, wid, body):
        with self.lock:
            if self.closed:
                raise DomainError(503, "SERVER_CLOSED", "服务已关闭，不能发起生成")
            if len(self.jobs) >= 8:
                raise DomainError(429, "QUEUE_FULL", "生成队列已满，请稍后重试")
            if isinstance(self.gateway, TokenHubPreviewGateway) and body.get("paid_call_confirmed") is not True:
                raise DomainError(422, "PAID_CALL_CONFIRMATION_REQUIRED", "请确认本次请求可能产生供应商费用")
            job = self.store.create_job(owner, wid, body, self.gateway.enabled("singing"),
                                        allow_preview_compare=isinstance(self.gateway, TokenHubPreviewGateway))
            # Idempotent replay of an already-running/checking job must not resubmit
            # a potentially billable provider request after the local worker exits.
            if job["status"] == "queued" and job["id"] not in self.jobs:
                self.jobs.add(job["id"])
                try:
                    self.pool.submit(self._run, owner, job)
                except RuntimeError:
                    self.jobs.discard(job["id"])
                    self.store.transition_job(job["id"], "failed", {"code": "WORKER_UNAVAILABLE", "message": "生成线程不可用，请重试"})
                    raise DomainError(503, "WORKER_UNAVAILABLE", "生成线程不可用，请重试") from None
        return job

    def _run(self, owner, job):
        try:
            self.measured(owner,"music",lambda:self.gateway.run_song(self.store, owner, job, self.assets),job["work_id"],job["id"])
        except Exception:
            self.store.transition_job(job["id"], "failed", {"code": "GENERATION_FAILED", "message": "演唱生成失败，歌词已保留"})
        finally:
            with self.lock:
                self.jobs.discard(job["id"])

    def work_view(self, owner, wid):
        work = self.store.get_work(owner, wid)
        for job in work['jobs']:
            job['preview_ready'] = (job['status']=='checking' and
                bool((job.get('snapshot') or {}).get('candidate',{}).get('sha256')) and
                (self.assets/(job['id']+'.candidate.mp3')).is_file())
        return work

    def job_view(self, owner, jid):
        job = self.store.get_job(owner, jid)
        job['preview_ready'] = (job['status']=='checking' and
            bool((job.get('snapshot') or {}).get('candidate',{}).get('sha256')) and
            (self.assets/(jid+'.candidate.mp3')).is_file())
        return job

    def preview_asset(self, owner, jid, variant='original'):
        job = self.store.get_job(owner, jid)
        if job["status"] != "checking" or not (job.get('snapshot') or {}).get('candidate',{}).get('sha256'):
            raise DomainError(404, "NOT_FOUND", "候选音频不存在或已取消")
        name = jid + ".candidate.mp3"
        mime = 'audio/mpeg'
        if variant == 'short':
            prepared = job['snapshot'].get('prepared')
            if not prepared:
                raise DomainError(404,'NOT_FOUND','没有调速候选')
            name, mime = prepared['asset_name'], prepared['mime']
        elif variant != 'original':
            raise DomainError(422,'VALIDATION_ERROR','未知试听版本')
        if not (self.assets / name).is_file():
            raise DomainError(404, "NOT_FOUND", "候选音频尚未返回")
        return {"asset_name": name, "mime": mime, "export_allowed": False}

    def review_audio(self, owner, jid, body):
        job = self.store.get_job(owner, jid)
        if job['status'] == 'ready':
            return {'approved': True, 'job': job, 'replayed': True}
        asset = self.preview_asset(owner, jid, body.get('variant','original'))
        inspection = (inspect_wav if asset['mime']=='audio/wav' else inspect_mp3)(self.assets / asset['asset_name'])
        return self.store.review_candidate(owner, jid, body, inspection)

    def prepare_audio(self, owner, jid, body):
        if body.get('confirm') is not True or body.get('processing_rights') is not True:
            raise DomainError(422,'CONFIRM_REQUIRED','请确认整曲调速处理及相应使用权')
        asset = self.preview_asset(owner,jid)
        job = self.store.get_job(owner,jid)
        if job['snapshot'].get('prepared'):
            return job
        output = self.assets/(jid+'.short.wav')
        prepared = prepare_short(self.assets/asset['asset_name'],output)
        try:
            return self.store.register_prepared(owner,jid,prepared)
        except Exception:
            output.unlink(missing_ok=True)
            raise

    def intro(self,owner,wid,aid,body):
        self.rate_limit(owner+':compose',6)
        source=self.store.intro_inputs(owner,wid,aid,body)
        song=(self.assets/source['audio']['asset_name']).resolve()
        if song.parent!=self.assets.resolve() or not song.is_file():raise DomainError(404,'ASSET_MISSING','原歌曲文件不可用')
        song_hash=hashlib.sha256(song.read_bytes()).hexdigest()
        output=self.assets/('intro_'+secrets.token_hex(16)+'.wav')
        try:
            result=compose_intro(source['clip_bytes'],source['clip_mime'],song,output,source['start_seconds'],source['end_seconds'])
            with self.store._lock:
                if not song.is_file() or hashlib.sha256(song.read_bytes()).hexdigest()!=song_hash:raise DomainError(409,'INTRO_SOURCE_CHANGED','歌曲文件已变更')
                return self.store.commit_intro(owner,wid,aid,body,source,result)
        except Exception:
            output.unlink(missing_ok=True);raise

    def measured(self, owner, kind, operation, work_id=None, job_id=None):
        model={'lyrics':os.getenv('TOKENHUB_LYRICS_MODEL','glm-5.3-flash'),'speech':'hy-asr-3.0-preview','music':(getattr(self.gateway.candidate_client,'MODEL',None) if isinstance(getattr(self.gateway.candidate_client,'MODEL',None),str) else 'mureka-music-v9')}[kind] if isinstance(self.gateway,TokenHubPreviewGateway) else 'configured_gateway'
        cid=self.store.start_model_call(owner,kind,model,work_id,job_id);started=time.monotonic()
        try:
            result=operation()
        except Exception as exc:
            self.store.finish_model_call(owner,cid,'failed',int((time.monotonic()-started)*1000),error_code=exc.code if isinstance(exc,DomainError) else 'PROVIDER_ERROR')
            raise
        metadata=result.pop('_diagnostics',{}) if isinstance(result,dict) else {}
        status='success'
        if job_id:
            job=self.store.get_job(owner,job_id)
            if job['status'] in {'failed','timed_out','cancelled'}: status=job['status']
            metadata={'provider_request_id':(job.get('snapshot') or {}).get('candidate',{}).get('request_id')}
            error_code=(job.get('error') or {}).get('code')
        else: error_code=None
        self.store.finish_model_call(owner,cid,status,int((time.monotonic()-started)*1000),metadata,error_code)
        return result

    def candidate(self, owner, wid, body):
        return self._candidate(owner,wid,body)

    def _candidate(self, owner, wid, body):
        work = self.store.get_work(owner, wid)
        if _base(body) != work["version"]:
            raise DomainError(409, "VERSION_CONFLICT", "作品已更新，请先刷新")
        if isinstance(self.gateway, TokenHubPreviewGateway) and body.get("paid_call_confirmed") is not True:
            raise DomainError(422, "PAID_CALL_CONFIRMATION_REQUIRED", "请确认 AI 写词可能产生供应商费用")
        instruction = _text(body.get("instruction", "写成自然口语的中文短歌词"), "修改要求", maximum=500)
        if re.search(r'(只|仅|单独).{0,8}(贝斯|bass|鼓点|鼓轨|人声轨|伴奏轨)',instruction,re.I):
            raise DomainError(422,'UNSUPPORTED_STEM_EDIT','当前不能只改独立音轨。请在成小歌中选择另一种感觉，确认后重新生成整曲。')
        current = next((v for v in work["lyrics"] if v["id"] == work["current_lyric_id"]), None)
        old = current["lines"] if current else []
        target = body.get("line_id")
        if target is not None and (not isinstance(target, str) or target not in {line["id"] for line in old}):
            raise DomainError(422, "INVALID_LINE_ID", "请重新选择当前版本的一句")
        self.rate_limit(owner + ":lyrics", 10)
        if not self.ai_slots.acquire(blocking=False):
            raise DomainError(429, "AI_BUSY", "模型请求繁忙，请稍后再试")
        self.store.server_event(owner,'revision_requested',{'work_id':wid,'category':'lyrics','scope':'line' if target else 'song'})
        try:
            return self.measured(owner,'lyrics',lambda:self._lyric_result(work,instruction,target,old),wid)
        finally:
            self.ai_slots.release()

    def _lyric_result(self, work, instruction, target, old):
        result=self.gateway.lyrics(work,instruction,target)
        raw = result.get("lines") if isinstance(result, dict) else None
        if not isinstance(raw, list) or not all(isinstance(x, dict) and (x.get("id") is None or isinstance(x["id"], str)) for x in raw):
            raise DomainError(502, "PROVIDER_INVALID", "模型返回的歌词结构无效")
        # Do not accept provider changes to user-authorized lock state.
        old_map = {x["id"]: x for x in old}
        for line in raw:
            before = old_map.get(line.get("id"), {"locked": False, "locked_spans": []})
            locked = line.get("locked", before["locked"])
            spans = line.get("locked_spans", before["locked_spans"])
            if type(locked) is not bool or locked != before["locked"] or spans != before["locked_spans"]:
                raise DomainError(422, "LOCK_CONFLICT", "模型试图改变锁定规则，本次候选未应用")
            line["locked"] = before["locked"]
            line["locked_spans"] = before["locked_spans"]
        parsed = _parse_lines(raw, old)
        _check_intent(parsed, work["intent"])
        if target and (len(parsed) != len(old) or any(a["id"] != b["id"] or (a["id"] != target and a["text"] != b["text"]) for a, b in zip(parsed, old))):
            raise DomainError(422, "SCOPE_CONFLICT", "模型改动了未选择的歌词，候选已拦截")
        # New line IDs produced during candidate validation are provisional;
        # Store assigns durable IDs only when the human saves a lyric version.
        old_ids = {line["id"] for line in old}
        parsed = [{key: value for key, value in line.items() if key != "id" or value in old_ids}
                  for line in parsed]
        return {"base_version": work["version"], "lines": parsed, "source": "ai", "requires_confirmation": True, "_diagnostics":result.get("_diagnostics",{})}


def handler_for(app):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "Shuoyipai/0.1"

        def setup(self):
            super().setup()
            self.connection.settimeout(35)

        def log_message(self, fmt, *args):
            pass  # Never log story, tokens, share paths or query strings.

        def respond(self, code, payload, headers=None, raw=False, mime="application/json; charset=utf-8"):
            data = payload if raw else json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "SAMEORIGIN")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; media-src 'self' blob:; connect-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'self'")
            self.send_header("Permissions-Policy", "camera=(), geolocation=(), microphone=(self)")
            self.send_header("X-Request-ID", self.request_id)
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def identity(self):
            try:
                cookie = SimpleCookie(self.headers.get("Cookie", ""))
                token = cookie["syp_session"].value if "syp_session" in cookie else ""
                return app.store.session(token)
            except Exception:
                return None

        def body(self):
            if self.headers.get("Transfer-Encoding") or len(self.headers.get_all("Content-Length", [])) > 1:
                raise DomainError(400, "INVALID_BODY", "请求长度不明确或使用了不支持的分块编码")
            try:
                size = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                raise DomainError(400, "INVALID_BODY", "无效请求长度") from None
            upload_path = re.fullmatch(r"/api/works/[A-Za-z0-9_-]+/input-clips", urlsplit(self.path).path)
            limit = 9 * 1024 * 1024 if self.path == "/api/transcribe" or upload_path else 64 * 1024
            if not 0 <= size <= limit:
                raise DomainError(413, "BODY_TOO_LARGE", "提交内容过大")
            if size and self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                raise DomainError(415, "CONTENT_TYPE", "仅支持JSON请求")
            def reject_constant(value):
                raise ValueError(value)
            try:
                data = json.loads(self.rfile.read(size), parse_constant=reject_constant) if size else {}
                if not isinstance(data, dict):
                    raise ValueError()
                return data
            except (ValueError, UnicodeDecodeError, RecursionError):
                raise DomainError(400, "INVALID_JSON", "请求格式不正确") from None

        def guard(self):
            host = self.headers.get("Host", "")
            expected = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"} | {"saybeat-production.up.railway.app"}
            if len(self.headers.get_all("Host", [])) != 1 or host not in expected:
                raise DomainError(403, "HOST_DENIED", "本机服务不接受这个访问地址")
            if self.command not in {"GET", "HEAD"}:
                origin = self.headers.get("Origin")
                if len(self.headers.get_all("Origin", [])) > 1 or (origin is not None and origin not in {"http://" + value for value in expected} | {"https://saybeat-production.up.railway.app"}):
                    raise DomainError(403, "ORIGIN_DENIED", "拒绝跨站操作")
                if self.headers.get("Sec-Fetch-Site") == "cross-site":
                    raise DomainError(403, "ORIGIN_DENIED", "拒绝跨站操作")

        def media(self, audio, export=False):
            if not export: return self._media(audio,False)
            owner=self.identity()['owner_id'];props={'audio_id':audio.get('id'),'work_id':audio.get('work_id')}
            # Candidate download has no formal audio ID and is forbidden.
            try:
                result=self._media(audio,True)
            except Exception as exc:
                app.store.server_event(owner,'export_result',{k:v for k,v in dict(props,status='error',error_code=exc.code if isinstance(exc,DomainError) else 'TRANSFER_FAILED').items() if v})
                raise
            app.store.server_event(owner,'export_result',{k:v for k,v in dict(props,status='headers' if self.command=='HEAD' else 'sent_partial' if self.headers.get('Range') else 'sent',format=audio['mime']).items() if v})
            return result

        def _media(self, audio, export=False):
            if export and not audio.get("export_allowed"):
                raise DomainError(403, "EXPORT_NOT_LICENSED", "当前音频未获得导出授权")
            filename = audio["asset_name"]
            file = (app.assets / filename).resolve()
            if file.parent != app.assets.resolve() or not file.is_file():
                raise DomainError(404, "ASSET_MISSING", "音频文件不可用")
            headers = {}
            if export:
                headers["Content-Disposition"] = 'attachment; filename="shuoyipai' + file.suffix + '"'
            self.audio_bytes(file.read_bytes(), audio["mime"], headers)

        def audio_bytes(self, data, mime, headers=None):
            headers = {**(headers or {}), "Accept-Ranges": "bytes"}
            requested = self.headers.get("Range")
            code = 200
            if requested:
                error = DomainError(416, "INVALID_RANGE", "无效的音频范围或范围超出文件")
                error.headers = {"Content-Range": f"bytes */{len(data)}", "Accept-Ranges": "bytes"}
                match = re.fullmatch(r"bytes=(\d*)-(\d*)", requested)
                if not match or not any(match.groups()):
                    raise error
                lo, hi = match.groups()
                try:
                    start = int(lo) if lo else max(0, len(data) - int(hi))
                    end = min(int(hi), len(data) - 1) if lo and hi else len(data) - 1
                except ValueError:
                    raise error from None
                if not 0 <= start <= end < len(data):
                    raise error
                headers["Content-Range"] = f"bytes {start}-{end}/{len(data)}"
                data, code = data[start:end + 1], 206
            self.respond(code, data, headers, raw=True, mime=mime)

        def dispatch(self):
            self.request_id = secrets.token_hex(12)
            try:
                self.guard()
                path = unquote(urlsplit(self.path).path)
                query = parse_qs(urlsplit(self.path).query)
                method = "GET" if self.command == "HEAD" else self.command
                if path == "/api/health" and method == "GET":
                    return self.respond(200, {"ok": True, "version": "0.1.0", "local_only": True})
                if path == "/api/capabilities" and method == "GET":
                    preview = isinstance(app.gateway, TokenHubPreviewGateway)
                    return self.respond(200, {**app.gateway.capabilities(), "manual_lyrics": True, "same_device_only": True,
                        "production_ready": False, "audio_format": "mp3" if preview else "wav",
                        "song_preview_only": preview, "local_daily_song_limit": False,
                        "notice": "TokenHub 语音识别、文字候选和成歌入口已配置，单次请求可能计费。音频先私有试听；可主动整曲调速，再逐项审核唱词、音质及使用权转为正式作品。失败不要自动重试。" if preview else "本地开发版：AI功能需接入并验证后才可使用；样例不是实时生成。"})
                if path == "/api/session" and method == "POST":
                    self.body()
                    app.rate_limit(self.client_address[0] + ":session", 30)
                    session = self.identity()
                    headers = {}
                    if not session:
                        session = app.store.create_session()
                        headers["Set-Cookie"] = f'syp_session={session["token"]}; HttpOnly; SameSite=Strict; Path=/; Max-Age=2592000'
                    return self.respond(200, {"csrf": session["csrf"], "same_device_only": True}, headers)
                public = re.fullmatch(r"/api/public/([A-Za-z0-9_-]+)(/audio)?", path)
                if public and method == "GET":
                    app.rate_limit(self.client_address[0] + ":public", 180)
                    if public[2]:
                        return self.media(app.store.share_asset(public[1]))
                    return self.respond(200, app.store.public_share(public[1]))
                if not path.startswith("/api/"):
                    if method != "GET":
                        raise DomainError(405, "METHOD_NOT_ALLOWED", "不支持这种操作")
                    files = {"/": "index.html", "/app.js": "app.js", "/commands.js":"commands.js", "/rhythm.js":"rhythm.js", "/styles.css": "styles.css", "/record.svg": "record.svg"}
                    name = "index.html" if path.startswith("/s/") else files.get(path)
                    if not name or not (ROOT / "web" / name).is_file():
                        raise DomainError(404, "NOT_FOUND", "页面不存在")
                    mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
                    return self.respond(200, (ROOT / "web" / name).read_bytes(), raw=True, mime=mime + ("; charset=utf-8" if mime.startswith("text/") or "javascript" in mime else ""))
                session = self.identity()
                if not session:
                    raise DomainError(401, "SESSION_REQUIRED", "会话已失效，请刷新后继续")
                owner = session["owner_id"]
                body = {}
                if method != "GET":
                    if not hmac.compare_digest(self.headers.get("X-CSRF-Token", "").encode("utf-8"), session["csrf"].encode("utf-8")):
                        raise DomainError(403, "CSRF_FAILED", "操作校验失败，请刷新页面")
                    app.rate_limit(owner)
                    body = self.body()
                if path == "/api/diary" and method == "GET":
                    return self.respond(200, app.store.diary_month(owner, query.get('month', [''])[0]))
                if path == "/api/diary/monthly" and method == "POST":
                    return self.respond(201, app.store.diary_monthly(owner, body))
                diary = re.fullmatch(r"/api/diary/(\d{4}-\d{2}-\d{2})(/work)?", path)
                if diary:
                    day = diary.group(1)
                    if diary.group(2) and method == "POST":
                        return self.respond(200, app.store.diary_work(owner, day, body))
                    if not diary.group(2) and method == "PUT":
                        return self.respond(200, app.store.diary_save(owner, day, body))
                    if not diary.group(2) and method == "DELETE":
                        return self.respond(200, app.store.diary_delete(owner, day, body))
                if path == "/api/works":
                    if method == "GET":
                        return self.respond(200, app.store.list_works(owner))
                    if method == "POST":
                        return self.respond(201, app.store.create_work(owner, body))
                if path == "/api/transcribe" and method == "POST":
                    app.rate_limit(owner + ":speech", 10)
                    return self.respond(200, app.measured(owner,"speech",lambda:app.gateway.speech(body)))
                if path == '/api/analytics/context' and method in {'GET','POST'}:
                    return self.respond(200,app.store.analytics_context(owner,body if method=='POST' else None))
                if path == '/api/internal/diagnostics' and method == 'GET':
                    try: days=int(query.get('days',['7'])[0])
                    except ValueError: raise DomainError(422,'VALIDATION_ERROR','天数无效')
                    return self.respond(200,app.store.diagnostics(owner,days,query.get('traffic',['user'])[0],query.get('device',['all'])[0]))
                cost=re.fullmatch(r'/api/internal/calls/([A-Za-z0-9_-]+)/cost',path)
                if cost and method=='POST': return self.respond(200,app.store.record_call_cost(owner,cost[1],body))
                if path=='/api/studies' and method=='POST': return self.respond(201,app.store.start_study(owner,body))
                study=re.fullmatch(r'/api/studies/([A-Za-z0-9_-]+)',path)
                if study and method=='POST': return self.respond(200,app.store.finish_study(owner,study[1],body))
                if study and method=='DELETE': return self.respond(200,app.store.withdraw_study(owner,study[1]))
                if path == "/api/events" and method == "POST":
                    return self.respond(200, app.store.add_event(owner, body))
                match = re.fullmatch(r"/api/jobs/([A-Za-z0-9_-]+)(/cancel|/preview|/review|/prepare)?", path)
                if match:
                    if method == "GET" and not match[2]:
                        return self.respond(200, app.job_view(owner, match[1]))
                    if method == "GET" and match[2] == "/preview":
                        return self.media(app.preview_asset(owner, match[1],query.get('variant',['original'])[0]), query.get("download") == ["1"])
                    if method == 'POST' and match[2] == '/review':
                        return self.respond(200, app.review_audio(owner, match[1], body))
                    if method == 'POST' and match[2] == '/prepare':
                        app.rate_limit(owner+':prepare',3)
                        return self.respond(200, app.prepare_audio(owner,match[1],body))
                    if method == "POST" and match[2] == "/cancel":
                        job = app.store.cancel_job(owner, match[1])
                        if job["status"] == "cancelled":
                            (app.assets / (match[1] + ".candidate.mp3")).unlink(missing_ok=True)
                            (app.assets / (match[1] + ".short.wav")).unlink(missing_ok=True)
                        return self.respond(200, job)
                match = re.fullmatch(r"/api/audio/([A-Za-z0-9_-]+)", path)
                if match and method == "GET":
                    return self.media(app.store.media_asset(owner, match[1]), query.get("download") == ["1"])
                match = re.fullmatch(r"/api/input-clips/([A-Za-z0-9_-]+)(/audio)?", path)
                if match:
                    if method == "GET" and match[2] == "/audio":
                        mime, data = app.store.input_clip_audio(owner, match[1])
                        return self.audio_bytes(data, mime)
                    if method == "DELETE" and not match[2]:
                        app.store.input_clip_audio(owner,match[1]) # Ownership before confirmation.
                        if body.get('confirm') is not True: raise DomainError(422,'CONFIRM_REQUIRED','删除原音需要二次确认')
                        with app.store._lock:
                            before=app.store._input_clip_for_cleanup(owner,match[1])
                            result=app.store.delete_input_clip(owner,match[1])
                        for name in before:
                            file=(app.assets/name).resolve()
                            if file.parent==app.assets.resolve():file.unlink(missing_ok=True)
                        return self.respond(200,result)
                match = re.fullmatch(r"/api/works/([A-Za-z0-9_-]+)(.*)", path)
                if match:
                    wid, suffix = match.groups()
                    if not suffix:
                        if method == "GET":
                            return self.respond(200, app.work_view(owner, wid))
                        if method == "PATCH":
                            return self.respond(200, app.store.update_work(owner, wid, body))
                        if method == "DELETE":
                            # 与同一Store中的建任务/完成任务串行，避免两次读写之间漏掉新音频。
                            with app.store._lock:
                                work = app.store.get_work(owner, wid)
                                result = app.store.delete_work(owner, wid, body)
                            # 初次读作品后worker仍可能完成；任务文件名也必须一起清理。
                            filenames = {audio["asset_name"] for audio in work["audios"]}
                            filenames.update(job["id"] + ".wav" for job in work["jobs"])
                            filenames.update(job["id"] + ".candidate.mp3" for job in work["jobs"])
                            filenames.update(job["id"] + ".short.wav" for job in work["jobs"])
                            for filename in filenames:
                                asset = (app.assets / filename).resolve()
                                if asset.parent == app.assets.resolve():
                                    asset.unlink(missing_ok=True)
                            return self.respond(200, result)
                    if method=='GET' and suffix=='/comparison':
                        return self.respond(200,app.store.line_comparison(owner,wid,query.get('line_id',[''])[0]))
                    if method == "POST":
                        if suffix=='/line-origins':return self.respond(200,app.store.save_line_origin(owner,wid,body))
                        intro=re.fullmatch(r'/audios/([A-Za-z0-9_-]+)/intro',suffix)
                        if intro:return self.respond(201,app.intro(owner,wid,intro[1],body))
                        if suffix == "/input-clips":
                            with app.store._lock:
                                before=app.store.get_work(owner,wid)['audios']
                                clip=app.store.create_input_clip(owner,wid,body)
                                remaining={a['id'] for a in app.store.get_work(owner,wid)['audios']}
                            for audio in before:
                                if audio['id'] not in remaining:
                                    file=(app.assets/audio['asset_name']).resolve()
                                    if file.parent==app.assets.resolve():file.unlink(missing_ok=True)
                            return self.respond(201,clip)
                        if suffix == "/lyrics":
                            return self.respond(201, app.store.save_lyrics(owner, wid, body))
                        if suffix == "/lyrics/restore":
                            return self.respond(200, app.store.restore_lyrics(owner, wid, body))
                        if suffix == "/lyrics/generate":
                            return self.respond(200, app.candidate(owner, wid, body))
                        if suffix == "/jobs":
                            return self.respond(202, app.song(owner, wid, body))
                        if suffix == "/shares":
                            return self.respond(201, app.store.create_share(owner, wid, body))
                        keep = re.fullmatch(r"/audios/([A-Za-z0-9_-]+)/keep", suffix)
                        if keep:
                            return self.respond(200, app.store.keep_audio(owner, wid, keep[1]))
                        revoke = re.fullmatch(r"/shares/([A-Za-z0-9_-]+)/revoke", suffix)
                        if revoke:
                            return self.respond(200, app.store.revoke_share(owner, wid, revoke[1]))
                raise DomainError(404, "NOT_FOUND", "接口不存在")
            except DomainError as exc:
                # Unread rejected bodies must not become the next keep-alive request.
                self.close_connection = True
                self.respond(exc.status, {"error": {"code": exc.code, "message": exc.message}, "request_id": self.request_id}, getattr(exc, "headers", None))
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception:
                self.close_connection = True
                self.respond(500, {"error": {"code": "INTERNAL_ERROR", "message": "服务暂时出错，请重试；已保存内容不会清空"}, "request_id": self.request_id})

        do_GET = dispatch
        do_HEAD = dispatch
        do_POST = dispatch
        do_PUT = dispatch
        do_PATCH = dispatch
        do_DELETE = dispatch
    return Handler


class LocalHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def server_close(self):
        try:
            super().server_close()
        finally:
            if hasattr(self, "app"):
                self.app.close()


def make_server(port=8765, data_dir=None, gateway=None, host=None):
    # 本地默认只监听回环地址；云平台通过 PORT 或 SYP_HOST 使用公网监听地址。
    bind_host = host or os.getenv("SYP_HOST") or ("0.0.0.0" if os.getenv("PORT") else "127.0.0.1")
    # 先绑定端口，再恢复数据库；端口占用不能使正在运行的服务任务失败。
    server = LocalHTTPServer((bind_host, port), BaseHTTPRequestHandler)
    try:
        server.app = Application(data_dir, gateway)
        server.RequestHandlerClass = handler_for(server.app)
    except Exception:
        server.server_close()
        raise
    return server


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="说一拍本地开发服务")
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8765")))
    parser.add_argument("--host", default=os.getenv("SYP_HOST") or ("0.0.0.0" if os.getenv("PORT") else "127.0.0.1"))
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--tokenhub-preview-lyrics", type=Path, help="只生成本机待听审候选；输入UTF-8歌词文件")
    parser.add_argument("--tokenhub-prompt", default="中文独立流行，温暖自然，有清晰人声")
    parser.add_argument("--tokenhub-model", default=MurekaCandidate.MODEL,
                        choices=[MurekaCandidate.MODEL])
    parser.add_argument("--confirm-paid-call", action="store_true")
    parser.add_argument("--confirm-input-rights", action="store_true")
    args = parser.parse_args()
    if args.tokenhub_preview_lyrics:
        root = Path(args.data_dir or ROOT / "data") / "candidates"
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if root.is_symlink() or root.stat().st_mode & 0o077:
            parser.error("候选音频目录必须是非符号链接、仅当前用户可访问（权限0700）")
        try:
            lyrics = args.tokenhub_preview_lyrics.read_text(encoding="utf-8")
            output = root / ("candidate-" + secrets.token_hex(12) + ".mp3")
            result = MurekaCandidate().generate(lyrics, args.tokenhub_prompt, output,
                model=args.tokenhub_model, confirm_paid_call=args.confirm_paid_call,
                confirm_input_rights=args.confirm_input_rights)
        except (DomainError, OSError, UnicodeError) as exc:
            parser.error(exc.message if isinstance(exc, DomainError) else "无法读取歌词或写入候选文件")
        print(json.dumps(result, ensure_ascii=False), flush=True)
    else:
        server = make_server(args.port, args.data_dir, host=args.host)
        print(f"说一拍 http://{args.host}:{server.server_port} · AI配置 {server.app.gateway.capabilities()}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
            server.app.close()
