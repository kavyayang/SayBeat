"""Offline PCM composition. Originals remain untouched; no external calls."""
import array,os,subprocess,tempfile,wave
from pathlib import Path
from .domain import DomainError
from .audio_review import inspect_wav

def compose_intro(clip_bytes,clip_mime,song_path,output,start,end):
    output=Path(output)
    if output.exists():raise DomainError(409,'OUTPUT_EXISTS','拼接文件已经存在')
    with tempfile.TemporaryDirectory(prefix='shuoyipai-intro-') as directory:
        root=Path(directory);source=root/('voice'+{'audio/wav':'.wav','audio/mpeg':'.mp3','audio/mp3':'.mp3','audio/mp4':'.m4a'}.get(clip_mime,'.audio'));source.write_bytes(clip_bytes)
        try:
            decoded=[]
            for index,path in enumerate((source,Path(song_path))):
                target=root/f'part-{index}.wav'
                process=subprocess.run(['/usr/bin/afconvert','-f','WAVE','-d','LEI16@48000','-c','2',str(path),str(target)],capture_output=True,timeout=40)
                if process.returncode or target.stat().st_size>32*1024*1024:raise ValueError()
                with wave.open(str(target),'rb') as stream:
                    if (stream.getnchannels(),stream.getsampwidth(),stream.getframerate())!=(2,2,48000):raise ValueError()
                    pcm=stream.readframes(stream.getnframes())
                    if len(pcm)!=stream.getnframes()*4:raise ValueError()
                    decoded.append(pcm)
            first,last=round(start*48000),round(end*48000)
            if first<0 or last*4>len(decoded[0]) or last<=first:raise ValueError()
            voice=decoded[0][first*4:last*4];song=decoded[1]
            if max(map(abs,array.array('h',voice)),default=0)<32:raise ValueError()
            # A brief explicit gap separates speech and singing; the complete song follows.
            pcm=voice+bytes(round(.12*48000)*4)+song
            fd=os.open(output,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'wb') as file:
                with wave.open(file,'wb') as stream:
                    stream.setnchannels(2);stream.setsampwidth(2);stream.setframerate(48000);stream.writeframes(pcm)
            result=inspect_wav(output)
            if not 15<=len(song)/4/48000<=30.05 or result['duration']>36.2:raise ValueError()
            return dict(result,asset_name=output.name,intro_duration=len(voice)/4/48000,gap_seconds=.12)
        except (OSError,ValueError,wave.Error,subprocess.TimeoutExpired):
            output.unlink(missing_ok=True)
            raise DomainError(422,'COMPOSITION_FAILED','本机拼接失败，原声和歌曲均保留；请检查选取范围及音频文件') from None
