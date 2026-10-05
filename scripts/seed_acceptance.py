"""Seed a PRIVATE test work using already-generated audio; never approve rights."""
import json
from pathlib import Path
import shutil
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend.domain import Store
from backend.audio_review import inspect_mp3, inspect_wav

request=json.load(sys.stdin)
root=Path(request['data_dir'])
store=Store(root/'shuoyipai.sqlite3')
owner=store.session(request['session_token'])['owner_id']
work=store.create_work(owner,{'story':'自动化网页验收，使用 AI 生成音频，尚未核验唱词或使用权。',
    'title':'验收专用 · 非正式作品','service_consent':True,
    'intent':{'theme':'验收','attitude':'温暖','preserve':'','avoid':'','recipient':'自己'}})
work=store.save_lyrics(owner,work['id'],{'base_version':work['version'],'source':'manual',
    'lines':[{'text':'别急我在呢'},{'text':'晚风陪你慢慢走'}]})
job=store.create_job(owner,work['id'],{'base_version':work['version'],'lyric_id':work['current_lyric_id'],
    'idempotency_key':'local-ui-acceptance-'+work['id'],'confirmed':True},True)
jid=job['id']
assets=root/'assets'
assets.mkdir(exist_ok=True)
source=Path(request['mp3'])
inspection=inspect_mp3(source)
original=assets/(jid+'.candidate.mp3')
shutil.copyfile(source,original);original.chmod(0o600)
store.transition_job(jid,'generating')
store.register_candidate(owner,jid,{'duration_seconds_probed':inspection['duration'],
    'duration_seconds_reported':inspection['duration'],'size_bytes':source.stat().st_size,'response_format':'url'},original)
store.transition_job(jid,'checking')
if request.get('short_wav'):
    short=assets/(jid+'.short.wav')
    shutil.copyfile(request['short_wav'],short);short.chmod(0o600)
    rendered=inspect_wav(short)
    store.register_prepared(owner,jid,dict(rendered,asset_name=short.name,mime='audio/wav',
        processing='whole_song_pitch_preserving_tempo',source_sha256=inspection['sha256']))
print(json.dumps({'work_id':work['id'],'job_id':jid}))
