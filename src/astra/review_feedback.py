"""Import user review text as evidence, never as executable note edits."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path


def import_feedback(project, source, *, audit_path=None):
    from .pipeline import save_json
    project = Path(project).resolve(strict=True)
    source = Path(source).resolve(strict=True)
    raw = source.read_bytes()
    if len(raw) > 2 * 1024 * 1024:
        raise ValueError('审核意见文件过大。')
    data = json.loads(raw.decode('utf-8-sig'))
    if not isinstance(data, dict) or data.get('version') != 1 or data.get('source') != 'astra-visual-review':
        raise ValueError('不是受支持的 Astra 谱面审核意见文件。')
    export = data.get('audit_export')
    if not isinstance(export, str) or not export or Path(export).name != export:
        raise ValueError('审核版本标识无效。')
    export_path = (project / 'exports' / export).resolve()
    if not export_path.is_relative_to(project / 'exports') or not (export_path / 'report.json').is_file():
        raise ValueError('此审核意见对应的导出版本不在当前项目中。')
    groups = data.get('groups')
    if not isinstance(groups, list) or not groups:
        raise ValueError('审核意见没有分组。')
    seen, seen_items = set(), set()
    for group in groups:
        if not isinstance(group, dict):
            raise ValueError('审核分组格式无效。')
        gid, ids = group.get('group_id'), group.get('item_ids')
        if type(gid) is not int or gid < 1 or gid in seen:
            raise ValueError('审核分组编号重复或无效。')
        if (not isinstance(ids, list) or not ids or any(type(i) is not int or i < 1 for i in ids)
                or len(set(ids)) != len(ids) or seen_items.intersection(ids)):
            raise ValueError('审核条目编号重复或无效。')
        if (group.get('decision', '') not in ('', 'keep', 'shorter', 'longer', 'unsure', 'other')
                or not isinstance(group.get('comment', ''), str)):
            raise ValueError('审核选项或文字格式无效。')
        seen.add(gid)
        seen_items.update(ids)
    if audit_path is not None:
        audit = json.loads(Path(audit_path).read_text(encoding='utf-8-sig'))
        if Path(audit['export']).resolve() != export_path:
            raise ValueError('审核清单与项目导出版本不匹配。')
        expected = {g['id']: g['item_ids'] for g in audit['groups']}
        if any(expected.get(g['group_id']) != g['item_ids'] for g in groups):
            raise ValueError('审核意见与原始清单条目不匹配。')
    digest = hashlib.sha256(raw).hexdigest()
    folder = project / 'reviews' / export
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / (digest + '.json')
    if not target.is_file():
        save_json(target, {'version': 1, 'imported_at': datetime.now(timezone.utc).isoformat(),
                          'source_sha256': digest, 'audit_items_verified': audit_path is not None,
                          'feedback': data})
    return {'path': str(target), 'audit_export': export, 'group_count': len(groups),
            'comment_count': sum(bool(g.get('comment', '').strip()) for g in groups),
            'groups': groups, 'note': '意见已保存；不会自动删除音符或覆盖原始 MIDI。'}
