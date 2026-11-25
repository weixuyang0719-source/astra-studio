"""Free measures are meter events, ending at the next event, never a second clock."""
from .timing import TempoMap

def free_ranges(data):
    clock=TempoMap(data)
    rows=data['time_signatures']
    result=[]
    for i,row in enumerate(rows):
        if not row.get('free'):continue
        end=rows[i+1]['beat'] if i+1<len(rows) else clock.beats[-1]
        if end>row['beat']+1e-8:
            result.append(dict(start_seconds=clock.to_seconds(row['beat']),end_seconds=clock.to_seconds(end),start_beat=row['beat'],end_beat=end,source='user'))
    return result
