"""Reuse committed results across explicit CPU/CUDA choices, not identities.

Exact analysis keys and worker provenance remain unchanged. This resolver only
looks at the current project's committed artifacts, never guesses another model
or scans another project's cache. Unfinished stages keep the requested device.
"""
from copy import deepcopy
from pathlib import Path


def _origin(state, stage):
    from .pipeline import _analysis_keys
    key = state.get('analysis_keys', {}).get(stage)
    recorded = state.get('analysis_origins', {}).get(stage)
    if recorded is not None:
        if not isinstance(recorded, dict) or recorded.get('key') != key:
            return None
        config = recorded.get('config')
        separator = recorded.get('separator_key')
    else:
        # A legacy config is evidence only if it reproduces the recorded key.
        config = state.get('config')
        separator = None
    if not isinstance(config, dict) or not key:
        return None
    if _analysis_keys(config, separator_key=separator if stage in ('piano', 'guitar') else None).get(stage) != key:
        return None
    return {'key': key, 'config': deepcopy(config),
            'separator_key': separator or _analysis_keys(config)['separator']}


def _compatible(requested, original, stage):
    from .pipeline import _analysis_keys
    from .rhythm_provider import selected_provider
    if stage == 'separator':
        names = ('separator',)
    elif stage in ('piano', 'guitar'):
        names = ('separator', 'transkun' if stage == 'piano' else 'basic_pitch')
    else:
        if selected_provider(requested) != selected_provider(original):
            return False
        names = (selected_provider(requested),)
    a, b = deepcopy(requested), deepcopy(original)
    for name in names:
        left = a.get('models', {}).get(name, {})
        right = b.get('models', {}).get(name, {})
        if left.get('device') != right.get('device'):
            if left.get('device') not in ('cpu', 'cuda') or right.get('device') not in ('cpu', 'cuda'):
                return False
        left.pop('device', None)
        right.pop('device', None)
    # All remaining options (including python, precision and model paths) stay
    # in the existing exact key machinery. No defaults/version guesses here.
    return _analysis_keys(a)[stage] == _analysis_keys(b)[stage]


def resolve_keys(project, state, config, instruments, keep_stems=True):
    """Return actual artifact keys and their verifiable creation configurations."""
    from . import pipeline
    project = Path(project)
    keys = pipeline._analysis_keys(config)
    origins = {}
    if not state or state.get('source_kind') == 'midi' or 'analysis_keys' not in state:
        return keys, origins
    duration = state['duration']
    old = {stage: _origin(state, stage) for stage in keys}

    def notes_exist(stage, key):
        try:
            bundle = pipeline.read_json(pipeline._notes_path(project, stage, key))
            pipeline.validate_notes(bundle['notes'])
            return isinstance(bundle.get('pedals', []), list)
        except (OSError, ValueError, KeyError, TypeError):
            return False

    def stems_exist(key, required):
        for instrument in required:
            frozen = project / 'stems' / key / f'{instrument}.wav'
            if pipeline._wave_complete(frozen, duration):
                continue
            path = pipeline.stem_path(project, state, instrument)
            if (not path or state.get('stem_keys', {}).get(instrument) != key
                    or not pipeline._wave_complete(path, duration)):
                return False
        return True

    old_piano = old['piano']
    reusable_notes = bool(old_piano and _compatible(config, old_piano['config'], 'piano')
                          and notes_exist('piano', old_piano['key']))
    need_piano = ('piano' in instruments and not notes_exist('piano', keys['piano'])
                  and not reusable_notes)
    required = list(instruments) if keep_stems else [n for n in instruments if n == 'guitar']
    if need_piano and 'piano' not in required:
        required.append('piano')
    separator = old['separator']
    if (separator and separator['key'] != keys['separator']
            and _compatible(config, separator['config'], 'separator')
            and not stems_exist(keys['separator'], required)
            and stems_exist(separator['key'], required)):
        keys['separator'] = separator['key']
        origins['separator'] = separator
    elif (separator and not required and reusable_notes
          and _compatible(config, separator['config'], 'separator')):
        # No audio is requested or needed; retain the dependency of the actual
        # committed note bundle, even when the user chose not to keep stems.
        keys['separator'] = separator['key']
        origins['separator'] = separator

    # New transcription keys must name the actual audio dependency, even if
    # that audio was produced on the other device.
    keys.update({k: v for k, v in pipeline._analysis_keys(
        config, separator_key=keys['separator']).items() if k in ('piano', 'guitar')})
    for stage in ('piano', 'guitar'):
        prior = old[stage]
        if (stage in instruments and prior and prior['key'] != keys[stage]
                and prior['separator_key'] == keys['separator']
                and _compatible(config, prior['config'], stage)
                and not notes_exist(stage, keys[stage]) and notes_exist(stage, prior['key'])):
            keys[stage] = prior['key']
            origins[stage] = prior

    beat = old['beat_this']
    if beat and beat['key'] != keys['beat_this'] and _compatible(config, beat['config'], 'beat_this'):
        try:
            from .rhythm_provider import selected_provider, reusable_auto_map
            result = pipeline.read_json(project / 'cache' / f'beats-{beat["key"]}.json')
            tempo = pipeline._committed_tempo(project, state)
            native = selected_provider(config) == 'cubase_local'
            correct_provider = (result.get('provider') == 'cubase_local_reconstruction') if native else (
                result.get('provider') in (None, 'beat_this'))
            if (correct_provider and isinstance(result.get('beats'), list) and result['beats']
                    and reusable_auto_map(tempo, beat['key'], beat['key'])):
                keys['beat_this'] = beat['key']
                origins['beat_this'] = beat
        except (OSError, ValueError, KeyError, TypeError):
            pass
    # Same-device reuses also need original provenance on subsequent runs.
    for stage, prior in old.items():
        if prior and keys[stage] == prior['key']:
            origins.setdefault(stage, prior)
    return keys, origins


def committed_origins(config, keys, reused, previous=None):
    """Bind each published key to its creation config; never relabel devices."""
    from .pipeline import _analysis_keys
    result = {}
    for stage, key in keys.items():
        if not key:
            continue
        prior = reused.get(stage) or (_origin(previous, stage) if previous else None)
        if prior and prior['key'] == key:
            result[stage] = deepcopy(prior)
        elif _analysis_keys(config, separator_key=keys['separator'] if stage in ('piano', 'guitar') else None).get(stage) == key:
            result[stage] = {'key': key, 'config': deepcopy(config), 'separator_key': keys['separator']}
    return result
