"""Private diagnostics. No story/lyrics/audio in generic events; no fabricated cost."""
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import json
import math
import secrets
from statistics import median
from zoneinfo import ZoneInfo

APP_VERSION = '0.1.1'
CONTEXT_ENUMS = {'traffic': {'user','team','test'}, 'device': {'desktop','mobile','tablet','unknown'},
                 'entry_source': {'direct','demo','shared','unknown'}, 'experiment': {'baseline','intent_lock','none'}}
SCHEMA = (
    '''CREATE TABLE IF NOT EXISTS analytics_settings(owner_id TEXT PRIMARY KEY, document TEXT NOT NULL)''',
    '''CREATE TABLE IF NOT EXISTS analytics_sessions(id TEXT PRIMARY KEY, owner_id TEXT NOT NULL,
       started_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, is_first INTEGER NOT NULL, context TEXT NOT NULL)''',
    '''CREATE INDEX IF NOT EXISTS analytics_sessions_owner ON analytics_sessions(owner_id,last_seen_at)''',
    '''CREATE TABLE IF NOT EXISTS event_context(owner_id TEXT NOT NULL, event_id TEXT NOT NULL,
       session_id TEXT NOT NULL REFERENCES analytics_sessions(id), origin TEXT NOT NULL CHECK(origin IN ('client','server')),
       client_at TEXT, app_version TEXT NOT NULL, PRIMARY KEY(owner_id,event_id),
       FOREIGN KEY(owner_id,event_id) REFERENCES events(owner_id,event_id) ON DELETE CASCADE)''',
    '''CREATE TABLE IF NOT EXISTS model_calls(id TEXT PRIMARY KEY, owner_id TEXT NOT NULL,
       work_id TEXT REFERENCES works(id), job_id TEXT, kind TEXT NOT NULL, model TEXT NOT NULL,
       started_at TEXT NOT NULL, ended_at TEXT, latency_ms INTEGER, status TEXT NOT NULL,
       error_code TEXT, provider_request_id TEXT, total_tokens INTEGER, cost_micros INTEGER,
       cost_reference TEXT, session_id TEXT NOT NULL REFERENCES analytics_sessions(id))''',
    '''CREATE TABLE IF NOT EXISTS play_studies(id TEXT PRIMARY KEY, owner_id TEXT NOT NULL,
       work_id TEXT REFERENCES works(id), session_id TEXT NOT NULL REFERENCES analytics_sessions(id),
       started_at TEXT NOT NULL, ended_at TEXT, consent_at TEXT NOT NULL, results TEXT)''',
)


