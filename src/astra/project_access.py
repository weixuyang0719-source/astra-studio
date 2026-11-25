"""Exact existing-project permissions, granted by the local chooser or a native drop."""
from dataclasses import dataclass, field
from itertools import islice
import json
import os
from pathlib import Path
import re
import stat

_REPARSE=getattr(stat,'FILE_ATTRIBUTE_REPARSE_POINT',0)
_KEY=re.compile(r'[A-Za-z0-9_-][A-Za-z0-9_.-]{0,127}')
_JOB_KEY='_external_project_permission'
_JOB_ENV='ASTRA_EXTERNAL_PROJECT_JOB'
_MAX_ENTRIES=24000
_MAX_META=8*1024*1024
_grants={}

def isolated():
    return os.environ.get('ASTRA_RELEASE_ISOLATED_PROJECTS')=='1'

def _identity(info):
    return info.st_dev,info.st_ino

def _plain(path, *, directory, allow_stem_link=False):
    info=path.lstat()
    if (not (stat.S_ISDIR if directory else stat.S_ISREG)(info.st_mode)
            or getattr(info,'st_file_attributes',0)&_REPARSE
            or (not directory and info.st_nlink!=1 and not (allow_stem_link and info.st_nlink==2))):
        raise ValueError('这个工程含有链接、目录联接或共享文件，未打开或写入。')
    return info

def _under(root,path):
    if not path.is_relative_to(root) or path==root:
        raise ValueError('引用不在当前工程内。')
    rel=path.relative_to(root)
    if any(p in ('.','..') or ':' in p for p in rel.parts):
        raise ValueError('工程引用路径无效。')
    return rel

def _validate_path(root,path,*,directory=False,missing=False,allow_stem_link=False):
    rel=_under(root,path)
    node=root
    for i,piece in enumerate(rel.parts):
        node=node/piece
        try:_plain(node,directory=directory or i<len(rel.parts)-1,
                   allow_stem_link=allow_stem_link and i==len(rel.parts)-1)
        except FileNotFoundError:
            if missing:return path
            raise
    if path.resolve(strict=True)!=path:
        raise ValueError('工程的文件位置已经变化。')
    return path

def _read(path):
    with path.open('rb') as f:
        raw=f.read(_MAX_META+1)
    if len(raw)>_MAX_META:
        raise ValueError('工程索引过大，未打开此工程。')
    value=json.loads(raw)
    if not isinstance(value,dict):
        raise ValueError('工程索引格式无效。')
    return value

def _stem_alias(root,path):
    rel=path.relative_to(root)
    name=rel.name
    found=re.fullmatch(r'(piano|guitar)(?:-[a-f0-9]{16})?\.wav',name)
    if found is None:return None
    instrument=found[1]
    if len(rel.parts)==1 and name==f'{instrument}.wav':return instrument,'alias'
    if len(rel.parts)==3 and rel.parts[0]=='stems' and _KEY.fullmatch(rel.parts[1]):
        return instrument,'stem'
    return None

@dataclass(frozen=True)
class Permit:
    root: Path
    identity: tuple
    stem_links: tuple=field(default=(),compare=False)

    def validate(self):
        if self.root.resolve(strict=True)!=self.root or _identity(_plain(self.root,directory=True))!=self.identity:
            raise ValueError('所选工程已移动或被替换，请重新打开。')
        return self.root

    def file(self,path,*,directory=False,missing=False):
        root=self.validate()
        if not directory:
            for pair,ident in self.stem_links:
                if path in pair:
                    # Only a proven legacy read-only WAV alias, never a save target.
                    for peer in pair:
                        _validate_path(root,peer,allow_stem_link=True)
                        info=_plain(peer,directory=False,allow_stem_link=True)
                        if _identity(info)!=ident or info.st_nlink!=2:
                            raise ValueError('工程的分轨兼容链接已变化，请重新打开。')
                    return path
        return _validate_path(root,path,directory=directory,missing=missing)

