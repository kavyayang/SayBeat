import json,tempfile,unittest
from pathlib import Path
from unittest.mock import Mock,patch
from backend.providers import MurekaCandidate
from backend.domain import DomainError

class MurekaAdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'song.mp3';self.opener=Mock();self.client=MurekaCandidate(api_key='test-key',opener=self.opener)
    def error(self,code,fn,*args,**kwargs):
        with self.assertRaises(DomainError) as e:fn(*args,**kwargs)
        self.assertEqual(e.exception.code,code)
    def test_requires_mureka_model_and_confirmations(self):
        self.error('CONFIRMATION_REQUIRED',self.client.generate,'a','b',self.path)
        self.error('INVALID_LYRICS',self.client.generate,'a','b',self.path,model='minimax-music-v3.0',confirm_paid_call=True,confirm_input_rights=True)
        self.error('INVALID_PROMPT',self.client.generate,'a','x'*1025,self.path,confirm_paid_call=True,confirm_input_rights=True)
    def test_payload_uses_v9_n_one_and_downloads_first_choice(self):
        response=Mock();response.read.return_value=json.dumps({'request_id':'1234567890abcdef','watermarked':True,'choices':[{'url':'https://audio.example/song.mp3','duration':20000}]}).encode();response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False);self.opener.open.return_value=response
        with patch('backend.providers.download_music',return_value=b'ID3'+b'x'*1000) as download,patch('backend.providers.subprocess.run') as run,patch('backend.providers.inspect_mp3',return_value={'duration':20.0}):
            run.return_value=Mock(returncode=0,stdout='File type ID: MPG3\n estimated duration: 20.0 sec')
            result=self.client.generate('第一句\n第二句','温暖轻快',self.path,confirm_paid_call=True,confirm_input_rights=True)
        request=self.opener.open.call_args.args[0];body=json.loads(request.data)
        self.assertEqual(body,{'model':'mureka-music-v9','lyrics':'第一句\n第二句','prompt':'温暖轻快','n':1})
        download.assert_called_once_with('https://audio.example/song.mp3');self.assertEqual(result['request_id'],'1234567890abcdef');self.assertTrue(result['watermarked']);self.assertEqual(result['duration_seconds_probed'],20.0);self.assertTrue(self.path.exists())
    def test_invalid_choice_is_not_saved(self):
        response=Mock();response.read.return_value=b'{"choices":[]}';response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False);self.opener.open.return_value=response
        self.error('AUDIO_INVALID',self.client.generate,'a','b',self.path,confirm_paid_call=True,confirm_input_rights=True)
        self.assertFalse(self.path.exists())
