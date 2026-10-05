import base64,copy,io,json,math,tempfile,unittest,wave
from pathlib import Path
from unittest.mock import patch,Mock
from backend.domain import Store,DomainError
from backend.creative import parse_tempo
from backend.composition import compose_intro
from server import Application

def wav(seconds,rate=16000,frequency=330,channels=1):
    import array
    buffer=io.BytesIO();frames=array.array('h',[int(6000*math.sin(2*math.pi*frequency*i/rate)) for i in range(round(seconds*rate)) for _ in range(channels)])
    with wave.open(buffer,'wb') as w:w.setnchannels(channels);w.setsampwidth(2);w.setframerate(rate);w.writeframes(frames.tobytes())
    return buffer.getvalue()

class CreativeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name);self.store=Store(self.root/'test.db');self.owner='owner-1'
        self.work=self.store.create_work(self.owner,{'story':'朋友说别急我在呢，晚风替我转弯','service_consent':True,'intent':{'theme':'陪伴','attitude':'温暖','preserve':'','avoid':'','recipient':''}})
        self.work=self.store.save_lyrics(self.owner,self.work['id'],{'base_version':self.work['version'],'lines':[{'text':'别急我在呢'},{'text':'晚风陪我慢慢走'}]})
    def error(self,code,fn,*args):
        with self.assertRaises(DomainError) as ctx:fn(*args)
        self.assertEqual(ctx.exception.code,code)
    def source(self,share=True,export=True):
        job=self.store.create_job(self.owner,self.work['id'],{'base_version':self.work['version'],'lyric_id':self.work['current_lyric_id'],'confirmed':True,'idempotency_key':'job'},True)
        self.store.transition_job(job['id'],'generating');self.store.transition_job(job['id'],'checking')
        source=self.root/'song.wav';source.write_bytes(wav(20,44100,channels=2))
        done=self.store.complete_job(job['id'],{'verified_singing':True,'lyrics_match':True,'duration':20,'asset_name':source.name,'mime':'audio/wav','export_allowed':export})
        aid=done['audio_id']
        if not share:
            with self.store._transaction() as c:
                row=c.execute('SELECT document FROM audios WHERE id=?',(aid,)).fetchone();doc=json.loads(row['document']);doc['share_allowed']=False;c.execute('UPDATE audios SET document=? WHERE id=?',(json.dumps(doc),aid))
        clip=self.store.create_input_clip(self.owner,self.work['id'],{'confirm':True,'role':'story','source':'upload','mime':'audio/wav','audio_base64':base64.b64encode(wav(3,16000)).decode()})
        body={'base_version':self.work['version'],'clip_id':clip['id'],'phrase':'别急我在呢','start_seconds':.25,'end_seconds':2.25,'confirm':True,'heard_phrase_confirmed':True,'voice_rights':True,'processing_rights':True,'share_voice':True,'export_voice':True}
        return aid,clip,body,source
    def test_tempo_validates_exact_bpm_and_rejects_forged_or_invalid_taps(self):
        good={'bpm':120,'taps_ms':[300,800,1300,1800]};self.assertEqual(parse_tempo(good),{'bpm':120,'taps_ms':[0,500,1000,1500]})
        for bad in ({'bpm':80,'taps_ms':[0,500,1000,1500]},{'bpm':120,'taps_ms':[0,500,1000]},{'bpm':120,'taps_ms':[0,500,499,1000]},{'bpm':120,'taps_ms':[0,True,1000,1500]}):self.error('INVALID_TEMPO',parse_tempo,bad)
    def test_tempo_snapshot_does_not_change_inflight_job(self):
        self.work=self.store.update_work(self.owner,self.work['id'],{'base_version':self.work['version'],'tempo':{'bpm':120,'taps_ms':[0,500,1000,1500]}})
        job=self.store.create_job(self.owner,self.work['id'],{'base_version':self.work['version'],'lyric_id':self.work['current_lyric_id'],'confirmed':True,'idempotency_key':'tempo'},True)
        self.work=self.store.update_work(self.owner,self.work['id'],{'base_version':self.work['version'],'tempo':None})
        self.assertEqual(self.store.get_job(self.owner,job['id'])['snapshot']['tempo']['bpm'],120)
    def test_saved_taps_reach_music_prompt_without_external_request(self):
        from backend.providers import TokenHubPreviewGateway
        self.work=self.store.update_work(self.owner,self.work['id'],{'base_version':self.work['version'],'tempo':{'bpm':120,'taps_ms':[0,500,1000,1500]}})
        job=self.store.create_job(self.owner,self.work['id'],{'base_version':self.work['version'],'lyric_id':self.work['current_lyric_id'],'confirmed':True,'idempotency_key':'prompt'},True)
        gateway=TokenHubPreviewGateway(api_key='test-only-key');gateway.candidate_client=Mock()
        gateway.candidate_client.generate.side_effect=DomainError(502,'PROVIDER_ERROR','mock stop')
        gateway.run_song(self.store,self.owner,job,self.root)
        prompt=gateway.candidate_client.generate.call_args.args[1]
        self.assertIn('120 BPM',prompt);self.assertIn('500,500,500',prompt)

    def test_line_origin_exact_story_and_version_survive_story_edit(self):
        lid=self.work['lyrics'][0]['lines'][0]['id'];base=self.work['version']
        self.work=self.store.save_line_origin(self.owner,self.work['id'],{'base_version':base,'line_id':lid,'quote':'别急我在呢'})
        self.work=self.store.update_work(self.owner,self.work['id'],{'base_version':self.work['version'],'story':'新的故事'})
        data=self.store.line_comparison(self.owner,self.work['id'],lid);self.assertEqual(data['origin'],{'quote':'别急我在呢','story_version':base})
        self.error('QUOTE_NOT_IN_STORY',self.store.save_line_origin,self.owner,self.work['id'],{'base_version':self.work['version'],'line_id':lid,'quote':'虚构原句'})
        self.error('NOT_FOUND',self.store.line_comparison,'foreign',self.work['id'],lid)
    def test_stable_line_history_marks_removal_and_does_not_match_by_index(self):
        lid=self.work['lyrics'][0]['lines'][0]['id'];lines=copy.deepcopy(self.work['lyrics'][0]['lines']);lines[0]['text']='别急我一直在';lines.append({'text':'月亮等我回家'})
        self.work=self.store.save_lyrics(self.owner,self.work['id'],{'base_version':self.work['version'],'lines':lines})
        lines=self.work['lyrics'][-1]['lines'][1:];self.work=self.store.save_lyrics(self.owner,self.work['id'],{'base_version':self.work['version'],'lines':lines})
        data=self.store.line_comparison(self.owner,self.work['id'],lid);self.assertEqual([v['text'] for v in data['timeline']],['别急我在呢','别急我一直在',None]);self.assertEqual(data['first']['number'],1)
    def test_intro_consent_phrase_scope_and_rights(self):
        aid,clip,body,path=self.source(share=False,export=False)
        self.error('NOT_FOUND',self.store.intro_inputs,'foreign',self.work['id'],aid,body)
        self.error('CONFIRM_REQUIRED',self.store.intro_inputs,self.owner,self.work['id'],aid,dict(body,voice_rights=False))
        self.error('INTRO_PHRASE_MISMATCH',self.store.intro_inputs,self.owner,self.work['id'],aid,dict(body,phrase='不存在的原话'))
        self.error('INVALID_INTRO_RANGE',self.store.intro_inputs,self.owner,self.work['id'],aid,dict(body,end_seconds=float('nan')))
        before=self.store.intro_inputs(self.owner,self.work['id'],aid,body);result=compose_intro(before['clip_bytes'],before['clip_mime'],path,self.root/'intro.wav',.25,2.25)
        audio=self.store.commit_intro(self.owner,self.work['id'],aid,body,before,result)
        self.assertAlmostEqual(audio['duration'],22.12,places=2);self.assertFalse(audio['export_allowed']);self.assertFalse(audio['share_allowed']);self.assertEqual(audio['lyrics'],before['audio']['lyrics']);self.assertEqual(path.read_bytes(),wav(20,44100,channels=2))
        self.error('INTRO_ALREADY_PRESENT',self.store.intro_inputs,self.owner,self.work['id'],audio['id'],body)
    def test_late_intro_commit_after_clip_delete_does_not_save(self):
        aid,clip,body,path=self.source();before=self.store.intro_inputs(self.owner,self.work['id'],aid,body);self.store.delete_input_clip(self.owner,clip['id'])
        self.error('NOT_FOUND',self.store.commit_intro,self.owner,self.work['id'],aid,body,before,{'asset_name':'late.wav','duration':22,'intro_duration':2,'sha256':'x','size_bytes':1})
    def test_deleting_clip_removes_composition_share_not_original_song(self):
        aid,clip,body,path=self.source();before=self.store.intro_inputs(self.owner,self.work['id'],aid,body);result=compose_intro(before['clip_bytes'],before['clip_mime'],path,self.root/'intro.wav',.25,2.25)
        audio=self.store.commit_intro(self.owner,self.work['id'],aid,body,before,result);share=self.store.create_share(self.owner,self.work['id'],{'confirm':True,'audio_id':audio['id']})
        self.store.delete_input_clip(self.owner,clip['id']);self.error('NOT_FOUND',self.store.media_asset,self.owner,audio['id']);self.error('NOT_FOUND',self.store.public_share,share['token']);self.assertTrue(self.store.media_asset(self.owner,aid))
    def test_native_resampling_selection_and_entire_song_are_preserved(self):
        voice=wav(3,16000,230);song=self.root/'song.wav';song.write_bytes(wav(20,48000,470,2));output=self.root/'intro.wav'
        result=compose_intro(voice,'audio/wav',song,output,1,2);self.assertAlmostEqual(result['duration'],21.12,places=2)
        with wave.open(str(output),'rb') as stream:pcm=stream.readframes(stream.getnframes());self.assertEqual(stream.getframerate(),48000)
        with wave.open(str(song),'rb') as stream:original=stream.readframes(stream.getnframes())
        self.assertEqual(pcm[-len(original):],original);self.assertEqual(output.stat().st_mode&0o777,0o600)
    def test_source_deleted_during_render_cleans_file(self):
        app=Application(self.root/'application',gateway=object());self.addCleanup(app.close);app.store=self.store;app.assets=self.root
        aid,clip,body,path=self.source()
        original=compose_intro
        def deleted(*args):
            result=original(*args);self.store.delete_input_clip(self.owner,clip['id']);return result
        with patch('server.compose_intro',side_effect=deleted):self.error('NOT_FOUND',app.intro,self.owner,self.work['id'],aid,body)
        self.assertEqual(list(self.root.glob('intro_*.wav')),[])
