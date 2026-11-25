"""Build immutable score click markers from pinned native measure geometry.

Use the existing click bus, never an independent metronome timer, synth, or
timeline restart. Sample offsets on the SMF transport include PauseMap gaps.
"""
from math import ceil, inf, isfinite, nextafter

BELOW_GATE=0x80000000
MAX_RATE_BASIS=30000


def project_clicks(ticks, geometry):
    if not isinstance(geometry,list) or not 1<=len(geometry)<=12000:
        raise ValueError("Invalid native metronome geometry")
    out=[]
    prev=-1
    tail=ticks.spans[-1]
    end_unrolled=tail[2]+tail[1]-tail[0]
    for row in geometry:
        if (not isinstance(row,list) or len(row) not in (7,9)
                or any(type(v) is not int for v in row[:5]) or type(row[5]) is not bool
                or type(row[6]) not in (int,float)):
            raise ValueError("Invalid native metronome row")
        played,length,unit,beat,nominal,anacrusis,multiplier=row[:7]
        if (not prev<played<end_unrolled or not 1<=length<=end_unrolled-played
                or not 1<=unit<=1920 or beat not in (unit,unit*3)
                or not unit<=nominal<=1920*64 or not 0<multiplier<1e8 or not isfinite(multiplier)):
            raise ValueError("Invalid native metronome extent")
        prev=played
        limit=ceil(nextafter(multiplier*10_000,-inf))
        def plan(step):
            remain=length%step if anacrusis else 0
            if (length-remain+step-1)//step>100_000-len(out):
                raise ValueError('Native metronome exceeds event budget')
            rel=nominal-length if anacrusis else 0
            return {n:n+rel==0 for n in range(remain,length,step)}
        if unit==beat or limit>MAX_RATE_BASIS:
            simple,compound=plan(unit),{}
            rank=0
        elif limit<=1:
            simple,compound=plan(beat),{}
            rank=0
        else:
            simple,compound=plan(unit),plan(beat)
            rank=limit
        if len(out)+len(simple)+len(compound)>100_000:
            raise ValueError("Native metronome exceeds event budget")
        for k in sorted(simple.keys() | compound.keys()):
            at=ticks.elapsed_unrolled(played+k)
            a,b=simple.get(k),compound.get(k)
            if not rank or a==b:
                out.append((at,a,0))
            else:
                if a is not None:
                    out.append((at,a,BELOW_GATE|rank))
                if b is not None:
                    out.append((at,b,rank))
    return tuple(sorted(out,key=lambda p:p[0]))


def count_plan(ticks, geometry, source_seconds, speed, sample_rate):
    """Pinned count-in measure/pickup rule, emitted in hardware frames."""
    from bisect import bisect_right
    from ctypes import c_float
    from fractions import Fraction
    played=ticks.unrolled_at(Fraction(str(source_seconds)))
    at=bisect_right(geometry,played,key=lambda row:row[0])-1
    if at<0:
        return None
    start,length,unit,beat,nominal,pickup,threshold,actual_unit,offset=geometry[at]
    rel=played-start
    if not 0<=rel<length:
        return None
    step=beat if speed>=threshold else unit
    pulses=(nominal+rel+(nominal-length if pickup else 0)+step-1)//step
    remainder=(-((rel+offset)%actual_unit))%actual_unit + (length%step if pickup else 0)
    bps=beat/(ticks.ppq*threshold)*speed
    def us(n):
        # The source helper first represents beats as C++ float, then
        # casts the final microseconds to the integral event timestamp.
        return int((c_float(n/ticks.ppq).value/bps)*1_000_000)
    step_us=us(step)
    step_basis=round(Fraction(step_us*sample_rate,100))
    frames=round(Fraction((pulses*step_us-us(remainder))*sample_rate,1_000_000))
    if not (1<=pulses<=128 and 1<=nominal//step<=64 and 1<=frames<=120*sample_rate
            and 10_000<=step_basis<=40*sample_rate*10_000
            and (pulses-1)*step_basis+5_000<frames*10_000+step_basis):
        return None
    return dict(frames=frames,stepBasis=step_basis,pulses=pulses,accentPeriod=nominal//step)
