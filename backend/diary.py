"""Explicit owner-only daily memories; monthly songs retain selected source snapshots."""
import calendar
import json
import re
import secrets
from datetime import date, datetime, timezone, timedelta

SCHEMA = ["""CREATE TABLE IF NOT EXISTS diary_entries(
 owner_id TEXT NOT NULL, day TEXT NOT NULL, version INTEGER NOT NULL,
 phrase TEXT NOT NULL, work_id TEXT, updated_at TEXT NOT NULL,
 PRIMARY KEY(owner_id, day))"""]

def today():
    return datetime.now(timezone(timedelta(hours=8))).date()

def valid_day(value):
    from .domain import _fail
    try:
        if not isinstance(value,str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}',value):raise ValueError()
        parsed=date.fromisoformat(value)
        if parsed>today():raise ValueError()
    except ValueError:_fail(422,'INVALID_DAY','请选择有效日期，不能记录未来的日记')
    return value

def valid_month(value):
    from .domain import _fail
    try:
        if not isinstance(value,str) or not re.fullmatch(r'\d{4}-\d{2}',value):raise ValueError()
        parsed=date.fromisoformat(value+'-01')
    except ValueError:_fail(422,'INVALID_MONTH','月份格式应为 YYYY-MM')
    return value, calendar.monthrange(parsed.year,parsed.month)[1]

