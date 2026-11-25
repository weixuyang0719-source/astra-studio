"""Provider identities and cache boundaries; importing this loads no models."""
from pathlib import Path
from copy import deepcopy


PROVIDERS = ('beat_this', 'cubase_local')


def new_task_config(config, root):
    """Defaults for new requests only; never reinterpret a stored project config."""
    result=deepcopy(config)
    result.setdefault('rhythm_provider','cubase_local')
    provider=selected_provider(result)
    if provider=='cubase_local':
        models=result.setdefault('models',{})
        models['cubase_local']={
            'python':str(Path(root)/'.venv'/'Scripts'/'python.exe'),
            'device':'cuda','min_bpm':60.,'max_bpm':240.,
            'meter_stay':.95,'quarters_per_pulse':1.,
            **models.get('cubase_local',{})}
    return result


def selected_provider(config):
    provider = config.get('rhythm_provider', 'beat_this')
    if provider not in PROVIDERS:
        raise ValueError(f'Unknown rhythm provider: {provider!r}')
    return provider


def analysis_identity(config):
    """Keep existing legacy keys stable; never relabel legacy observations."""
    provider = selected_provider(config)
    options = config.get('models', {}).get(provider, {})
    if provider == 'beat_this':
        model_dir = options.get('model_dir', config.get('model_dir', 'models'))
        checkpoint = options.get('checkpoint') or str(Path(model_dir) / 'final0.ckpt')
        return [1, checkpoint, options]
    from .cubase_local_network import DLL_SHA256, MODEL_SHA256
    from .cubase_local_worker import WORKER_VERSION, FEATURE_VERSION, DECODER_VERSION, NETWORK_VERSION
    from .cubase_pulse_map import MAPPING_VERSION
    # This is the accepted installed build, not a promise it is installed.
    # The worker verifies actual bytes and refuses incompatible components.
    return [1, provider, DLL_SHA256, MODEL_SHA256,
            WORKER_VERSION, FEATURE_VERSION, DECODER_VERSION, NETWORK_VERSION, MAPPING_VERSION, options]


def reusable_auto_map(tempo, requested_key, previous_key):
    """An auto map needs matching provider/config and mapping implementation."""
    if requested_key != previous_key:
        return False
    if tempo.get('source') == 'duet_first_pass':
        # Rebuilt from the (cached) DuET prediction of the current notes.
        return False
    if tempo.get('source') == 'cubase_local_reconstruction':
        from .cubase_pulse_map import MAPPING_VERSION
        return tempo.get('pulse_mapping_version') == MAPPING_VERSION
    from .timing import BEAT_MAPPING_VERSION
    from .pipeline import _meter_mapping_current
    return (tempo.get('beat_mapping_version') == BEAT_MAPPING_VERSION
            and _meter_mapping_current(tempo))
