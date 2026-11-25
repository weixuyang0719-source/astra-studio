"""Audible timings from MuseScore's actual RepeatList and exporter PauseMap."""
from bisect import bisect_right
from dataclasses import dataclass, field
from fractions import Fraction

@dataclass(frozen=True,slots=True)
class PlayedBar:
    source_ordinal:int
    occurrence:int
    start_tick:int
    start:Fraction
    end:Fraction
    page:int=0
    y:int=0

def decode_bar_map(raw, score_playback, packet):
    if not isinstance(packet,dict) or set(packet) not in ({'bars','endMidiTick'}, {'bars','endMidiTick','spans','pauses','selection'}, {'bars','endMidiTick','spans','pauses','selection','writtenBounds'},
                                                           {'bars','endMidiTick','spans','pauses','selection','writtenBounds','clickGeometry'},
                         {'bars','endMidiTick','spans','pauses','selection','writtenBounds','clickGeometry','beatGeometry'}):
        raise ValueError("Invalid native playback bar map")
    rows,end=packet['bars'],packet['endMidiTick']
    ppq=int.from_bytes(raw[12:14],"big")
    if not 1<=ppq<=32767 or not isinstance(rows,list) or not 1<=len(rows)<=12000 or type(end) is not int or end<=0:
        raise ValueError("Invalid native playback bar geometry")
    raw_tempo=score_playback.tempo
    beats=[t.beat for t in raw_tempo]
    def elapsed(tick):
        beat=Fraction(tick,ppq)
        t=raw_tempo[max(0,bisect_right(beats,beat)-1)]
        return t.at+(beat-t.beat)*Fraction(t.microseconds_per_quarter,1_000_000)
    for i,row in enumerate(rows):
        if (not isinstance(row,list) or len(row) not in (2,4) or any(type(v) is not int for v in row)
                or not 0<=row[0]<12000 or not 0<=row[1]<end
                or (len(row)==4 and not (0<=row[2]<=2000 and 0<=row[3]<1800))
                or (i and rows[i-1][1]>=row[1])):
            raise ValueError("Invalid native played measure boundary")
    seen={};out=[]
    for index,row in enumerate(rows):
        number,tick=row[:2]
        page,y=row[2:] or (0,0)
        seen[number]=occurrence=seen.get(number,0)+1
        right=rows[index+1][1] if index+1<len(rows) else end
        out.append(PlayedBar(number,occurrence,tick,elapsed(tick),elapsed(right),page,y))
    return tuple(out)

@dataclass(frozen=True, slots=True)
class BarPlaybackAuthority:
    session: str
    revision: int
    rt_sequence: int
    bars: tuple[PlayedBar, ...]
    _starts: tuple[Fraction,...] = field(init=False, repr=False)

    def __post_init__(self):
        object.__setattr__(self,'_starts',tuple(b.start for b in self.bars))

    def _owned(self, session, revision, applied):
        return self.session==session and self.revision==revision and self.rt_sequence==applied

    def follow(self, session, revision, applied, position: Fraction):
        if not self._owned(session,revision,applied) or not self.bars or position<0:
            return None
        index=bisect_right(self._starts,position)-1
        return self.bars[index] if index>=0 and position<self.bars[index].end else None

    def resolve_loop(self, session, revision, applied, left, right):
        """Selection keys are (printed source ordinal, occurrence within playback)."""
        if not self._owned(session,revision,applied):
            return None
        l=next((b for b in self.bars if (b.source_ordinal,b.occurrence)==left),None)
        r=next((b for b in self.bars if (b.source_ordinal,b.occurrence)==right),None)
        if l is None or r is None or l.start>r.start:
            return None
        return l.start,r.end


@dataclass(frozen=True, slots=True)
class PlaybackTicks:
    """RepeatList raw/played spans and the exact SMF writer's pause offsets."""
    spans: tuple
    pauses: tuple
    tempo: tuple
    ppq: int
    _expanded: tuple = field(init=False, repr=False)
    _tempo_times: tuple = field(init=False, repr=False)
    _tempo_beats: tuple = field(init=False, repr=False)
    _pause_at: tuple = field(init=False, repr=False)
    _pause_end: tuple = field(init=False, repr=False)

    def __post_init__(self):
        object.__setattr__(self, '_expanded', tuple(s[2] for s in self.spans))
        object.__setattr__(self, '_tempo_times', tuple(t.at for t in self.tempo))
        object.__setattr__(self, '_tempo_beats', tuple(t.beat for t in self.tempo))
        object.__setattr__(self, '_pause_at', tuple(t for t,_ in self.pauses))
        object.__setattr__(self, '_pause_end', tuple(t+offset for t,offset in self.pauses))

    def resolve(self, tick, occurrence=1):
        """First written occurrence by default, like RepeatList::tick2utick."""
        if type(tick) is not int or tick<0 or type(occurrence) is not int or occurrence<1:
            return None
        for start,end,played in self.spans:
            if start <= tick < end:
                occurrence -= 1
                if occurrence == 0:
                    played += tick-start
                    return self.elapsed_unrolled(played)
        # Unlike upstream's out-of-range extrapolation, never seek unplayed notation.
        return None

    def elapsed_unrolled(self, played):
        offset=self.pauses[bisect_right(self._pause_at,played)-1][1]
        beat=Fraction(played+offset,self.ppq)
        t=self.tempo[max(0,bisect_right(self._tempo_beats,beat)-1)]
        return t.at+(beat-t.beat)*Fraction(t.microseconds_per_quarter,1_000_000)

    def unrolled_at(self, seconds):
        # In inserted time, keep the pause's written tick, not a phantom beat.
        t=self.tempo[max(0,bisect_right(self._tempo_times,seconds)-1)]
        midi=(t.beat+(seconds-t.at)*Fraction(1_000_000,t.microseconds_per_quarter))*self.ppq
        p=bisect_right(self._pause_end,midi)-1
        played=midi-self.pauses[p][1]
        if p+1<len(self.pauses):
            played=min(played,self.pauses[p+1][0])
        return round(played)  # pinned TempoMap::time2tick rounding

    def written_at(self, seconds):
        played=self.unrolled_at(seconds)
        i=bisect_right(self._expanded,played)-1
        if i<0:
            return None
        a,b,start=self.spans[i]
        written=min(b-1,a+played-start)
        occurrence=sum(left<=written<right for left,right,_ in self.spans[:i])+1
        return written,occurrence


