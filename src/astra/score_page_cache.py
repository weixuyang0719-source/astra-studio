"""Bounded derived page cache; content identity never substitutes revision checks."""
from collections import OrderedDict
from copy import deepcopy
from hashlib import sha256
from pathlib import Path

class ScorePageCache:
    def __init__(self, max_bytes=32*1024*1024):
        self.max_bytes=max_bytes
        self.entries=OrderedDict()
        self.size=0

    def get(self,path,index,render):
        path=Path(path)
        key=(str(path.resolve()),sha256(path.read_bytes()).hexdigest(),index)
        if key in self.entries:
            value,size=self.entries.pop(key)
            self.entries[key]=(value,size)
            return deepcopy(value),True
        # L/R PDF and SVG share a complete A4 sheet, including page margins.
        value=render('pdf',path,index)
        size=len(value.get('dataUrl',''))+1024
        if size<=self.max_bytes:
            while self.entries and self.size+size>self.max_bytes:
                _,(_,removed)=self.entries.popitem(last=False)
                self.size-=removed
            self.entries[key]=(deepcopy(value),size)
            self.size+=size
        return value,False
