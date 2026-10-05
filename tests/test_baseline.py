import json,hashlib,unittest
from pathlib import Path
from backend.domain import _parse_lines,DomainError
ROOT=Path(__file__).resolve().parents[1]
class FrozenBaselineTests(unittest.TestCase):
    def test_frozen_hash_and_locked_mutations(self):
        raw=(ROOT/'evaluation/hard-cases-v1.jsonl').read_bytes();manifest=json.loads((ROOT/'evaluation/manifest-v1.json').read_text())
        self.assertEqual(hashlib.sha256(raw).hexdigest(),manifest['sha256'])
        for case in map(json.loads,raw.decode().splitlines()):
            if case['type']!='locked_line':continue
            with self.subTest(case=case['id']):
                original=[{'id':'line_1','text':case['input'],'locked':True,'locked_spans':[]},{'id':'line_2','text':'今天先这样','locked':False,'locked_spans':[]}]
                proposed=[dict(original[0],text=case['mutation']),original[1]]
                with self.assertRaises(DomainError) as ctx:_parse_lines(proposed,original)
                self.assertEqual(ctx.exception.code,'LOCK_CONFLICT')
