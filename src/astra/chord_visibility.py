"""Transactional chord visibility changes on an immutable export snapshot.

No model or harmony analysis is invoked. MusicXML notes and MIDI event clocks
are retained; only labels and the PDF engraving are refreshed.
"""
from copy import deepcopy
from datetime import datetime
from pathlib import Path
import hashlib
import shutil
import tempfile
import xml.etree.ElementTree as ET

from .chord_markers import write_export_chord_markers


def _xml_visibility(path, chords, enabled, visibility):
    from .notation_readability import sounding_signature
    from .score_tempo import _measure_layout
    before=sounding_signature(path)
    tree=ET.parse(path);root=tree.getroot()
    layout=_measure_layout(root)
    generated=0
    # Cached labels are inserted directly, avoiding a music21 score roundtrip.
    # Author-provided harmony in a measure takes precedence over our analysis.
    if enabled:
        from music21 import harmony
        from music21.musicxml.m21ToXml import MeasureExporter
        first_part=root.find('part')
        primary=[item for item in layout if item[0] is first_part]
        for _,measure,start,length,divisions in primary:
            if measure.findall('harmony'): continue
            insertion=next((i for i,c in enumerate(measure) if c.tag in ('note','backup','forward')),len(measure))
            for event in chords:
                if event.get('uncertain',True): continue
                beat=float(event['absolute_quarters'])
                if not float(start)<=beat<float(start+length): continue
                offset=(beat-float(start))*divisions
                if abs(offset-round(offset))>1e-5:
                    raise ValueError('缓存和弦拍位超出原谱分辨率，请常规重新导出')
                node=MeasureExporter().chordSymbolToXml(harmony.ChordSymbol(event['label']),append=False)
                old=node.find('offset')
                if old is not None: node.remove(old)
                if round(offset): ET.SubElement(node,'offset').text=str(round(offset))
                measure.insert(insertion,node);insertion+=1;generated+=1
    originals=visibility.setdefault('original_print_object',{})
    count=0
    for pi,part in enumerate(root.findall('part')):
        for mi,measure in enumerate(part.findall('measure')):
            for hi,node in enumerate(measure.findall('harmony')):
                key=f'{pi}/{mi}/{hi}'
                originals.setdefault(key,node.get('print-object'))
                if enabled:
                    original=originals[key]
                    if original is None:node.attrib.pop('print-object',None)
                    else:node.set('print-object',original)
                else:node.set('print-object','no')
                count+=1
    if enabled and not count:
        raise ValueError('没有可显示的可靠和弦标签，请先常规重新导出生成和声分析')
    tree.write(path,encoding='utf-8',xml_declaration=True)
    if sounding_signature(path)!=before:
        raise RuntimeError('和弦显示修改意外改变了音符，已取消发布')
    visibility.update(show_chords=enabled,harmony_nodes=count,inserted_from_cache=generated)
    return visibility


