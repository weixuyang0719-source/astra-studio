"""Manual review annotations. Not tempo events and not the editor undo stack."""
import json
import math
import os
from pathlib import Path
from uuid import uuid4


def request(project, payload):
    if not isinstance(project, dict) or not project.get('path'):
        raise ValueError('请先打开项目。')
    root = Path(project['path']).resolve()
    if payload.get('project') != str(project['path']):
        raise ValueError('项目已经切换，请重试。')
    if not root.is_dir():
        raise ValueError('项目目录不可用。')
    path = root / 'beat-check-marks.json'
    doc = json.loads(path.read_text('utf-8')) if path.exists() else dict(version=1, revision=0, marks=[], undo=None)
    if doc.get('version') != 1 or not isinstance(doc.get('marks'), list):
        raise ValueError('拍点检查标记文件无效。')
    op = payload.get('op', 'read')
    if op != 'read':
        if payload.get('revision') != doc['revision']:
            raise ValueError('标记已更新，请重试。')
        before = doc['marks']
        if op == 'add':
            at = payload.get('seconds')
            if isinstance(at, bool) or not isinstance(at, (float, int)) or not math.isfinite(at) or at < 0:
                raise ValueError('标记位置无效。')
            after = sorted(before + [dict(id=uuid4().hex, seconds=at)], key=lambda m: (m['seconds'], m['id']))
        elif op == 'remove':
            after = [m for m in before if m['id'] != payload.get('id')]
        elif op == 'clear':
            after = []
        elif op == 'undo':
            after = doc.get('undo')
            if after is None: return dict(ok=True, project=str(project['path']), **doc)
        else:
            raise ValueError('未知标记操作。')
        if after != before:
            doc = dict(version=1, revision=doc['revision']+1, marks=after, undo=None if op=='undo' else before)
            tmp = path.with_name(path.name+'.'+uuid4().hex+'.tmp')
            try:
                tmp.write_text(json.dumps(doc, ensure_ascii=False), encoding='utf-8')
                os.replace(tmp, path)
            finally:
                if tmp.exists(): tmp.unlink()
    return dict(ok=True, project=str(project['path']), **doc)
