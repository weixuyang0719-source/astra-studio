"""Read-only admission check. Does not import Qt, SoundCard or acquire devices."""
import sys,importlib.util,importlib.metadata

def assess_trial(platform=None,find=None,version=None):
 platform=platform or sys.platform;find=find or importlib.util.find_spec;version=version or importlib.metadata.version
 required={'PySide6':'PySide6','numpy':'numpy','soundfile':'soundfile','scipy':'scipy','mido':'mido'}
 optional={'soundcard':'SoundCard','cffi':'cffi'};versions={};missing=[]
 for module,distribution in {**required,**optional}.items():
  try:
   if find(module) is None:missing.append(module);continue
   versions[distribution]=version(distribution)
  except (ImportError,ValueError,importlib.metadata.PackageNotFoundError):missing.append(module)
 tested={'SoundCard':'0.4.6','cffi':'2.1.1','PySide6':'6.12.0'}
 mismatched={name:value for name,value in tested.items() if name in versions and versions[name]!=value}
 can_import=not any(name in missing for name in required)
 return dict(can_import_candidate=can_import,can_try_raw=can_import and platform=='win32' and not missing and not mismatched,missing=missing,versions=versions,unverified_versions=mismatched,hardware_support='not checked',production_changed=False)
