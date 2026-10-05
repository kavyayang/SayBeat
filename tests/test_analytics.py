import json
import tempfile
import unittest
from pathlib import Path
from datetime import datetime,timedelta,timezone
from unittest.mock import patch
from backend.domain import Store,DomainError

class AnalyticsTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=Store(Path(self.tmp.name)/'test.sqlite3');self.owner='owner-test';self.other='owner-other'
    def work(self):
        return self.store.create_work(self.owner,{'story':'我的冰块先下班','service_consent':True,'intent':{'theme':'日常','attitude':'平静','preserve':'','avoid':'','recipient':''}})
    def events(self,name=None):
        with self.store._transaction() as c:
            return [dict(r) for r in c.execute('SELECT * FROM events') if not name or r['name']==name]
    def test_context_refusal_does_not_block_creation(self):
        self.assertFalse(self.store.analytics_context(self.owner)['sampling_enabled'])
        with self.assertRaises(DomainError): self.store.start_study(self.owner,{'consent':False})
        self.assertTrue(self.work()['id'])
        with self.store._transaction() as c:self.assertEqual(c.execute('SELECT count(*) FROM play_studies').fetchone()[0],0)
    def test_context_rejects_text_and_filters_internal_traffic(self):
        with self.assertRaises(DomainError):self.store.analytics_context(self.owner,{'story':'私密'})
        self.store.analytics_context(self.owner,{'traffic':'test','device':'desktop'})
        self.store.add_event(self.owner,{'event_id':'entry','name':'create_entry_view'})
        work=self.work()
        self.assertEqual(self.store.diagnostics(self.owner)['funnel'][0]['sessions'],0)
        diag=self.store.diagnostics(self.owner,traffic='test');self.assertEqual(diag['funnel'][1]['sessions'],1)
        self.assertNotIn(work['story'],json.dumps(diag,ensure_ascii=False))
        self.assertEqual(self.store.diagnostics(self.other,traffic='all')['events']['server'],0)
    def test_session_boundary_and_idempotent_client_event(self):
        start=datetime(2026,10,1,tzinfo=timezone.utc)
        with patch('backend.domain._now',return_value=start):a=self.store.analytics_context(self.owner)['session_id']
        with patch('backend.domain._now',return_value=start+timedelta(minutes=29)):b=self.store.analytics_context(self.owner)['session_id']
        with patch('backend.domain._now',return_value=start+timedelta(minutes=59)):c=self.store.analytics_context(self.owner)['session_id']
        self.assertEqual(a,b);self.assertNotEqual(b,c)
        body={'event_id':'same','name':'create_entry_view'}
        self.store.add_event(self.owner,body);self.store.add_event(self.owner,body)
        with self.store._transaction() as c:self.assertEqual(c.execute("SELECT count(*) FROM event_context WHERE origin='client'").fetchone()[0],1)
    def test_cost_missing_not_zero_and_ownership(self):
        work=self.work();cid=self.store.start_model_call(self.owner,'lyrics','test-model',work['id'])
        self.store.finish_model_call(self.owner,cid,'failed',2000,error_code='PROVIDER_TIMEOUT')
        diag=self.store.diagnostics(self.owner);self.assertEqual(diag['cost']['unknown_attempts'],1)
        self.assertIsNone(diag['cost']['cost_per_effective_kept_micros'])
        body={'confirmed':True,'cost_micros':12345,'reference':'测试账单条目'}
        with self.assertRaises(DomainError):self.store.record_call_cost(self.other,cid,body)
        self.store.record_call_cost(self.owner,cid,body)
        self.assertEqual(self.store.diagnostics(self.owner)['cost']['known_cost_micros'],12345)
    def test_independent_study_consent_withdrawal_and_delete(self):
        study=self.store.start_study(self.owner,{'consent':True});work=self.work()
        body={'work_id':work['id'],'independent_completion':True,'recognized_original':False,'wanted_revision':True,'needed_help':False}
        with self.assertRaises(DomainError):self.store.finish_study(self.other,study['id'],body)
        self.store.finish_study(self.owner,study['id'],body)
        self.assertEqual(self.store.diagnostics(self.owner)['study_summary']['completed'],1)
        self.store.withdraw_study(self.owner,study['id']);self.assertEqual(self.store.diagnostics(self.owner)['study_summary']['consented'],0)
        cid=self.store.start_model_call(self.owner,'lyrics','test',work['id'])
        self.store.delete_work(self.owner,work['id'],{'confirm':True,'base_version':work['version']})
        self.store.finish_model_call(self.owner,cid,'success',1000)
        with self.store._transaction() as c:
            for table in ('model_calls','events','event_context'):
                self.assertEqual(c.execute('SELECT count(*) FROM '+table).fetchone()[0],0)
    def test_job_events_server_deduplicated_even_with_client_spoof(self):
        work=self.work();work=self.store.save_lyrics(self.owner,work['id'],{'base_version':work['version'],'lines':[{'text':'冰块先下班'},{'text':'晚风替我转弯'}]})
        body={'base_version':work['version'],'confirmed':True,'lyric_id':work['current_lyric_id'],'idempotency_key':'unique'}
        job=self.store.create_job(self.owner,work['id'],body,True);self.store.create_job(self.owner,work['id'],body,True)
        self.store.add_event(self.owner,{'event_id':'spoof','name':'song_job_created','properties':{'work_id':work['id'],'job_id':job['id']}})
        self.store.cancel_job(self.owner,job['id']);self.store.cancel_job(self.owner,job['id'])
        diag=self.store.diagnostics(self.owner);self.assertEqual(diag['jobs']['total'],1);self.assertEqual(diag['jobs']['statuses']['cancelled'],1)
        self.assertEqual(len(self.events('song_job_terminal')),1)
    def test_funnel_requires_sequence_and_same_work(self):
        self.work() # confirmed before entry; must not reach stage 2
        self.store.add_event(self.owner,{'event_id':'entry','name':'create_entry_view'})
        self.assertEqual(self.store.diagnostics(self.owner)['funnel'][1]['sessions'],0)
        self.work();self.assertEqual(self.store.diagnostics(self.owner)['funnel'][1]['sessions'],1)
    def test_recover_interrupted_request_without_inventing_latency_or_cost(self):
        cid=self.store.start_model_call(self.owner,'lyrics','test')
        self.assertEqual(self.store.abort_pending_calls(),1)
        self.assertEqual(self.store.abort_pending_calls(),0)
        call=self.store.diagnostics(self.owner)['calls'][0]
        self.assertEqual(call['error_code'],'SERVER_RESTARTED')
        self.assertIsNone(call['latency_ms']);self.assertIsNone(call['cost_micros'])
    def test_schema_four_migrates_non_destructively(self):
        work=self.work()
        with self.store._transaction() as c:
            for table in ('event_context','model_calls','play_studies','analytics_sessions','analytics_settings'):c.execute('DROP TABLE '+table)
            c.execute('DELETE FROM schema_versions WHERE version=5');c.execute('PRAGMA user_version=4')
        reopened=Store(self.store.db_path);self.assertEqual(reopened.get_work(self.owner,work['id'])['story'],work['story'])
        self.assertEqual(reopened.diagnostics(self.owner)['missing']['legacy_events'],1)

if __name__=='__main__':unittest.main()
