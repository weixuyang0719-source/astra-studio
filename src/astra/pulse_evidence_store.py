"""Keep the selected detector result independent of mutable musical anchors."""
import hashlib
import json
from pathlib import Path
from copy import deepcopy


def _immutable(path,payload):
    path.parent.mkdir(parents=True,exist_ok=True)
    try:
        with path.open('xb') as stream:stream.write(payload)
    except FileExistsError:
        if path.read_bytes()!=payload:
            raise ValueError('已保存的拍点证据内容改变；未覆盖历史证据')


def store_result(project, project_source_sha256, result):
    """Content-address a complete worker result; never replace another decode."""
    if result.get('provider') != 'cubase_local_reconstruction':
        raise ValueError('仅可保存本机独立拍点结果')
    if not project_source_sha256 or not result.get('source_sha256'):
        raise ValueError('拍点证据缺少工程或归一化音频身份')
    root=Path(project).resolve();result=deepcopy(result)
    # Reusing another project must not leave a live dependency on its mutable
    # cache. Copy only the small original observations, not audio or models.
    if result.get('observations_path'):
        original=Path(result['observations_path']).resolve()
        if not original.is_relative_to(root):
            raw=original.read_bytes();obs_sha=hashlib.sha256(raw).hexdigest()
            if obs_sha!=result.get('observations_sha256'):
                raise ValueError('复用观测与拍点结果的身份不一致')
            manifest_raw=(original.parent/'manifest.json').read_bytes()
            manifest=json.loads(manifest_raw)
            if (manifest.get('sha256')!=obs_sha or manifest.get('identity')!=
                result.get('observation_provenance',{}).get('worker_observation_identity')):
                raise ValueError('复用观测清单与原始模型身份不一致')
            folder=root/'cache'/'pulse-observations'/obs_sha/original.parent.name
            if not folder.resolve().is_relative_to(root):raise ValueError('观测目标路径无效')
            _immutable(folder/original.name,raw)
            _immutable(folder/'manifest.json',manifest_raw)
            result['observations_path']=str(folder/original.name)
    payload=json.dumps(result,ensure_ascii=False,sort_keys=True,
                       allow_nan=False,separators=(',',':')).encode('utf-8')
    digest=hashlib.sha256(payload).hexdigest()
    path=root/'cache'/'pulse-evidence'/f'{digest}.json'
    # Exclusive creation prevents one candidate from rewriting evidence that
    # another editor revision still references.
    _immutable(path,payload)
    return {'version':1,'provider':result['provider'],
            'project_source_sha256':project_source_sha256,
            'result_path':path.relative_to(root).as_posix(),
            'result_sha256':digest,'normalized_sha256':result['source_sha256'],
            'observations_sha256':result.get('observations_sha256'),
            'decode_key':result.get('cache',{}).get('decode_key')}


def copy_reference(source_project, target_project, tempo, project_source_sha256):
    """Rehome a reused clock's evidence without changing any musical address."""
    from .active_pulse_evidence import resolve
    if Path(source_project).resolve()==Path(target_project).resolve():return deepcopy(tempo)
    result=deepcopy(tempo)
    if result.get('pulse_evidence') is None:return result
    evidence=resolve(source_project,tempo=result)
    if evidence['evidence_ref']['project_source_sha256']!=project_source_sha256:
        raise ValueError('不能将另一原曲的拍点证据复制到本工程')
    result['pulse_evidence']=store_result(target_project,project_source_sha256,evidence['result'])
    if result.get('adopted_candidate',{}).get('pulse_evidence') is not None:
        result['adopted_candidate']['pulse_evidence']=deepcopy(result['pulse_evidence'])
    return result
