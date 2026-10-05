"""Synthetic disposable fixture. Never run against the user's database."""
import base64,io,json,sys,wave,math,array
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend.domain import Store

def tone(seconds,freq):
    b=io.BytesIO();pcm=array.array('h',[int(5000*math.sin(2*math.pi*freq*i/16000)) for i in range(round(seconds*16000))])
    with wave.open(b,'wb') as s:s.setnchannels(1);s.setsampwidth(2);s.setframerate(16000);s.writeframes(pcm.tobytes())
    return b.getvalue()
r=json.load(sys.stdin);root=Path(r['data_dir']);store=Store(root/'shuoyipai.sqlite3');owner=store.session(r['session_token'])['owner_id'];store.analytics_context(owner,{'traffic':'test'})
work=store.create_work(owner,{'title':'原声与版本 · 合成音频验收专用','story':'朋友说别急我在呢，晚风陪我走','service_consent':True,'intent':{'theme':'陪伴','attitude':'温暖','preserve':'别急我在呢','avoid':'','recipient':''}})
work=store.save_lyrics(owner,work['id'],{'base_version':work['version'],'lines':[{'text':'别急我在呢'},{'text':'晚风陪我慢慢走'}]});lid=work['lyrics'][0]['lines'][0]['id']
for i in (1,2):
 if i==2:
    lines=work['lyrics'][-1]['lines'];lines[0]['text']='别急我在呢，今天陪你走';work=store.save_lyrics(owner,work['id'],{'base_version':work['version'],'lines':lines})
 job=store.create_job(owner,work['id'],{'base_version':work['version'],'lyric_id':work['current_lyric_id'],'confirmed':True,'idempotency_key':'synthetic-'+str(i)},True);store.transition_job(job['id'],'generating');store.transition_job(job['id'],'checking')
 name='synthetic-'+str(i)+'.wav';(root/'assets'/name).write_bytes(tone(20,330+i*70))
 store.complete_job(job['id'],{'verified_singing':True,'lyrics_match':True,'duration':20,'asset_name':name,'mime':'audio/wav','export_allowed':True})
clip=store.create_input_clip(owner,work['id'],{'confirm':True,'source':'upload','role':'story','mime':'audio/wav','audio_base64':base64.b64encode(tone(3,240)).decode()})
print(json.dumps({'work_id':work['id'],'line_id':lid,'clip_id':clip['id'],'source':'synthetic_tones_not_human_or_singing'}))
