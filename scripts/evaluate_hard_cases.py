"""Run frozen offline safety baseline; never calls an external model."""
import hashlib,json,subprocess,sys,shutil
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend.domain import _parse_lines,DomainError
root=Path(__file__).resolve().parents[1]
node=shutil.which('node') or str(Path.home()/'.cache/codex-runtimes/codex-primary-runtime/dependencies/node/bin/node')
result=json.loads(subprocess.check_output([node,str(root/'scripts/quality_baseline.cjs')],text=True))
rows=[json.loads(line) for line in (root/'evaluation/hard-cases-v1.jsonl').read_text().splitlines()]
for case in rows:
 if case['type']!='locked_line':continue
 original=[dict(id='line1',text=case['input'],locked=True,locked_spans=[]),dict(id='line2',text='今天先这样',locked=False,locked_spans=[])]
 try:_parse_lines([dict(original[0],text=case['mutation']),original[1]],original);actual='accepted'
 except DomainError as exc:actual=exc.code
 result['results'].append(dict(id=case['id'],status='pass' if actual==case['expected'] else 'fail',actual=actual,expected=case['expected']))
result.update(scope='offline_safety_invariants_not_music_quality',passed=sum(r['status']=='pass' for r in result['results']),failed=sum(r['status']=='fail' for r in result['results']))
text=json.dumps(result,ensure_ascii=False,indent=2)+'\n'
if len(sys.argv)>1:Path(sys.argv[1]).write_text(text)
print(json.dumps({k:v for k,v in result.items() if k!='results'},ensure_ascii=False))
sys.exit(bool(result['failed']))
