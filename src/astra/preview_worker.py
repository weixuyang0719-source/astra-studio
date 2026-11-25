"""Isolate PDF rasterization and MIDI parsing from the UI's Python GIL."""
from .release_paths import data_root
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


def prepare_preview(kind, path, index=0, *, background=False, image_bytes=False):
    with tempfile.TemporaryDirectory(prefix='astra-preview-') as folder:
        result = Path(folder) / 'result.json'
        env = dict(os.environ, PYTHONIOENCODING='utf-8', QT_QPA_PLATFORM='offscreen')
        env['PYTHONPATH'] = os.pathsep.join(filter(None, (str(Path(__file__).resolve().parents[1]), env.get('PYTHONPATH'))))
        python = data_root() / '.venv' / 'Scripts' / 'python.exe'
        if background:
            env.update(OMP_NUM_THREADS='1',OPENBLAS_NUM_THREADS='1',MKL_NUM_THREADS='1')
        from .preview_cancel import run_process
        mode = 'pdf-binary' if kind == 'pdf' and image_bytes else kind
        process = run_process([str(python) if python.is_file() else sys.executable,
                                  '-m', 'astra.preview_worker', mode, str(path), str(index), str(result)],
                                 env=env, capture_output=True, timeout=30,
                                 creationflags=(getattr(subprocess, 'CREATE_NO_WINDOW', 0) |
                                                (getattr(subprocess,'BELOW_NORMAL_PRIORITY_CLASS',0) if background else 0)))
        if process.returncode or not result.is_file():
            raise ValueError('预览读取失败：' + process.stderr.decode('utf-8', errors='replace')[-1500:])
        metadata = json.loads(result.read_text(encoding='utf-8'))
        if mode == 'pdf-binary':
            metadata['previewPng'] = result.with_suffix('.png').read_bytes()
        return metadata


def unpack_midi(data):
    import mido
    from .midi_preview import _Note
    return ([(at, mido.Message.from_dict(message)) for at, message in data['events']],
            [_Note(**note) for note in data['notes']], data['duration'], data['metadata'])


def trim_score_whitespace(png):
    """Trim empty page margins only in the dock; never change the PDF itself."""
    import numpy as np
    from PySide6.QtCore import QBuffer, QIODevice
    from PySide6.QtGui import QImage
    image = QImage.fromData(png).convertToFormat(QImage.Format.Format_RGBA8888)
    if image.isNull():
        return png
    pixels = np.frombuffer(image.constBits(), dtype=np.uint8).reshape(
        image.height(), image.bytesPerLine())[:, :image.width()*4].reshape(image.height(), image.width(), 4)
    # Composite alpha over white before finding ink (transparent margins are empty).
    alpha = pixels[:, :, 3:4].astype(np.float32) / 255
    rgb = pixels[:, :, :3] * alpha + 255 * (1-alpha)
    rows = np.flatnonzero(np.any(rgb < 245, axis=(1,2)))
    if not len(rows):
        return png
    top, bottom = max(0, int(rows[0])-16), min(image.height(), int(rows[-1])+17)
    output = QBuffer()
    output.open(QIODevice.OpenModeFlag.WriteOnly)
    if not image.copy(0, top, image.width(), bottom-top).save(output, 'PNG'):
        return png
    return bytes(output.data())


def main():
    kind, filename, page_index, result_path = sys.argv[1:]
    path = Path(filename)
    if kind == 'midi':
        from dataclasses import asdict
        from .midi_preview import _read_midi, _WindowsSynth
        events, notes, duration, metadata = _read_midi(path, include_metadata=True)
        metadata['_synth_available'] = _WindowsSynth().device() is not None
        channels = {n.channel for n in notes}
        # Keep only audible channels in this preview payload. Source MIDI stays intact.
        for field in ('pedals', 'pedal_events'):
            metadata[field] = [p for p in metadata.get(field, []) if p.get('channel') in channels]
        result = {'events': [(at, message.dict()) for at, message in events if message.channel in channels],
                  'notes': [asdict(note) for note in notes], 'duration': duration, 'metadata': metadata}
    elif kind in ('pdf', 'pdf-content', 'pdf-binary'):
        import base64
        from PySide6.QtCore import QBuffer, QIODevice, QSize
        from PySide6.QtGui import QGuiApplication, QImageWriter
        from PySide6.QtPdf import QPdfDocument
        app = QGuiApplication([])
        document = QPdfDocument()
        buffer = QBuffer()
        buffer.setData(path.read_bytes())
        buffer.open(QIODevice.OpenModeFlag.ReadOnly)
        document.load(buffer)
        try:
            # QIODevice-backed PDF loading can finish asynchronously. Checking
            # pageCount immediately races the loader on larger documents.
            from time import monotonic
            from PySide6.QtCore import QThread
            deadline=monotonic()+5
            while document.pageCount()==0 and document.error()==QPdfDocument.Error.None_ and monotonic()<deadline:
                app.processEvents()
                QThread.msleep(2)
            index = int(page_index)
            if document.error() != QPdfDocument.Error.None_ or not 0 <= index < document.pageCount():
                raise ValueError('无法读取这个 PDF 页面。')
            page = document.pagePointSize(index)
            # Leave sampling headroom for the 790 CSS px score at HiDPI.
            # Keep the raster bounded and cached per page, independent of zoom.
            scale = min(1800 / page.width(), 2450 / page.height())
            image = document.render(index, QSize(round(page.width()*scale), round(page.height()*scale)))
            png = QBuffer()
            png.open(QIODevice.OpenModeFlag.WriteOnly)
            if kind == 'pdf-binary':
                writer = QImageWriter(png, b'PNG')
                # Keep full raster detail with faster lossless encoding for interactive preview.
                writer.setCompression(35)
                saved = not image.isNull() and writer.write(image)
            else:
                saved = not image.isNull() and image.save(png, 'PNG')
            if not saved:
                raise ValueError('无法绘制这个 PDF 页面。')
            payload = bytes(png.data())
            if kind == 'pdf-content':
                payload = trim_score_whitespace(payload)
            result = {'pages': document.pageCount(), 'index': index, 'title': path.name}
            if kind == 'pdf-binary':
                Path(result_path).with_suffix('.png').write_bytes(payload)
                result.update(width=image.width(),height=image.height())
            else:
                result['dataUrl'] = 'data:image/png;base64,' + base64.b64encode(payload).decode('ascii')
        finally:
            document.close()
            buffer.close()
    else:
        raise ValueError('Unknown preview type')
    Path(result_path).write_text(json.dumps(result, ensure_ascii=False, allow_nan=False), encoding='utf-8')


if __name__ == '__main__':
    main()