class AnalyticsMixin:
    def _analytics_session(self, connection, owner, now=None):
        from .domain import _stamp, _now, _dump
        now = now or _now()
        setting=connection.execute('SELECT document FROM analytics_settings WHERE owner_id=?',(owner,)).fetchone()
        context=json.loads(setting['document']) if setting else {
            'traffic':'user','device':'unknown','entry_source':'unknown','experiment':'none'}
        row=connection.execute('SELECT * FROM analytics_sessions WHERE owner_id=? ORDER BY last_seen_at DESC LIMIT 1',(owner,)).fetchone()
        if row and now-datetime.fromisoformat(row['last_seen_at']) < timedelta(minutes=30) and json.loads(row['context'])==context:
            connection.execute('UPDATE analytics_sessions SET last_seen_at=? WHERE id=?',(_stamp(now),row['id']))
            return row['id']
        sid='session_'+secrets.token_hex(12)
        connection.execute('INSERT INTO analytics_sessions VALUES(?,?,?,?,?,?)',
            (sid,owner,_stamp(now),_stamp(now),int(row is None),_dump(context)))
        return sid

    def analytics_context(self, owner, body=None):
        from .domain import _object, _fail, _dump
        with self._transaction() as connection:
            row=connection.execute('SELECT document FROM analytics_settings WHERE owner_id=?',(owner,)).fetchone()
            context=json.loads(row['document']) if row else {'traffic':'user','device':'unknown','entry_source':'unknown','experiment':'none'}
            if body is not None:
                _object(body)
                if set(body)-set(CONTEXT_ENUMS): _fail(422,'VALIDATION_ERROR','不允许的分析上下文字段')
                for key,value in body.items():
                    if not isinstance(value,str) or value not in CONTEXT_ENUMS[key]: _fail(422,'VALIDATION_ERROR','分析分组必须使用规定值')
                context.update(body)
                connection.execute('INSERT INTO analytics_settings VALUES(?,?) ON CONFLICT(owner_id) DO UPDATE SET document=excluded.document',(owner,_dump(context)))
            sid=self._analytics_session(connection,owner)
            return {'context':context,'session_id':sid,'sampling_enabled':False,'sampling_scope':'team_original_only'}

    def _event_context(self, connection, owner, event_id, origin='server', client_at=None, now=None):
        sid=self._analytics_session(connection,owner,now)
        # Async results keep the session in which the request was initiated.
        event=connection.execute('SELECT properties FROM events WHERE owner_id=? AND event_id=?',(owner,event_id)).fetchone()
        jid=json.loads(event['properties']).get('job_id') if event else None
        if jid:
            origin_row=connection.execute('SELECT session_id FROM event_context WHERE owner_id=? AND event_id=?',(owner,'created_'+jid)).fetchone()
            if origin_row: sid=origin_row['session_id']
        connection.execute('INSERT OR IGNORE INTO event_context VALUES(?,?,?,?,?,?)',
            (owner,event_id,sid,origin,client_at,APP_VERSION))
        return sid

    def _server_event(self, connection, owner, name, properties, event_id=None, now=None):
        from .domain import _dump,_hash,_stamp
        if not owner: return
        from .domain import _now
        now=now or _now()
        if properties.get('work_id'):
            alive=connection.execute('SELECT owner_id FROM works WHERE id=? AND deleted_at IS NULL',(properties['work_id'],)).fetchone()
            if not alive or alive['owner_id']!=owner: return
        event_id=event_id or 'server_'+secrets.token_hex(12)
        fingerprint=_hash(_dump({'name':name,'properties':properties}))
        connection.execute('INSERT OR IGNORE INTO events VALUES(?,?,?,?,?,?,?)',
            (owner,event_id,name,_dump(properties),fingerprint,properties.get('work_id'),_stamp(now)))
        self._event_context(connection,owner,event_id,now=now)

    def server_event(self, owner, name, properties, event_id=None):
        with self._transaction() as connection:
            self._server_event(connection,owner,name,properties,event_id)

    def start_model_call(self, owner, kind, model, work_id=None, job_id=None):
        from .domain import _stamp,_id
        with self._transaction() as connection:
            if work_id: self._work(connection,owner,work_id)
            sid=self._analytics_session(connection,owner)
            if job_id:
                initial=connection.execute('SELECT session_id FROM event_context WHERE owner_id=? AND event_id=?',(owner,'created_'+job_id)).fetchone()
                if initial: sid=initial['session_id']
            cid=_id('call')
            connection.execute('''INSERT INTO model_calls(id,owner_id,work_id,job_id,kind,model,started_at,status,session_id)
                VALUES(?,?,?,?,?,?,?,'pending',?)''',(cid,owner,work_id,job_id,kind,model,_stamp(),sid))
            return cid

    def finish_model_call(self, owner, cid, status, latency_ms, metadata=None, error_code=None):
        from .domain import _stamp,_fail
        metadata=metadata if isinstance(metadata,dict) else {}
        with self._transaction() as connection:
            row=connection.execute('SELECT * FROM model_calls WHERE id=? AND owner_id=?',(cid,owner)).fetchone()
            if not row: return # Work deletion removed the attempt while the provider was running.
            tokens=metadata.get('total_tokens')
            tokens=tokens if type(tokens) is int and 0<=tokens<=10**12 else None
            rid=metadata.get('provider_request_id')
            import re
            rid=rid if isinstance(rid,str) and re.fullmatch(r'[A-Za-z0-9_-]{8,100}',rid) else None
            connection.execute('''UPDATE model_calls SET ended_at=?,latency_ms=?,status=?,error_code=?,provider_request_id=?,total_tokens=? WHERE id=?''',
                (_stamp(),min(max(0,int(latency_ms)),86400000),status,error_code if isinstance(error_code,str) and re.fullmatch(r'[A-Z][A-Z0-9_]{0,63}',error_code) else None,rid,tokens,cid))
            props={'status':status,'latency_ms':min(max(0,int(latency_ms)),86400000),'model':row['model'],'request_id':cid}
            if row['work_id']: props['work_id']=row['work_id']
            if row['job_id']: props['job_id']=row['job_id']
            if isinstance(error_code,str) and re.fullmatch(r'[A-Z][A-Z0-9_]{0,63}',error_code): props['error_code']=error_code
            name={'lyrics':'lyric_generate_result','speech':'transcription_result','music':'song_generate_result'}[row['kind']]
            self._server_event(connection,owner,name,props,'model_result_'+cid)

    def abort_pending_calls(self):
        from .domain import _stamp
        with self._transaction() as connection:
            pending=connection.execute("SELECT * FROM model_calls WHERE status='pending'").fetchall()
            for row in pending:
                connection.execute("UPDATE model_calls SET status='failed',error_code='SERVER_RESTARTED',ended_at=? WHERE id=?",(_stamp(),row['id']))
                props={'request_id':row['id'],'status':'failed','error_code':'SERVER_RESTARTED'}
                if row['work_id']: props['work_id']=row['work_id']
                if row['job_id']: props['job_id']=row['job_id']
                self._server_event(connection,row['owner_id'],{'lyrics':'lyric_generate_result','music':'song_generate_result','speech':'transcription_result'}[row['kind']],props,'model_result_'+row['id'])
            return len(pending)

    def record_call_cost(self, owner, cid, body):
        from .domain import _object,_text,_fail
        _object(body)
        amount=body.get('cost_micros')
        if body.get('confirmed') is not True or type(amount) is not int or not 0<=amount<=10**12:
            _fail(422,'VALIDATION_ERROR','请确认实际账单金额，单位为百万分之一元人民币')
        reference=_text(body.get('reference'),'账单依据',maximum=160)
        with self._transaction() as connection:
            if not connection.execute('SELECT 1 FROM model_calls WHERE id=? AND owner_id=?',(cid,owner)).fetchone(): _fail(404,'NOT_FOUND','调用记录不存在')
            connection.execute('UPDATE model_calls SET cost_micros=?,cost_reference=? WHERE id=? AND owner_id=?',(amount,reference,cid,owner))
            return {'recorded':True,'cost_source':'owner_checked_bill'}

    def start_study(self, owner, body):
        from .domain import _object,_fail,_stamp,_id
        _object(body)
        if body.get('consent') is not True: _fail(422,'CONSENT_REQUIRED','试玩行为记录需要独立同意；拒绝仍能正常创作')
        with self._transaction() as connection:
            active=connection.execute('SELECT * FROM play_studies WHERE owner_id=? AND ended_at IS NULL',(owner,)).fetchone()
            if active: return dict(active)
            sid=self._analytics_session(connection,owner);now=_stamp();rid=_id('study')
            connection.execute('INSERT INTO play_studies VALUES(?,?,?,?,?,?,?,?)',(rid,owner,None,sid,now,None,now,None))
            return {'id':rid,'started_at':now,'sampling_enabled':False}

    def finish_study(self, owner, rid, body):
        from .domain import _object,_fail,_stamp,_dump
        _object(body)
        flags=('independent_completion','recognized_original','wanted_revision','needed_help')
        if any(type(body.get(k)) is not bool for k in flags): _fail(422,'VALIDATION_ERROR','请如实逐项记录试玩结果')
        with self._transaction() as connection:
            row=connection.execute('SELECT * FROM play_studies WHERE id=? AND owner_id=?',(rid,owner)).fetchone()
            if not row: _fail(404,'NOT_FOUND','试玩记录不存在')
            wid=body.get('work_id')
            if wid: self._work(connection,owner,wid)
            if not row['ended_at']:
                connection.execute('UPDATE play_studies SET work_id=?,ended_at=?,results=? WHERE id=?',(wid,_stamp(),_dump({k:body[k] for k in flags}),rid))
            return {'saved':True,'self_reported':True}

    def withdraw_study(self, owner, rid):
        from .domain import _fail
        with self._transaction() as connection:
            if not connection.execute('SELECT 1 FROM play_studies WHERE id=? AND owner_id=?',(rid,owner)).fetchone(): _fail(404,'NOT_FOUND','试玩记录不存在')
            connection.execute('DELETE FROM play_studies WHERE id=? AND owner_id=?',(rid,owner))
            return {'deleted':True}

    def diagnostics(self, owner, days=7, traffic='user', device='all'):
        from .domain import _now,_stamp,_fail
        if type(days) is not int or days not in (1,7,30) or traffic not in {'user','team','test','all'} or device not in {'desktop','mobile','tablet','unknown','all'}:
            _fail(422,'VALIDATION_ERROR','无效的诊断筛选')
        now=_now();start=_stamp(now-timedelta(days=days))
        with self._transaction() as connection:
            sessions=[dict(r) for r in connection.execute('SELECT * FROM analytics_sessions WHERE owner_id=?',(owner,))]
            selected={s['id']:s for s in sessions if (traffic=='all' or json.loads(s['context'])['traffic']==traffic) and (device=='all' or json.loads(s['context'])['device']==device)}
            rows=connection.execute('''SELECT e.*,c.session_id,c.origin,c.client_at,c.app_version FROM events e
                LEFT JOIN event_context c ON c.owner_id=e.owner_id AND c.event_id=e.event_id
                WHERE e.owner_id=? AND e.created_at>=? ORDER BY e.created_at''',(owner,start)).fetchall()
            events=[dict(r,properties=json.loads(r['properties'])) for r in rows if r['session_id'] in selected]
            jobs={r['id']:dict(r) for r in connection.execute('SELECT * FROM jobs WHERE owner_id=? AND created_at>=?',(owner,start))}
            job_ids={e['properties']['job_id'] for e in events if e['origin']=='server' and 'job_id' in e['properties']}
            # Historical jobs have no context; keep them separate, never infer traffic/session.
            scoped_jobs=[j for jid,j in jobs.items() if jid in job_ids]
            calls=[dict(r) for r in connection.execute('SELECT * FROM model_calls WHERE owner_id=? AND started_at>=? ORDER BY started_at DESC ',(owner,start)) if r['session_id'] in selected]
            studies=[dict(r,results=json.loads(r['results']) if r['results'] else None) for r in connection.execute('SELECT * FROM play_studies WHERE owner_id=? AND started_at>=?',(owner,start)) if r['session_id'] in selected]
            audios={r['id']:dict(json.loads(r['document']),work_id=r['work_id']) for r in connection.execute('SELECT * FROM audios WHERE owner_id=?',(owner,))}
        def unique(name,key,origin=None):
            return {e['properties'].get(key) for e in events if e['name']==name and (origin is None or e['origin']==origin) and e['properties'].get(key)}
        # Reject incoherent qualifying reports from aggregation; playback itself remains client-reported.
        def listen_valid(e):
            if e['name']!='audio_listen_qualified': return True
            prop=e['properties'];audio=audios.get(prop.get('audio_id'))
            return bool(audio and type(prop.get('covered_ms')) is int and type(prop.get('duration_ms')) is int and abs(prop['duration_ms']-audio['duration']*1000)<=1000 and prop['duration_ms']>0 and .5*prop['duration_ms']<=prop['covered_ms']<=prop['duration_ms']+1000)
        events=[e for e in events if listen_valid(e)]
        entered={e['session_id'] for e in events if e['name']=='create_entry_view'}
        names=['input_confirmed','lyric_confirmed','song_job_terminal','audio_listen_qualified','value_action','share_created_or_revoked']
        funnel=[{'stage':'创作入口','sessions':len(entered),'denominator':len(entered)}]
        cursors={sid:next(e for e in events if e['session_id']==sid and e['name']=='create_entry_view') for sid in entered}
        for name in names:
            following={}
            for sid,previous in cursors.items():
                group=[e for e in events if e['session_id']==sid and e['created_at']>=previous['created_at']]
                wid=previous['properties'].get('work_id')
                for event in group:
                    prop=event['properties']
                    if wid and prop.get('work_id')!=wid: continue
                    if name=='value_action':
                        good=event['name'] in {'version_kept','revision_requested','lyric_revision_applied'} and event['origin']=='server' and (event['name']!='lyric_revision_applied' or prop.get('is_change'))
                    else:
                        good=event['name']==name and (event['origin']=='server' or name=='audio_listen_qualified') and (name!='song_job_terminal' or prop.get('status')=='ready') and (name!='share_created_or_revoked' or prop.get('status')=='created')
                    if good:
                        following[sid]=event;break
            funnel.append({'stage':{'input_confirmed':'确认素材','lyric_confirmed':'确认成歌歌词','song_job_terminal':'正式音频成功','audio_listen_qualified':'有效试听（客户端报告）','value_action':'试听后保留或再改','share_created_or_revoked':'主动分享'}[name], 'sessions':len(following),'denominator':len(cursors)})
            cursors=following
        # Anonymous identity is scoped to this cookie; cross-device retention is unknown.
        first_dates={datetime.fromisoformat(e['created_at']).astimezone(ZoneInfo('Asia/Shanghai')).date() for e in events if e['name']=='audio_listen_qualified' and selected[e['session_id']]['is_first']}
        today=now.astimezone(ZoneInfo('Asia/Shanghai')).date()
        eligible={d for d in first_dates if d+timedelta(days=1)<today}
        activity_dates={datetime.fromisoformat(e['created_at']).astimezone(ZoneInfo('Asia/Shanghai')).date() for e in events if e['name']=='input_confirmed' and e['origin']=='server'}
        retention={'unit':'anonymous_owner','eligible_owners':int(bool(eligible)), 'returned_owners':int(any(d+timedelta(days=1) in activity_dates for d in eligible)), 'pending_owners':int(bool(first_dates-eligible)),'cross_device':'unavailable'}
        kept_works={e['properties']['work_id'] for e in events if e['name']=='version_kept' and e['origin']=='server' and any(l['name']=='audio_listen_qualified' and l['properties'].get('audio_id')==e['properties'].get('audio_id') and l['created_at']<=e['created_at'] for l in events)}
        known_cost=sum(c['cost_micros'] for c in calls if c['cost_micros'] is not None)
        unknown_cost=sum(c['cost_micros'] is None for c in calls)
        latencies=sorted(c['latency_ms'] for c in calls if c['latency_ms'] is not None)
        durations=[(datetime.fromisoformat(s['ended_at'])-datetime.fromisoformat(s['started_at'])).total_seconds() for s in studies if s['ended_at']]
        return {'scope':'current_owner_only','timezone':'Asia/Shanghai','range':{'start':start,'end':_stamp(now),'days':days},
            'filters':{'traffic':traffic,'device':device},'funnel':funnel,'retention_d1':retention,
            'events':{'counts':dict(Counter(e['name'] for e in events)),'server':sum(e['origin']=='server' for e in events),'client':sum(e['origin']=='client' for e in events)},
            'jobs':{'total':len(scoped_jobs),'statuses':dict(Counter(j['status'] for j in scoped_jobs)),
                    'failure_codes':dict(Counter((json.loads(j['error']) if j['error'] else {}).get('code','UNKNOWN') for j in scoped_jobs if j['status'] in {'failed','timed_out'}))},
            'behavior':{'qualified_works':len(unique('audio_listen_qualified','work_id')),'kept_works':len(unique('version_kept','work_id','server')),'revision_works':len(unique('revision_requested','work_id','server')),'candidate_listen_jobs':len(unique('candidate_listen_qualified','job_id')),'lyric_versions':len(unique('lyric_revision_applied','lyric_id','server')),'exports_sent':sum(e['name']=='export_result' and e['origin']=='server' and e['properties'].get('status')=='sent' for e in events)},
            'latency':{'count':len(latencies),'median_ms':median(latencies) if latencies else None,'p95_ms':latencies[math.ceil(len(latencies)*.95)-1] if latencies else None},
            'cost':{'currency':'CNY','known_cost_micros':known_cost,'unknown_attempts':unknown_cost,'attempts':len(calls),'effective_kept_works':len(kept_works),'cost_per_effective_kept_micros':known_cost//len(kept_works) if kept_works and not unknown_cost else None,'source':'owner_checked_bill'},
            'calls':[{k:c[k] for k in ('id','kind','model','started_at','latency_ms','status','error_code','total_tokens','cost_micros')} for c in calls[:50]],
            'studies':[{k:s[k] for k in ('id','started_at','ended_at','results')} for s in studies],
            'study_summary':{'consented':len(studies),'completed':len(durations),'median_seconds':median(durations) if durations else None,'self_reported':True},
            'missing':{'legacy_events':sum(r['session_id'] is None for r in rows),'legacy_jobs':len(set(jobs)-job_ids),'unknown_device_sessions':sum(json.loads(s['context'])['device']=='unknown' and s['last_seen_at']>=start for s in selected.values())},
            'notes':['30分钟无操作切分会话；内部/自动测试分组单独显示；分组是记录口径，不是随机实验。','有效试听来自客户端报告，不证明真人满意。首创及D1按匿名身份与上海时区，跨设备不合并。','历史事件缺少上下文，未补造会话、耗时或成本；删除作品会清理其关联分析记录。','模型费用需手动核对账单；包含失败与取消，缺失金额不当作免费。'],
            'sampling':{'enabled':False,'scope':'team_original_only'}}
