"""Conservative display suggestions from an already accepted clock, not a tracker.

Thresholds are presentation heuristics. A count between two anchors supplies
one average only: it cannot establish acceleration inside that interval.
"""

VERSION = 1
POLICY = {'minimum_changes': 3, 'minimum_seconds': 2., 'minimum_quarters': 4.,
          'minimum_relative_change': .08, 'stable_tolerance': .02,
          'return_tolerance': .03, 'edge_noise': .002}


def analyze_print_expressions(reference, *, cutoff_seconds=0.):
    rows=[]
    for a,b in zip(reference['anchors'],reference['anchors'][1:]):
        if b['seconds'] <= cutoff_seconds: continue
        bpm=60*(b['beat']-a['beat'])/(b['seconds']-a['seconds'])
        start=max(a['seconds'],cutoff_seconds)
        rows.append({'start':start,'end':b['seconds'],'beat':a['beat']+(start-a['seconds'])*bpm/60,
                     'end_beat':b['beat'],'bpm':bpm})
    def stable(index,step):
        selected=[];anchor=rows[index]['bpm']
        while 0<=index<len(rows) and abs(rows[index]['bpm']/anchor-1)<=POLICY['stable_tolerance']:
            selected.append(rows[index]);index+=step
        seconds=sum(r['end']-r['start'] for r in selected)
        quarters=sum(r['end_beat']-r['beat'] for r in selected)
        if seconds<2 or quarters<4:return None
        bpm=sum(r['bpm']*(r['end']-r['start']) for r in selected)/seconds
        return {'bpm':bpm,'seconds':seconds,'quarters':quarters}
    marks=[];candidates=[];i=0
    while i<len(rows)-1:
        delta=rows[i+1]['bpm']/rows[i]['bpm']-1
        if abs(delta)<=POLICY['edge_noise']:i+=1;continue
        direction=1 if delta>0 else -1;s=i;e=i+1;changes=1
        while e<len(rows)-1:
            difference=rows[e+1]['bpm']/rows[e]['bpm']-1
            # A stable plateau terminates a trend; don't join two ramps across it.
            if abs(difference)<=POLICY['edge_noise'] or difference*direction<0:break
            changes+=1;e+=1
        first,last=rows[s],rows[e]
        seconds=last['start']-first['end'];quarters=last['beat']-first['end_beat']
        magnitude=abs(last['bpm']/first['bpm']-1)
        evidence={'start_seconds':first['end'],'end_seconds':last['start'],
                  'start_beat':first['end_beat'],'end_beat':last['beat'],
                  'changes':changes,'seconds':seconds,'quarters':quarters,'relative_change':magnitude}
        before,after=stable(s,-1),stable(e,1)
        sufficient=changes>=3 and seconds>=2 and quarters>=4 and magnitude>=.08 and before is not None
        if sufficient:
            marks.append({'beat':first['end_beat'],'text':'accel.' if direction>0 else 'rit.',
                          'evidence':evidence,'baseline':before})
            if after is not None:
                # "a tempo" means this ramp returned to an earlier established
                # baseline, not merely that it stopped changing.
                preceding=next((m['baseline'] for m in reversed(marks[:-1]) if 'baseline' in m),None)
                if preceding and abs(after['bpm']/preceding['bpm']-1)<=.03:
                    marks.append({'beat':last['beat'],'text':'a tempo','stable':after,'return_baseline':preceding})
                else:
                    marks.append({'beat':last['beat'],'bpm':int(after['bpm']+.5),'stable':after})
            else:candidates.append(dict(evidence,reason='trend_has_no_supported_stable_destination'))
        elif magnitude>=.08:
            # A step between supported plateaus proves a new stable speed,
            # but does not prove a ramp. Return wording needs a prior baseline.
            preceding=next((m['baseline'] for m in reversed(marks) if 'baseline' in m),None)
            if changes==1 and before and after:
                if preceding and abs(after['bpm']/preceding['bpm']-1)<=.03:
                    marks.append({'beat':last['beat'],'text':'a tempo','stable':after,'return_baseline':preceding})
                else:
                    marks.append({'beat':last['beat'],'bpm':int(after['bpm']+.5),'stable':after})
            else:candidates.append(dict(evidence,reason='insufficient_sustained_trend_or_baseline'))
        i=e
    return {'version':VERSION,'policy':dict(POLICY),'marks':marks,'candidates':candidates,
            'basis':'accepted_clock_only','explicit_marks_precedence':True}


