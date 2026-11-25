// Included in document_detail. A range never owns a second clock or a list of
// selected IDs. All membership is evaluated against this exact live head.
struct SelectionScope {
    std::string mode;
    int channel;
    bool notes() const { return mode!="cc64"; }
    bool controls() const { return mode!="notes"; }
    bool has(int lane) const { return mode=="full" || (controls() && lane/16==64 && lane%16==channel); }
    J wire() const {
        J out{{"mode",mode}};
        if (channel>=0) out["channel"]=channel;
        return out;
    }
};
SelectionScope selection_scope(const J& j) {
    t::object(j);
    const auto mode=t::text(required(j,"mode"),"Invalid selection membership");
    if (mode=="notes" || mode=="full") return {mode,-1};
    need(mode=="cc64" || mode=="notes_cc64","Invalid selection membership");
    return {mode,midi(required(j,"channel"),0,15,"Invalid CC64 lane")};
}
int selection_lane(const J& r) {
    return 16*midi(required(r,"number"),0,127,"Invalid controller number")+
        midi(get(r,"channel",J(0)),0,15,"Invalid controller channel");
}
struct SelectionSpan {
    double a,b,q,z;
    double first(bool bars) const { return bars?q:a; }
    double last(bool bars) const { return bars?z:b; }
    double length(bool bars) const { return last(bars)-first(bars); }
    bool contains(double x) const { return a<=x && x<b; }
    J wire(const SelectionScope& scope) const {
        return J{{"start",a},{"end",b},{"length",b-a},{"start_beat",q},{"end_beat",z},
                 {"length_beat",z-q},{"scope",scope.wire()}};
    }
};
SelectionSpan selection_span(const J& r) {
    return {sec(r.at("start")),sec(r.at("end")),t::number(r.at("start_beat")),t::number(r.at("end_beat"))};
}
const J& active_selection(const aed_session& s,const J& command) {
    need(s.selection.is_object() && text_is(get(s.selection,"revision"),rev(s)) &&
         text_is(get(command,"token"),s.selection.at("token").get_ref<const std::string&>()),stale_message,AEC_STALE);
    return s.selection;
}
struct SelectionClock {
    const Document& d;
    const J& ctx;
    double duration() const { return d.env->duration; }
    double beat(double s) const { return t::timing_query(d.env->curve,t::InputAxis::seconds,s); }
    double time(double q) const { return t::timing_query(d.env->curve,t::InputAxis::beat,q); }
    bool bars() const { return text_is(get(get(ctx,"axis"),"mode"),"bars"); }
    double coord(bool bars,double s) const { return bars?beat(s):s; }
    double extent(bool bars) const { return coord(bars,duration()); }
    double seconds(bool bars,double v) const {
        if (v==extent(bars)) return duration();
        return bars?time(v):v;
    }
    double clamp(double v) const { return std::clamp(v,0.,duration()); }
    SelectionSpan at(double a,double b) const { return {a,b,beat(a),beat(b)}; }
    SelectionSpan translate(bool bars,double first,double length,bool fit,const double* exact_start=nullptr) const {
        const double max=extent(bars);
        const double eps=8*std::numeric_limits<double>::epsilon()*std::max(1.,max);
        need(length>=0 && length<=max+eps,"时间选区超出曲目范围。");
        if (fit) first=std::clamp(first,0.,std::max(0.,max-length));
        const double last=t::finite(first+length);
        need(first>=0. && first<=max && last<=max+eps,"时间选区超出曲目范围。");
        const double stop=std::min(max,last);
        const double a=exact_start && first==coord(bars,*exact_start)?*exact_start:clamp(seconds(bars,first));
        const double b=length==0?a:clamp(seconds(bars,stop));
        need(!length || b>a,"目标选区精度不足；未应用编辑。");
        return bars?SelectionSpan{a,b,first,stop}:at(a,b);
    }
    double width() const { return t::number(required(t::object(required(ctx,"axis")),"width")); }
    double display_extent() const {
        return t::number(required(ctx.at("axis"),bars()?"beat_extent":"seconds_extent"));
    }
    double x(double s) const { return coord(bars(),s)/display_extent()*width(); }
    double raw(double px) const {
        const double w=width(), limit=display_extent();
        const double v=std::clamp(px,0.,w)/std::max(1.,w)*limit;
        return clamp(seconds(bars(),v));
    }
    double unit(const J& cfg) const {
        const auto v=get(cfg,"unit",J("auto"));
        if (v.is_number()) {
            const double u=t::number(v);need(u>0.,"Invalid grid unit");return u;
        }
        need(text_is(v,"auto"),"Invalid grid unit");
        const double pixels=width()/t::number(ctx.at("axis").at("beat_extent"));
        double u=1./32.;while (u<64. && u*pixels<12.) u*=2.;
        return u;
    }
    double snap(double s,const J& point,const J& cfg,bool free,const SelectionScope& scope,
                std::optional<double> ref={},const SelectionSpan* exclude=nullptr) const {
        if (free || get(point,"bypass",J(false)).get<bool>() || !get(cfg,"enabled",J(true)).get<bool>()) return s;
        const auto mode=t::text(get(cfg,"mode",J("grid")),"Invalid range Snap type");
        const auto distance=[&](double v){return std::abs(x(v)-x(s));};
        double best=s, gap=std::numeric_limits<double>::infinity();
        const auto offer=[&](double v){
            const auto g=distance(v);
            if (g<gap || (g==gap && v<best)) {gap=g;best=v;}
        };
        if (mode=="detected") {
            for (const auto& p:list(ctx,"detected_seconds")) offer(sec(p));
            return gap<=10.?clamp(best):s;
        }
        if (mode=="events" || mode=="grid-events-cursor") {
            if (scope.notes()) for (const auto& v:d.events[0].rows) {
                const double a=sec(v.second.json.at("start")),b=sec(v.second.json.at("end"));
                if (!exclude || a>=exclude->b || b<=exclude->a) {offer(a);offer(b);}
            }
            if (scope.controls()) for (const auto& v:d.events[1].rows) {
                const auto& r=v.second.json; const double a=sec(r.at("seconds"));
                if (scope.has(selection_lane(r)) && (!exclude || !exclude->contains(a))) offer(a);
            }
        }
        if (mode=="cursor" || mode=="grid-events-cursor") {
            const double cursor=sec(get(point,"cursor_seconds",get(ctx,"cursor_seconds",J(0))));
            if (distance(cursor)<gap) {best=cursor;gap=distance(cursor);}
        }
        if (gap<=10.) return clamp(best);
        if (mode=="events" || mode=="cursor") return s;
        need(mode=="grid" || mode=="relative" || mode=="grid-events-cursor","Invalid range Snap type");
        const auto rounded=[](double v){const double k=std::floor(v);return v-k<.5?k:k+1.;};
        const double q=beat(s),u=unit(cfg);
        if (mode=="relative" && ref) {
            const double a=beat(*ref);
            return clamp(time(std::clamp(t::finite(a+rounded((q-a)/u)*u),0.,extent(true))));
        }
        // Only structural fields are needed. In particular, don't copy a
        // potentially large rhythm baseline, evidence or vendor data here.
        const auto& tempo=d.header.at("tempo");
        J layout=J::object();
        for (const char* k:{"anchors","time_signatures","pickup_beats","first_full_bar_beat","short_bar_annotations"})
            if (const auto* v=t::find(tempo,k)) layout[k]=*v;
        const auto bar=astra::editor::timing_query("editor_bar_at",J{{"tempo",std::move(layout)},{"beat",q}});
        const double a=t::number(bar.at("start")),b=t::number(bar.at("end"));
        return clamp(time(std::clamp(std::min(b,t::finite(a+rounded((q-a)/u)*u)),0.,extent(true))));
    }
};