class DiaryMixin:
    def _diary(self,c,owner,day,body):
        from .domain import _fail
        row=c.execute('SELECT * FROM diary_entries WHERE owner_id=? AND day=?',(owner,day)).fetchone()
        if row is None:_fail(404,'NOT_FOUND','这一天还没有日记')
        if type(body.get('base_version')) is not int or body['base_version']!=row['version']:
            _fail(409,'VERSION_CONFLICT','日记已更新，请刷新后重试')
        return dict(row)

    def diary_save(self,owner,day,body):
        from .domain import _object,_text,_stamp,_fail
        valid_day(day);_object(body);_text(owner,'所有者')
        phrase=_text(body.get('phrase'),'今日原话',maximum=80)
        if any(x in phrase for x in '\r\n\v\f\x85\u2028\u2029'):_fail(422,'INVALID_PHRASE','每天留一句话，请勿换行')
        with self._transaction() as c:
            existing=c.execute('SELECT * FROM diary_entries WHERE owner_id=? AND day=?',(owner,day)).fetchone()
            version=existing['version'] if existing else 0
            if type(body.get('base_version')) is not int or body['base_version']!=version:_fail(409,'VERSION_CONFLICT','日记已变更，请刷新后重试')
            wid=body.get('work_id',existing['work_id'] if existing else None)
            if wid is not None:self._work(c,owner,_text(wid,'作品 ID'))
            next_version=version+1 if existing else secrets.randbelow(2**48)+1
            c.execute('INSERT INTO diary_entries VALUES(?,?,?,?,?,?) ON CONFLICT(owner_id,day) DO UPDATE SET version=excluded.version,phrase=excluded.phrase,work_id=excluded.work_id,updated_at=excluded.updated_at',(owner,day,next_version,phrase,wid,_stamp()))
        return {'day':day,'version':next_version,'phrase':phrase,'work_id':wid}

    def diary_month(self,owner,month):
        from .domain import _text
        _text(owner,'所有者');month,days=valid_month(month)
        with self._transaction() as c:
            entries=[]
            for row in c.execute('SELECT * FROM diary_entries WHERE owner_id=? AND day LIKE ? ORDER BY day DESC',(owner,month+'-%')):
                entry={k:row[k] for k in ('day','version','phrase','work_id','updated_at')};entry.update(work=None,audios=[],clips=[])
                workrow=c.execute('SELECT document FROM works WHERE owner_id=? AND id=? AND deleted_at IS NULL',(owner,row['work_id'])).fetchone()
                if workrow:
                    work=self._detail(c,json.loads(workrow['document']))
                    entry['work']={'id':work['id'],'title':work['title'],'status':work['status']}
                    # Audios must actually contain the saved phrase; private candidates are excluded.
                    entry['audios']=[{'id':a['id'],'duration':a['duration']} for a in work['audios'] if entry['phrase'] in '\n'.join(l['text'] for l in a['lyrics'])]
                    entry['clips']=[{'id':clip['id'],'duration':clip['duration']} for clip in work['input_clips'] if clip['role']=='story']
                entries.append(entry)
            works=[json.loads(r['document']) for r in c.execute('SELECT document FROM works WHERE owner_id=? AND deleted_at IS NULL',(owner,))]
            monthly=[{'id':w['id'],'title':w['title'],'sources':w['monthly_diary']['sources']} for w in works if w.get('monthly_diary',{}).get('month')==month]
            return {'month':month,'days':days,'today':today().isoformat(),'entries':entries,'monthly':monthly}

    def diary_delete(self,owner,day,body):
        from .domain import _object,_fail
        valid_day(day);_object(body)
        if body.get('confirm') is not True:_fail(422,'CONFIRM_REQUIRED','请确认删除日记')
        with self._transaction() as c:
            self._diary(c,owner,day,body)
            c.execute('DELETE FROM diary_entries WHERE owner_id=? AND day=?',(owner,day))
        return {'deleted':True}

    def diary_work(self,owner,day,body):
        from .domain import _object,_fail,_intent,_stamp
        valid_day(day);_object(body)
        if body.get('service_consent') is not True:_fail(422,'CONSENT_REQUIRED','请同意创作服务后再进入录音与歌曲创作')
        with self._transaction() as c:
            entry=self._diary(c,owner,day,body)
            if entry['work_id']:
                row=c.execute('SELECT id FROM works WHERE owner_id=? AND id=? AND deleted_at IS NULL',(owner,entry['work_id'])).fetchone()
                if row:return self._detail(c,self._work(c,owner,entry['work_id']))
            intent=_intent({'theme':'我的一天','attitude':'真实','preserve':entry['phrase'],'avoid':'','recipient':'自己'})
            work=self._create_work(c,owner,day+' · 声音日记',entry['phrase'],intent)
            c.execute('UPDATE diary_entries SET work_id=?,version=version+1,updated_at=? WHERE owner_id=? AND day=?',(work['id'],_stamp(),owner,day))
            return work

    def diary_monthly(self,owner,body):
        from .domain import _object,_fail,_text,_intent,_parse_lines
        _object(body);month,_=valid_month(body.get('month'));_text(owner,'所有者')
        if body.get('service_consent') is not True:_fail(422,'CONSENT_REQUIRED','请确认创作服务')
        selected=body.get('entries')
        if not isinstance(selected,list) or not 2<=len(selected)<=4:_fail(422,'INVALID_SELECTION','请选择同月 2—4 天的原话')
        with self._transaction() as c:
            sources=[];seen=set()
            for item in selected:
                _object(item);day=valid_day(item.get('day'))
                if day[:7]!=month or day in seen:_fail(422,'INVALID_SELECTION','请选择同月不同日期')
                seen.add(day);entry=self._diary(c,owner,day,item)
                sources.append({k:entry[k] for k in ('day','version','phrase')})
            sources.sort(key=lambda x:x['day'])
            # Repeated clicks return the same retained snapshot, without making duplicate works.
            for row in c.execute('SELECT document FROM works WHERE owner_id=? AND deleted_at IS NULL',(owner,)):
                work=json.loads(row['document'])
                if work.get('monthly_diary')=={'month':month,'sources':sources}:return self._detail(c,work)
            story='这个月的我\n'+'\n'.join(x['day']+'：'+x['phrase'] for x in sources)
            intent=_intent({'theme':'这个月的我','attitude':'真实','preserve':'\n'.join(x['phrase'] for x in sources),'avoid':'','recipient':'自己'})
            work=self._create_work(c,owner,month+' · 这个月的我',story,intent)
            work['monthly_diary']={'month':month,'sources':sources}
            lines=_parse_lines([{'text':x['phrase'],'locked':True} for x in sources],[])
            work['line_origins']={line['id']:{'quote':source['phrase'],'story_version':1} for line,source in zip(lines,sources)}
            return self._append_lyrics(c,work,lines,'manual')