def export_chord_visibility(project, base_export, show_chords, log=print, *,
                            _update_harmony=None, _operation='chord_visibility'):
    from . import pipeline
    from .osmd_pdf import render_score_pdf
    if type(show_chords) is not bool:raise ValueError('和弦显示必须是布尔值')
    project=Path(project).resolve();base=Path(base_export).resolve()
    with pipeline.project_lock(project):
        state=pipeline.read_json(project/'project.json')
        if (base.parent!=(project/'exports').resolve() or not base.is_dir()
                or Path(state['latest_export']).resolve()!=base):
            raise ValueError('导出结果已变化，请返回最新结果后再切换和弦')
        report=pipeline.read_json(base/'report.json')
        from . import editing
        editor=editing.current(project)
        if editor is not None:
            if editor['dirty']:
                raise ValueError('当前编辑尚未生成结果，请先更新谱面/MIDI，再分析或切换和弦。')
        if report.get('status') not in ('complete','complete_with_warnings'):
            raise ValueError('只能修改成功导出的和弦显示')
        tempo=pipeline.read_json(base/'tempo_map.json')
        name=datetime.now().strftime('%Y%m%d-%H%M%S')+'-chords'
        final=base.parent/name
        counter=1
        while final.exists():
            final=base.parent/f'{name}-{counter}';counter+=1
        pending=Path(tempfile.mkdtemp(prefix='.chords-pending-',dir=base.parent))
        try:
            shutil.copytree(base,pending,dirs_exist_ok=True)
            # MIDI-only exports do not invalidate an existing score at the same
            # editor revision. Include that immutable score for this explicit
            # display edit, so the visible PDF and MIDI labels change together.
            if not (pending/'piano.musicxml').is_file():
                from .score_artifacts import current_score_export,resolve_score_export
                retained=current_score_export(project,state,editor)
                if retained is None and editor and resolve_score_export(project,state,editor.get('source_key')):
                    raise ValueError('已有谱面属于旧编辑版本，请先更新谱面，再修改谱面和弦显示。')
                if retained is not None:
                    for artifact in retained.iterdir():
                        if (artifact.is_file() and not artifact.is_symlink()
                                and artifact.name.startswith('piano')
                                and not (pending/artifact.name).exists()):
                            shutil.copy2(artifact,pending/artifact.name)
                    old_report=pipeline.read_json(retained/'report.json')
                    old_entry=old_report.get('instruments',{}).get('piano',{})
                    entry=report.setdefault('instruments',{}).setdefault('piano',{})
                    for key,value in old_entry.items():entry.setdefault(key,value)
                    report['retained_score_export']=str(retained)
            new_state=deepcopy(state)
            if editor is not None:
                new_state['_editor_revision']=editor['revision']
                new_state['_editor_tempo']=editor['tempo']
            if _update_harmony is not None:
                _update_harmony(pending, new_state, tempo, log)
            audits={}
            for instrument in state.get('instruments',['piano']):
                xml=pending/f'{instrument}.musicxml'
                harmony_path=pending/f'{instrument}_harmony.json'
                cache=pipeline.read_json(harmony_path) if harmony_path.is_file() else {}
                chords=cache.get('chords',[])
                midi_paths=[pending/f'{instrument}_{v}.mid' for v in
                            ('raw','quantized','reviewed','score','performance','target_grid','seconds_120')]
                relevant=xml.is_file() or any(p.is_file() for p in midi_paths)
                if not relevant:continue
                reliable=[c for c in chords if not c.get('uncertain',True)]
                if show_chords and not reliable:
                    raise ValueError('缺少可靠的缓存和弦标签，请先常规重新导出；快速切换不会重新分析')
                audit_path=pending/f'{instrument}_chord_visibility.json'
                audit=pipeline.read_json(audit_path) if audit_path.is_file() else {}
                if xml.is_file():
                    audit=_xml_visibility(xml,chords,show_chords,audit)
                    pdf=pending/f'{instrument}.pdf'
                    log('正在本地重排和弦显示 PDF')
                    pdf_render=render_score_pdf(xml,pdf)
                    report['instruments'].setdefault(instrument,{})['pdf_render']={'status':'ready',**pdf_render,
                        'source_key':(state.get('analysis_keys') or {}).get(instrument)}
                    # The existing staging copy may contain a projection from
                    # an older renderer. It cannot describe the new PDF.
                    for suffix in ('.print.musicxml', '_print_layout.json'):
                        (pending/f'{instrument}{suffix}').unlink(missing_ok=True)
                counts=write_export_chord_markers(pending,instrument,chords,tempo,
                    source_kind=state.get('source_kind'),enabled=show_chords)
                audit.update(show_chords=show_chords,midi_marker_counts=counts,
                    base_export=str(base),analysis_reused=True)
                pipeline.save_json(audit_path,audit);audits[instrument]=audit
                entry=report.setdefault('instruments',{}).setdefault(instrument,{})
                entry.update(chord_marker_counts=counts,chord_markers=max(counts.values(),default=0))
                if harmony_path.is_file():
                    cache['visible_chords']=show_chords
                    if xml.is_file():cache['musicxml_path']=str(final/xml.name)
                    pipeline.save_json(harmony_path,cache)
                for variant in ('score','reviewed'):
                    metadata=pending/f'{instrument}_{variant}_midi.json'
                    if metadata.is_file():
                        data=pipeline.read_json(metadata)
                        data['output_path']=str(final/f'{instrument}_{variant}.mid')
                        if 'output' in data:data['output']=data['output_path']
                        if xml.is_file() and variant=='score':
                            data['source_xml_sha256']=hashlib.sha256(xml.read_bytes()).hexdigest()
                        for field in ('midi_sha256','output_sha256'):
                            if field in data:data[field]=hashlib.sha256((pending/f'{instrument}_{variant}.mid').read_bytes()).hexdigest()
                        pipeline.save_json(metadata,data)
                        if f'{variant}_midi' in entry:entry[f'{variant}_midi']=data
            if not audits:raise ValueError('当前导出没有可切换和弦的乐谱或 MIDI')
            from .notation_ornament_service import rebind_copied_export
            rebind_copied_export(project,pending,final)
            report.update(show_chords=show_chords,operation=_operation,derived_from_export=str(base),
                chord_visibility=audits)
            # Keep every artifact, including review identities and immutable audio bindings.
            if 'stem_paths' not in report:
                report['stem_paths']=(state.get('legacy_stem_paths',{}).get(str(base))
                    or state.get('stem_paths') or pipeline._snapshot_legacy_stems(project,state))
            report['files']=sorted(set(report.get('files',[]))|{p.name for p in pending.iterdir() if p.is_file() and p.name!='report.json'})
            # These XMLs are internal projections, never alternate synchronized
            # MusicXML choices in the application's score/open-file controls.
            report['files']=[name for name in report['files'] if (pending/name).is_file() and not
                name.endswith(('.print.musicxml','_print_layout.json','_ornament_base.musicxml'))]
            pipeline.save_json(pending/'report.json',report)
            new_state.setdefault('config',{}).setdefault('score',{})['show_chords']=show_chords
            pending.rename(final)
            return pipeline._commit_export(project,new_state,tempo,final,log)
        finally:
            # Only our verified, private staging directory is eligible for cleanup.
            if pending.is_dir() and pending.parent==base.parent and pending.name.startswith('.chords-pending-'):
                shutil.rmtree(pending)
