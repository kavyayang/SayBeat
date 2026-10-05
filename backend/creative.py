"""Owner-only original voice composition, rhythm snapshots and line provenance."""
import hashlib,json,math
from contextlib import nullcontext
from statistics import median


def parse_tempo(value):
    from .domain import _fail,_object
    if value is None:return None
    _object(value)
    taps=value.get('taps_ms')
    if set(value)!={'bpm','taps_ms'} or not isinstance(taps,list) or not 4<=len(taps)<=16 or any(type(x) is not int or x<0 or x>60000 for x in taps):
        _fail(422,'INVALID_TEMPO','请拍 4—16 下有效节奏')
    intervals=[b-a for a,b in zip(taps,taps[1:])]
    if any(not 300<=x<=1500 for x in intervals):_fail(422,'INVALID_TEMPO','相邻拍击间隔须为 0.3—1.5 秒（40—200 BPM）')
    bpm=math.floor(60000/median(intervals)+.5)
    if type(value.get('bpm')) is not int or value['bpm']!=bpm:_fail(422,'INVALID_TEMPO','BPM 与拍击记录不一致')
    return {'bpm':bpm,'taps_ms':[x-taps[0] for x in taps]}


class CreativeMixin:
    def _input_clip_for_cleanup(self,owner,clip_id):
        with self._transaction() as c:
            self._input_clip(c,owner,clip_id)
            return [json.loads(r['document'])['asset_name'] for r in c.execute("SELECT document FROM audios WHERE owner_id=? AND json_extract(document,'$.intro.clip_id')=?",(owner,clip_id))]

    def save_line_origin(self,owner,wid,body):
        from .domain import _object,_text,_check_version,_stamp
        _object(body)
        with self._transaction() as c:
            work=self._work(c,owner,wid);_check_version(work,body)
            lid=_text(body.get('line_id'),'歌词行 ID');quote=_text(body.get('quote'),'原话',maximum=160)
            if not any(line['id']==lid for lyric in work['lyrics'] for line in lyric['lines']):
                from .domain import _fail
                _fail(404,'NOT_FOUND','歌词行不存在')
            if quote not in work['story']:
                from .domain import _fail
                _fail(422,'QUOTE_NOT_IN_STORY','请逐字填写故事中真实存在的原话，不会推测来源')
            work.setdefault('line_origins',{})[lid]={'quote':quote,'story_version':work['version']}
            work['version']+=1;work['updated_at']=_stamp();self._persist(c,work,revision=True)
            return self._detail(c,work)

    def line_comparison(self,owner,wid,lid):
        from .domain import _text,_fail
        _text(lid,'歌词行 ID',maximum=128)
        with self._transaction() as c:
            work=self._detail(c,self._work(c,owner,wid));timeline=[]
            for lyric in work['lyrics']:
                line=next((x for x in lyric['lines'] if x['id']==lid),None)
                timeline.append({'lyric_id':lyric['id'],'number':lyric['number'],'text':line['text'] if line else None,'locked':line['locked'] if line else False,
                  'audios':[{'id':a['id'],'kind':a.get('kind','song'),'duration':a['duration']} for a in work['audios'] if a['lyric_id']==lyric['id']],
                  'candidates':[{'job_id':j['id'],'style':j['style']} for j in work['jobs'] if j['lyric_id']==lyric['id'] and j['status']=='checking' and j.get('snapshot',{}).get('candidate',{}).get('sha256')]})
            if not any(x['text'] is not None for x in timeline):_fail(404,'NOT_FOUND','歌词行不存在')
            return {'line_id':lid,'origin':work.get('line_origins',{}).get(lid),'story':work['story'],'first':next(x for x in timeline if x['text'] is not None),'timeline':timeline}

    def intro_inputs(self,owner,wid,aid,body,_connection=None):
        from .domain import _object,_check_version,_text,_fail
        from .audio_review import words
        _object(body)
        with (nullcontext(_connection) if _connection is not None else self._transaction()) as c:
            work=self._work(c,owner,wid);_check_version(work,body)
            audio=json.loads(self._audio(c,owner,aid,wid)['document'])
            if audio.get('kind')=='voice_intro':_fail(422,'INTRO_ALREADY_PRESENT','请使用原始歌曲添加开场，不能层层叠加')
            clip=self._input_clip(c,owner,_text(body.get('clip_id'),'原音 ID'))
            if clip['work_id']!=wid or clip['role']!='story':_fail(422,'CLIP_REFERENCE_CONFLICT','请选择本作品的故事原音')
            phrase=_text(body.get('phrase'),'开场原话',maximum=160)
            if not words(phrase) or not any(words(phrase) in words(line['text']) for line in audio['lyrics']):_fail(422,'INTRO_PHRASE_MISMATCH','开场原话必须出现在这首歌曲绑定的歌词中')
            start,end=body.get('start_seconds'),body.get('end_seconds')
            if type(start) not in (int,float) or type(end) not in (int,float) or not math.isfinite(start) or not math.isfinite(end) or not 0<=start<end<=clip['duration']+.001 or not .3<=end-start<=6:
                _fail(422,'INVALID_INTRO_RANGE','明确选取 0.3—6 秒原音，范围不能超过录音长度')
            if any(body.get(k) is not True for k in ('confirm','heard_phrase_confirmed','voice_rights','processing_rights')):_fail(422,'CONFIRM_REQUIRED','请确认原声确实包含原话、声音使用权和本次歌曲编辑权')
            for key in ('share_voice','export_voice'):
                if type(body.get(key)) is not bool:_fail(422,'VALIDATION_ERROR','请选择原声分享及导出范围')
            return {'work_id':wid,'source_audio_id':aid,'audio':audio,'clip_id':clip['id'],'clip_bytes':bytes(clip['audio']),'clip_mime':clip['mime'],'clip_sha256':hashlib.sha256(clip['audio']).hexdigest(),'phrase':phrase,'start_seconds':start,'end_seconds':end}

    def commit_intro(self,owner,wid,aid,body,original,result):
        from .domain import _id,_stamp,_dump,_fail
        with self._transaction() as c:
            current=self.intro_inputs(owner,wid,aid,body,_connection=c)
            if current['clip_sha256']!=original['clip_sha256'] or current['audio']!=original['audio']:_fail(409,'INTRO_SOURCE_CHANGED','原音或歌曲已变更，请重新确认')
            source=current['audio'];audio=json.loads(_dump(source));audio.update(id=_id('audio'),asset_name=result['asset_name'],mime='audio/wav',duration=result['duration'],created_at=_stamp(),kept=False,kind='voice_intro',source_audio_id=aid,song_duration=source['duration'])
            audio['intro']={k:current[k] for k in ('clip_id','clip_sha256','phrase','start_seconds','end_seconds')}
            audio['intro'].update(duration=result['intro_duration'],confirmed_by_owner=True,heard_phrase_confirmed=True,voice_rights=True,processing_rights=True,share_voice=body['share_voice'],export_voice=body['export_voice'])
            audio['share_allowed']=source.get('share_allowed',True) and body['share_voice'];audio['export_allowed']=source.get('export_allowed',False) and body['export_voice']
            # Existing singing review applies to the song portion, not to spoken words.
            if audio.get('audit'):audio['audit']['composition_inspection']={k:result[k] for k in ('sha256','duration','size_bytes')}
            c.execute('INSERT INTO audios VALUES(?,?,?,?,?)',(audio['id'],wid,owner,_dump(audio),audio['created_at']))
            self._server_event(c,owner,'revision_requested',{'work_id':wid,'audio_id':audio['id'],'category':'music'})
            self._server_event(c,owner,'voice_intro_created',{'work_id':wid,'audio_id':audio['id'],'source_audio_id':aid})
            return audio
