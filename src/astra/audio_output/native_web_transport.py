"""Expose the C++ command export directly as a Qt WebChannel slot."""

import ctypes
from PySide6.QtCore import QObject, Slot
from .native_assets import native_library

# PySide registers the signature and calls this ctypes export. There is no
# Python method handling the command, JSON envelope or dispatch callback.
_library = ctypes.CDLL(str(native_library('astra_audio_core.dll')))
_command = _library.astra_core_web_command
_command.__name__ = 'command'
_command.argtypes = (ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_double)
_command.restype = ctypes.c_int

class NativeWebTransport(QObject):
    command = Slot(int, int, int, float, result=int)(_command)
