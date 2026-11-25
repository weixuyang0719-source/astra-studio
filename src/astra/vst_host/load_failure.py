"""Small, printable startup receipts; never serialize vendor state or raw transcripts."""
import locale
import json
import os
from pathlib import Path
import re
import time

STAGES={'launch':'启动隔离进程','host':'加载宿主','plugin':'初始化插件','handshake':'建立控制连接',
        'effect':'准备效果器音频','probe':'验证效果器音频'}

_ACTIONS={
    'missing_plugin':('找不到插件文件','请刷新 VST3 列表并重新选择。'),
    'missing_host':('宿主组件缺失','请重新解压完整的测试包。'),
    'host_binary':('宿主组件无法载入','请核对宿主及其依赖的 Windows 架构，再重新解压测试包。'),
    'class':('插件类与目录不匹配','请刷新 VST3 列表，然后重新选择插件。'),
    'role':('插件音频接口与当前用途不兼容','请从对应的乐器或效果器列表选择。'),
    'format':('插件不支持当前音频格式','请检查插件是否提供单声道或立体声、32 位浮点处理。'),
    'setup':('插件拒绝音频配置','请检查当前采样率和设备块长，再重试。'),
    'timeout':('隔离进程响应超时','结束该实例后可重新选择；若再次超时，可在厂商独立程序中检查首次启动。'),
    'exit':('插件进程提前退出','可重新选择；若在相同阶段再次退出，可用厂商独立程序核对安装。'),
    'protocol':('宿主通信异常','请重新选择；若反复出现，请附上此诊断反馈。'),
    'state':('插件未接受保存的状态','可在新插槽以默认状态试挂。'),
    'license':('厂商明确报告授权不可用','请在厂商授权管理器确认后重试。'),
    'content':('厂商明确报告音库缺失','请在厂商程序检查内容路径后重试。'),
    'unknown':('插件准备失败，原因待确认','可重新选择；若再次失败，请附上此诊断反馈。'),
}
_NATIVE={
    'Requested VST3 class was not found':'class',
    'Plugin buses do not match the requested role':'role',
    'Effect has no compatible mono or stereo main bus arrangement':'format',
    'First output must be mono or stereo in this host version':'format',
    'Plugin does not support float32 audio processing':'format',
    'Invalid host configuration':'setup','Invalid audio configuration':'setup',
    'Plugin setupProcessing failed':'setup',
    'Component rejected state':'state','Controller rejected state':'state',
    'Invalid state envelope or plugin class mismatch':'state','Invalid state lengths':'state',
}
# Extract only fixed phrases; a text log can contain preset names, opaque
# blobs, keys and paths even on lines containing the word "error".
_SIGNALS=(
    ('license',r'\b(?:license (?:not found|expired|missing)|activation required|no valid licen[cs]e)\b|许可证已过期','授权不可用'),
    ('content',r'\b(?:sample library missing|samples? not found|content library not found)\b|找不到音色库','音库缺失'),
    ('',r'\b(?:access denied|permission denied)\b|拒绝访问','访问被拒绝'),
    ('',r'\b(?:module not found|module could not be found|library not found)\b|找不到指定的模块','模块未找到'),
    ('',r'\b(?:out of memory|allocation failed)\b|内存不足','内存分配失败'),
)

def _signals(value):
    return [(code,label) for code,pattern,label in _SIGNALS if re.search(pattern,value,re.I)]

def receipt(plugin,role,stage,exc,exit_code=None,vendor=b'',category=None):
    message=str(exc)
    if category is None:
        category=_NATIVE.get(message)
    if not category:
        if message.startswith('Plugin process exited'):category='exit'
        elif message.startswith(('Plugin protocol ','Malformed private plugin','Invalid private plugin','Oversized plugin','Plugin response pipe failed')):category='protocol'
        elif any(code for code,_ in _signals(message)):category=next(code for code,_ in _signals(message) if code)
        else:category='unknown'
    title,action=_ACTIONS[category]
    name=Path(plugin).name
    name=''.join(c for c in name if c.isprintable())[:80]
    brief=f'{"效果器" if role=="effect" else "乐器"} {name} · {STAGES[stage]}：{title}。{action}'
    if exit_code is not None:
        brief+=f' 退出码 0x{exit_code&0xffffffff:08X}。'
    tail=bytes(vendor[-8192:])
    labels=[]
    if tail:
        try:text=tail.decode('utf8')
        except UnicodeError:text=tail.decode(locale.getpreferredencoding(False),errors='replace')
        labels=[label for _,label in _signals(text)]
        brief+=f' 厂商日志：{"、".join(labels) if labels else "有输出，未识别到明确错误"}。'
    return dict(category=category,stage=stage,exit_code=exit_code,module=name,role=role,
                vendor_summary=labels,diagnostic_bytes=len(tail),message=brief,short=title+'。')


def record(details):
    """Best-effort across worker processes. Only allowlisted fields reach disk."""
    try:
        import msvcrt
        from astra.release_paths import data_root
        directory=data_root()/'.cache'/'vst-diagnostics'
        directory.mkdir(parents=True,exist_ok=True)
        path=directory/'load.jsonl'
        line=(json.dumps(dict(time_utc=time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),**details),
                         ensure_ascii=False,separators=(',',':'))+'\n').encode('utf8')
        with (directory/'load.lock').open('a+b') as lock:
            for retry in range(12):
                try:
                    lock.seek(0)
                    msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
                    break
                except OSError:
                    if retry==11:return
                    time.sleep(.008)
            try:
                if path.exists() and path.stat().st_size+len(line)>128*1024:
                    os.replace(path,path.with_suffix('.1.jsonl'))
                with path.open('ab') as stream:
                    stream.write(line)
            finally:
                lock.seek(0)
                msvcrt.locking(lock.fileno(),msvcrt.LK_UNLCK,1)
    except (ImportError,OSError,ValueError):
        pass


def native_activity(session):
    """Actual CPU time (100 ns) and I/O bytes of our isolated Windows job."""
    if os.name!='nt':return None
    import ctypes as C
    with session._job_lock:
        job=session._job
        if not job or not job.handle:return None
        query=job.api.QueryInformationJobObject
        query.argtypes,query.restype=[C.c_void_p,C.c_int,C.c_void_p,C.c_uint32,C.c_void_p],C.c_int
        # JOBOBJECT_BASIC_AND_IO_ACCOUNTING_INFORMATION. First four LARGE_INTEGER,
        # then four DWORD counters, then IO_COUNTERS (six ULONGLONGs).
        class Counters(C.Structure):
            _fields_=[('times',C.c_int64*4),('counts',C.c_uint32*4),('io',C.c_uint64*6)]
        totals=Counters()
        if not query(job.handle,8,C.byref(totals),C.sizeof(totals),None):return None
        return sum(totals.times[:2]),sum(totals.io[3:5])
