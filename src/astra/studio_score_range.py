"""Latest-request-only local score feedback, independent of the formal PDF."""
from PySide6.QtCore import Slot
import math
from pathlib import Path
from uuid import uuid4
from time import perf_counter
from .score_page_cache import ScorePageCache


class ScoreRangeBridge:
    def _score_range_cancel(self, token=None):
        request = getattr(self, '_score_range_request', None)
        matches = request and (token is None or token in (request['token'], request.get('_document_token')))
        if matches:
            event = request.get('_cancel_event')
            if event is not None: event.set()
            if request.get('notes_compare') or request.get('status') in ('running','queued','validating'):
                request.update(status='cancelled')
            if getattr(self, '_score_range_pending', None) is request:
                self._score_range_pending = None
        # A queued request may have replaced the active item. It must not
        # leave the old subprocess alive when the window closes.
        active = getattr(self, '_score_range_active_item', None)
        if active and (token is None or matches):
            event = active.get('_cancel_event')
            if event is not None: event.set()

    @staticmethod
    def _score_range_saved_revision(project):
        from . import editor_checkpoint, live_owner

        owner = live_owner.retained(project)
        if owner is not None:
            return owner.status()['persisted_revision']
        record = editor_checkpoint.read(project)
        return record.index['revision'] if record is not None else None

    def _score_range_current(self, request):
        if not self._project or str(Path(self._project['path']).resolve()) != request['project']:
            return False
        try:
            if request.get('native'):
                # Unlike the old PDF path, the native excerpt is a derivative
                # of an exact retained in-memory revision and need not save it.
                from . import live_owner
                owner = live_owner.retained(request['project'])
                revision = owner.status()['revision'] if owner is not None else self._score_range_saved_revision(request['project'])
                current = revision
            else:
                revision = self._score_range_saved_revision(request['project'])
                current = (self._project.get('editor') or {}).get('revision', revision)
            from .score_range_dependency import source_stamp
            stamp=source_stamp(request['project'],request.get('source_export'))
            source_changed=request.get('_source_stamp') is not None and request['_source_stamp']!=stamp
            if revision != current or revision != request['base_revision'] or source_changed:
                return False
            doc=request.get('result') or request.get('existing') or {}
            left_identity=doc.get('_left_identity') if request.get('notes_compare') else None
            if left_identity is not None:
                left_identity.check()
            return True
        except (OSError,ValueError,KeyError,TypeError):
            return False

    def _score_range_submit(self, request):
        from PySide6.QtCore import Qt
        from .studio_bridge import _InputInspectionSignal
        if not hasattr(self,'_score_range_signal'):
            self._score_range_signal = _InputInspectionSignal(self)
            self._score_range_signal.done.connect(self._score_range_done, Qt.ConnectionType.QueuedConnection)
        self._score_range_cancel()
        from threading import Event
        request['_cancel_event'] = Event()
        request['token'] = uuid4().hex
        if request.get('notes_compare'):
            request.setdefault('_document_token',request['token'])
        request['status'] = 'queued' if getattr(self,'_score_range_active',False) else 'running'
        self._score_range_request = request
        self._score_range_pending = request
        self._score_range_start_pending()
        response={'ok':True,'token':request['token'],'status':request['status']}
        if request.get('notes_compare'):
            response['document_token']=request['_document_token']
        return response

    def _score_range_preview_start(self, data, *, render_pdf=True):
        if not isinstance(data,dict):
            raise ValueError('局部预览请求必须是对象。')
        if not self._project:
            raise ValueError('请先打开钢琴工程。')
        if self.jobs.running or self._gpu_request:
            raise ValueError('请等待当前分析或正式导出结束再预览局部谱面。')
        start, end = data.get('start'), data.get('end')
        if any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) for v in (start,end)) or not 0 <= start < end:
            raise ValueError('请选择有效的左右定位点范围。')
        from . import native_score_runtime
        native = native_score_runtime.enabled()
        compare = data.get('notes_compare') is True and native
        if compare:
            if data.get('project') != self._project.get('path') or data.get('source_kind') not in ('marquee','locators'):
                raise ValueError('临时看谱工程或范围已变化')
        request = {'project':str(Path(self._project['path']).resolve()),
                   'base_revision':data.get('base_revision'), 'start':start,'end':end,
                   'render_pdf':render_pdf and not native, 'native':native,'notes_compare':compare}
        if compare:
            from .score_range_dependency import source_stamp
            request.update(raster_scale=native_score_runtime.range_raster_scale(data.get('raster_scale',1)),
                           _source_stamp=source_stamp(request['project'],None),
                           fast_only=data.get('fast_preview_only') is True)
        if not self._score_range_current(request):
            raise ValueError('工程版本已变化，请重新选择预览范围。')
        return self._score_range_submit(request)

    def _score_range_page_start(self, data):
        if not isinstance(data,dict):
            raise ValueError('局部翻页请求必须是对象。')
        request = getattr(self,'_score_range_request',None)
        if not request:
            raise ValueError('此局部谱面已失效，请重新预览。')
        compare=request.get('notes_compare')
        token=data.get('token')
        if compare:
            authorized=token in (request['token'],request['_document_token'])
            result=request.get('result') or request.get('existing')
        else:
            authorized=token==request['token'] and request['status']=='ready'
            result=request.get('result')
        if (not authorized or request['status'] in ('cancelled','stale') or result is None
                or not self._score_range_current(request)):
            raise ValueError('此局部谱面已失效，请重新预览。')
        index=data.get('index')
        if type(index) is not int or not 0 <= index < result['page']['pages']:
            raise ValueError('局部谱面页码超出范围。')
        item={k:request[k] for k in ('project','base_revision','start','end','native','notes_compare')} | {
            'existing':result,'page_index':index,'render_pdf':not request['native'],
            'source_export':request.get('source_export'),'_source_stamp':request.get('_source_stamp')}
        if compare:
            from .native_score_runtime import range_raster_scale
            item.update(_document_token=request['_document_token'],raster_scale=range_raster_scale(data.get('raster_scale',1)))
        return self._score_range_submit(item)

    def _score_range_start_pending(self):
        from PySide6.QtCore import QThreadPool
        from .studio_bridge import _InputInspectionTask
        if self._closed or getattr(self,'_score_range_active',False):
            return
        request=getattr(self,'_score_range_pending',None)
        if not request:
            return
        self._score_range_pending=None
        if not self._score_range_current(request):
            request.update(status='stale',error='工程已变化，请重新预览。')
            return
        request['status']='running'; self._score_range_active=True
        self._score_range_active_item=request
        def prepare_inner(item):
            from .score_range import preview_score_range
            started=perf_counter()
            if item.get('notes_compare'):
                from .score_compare_left import (capture_score_compare_left,prepare_score_compare_left,
                                                 replay_score_compare_left,CompareLeftPageCache)
                if not hasattr(self,'_compare_left_pages'):
                    self._compare_left_pages=CompareLeftPageCache()
                def still_current():
                    return self._score_range_request is item and not item['_cancel_event'].is_set() and self._score_range_current(item)
                options=dict(page_index=item.get('page_index',0),raster_scale=item['raster_scale'],
                             page_cache=self._compare_left_pages,still_current=still_current)
                if item.get('existing') is not None:
                    left=item['existing']
                    if (left['validated_revision']!=item['base_revision'] or left['requested_range']!=
                            dict(start=item['start'],end=item['end']) or left['_left_identity'].project!=item['project']):
                        raise ValueError('局部谱面的项目、版本或范围已变化。')
                    return replay_score_compare_left(left,**options)
                src=capture_score_compare_left(item['project'],item['base_revision'])
                return prepare_score_compare_left(src,item['start'],item['end'],
                    progress=lambda value:item.update(_preview_progress=value),
                    fast_only=item.get('fast_only',False),**options)
            if item.get('render_pdf') is False:
                return self._score_range_musicxml(item, preview_score_range)
            from .preview_worker import prepare_preview
            cache=self._score_range_cache(item['project'])
            result=dict(item['existing']) if 'existing' in item else preview_score_range(
                item['project'],item['base_revision'],item['start'],item['end'],
                context_bars=0,cache_dir=cache,exact_revision=True)
            item['source_export']=result.get('source_export')
            from .score_range_dependency import source_stamp
            item['_source_stamp']=result.get('dependency_source_stamp') or source_stamp(item['project'],item['source_export'])
            latest=getattr(self,'_score_range_request',None)
            if not latest or latest['token']!=item['token'] or not self._score_range_current(item):
                raise ValueError('Preview superseded; obsolete page rendering skipped')
            if not result.get('pdf'):
                return result
            path=Path(result['pdf']).resolve(strict=True)
            expected=cache/result['cache_key']/'preview.pdf'
            if path != expected or not path.is_relative_to(cache):
                raise ValueError('局部 PDF 不在候选的选区缓存内。')
            notation_elapsed=perf_counter()-started
            if not hasattr(self,'_score_page_cache'):
                self._score_page_cache=ScorePageCache()
            raster_started=perf_counter()
            result['page'],hit=self._score_page_cache.get(path,item.get('page_index',0),prepare_preview)
            result['preview_timing']={'notation_seconds':notation_elapsed,
                'page_seconds':perf_counter()-raster_started,'page_cache_hit':hit,
                'total_seconds':perf_counter()-started}
            return result
        def prepare(item):
            from .preview_cancel import cancellation_scope
            import subprocess
            try:
                with cancellation_scope(item.get('_cancel_event')):
                    return prepare_inner(item)
            except subprocess.TimeoutExpired as exc:
                if item.get('notes_compare'):
                    raise ValueError('DuET 暂时没有返回进度。可重新生成；当前编辑和原谱均未改变。') from exc
                raise ValueError('局部预览处理超时，请缩小范围后重试；当前编辑与正式谱均未改变。') from exc
        QThreadPool.globalInstance().start(_InputInspectionTask(request,request['token'],self._score_range_signal,prepare))

    @staticmethod
    def _score_range_cache(project):
        from hashlib import sha256
        from .release_paths import data_root

        name = sha256(str(Path(project).resolve()).encode('utf8')).hexdigest()
        return data_root()/'.cache'/'local-score-ranges'/name

    def _score_range_musicxml(self, item, generate):
        from hashlib import sha256
        from .preview_cancel import check_cancelled

        cache = self._score_range_cache(item['project'])
        result = item.get('existing')
        if result is None:
            result = generate(item['project'], item['base_revision'], item['start'], item['end'],
                              context_bars=0, render_pdf=False, cache_dir=cache, exact_revision=True)
        check_cancelled()
        expected = (cache/result['cache_key']/'preview.musicxml').resolve(strict=True)
        if not expected.is_relative_to(cache.resolve()) or Path(result['musicxml']).resolve() != expected:
            raise ValueError('未能确认选区 MusicXML 的来源。')
        with expected.open('rb') as stream:
            content = stream.read(1_500_001)
        if not content or len(content) > 1_500_000:
            raise ValueError('选区 MusicXML 超出预览上限，请缩小 L/R 范围。')
        if sha256(content).hexdigest() != result.get('artifacts', {}).get('preview.musicxml'):
            raise ValueError('选区 MusicXML 在读取期间已改变，请重试。')
        check_cancelled()
        if item.get('native'):
            from .native_score_runtime import render_range_page, RENDERER
            started = perf_counter()
            page = render_range_page(expected, sha256(content).hexdigest(), item.get('page_index', 0))
            return dict(result, renderer=RENDERER, page=page, pdf=None, pdf_render=None,
                        native_render_seconds=perf_counter()-started)
        from .osmd_pdf import free_score_timing
        try:
            timing = free_score_timing(content)
        except ValueError as exc:
            return dict(result, render_error=str(exc), renderer='osmd')
        return dict(result, musicxml_text=content.decode('utf-8-sig'), free_timing=timing, renderer='osmd')

    @Slot(str, object, str)
    def _score_range_done(self, token, result, error):
        self._score_range_active=False
        self._score_range_active_item=None
        if self._closed:
            return
        request=getattr(self,'_score_range_request',None)
        if request and request['token']==token and request.get('status')!='cancelled':
            request['status']='validating'
            if result:
                request['source_export']=result.get('source_export')
                if not request.get('notes_compare'):
                    request['_source_stamp']=result.get('dependency_source_stamp')
            checked=dict(request,result=result) if result and request.get('notes_compare') else request
            if not self._score_range_current(checked):
                request.update(status='stale',error='工程已变化，旧局部谱面未应用。')
            elif error:
                request.update(status='failed',error=error)
            else:
                result=dict(result)
                result['can_open_pdf']=bool(result.get('pdf'))
                result['validated_revision']=request['base_revision']
                render = result.get('pdf_render') or {}
                if render.get('status') == 'failed':
                    request.update(status='failed',error=render.get('error'),result=result)
                else:
                    request.update(status='ready',result=result)
        self._score_range_start_pending()

    def _score_range_status(self, token):
        request=getattr(self,'_score_range_request',None)
        if not request or request['token']!=token:
            return {'ok':True,'status':'stale','error':'Preview replaced'}
        if not self._score_range_current(request):
            return {'ok':True,'status':'stale','error':'Local content changed; refresh preview'}
        result={'ok':True,'status':request['status']}
        if request.get('_preview_progress'):result['progress']=request['_preview_progress']
        if request.get('error'):result['error']=request['error']
        result['validated_revision']=request['base_revision']
        if request.get('result') is not None:
            result['result']={k:v for k,v in request['result'].items() if k not in ('musicxml_bytes','_left_identity')}
            result['result']['validated_revision']=result['validated_revision']
        return result
