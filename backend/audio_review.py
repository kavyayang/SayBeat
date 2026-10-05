"""Local, full-stream audio validation; no inference of singing or licences."""
import array
import hashlib
import ipaddress
import re
import socket
import ssl
import subprocess
import tempfile
import unicodedata
import urllib.parse
import wave
import os
from http.client import HTTPSConnection
from pathlib import Path
from .domain import DomainError


def download_music(url):
    """Fetch a vendor signed URL without credentials, redirects or private networks.

    Connect to the validated IP directly, keeping the original TLS hostname.
    This also prevents a second DNS lookup from bypassing address validation.
    """
    try:
        parts = urllib.parse.urlsplit(url)
        host = parts.hostname
        if (parts.scheme != 'https' or not host or parts.port not in (None, 443)
                or parts.username or parts.password or parts.fragment):
            raise ValueError()
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
            raise ValueError()
        address = addresses[0][4]

        class PinnedConnection(HTTPSConnection):
            def connect(self):
                raw = socket.socket(addresses[0][0], socket.SOCK_STREAM)
                raw.settimeout(self.timeout)
                try:
                    raw.connect(address)
                    self.sock = self._context.wrap_socket(raw, server_hostname=host)
                except Exception:
                    raw.close()
                    raise

        connection = PinnedConnection(host, timeout=45, context=ssl.create_default_context())
        try:
            target = urllib.parse.urlunsplit(('', '', parts.path or '/', parts.query, ''))
            connection.request('GET', target, headers={'Accept': 'audio/mpeg, application/octet-stream'})
            response = connection.getresponse()
            if response.status != 200:
                raise ValueError()
            audio = response.read(16 * 1024 * 1024 + 1)
            if not 1000 <= len(audio) <= 16 * 1024 * 1024:
                raise ValueError()
            return audio
        finally:
            connection.close()
    except Exception:
        raise DomainError(502, 'AUDIO_INVALID', 'HTTPS 链接音频下载失败或不安全；签名链接未保存，请核对调用记录') from None


def inspect_mp3(path):
    """Decode all MP3 frames, then measure PCM duration and reject silence."""
    path = Path(path)
    with tempfile.TemporaryDirectory(prefix='shuoyipai-audio-') as directory:
        decoded = Path(directory) / 'decoded.wav'
        try:
            probe = subprocess.run(['/usr/bin/afinfo', '-r', str(path)],
                capture_output=True, text=True, timeout=20)
            if probe.returncode or not re.search(r'(?m)^\s*File type ID:\s*MPG3\s*$', probe.stdout):
                raise ValueError()
            result = subprocess.run(['/usr/bin/afconvert', '-f', 'WAVE', '-d', 'LEI16',
                str(path), str(decoded)], capture_output=True, timeout=30)
            if result.returncode or decoded.stat().st_size > 128 * 1024 * 1024:
                raise ValueError()
            with wave.open(str(decoded), 'rb') as stream:
                count, rate, channels = stream.getnframes(), stream.getframerate(), stream.getnchannels()
                if stream.getsampwidth() != 2 or not rate or channels not in (1, 2):
                    raise ValueError()
                pcm = stream.readframes(count)
                if len(pcm) != count * channels * 2 or not 0 < count / rate <= 600:
                    raise ValueError()
            samples = array.array('h', pcm)
            peak = max(map(abs, samples), default=0)
            if peak < 32:
                raise ValueError()
            return {'duration': count / rate, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                    'size_bytes': path.stat().st_size, 'decoded_frames': count,
                    'sample_rate': rate, 'channels': channels, 'peak': peak}
        except (OSError, ValueError, wave.Error, subprocess.TimeoutExpired):
            raise DomainError(422, 'AUDIO_INVALID', 'MP3无法完整解码、没有有效声音或文件已损坏，不能转为正式作品') from None


def words(text):
    # Ignore punctuation/spacing only, never translate, use fuzzy matching or drop words.
    return ''.join(c for c in unicodedata.normalize('NFC', text)
                   if not c.isspace() and not unicodedata.category(c).startswith('P'))


def inspect_wav(path):
    try:
        data = Path(path).read_bytes()
        with wave.open(str(path), 'rb') as stream:
            count, rate, channels = stream.getnframes(), stream.getframerate(), stream.getnchannels()
            if stream.getsampwidth() != 2 or channels not in (1, 2) or not rate:
                raise ValueError()
            pcm = stream.readframes(count)
        peak = max(map(abs, array.array('h', pcm)), default=0)
        if len(pcm) != count * channels * 2 or not 0 < count / rate <= 600 or peak < 32:
            raise ValueError()
        return {'duration':count/rate,'sha256':hashlib.sha256(data).hexdigest(),
                'size_bytes':len(data),'decoded_frames':count,'sample_rate':rate,'channels':channels,'peak':peak}
    except (OSError, ValueError, wave.Error):
        raise DomainError(422,'AUDIO_INVALID','短歌候选无法解码或没有有效声音') from None


def prepare_short(source, output):
    original = inspect_mp3(source)
    duration = original['duration']
    if 15 <= duration <= 30:
        raise DomainError(422,'DURATION_ALREADY_VALID','原曲时长已符合目标，请直接试听审核')
    # Bound the effect to modest tempo changes. One second is reserved for its tail.
    target = 28.5 if duration > 30 else 15
    rate = duration / target
    if not .8 <= rate <= 1.25:
        raise DomainError(422,'DURATION_NEEDS_NEW_GENERATION','时长偏差过大，无法用轻微调速保留整曲；请修改歌词或主动重新生成')
    output = Path(output)
    if output.exists():
        raise DomainError(409,'OUTPUT_EXISTS','短歌候选已经存在，请刷新')
    source_code = Path(__file__).with_name('timepitch.m')
    with tempfile.TemporaryDirectory(prefix='shuoyipai-tempo-') as directory:
        root = Path(directory)
        binary, floating, pcm_file = root/'renderer', root/'float.wav', root/'pcm.wav'
        commands = [
            ['/usr/bin/clang','-fobjc-arc','-framework','AVFoundation','-framework','Foundation',str(source_code),'-o',str(binary)],
            [str(binary),str(Path(source).resolve()),str(floating),str(rate)],
            ['/usr/bin/afconvert','-f','WAVE','-d','LEI16',str(floating),str(pcm_file)],
        ]
        try:
            for command in commands:
                result = subprocess.run(command,capture_output=True,timeout=45)
                if result.returncode:
                    raise ValueError()
            # Write canonical PCM16 WAV; preserve the full rendered frame sequence.
            with wave.open(str(pcm_file),'rb') as stream:
                params, pcm = stream.getparams(), stream.readframes(stream.getnframes())
            fd = os.open(output,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            try:
                with os.fdopen(fd,'wb') as file:
                    with wave.open(file,'wb') as stream:
                        stream.setparams(params)
                        stream.writeframes(pcm)
                inspection = inspect_wav(output)
                if not 15 <= inspection['duration'] <= 30:
                    raise ValueError()
            except Exception:
                output.unlink(missing_ok=True)
                raise
            return {**inspection,'asset_name':output.name,'mime':'audio/wav',
                'processing':'whole_song_pitch_preserving_tempo','rate':rate,
                'source_sha256':original['sha256'],'source_duration':duration,'tail_seconds':1}
        except (OSError, ValueError, wave.Error, subprocess.TimeoutExpired):
            raise DomainError(422,'AUDIO_PROCESSING_FAILED','本机整曲调速未完成；原始 MP3仍保留，不会截断或自动批准作品') from None
