"""Persistent ownership and checkpoint receipts for a desktop workflow task."""
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import wave
from uuid import uuid4

_LEDGER = 'task-products.json'
_OWNER = '.astra-workflow-owner.json'
_TOKEN = re.compile(r'[0-9a-f]{32}\Z')
_INSTRUMENTS = {'piano': '钢琴分轨', 'guitar': '吉他分轨'}
_REPARSE = getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)
_ALLOWED_TOP = {_OWNER, 'process.log', '.running', '.running.guard', 'tempo_map.json',
                'project.json', 'source_tempo_map.json', 'piano.wav', 'guitar.wav'}
_ALLOWED_DIRS = {'cache', 'stems', 'notes', 'exports', 'editing', 'notation_revisions'}


def _write(path, value):
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=path.name+'-', delete=False,
                                         mode='w', encoding='utf-8') as handle:
            temporary = Path(handle.name)
            json.dump(value, handle, ensure_ascii=False, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _read(path):
    kind = _checked_stat(path)
    if not stat.S_ISREG(kind.st_mode):
        raise ValueError('任务记录无效，已保留文件。')
    with path.open(encoding='utf-8') as handle:
        return json.load(handle)


def _checked_stat(path):
    current = path.lstat()
    if stat.S_ISLNK(current.st_mode) or getattr(current, 'st_file_attributes', 0)&_REPARSE:
        raise ValueError('任务路径包含链接，无法确认文件归属。')
    return current


def _jobs(root):
    return Path(root).resolve() / '.cache' / 'jobs'


def _job_dir(root, token):
    if not isinstance(token, str) or not _TOKEN.fullmatch(token):
        raise ValueError('无效的任务编号。')
    return _jobs(root) / token


def _record(root, token):
    folder = _job_dir(root, token)
    if folder.resolve(strict=True) != folder:
        raise ValueError('任务记录目录已变化。')
    record = _read(folder/_LEDGER)
    if (not isinstance(record,dict) or record.get('version')!=1 or record.get('id')!=token
            or record.get('state') not in ('working','completed','cancelled','failed','kept','cleaned')
            or record.get('phase') not in ('separation','score')
            or 'project' not in record or not isinstance(record.get('original'),str)):
        raise ValueError('任务记录已变化。')
    return record


def _records(root,*,strict=False):
    for folder in _jobs(root).glob('*'):
        if not _TOKEN.fullmatch(folder.name) or not (folder/_LEDGER).is_file():
            continue
        try:
            yield _record(root,folder.name)
        except (OSError,ValueError,TypeError):
            if strict:
                raise ValueError('另一任务的记录无法验证，已保留这些文件。') from None


def _identity(info):
    return [info.st_dev, info.st_ino]


def _owned_project(record):
    project=Path(record['project'])
    parent=Path(record['output_root'])
    if (not project.is_absolute() or project.parent!=parent or
            project.resolve(strict=True)!=project or parent.resolve(strict=True)!=parent
            or not project.name.endswith('-'+record['owner_id'][:12])):
        raise ValueError('任务目录位置已变化。')
    info=_checked_stat(project)
    marker=_read(project/_OWNER)
    if (not stat.S_ISDIR(info.st_mode) or marker!={'version':1,'id':record['owner_id'],'project':str(project)}
            or _identity(info)!=record['project_identity']):
        raise ValueError('无法验证任务目录归属。')
    return project


def _entries(project):
    """Read only an exact managed project. Reject junctions and unknown paths."""
    result={}
    stack=[project]
    while stack:
        parent=stack.pop()
        for child in parent.iterdir():
            relative=child.relative_to(project)
            info=_checked_stat(child)
            directory=stat.S_ISDIR(info.st_mode)
            if relative.parts[0] not in (_ALLOWED_DIRS if directory or len(relative.parts)>1 else _ALLOWED_TOP):
                raise ValueError(f'发现未知产物 {relative.name}，已保留。')
            if child.resolve(strict=True)!=child or not (directory or stat.S_ISREG(info.st_mode)):
                raise ValueError('任务目录中存在无法确认的产物。')
            result[relative.as_posix()]={
                'kind':'directory' if directory else 'file','identity':_identity(info),
                'size':info.st_size,'modified':info.st_mtime_ns,'links':info.st_nlink}
            if directory:
                stack.append(child)
    return result


def begin_task(root, folder, request):
    """Return a worker request targeting a new owned directory or a kept prior checkpoint."""
    token=folder.name
    if folder.resolve(strict=True)!=_job_dir(root,token) or request.get('kind')!='run':
        raise ValueError('当前请求不是受管理的分轨任务。')
    phase=request['workflow_phase']
    if phase=='separation' and not request.get('workflow_origin'):
        source=Path(request['audio']).resolve(strict=True)
        output=Path(request['output']).expanduser().resolve()
        output.mkdir(parents=True,exist_ok=True)
        if output.resolve(strict=True)!=output or not source.is_file():
            raise ValueError('分轨输入或输出路径无效。')
        prefix=source.stem[:48].rstrip(' .') or 'audio'
        project=output/(prefix+'-'+token[:12])
        project.mkdir(exist_ok=False)
        _write(project/_OWNER,{'version':1,'id':token,'project':str(project)})
        project_identity=_identity(_checked_stat(project))
        owner_id=token
        original=str(source)
        stems={}
        before=[]
    elif phase in ('separation','score'):
        earlier=_record(root,request['workflow_origin'])
        project=_owned_project(earlier)
        original=earlier['original']
        stems=_valid_checkpoint(root,earlier)
        complete=bool(stems and _checkpoint_complete(earlier,stems))
        if (earlier['state'] not in ('completed','kept') or not stems
                or Path(request['audio']).resolve(strict=True)!=Path(original)
                or not _same_work(earlier['request'],request)
                or (phase=='score' and not complete)):
            raise ValueError('无法验证续做任务的原曲、选项或已完成分轨。')
        if any(other['project']==str(project) and other['state']=='working' for other in _records(root,strict=True)):
            raise ValueError('此项目还有正在运行的任务。')
        owner_id=earlier['owner_id']
        output=project.parent
        project_identity=earlier['project_identity']
        before=list(_entries(project))
    else:
        raise ValueError('未知的工作阶段。')
    if not project_identity[1]:
        raise ValueError('无法确认新任务目录的归属。')
    record={'version':1,'id':token,'owner_id':owner_id,'state':'working','phase':phase,
            'project':str(project),'output_root':str(output),'project_identity':project_identity,
            'original':original,'instruments':list(request['instruments']),'before':before,
            'checkpoint':stems,'request':{k:v for k,v in request.items()
                                        if k not in ('workflow_project','workflow_checkpoint')},
            'controller':_current_process()}
    _write(folder/_LEDGER,record)
    return {**request,'workflow_project':str(project),
            **({'workflow_checkpoint':stems} if stems else {})}


def active_project(root, folder):
    """Recheck the ledged request before the worker can write anything."""
    record=_record(root,folder.name)
    if record['state']!='working':
        raise ValueError('任务已结束。')
    return _owned_project(record)


def active_checkpoint(root,folder,expected):
    '''Validate the resume receipt in the worker, independently of the JSON request.'''
    record=_record(root,folder.name)
    if record['state']!='working':
        raise ValueError('续做任务已结束。')
    checkpoint=_valid_checkpoint(root,record)
    if not checkpoint or checkpoint!=expected:
        raise ValueError('续做时已验证的分轨发生变化。')
    return checkpoint


def _complete_wave(path, seconds):
    with wave.open(str(path),'rb') as pcm:
        frames=pcm.getnframes()
        if pcm.getframerate()!=44100 or frames<=0 or frames!=round(44100*seconds):
            return False
        pcm.setpos(frames-1)
        return len(pcm.readframes(1))==pcm.getnchannels()*pcm.getsampwidth()


def _file_signature(path):
    info=_checked_stat(path)
    if not stat.S_ISREG(info.st_mode):
        raise ValueError('无法验证任务文件。')
    return [*_identity(info),info.st_size,info.st_mtime_ns]


def _checkpoint_complete(record,value):
    return set(value['stems'])==set(record['instruments'])


def _same_work(a,b):
    # Device may be selected again, but a resumed model result is checked
    # against the exact separator key before the pipeline uses it.
    keys=('kind','instruments','options','keep_stems','piano_model',
          'quantization','rhythm_provider','score_engine','score_chords',
          'score_pedal','score_voice_mode')
    return all(a.get(key)==b.get(key) for key in keys)


def _validate_checkpoint(project,record,value,*,require_complete=False):
    duration=value['duration']
    if type(duration) not in (float,int) or not math.isfinite(duration) or duration<=0:
        raise ValueError('分轨时长无效。')
    source=Path(value['source'])
    if not source.is_absolute() or source.resolve(strict=True)!=Path(record['original']) or not source.is_file():
        raise ValueError('原曲已不可用。')
    if 'source_signature' in value and value['source_signature']!=_file_signature(source):
        raise ValueError('原曲已变化，无法沿用该任务的分轨。')
    completed=set(value['stems'])
    if (value['project']!=str(project) or not completed or not completed<=set(record['instruments'])
            or (require_complete and not _checkpoint_complete(record,value))
            or ('complete' in value and value['complete'] is not _checkpoint_complete(record,value))):
        raise ValueError('已完成分轨与本次选择不符。')
    signatures={}
    for name,path in value['stems'].items():
        path=Path(path)
        if (name not in _INSTRUMENTS or not path.is_absolute() or path.suffix.lower()!='.wav'
                or not path.is_relative_to(project/'stems') or path.resolve(strict=True)!=path
                or not _complete_wave(path,duration)):
            raise ValueError(f'{_INSTRUMENTS.get(name,name)}未完整写入。')
        signatures[name]=_file_signature(path)
    if 'signatures' in value and value['signatures']!=signatures:
        raise ValueError('已完成的分轨文件已变化。')
    return signatures


def record_checkpoint(folder,value,*,partial=False):
    root=folder.parent.parent.parent
    record=_record(root,folder.name)
    project=active_project(root,folder)
    previous=record.get('checkpoint')
    if partial or 'separator_key' in value or 'source_fingerprint' in value:
        if (not re.fullmatch(r'[0-9a-f]{12}',value.get('separator_key',''))
                or not re.fullmatch(r'[0-9a-f]{64}',value.get('source_fingerprint',''))):
            raise ValueError('分轨缺少模型或原曲指纹。')
    value={**value,'source_signature':_file_signature(Path(value['source']))}
    if previous:
        _validate_checkpoint(project,record,previous)
        keys=('source','duration','separator_key','source_fingerprint')
        if any(previous.get(key)!=value.get(key) for key in keys):
            raise ValueError('后续分轨与原任务的模型或原曲不符。')
        if any(value['stems'].get(key,path)!=path for key,path in previous['stems'].items()):
            raise ValueError('不能替换已验证的分轨。')
        value['stems']={**previous['stems'],**value['stems']}
        if 'separator_provenance' in previous or 'separator_provenance' in value:
            value['separator_provenance']={**previous.get('separator_provenance',{}),
                                           **value.get('separator_provenance',{})}
    value['complete']=_checkpoint_complete(record,value)
    try:
        signatures=_validate_checkpoint(project,record,value,require_complete=not partial)
    except (EOFError,wave.Error) as error:
        raise ValueError('分轨未完整写入。') from error
    record['checkpoint']={**value,'signatures':signatures}
    _write(folder/_LEDGER,record)



def finish_task(root,folder,state,*,released=True):
    if not (folder/_LEDGER).is_file():
        return
    if state not in ('completed','cancelled','failed'):
        raise ValueError('未知的任务状态。')
    record=_record(root,folder.name)
    if record['state']!='working':
        return
    record['state']=state
    record['released']=bool(released)
    if state=='cancelled' and not released:
        record['cleanup_error']='未确认所有任务进程已停止，已保留文件。'
    elif state=='cancelled':
        try:
            project=_owned_project(record)
            # The controller calls only after the worker and its Windows job have
            # stopped. Freeze an ownership inventory at that boundary.
            before=set(record.pop('before',[]))
            record['products']={path:value for path,value in _entries(project).items() if path not in before}
        except (OSError,ValueError) as error:
            record['cleanup_error']=str(error)
    else:
        record.pop('before',None)
    _write(folder/_LEDGER,record)


def _valid_checkpoint(root,record):
    if not record.get('checkpoint'):
        return None
    try:
        _validate_checkpoint(_owned_project(record),record,record['checkpoint'])
    except (EOFError,OSError,ValueError,KeyError,TypeError,wave.Error):
        return None
    return record['checkpoint']


def choice(root,token):
    record=_record(root,token)
    if record['state'] not in ('cancelled','kept','cleaned'):
        return None
    result={'id':token,'phase':'choice' if record['state']=='cancelled' else record['state'],
            'count':len((_valid_checkpoint(root,record) or {}).get('stems',{}))}
    if record.get('cleanup_error'):
        result['error']=record['cleanup_error']
    return result


def pending(root):
    return max((r['id'] for r in _records(root) if r['state']=='cancelled'),
               key=lambda t:(_job_dir(root,t)/_LEDGER).stat().st_mtime_ns, default=None)


def history(root):
    result=[]
    for record in _records(root):
        checkpoint=_valid_checkpoint(root,record) if record['state']=='kept' else None
        if checkpoint:
            result.append({'id':record['id'],'label':Path(record['original']).name,
                           'files':[{'kind':'audio','label':_INSTRUMENTS[key]} for key in checkpoint['stems']]})
    return result


def keep(root,token):
    record=_record(root,token)
    if record['state']!='cancelled':
        raise ValueError('任务已处理。')
    if _valid_checkpoint(root,record) is None:
        raise ValueError('本次没有已完成的产物可保留。')
    record['state']='kept'
    record.pop('cleanup_error',None)
    _write(_job_dir(root,token)/_LEDGER,record)


def open_kept(root,token):
    record=_record(root,token)
    checkpoint=_valid_checkpoint(root,record) if record['state']=='kept' else None
    if checkpoint is None:
        raise ValueError('保留的分轨已不可用。')
    return record['request'], {**checkpoint,'complete':_checkpoint_complete(record,checkpoint),
                               'origin':record['id'],'owner_id':record['owner_id']}


def _reference_strings(value):
    if isinstance(value,str):
        yield value
    elif isinstance(value,list):
        for item in value:yield from _reference_strings(item)
    elif isinstance(value,dict):
        for item in value.values():yield from _reference_strings(item)


def _references(path,owned,project):
    for item in _reference_strings(_read(path)):
        try:
            target=Path(item)
            anchors=(target,) if target.is_absolute() else (path.parent/target,project/target)
            if any(anchor.resolve() in owned for anchor in anchors):
                return True
        except (OSError,ValueError):
            # Most strings in project JSON are labels, not paths.
            continue
    return False


def _check_references(root,record,project,owned):
    for other in _records(root,strict=True):
        if other['id']==record['id']:
            continue
        if other['project']==str(project) and other['state']=='working':
            raise ValueError('另一个任务正在使用这些文件，已保留。')
        for item in _reference_strings((other.get('checkpoint'),)):
            path=Path(item)
            if path.is_absolute() and path.resolve() in owned:
                raise ValueError('另一个任务引用了这些产物，已保留。')
    # The project index spans output directories. Also inspect sibling projects
    # that might not be indexed (for example, an offline project copy).
    projects={folder/'project.json' for folder in project.parent.iterdir()
              if folder!=project and folder.is_dir() and (folder/'project.json').is_file()}
    for index in (_jobs(root).parent/'projects').glob('*.json'):
        data=_read(index)
        target=Path(data['project']).resolve()
        if (target/'project.json').is_file():
            projects.add(target/'project.json')
        if target==project and project in owned:
            raise ValueError('当前项目已登记完成，不能按取消任务清理。')
    for metadata in projects:
        if _references(metadata,owned,metadata.parent):
            raise ValueError('另一个工程引用了这些产物，已保留。')
        for report in (metadata.parent/'exports').glob('*/report.json'):
            if _references(report,owned,metadata.parent):
                raise ValueError('另一个工程的导出引用了这些产物，已保留。')


def clean(root,token,*,live_paths=()):
    record=_record(root,token)
    if record['state']!='cancelled':
        raise ValueError('任务已处理。')
    folder=_job_dir(root,token)
    if record.get('no_products'):
        record['state']='cleaned'
        _write(folder/_LEDGER,record)
        return choice(root,token)
    try:
        project=_owned_project(record)
        owns_project=record['phase']=='separation' and record['owner_id']==record['id']
        products=record.get('products')
        if not isinstance(products,dict):
            raise ValueError(record.get('cleanup_error','未能确认本次产物。'))
        if (project/'project.json').is_file():
            published=_read(project/'project.json')
            if published.get('latest_export'):
                raise ValueError('项目已提交，不能按取消任务清理。')
        present=_entries(project)
        current={name:entry for name,entry in present.items() if name in products}
        for name,entry in current.items():
            if entry!=products[name]:
                raise ValueError(f'产物 {Path(name).name} 已变化，已保留。')
        if owns_project and set(present)-set(products):
            raise ValueError('任务目录包含新增文件，已保留。')
        owned={(project/name) for name in current}
        if owns_project:owned.add(project)
        if {Path(name).resolve() for name in live_paths if name}&owned:
            raise ValueError('试听仍在使用这些文件。关闭该试听后可以再清理。')
        _check_references(root,record,project,owned)
        links={}
        for entry in current.values():
            if entry['kind']=='file':
                identity=tuple(entry['identity'])
                links[identity]=links.get(identity,0)+1
        for entry in current.values():
            if entry['kind']=='file' and links[tuple(entry['identity'])]!=entry['links']:
                raise ValueError('存在另一个位置共用的硬链接，已保留。')
        # No rmtree. Only paths frozen in this task's stopped inventory are
        # eligible. An unexpected entry makes the parent rmdir fail safely.
        for name,entry in sorted(current.items(),key=lambda item:(item[0]==_OWNER,-item[0].count('/'))):
            path=project/name
            info=_checked_stat(path)
            if path.resolve(strict=True)!=path or _identity(info)!=entry['identity']:
                raise ValueError('清理期间检测到路径发生变化，已保留其余文件。')
            if entry['kind']=='directory':
                path.rmdir()
            else:
                # Deleting another owned alias can lower the link count. A
                # new alias outside this inventory raises it and blocks deletion.
                if info.st_size!=entry['size'] or info.st_mtime_ns!=entry['modified'] or info.st_nlink>entry['links']:
                    raise ValueError('清理期间检测到文件修改或共享，已保留其余文件。')
                path.unlink()
        if owns_project:
            if _identity(_checked_stat(project))!=record['project_identity']:
                raise ValueError('任务目录已变化。')
            project.rmdir()
        record['state']='cleaned'
        record.pop('cleanup_error',None)
    except (OSError,RuntimeError,ValueError,KeyError,TypeError) as error:
        record['cleanup_error']=str(error)
    _write(folder/_LEDGER,record)
    return choice(root,token)



def cancel_preflight(root,request):
    token=uuid4().hex
    folder=_job_dir(root,token)
    folder.mkdir(parents=True,exist_ok=False)
    _write(folder/_LEDGER,{'version':1,'id':token,'owner_id':token,'state':'cancelled',
                          'phase':'separation','project':None,'original':request['audio'],
                          'checkpoint':None,'no_products':True})
    return token

def _current_process():
    from .process_locks import _process_identity
    return {'pid':os.getpid(),'created':_process_identity()}


def _process_gone(identity):
    """Only prove a specific lifetime ended. Permission errors stay uncertain."""
    pid,created=identity['pid'],identity['created']
    if type(pid) is not int or pid<=0 or not isinstance(created,str):
        return False
    if os.name!='nt':
        boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        try:
            row=Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()
        except FileNotFoundError:
            return True
        return row[0]=='Z' or f'{boot}:{row[19]}'!=created

    import ctypes
    from ctypes import wintypes
    api=ctypes.WinDLL('kernel32',use_last_error=True)
    api.OpenProcess.argtypes=[wintypes.DWORD,wintypes.BOOL,wintypes.DWORD]
    api.OpenProcess.restype=wintypes.HANDLE
    api.GetProcessTimes.argtypes=[wintypes.HANDLE]+[ctypes.POINTER(wintypes.FILETIME)]*4
    api.GetProcessTimes.restype=wintypes.BOOL
    api.WaitForSingleObject.argtypes=[wintypes.HANDLE,wintypes.DWORD]
    api.WaitForSingleObject.restype=wintypes.DWORD
    api.CloseHandle.argtypes=[wintypes.HANDLE]
    api.CloseHandle.restype=wintypes.BOOL
    handle=api.OpenProcess(0x101000,False,pid)  # SYNCHRONIZE | QUERY_LIMITED_INFORMATION
    if not handle:
        return ctypes.get_last_error()==87  # ERROR_INVALID_PARAMETER / no such PID
    try:
        times=[wintypes.FILETIME() for _ in range(4)]
        if not api.GetProcessTimes(handle,*(ctypes.byref(value) for value in times)):
            return False
        start=times[0]
        actual=str((start.dwHighDateTime<<32)|start.dwLowDateTime)
        return actual!=created or api.WaitForSingleObject(handle,0)==0
    finally:
        api.CloseHandle(handle)


def _job_absent(token):
    if os.name!='nt':
        return True
    import ctypes
    from .desktop_worker import _kernel32
    api=_kernel32()
    handle=api.OpenJobObjectW(0x0004,False,f'Local\\AstraDesktop-{token}')  # JOB_OBJECT_QUERY
    if not handle:
        return ctypes.get_last_error()==2  # ERROR_FILE_NOT_FOUND, not access denied
    api.CloseHandle(handle)
    return False


def _failed_worker_receipt(root,record,marker):
    # The pre-checkpoint controller wrote failed only after QProcess exit.
    # Verify the matching worker's terminal receipt before relying on that state.
    folder=_job_dir(root,record['id'])
    intent=_read(folder/'lock.json')
    log=folder/'events.jsonl'
    if intent.get('path')!=str(marker):
        return False
    _file_signature(log)
    worker=None
    failure=False
    with log.open(encoding='utf-8') as events:
        for line in events:
            event=json.loads(line)
            kind=event.get('type')
            if kind=='ready':
                if worker is not None or failure:
                    return False
                worker=event.get('pid')
            elif kind in ('completed','separated'):
                return False
            elif kind=='failed':
                if failure or not isinstance(event.get('message'),str):
                    return False
                failure=True
    return failure and type(worker) is int and worker>0 and worker==intent.get('pid')


@contextmanager
def _recovery_guard(root,record,project):
    """Hold the existing kernel guard without replacing any stale marker."""
    if record['state'] not in ('working','failed') or not _job_absent(record['id']):
        raise ValueError('任务仍在运行，暂不能恢复。')
    controller=record.get('controller')
    explicitly_released=record['state']=='failed' and record.get('released') is True
    if record['state']=='working' and controller and not _process_gone(controller):
        raise ValueError('任务所属窗口仍在运行。')
    marker=project/'.running'
    guard=project/'.running.guard'
    if not guard.exists() and explicitly_released and not marker.exists():
        # A confirmed worker may fail before it ever claims the project.
        yield
        return

    from .process_locks import _lock, _read_marker, _valid
    info=_checked_stat(guard)
    if not stat.S_ISREG(info.st_mode) or not info.st_ino:
        raise ValueError('无法验证项目锁。')
    descriptor=os.open(guard,os.O_RDONLY)
    try:
        _lock(descriptor)
        actual=os.fstat(descriptor)
        if _identity(_checked_stat(guard))!=_identity(actual) or _identity(info)!=_identity(actual):
            raise ValueError('项目锁已变化。')
        if marker.exists():
            _checked_stat(marker)
            _,owner=_read_marker(marker)
            intent=_read(_job_dir(root,record['id'])/'lock.json')
            if (not _valid(owner,guard,actual) or intent.get('pid')!=owner['pid']
                    or intent.get('path')!=str(marker) or not _process_gone(owner)):
                raise ValueError('原任务工作进程身份未能验证。')
        else:
            controller_gone=record['state']=='working' and controller is not None
            legacy_failed=(record['state']=='failed' and 'released' not in record
                           and _failed_worker_receipt(root,record,marker))
            if not (explicitly_released or controller_gone or legacy_failed):
                raise ValueError('缺少原任务的进程退出记录，已保留现有文件。')
        yield
    finally:
        os.close(descriptor)


def _recovery_card(record,stems):
    return {'id':record['id'],'label':Path(record['original']).name,
            'phase':'score' if 'piano' in stems and set(stems)==set(record['instruments']) else 'separation',
            'files':[{'kind':'audio','label':_INSTRUMENTS[key]} for key in stems]}


def recovery_history(root):
    result=[]
    for record in _records(root):
        if record['state'] not in ('working','failed') or not record.get('checkpoint'):
            continue
        try:
            project=_owned_project(record)
            with _recovery_guard(root,record,project):
                checkpoint=_valid_checkpoint(root,record)
                if checkpoint:
                    result.append(_recovery_card(record,checkpoint['stems']))
        except (OSError,ValueError,RuntimeError,TypeError,KeyError):
            continue
    return result


def recover(root,token):
    record=_record(root,token)
    project=_owned_project(record)
    with _recovery_guard(root,record,project):
        current=_record(root,token)
        if record!=current or not _valid_checkpoint(root,current):
            raise ValueError('原任务记录已变化或没有完整分轨。')
        current['state']='kept'
        current.pop('products',None)
        current.pop('before',None)
        current.pop('cleanup_error',None)
        _write(_job_dir(root,token)/_LEDGER,current)
    return token


def _pcm_signature(path):
    """Digest normalized PCM bytes; WAV container metadata may differ."""
    _file_signature(path)
    with wave.open(str(path),'rb') as wav:
        params=(wav.getnchannels(),wav.getsampwidth(),wav.getframerate(),wav.getnframes())
        if params[:3]!=(2,2,44100) or not params[3]:
            raise ValueError('此分轨的 WAV 格式或长度不符。')
        remaining=params[3]*4
        digest=hashlib.sha256()
        while remaining:
            chunk=wav.readframes(min(remaining//4,65536))
            if not chunk or len(chunk)>remaining:
                raise ValueError('WAV 没有完整写入。')
            remaining-=len(chunk)
            digest.update(chunk)
    return params,digest.hexdigest()


def _legacy_assets(root,record,project):
    """Recognize only old adapter outputs with their written completion sidecar."""
    raw_separation=(record['phase']=='separation' and record['owner_id']==record['id']
                    and not record.get('checkpoint'))
    score_only=(record['phase']=='score' and record['state']=='failed'
                and record['instruments']==['piano'])
    if project.parent!=Path(root).resolve()/'outputs' or not (raw_separation or score_only):
        raise ValueError('不是旧候选下可验证的分轨任务。')
    request=record['request']
    persisted=_read(_job_dir(root,record['id'])/'request.json')
    if (not _same_work(request,persisted) or persisted.get('workflow_phase')!=record['phase']
            or persisted.get('workflow_project')!=str(project)
            or persisted.get('audio')!=record['original'] or request.get('audio')!=record['original']
            or persisted.get('execution_device')!=request.get('execution_device')
            or request.get('piano_model')!='xlance' or 'piano' not in record['instruments']):
        raise ValueError('不能证明这个旧任务的输入或模型设置。')
    source=Path(record['original'])
    if source.resolve(strict=True)!=source or source.suffix.casefold()=='.ncm':
        raise ValueError('不能直接验证此旧任务的原曲。')
    normalized=project/'cache'/'source.wav'
    staged=project/'cache'/'separation'/'piano.wav'
    sidecar=staged.with_name('xlance_separation.json')
    if any(path.resolve(strict=True)!=path for path in (normalized,staged,sidecar)):
        raise ValueError('缓存路径已变化。')
    normalized_sig=_file_signature(normalized)
    piano_sig=_file_signature(staged)
    from .adapters import XLANCE_PIANO_MODEL
    provenance=_read(sidecar)
    if (provenance.get('model')!=XLANCE_PIANO_MODEL or provenance.get('sample_rate')!=44100
            or provenance.get('device') not in ('cpu','cuda')
            or provenance.get('precision') not in ('FP32','AMP','FP16')
            or (provenance.get('precision')=='FP16' and provenance.get('device')!='cuda')
            or not (normalized_sig[3]<=piano_sig[3]<=_file_signature(sidecar)[3])):
        raise ValueError('旧分轨没有匹配的模型完成记录。')
    with wave.open(str(normalized),'rb') as wav:
        duration=wav.getnframes()/44100
    if not _complete_wave(normalized,duration) or not _complete_wave(staged,duration):
        raise ValueError('旧分轨没有完整写入。')
    if not score_only:
        return request,normalized,staged,provenance,duration

    prior=_record(root,record['owner_id'])
    saved=_valid_checkpoint(root,record)
    manifest=_read(project/'cache'/'separation.json')
    separator_key=manifest.get('key')
    if (prior['state']!='completed' or prior['phase']!='separation' or _owned_project(prior)!=project
            or prior['original']!=record['original'] or not _same_work(prior['request'],request)
            or request.get('workflow_origin')!=prior['id'] or persisted.get('workflow_origin')!=prior['id']
            or not saved or saved!=_valid_checkpoint(root,prior) or not _checkpoint_complete(record,saved)
            or saved['duration']!=duration or not isinstance(separator_key,str)
            or not re.fullmatch(r'[0-9a-f]{12}',separator_key) or manifest.get('instruments')!=['piano']
            or (project/'project.json').is_file()):
        raise ValueError('不能验证旧制谱任务继承的完整钢琴分轨。')
    frozen=project/'stems'/separator_key/'piano.wav'
    if str(frozen)!=saved['stems']['piano']:
        raise ValueError('已完成分轨与旧模型缓存不符。')
    return request,normalized,frozen,provenance,duration


def legacy_recovery_request(source_root,token):
    record=_record(source_root,token)
    project=_owned_project(record)
    with _recovery_guard(source_root,record,project):
        request,_,_,provenance,_=_legacy_assets(source_root,record,project)
    return {**request,'workflow_recovered_device':provenance['device']}


def legacy_recovery_history(source_root):
    result=[]
    for record in _records(source_root):
        try:
            legacy_recovery_request(source_root,record['id'])
            result.append(_recovery_card(record,['piano']))
        except (EOFError,OSError,ValueError,RuntimeError,TypeError,KeyError,wave.Error):
            continue
    return result


def import_legacy_recovery(root,source_root,token,*,separator_key,separator_options):
    """Import an explicitly selected old WAV, checking source and model first."""
    root=Path(root).resolve(strict=True)
    source_root=Path(source_root).resolve(strict=True)
    if root==source_root or not re.fullmatch(r'[0-9a-f]{12}',separator_key):
        raise ValueError('需要在独立的候选配置中恢复旧分轨。')
    old=_record(source_root,token)
    old_project=_owned_project(old)
    folder=_job_dir(root,token)
    recovered_from={'profile':str(source_root),'id':token}
    if folder.exists():
        existing=_record(root,token)
        if existing.get('recovered_from')==recovered_from and existing['state']=='kept':
            open_kept(root,token)
            return token
        raise ValueError('已有同编号任务，已保留现有文件。')
    with _recovery_guard(source_root,old,old_project):
        request,normalized,stem,provenance,duration=_legacy_assets(source_root,old,old_project)
        if old['phase']=='score':
            if stem.parent.name!=separator_key:
                raise ValueError('当前分轨模型配置与旧任务的已完成分轨不符。')
            if _pcm_signature(stem)!=_pcm_signature(old_project/'cache'/'separation'/'piano.wav'):
                raise ValueError('已提交的钢琴 WAV 与旧模型缓存已不一致。')
        device=separator_options.get('device')
        use_amp=device=='cuda' and bool(separator_options.get('autocast',False))
        use_fp16=device=='cuda' and bool(separator_options.get('native_fp16',False)) and not use_amp
        precision='FP16' if use_fp16 else ('AMP' if use_amp else 'FP32')
        if (separator_options.get('piano_model')!='xlance' or device!=provenance['device']
                or separator_options.get('piano_segment_size',1101)!=provenance.get('segment_size')
                or separator_options.get('overlap',2)!=provenance.get('overlap')
                or precision!=provenance['precision'] or provenance.get('precision_fallback') is not None):
            raise ValueError('当前分轨模型质量设置与旧分轨不符。')
        from .pipeline import decode_audio, fingerprint
        source=Path(request['audio'])
        source_identity=_file_signature(source)
        with tempfile.TemporaryDirectory(prefix='astra-recovered-audio-',dir=root) as temporary:
            decoded=Path(temporary)/'source.wav'
            decode_audio(source,decoded,lambda *_:None)
            if _pcm_signature(decoded)!=_pcm_signature(normalized):
                raise ValueError('原曲内容与旧任务的缓存不符。')
            digest=fingerprint(source)
            if _file_signature(source)!=source_identity:
                raise ValueError('恢复期间原曲发生变化。')
            stem_sig=_pcm_signature(stem)
            folder.mkdir(parents=True,exist_ok=False)
            resumed={**request,'workflow_phase':'separation','output':str(root/'outputs')}
            resumed.pop('workflow_origin',None)
            started=begin_task(root,folder,resumed)
            project=Path(started['workflow_project'])
            try:
                target=project/'stems'/separator_key/'piano.wav'
                target.parent.mkdir(parents=True)
                shutil.copyfile(stem,target)
                if _pcm_signature(target)!=stem_sig or _file_signature(source)!=source_identity:
                    raise ValueError('恢复期间验证的文件发生变化。')
                cache=project/'cache'
                cache.mkdir()
                shutil.copyfile(decoded,cache/'source.wav')
                record_checkpoint(folder,{
                    'project':str(project),'source':str(source),'duration':duration,
                    'stems':{'piano':str(target)},'source_fingerprint':digest,
                    'separator_key':separator_key,'separator_provenance':{'piano':provenance},
                },partial=True)
                record=_record(root,token)
                record.update(state='kept',released=True,recovered_from=recovered_from)
                record.pop('before',None)
                _write(folder/_LEDGER,record)
            except (EOFError,OSError,ValueError,RuntimeError,TypeError,KeyError,wave.Error):
                finish_task(root,folder,'failed')
                raise
    return token
