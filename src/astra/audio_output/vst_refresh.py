"""Explicit third-party refresh. Only called from the existing setup worker."""
from pathlib import Path
import json
import os
import re
import stat
import subprocess
import tempfile
from xml.etree import ElementTree
from astra.vst_host.catalog import Catalog, SCAN_RESULT_PROTOCOL, fingerprint, scan_one
from . import vst_metadata as meta


def _snapshot_path():
    from astra.release_paths import data_root
    return data_root() / '.cache' / 'vst-inventory-snapshot.json'


def _host_metadata_path():
    roaming = os.environ.get('APPDATA')
    if not roaming:
        return None
    return Path(roaming) / 'Steinberg/Cubase 16_64/Cubase Pro VST3 Cache/vst3plugins.xml'


def _file_sig(path):
    if path is None:
        return None
    try:
        st = Path(path).lstat()
        return [st.st_size, st.st_mtime_ns]
    except OSError:
        return None


def _dir_sigs(roots):
    sigs = []
    for root in roots:
        root = Path(root).absolute()
        try:
            if str(root).startswith(chr(92) * 2):
                continue
            for parent in (*reversed(root.parents), root):
                meta._plain(parent)
        except (OSError, ValueError):
            continue
        stack = [(root, 0)]
        while stack:
            folder, depth = stack.pop()
            try:
                st = meta._plain(folder)
            except (OSError, ValueError):
                continue
            sigs.append([str(folder), st.st_mtime_ns])
            try:
                with os.scandir(folder) as entries:
                    for entry in entries:
                        p = Path(entry.path)
                        try:
                            mode = meta._plain(p).st_mode
                        except (OSError, ValueError):
                            continue
                        if p.suffix.lower() != '.vst3' and stat.S_ISDIR(mode) and depth < meta._MAX_DEPTH:
                            stack.append((p, depth + 1))
            except OSError:
                continue
    return sigs


def _load_inventory_snapshot(roots):
    path = _snapshot_path()
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get('version') != 1:
        return None
    if data.get('roots') != [str(Path(r).absolute()) for r in roots]:
        return None
    if data.get('catalog_stat') != _file_sig(cache_path()):
        return None
    if data.get('host_cache_stat') != _file_sig(_host_metadata_path()):
        return None
    dirs = data.get('dirs')
    modules = data.get('modules')
    rows = data.get('rows')
    coverage = data.get('coverage')
    if not (isinstance(dirs, list) and isinstance(modules, list) and isinstance(rows, list) and isinstance(coverage, dict)):
        return None
    try:
        for dpath, mtime_ns in dirs:
            st = meta._plain(Path(dpath))
            if st.st_mtime_ns != mtime_ns:
                return None
        for mpath, mtime_ns in modules:
            st = meta._plain(Path(mpath))
            if st.st_mtime_ns != mtime_ns:
                return None
        accepted = tuple(meta.MetadataClass(*r) for r in rows)
    except (OSError, ValueError, TypeError):
        return None
    return accepted, dict(coverage)


def _save_inventory_snapshot(roots, modules, accepted, coverage):
    path = _snapshot_path()
    try:
        mod_sigs = []
        for m in modules:
            try:
                mod_sigs.append([str(m), meta._plain(m).st_mtime_ns])
            except (OSError, ValueError):
                continue
        payload = dict(
            version=1,
            roots=[str(Path(r).absolute()) for r in roots],
            dirs=_dir_sigs(roots),
            modules=mod_sigs,
            catalog_stat=_file_sig(cache_path()),
            host_cache_stat=_file_sig(_host_metadata_path()),
            rows=[[r.module, r.cid, r.name, r.vendor, r.category, r.kind] for r in accepted],
            coverage=dict(coverage),
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix='vst-inv-', suffix='.tmp', dir=path.parent)
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump(payload, stream, ensure_ascii=False)
            os.replace(tmp, path)
        finally:
            tmp.unlink(missing_ok=True)
    except OSError:
        pass


def excluded(path):
    # Applies to roots, bundle paths and resolved binaries, including explicit paths.
    return any(re.match(r'^(cubase|nuendo)(?:$|[\s._-]|\d)', part, re.I)
               for part in Path(path).parts)


def cache_path():
    from astra.release_paths import data_root
    return data_root()/'.cache'/'vst-third-party-catalog.json'


def _catalog(path):
    try:
        return Catalog(path)
    except (OSError, ValueError, TypeError):
        obj=Catalog.__new__(Catalog);obj.path=Path(path);obj.records={};return obj


def _rows(record):
    result=[]
    for row in record.get('classes',[]):
        if not isinstance(row,dict):continue
        cid=row.get('id',''); name=meta._label(row.get('name'))
        if not isinstance(cid,str):continue
        cid=cid.lower()
        category=row.get('category','')
        if not isinstance(category,str):continue
        kinds={x.strip().casefold() for x in category.split('|')} & {'instrument','fx'}
        if not re.fullmatch('[0-9a-f]{32}',cid) or not name:continue
        for kind in sorted(kinds):
            result.append(meta.MetadataClass(record['path'],cid,name,meta._label(row.get('vendor')),category,kind))
    return result


