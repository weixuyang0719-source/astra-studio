"""Opaque ornament review operations. All hashes/engraving run off the GUI thread."""
from copy import deepcopy
from pathlib import Path
from uuid import uuid4
from . import editing,pipeline
from .score_artifacts import resolve_score_export,current_score_export
from . import notation_ornament_service as service
from .notation_ornaments import _hash,render_review,_validate_catalog


def stamp(path):
    p=Path(path)
    return [p.stat().st_size,p.stat().st_mtime_ns] if p.exists() else None


def _rebuild_manual(project,export,base,catalog,requests):
    from .notation_ornaments import build_catalog
    sources=catalog['identity'].get('source_files',[])
    if len(sources)!=2:raise ValueError('当前谱面缺少可验证的 MIDI/预测来源，请先更新谱面。')
    doc=editing.current(project)
    return build_catalog(base,sources[0]['path'],sources[1]['path'],
        pipeline.read_json(Path(export)/'tempo_map.json'),context=catalog['identity']['context'],
        protected_note_onsets=[{'pitch':n['pitch'],'seconds':n['start']} for n in doc['notes'] if n.get('origin')=='manual'],
        manual_requests=requests)


def bind(project,revision):
    project=Path(project).resolve();doc=editing.current(project);editing._check_base(doc,revision)
    state=pipeline.read_json(project/'project.json');export=resolve_score_export(project,state,doc.get('source_key'))
    if export is None:return {'available':False,'reason':'此项目尚无可复核的正式谱面，请先更新谱面。'}
    if current_score_export(project,state,doc)!=export:
        raise ValueError('保留的正式谱面属于旧编辑版本，请先更新谱面，再审阅装饰候选。')
    if doc.get('score_stale',doc.get('dirty',False)):
        raise ValueError('演奏或拍位已修改，请先更新谱面，再审阅新的装饰候选。')
    path=export/'piano_ornament_catalog.json';base=export/'piano_ornament_base.musicxml'
    if not path.is_file() or not base.is_file():return {'available':False,'reason':'此版导出尚无装饰候选，请先更新谱面。'}
    catalog=pipeline.read_json(path);_validate_catalog(catalog)
    context=catalog['identity']['context']
    if (Path(context.get('project','')).resolve()!=project or
        Path(context.get('export_identity','')).resolve()!=export or
        context.get('editor_revision',revision)!=revision):raise ValueError('候选目录与当前工程/导出版本不符，请更新谱面。')
    overlay=service.read_overlay(project)
    requests=service.manual_requests(overlay)
    if requests:catalog=_rebuild_manual(project,export,base,catalog,requests)
    review,audit=service.project_review(catalog,overlay)
    # Validate actual immutable source/notation identities, not just JSON self-hash.
    folder=project/'cache'/'ornament-review'/uuid4().hex;folder.mkdir(parents=True)
    render_review(base,catalog,review,folder/'validated.musicxml',current_context=context)
    cap={'project':str(project),'base_revision':revision,'export':str(export),
         'catalog':catalog,'catalog_file':str(path),'catalog_hash':_hash(path.read_bytes()),
         'base':str(base),'base_hash':_hash(base.read_bytes()),'context':context,
         'overlay_revision':overlay['revision'],'overlay_stamp':stamp(service.overlay_path(project)),
         'folder':str(folder),'available':True}
    return cap


def validate(cap):
    project=Path(cap['project']);doc=editing.current(project);editing._check_base(doc,cap['base_revision'])
    if current_score_export(project,editor=doc)!=Path(cap['export']):
        raise ValueError('正式导出已变化，请重新打开装饰候选。')
    if service.read_overlay(project)['revision']!=cap['overlay_revision']:raise ValueError('装饰决定已变化，请重新读取。')
    if (_hash(Path(cap['catalog_file']).read_bytes())!=cap['catalog_hash'] or
        _hash(Path(cap['base']).read_bytes())!=cap['base_hash']):raise ValueError('候选来源谱面已变化。')
    for source in cap['catalog']['identity'].get('source_files',[]):
        if _hash(Path(source['path']).read_bytes())!=source['sha256']:
            raise ValueError('装饰候选的原始 MIDI 或模型缓存已变化。')


