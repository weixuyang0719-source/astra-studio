"""Bind a private plugin subprocess to the calling installation, not cwd/PYTHONPATH."""
import sys
from pathlib import Path

def child_command(module, *arguments):
    if module not in ('astra.vst_host.worker','astra.vst_host.session_worker','astra.vst_host.session_entry'):
        raise ValueError('unsupported plugin child module')
    root=Path(__file__).resolve().parents[2]
    expected=root.joinpath(*module.split('.')).with_suffix('.py')
    code=("import sys,runpy,importlib.util,pathlib;sys.path.insert(0,"+repr(str(root))+");"
          "spec=importlib.util.find_spec("+repr(module)+");"
          "assert spec is not None and pathlib.Path(spec.origin).resolve()==pathlib.Path("+repr(str(expected))+"),'Plugin worker source mismatch';"
          "runpy.run_module("+repr(module)+",run_name='__main__',alter_sys=True)")
    return [sys.executable,'-I','-B','-c',code,*map(str,arguments)]
