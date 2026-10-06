"""Public Douyin downloads -> local speech transcripts -> verified owned-file cleanup."""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

SCHEMA = 2
MODEL_NAME = 'faster-whisper-large-v3-turbo-int8'
ROOT = Path(__file__).resolve().parent.parent
MODEL_DIR = ROOT / 'runtime' / 'model-turbo'
MEDIA_HOSTS = ('douyinvod.com', 'douyinstatic.com', 'douyin.com', 'ixiguavideo.com',
               'bdxiguastatic.com', 'zjcdn.com', 'bytecdn.cn', 'ibytedtos.com')
UA = 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148 Safari/604.1'


def emit(data):
    print(json.dumps(data, ensure_ascii=False), flush=True)


def digest(path):
    result = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            result.update(chunk)
    return result.hexdigest()


def child(root, *parts):
    """Reject symlinks/junctions that would route writes/deletion outside this root."""
    root = root.resolve()
    candidate = root.joinpath(*parts)
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root) or resolved != candidate.absolute():
        raise ValueError('输出路径包含重定向或越界，未操作文件')
    return candidate


def atomic_bytes(path, data):
    temp = path.with_name('.' + path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temp.open('xb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if temp.exists():
            temp.unlink()


def save_json(path, data):
    atomic_bytes(path, json.dumps(data, ensure_ascii=False, indent=2).encode('utf-8'))


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8'))


def trusted_media(url):
    parsed = urllib.parse.urlsplit(url)
    try:
        port = parsed.port
    except ValueError:
        return False
    host = (parsed.hostname or '').lower()
    return (parsed.scheme == 'https' and not parsed.username and not parsed.password
            and port in (None, 443)
            and any(host == suffix or host.endswith('.' + suffix) for suffix in MEDIA_HOSTS))


class TrustedRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        if not trusted_media(new_url):
            raise ValueError('媒体跳转到了非预期地址，已停止')
        return super().redirect_request(request, fp, code, message, headers, new_url)


def download(url, target):
    if not trusted_media(url):
        raise ValueError('没有可信的公开视频地址')
    request = urllib.request.Request(url, headers={'User-Agent': UA, 'Referer': 'https://jingxuan.douyin.com/'})
    opener = urllib.request.build_opener(TrustedRedirect())
    with opener.open(request, timeout=60) as response, target.open('xb') as stream:
        if not trusted_media(response.geturl()):
            raise ValueError('媒体响应地址不可信')
        for chunk in iter(lambda: response.read(1024 * 1024), b''):
            stream.write(chunk)
        stream.flush()
        os.fsync(stream.fileno())


def valid_mp4(path, expected_size=0):
    if not path.is_file() or path.stat().st_size < 32:
        return False
    if expected_size and path.stat().st_size != expected_size:
        return False
    with path.open('rb') as stream:
        return stream.read(12)[4:8] in (b'ftyp', b'styp')


def own_video(account, video_id):
    work = child(account, '.work', video_id)
    video = child(account, '.work', video_id, 'video.mp4')
    receipt = child(account, '.work', video_id, 'ownership.json')
    return work, video, receipt


def ownership_matches(video, receipt, video_id):
    if not video.exists() or not receipt.exists():
        return False
    data = read_json(receipt)
    return (data.get('schema') == SCHEMA and data.get('id') == video_id
            and data.get('created_by') == 'douyin-account-batch-download-v2'
            and data.get('size') == video.stat().st_size and data.get('sha256') == digest(video))


def cleanup_owned(account, video_id):
    _, video, receipt = own_video(account, video_id)
    if not video.exists():
        return False
    if not ownership_matches(video, receipt, video_id):
        raise ValueError('临时视频所有权或校验不符，未删除')
    # Never delete directories, glob patterns, imported videos, or paths from the receipt.
    video.unlink()
    return True


def acquire_video(account, job, downloader):
    video_id = str(job['id'])
    work, video, receipt = own_video(account, video_id)
    work.mkdir(parents=True, exist_ok=True)
    if video.exists():
        if ownership_matches(video, receipt, video_id) and valid_mp4(video, int(job.get('expected_bytes', 0))):
            return video
        raise ValueError('已有临时视频不属于本工具或已变化，未覆盖、未删除')
    urls = [url for url in job.get('media_urls', []) if trusted_media(url)]
    if not urls:
        raise ValueError('没有可信的公开视频地址')
    part = child(account, '.work', video_id, 'download-' + uuid.uuid4().hex + '.part')
    try:
        downloader(urls[0], part)
        if not valid_mp4(part, int(job.get('expected_bytes', 0))):
            raise ValueError('下载文件大小或 MP4 文件头校验失败')
        os.rename(part, video)
        save_json(receipt, {'schema': SCHEMA, 'id': video_id,
                          'created_by': 'douyin-account-batch-download-v2',
                          'size': video.stat().st_size, 'sha256': digest(video)})
        return video
    finally:
        if part.exists():
            part.unlink()


class LocalTranscriber:
    def __init__(self):
        self.model = None

    def __call__(self, video):
        from faster_whisper import WhisperModel
        if self.model is None:
            self.model = WhisperModel(str(MODEL_DIR), device='cpu', compute_type='int8',
                                      cpu_threads=min(8, os.cpu_count() or 2), local_files_only=True)
        iterator, info = self.model.transcribe(str(video), beam_size=5, vad_filter=True,
                                              vad_parameters={'min_silence_duration_ms': 500},
                                              condition_on_previous_text=False,
                                              initial_prompt='以下是简体中文的日常口语对话。')
        segments = [{'start': float(s.start), 'end': float(s.end), 'text': s.text.strip(),
                     'avg_logprob': float(s.avg_logprob), 'no_speech_prob': float(s.no_speech_prob),
                     'compression_ratio': float(s.compression_ratio)} for s in iterator if s.text.strip()]
        return {'duration': float(info.duration), 'language': info.language,
                'language_probability': float(info.language_probability), 'segments': segments}


def quality_issue(transcript):
    segments = transcript.get('segments', [])
    if not segments or not any(re.search(r'\w', s.get('text', ''), re.UNICODE) for s in segments):
        return 'no_speech', '没有识别到有效语音，保留临时视频'
    duration = float(transcript.get('duration', 0))
    if not math.isfinite(duration) or duration <= 0:
        return 'needs_review', '语音时长无效'
    previous = 0.0
    for s in segments:
        start, end = float(s['start']), float(s['end'])
        if (not math.isfinite(start) or not math.isfinite(end) or start < 0
                or end <= start or start + 0.05 < previous or end > duration + 2):
            return 'needs_review', '识别时间轴异常，保留视频'
        previous = end
        if (float(s.get('avg_logprob', -99)) < -1.2 or float(s.get('no_speech_prob', 1)) > 0.8
                or float(s.get('compression_ratio', 99)) > 2.4):
            return 'needs_review', '部分语音识别置信度偏低或重复异常，保留视频'
    texts = [re.sub(r'\W+', '', s['text']).lower() for s in segments]
    if len(texts) >= 4 and max(Counter(texts).values()) / len(texts) > 0.65:
        return 'needs_review', '识别内容大量重复，保留视频'
    return None, None


def timestamp(seconds):
    millis = round(seconds * 1000)
    hours, millis = divmod(millis, 3600000)
    minutes, millis = divmod(millis, 60000)
    seconds, millis = divmod(millis, 1000)
    return f'{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}'


def result_dir(account, video_id):
    return child(account, '文字', video_id)


def verified_result(folder, video_id):
    record_file = child(folder, 'record.json')
    if not record_file.exists():
        return None
    record = read_json(record_file)
    if record.get('schema') != SCHEMA or record.get('id') != video_id or record.get('status') != 'ok':
        return None
    for name in ('正文.txt', '字幕.srt', 'transcript.json'):
        path = child(folder, name)
        if not path.is_file() or path.stat().st_size == 0 or record.get('sha256', {}).get(name) != digest(path):
            return None
    return record


def process_job(account, account_name, job, transcriber, downloader=download, keep_videos=False):
    video_id = str(job['id'])
    if not re.fullmatch(r'\d{15,20}', video_id):
        raise ValueError('作品 ID 无效')
    folder = result_dir(account, video_id)
    existing = verified_result(folder, video_id)
    if existing:
        removed = False if keep_videos else cleanup_owned(account, video_id)
        return {'id': video_id, 'status': 'skipped', 'video_deleted': removed}
    if folder.exists() and any(folder.iterdir()):
        raise ValueError('文字目录已有未通过完整性检查的内容，未覆盖；请核对后另选输出目录')
    video = acquire_video(account, job, downloader)
    transcript = transcriber(video)
    status, reason = quality_issue(transcript)
    if status:
        review = child(account, '.work', video_id, 'review.json')
        save_json(review, {'status': status, 'reason': reason, 'transcript': transcript,
                           'id': video_id, 'title': job.get('title', '')})
        return {'id': video_id, 'status': status, 'reason': reason, 'video_deleted': False}
    # Publish the complete text directory atomically. A crash during writing leaves
    # only our staging files, so the next run can reuse the owned video and retry.
    child(account, '文字').mkdir(parents=True, exist_ok=True)
    staging = child(account, '.work', video_id, 'staging-' + uuid.uuid4().hex)
    staging.mkdir()
    source = 'https://www.douyin.com/video/' + video_id
    plain = ('作者：' + account_name + '\n标题：' + job.get('title', '') + '\n来源：' + source
             + '\n说明：机器转写，未人工校对；不包含画面动作说明。\n\n'
             + '\n'.join(s['text'] for s in transcript['segments']) + '\n')
    subtitle = '\n\n'.join(f"{n}\n{timestamp(s['start'])} --> {timestamp(s['end'])}\n{s['text']}"
                            for n, s in enumerate(transcript['segments'], 1)) + '\n'
    atomic_bytes(child(staging, '正文.txt'), plain.encode('utf-8'))
    atomic_bytes(child(staging, '字幕.srt'), subtitle.encode('utf-8'))
    save_json(child(staging, 'transcript.json'), {'model': MODEL_NAME, 'source': source, **transcript})
    hashes = {name: digest(child(staging, name)) for name in ('正文.txt', '字幕.srt', 'transcript.json')}
    save_json(child(staging, 'record.json'), {'schema': SCHEMA, 'id': video_id, 'status': 'ok',
              'title': job.get('title', ''), 'author': account_name, 'source': source, 'sha256': hashes})
    if verified_result(staging, video_id) is None:
        raise ValueError('暂存文字回读校验失败，未删除视频')
    if folder.exists():
        folder.rmdir()  # Only the already-checked empty result directory.
    os.rename(staging, folder)
    if verified_result(folder, video_id) is None:
        raise ValueError('文字输出回读校验失败，未删除视频')
    removed = False if keep_videos else cleanup_owned(account, video_id)
    return {'id': video_id, 'status': 'ok', 'video_deleted': removed}


@contextmanager
def account_lock(account):
    import msvcrt
    path = child(account, '.processing.lock')
    with path.open('a+b') as stream:
        if path.stat().st_size == 0:
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise ValueError('同一输出目录已有任务运行，请勿同时重复启动') from exc
        try:
            yield
        finally:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def run(request, account, keep_videos=False):
    account.mkdir(parents=True, exist_ok=True)
    transcriber = LocalTranscriber()
    results = []
    with account_lock(account):
        for index, job in enumerate(request['jobs'], 1):
            blocked = False
            try:
                result = process_job(account, request['account_name'], job, transcriber, keep_videos=keep_videos)
            except urllib.error.HTTPError as exc:
                blocked = exc.code in (401, 403, 429)
                result = {'id': job['id'], 'status': 'failed', 'reason': f'公开视频请求返回 HTTP {exc.code}', 'video_deleted': False}
            except Exception as exc:
                # Avoid leaking signed media URLs in exception messages or reports.
                message = re.sub(r'https?://\S+', '[地址已省略]', str(exc))[:600]
                result = {'id': job['id'], 'status': 'failed', 'reason': message, 'video_deleted': False}
            results.append(result)
            report = make_report(request, results, account)
            save_json(child(account, 'processing-report.json'), report)
            emit({'event': 'progress', 'index': index, 'total': len(request['jobs']), **result})
            if blocked:
                break
            time.sleep(0.7)
        report = make_report(request, results, account)
        save_json(child(account, 'processing-report.json'), report)
        emit(report)
        return 0 if report['status'] == 'complete' else 2


def make_report(request, results, account):
    counts = Counter(item['status'] for item in results)
    success = counts['ok'] + counts['skipped']
    return {'status': 'complete' if request['catalog_complete'] and success == request['listed_count'] else 'partial',
            'account_id': request['account_id'], 'account_name': request['account_name'],
            'catalog_complete': request['catalog_complete'], 'listed_count': request['listed_count'],
            'selected_count': len(request['jobs']), 'processed_count': len(results),
            'transcribed': counts['ok'], 'skipped': counts['skipped'], 'failed': counts['failed'],
            'needs_review': counts['needs_review'], 'no_speech': counts['no_speech'],
            'videos_deleted_this_run': sum(bool(item['video_deleted']) for item in results),
            'output_dir': str(account), 'results': results}


def check_native_runtime():
    import ctypes
    try:
        for name in ('msvcp140.dll', 'msvcp140_1.dll'):
            ctypes.WinDLL(name)
    except OSError as exc:
        raise RuntimeError('缺少或无法加载微软 Visual C++ x64 运行库；尚未下载或删除视频。'
                           '请先由用户确认，再从微软官方安装/修复，入口见随包 README.md。'
                           '无需登录抖音或安装 Python。') from exc


def check_runtime():
    check_native_runtime()
    from faster_whisper import WhisperModel
    import av
    import ctranslate2
    import onnxruntime
    for name in ('model.bin', 'config.json', 'tokenizer.json', 'vocabulary.json', 'preprocessor_config.json'):
        if not (MODEL_DIR / name).is_file():
            raise RuntimeError('随包识别模型不完整，请重新完整解压')
    emit({'status': 'ready', 'python': sys.version.split()[0], 'model': MODEL_NAME,
          'device': 'cpu', 'offline_transcription': True})


def main():
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser()
    parser.add_argument('--request', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--keep-videos', action='store_true')
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    try:
        if args.check:
            check_runtime()
            return 0
        if args.request is None or args.output is None:
            raise ValueError('缺少任务或输出目录')
        check_runtime()
        request = read_json(args.request)
        if not re.fullmatch(r'\d{15,20}', str(request['account_id'])):
            raise ValueError('作者 ID 无效')
        return run(request, args.output.resolve(), args.keep_videos)
    except Exception as exc:
        emit({'status': 'error', 'message': re.sub(r'https?://\S+', '[地址已省略]', str(exc))[:600]})
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
