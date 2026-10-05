"""Explicit, bounded live checks. Never auto-retry paid requests or certify singing."""
import argparse
import base64
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from server import Application
from backend.domain import DomainError
from backend.audio_review import inspect_mp3
from backend.providers import TokenHubCandidate


def run(directory, modes, speech_file=None, expected=None):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    app = Application(directory / 'isolated-site')
    result = {'started_at':time.strftime('%Y-%m-%dT%H:%M:%S%z'), 'automatic_retries':0, 'results':[]}
    owner = app.store.create_session()['owner_id']
    try:
        if 'lyrics' in modes:
            cases = [('昨天朋友陪我赶末班车，临走说“别急，我在呢”。我想把这句话唱给她。','别急，我在呢'),
                     ('今天第一次做完自己的小产品，窗外下着雨。我想记住这一刻，不要写爱情或离别。','第一次')]
            for story, preserve in cases:
                item = {'feature':'lyrics','story':story,'preserve':preserve}
                try:
                    work = app.store.create_work(owner, {'story':story,'service_consent':True,
                        'intent':{'theme':'生活记录','attitude':'温暖','preserve':preserve,'avoid':'离别','recipient':'自己'}})
                    before = app.store.get_work(owner,work['id'])
                    candidate = app.candidate(owner,work['id'],{'base_version':work['version'],
                        'instruction':'根据这段故事写2至4行短歌词，保留指定原话。','paid_call_confirmed':True})
                    after = app.store.get_work(owner,work['id'])
                    item.update(success=True,lines=candidate['lines'],requires_confirmation=candidate['requires_confirmation'],
                        work_unchanged_before_confirmation=before==after)
                except DomainError as exc:
                    item.update(success=False,error_code=exc.code,message=exc.message)
                result['results'].append(item)
                print(json.dumps(item,ensure_ascii=False),flush=True)
        if 'music' in modes:
            # Original test material created for this acceptance run.
            lyrics = '[Verse]\n别急我在呢\n晚风陪你慢慢走'
            item = {'feature':'music','lyrics':lyrics,'singing_verified':False,'rights_verified':False}
            try:
                output = directory/'live-original.mp3'
                generated = TokenHubCandidate().generate(lyrics,
                    '中文舒缓流行，清晰自然人声，20秒短歌。只唱给定两句，各唱一次，不添加任何歌词。无前奏、间奏，唱完自然结束，总时长15至30秒。',
                    output,confirm_paid_call=True,confirm_input_rights=True)
                item.update(success=True,metadata=generated,inspection=inspect_mp3(output))
            except DomainError as exc:
                item.update(success=False,error_code=exc.code,message=exc.message)
            result['results'].append(item)
            print(json.dumps(item,ensure_ascii=False),flush=True)
        if 'speech' in modes:
            mime = {'.wav':'audio/wav','.mp3':'audio/mpeg','.m4a':'audio/mp4','.mp4':'audio/mp4'}[speech_file.suffix.lower()]
            item = {'feature':'speech','source':str(speech_file),'expected_text':expected}
            try:
                data = speech_file.read_bytes()
                response = app.gateway.speech({'mime':mime,'audio_base64':base64.b64encode(data).decode(),
                    'consent':True,'paid_call_confirmed':True})
                item.update(success=True,response=response)
            except DomainError as exc:
                item.update(success=False,error_code=exc.code,message=exc.message)
            result['results'].append(item)
            print(json.dumps(item,ensure_ascii=False),flush=True)
    finally:
        app.close()
        result['finished_at']=time.strftime('%Y-%m-%dT%H:%M:%S%z')
        path=directory/'live-results.json'
        path.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
        path.chmod(0o600)


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--features',choices=['lyrics','music','speech'],nargs='+',required=True)
    parser.add_argument('--speech-file',type=Path)
    parser.add_argument('--expected-text')
    parser.add_argument('--confirm-paid-calls',action='store_true')
    args=parser.parse_args()
    if not args.confirm_paid_calls:
        parser.error('真实调用可能计费，需显式传入 --confirm-paid-calls')
    if 'speech' in args.features and not args.speech_file:
        parser.error('真人转写需要 --speech-file')
    run(args.output,args.features,args.speech_file,args.expected_text)