// Controllers with an ordered, held raw value. Never fabricate a bank select,
// data-entry/RPN sequence, channel-mode message, or unspecified device default.
// CC64 alone has an editor-defined empty level (off).
bool range_held(int lane) {
    const int n=lane/16;
    return n<120 && n!=0 && n!=32 && n!=6 && n!=38 && !(96<=n && n<=101);
}
struct RangeLevel { double seconds=-1.; int value=-1; };
using RangeLevels=std::map<int,RangeLevel>;
RangeLevel range_level(const RangeLevels& rows,int lane) {
    const auto i=rows.find(lane);
    return i==rows.end()?RangeLevel{-1.,lane/16==64?0:-1}:i->second;
}
void put_level(RangeLevels& out,int lane,double seconds,int value) {
    auto& v=out[lane];if (seconds>=v.seconds) v={seconds,value};
}
int raw_level(const J& row) { return midi(required(row,"value"),0,127,"Invalid raw controller value"); }
RangeLevels range_levels(const Document& d,const SelectionScope& scope,double at,bool inclusive) {
    RangeLevels out;
    for (const auto& item:d.events[1].rows) {
        const auto& row=item.second.json; const double s=sec(row.at("seconds"));
        if (s>at || (!inclusive && s==at)) continue;
        const int lane=selection_lane(row);
        if (scope.has(lane) && range_held(lane)) put_level(out,lane,s,raw_level(row));
    }
    return out;
}

