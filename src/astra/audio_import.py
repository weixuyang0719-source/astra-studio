"""Prepare NCM as ordinary local audio, without a directory watcher or uploads.

The pinned ncmdump-py parser/decoder does the conversion. Its cover download
hook is deliberately overridden: import must work completely offline.
"""
from pathlib import Path
import json
import math
import os
import shutil
import tempfile

VERSION = 1


def _converter():
    try:
        from ncmdump import NeteaseCloudMusicFile
    except ImportError as exc:
        raise RuntimeError('NCM 导入组件未安装，请重新运行 Install-Astra.cmd。') from exc

    class OfflineNCM(NeteaseCloudMusicFile):
        def _try_get_cover_data(self):
            return self._cover_data_size

    return OfflineNCM


def _audio_info(path):
    import soundfile as sf
    info = sf.info(str(path))
    if info.frames <= 0 or info.samplerate <= 0 or not math.isfinite(info.duration):
        raise ValueError('转换结果不是有效音频。')
    return {'duration': info.duration, 'sample_rate': info.samplerate,
            'channels': info.channels, 'frames': info.frames}


def prepare_audio_import(source, cache_root, log=print):
    """Return a playable path and provenance. Ordinary audio is passed through.

    Call in a worker, never on the GUI thread. Source NCM is read-only. Cache
    commits are atomic and keyed by full source hash, not basename or mtime.
    """
    from .pipeline import fingerprint, project_lock, save_json
    source = Path(source).resolve(strict=True)
    if not source.is_file():
        raise ValueError('请选择本地音乐文件。')
    if source.suffix.lower() != '.ncm':
        return {'path': str(source), 'original_path': str(source), 'converted': False}
    with source.open('rb') as handle:
        if handle.read(8) != b'CTENFDAM':
            raise ValueError('NCM 文件头无效，可能未下载完成或不是受支持的 NCM。')
    log('准备 NCM 音频：本地转换，保留原文件')
    digest = fingerprint(source)
    folder = Path(cache_root).resolve() / 'ncm' / digest
    folder.mkdir(parents=True, exist_ok=True)
    manifest = folder / 'import.json'
    with project_lock(folder):
        try:
            record = json.loads(manifest.read_text(encoding='utf-8'))
            name = record['audio_file']
            if name not in ('audio.flac', 'audio.mp3'):
                raise ValueError('Invalid cache name')
            target = folder / name
            if (record['version'] == VERSION and record['source_sha256'] == digest
                    and fingerprint(target) == record['audio_sha256']):
                _audio_info(target)
                log('复用已转换的 NCM 音频')
                return dict(record, path=str(target), original_path=str(source), cached=True)
        except (OSError, ValueError, KeyError, TypeError, RuntimeError):
            pass
        with tempfile.TemporaryDirectory(prefix='convert-', dir=folder) as temporary:
            try:
                music = _converter()(source).decrypt()
                # The upstream writer infers extension from metadata. Detect
                # the payload too, so missing/wrong metadata cannot mislabel FLAC.
                music._decrypt_music_data()
                payload = music._music_data
                if payload.startswith(b'fLaC'):
                    fmt = 'flac'
                elif payload.startswith(b'ID3') or (len(payload) > 2 and payload[0] == 255 and payload[1] & 224 == 224):
                    fmt = 'mp3'
                else:
                    raise ValueError('NCM 内不是受支持的 FLAC / MP3 音频。')
                music.music_metadata.json['format'] = fmt
                result = music.dump_music(Path(temporary) / ('audio.' + fmt))
                info = _audio_info(result)
                if fingerprint(source) != digest:
                    raise ValueError('转换期间源文件发生变化，请等待下载完成后重试。')
                target = folder / ('audio.' + fmt)
                record = {'version': VERSION, 'converted': True, 'cached': False,
                          'original_path': str(source), 'source_sha256': digest,
                          'audio_file': target.name, 'audio_sha256': fingerprint(result),
                          'backend': 'ncmdump-py==1.1.6 (offline)', 'format': fmt,
                          'title': music.music_metadata.name,
                          'artists': music.music_metadata.artists, **info}
                result.replace(target)
                save_json(manifest, record)
                log(f'NCM 已准备：{fmt.upper()}，{info["duration"]:.1f} 秒')
                return dict(record, path=str(target))
            except (ValueError, TypeError, OSError, RuntimeError, IndexError, ZeroDivisionError) as exc:
                raise ValueError(f'NCM 导入失败，原文件已保留：{exc}') from exc


def convert_ncm_files(sources, output_root, cache_root, log=print, progress=None, cancelled=None):
    """Standalone conversion queue. Commit complete files without overwriting.

    Cancellation finishes the current file, then leaves unstarted files alone.
    The callable belongs on a worker; progress receives completed, total, record.
    """
    from .pipeline import fingerprint
    output = Path(output_root).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    paths = list(dict.fromkeys(str(Path(p).expanduser().resolve()) for p in sources))
    if not paths:
        raise ValueError('请选择至少一个 NCM 文件。')
    results = []
    for value in paths:
        if cancelled and cancelled():
            break
        source = Path(value)
        temporary = None
        try:
            if source.suffix.lower() != '.ncm':
                raise ValueError('此工具仅接受 NCM；普通音频请直接导入工作区。')
            imported = prepare_audio_import(source, cache_root, log)
            audio = Path(imported['path'])
            digest = imported['audio_sha256']
            stem, suffix = source.stem, audio.suffix
            sequence = 1
            while True:
                name = stem + (f' ({sequence})' if sequence > 1 else '') + suffix
                target = output / name
                if target.exists():
                    if target.is_file() and fingerprint(target) == digest:
                        status = 'reused'
                        break
                    sequence += 1
                    continue
                if temporary is None:
                    with tempfile.NamedTemporaryFile(prefix='.astra-convert-', suffix='.pending',
                                                     dir=output, delete=False) as handle:
                        temporary = Path(handle.name)
                    shutil.copyfile(audio, temporary)
                    if fingerprint(temporary) != digest:
                        raise ValueError('转换结果写入校验失败，请检查磁盘空间。')
                try:
                    if os.name == 'nt':
                        temporary.rename(target)  # Windows rename does not replace an existing destination.
                    else:
                        os.link(temporary, target)
                        temporary.unlink()
                    temporary = None
                    status = 'converted'
                    break
                except FileExistsError:
                    sequence += 1
            record = {'source': str(source), 'path': str(target), 'status': status,
                      'format': imported['format'], 'duration': imported['duration']}
        except (OSError, ValueError, RuntimeError) as exc:
            record = {'source': str(source), 'status': 'failed', 'error': str(exc)}
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        results.append(record)
        if progress:
            progress(len(results), len(paths), record)
    return {'output_root': str(output), 'results': results, 'total': len(paths),
            'converted': sum(r['status'] == 'converted' for r in results),
            'reused': sum(r['status'] == 'reused' for r in results),
            'failed': sum(r['status'] == 'failed' for r in results),
            'cancelled': len(results) < len(paths), 'remaining': len(paths)-len(results)}
