import tempfile,unittest
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from backend.domain import Store,DomainError
from backend.diary import today

class DiaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'diary.db';self.s=Store(self.path);self.owner='diary-owner'
    def save(self,day='2026-01-01',phrase='别急，我在呢',**kwargs):
        return self.s.diary_save(self.owner,day,{'phrase':phrase,'base_version':0,**kwargs})
    def error(self,code,fn,*args,**kwargs):
        with self.assertRaises(DomainError) as e:fn(*args,**kwargs)
        self.assertEqual(e.exception.code,code)
    def monthly(self,entries,**kwargs):
        return self.s.diary_monthly(self.owner,{'month':'2026-01','service_consent':True,'entries':[{'day':e['day'],'base_version':e['version']} for e in entries],**kwargs})
    def test_daily_edit_conflict_persistence_and_owner(self):
        e=self.save();self.assertEqual(len(self.s.diary_month(self.owner,'2026-01')['entries']),1)
        self.assertEqual(self.s.diary_month('another','2026-01')['entries'],[])
        self.error('VERSION_CONFLICT',self.save)
        e2=self.save(phrase='晚风陪我回家',base_version=e['version']);self.assertGreater(e2['version'],e['version'])
        self.assertEqual(Store(self.path).diary_month(self.owner,'2026-01')['entries'][0]['phrase'],'晚风陪我回家')
    def test_invalid_dates_future_month_newlines_length(self):
        for day in ('2026-02-30','2026-1-01','9999-12-31'):
            self.error('INVALID_DAY',self.save,day)
        self.error('INVALID_MONTH',self.s.diary_month,self.owner,'2026-13')
        self.error('INVALID_PHRASE',self.save,'2026-01-01','一\n二')
        self.error('VALIDATION_ERROR',self.save,'2026-01-01','话'*81)
        self.assertEqual(self.s.diary_month(self.owner,'2024-02')['days'],29)
        self.assertEqual(self.s.diary_month(self.owner,'2026-01')['today'],today().isoformat())
    def test_daily_work_requires_consent_reuses_and_recreates_deleted_work(self):
        e=self.save();body={'base_version':e['version'],'service_consent':True}
        self.error('CONSENT_REQUIRED',self.s.diary_work,self.owner,e['day'],{'base_version':e['version']})
        work=self.s.diary_work(self.owner,e['day'],body)
        updated=self.s.diary_month(self.owner,'2026-01')['entries'][0];body['base_version']=updated['version']
        self.assertEqual(self.s.diary_work(self.owner,e['day'],body)['id'],work['id'])
        self.s.delete_work(self.owner,work['id'],{'base_version':work['version'],'confirm':True})
        self.assertIsNone(self.s.diary_month(self.owner,'2026-01')['entries'][0]['work'])
        self.assertNotEqual(self.s.diary_work(self.owner,e['day'],body)['id'],work['id'])
    def test_monthly_chronological_locked_sources_no_jobs_and_idempotency(self):
        a=self.save();b=self.save('2026-01-03','今天终于见到你')
        work=self.monthly([b,a]);self.assertEqual(work['monthly_diary']['sources'][0]['day'],a['day'])
        self.assertEqual([l['text'] for l in work['lyrics'][0]['lines']],[a['phrase'],b['phrase']])
        self.assertTrue(all(l['locked'] for l in work['lyrics'][0]['lines']));self.assertEqual(work['jobs'],[])
        self.assertEqual(self.monthly([a,b])['id'],work['id']);self.assertEqual(len(self.s.list_works(self.owner)),1)
        self.assertEqual(len(self.s.diary_month(self.owner,'2026-01')['monthly']),1)
    def test_source_update_delete_do_not_change_monthly_snapshot(self):
        a=self.save();b=self.save('2026-01-02','今天也有好心情');work=self.monthly([a,b])
        updated=self.save(phrase='新的原话',base_version=a['version'])
        self.error('VERSION_CONFLICT',self.monthly,[a,b])
        new=self.monthly([updated,b]);self.assertNotEqual(work['id'],new['id'])
        self.s.diary_delete(self.owner,b['day'],{'base_version':b['version'],'confirm':True})
        self.assertEqual(self.s.get_work(self.owner,work['id'])['monthly_diary'],work['monthly_diary'])
        self.error('NOT_FOUND',self.monthly,[updated,b])
    def test_wrong_owner_link_and_monthly_access(self):
        a=self.save();b=self.save('2026-01-02','今天也有好心情');work=self.monthly([a,b])
        self.error('NOT_FOUND',self.s.diary_save,'another','2026-01-01',{'phrase':'别人的歌','base_version':0,'work_id':work['id']})
        self.error('NOT_FOUND',self.s.diary_monthly,'another',{'month':'2026-01','service_consent':True,'entries':[{'day':e['day'],'base_version':e['version']} for e in [a,b]]})
    def test_selection_bounds_duplicate_and_wrong_month(self):
        a=self.save();b=self.save('2026-02-01','另一个月')
        for entries in ([a],[a,a],[a,b],[a]*5):self.error('INVALID_SELECTION',self.monthly,entries)
        self.error('CONSENT_REQUIRED',self.monthly,[a,a],service_consent=False)
    def test_delete_requires_confirmation_and_recreate_rejects_stale_revision(self):
        a=self.save();body={'base_version':a['version'],'confirm':True}
        self.error('CONFIRM_REQUIRED',self.s.diary_delete,self.owner,a['day'],{'base_version':a['version']})
        self.s.diary_delete(self.owner,a['day'],body);new=self.save();self.assertNotEqual(a['version'],new['version'])
        self.error('VERSION_CONFLICT',self.s.diary_delete,self.owner,a['day'],body)
    def test_monthly_concurrent_creation_is_single_work(self):
        entries=[self.save(),self.save('2026-01-02','今天也有好心情')]
        with ThreadPoolExecutor(max_workers=4) as pool:
            works=list(pool.map(lambda _:self.monthly(entries),range(4)))
        self.assertEqual(len({w['id'] for w in works}),1)
    def test_schema5_migrates_without_modifying_existing_work(self):
        e=self.save();w=self.s.diary_work(self.owner,e['day'],{'base_version':e['version'],'service_consent':True})
        with self.s._transaction() as c:
            c.execute('DROP TABLE diary_entries');c.execute('DELETE FROM schema_versions WHERE version=6');c.execute('PRAGMA user_version=5')
        upgraded=Store(self.path);self.assertEqual(upgraded.get_work(self.owner,w['id']),w)
        self.assertEqual(upgraded.diary_month(self.owner,'2026-01')['entries'],[])