def apply_print_expressions(root, reference, *, origin, principal):
    """Change visual directions only; every sound element survives untouched."""
    from fractions import Fraction
    import xml.etree.ElementTree as ET
    from .score_tempo import _measure_layout
    audit=analyze_print_expressions(reference,cutoff_seconds=principal['excluded_prefix_seconds'])
    layout=_measure_layout(root)
    if not layout:return audit
    primary=layout[0][0];bars=[r for r in layout if r[0] is primary]
    existing=[]
    for _,measure,start,_,divisions in bars:
        cursor=0
        for child in measure:
            if child.tag=='direction':
                pos=start+Fraction(cursor+int(child.findtext('offset','0')),divisions)
                for dtype in child.findall('direction-type'):
                    for item in dtype:
                        if item.tag=='metronome' or item.tag=='words' and (item.text or '').strip().lower() in {
                                'rit.','rit','ritardando','rall.','rallentando','accel.','accelerando','a tempo'}:
                            existing.append((pos,ET.tostring(item,encoding='unicode')))
            elif child.tag in ('backup','forward'):
                cursor+=int(child.findtext('duration','0'))*(-1 if child.tag=='backup' else 1)
            elif child.tag=='note' and child.find('chord') is None and child.find('grace') is None:
                cursor+=int(child.findtext('duration','0'))
    visible=audit['marks'];suppressed=[]
    printed=[];printed_bpms=[principal['bpm']]
    for original in visible:
        mark=dict(original)
        from .timing import TempoMap
        clock=TempoMap(reference)
        if any(clock.to_beat(a['start_seconds'])-1e-8 <= mark['beat'] <= clock.to_beat(a['end_seconds'])+1e-8
               for a in reference.get('tempo_expression_annotations',[])):
            suppressed.append(dict(mark,reason='user_confirmed_expression_range_has_precedence'));continue
        if mark.get('text')=='a tempo' and not any(abs(mark['return_baseline']['bpm']/bpm-1)<=.03 for bpm in printed_bpms):
            mark.pop('text');mark['bpm']=int(mark['stable']['bpm']+.5)
            mark['display_reason']='returned_baseline_not_previously_printed_use_explicit_number'
        q=Fraction(str(mark['beat']))-origin
        nearby=[value for position,value in existing if abs(float(position-q))<=.125]
        if nearby:
            suppressed.append(dict(mark,reason='existing_visual_tempo_has_precedence',existing=nearby));continue
        target=next((row for row in reversed(bars) if row[2]<=q<row[2]+row[3]),None)
        if target is None:continue
        _,measure,start,length,divisions=target
        direction=ET.Element('direction',{'placement':'above'});dtype=ET.SubElement(direction,'direction-type')
        if 'text' in mark:ET.SubElement(dtype,'words',{'font-style':'italic'}).text=mark['text']
        else:
            metro=ET.SubElement(dtype,'metronome');ET.SubElement(metro,'beat-unit').text='quarter'
            ET.SubElement(metro,'per-minute').text=str(mark['bpm'])
        offset=round((q-start)*divisions)
        if offset:ET.SubElement(direction,'offset',{'sound':'no'}).text=str(offset)
        ET.SubElement(direction,'staff').text='1'
        measure.insert(1 if measure.find('attributes') is not None else 0,direction)
        printed.append(dict(mark,measure=measure.get('number'),offset_units=offset))
        if 'bpm' in mark:printed_bpms.append(mark['bpm'])
    audit.update(printed=printed,suppressed=suppressed,existing_visual_marks_preserved=len(existing))
    return audit
