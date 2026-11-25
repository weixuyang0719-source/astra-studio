"""Explicit local constraints on the active model's original probabilities.

This reuses counted/phase DP, not another acoustic model or a claim of Cubase
equivalence. Manual-count and quality-review purposes have explicit provenance;
quality review returns an unrefined hypothesis and never automatically adopts.
"""
from .release_paths import runtime_env_root
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def decode_evidence(evidence, constraints, *, python=None, pulse_strategy='coarse_counted', purpose='manual_count'):
    import numpy as np
    if purpose not in ('manual_count','quality_review'):
        raise ValueError('未知的局部拍点解码用途。')
    if purpose=='quality_review' and pulse_strategy!='dense_counted':
        raise ValueError('逐帧质量复核必须明确使用 dense_counted 策略。')
    if pulse_strategy not in ('coarse_counted', 'dense_counted'):
        raise ValueError('未知的局部拍点解码策略。')
    if pulse_strategy == 'dense_counted' and (
            constraints.get('kind') != 'pulse_times' or
            evidence.get('probability_source') != 'auf_takt_three_class'):
        raise ValueError('逐帧拍点只用于已绑定本机观测的人工拍数修正。')
    root=runtime_env_root()
    python=Path(python or root/'.venv-rhythm'/'Scripts'/'python.exe')
    if not python.is_file():
        raise FileNotFoundError('局部约束解码环境未安装；仍可手动划拍，不会切换声学模型。')
    identity=evidence['activation_identity']
    env=os.environ.copy()
    env['PYTHONPATH']=os.pathsep.join(filter(None,(str(Path(__file__).resolve().parents[1]),env.get('PYTHONPATH'))))
    env['PYTHONIOENCODING']='utf-8'
    with tempfile.TemporaryDirectory(prefix='astra-manual-pulse-') as folder:
        folder=Path(folder);arrays=folder/'probabilities.npz'
        values=dict(beat=np.asarray(evidence['beat_probability'],dtype=np.float64),
                    downbeat=np.asarray(evidence['downbeat_probability'],dtype=np.float64))
        if pulse_strategy=='dense_counted':
            values['observations']=np.asarray(evidence.get('observations'),dtype=np.float64)
        np.savez(arrays,**values)
        request={'activation_identity':identity,'activation_sha256':evidence['activation_sha256'],
                 'probability_source':evidence['probability_source'],
                 'fps':evidence['fps'],
                 'pulse_strategy':pulse_strategy,
                 'purpose':purpose,
                 'array_sha256':hashlib.sha256(arrays.read_bytes()).hexdigest(),
                 'constraints':constraints}
        (folder/'request.json').write_text(json.dumps(request,allow_nan=False),encoding='utf-8')
        process=subprocess.run([str(python),'-u','-m','astra.manual_pulse_decode',str(folder)],
            env=env,capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=120,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        if process.returncode:
            raise RuntimeError('局部约束解码失败：'+(process.stderr or process.stdout)[-1200:])
        result=json.loads((folder/'result.json').read_text(encoding='utf-8'))
    if (result.get('activation_identity')!=identity or
            result.get('activation_sha256')!=evidence['activation_sha256']):
        raise ValueError('局部解码的证据身份发生变化')
    if hashlib.sha256(Path(evidence['activation_path']).read_bytes()).hexdigest()!=evidence['activation_sha256']:
        raise ValueError('解码期间原拍点观测已变化，请重新预览')
    return result


def _worker(folder):
    import numpy as np
    folder=Path(folder);request=json.loads((folder/'request.json').read_text(encoding='utf-8'))
    path=folder/'probabilities.npz'
    if hashlib.sha256(path.read_bytes()).hexdigest()!=request['array_sha256']:
        raise ValueError('局部概率传递校验失败')
    with np.load(path,allow_pickle=False) as arrays:
        beat=arrays['beat'];down=arrays['downbeat']
        observations=arrays['observations'] if 'observations' in arrays else None
    if (beat.ndim!=1 or down.shape!=beat.shape or not len(beat) or
        not np.isfinite(beat).all() or not np.isfinite(down).all() or
        np.any(beat<0) or np.any(beat>1) or np.any(down<0) or np.any(down>1)):
        raise ValueError('无效的原始拍点概率')
    constraints=request['constraints'];kind=constraints.get('kind')
    strategy=request.get('pulse_strategy','coarse_counted')
    purpose=request.get('purpose','manual_count')
    if purpose not in ('manual_count','quality_review') or (purpose=='quality_review' and strategy!='dense_counted'):
        raise ValueError('无效的局部拍点解码用途或策略')
    if strategy not in ('coarse_counted','dense_counted'):
        raise ValueError('未知的局部拍点解码策略')
    if strategy=='dense_counted':
        if kind!='pulse_times':
            raise ValueError('逐帧拍点仅用于明确的人工拍数约束')
        if (observations is None or observations.shape!=(len(beat),3) or
                not np.isfinite(observations).all() or
                np.any(observations<0) or np.any(observations>1) or
                not np.allclose(observations.sum(axis=1),1.,atol=1e-5) or
                not np.allclose(1.-observations[:,2],beat,atol=1e-7) or
                not np.allclose(observations[:,1],down,atol=1e-7)):
            raise ValueError('逐帧拍点缺少一致的原始三分类观测，请重新预览')
    if kind=='pulse_times' and strategy=='dense_counted':
        if request['probability_source']!='auf_takt_three_class':
            raise ValueError('逐帧拍点要求本机原始观测')
        from .counted_pulse_decode import decode_pulse_times
        decoder=decode_pulse_times
    elif kind=='pulse_times':
        from .constrained_pulse_decode import decode_pulse_times
        decoder=decode_pulse_times
    elif kind=='bar_phase':
        from .rhythm_worker import _constrained_bar_decode
        decoder=_constrained_bar_decode
    else:raise ValueError('本入口只接受明确的人工局部拍数或分组约束')
    derived_identity={'source_identity':request['activation_identity'],'fps':request['fps'],
                      'role':'manual_constraint_probability_adapter'}
    result=decoder(beat,down,derived_identity,request['activation_sha256'],constraints)
    if strategy=='dense_counted' and purpose=='manual_count':
        # Reuse the existing <=50ms whole-range refinement. This does not run
        # or bypass the automatic acceptance gate; the manual caller previews
        # and explicitly adopts a proposal with its unmodified weak evidence.
        from .local_pulse_review import refine
        evidence=dict(beat_probability=beat,downbeat_probability=down,observations=observations,
                      fps=request['fps'],probability_source=request['probability_source'])
        raw=list(result['beat_seconds'])
        refined=refine(evidence,raw,constraints)
        result['raw_beat_seconds']=raw
        result['beat_seconds']=refined['beat_seconds']
        result['acoustic_refinement']=refined
        result['log_likelihood_scope']='pre_refinement_dp_path_only'
        frames=np.floor(np.asarray(result['beat_seconds'])*request['fps']+.5).astype(int)
        result['beat_probability']=beat[frames].tolist()
        result['downbeat_probability']=down[frames].tolist()
        diagnostics=result['constraint_diagnostics']
        diagnostics['raw_decoder_frame_indices']=diagnostics['observation_frame_indices']
        diagnostics['observation_frame_indices']=frames.tolist()
        diagnostics['weak_pulse_indices']=[i for i,p in enumerate(result['beat_probability']) if p<.2]
        result['warnings']=result['warnings'][:1]
        if diagnostics['weak_pulse_indices']:
            result['warnings'].append(f"{len(diagnostics['weak_pulse_indices'])} 个拍点声学证据较弱，可能由拍数约束补足，需人工核对。")
    if purpose=='quality_review':
        # No refinement here: propose owns the one <=50ms pass and original
        # quality gate. The worker never claims this inferred count is approved.
        result['constraint_diagnostics'].update(interval_count_confirmed_by_user=False,
            count_basis='current_quarter_clock_hypothesis')
        result['warnings'][0]='按当前图的拍数假设逐帧寻找局部候选；拍数未经人工确认，尚未通过质量复核。'
        result['warnings'].append('仅复用当前拍点模型的概率；不自动采用，不改变当前拍数。')
    else:
        result.setdefault('warnings',[]).append('仅按人工约束复用当前拍点模型的概率；这是局部辅助解码，不代表自动识别已确认。')
    result['decode_purpose']=purpose
    result['decoder_input_identity']=derived_identity
    result['activation_identity']=request['activation_identity']
    result['probability_source']=request['probability_source']
    result['manual_constraint_decoder']=('dense_boundary_counted_hmm' if strategy=='dense_counted' else 'madmom_constrained_dp')
    (folder/'result.json').write_text(json.dumps(result,ensure_ascii=False,allow_nan=False),encoding='utf-8')


if __name__=='__main__':_worker(sys.argv[1])
