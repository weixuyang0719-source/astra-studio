"""Explicit fixed-boundary flourish engraving, never automatic classification."""
from fractions import Fraction as F
from math import isfinite,lcm
import json
import xml.etree.ElementTree as ET

FIELD='astra-free-run-playback-v1'


def candidate(records,performance,model,clock,identity,request,protected=()):
    from .notation_ornaments import _hash,_locator
    if request.get('input_sha256',identity['input_sha256'])!=identity['input_sha256']:
        raise ValueError('花奏选择的原始输入已变化，请重新选择。')
    if 'input_ids' in request:
        ids=request['input_ids']
        if not isinstance(ids,list) or any(type(i) is not int or not 0<=i<len(performance) for i in ids):
            raise ValueError('无效花奏源音符 ID。')
    else:
        start,end=request.get('start_seconds'),request.get('end_seconds');staff=request.get('staff')
        if (not isinstance(start,(int,float)) or not isinstance(end,(int,float)) or
                type(start) is bool or type(end) is bool or type(staff) is not int or
                not isfinite(start) or not isfinite(end) or not 0<=start<end or staff not in (1,2)):
            raise ValueError('请选择有效的音频秒范围及左右手。')
        ids=[i for i,n in enumerate(performance) if start<=n['start']<end and int(model[i][3])+1==staff]
    if not 4<=len(ids)<=16 or len(set(ids))!=len(ids):raise ValueError('花奏小音组请选择同手 4–16 个独立音符。')
    if set(ids)&set(protected):raise ValueError('选择包含受保护的人工音符，请保留其明确记谱或另选片段。')
    selected=[r for r in records if r.get('input_id') in ids]
    if len(selected)!=len(ids) or {r.get('input_id') for r in selected}!=set(ids):
        raise ValueError('选区源事件不能唯一对应完整谱面音符，不会猜测或省略。')
    selected.sort(key=lambda r:r['start'])
    if len({(r['part'],r['measure'],r['staff'],r['voice']) for r in selected})!=1:
        raise ValueError('花奏小音组只支持同小节、同谱表、同声部。')
    if any(r['grace'] or r['node'].find('chord') is not None or r['node'].find('tie') is not None or
           r['node'].get('id') is not None or r['node'].find('notations') is not None or
           r['node'].find('time-modification') is not None for r in selected):
        raise ValueError('选择包含和弦、延音、装饰音或已有连音/作者标记，不能直接重写。')
    if any(a['end']!=b['start'] for a,b in zip(selected,selected[1:])):
        raise ValueError('请选择连续、不重叠的单音跑动，不能吞掉休止或其他声部。')
    measure=selected[0]['measure_node'];children=list(measure)
    first,last=children.index(selected[0]['node']),children.index(selected[-1]['node'])
    if children[first:last+1]!=[r['node'] for r in selected]:
        raise ValueError('选择内部有其他音符或方向标记，保留其原位置，不作粗记谱。')
    # Reject simultaneous chord members that weren't requested explicitly.
    if any(r not in selected and r['part']==selected[0]['part'] and r['staff']==selected[0]['staff'] and
           r['voice']==selected[0]['voice'] and r['start']<selected[-1]['end'] and r['end']>selected[0]['start']
           for r in records):raise ValueError('选区存在未选择的同声部音符。')
    start,end=selected[0]['start'],selected[-1]['end'];span=end-start
    count=len(selected);normal=2**(count.bit_length()-1);value=span/normal
    types={F(1):'quarter',F(1,2):'eighth',F(1,4):'16th',F(1,8):'32nd',F(1,16):'64th'}
    if value not in types or not 0<span<=4:
        raise ValueError('当前两端跨度不能写成简明的小音连音组；请选择不超过四拍的二分网格跨度。')
    ids=[r['input_id'] for r in selected]
    if any(performance[a]['start']>=performance[b]['start'] for a,b in zip(ids,ids[1:])):
        raise ValueError('原始音符次序与谱面不一致，不能重排演奏攻击。')
    request={'input_sha256':identity['input_sha256'],'input_ids':ids}
    result={'kind':'free_run','label':'人工花奏小音组',
        'explanation':'仅在你确认这是自由花奏时采用：两端与全部音高保留，内部均整小音并隐藏比例。谱面内部时值是粗记谱；Astra MIDI 保留该组原始演奏秒时间。',
        'measure':selected[0]['measure'],'staff':selected[0]['staff'],'start_beat':float(start),'end_beat':float(end),
        'input_ids':ids,'anchor_input_ids':[],'selected':[_locator(r) for r in selected],'anchors':[],
        'source_seconds':[dict(input_id=i,**performance[i]) for i in ids],
        'manual_request':request,'evidence':{'explicit_user_selection':True,'actual_notes':count,'normal_notes':normal,
             'type':types[value],'start':str(start),'end':str(end),'classification':'not_automatically_classified'}}
    result['id']=_hash([identity,result['selected'],[],result['kind']])[:24]
    return result