def _inspect_index(value):
    # Read only the two bounded index files. No child traversal or grant.
    # No authority can come from a path named by project.json.
    from .release_paths import data_root
    root=Path(value).expanduser()
    if not root.is_absolute() or root.anchor.startswith(('\\\\','//')) or root.resolve(strict=True)!=root:
        raise ValueError('请选择工程的实际目录，不要选择路径链接。')
    data=data_root()
    if data.is_relative_to(root) or any(root.is_relative_to(p) for p in
        (data/'.cache',data/'temp',data.parent/'runtime',data.parent/'musescore-proof')):
        raise ValueError('请选择具体工程文件夹，而不是 Astra 安装或缓存目录。')
    ident=_identity(_plain(root,directory=True))
    if not ident[1]:
        raise ValueError('无法确认该工程目录身份，未开启写入。')
    p=Permit(root,ident)
    from .score_commit import read_project
    metadata=p.file(root/'project.json')
    p.file(root/'editing',directory=True,missing=True)
    p.file(root/'editing'/'current.json',missing=True)
    state=read_project(metadata, raw=_read(metadata))
    instruments=state.get('instruments')
    if (not isinstance(instruments,list) or not instruments or
            any(not isinstance(v,str) or v not in ('piano','guitar') for v in instruments)):
        raise ValueError('所选目录没有可用的 Astra 工程索引。')
    keys=state.get('analysis_keys',{})
    if not isinstance(keys,dict):
        raise ValueError('工程缓存索引格式不支持。')
    path_keys={key:keys[key] for key in ('separator','beat_this','piano','guitar') if key in keys}
    if 'separator_key' in state:
        path_keys['separator_key']=state['separator_key']
    for key,v in path_keys.items():
        if key=='beat_this' and v is None:continue
        if not isinstance(v,str) or not _KEY.fullmatch(v) or v in ('.','..'):
            raise ValueError('工程缓存标识不安全，未打开。')
    name=state.get('latest_export')
    leaf=Path(name).name if isinstance(name,str) else ''
    if not leaf or leaf in ('.','..') or ':' in leaf:
        raise ValueError('此工程没有已完成的导出。')
    export=p.file(root/'exports'/leaf,directory=True)
    report=_read(p.file(export/'report.json'))
    if report.get('status') not in ('complete','complete_with_warnings'):
        raise ValueError('此工程没有已完成的导出。')
    p.validate()
    return p,state,export,report

def summarize(value):
    """Read a discoverable project without opening an owner or granting writes."""
    _,state,export,report=_inspect_index(value)
    return state,export,report

def prepare(value):
    # Call only after explicit native project selection/approval.
    p,_,_,_=_inspect_index(value)
    root=p.root
    # Bound traversal by entry count/depth. It does not read the bytes of WAVs,
    # caches, revisions or assets; inconsistent switch operations cannot adopt it.
    stack=[(root,0)]
    visited=0
    links={}
    while stack:
        folder,depth=stack.pop()
        if depth>=24:raise ValueError('外部工程目录过深，未开启写入。')
        with os.scandir(folder) as it:
            for entry in islice(it,_MAX_ENTRIES-visited+1):
                visited+=1
                if visited>_MAX_ENTRIES:raise ValueError('外部工程文件过多，未开启写入。')
                path=Path(entry.path)
                directory=entry.is_dir(follow_symlinks=False)
                alias=None if directory else _stem_alias(root,path)
                info=_plain(path,directory=directory,allow_stem_link=alias is not None)
                if info.st_nlink==2 and alias is not None:
                    links.setdefault(_identity(info),[]).append((path,alias))
                if directory:stack.append((path,depth+1))
    approved_pairs=[]
    for ident,rows in links.items():
        if (not ident[1] or len(rows)!=2 or rows[0][1][0]!=rows[1][1][0]
                or {row[1][1] for row in rows}!={'alias','stem'}):
            raise ValueError('无法确认工程分轨硬链接只在所选工程内，未打开。')
        approved_pairs.append((tuple(row[0] for row in rows),ident))
    p.validate()
    return Permit(root,p.identity,tuple(approved_pairs))

def register(p):
    if not isinstance(p,Permit):
        raise ValueError('无效的指定工程。')
    key=p.validate()
    prev=_grants.get(key)
    if prev is not None and prev!=p:
        raise ValueError('工程位置已变化，请关闭旧会话后重新选择。')
    _grants[key]=p
    return prev

def forget(p):
    if _grants.get(p.root) is p:
        _grants.pop(p.root,None)

def approved(project):
    # Match the original path, not a newly redirected canonical target.
    if isolated():
        return _grants.get(Path(project))
    return None

def directory_grant(path):
    if not isolated():return None
    for p in tuple(_grants.values()):
        if path.is_relative_to(p.root):
            p.validate()
            return p
    return None

def existing_path(path):
    from .release_paths import project_write_path
    original=Path(path).expanduser()
    p=approved(original)
    if p is not None:return p.validate()
    try:return project_write_path(original)
    except ValueError:
        raise ValueError('请用「打开项目」选择这个现有工程，未授予其他目录写入权限。') from None

def reference(project,value,*,directory=False):
    """Resolve an admitted project's own reference, never against process cwd."""
    p=approved(project)
    if value is None or not isinstance(value,(str,Path)) or not str(value):
        return None
    path=Path(value)
    if p is None:
        return path
    if not path.is_absolute():
        if path.drive or any(v in ('.','..') for v in path.parts):
            return None
        path=p.root/path
    try:return p.file(path,directory=directory)
    except (OSError,ValueError,RuntimeError):return None

def export_folder(project,value):
    """Existing export pointers may retain an old absolute path; only its basename is used."""
    p=approved(project)
    if p is None:
        return Path(value) if value else None
    if not isinstance(value,str) or not value:return None
    name=Path(value).name
    if not name or name in ('.','..') or ':' in name:return None
    return reference(project,Path('exports')/name,directory=True)