J selection_material(const SelectionClock& clock,SelectionSpan span,const SelectionScope& scope,bool bars) {
    J notes=J::array(), controls=J::array(), entry=J::array();
    const double length=span.length(bars);
    const auto offset=[&](double time){
        if (time==span.a) return 0.;
        if (time==span.b) return length;
        return clock.coord(bars,time)-span.first(bars);
    };
    if (span.b>span.a && scope.notes()) for (const auto& item:clock.d.events[0].rows) {
        const auto& n=item.second.json;
        const double a=std::max(span.a,sec(n.at("start"))), b=std::min(span.b,sec(n.at("end")));
        if (b<=a) continue;
        J row=n;row["start"]=offset(a);row["end"]=offset(b);notes.push_back(std::move(row));
    }
    if (span.b>span.a && scope.controls()) {
        for (const auto& item:clock.d.events[1].rows) {
            const auto& c=item.second.json; const double x=sec(c.at("seconds"));
            if (!span.contains(x) || !scope.has(selection_lane(c))) continue;
            J row=c;row["seconds"]=offset(x);controls.push_back(std::move(row));
        }
        for (const auto& v:range_levels(clock.d,scope,span.a,false))
            entry.push_back(J::array({v.first,v.second.value}));
    }
    return J{{"type","selection_range"},{"version",1},{"basis",bars?"bars":"seconds"},
             {"origin",span.first(bars)},{"length",length},{"scope",scope.wire()},
             {"notes",std::move(notes)},{"controls",std::move(controls)},{"entry",std::move(entry)}};
}

struct SelectionBatch {
    aed_session& session;
    Plan& plan;
    bool preview;
    std::string mint;
    const Document& current() const { return plan.replacement?*plan.replacement:*session.doc; }
    void stage(unsigned group,const J& op,const RangeProjection* window=nullptr) {
        if (list(op,"add").empty() && list(op,"delete").empty() && list(op,"updates").empty()) return;
        const auto& d=current();
        auto lane=d.lane;
        if (!lane) {
            if (!session.reopened_lane) session.reopened_lane=make_lane(d.events,*d.env,session.resolution);
            lane=session.reopened_lane;
        }
        J feedback=J::object();
        auto patch=event_prepare(d,lane,op,group,session.resolution,preview,{},feedback,window,&mint);
        if (!patch) return;
        auto& keys=plan.batch_keys[group];
        for (auto k:patch->remove) keys.insert(k);
        for (const auto& v:patch->put) keys.insert(v.first);
        if (!plan.replacement) plan.replacement=std::make_unique<Document>(d);
        patch->install(*plan.replacement);
        patch->core_patch.accept();
        auto& batches=plan.delta["event_batches"];
        if (batches.is_null()) batches=J::array();
        batches.push_back(std::move(feedback.at("events")));
    }
};

