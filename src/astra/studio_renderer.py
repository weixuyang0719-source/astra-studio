"""Choose the Studio renderer before Qt WebEngine is imported."""

import os
import sys


def configure_renderer(environ=None, platform=None):
    env = os.environ if environ is None else environ
    platform = sys.platform if platform is None else platform
    if env.get('ASTRA_RENDERER', 'opengl').strip().casefold() == 'software':
        env.setdefault('QTWEBENGINE_CHROMIUM_FLAGS', '--disable-gpu --disable-gpu-compositing')
    elif platform == 'win32':
        env.setdefault('QSG_RHI_BACKEND', 'opengl')
        env.setdefault('QT_OPENGL', 'desktop')
