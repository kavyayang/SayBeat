import copy
import hashlib
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import Mock, patch

from backend.domain import Store, DomainError
from backend.audio_review import download_music, inspect_mp3, inspect_wav, prepare_short
import io
import wave


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Path(self.tmp.name) / 'test.sqlite3')
        self.owner = self.store.create_session()['owner_id']
        self.work = self.store.create_work(self.owner, {'story':'原创测试故事', 'service_consent':True,
            'intent': {'theme':'感谢','attitude':'温暖','preserve':'','avoid':'','recipient':'朋友'}})
        self.work = self.store.save_lyrics(self.owner, self.work['id'], {'base_version':1,
            'source':'manual','lines':[{'text':'你在雨里等我。'},{'text':'把晴天装进口袋。'}]})
        self.job = self.store.create_job(self.owner, self.work['id'], {'base_version':self.work['version'],
            'lyric_id':self.work['current_lyric_id'],'idempotency_key':'review-test','confirmed':True}, True)
        self.jid = self.job['id']
        self.store.transition_job(self.jid, 'generating')
        self.file = Path(self.tmp.name) / (self.jid + '.candidate.mp3')
        self.file.write_bytes(b'UNIT TEST ONLY, NOT MUSIC')
        self.digest = hashlib.sha256(self.file.read_bytes()).hexdigest()
        self.store.register_candidate(self.owner, self.jid, {'duration_seconds_probed':22}, self.file)
        self.store.transition_job(self.jid, 'checking')
        self.body = {'confirm':True,'sha256':self.digest,'heard_lyrics':'你在雨里等我\n把晴天装进口袋',
            'rights_reference':'测试授权记录，不是实际平台许可'}
        for key in ('singing','no_missing_words','no_extra_words','quality','complete_ending',
                    'input_rights','output_rights','display_allowed','share_allowed','export_allowed'):
            self.body[key] = True
        self.inspection = {'sha256':self.digest,'duration':22,'decoded_frames':176000,'sample_rate':8000}

    def review(self, **changes):
        return self.store.review_candidate(self.owner,self.jid,dict(self.body,**changes),self.inspection)

    def test_complete_review_promotes_once_keeps_and_shares_snapshot(self):
        result = self.review()
        self.assertTrue(result['approved'])
        audio = self.store.media_asset(self.owner, result['job']['audio_id'])
        self.assertEqual(audio['audit']['inspection']['sha256'], self.digest)
        self.assertEqual(audio['audit']['method'], 'owner_listening_attestation')
        self.assertTrue(self.review()['replayed'])
        self.assertEqual(len(self.store.get_work(self.owner,self.work['id'])['audios']),1)
        self.store.keep_audio(self.owner,self.work['id'],audio['id'])
        share = self.store.create_share(self.owner,self.work['id'],{'audio_id':audio['id'],'confirm':True})
        self.assertEqual(self.store.public_share(share['token'])['audio']['id'], audio['id'])
        self.store.revoke_share(self.owner,self.work['id'],share['id'])
        with self.assertRaises(DomainError):
            self.store.public_share(share['token'])

    def test_wrong_missing_extra_or_repeated_words_never_promote(self):
        for text in ('你在雨里等她把晴天装进口袋','你在雨里等我','你在雨里等我把晴天装进口袋谢谢',
                     '你在雨里等我把晴天装进口袋你在雨里等我'):
            result = self.review(heard_lyrics=text)
            self.assertIn('LYRICS_MISMATCH',result['reasons'])
        self.assertEqual(self.store.get_work(self.owner,self.work['id'])['audios'],[])
        self.assertEqual(len(self.store.get_job(self.owner,self.jid)['snapshot']['reviews']),4)

    def test_long_short_truncated_and_quality_are_recorded_and_blocked(self):
        for duration in (14.99,30.01):
            self.inspection['duration']=duration
            self.assertFalse(self.review()['approved'])
        self.inspection['duration']=22
        for key in ('singing','no_missing_words','no_extra_words','quality','complete_ending','input_rights','output_rights','display_allowed'):
            self.assertFalse(self.review(**{key:False})['approved'])
        self.assertEqual(self.store.get_job(self.owner,self.jid)['status'],'checking')

    def test_boundaries_15_and_30_are_allowed(self):
        self.inspection['duration']=15
        self.assertTrue(self.review()['approved'])
        # Separate job for the other boundary.
        self.setUp()
        self.inspection['duration']=30
        self.assertTrue(self.review()['approved'])

    def test_rights_scopes_are_enforced(self):
        done=self.review(share_allowed=False,export_allowed=False)
        audio=self.store.media_asset(self.owner,done['job']['audio_id'])
        self.assertFalse(audio['export_allowed'])
        with self.assertRaises(DomainError) as raised:
            self.store.create_share(self.owner,self.work['id'],{'confirm':True,'audio_id':audio['id']})
        self.assertEqual(raised.exception.code,'SHARE_FORBIDDEN')

    def test_missing_attestation_changed_hash_and_foreign_owner_blocked(self):
        for changes in ({'sha256':'changed'},{'confirm':False},{'quality':'true'},{'rights_reference':''}):
            with self.assertRaises(DomainError):
                self.review(**changes)
        with self.assertRaises(DomainError):
            self.store.review_candidate('other-owner',self.jid,self.body,self.inspection)

    def test_cancel_and_delete_prevent_late_promotion(self):
        self.store.cancel_job(self.owner,self.jid)
        with self.assertRaises(DomainError):
            self.review()
        self.store.delete_work(self.owner,self.work['id'],{'base_version':self.work['version'],'confirm':True})
        with self.assertRaises(DomainError):
            self.review()

    def test_restart_retains_registered_candidate_but_not_inflight_job(self):
        reopened=Store(Path(self.tmp.name)/'test.sqlite3')
        self.assertEqual(reopened.abort_incomplete_jobs({self.jid}),[])
        self.assertEqual(reopened.get_job(self.owner,self.jid)['status'],'checking')
        self.assertEqual(len(reopened.abort_incomplete_jobs()),1)

    def test_prepared_audio_binds_exact_rendered_file_and_requires_processing_rights(self):
        prepared=dict(self.inspection,asset_name=self.jid+'.short.wav',mime='audio/wav',
                      sha256='different-render-hash',source_sha256=self.digest,processing='whole_song_pitch_preserving_tempo')
        self.store.register_prepared(self.owner,self.jid,prepared)
        body=dict(self.body,variant='short',sha256=prepared['sha256'])
        inspection=dict(self.inspection,sha256=prepared['sha256'])
        result=self.store.review_candidate(self.owner,self.jid,body,inspection)
        self.assertIn('PROCESSING_NOT_LICENSED',result['reasons'])
        result=self.store.review_candidate(self.owner,self.jid,dict(body,processing_rights=True),inspection)
        audio=self.store.media_asset(self.owner,result['job']['audio_id'])
        self.assertEqual(audio['asset_name'],prepared['asset_name'])
        self.assertEqual(audio['mime'],'audio/wav')
        self.assertEqual(audio['audit']['source_sha256'],self.digest)

    def test_prepared_file_source_change_and_duplicate_are_blocked(self):
        prepared={'source_sha256':'wrong','sha256':'render-hash'}
        with self.assertRaises(DomainError):
            self.store.register_prepared(self.owner,self.jid,prepared)
        prepared['source_sha256']=self.digest
        self.store.register_prepared(self.owner,self.jid,prepared)
        with self.assertRaises(DomainError):
            self.store.register_prepared(self.owner,self.jid,prepared)