def transform(root,candidates,lookup,expected,used):
    """Uniform internal notation only. Source playback data stays explicit."""
    from .notation_ornaments import _hash,_locator
    from .score_tempo import _measure_layout
    targets=[c for c in candidates if c['kind']=='free_run']
    if not targets:return []
    layout=_measure_layout(root)
    division=1
    for row in layout:division=lcm(division,row[4])
    for c in targets:division=lcm(division,((F(c['evidence']['end'])-F(c['evidence']['start']))/len(c['selected'])).denominator)
    for _,measure,_,_,old in layout:
        factor=division//old
        for node in measure.iter():
            if node.tag in ('duration','offset') and node.text:node.text=str(int(node.text)*factor)
        attrs=measure.find('attributes')
        if attrs is None:attrs=ET.Element('attributes');measure.insert(0,attrs)
        div=attrs.find('divisions')
        if div is None:div=ET.Element('divisions');attrs.insert(0,div)
        div.text=str(division)
    entries=[]
    for c in targets:
        records=[lookup[_hash(loc)] for loc in c['selected']]
        start,end=F(c['evidence']['start']),F(c['evidence']['end']);width=(end-start)/len(records)
        count,normal=len(records),c['evidence']['normal_notes'];name=c['evidence']['type']
        for index,(r,source) in enumerate(zip(records,c['source_seconds'])):
            key=_hash(_locator(r))
            if key in used:raise ValueError('花奏与已有装饰选择重叠，请先撤回冲突选择。')
            used.add(key);node=r['node'];a,b=start+index*width,start+(index+1)*width
            ordinary=(r['key'],r['start'],r['end'],False)
            if expected[ordinary]!=1:raise ValueError('花奏原音符区间不唯一。')
            expected[ordinary]-=1;expected[(r['key'],a,b,False)]+=1
            for tag in ('dot','time-modification','beam','notations'):
                for child in list(node.findall(tag)):node.remove(child)
            node.find('duration').text=str(int(width*division));node.find('type').text=name
            node.find('type').set('size','cue')  # Graphical size, not a silent <cue/> note.
            if count!=normal:
                tm=ET.Element('time-modification');ET.SubElement(tm,'actual-notes').text=str(count)
                ET.SubElement(tm,'normal-notes').text=str(normal);ET.SubElement(tm,'normal-type').text=name
                node.insert(list(node).index(node.find('type'))+1,tm)
            beam_levels={'quarter':0,'eighth':1,'16th':2,'32nd':3,'64th':4}[name]
            for level in range(1,beam_levels+1):
                ET.SubElement(node,'beam',{'number':str(level)}).text='begin' if index==0 else 'end' if index==count-1 else 'continue'
            if count!=normal and index in (0,count-1):
                notation=ET.SubElement(node,'notations');ET.SubElement(notation,'tuplet',
                    {'type':'start' if index==0 else 'stop','number':'1','bracket':'no','show-number':'none','show-type':'none'})
            note_id=f'astra-free-{c["id"]}-{source["input_id"]}';node.set('id',note_id)
            entries.append(dict(source,note_id=note_id,part=r['part'],staff=r['staff'],
                                written_start=str(a),written_end=str(b)))
    identification=root.find('identification')
    if identification is None:identification=ET.Element('identification');root.insert(0,identification)
    misc=identification.find('miscellaneous')
    if misc is None:misc=ET.SubElement(identification,'miscellaneous')
    if misc.find(f"miscellaneous-field[@name='{FIELD}']") is not None:raise ValueError('源谱已带花奏播放映射，须从不可变基础谱重建。')
    ET.SubElement(misc,'miscellaneous-field',{'name':FIELD}).text=json.dumps({'version':1,'entries':entries},ensure_ascii=False)
    return entries


def playback_overrides(root,performance,clock):
    """Verify note ID, visual coordinates and source events before any override."""
    field=root.find(f"identification/miscellaneous/miscellaneous-field[@name='{FIELD}']")
    if field is None:return {}
    from .notation_ornaments import _records
    data=json.loads(field.text)
    if data.get('version')!=1 or not isinstance(data.get('entries'),list):raise ValueError('无效花奏播放映射。')
    identified=[r for r in _records(root) if r['node'].get('id')]
    records={r['node'].get('id'):r for r in identified}
    if len(records)!=len(identified):raise ValueError('谱面音符 ID 重复，花奏播放映射不能唯一验证。')
    overrides={};used=set()
    for entry in data['entries']:
        r=records.get(entry['note_id'])
        if not r or (r['part'],r['staff'],r['pitch'],r['start'],r['end'])!=(entry['part'],entry['staff'],entry['pitch'],F(entry['written_start']),F(entry['written_end'])):
            raise ValueError('花奏谱面已改变，原播放映射不能静默套用。')
        matches=[n for n in performance if n['index'] not in used and n['pitch']==entry['pitch'] and n['velocity']==entry['velocity'] and
                 abs(n['start']-entry['start'])<=.002 and abs(n['end']-entry['end'])<=.002]
        if len(matches)!=1:raise ValueError('花奏输入不能唯一恢复原演奏秒事件。')
        source=matches[0];used.add(source['index'])
        key=(r['part'],r['staff'],r['pitch'],r['start'],r['end'])
        if key in overrides:raise ValueError('花奏播放映射重复。')
        overrides[key]=dict(source,input_id=entry['input_id'])
    return overrides
