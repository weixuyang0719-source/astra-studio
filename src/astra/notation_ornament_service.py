"""Project-persistent ornament decisions, reprojected onto each fresh export.

No prediction, performance-note editing or export-pointer publication happens
here. The UI bridge owns project/editor CAS and opaque capability tokens.
"""
from copy import deepcopy
from pathlib import Path
import json
import os

from .notation_ornaments import (
    VERSION, _hash, _atomic, _validate_catalog, build_catalog, render_review,
)


def overlay_path(project):
    return Path(project)/'notation_ornaments.json'


def read_overlay(project):
    path = overlay_path(project)
    if not path.exists(): return dict(version=VERSION, revision=0, decisions={}, history=[], undo_stack=[])
    state = json.loads(path.read_text(encoding='utf8'))
    if (state.get('version') != VERSION or type(state.get('revision')) is not int
            or state['revision'] < 0 or not isinstance(state.get('decisions'),dict)
            or not isinstance(state.get('history'),list) or not isinstance(state.get('undo_stack'),list)):
        raise ValueError('Invalid persistent ornament overlay; original decisions were not overwritten')
    for key, decision in state['decisions'].items():
        if decision.get('decision') not in ('accept','reject') or key != _hash(decision['binding']):
            raise ValueError('Invalid ornament decision identity')
    return state


def decision_binding(catalog, candidate):
    return dict(input_sha256=catalog['identity']['input_sha256'], kind=candidate['kind'],
                input_ids=candidate['input_ids'], anchor_input_ids=candidate['anchor_input_ids'])


def manual_requests(state):
    return [deepcopy(item['candidate']['manual_request']) for item in state['decisions'].values()
            if item.get('candidate',{}).get('kind')=='free_run' and item['candidate'].get('manual_request')]


def _context(catalog,current_context):
    _validate_catalog(catalog)
    if catalog['identity'].get('context') != current_context:
        raise ValueError('Ornament project/editor/export context changed')


def decide_project(project,catalog,expected_revision,candidate_id,decision,*,current_context):
    """Adopt/reject/clear one reviewed candidate, preserving prior history."""
    _context(catalog,current_context)
    if decision not in ('accept','reject','clear'): raise ValueError('Unknown ornament decision')
    candidate = next((c for c in catalog['candidates'] if c['id']==candidate_id),None)
    if candidate is None: raise ValueError('Unknown ornament candidate')
    binding=decision_binding(catalog,candidate); key=_hash(binding)
    def action(state):
        state['undo_stack'].append(deepcopy(state['decisions']))
        if decision=='clear': state['decisions'].pop(key,None)
        else: state['decisions'][key]=dict(binding=binding,decision=decision,
            accepted_catalog_sha256=catalog['catalog_sha256'], candidate=deepcopy(candidate))
        return dict(candidate_id=candidate_id,stable_id=key,decision=decision)
    return _mutate(project,expected_revision,action)


def undo_project(project,expected_revision):
    def action(state):
        if not state['undo_stack']: raise ValueError('No ornament decision to undo')
        state['decisions']=state['undo_stack'].pop(); return 'undo'
    return _mutate(project,expected_revision,action)


def _mutate(project,expected_revision,action):
    path=overlay_path(project);path.parent.mkdir(parents=True,exist_ok=True);lock=path.with_suffix('.lock')
    from contextlib import ExitStack
    from .process_locks import owned_lock, LockUnavailable
    with ExitStack() as stack:
        try:
            stack.enter_context(owned_lock(lock))
        except (OSError,LockUnavailable) as exc:
            raise ValueError('Ornament overlay is busy or its lock cannot be safely recovered; preserved') from exc
        state=read_overlay(project)
        if state['revision']!=expected_revision: raise ValueError('Ornament overlay revision changed')
        before=deepcopy(state['decisions']);operation=action(state);state['revision']+=1
        state['history'].append(dict(revision=state['revision'],operation=operation,before=before,
                                     after=deepcopy(state['decisions'])))
        _atomic(path,json.dumps(state,ensure_ascii=False,indent=2).encode());return state



def project_review(catalog,state):
    """Revalidate the candidate class, not just old ID or XML page location."""
    _validate_catalog(catalog); current={_hash(decision_binding(catalog,c)):c for c in catalog['candidates']}
    decisions={};pending=[];applied=[]
    for key, stored in state['decisions'].items():
        candidate=current.get(key)
        if candidate is None:
            pending.append(dict(stable_id=key,decision=stored['decision'],
                input_ids=stored['binding']['input_ids'],
                reason='Input identity or candidate evidence changed; saved decision retained for review.'))
        else:
            decisions[candidate['id']]=stored['decision']
            applied.append(dict(stable_id=key,candidate_id=candidate['id'],decision=stored['decision'],
                                reprojected=stored['accepted_catalog_sha256']!=catalog['catalog_sha256']))
    review=dict(version=VERSION,catalog_sha256=catalog['catalog_sha256'],revision=state['revision'],decisions=decisions)
    return review,dict(overlay_revision=state['revision'],reprojected_decisions=applied,pending_review=pending)


