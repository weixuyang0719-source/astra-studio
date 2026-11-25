"""Locate a retained successful score independently of the last MIDI export."""
import json
from pathlib import Path


def _read(path):
    try:
        value=json.loads(path.read_text(encoding='utf-8'))
        return value if isinstance(value,dict) else {}
    except (OSError,ValueError):
        return {}


def resolve_score_export(project,state=None,source_key=None):
    """Return a project-local score snapshot, never imply it is the current edit.

    Revision freshness remains the editor's responsibility. Source identity is
    required when an editor exists, so a model/input switch cannot resurrect an
    unrelated score. The directory scan only supports pre-pointer projects.
    """
    project=Path(project).resolve();root=project/'exports'
    if state is None:
        from .pipeline import read_json
        try:
            state=read_json(project/'project.json')
        except (OSError,ValueError):
            state={}
    index=_read(project/'editing'/'current.json')
    if source_key is None:
        revision=index.get('revision')
        if isinstance(revision,str) and Path(revision).name==revision:
            source_key=_read(project/'editing'/'revisions'/f'{revision}.json').get('source_key')
    def candidates():
        for field in ('latest_export','latest_score_export'):
            value=state.get(field)
            if isinstance(value,str) and value:
                yield root/Path(value).name
        # An explicit modern pointer is a commit boundary. A worker may have
        # completed report.json before it was cancelled or failed to publish;
        # scanning those directories would silently promote uncommitted output.
        if 'latest_score_export' not in state and index.get('score_revision') and root.is_dir():
            from .provisional_score import draft_directory_name
            for candidate in sorted(root.iterdir(),reverse=True):
                if not draft_directory_name(candidate.name) and _read(candidate/'editor_snapshot.json').get('revision')==index['score_revision']:
                    yield candidate
    seen=set()
    for candidate in candidates():
        if candidate in seen:continue
        seen.add(candidate)
        if candidate.is_symlink() or not candidate.is_dir() or candidate.resolve().parent!=root:continue
        report=_read(candidate/'report.json');snapshot=_read(candidate/'editor_snapshot.json')
        if report.get('status') not in ('complete','complete_with_warnings'):continue
        if not (candidate/'piano.musicxml').is_file() or not snapshot.get('revision'):continue
        if source_key is not None and snapshot.get('source_key')!=source_key:continue
        if report.get('editor_revision') not in (None,snapshot['revision']):continue
        return candidate
    return None


def resolve_pdf_export(project, state):
    """Resolve the last committed PDF without confusing it with current XML."""
    root = Path(project).resolve()/'exports'
    source = (state.get('analysis_keys') or {}).get('piano')
    for field in ('latest_pdf_export', 'latest_score_export', 'latest_export'):
        value = state.get(field)
        if not isinstance(value, str) or not value:
            continue
        folder = root/Path(value).name
        pdf = folder/'piano.pdf'
        if folder.is_symlink() or folder.resolve().parent != root or pdf.is_symlink() or not pdf.is_file():
            continue
        report = _read(folder/'report.json')
        if report.get('status') not in ('complete', 'complete_with_warnings') or 'piano.pdf' not in report.get('files', []):
            continue
        rendered = report.get('instruments', {}).get('piano', {}).get('pdf_render', {})
        snapshot = _read(folder/'editor_snapshot.json')
        bound_source = rendered.get('source_key') or snapshot.get('source_key')
        if source is not None and bound_source != source:
            continue
        if rendered.get('status') not in (None, 'ready'):
            continue
        try:
            with pdf.open('rb') as stream:
                if stream.read(5) != b'%PDF-':
                    continue
        except OSError:
            continue
        revision = report.get('editor_revision') or snapshot.get('revision')
        return pdf, revision
    return None


def current_score_export(project,state=None,editor=None):
    """A retained score may be carried into display edits only at its exact revision."""
    if editor is None:
        from .editing import current
        editor=current(project)
    if not editor:return None
    score=resolve_score_export(project,state,editor.get('source_key'))
    if score and _read(score/'editor_snapshot.json').get('revision')==editor['revision']:
        return score
    return None