class DownloadTests(unittest.TestCase):
    def test_unsafe_url_and_private_mixed_dns_never_connect(self):
        for url in ('http://example.com/a','https://u:p@example.com/a','https://example.com:8443/a'):
            with self.assertRaises(DomainError):
                download_music(url)
        for addresses in (['127.0.0.1'],['169.254.169.254'],['::1'],['8.8.8.8','10.0.0.1']):
            dns=[(socket.AF_INET,socket.SOCK_STREAM,6,'',(a,443)) for a in addresses]
            with patch('backend.audio_review.socket.getaddrinfo',return_value=dns), patch('backend.audio_review.socket.socket') as connect:
                with self.assertRaises(DomainError):
                    download_music('https://example.com/private?secret=signed')
                connect.assert_not_called()

    def test_pinned_tls_no_authorization_and_no_redirect(self):
        sock=Mock()
        response=sock.makefile.return_value
        response.readline.side_effect=[b'HTTP/1.1 302 Found\r\n',b'Location: http://127.0.0.1\r\n',b'\r\n']
        context=Mock()
        context.wrap_socket.return_value=sock
        dns=[(socket.AF_INET,socket.SOCK_STREAM,6,'',('8.8.8.8',443))]
        with patch('backend.audio_review.socket.getaddrinfo',return_value=dns), patch('backend.audio_review.socket.socket',return_value=sock), patch('backend.audio_review.ssl.create_default_context',return_value=context):
            with self.assertRaises(DomainError):
                download_music('https://example.com/a?secret=signed')
        sock.connect.assert_called_once_with(('8.8.8.8',443))
        context.wrap_socket.assert_called_once_with(sock,server_hostname='example.com')
        request=sock.sendall.call_args[0][0]
        self.assertNotIn(b'Authorization',request)

    def test_full_decoder_rejects_fake_mp3(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'fake.mp3'
            path.write_bytes(b'ID3'+b'not music'*100)
            with self.assertRaises(DomainError):
                inspect_mp3(path)

    def test_extreme_durations_never_crop_or_render(self):
        for duration in (1,11,15,30,36,600):
            with patch('backend.audio_review.inspect_mp3',return_value={'duration':duration}), patch('backend.audio_review.subprocess.run') as renderer:
                with self.assertRaises(DomainError):
                    prepare_short('unit-only.mp3','unused-output.wav')
                renderer.assert_not_called()

    def test_wav_full_frame_validation_rejects_silence_and_truncation(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'short.wav'
            for silent in (False,True):
                with wave.open(str(path),'wb') as stream:
                    stream.setnchannels(1);stream.setsampwidth(2);stream.setframerate(8000)
                    stream.writeframes((b'\x00\x00' if silent else b'\x00\x10')*120000)
                if silent:
                    with self.assertRaises(DomainError): inspect_wav(path)
                else:
                    self.assertEqual(inspect_wav(path)['duration'],15)
                    path.write_bytes(path.read_bytes()[:-10])
                    with self.assertRaises(DomainError): inspect_wav(path)