// Half-open physical controller windows. At a right edge an existing exact
// row already restores its own state. A synthetic restoration is added only
// when the edited envelope would actually leave a different held value.
J selection_guards(const Document& old,const Document& next,const SelectionScope& scope,
                   const SelectionSpan* erase,const SelectionSpan* dest,
                   const std::map<int,int>& entering,const std::set<int>& exact_start) {
    J result=J::object();
    if (!scope.controls()) return result;
    std::set<double> edges;
    if (erase) {edges.insert(erase->a);edges.insert(erase->b);}
    if (dest && !entering.empty()) {edges.insert(dest->a);edges.insert(dest->b);}
    if (edges.empty()) return result;
    const auto incoming=erase?range_levels(old,scope,erase->a,false):RangeLevels{};
    RangeLevels made;
    for (double at:edges) {
        const auto before=range_levels(old,scope,at,true), now=range_levels(next,scope,at,true);
        std::set<int> lanes;
        for (const auto& v:before) lanes.insert(v.first);
        for (const auto& v:now) lanes.insert(v.first);
        for (const auto& v:entering) lanes.insert(v.first);
        for (const auto& v:made) lanes.insert(v.first);
        for (int lane:lanes) {
            const auto landed=entering.find(lane);
            const bool in_dest=dest && dest->contains(at) && landed!=entering.end();
            const bool in_source=erase && erase->contains(at);
            const bool relevant=erase || landed!=entering.end();
            if (!relevant) continue;
            int want=-1;
            if (in_dest) {
                if (at==dest->a && !exact_start.count(lane)) want=landed->second;
            } else if (in_source) want=lane/16==64?0:range_level(incoming,lane).value;
            else want=range_level(before,lane).value;
            const auto raw=range_level(now,lane), guard=range_level(made,lane);
            const int value=guard.seconds>=0. && guard.seconds>=raw.seconds?guard.value:raw.value;
            if (want<0 || want==value) continue;
            if (!result.contains("add")) result["add"]=J::array();
            result.at("add").push_back(J{{"seconds",at},{"channel",lane%16},{"number",lane/16},{"value",want}});
            put_level(made,lane,at,want);
        }
    }
    return result;
}

