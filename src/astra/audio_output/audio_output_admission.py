"""Startup policy only. Caller must install the verified window close coordinator.
No imports of candidate/Qt/device APIs until admit_runtime is explicitly called.
"""
from dataclasses import dataclass
from .audio_trial_preflight import assess_trial

@dataclass(frozen=True)
class Admission:
 use_candidate:bool
 prefer_raw:bool
 reason:str
 experimental_joint:bool=False


def choose_admission(report,*,lifecycle_ready=False,prefer_raw=True):
 if not lifecycle_ready:return Admission(False,False,'verified-close-lifecycle-required')
 if not report.get('can_import_candidate'):return Admission(False,False,'candidate-dependencies-missing')
 if not prefer_raw:return Admission(True,False,'explicit-compatibility')
 if not report.get('can_try_raw'):return Admission(True,False,'raw-dependency-or-platform-unverified')
 return Admission(True,True,'raw-preferred-device-negotiation-pending')


def admit_runtime(parent=None,*,lifecycle_ready=False,prefer_raw=True,assessment=None,candidate_factory=None,legacy_factory=None):
 report=(assessment or assess_trial)();admission=choose_admission(report,lifecycle_ready=lifecycle_ready,prefer_raw=prefer_raw)
 if admission.use_candidate:
  if candidate_factory is None:
   from .unified_studio_bridge import UnifiedStudioBridge
   candidate_factory=UnifiedStudioBridge
  # Device unsupported/failed handling stays in Handoff; never catch arbitrary
  # construction errors and open another output with unproven release.
  bridge=candidate_factory(parent,raw=admission.prefer_raw,experimental_joint=False,allow_output_settings=bool(report.get('can_try_raw')))
 else:
  if legacy_factory is None:
   from astra.studio_bridge import StudioBridge
   legacy_factory=StudioBridge
  bridge=legacy_factory(parent)
 return bridge,admission,report