def preview_project(project,catalog,base_xml,output_xml,*,expected_revision,current_context):
    _context(catalog,current_context);state=read_overlay(project)
    if state['revision']!=expected_revision: raise ValueError('Ornament overlay revision changed')
    review,audit=project_review(catalog,state)
    audit['engraving']=render_review(base_xml,catalog,review,output_xml,current_context=current_context)
    assert_revision(project,audit)
    return audit


def assert_revision(project,audit):
    if read_overlay(project)['revision']!=audit['overlay_revision']:
        raise ValueError('Ornament decisions changed during export; rerun with current revision')


def apply_to_export(project,xml_path,input_midi,prediction_npz,tempo,*,context=None,protected_note_onsets=()):
    """Called after smart/roll, before harmony, reference clock, score MIDI/PDF.

    Fresh catalogs are written per export. No persistent user decision is
    deleted when its old source/model no longer has a matching candidate.
    """
    xml_path=Path(xml_path);state=read_overlay(project)
    base=xml_path.with_name('piano_ornament_base.musicxml')
    _atomic(base,xml_path.read_bytes())
    context=context or dict(project=str(Path(project).resolve()),export_identity=str(xml_path.parent.resolve()))
    catalog=build_catalog(base,input_midi,prediction_npz,tempo,
                          protected_note_onsets=protected_note_onsets,context=context,
                          manual_requests=manual_requests(state))
    review,audit=project_review(catalog,state)
    staged=xml_path.with_name('piano_ornament_pending.musicxml')
    audit['engraving']=render_review(base,catalog,review,staged,current_context=context)
    assert_revision(project,audit)
    staged.replace(xml_path)
    catalog_path=xml_path.with_name('piano_ornament_catalog.json')
    _atomic(catalog_path,json.dumps(catalog,ensure_ascii=False,indent=2).encode())
    audit.update(catalog=str(catalog_path.resolve()),base_musicxml=str(base.resolve()),
                 candidate_count=len(catalog['candidates']),context=context)
    _atomic(xml_path.with_name('piano_ornament_review.json'),json.dumps(audit,ensure_ascii=False,indent=2).encode())
    return audit


def rebind_copied_export(project,directory,final_directory):
    """Relocate verified annotation-only snapshots without adopting new choices.

    Chord toggles do not regenerate notation. Retain the original exported
    overlay revision (possibly stale), so pending user choices stay visibly
    pending rather than falsely being advertised as present in this PDF.
    """
    import xml.etree.ElementTree as ET
    from .notation_ornaments import _semantic
    directory,final=Path(directory),Path(final_directory)
    path=directory/'piano_ornament_catalog.json'
    if not path.is_file():return None
    catalog=json.loads(path.read_text(encoding='utf8'));_validate_catalog(catalog)
    base=directory/'piano_ornament_base.musicxml'
    if _semantic(ET.parse(base).getroot())!=catalog['identity']['notation_sha256']:
        raise ValueError('Copied ornament notation identity changed')
    for source in catalog['identity'].get('source_files',[]):
        if _hash(Path(source['path']).read_bytes())!=source['sha256']:
            raise ValueError('Copied ornament input identity changed')
    old_context=catalog['identity'].get('context',{})
    if Path(old_context.get('project','')).resolve()!=Path(project).resolve():
        raise ValueError('Copied ornament catalog belongs to another project')
    catalog['identity']['context']=dict(old_context,export_identity=str(final.resolve()))
    ids={}
    for candidate in catalog['candidates']:
        old=candidate['id'];candidate['id']=_hash([catalog['identity'],candidate['selected'],candidate['anchors'],candidate['kind']])[:24]
        ids[old]=candidate['id']
    catalog['catalog_sha256']=_hash([catalog['identity'],catalog['candidates']])
    audit_path=directory/'piano_ornament_review.json'
    audit=json.loads(audit_path.read_text(encoding='utf8'))
    audit['context']=catalog['identity']['context']
    audit['catalog']=str((final/path.name).resolve());audit['base_musicxml']=str((final/base.name).resolve())
    audit['engraving']['accepted_ids']=[ids[value] for value in audit['engraving']['accepted_ids']]
    for item in audit.get('reprojected_decisions',[]):item['candidate_id']=ids[item['candidate_id']]
    _atomic(path,json.dumps(catalog,ensure_ascii=False,indent=2).encode())
    _atomic(audit_path,json.dumps(audit,ensure_ascii=False,indent=2).encode())
    return audit