void selection_transfer(SelectionBatch& batch,const SelectionClock& clock,const J& clip,
                        const SelectionScope& scope,const SelectionSpan* erase,const SelectionSpan* dest,bool move) {
    const bool bars=text_is(clip.at("basis"),"bars");
    const double length=sec(clip.at("length"));
    std::array<J,2> ops{J::object(),J::object()};
    const auto push=[&](unsigned group,const char* field,J row){
        auto& op=ops[group];if (!op.contains(field)) op[field]=J::array();op.at(field).push_back(std::move(row));
    };
    const auto landing=[&](double offset){
        if (offset==0.) return dest->a;
        if (offset==length) return dest->b;
        return std::clamp(clock.seconds(bars,dest->first(bars)+offset),dest->a,dest->b);
    };
    const auto original=[&](unsigned group,const J& row)->const J& {
        const auto& events=clock.d.events[group];
        return events.rows.at(events.ids.at(id_key(row.at("id")))).json;
    };
    for (const auto& row:list(clip,"notes")) {
        t::object(row);need(scope.notes(),"Note outside clipboard membership");
        const double a=sec(required(row,"start")),b=sec(required(row,"end"));
        need((!dest || b>a) && b<=length && a<=b,"Invalid clipped note span");
        bool keep_moving_id=false;
        if (erase) {
            const auto& old=original(0,row);const auto& id=old.at("id");
            const double from=sec(old.at("start")),to=sec(old.at("end"));
            const bool left=from<erase->a,right=to>erase->b;
            if (left) {
                push(0,"updates",J{{"id",id},{"end",erase->a}});
                if (right) {J tail=old;tail["start"]=erase->b;push(0,"add",std::move(tail));}
            } else if (right) push(0,"updates",J{{"id",id},{"start",erase->b}});
            else if (move) keep_moving_id=true;
            else push(0,"delete",id);
        }
        if (dest) {
            const double start=landing(a),end=landing(b);
            need(end>start,"目标音符时长低于当前坐标精度；未应用编辑。");
            J n=keep_moving_id?J{{"id",row.at("id")}}:row;
            n["start"]=start;n["end"]=end;
            push(0,keep_moving_id?"updates":"add",std::move(n));
        }
    }
    std::map<int,int> levels,entering;
    std::set<int> exact_start;
    for (const auto& v:list(clip,"entry")) {
        t::array(v);need(v.size()==2,"Invalid clipboard controller state");
        const int lane=midi(v.at(0),0,2047,"Invalid clipboard lane");
        need(scope.has(lane)&&range_held(lane) && levels.emplace(lane,midi(v.at(1),0,127,"Invalid clipboard value")).second,
             "Invalid clipboard entry state");
    }
    if (dest) for (const auto& v:levels) if (v.first/16==64 && v.second!=0) entering.emplace(v);
    for (const auto& row:list(clip,"controls")) {
        const int lane=selection_lane(t::object(row)); (void)raw_level(row);
        const double q=sec(required(row,"seconds"));
        need(scope.has(lane) && q<length,"Controller outside copied interval");
        if (erase&&!move) push(1,"delete",row.at("id"));
        if (!dest) continue;
        const double x=landing(q);
        need(dest->contains(x),"目标控制点低于当前坐标精度；未应用编辑。");
        if (range_held(lane)) {
            const auto it=levels.find(lane);
            entering.emplace(lane,it!=levels.end()?it->second:lane/16==64?0:-1);
            if (x==dest->a) exact_start.insert(lane);
        }
        J out=move?J{{"id",row.at("id")}}:row;out["seconds"]=x;
        push(1,move?"updates":"add",std::move(out));
    }
    const RangeProjection window=dest?RangeProjection{dest->a,dest->b}:RangeProjection{0.,0.};
    batch.stage(0,ops[0]);batch.stage(1,ops[1],&window);
    const RangeProjection boundary_only{0.,0.};
    batch.stage(1,selection_guards(clock.d,batch.current(),scope,erase,dest,entering,exact_start),&boundary_only);
}