def _background_scan(path, library, timeout):
    def run(*args, **kwargs):
        kwargs['creationflags']=kwargs.get('creationflags',0)|getattr(subprocess,'BELOW_NORMAL_PRIORITY_CLASS',0)
        return subprocess.run(*args,**kwargs)
    return scan_one(path,library,timeout=timeout,runner=run)


def host_metadata(file=None):
    """Optional read-only cache. Never use its paths as discovery roots."""
    if file is None:
        roaming = os.environ.get('APPDATA')
        if not roaming:
            return {}
        file = Path(roaming)/'Steinberg/Cubase 16_64/Cubase Pro VST3 Cache/vst3plugins.xml'
    try:
        with Path(file).open('rb') as stream:
            raw = stream.read(16*1024*1024+1)
        if len(raw)>16*1024*1024 or b'<!DOCTYPE' in raw.upper():
            return {}
        plugins = ElementTree.fromstring(raw)
    except (OSError, ValueError, ElementTree.ParseError):
        return {}
    result = {}
    for plugin in plugins.findall('plugin')[:meta._MAX_MODULES]:
        path, timestamp = plugin.findtext('path'), plugin.findtext('timestamps/executable')
        if not path or not timestamp or not re.fullmatch(r'\d{1,19}',timestamp):
            continue
        classes = [dict(id=c.findtext('cid'), name=c.findtext('name'),
                        vendor=c.findtext('vendor') or plugin.findtext('vendor'),
                        category=c.findtext('subCategories'))
                   for c in plugin.findall('class')[:meta._MAX_CLASSES]
                   if c.findtext('category')=='Audio Module Class']
        result[str(Path(path)).casefold()] = int(timestamp), classes
    return result


def inventory(roots=None, *, cache=None, library=None, refresh=False, scanner=None, progress=None, retry_failed=True, host_cache=None):
    """Static metadata first; cached worker data only for unresolved current modules.

    Explicit refresh retries failed modules and scans new/changed unresolved modules.
    A removed or changed DLL never contributes stale classes to the snapshot.
    """
    scanner=scanner or _background_scan
    default_scope = roots is None and cache is None and host_cache is None
    resolved_roots = meta.standard_roots() if roots is None else roots
    if default_scope and not refresh:
        cached = _load_inventory_snapshot(resolved_roots)
        if cached is not None:
            return cached
    modules,limited=meta._modules(resolved_roots)
    modules=list(modules)
    external = host_metadata() if host_cache is None else host_cache
    cat=_catalog(cache if cache is not None else cache_path())
    rows=[]; failed=unresolved=incompatible=blocked=host_cached=0; live=set(); failures=[]
    for index,module in enumerate(modules):
        if progress:progress(dict(completed=index,total=len(modules),module=module.name))
        if excluded(module) or excluded(module.resolve()):blocked+=1;continue
        try:
            if meta._validated_architecture(module)!='x64':incompatible+=1;continue
            stamp=fingerprint(module)
            if excluded(stamp['binary']):blocked+=1;continue
        except (OSError,ValueError):incompatible+=1;continue
        key=str(module.resolve()).casefold();live.add(key)
        try: static=meta.manifest_classes(module)
        except (OSError,ValueError,RecursionError):static=()
        if static:rows.extend(static);continue
        old=cat.records.get(key,{})
        valid=old.get('fingerprint')==stamp
        legacy_failure = old.get('status') == 'failed' and old.get('scan_result_protocol') != SCAN_RESULT_PROTOCOL
        # Timestamp-matched metadata from a local host can fill a virgin catalog
        # immediately. A real, current Astra failure always takes precedence.
        # Manual refresh still probes all unresolved modules via our own factory.
        if (not valid or legacy_failure) and key in external and not (refresh and retry_failed):
            stamp_ticks, classes = external[key]
            if stamp_ticks == stamp['mtime_ns']//100+116444736000000000:
                rows.extend(_rows(dict(path=str(module), classes=classes)))
                host_cached+=1
                continue
        if refresh and (not valid or legacy_failure or (retry_failed and old.get('status')!='ready')):
            old=scanner(module,library,timeout=20)
            # Do not accept stale scan results when files changed during the worker.
            try:valid=old.get('fingerprint')==fingerprint(module)
            except (OSError,ValueError):valid=False
            cat.records[key]=old;cat.save()
        if valid and old.get('status')=='ready':rows.extend(_rows(dict(old,path=str(module))))
        else:
            unresolved+=1
            if valid and old.get('status')=='failed':
                failed+=1;failures.append(dict(module=module.name,error=old.get('error','Scan failed')))
    if refresh:
        cat.records={k:v for k,v in cat.records.items() if k in live};cat.save()
    accepted, ambiguous = meta.unambiguous_classes(rows)
    coverage = dict(modules=len(modules),unresolved=unresolved,failed=failed,incompatible=incompatible,excluded=blocked,host_cached=host_cached,ambiguous=ambiguous,limited=limited,failures=failures,completed=len(modules),total=len(modules))
    if default_scope:
        _save_inventory_snapshot(resolved_roots, modules, accepted, coverage)
    return accepted, coverage