def validate_edit(project):
    p=approved(project)
    if isolated():
        existing_path(project)
    if p is not None:
        for rel,is_dir in (('project.json',False),('exports',True),('editing',True),
                          ('editing/current.json',False),('editing/checkpoint.previous.json',False),
                          ('editing/revisions',True),('editing/.astra-recovery',True),
                          ('editing/.astra-recovery-project.json',False),
                          ('editing/.lock',False),('editing/.lock.guard',False)):
            p.file(p.root/rel,directory=is_dir,missing=rel.startswith('editing'))

def prepare_job(request):
    clean={k:v for k,v in request.items() if k!=_JOB_KEY}
    project=clean.get('project')
    if project and isolated():
        root=existing_path(project)
        p=approved(root)
        if p is not None:
            validate_edit(root)
            clean[_JOB_KEY]={'path':str(root),'identity':list(p.identity)}
    return clean

def install_job(request,folder):
    if not isolated():return
    project=request.get('project')
    if not project:return
    tag=request.get(_JOB_KEY)
    if tag is not None:
        from .release_paths import data_root
        folder=Path(folder)
        jobs=data_root()/'.cache'/'jobs'
        if (not isinstance(tag,dict) or set(tag)!= {'path','identity'} or tag.get('path')!=project
                or not re.fullmatch(r'[0-9a-f]{32}',folder.name) or folder.parent!=jobs
                or folder.resolve(strict=True)!=folder or os.environ.get(_JOB_ENV)!=folder.name):
            raise ValueError('外部工程任务与已选工程不一致，未写入。')
        p=prepare(project)
        if list(p.identity)!=tag.get('identity'):
            raise ValueError('外部工程在任务启动前已经变化，未写入。')
        register(p)
    existing_path(project)

def audio_kind(state):
    value=state.get('source')
    return 'midi' if (state.get('source_kind')=='midi' or
        isinstance(value,str) and Path(value).suffix.lower() in ('.mid','.midi')) else 'audio'

def _sha(value):
    return value.lower() if isinstance(value,str) and re.fullmatch(r'[0-9a-fA-F]{64}',value) else None

def audio_identity(state):
    # Metadata determines expected content, never extra path authority.
    source=state.get('source')
    name=source if isinstance(source,str) else ''
    imported=state.get('source_import')
    decoded=_sha(imported.get('audio_sha256')) if isinstance(imported,dict) and imported.get('converted') is True else None
    return name,_sha(state.get('source_sha256')),decoded

def _audio_stamp(path):
    if not path.is_absolute() or path.anchor.startswith(('\\\\','//')) or path.resolve(strict=True)!=path:
        raise ValueError('原曲须为可直接读取的本地文件。')
    info=_plain(path,directory=False)
    return _identity(info),info.st_size,info.st_mtime_ns,info.st_ctime_ns

@dataclass(frozen=True)
class AudioReadLink:
    selected: Path
    media: Path
    selected_stamp: tuple
    media_stamp: tuple

    def verify(self):
        if _audio_stamp(self.selected)!=self.selected_stamp or _audio_stamp(self.media)!=self.media_stamp:
            raise ValueError('所选原曲发生变化，请重新选择。')
        return self.media,self.selected

def prepare_audio_link(value, identity, formats):
    """For a native file chooser result only. Never open a path from project metadata."""
    from .pipeline import fingerprint
    name,source_digest,decoded_digest=identity
    chosen=Path(value)
    suffix=chosen.suffix.lower()
    if suffix not in formats|{'.ncm'}:
        raise ValueError('请选择受支持的音频文件。')
    try:
        stamp=_audio_stamp(chosen)
        expected=source_digest
        if Path(name).suffix.lower()=='.ncm' and suffix!='.ncm':
            expected=decoded_digest
            if source_digest and expected is None:
                raise ValueError('请直接选择原 NCM 文件。')
        actual=fingerprint(chosen) if expected or suffix=='.ncm' else None
        if expected and actual!=expected:
            raise ValueError('所选文件与工程原曲不一致，请重新选择。')
        media=chosen
        if suffix=='.ncm':
            from .audio_import import prepare_audio_import
            from .release_paths import data_root
            data=data_root()
            cache=data/'.cache'/'imports'
            folder=cache/'ncm'/actual
            _validate_path(data,folder,directory=True,missing=True)
            for leaf in ('audio.flac','audio.mp3','import.json'):
                _validate_path(data,folder/leaf,missing=True)
            try:
                imported=prepare_audio_import(chosen,cache,log=lambda _:None)
            except (ArithmeticError,ImportError,OSError,ValueError,KeyError,RuntimeError,TypeError):
                raise ValueError('无法读取所选 NCM。请检查文件，或选择它对应的转换音频。') from None
            media=Path(imported['path'])
            if media not in (folder/'audio.flac',folder/'audio.mp3') or imported.get('source_sha256')!=actual:
                raise ValueError('无法确认 NCM 转换结果，未连接原曲。')
        link=AudioReadLink(chosen,media,stamp,_audio_stamp(media))
        link.verify()
        return link
    except (OSError,RuntimeError,KeyError,TypeError):
        raise ValueError('无法安全读取所选原曲，请重新选择。') from None