def public(cap):
    if not cap['available']:return cap
    overlay=service.read_overlay(cap['project']);review,audit=service.project_review(cap['catalog'],overlay)
    export_audit=pipeline.read_json(Path(cap['export'])/'piano_ornament_review.json')
    return {'available':True,'base_revision':cap['base_revision'],'overlay_revision':overlay['revision'],
        'can_undo':bool(overlay['undo_stack']),'needs_update':export_audit['overlay_revision']!=overlay['revision'],
        'pending_count':len(audit['pending_review']),
        'candidates':[{k:c[k] for k in ('id','kind','label','explanation','measure','staff','source_seconds')} |
                      {'decision':review['decisions'].get(c['id'],'')} for c in cap['catalog']['candidates']]}


def action(cap,data):
    kind=data.get('action');project=Path(cap['project'])
    if data.get('base_revision')!=cap['base_revision'] or data.get('overlay_revision')!=cap['overlay_revision']:
        raise ValueError('编辑或装饰决定版本不符，请重新读取。')
    with pipeline.project_lock(project),editing.edit_lock(project):
        validate(cap)
        catalog=cap['catalog'];candidate=data.get('candidate_id')
        if kind=='create_flourish':
            selection=data.get('selection')
            if not isinstance(selection,dict):raise ValueError('请选择花奏的秒范围和左右手。')
            requests=[c['manual_request'] for c in catalog['candidates'] if c.get('manual_request')]
            requests+=service.manual_requests(service.read_overlay(project))
            rebuilt=_rebuild_manual(project,cap['export'],cap['base'],catalog,[*requests,selection])
            rejected=next((r for r in rebuilt.get('rejected_manual_requests',[]) if r['request']==selection),None)
            if rejected:raise ValueError(rejected['reason'])
            updated=deepcopy(cap);updated['catalog']=rebuilt
            return updated,public(updated)|{'message':'已创建人工花奏候选；请先预览再采用，尚未修改谱面或演奏。'}
        if kind!='undo' and not any(c['id']==candidate for c in catalog['candidates']):raise ValueError('请选择当前候选。')
        if kind in ('accept','reject','clear'):
            service.decide_project(project,catalog,cap['overlay_revision'],candidate,kind,current_context=cap['context'])
        elif kind=='undo':service.undo_project(project,cap['overlay_revision'])
        elif kind!='preview':raise ValueError('未知装饰审阅操作。')
    if kind=='preview':
        state=service.read_overlay(project);review,_=service.project_review(catalog,state)
        review=deepcopy(review);review['decisions'][candidate]='accept'
        folder=Path(cap['folder'])/uuid4().hex;folder.mkdir()
        xml=folder/'preview.musicxml';render_review(cap['base'],catalog,review,xml,current_context=cap['context'])
        from .osmd_pdf import render_score_pdf
        from .score_range import _formal_slice,_compact_preview_layout
        from .score_tempo import _measure_layout
        import xml.etree.ElementTree as ET
        from .timing import TempoMap
        from .preview_worker import prepare_preview
        tempo=pipeline.read_json(Path(cap['export'])/'tempo_map.json')
        clock=TempoMap(tempo);selected=next(c for c in catalog['candidates'] if c['id']==candidate)
        layout=list(_measure_layout(ET.parse(xml).getroot()))
        at=next(i for i,(_,measure,_,_,_) in enumerate(layout) if measure.get('number')==str(selected['measure']))
        context=layout[max(0,at-1):at+2]
        starts=[float(row[2]) for row in context];lengths=[float(row[3]) for row in context]
        printed=folder/'print.musicxml';_formal_slice(xml,clock,starts,lengths,printed);_compact_preview_layout(printed)
        pdf=folder/'preview.pdf';render_score_pdf(printed,pdf)
        validate(cap)
        return cap,public(cap)|{'preview':prepare_preview('pdf',pdf,0),'message':'仅预览采用效果，尚未保存决定；当前正式 PDF 不变。'}
    cap=deepcopy(cap);state=service.read_overlay(project)
    cap.update(overlay_revision=state['revision'],overlay_stamp=stamp(service.overlay_path(project)))
    return cap,public(cap)|{'message':'决定已保存，待更新谱面。当前 PDF 尚未改变；原始 MIDI 与 CC64 保持。'}