void plan_selection_range(aed_session& s,Plan& p,const J& op,const J& trusted,bool preview,const std::string& mint) {
    const auto& c=t::object(required(op,"command"));
    const auto action=t::text(required(c,"action"),"Unknown selection command");
    const J ctx=semantic_context(s,op,trusted,false);
    const SelectionClock clock{*s.doc,ctx};
    bool bars=clock.bars();
    const bool gesture=action=="gesture", paste=action=="paste";
    const auto part=gesture?t::text(required(c,"part"),"Invalid range gesture"):std::string();
    const bool draw=gesture && part=="draw", extend_draw=gesture && part=="extend";
    const J& active=draw||paste?t::nil():active_selection(s,c);
    const auto scope=paste?SelectionScope{"notes",-1}:selection_scope(draw?required(c,"scope"):active.at("scope"));
    const SelectionSpan source=active.is_null()?clock.at(0.,0.):selection_span(active);
    const J cfg=t::object(get(c,"snap",J::object()));
    J& warnings=p.delta["semantic_warnings"];warnings=J::array();
    if (action=="clear") {p.selection=J();p.delta["semantic_preview"]=J{{"selection",nullptr}};return;}
    const auto select=[&](SelectionSpan span,const SelectionScope& lanes){
        p.selection=span.b>span.a?span.wire(lanes):J();
        p.delta["semantic_preview"]=J{{"selection",*p.selection}};
    };
    if (action=="scope") {select(source,selection_scope(required(c,"scope")));return;}
    if (action=="info" || action=="nudge") {
        double a=source.first(bars),b=source.last(bars);
        if (action=="info") {
            const auto field=t::text(required(c,"field"),"Invalid selection field");
            const double value=sec(required(c,"value"));
            if (field=="start") a=value;
            else if (field=="end") b=value;
            else if (field=="length") b=t::finite(a+value);
            else need(false,"Invalid selection field");
            need(a<=b&&b<=clock.extent(bars),"请输入曲目范围内的起点、终点或长度。");
            // A literal endpoint assignment must not round-trip the other edge.
            const auto span=clock.translate(bars,a,b-a,false,field=="start"?nullptr:&source.a);
            if (field=="start") {
                const double start=std::min(source.b,span.a);
                select(bars?SelectionSpan{start,source.b,a,source.z}:clock.at(start,source.b),scope);
            } else select(span,scope);
        } else {
            const auto which=t::text(required(c,"part"),"Invalid selection nudge");
            const int dir=midi(required(c,"direction"),-1,1,"Invalid nudge direction");
            need(dir!=0,"Invalid nudge direction");
            const auto step=[&](double q){return std::clamp(t::finite(q+dir*clock.unit(cfg)),0.,clock.extent(true));};
            if (which=="move") select(clock.translate(bars,clock.coord(bars,clock.seconds(true,step(source.q))),b-a,true),scope);
            else if (which=="start") { const double q=std::min(source.z,step(source.q));select({q==source.q?source.a:q==source.z?source.b:clock.seconds(true,q),source.b,q,source.z},scope); }
            else if (which=="end") { const double z=std::max(source.q,step(source.z));select({source.a,z==source.z?source.b:z==source.q?source.a:clock.seconds(true,z),source.q,z},scope); }
            else need(false,"Invalid selection nudge");
        }
        return;
    }
    const bool body=part=="move_body" || part=="copy_body";
    std::optional<SelectionSpan> destination;
    const bool move=gesture && part=="move_body";
    if (gesture) {
        const auto& press=t::object(required(c,"press"));const auto& point=t::object(required(c,"point"));
        const double x=t::number(required(press,"x")),y=t::number(required(point,"x"));
        // Ctrl/Meta is sampled live. Older clients supplied only bypass,
        // which stays the latched fallback (the unchanged B-at-press behavior).
        const bool bypass=get(press,"latched_bypass",get(press,"bypass",J(false))).get<bool>();
        if (draw || extend_draw) {
            const double a=clock.snap(clock.raw(x),press,cfg,bypass,scope);
            const double b=x==y?a:clock.snap(clock.raw(y),point,cfg,bypass,scope,a);
            if (extend_draw) {
                select(clock.at(std::min({source.a,a,b}),std::max({source.b,a,b})),scope);
            } else select(clock.at(std::min(a,b),std::max(a,b)),scope);
            return;
        }
        const bool left=part=="start" || part=="extend_start",right=part=="end" || part=="extend_end";
        need(left||right||body,"Invalid selection gesture part");
        const bool extend=part=="extend_start" || part=="extend_end";
        const double ref=right?source.b:source.a;
        const double raw=extend?clock.raw(y):x==y?ref:clock.raw(clock.x(ref)+(y-x));
        const double at=!extend&&raw==ref?ref:clock.snap(raw,point,cfg,bypass,scope,ref,body?&source:nullptr);
        if (!body) {
            const double fixed=left?source.b:source.a;
            const double moving=extend?(left?std::min(at,fixed):std::max(at,fixed)):at;
            select(clock.at(std::min(fixed,moving),std::max(fixed,moving)),scope);return;
        }
        destination=clock.translate(bars,clock.coord(bars,at),source.length(bars),true,&at);
    }
    need(gesture||paste||action=="copy"||action=="cut"||action=="delete"||action=="duplicate","Unknown selection command");
    if (!preview && action=="copy") need(false,"Copy is a read-only preview request");
    const bool erase=move || action=="cut" || action=="delete";
    if (!paste) {
        p.delta["semantic_preview"]=J{{"selection",source.wire(scope)}};
        // Still presses and returns to the source are not splits or copies.
        if (source.a==source.b || (body&&destination->a==source.a)) return;
        if (action=="duplicate") destination=clock.translate(bars,source.last(bars),source.length(bars),false,&source.b);
    }
    J clip;
    if (paste) {
        const auto& wire=required(t::object(required(op,"context")),"clipboard");
        clip=parse(bytes(t::text(wire,"Expected a native range clipboard string")));
        t::object(clip);
        need(text_is(get(clip,"type"),"selection_range") && midi(required(clip,"version"),1,1,"Unsupported range clipboard version")==1,
             "Not a range clipboard");
        bars=text_is(get(clip,"basis"),"bars");
        need(bars||text_is(get(clip,"basis"),"seconds"),"Invalid clipboard basis");
        const auto& at=t::text(required(c,"at"),"Invalid paste location");
        need(at=="origin" || at=="cursor","Invalid paste location");
        const double cursor=sec(get(ctx,"cursor_seconds",J(0)));
        const double start=at=="origin"?sec(required(clip,"origin")):clock.coord(bars,cursor);
        const double length=sec(required(clip,"length"));
        const auto& notes=t::array(required(clip,"notes"));const auto& controls=t::array(required(clip,"controls"));
        // At most one boundary value for each 16 x 128 MIDI controller lane.
        need(list(clip,"entry").size()<=2048,"Range clipboard exceeds MIDI lane domain",AEC_LIMIT);
        if (length==0.) {need(notes.empty()&&controls.empty(),"Nonempty zero-length clipboard");p.delta["semantic_preview"]=J::object();return;}
        destination=clock.translate(bars,start,length,false,at=="cursor"?&cursor:nullptr);
    } else clip=selection_material(clock,source,scope,bars);
    if (action=="copy" || action=="cut") {
        auto& meta=p.delta.at("semantic_preview");
        meta["clipboard_type"]="selection_range";
        meta["clipboard_json"]=lossless::dump(clip);
        if (action=="copy") return;
    }
    const auto lanes=paste?selection_scope(required(clip,"scope")):scope;
    SelectionBatch batch{s,p,preview,mint};
    selection_transfer(batch,clock,clip,lanes,erase?&source:nullptr,destination?&*destination:nullptr,move);
    if (destination) select(*destination,lanes);
}

void prepare_selection_view(aed_session& s,Plan& p) {
    const auto& to=p.target?*p.target:*s.doc;
    if (p.step && p.edge->range_view) {
        const auto& view=*p.edge->range_view;
        p.selection=p.step<0?view.before:view.after;
    }
    if (!p.selection && p.content && s.selection.is_object()) {
        if (p.edge->replace||!py_equal(to.header.at("source_key"),s.doc->header.at("source_key"))) p.selection=J();
        else if (to.env==s.doc->env) p.selection=s.selection;
        else {
            const J ctx=J::object(); const SelectionClock clock{to,ctx};
            const auto old=selection_span(s.selection);
            const auto span=clock.at(clock.clamp(old.a),clock.clamp(old.b));
            p.selection=span.b>span.a?span.wire(selection_scope(s.selection.at("scope"))):J();
        }
    }
    if (p.selection && p.selection->is_object()) {
        (*p.selection)["token"]=std::to_string(s.stamp.epoch)+":"+std::to_string(p.token.request);
        (*p.selection)["revision"]=revision(to.header.at("revision"));
    }
}
