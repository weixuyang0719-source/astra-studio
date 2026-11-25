from ..release_paths import data_root
from pathlib import Path
from .audio_trial_preflight import assess_trial

def main():
 report=assess_trial()
 if not report['can_import_candidate']:
  print('Audio candidate dependencies missing; using original compatibility entry:',report['missing'])
  from astra.studio import main as legacy
  return legacy()
 from . import trial_window
 trial_window.TRIAL_SESSION_ROOT=data_root()/'.cache'/'raw-output-session'
 from .audio_output_admission import admit_runtime
 def factory(parent):
  bridge,admission,_=admit_runtime(parent,lifecycle_ready=True,assessment=lambda:report)
  bridge._audio_startup_admission=admission
  return bridge
 trial_window.StudioBridge=factory
 return trial_window.main()
if __name__=='__main__':raise SystemExit(main())
