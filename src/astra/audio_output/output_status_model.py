"""Small user-facing output state, separate from diagnostic exception text."""
_UNREAD=object()

def output_status(requested,h,failure=None,device_notice=False,available=True,*,sample=_UNREAD):
 intent=getattr(h,'intent',None);owner=getattr(h,'owner',None)
 s=(owner.snapshot() if owner else None) if sample is _UNREAD else sample
 blocked=getattr(h,'blocked',None);audition=getattr(intent,'audition','audio')
 phase='idle';label='尚未开始播放';actual=None
 if not available:phase='unavailable';label='没有可用的音频输出设备'
 elif blocked:phase='blocked';label='播放已暂停，等待旧音频设备释放' if 'release unconfirmed' in blocked.lower() else '播放已暂停，请检查音频输出'
 elif s is not None and getattr(s,'error',None):phase='error';label='音频输出异常，播放已暂停'
 elif getattr(h,'retiring',False):phase='switching';label='正在切换音频输出'
 elif s is not None:
  if s.closed:phase='stopped';label='播放已停止'
  elif getattr(s,'device_parked',False):pass
  elif not s.ready or getattr(s,'preparing',False):phase='preparing';label='正在准备播放'
  else:
   actual=h.owner_mode;phase='playing' if s.playing else 'paused';label={'raw':'RAW 输出','native-shared':'原生共享输出（设备不支持 RAW）'}.get(actual,'兼容输出')
   if device_notice:label='音频设备已改变，点击播放继续'
   elif audition=='midi':label='MIDI 合成输出'
   elif audition=='joint':label='音频与 MIDI 联合输出'
   elif failure and actual=='ordinary':label='已切换为兼容输出'
   elif requested and actual=='ordinary' and intent.rate!=1:label='变速播放使用兼容输出'
 display_mode=('midi' if audition=='midi' else 'joint' if audition=='joint' else actual) if actual else None
 result=dict(audition=audition,displayMode=display_mode,requested='raw' if requested else 'ordinary',actual=actual,phase=phase,label=label,diagnostic=blocked or getattr(s,'error',None) or (failure if audition=='audio' else None),requiresResume=bool(device_notice and phase!='playing'))

 endpoint=getattr(s,'pcm_endpoint',None)
 if endpoint:result['pcmEndpoint']=dict(endpoint)
 return result
