"""Explicit ASIO control-panel lifecycle. No audio stream or unsolicited dialog.
The GUI polls this state machine; driver calls run on NativeAsioControl's thread.
"""
from dataclasses import replace

def busy(bridge):
    return getattr(bridge, '_asio_panel', None) is not None

def status(bridge):
    return dict(getattr(bridge, '_asio_panel_status', {'phase': 'idle'}), busy=busy(bridge) or bool(getattr(bridge, '_vst_panel_transition', None)))

def open_panel(bridge, driver):
    if bridge._output_shutdown:
        return {'ok': False, 'error': '应用正在关闭。'}
    if busy(bridge):
        return {'ok': False, 'error': '驱动面板操作尚未结束。'}
    listing=bridge._dm().listing()
    row=next((r for r in listing['catalog']['asio'] if r.get('name')==driver),None)
    if not row or row.get('state')!='registered':
        return {'ok': False, 'error': '请选择可用的 ASIO 驱动。'}
    if not listing['catalog'].get('asio_runtime',{}).get('available'):
        return {'ok': False, 'error': 'ASIO 运行库不可用。'}
    hands=bridge._output_retiring+([bridge._output] if bridge._output else [])
    if any(h.blocked for h in hands):
        return {'ok': False, 'error': '旧音频输出尚未确认释放，暂不能打开驱动面板。'}
    h=bridge._output
    if h:
        s=h.owner.snapshot() if h.owner else None
        position=getattr(s,'pending_target',None)
        if position is None:position=getattr(s,'position',h.intent.position)
        bridge._output_desired=replace(h.intent,playing=False,position=position)
        h.close();bridge._output_retiring.append(h);bridge._output=None
    else:bridge._output_desired=replace(bridge._output_desired,playing=False)
    bridge._deferred_delta=None
    bridge._asio_panel={'driver':driver,'stage':'releasing','control':None,'error':None}
    bridge._asio_panel_status={'phase':'releasing','driver':driver,'message':'正在释放当前输出…'}
    pause_memory=getattr(bridge, '_pause_memory_for_asio_panel', None)
    if pause_memory is not None:pause_memory()
    bridge._player_tick()
    return {'ok':True,'pending':True,'control_panel':status(bridge)}

def _factory():
    from .native_assets import native_dependencies
    from .native_host import NativeAsioControl
    return NativeAsioControl(native_dependencies(need_portaudio=True)['portaudio_dll'])

def poll_panel(bridge):
    panel=getattr(bridge,'_asio_panel',None)
    if panel is None:return
    try:
        control=panel['control']
        if panel['stage']=='releasing':
            if bridge._output_retiring or bridge._output_owners:return
            if bridge._output_shutdown:
                bridge._asio_panel=None;return
            control=panel['control']=getattr(bridge,'_asio_control_factory',_factory)()
            panel['stage']='initializing'
        if bridge._output_shutdown and control:
            control.close();panel['stage']='closing'
        if panel['stage']=='initializing':
            s=control.poll()
            if s.get('error') or s.get('released'):raise RuntimeError(s.get('message') or '驱动面板初始化失败')
            if s.get('state')!=1:return
            if not control.show_panel(panel['driver']):raise RuntimeError('驱动面板请求未被接受')
            panel['stage']='open';bridge._asio_panel_status.update(phase='open',message='驱动面板已打开；关闭后恢复为暂停状态。')
        elif panel['stage']=='open':
            s=control.poll()
            if s.get('control_state') not in (2,3):return
            if s.get('control_state')==3:panel['error']=s.get('message') or '驱动面板操作失败'
            else:
                info=control.buffer_info()
                bridge._asio_panel_status['buffer_info']=info
            control.close();panel['stage']='closing'
        if panel['stage']=='closing' and control.finish_close():
            bridge._asio_panel=None
            bridge._asio_panel_status.update(phase='error' if panel['error'] else 'closed',message=panel['error'] or '驱动面板已关闭；播放保持暂停。')
            bridge._device_resume_position=bridge._output_desired.position
            if not bridge._output_shutdown and bridge._sources:
                bridge._deferred_delta=dict(vars(replace(bridge._output_desired,playing=False)))
            bridge._player_tick()
    except Exception as exc:
        panel['error']=str(exc)
        bridge._asio_panel_status.update(phase='error',message=str(exc))
        if panel.get('control'):
            # Do not free or replace a driver whose release is unconfirmed.
            try:panel['control'].close();panel['stage']='closing'
            except Exception as close_error:
                panel['stage']='blocked';bridge._asio_panel_status.update(phase='error',message='驱动释放未确认：'+str(close_error))
        else:
            bridge._asio_panel=None
            bridge._device_resume_position=bridge._output_desired.position
            if not bridge._output_shutdown and bridge._sources:
                bridge._deferred_delta=dict(vars(replace(bridge._output_desired,playing=False)))
