"""Production observation-cache-only re-decoding; no legacy cache adoption."""
import json
import math
from pathlib import Path
import time


def redecode_cached_result(base_result,output_dir,options=None,log=print):
    from . import cubase_local_worker as w
    started=time.perf_counter();base=json.loads(w._json(base_result))
    if base.get('provider')!='cubase_local_reconstruction':
        raise ValueError('仅接受正式本机节拍结果，研究缓存不能冒充生产缓存')
    provenance=base.get('observation_provenance',{});identity=provenance.get('worker_observation_identity')
    if not isinstance(identity,dict):raise ValueError('缺少正式观测缓存身份；不会重新推理或迁移研究缓存')
    if (identity.get('worker_version')!=w.WORKER_VERSION or identity.get('feature_version')!=w.FEATURE_VERSION
        or identity.get('network_version')!=w.NETWORK_VERSION or identity.get('component_sha256')!=w.DLL_SHA256
        or identity.get('model_sha256')!=w.MODEL_SHA256 or identity.get('precision')!=w.PRECISION
        or identity.get('sample_rate')!=44100 or identity.get('source_sha256')!=base.get('source_sha256')):
        raise ValueError('本机观测来源或算法版本不匹配')
    count=identity.get('sample_frames')
    if type(count) is not int or count<=0:raise ValueError('观测音频帧数无效')
    frames,_=w._frame_count(count);duration=count/44100
    if not math.isfinite(float(base.get('audio_duration',-1))) or abs(base['audio_duration']-duration)>1e-9:
        raise ValueError('来源时长与观测身份不一致')
    origin=float(base.get('clock_origin_seconds',0.))
    if not math.isfinite(origin) or origin<0:raise ValueError('来源时钟原点无效')
    observation_key=w._key(identity);path=Path(base['observations_path']).resolve(strict=True)
    if path.parent.name!=observation_key or base.get('cache',{}).get('observation_key')!=observation_key:
        raise ValueError('观测目录与正式缓存身份不一致')
    values,verified_provenance,verified_path,digest=w._read_observations(path.parent,identity,frames)
    if verified_path.resolve()!=path or base.get('observations_sha256')!=digest or verified_provenance!=provenance:
        raise ValueError('请求结果与观测文件摘要或内部来源不一致')
    accepted={'min_bpm','max_bpm','meters','transition_lambda','meter_stay','known_downbeats','max_frames'}
    supplied=dict(options or {})
    if set(supplied)-accepted:raise ValueError('观测重解码只接受解码参数，不接受网络/源音频选项')
    inherited={k:v for k,v in base.get('parameters',{}).items() if k in accepted}
    inherited.update(meters=[6,8]);inherited.update(supplied)
    decoder=w._decoder_options(inherited,frames)
    decode_identity=dict(decoder_version=w.DECODER_VERSION,observation_key=observation_key,
                         observations_sha256=digest,parameters=decoder)
    key=w._key(decode_identity);output=Path(output_dir).resolve()
    if output.exists() and not output.is_dir():raise ValueError('重解码输出必须是独立目录')
    destination=output/'decodes'/(key+'.json');hit=False
    try:
        cached=json.loads(destination.read_text(encoding='utf8'))
        if cached['identity']!=decode_identity or w._key(cached['result'])!=cached['result_sha256']:
            raise ValueError('解码缓存身份或内容损坏')
        result=cached['result'];hit=True;log('[cubase_local] 复用已验证观测的分组候选缓存')
    except (OSError,ValueError,TypeError,KeyError):
        log('[cubase_local] 仅重解码既有三类观测；不访问音频或运行网络')
        result=w.decode_observations(values,provenance=verified_provenance,**decoder)
        w._atomic_json(destination,dict(identity=decode_identity,result=result,result_sha256=w._key(result)))
    result=json.loads(w._json(result))
    result.update(provider='cubase_local_reconstruction',source=base.get('source'),source_path=base.get('source_path'),
        source_sha256=base['source_sha256'],audio_duration=duration,clock_origin_seconds=origin,
        first_full_bar_seconds=None,effective_map_published=False,source_events_modified=False,
        observations_path=str(path),observations_sha256=digest,
        cache=dict(observation_key=observation_key,decode_key=key,observations_hit=True,decode_hit=hit),
        timings=dict(features_and_model_load=0.,network=0.,decode=time.perf_counter()-started,total=time.perf_counter()-started),
        cached_observation_only=True)
    result['absolute_beat_seconds']=[s+origin for s in result['beat_seconds']]
    result['absolute_downbeat_seconds']=[s+origin for s in result['downbeat_seconds']]
    result['beats']=list(result['beat_seconds']);result['downbeats']=list(result['downbeat_seconds'])
    return result