def decode_playback_ticks(raw, score_playback, packet):
    if not packet or "spans" not in packet:  # compatible with a previous native worker
        return None
    spans,pauses=packet['spans'],packet.get('pauses')
    if (not isinstance(spans,list) or not 1<=len(spans)<=12000
            or not isinstance(pauses,list) or not 1<=len(pauses)<=12000 or pauses[0]!=[0,0]):
        raise ValueError("Invalid native score tick map")
    end=0
    for row in spans:
        if (not isinstance(row,list) or len(row)!=3 or any(type(v) is not int for v in row)
                or not 0<=row[0]<row[1] or row[2]!=end):
            raise ValueError("Invalid native repeat span")
        end += row[1]-row[0]
    at,offset=-1,-1
    for row in pauses:
        if (not isinstance(row,list) or len(row)!=2 or any(type(v) is not int for v in row)
                or not at<row[0]<=end or not offset<row[1]):
            raise ValueError("Invalid native pause offset")
        at,offset=row
    if end+offset != packet['endMidiTick']:
        raise ValueError("Invalid native expanded score end")
    return PlaybackTicks(tuple(map(tuple,spans)),tuple(map(tuple,pauses)),score_playback.tempo,
                         int.from_bytes(raw[12:14],"big"))


@dataclass(frozen=True,slots=True)
class ScoreBeatMap:
    """Compact native TimeSigMap, not SMF nominal meter or click accents."""
    rows: tuple
    maximum: int
    written_end: int
    _ticks: tuple = field(init=False,repr=False)
    _bars: tuple = field(init=False,repr=False)

    def __post_init__(self):
        object.__setattr__(self,'_ticks',tuple(row[0] for row in self.rows))
        object.__setattr__(self,'_bars',tuple(row[1] for row in self.rows))

    def resolve(self, measure, beat):
        if not 1<=measure<=self.maximum or not 1<=beat:
            return None
        i=bisect_right(self._bars,measure-1)-1
        if i<0:
            return None
        t,m,n,b=self.rows[i]
        if beat>n:
            return None
        raw=t+((measure-1-m)*n+beat-1)*b
        end=self._ticks[i+1] if i+1<len(self.rows) else self.written_end
        return raw if t<=raw<end else None

    def at(self, raw):
        i=bisect_right(self._ticks,raw)-1
        if i<0 or raw>=self.written_end:
            return None
        t,m,n,b=self.rows[i]
        bars,inside=divmod(raw-t,n*b)
        bar_start=t+bars*n*b
        end=self._ticks[i+1] if i+1<len(self.rows) else self.written_end
        return m+bars+1,inside//b+1,min(n,(end-bar_start+b-1)//b)


def decode_beat_geometry(packet, bounds):
    if not isinstance(packet,dict) or not bounds:
        return None
    rows,maximum=packet.get('rows'),packet.get('maxMeasure')
    if not isinstance(rows,list) or not 1<=len(rows)<=4096 or type(maximum) is not int or not 1<=maximum<=12000:
        return None
    end=bounds[1]
    last_time,last_bar=-1,-1
    for row in rows:
        if (not isinstance(row,list) or len(row)!=4 or any(type(x) is not int for x in row)
                or not last_time<row[0]<=end or not last_bar<=row[1]<=maximum
                or not 1<=row[2]<=64 or not 1<=row[3]<=1920):
            return None
        last_time,last_bar=row[:2]
    if rows[0][0]>bounds[0]:
        return None
    table=ScoreBeatMap(tuple(map(tuple,rows)),maximum,end)
    final=table.at(end-1)
    if not final:
        return None
    # Pinned measures()->size() may also count a V/HBox. Keep the maximum
    # seekable bar, not an extra dead toolbar position from a title frame.
    return ScoreBeatMap(table.rows,min(maximum,final[0]),end)
