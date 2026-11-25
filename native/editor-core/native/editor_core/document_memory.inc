// Included inside document_detail after the session and its liveness helpers.
// No serialization or allocation is allowed in publish_memory().
void reserve_history_slot(HistoryStack& stack) {
    if (stack.size()<stack.capacity()) return;
    const auto remaining=stack.max_size()-stack.size();
    need(remaining!=0,"History exceeds container capacity",AEC_LIMIT);
    // Geometric growth; reserve(size+1) on every edit would be quadratic.
    stack.reserve(stack.size()+std::min(remaining,std::max(size_t{1},stack.size())));
}

J head(const aed_session& s,const Plan* p=nullptr) {
    const auto& doc=p && p->target?*p->target:*s.doc;
    const bool advanced=p && !p->index.is_null();
    const auto& idx=advanced?p->index:s.index;
    const K seq=s.stamp.sequence+(p && !p->metadata_noop && (!p->selection_op || p->content)?1:0);
    auto undo=s.undo.size(), redo=s.redo.size();
    if (p && p->content) {
        if (p->step<0) { --undo; ++redo; }
        else if (p->step>0) { ++undo; --redo; }
        else { ++undo; redo=0; }
    }
    J result{{"project_id",s.project},{"revision",revision(doc.header.at("revision"))},
        {"stamp",J::array({s.stamp.epoch,seq})},{"applied_sequence",advanced?seq:s.applied_sequence},
        {"can_undo",undo!=0},{"can_redo",redo!=0},{"undo_count",undo},{"redo_count",redo},
        {"range",get(idx,"range",doc.header.at("range"))},
        {"source_key",doc.header.at("source_key")},{"duration",doc.env->duration},
        {"notes",doc.events[0].rows.size()},{"controls",doc.events[1].rows.size()}};
    const auto& selection=p && p->selection ? *p->selection : s.selection;
    if (!selection.is_null()) result["selection_range"]=selection;
    if (idx.contains("instrument_state")) result["instrument_state"]=idx.at("instrument_state");
    return result;
}

int direction(const J& op) {
    const auto& d=t::text(required(op,"direction"),"未知历史操作。");
    need(d=="undo" || d=="redo","未知历史操作。");
    return d=="undo"?-1:1;
}
const auto& history_from(const aed_session& s,int step) { return step<0?s.undo:s.redo; }
J history_target(const aed_session& s,const J& op) {
    const auto step=direction(op);
    const auto& stack=history_from(s,step);
    if (stack.empty()) return nullptr;
    auto idx=s.index;
    idx["revision"]=(step<0?stack.back()->before:stack.back()->after)->header.at("revision");
    return idx;
}

J view_changes(const Document& from,const Document& to,const History& edge) {
    if (edge.replace) return J{{"replace",true}};
    J fields=J::object(), unset=J::array();
    for (const auto& f:to.header.fields) {
        if (!f.second || f.first=="range") continue;
        const auto old=std::find_if(from.header.fields.begin(),from.header.fields.end(),
                                    [&](const auto& item){return item.first==f.first;});
        if (old==from.header.fields.end() || old->second!=f.second) fields[f.first]=*f.second;
    }
    for (const auto& f:from.header.fields)
        if (f.second && f.first!="range" && !to.header.has(f.first)) unset.push_back(f.first);
    J result{{"set",std::move(fields)},{"unset",std::move(unset)}};
    if (edge.group<0) return result;
    const auto splice=[&](unsigned g) {
        const auto& a=from.events[g].rows;
        const auto& b=to.events[g].rows;
        J erase=J::array(), write=J::array();
        // Keys are in list order, so writes are also in final position order.
        for (K key:edge.keys[g]) {
            const auto* before=a.find(key);
            const auto* after=b.find(key);
            if (!after && before) erase.push_back(a.rank(key));
            if (after) write.push_back(J::array({b.rank(key),before==nullptr,after->json}));
        }
        return J{{"group",groups[g]},{"erase",std::move(erase)},{"write",std::move(write)}};
    };
    if (edge.group==2) result["row_groups"]=J::array({splice(0),splice(1)});
    else result["rows"]=splice(static_cast<unsigned>(edge.group));
    auto& tomb_view=edge.group==2?result:result.at("rows");
    const auto prefix=std::min(from.deleted.size(),to.deleted.size());
    J tail=J::array();
    for (size_t i=prefix;i<to.deleted.size();++i) tail.push_back(to.deleted.at(i));
    tomb_view["deleted_prefix"]=prefix;
    tomb_view["deleted_tail"]=std::move(tail);
    return result;
}

void prepare_memory(aed_session& s,Plan& p) {
    if (!p.content) return;
    // PREPARE can subsequently be accepted or discarded but cannot be
    // interleaved with a different mutation. Capacity is guaranteed either way.
    reserve_history_slot(p.step<0?s.redo:s.undo);
    if (!p.step) {
        auto edge=std::make_shared<History>();
        edge->before=s.doc;
        edge->replace=p.delta.contains("replacement");
        auto next=std::move(p.replacement);
        if (!next) next=std::make_unique<Document>(*s.doc);
        if (p.header) next->header.fields.swap(p.header->fields);
        if (p.env) { next->env=std::move(p.env); next->lane=std::move(p.lane); }
        if (p.events) {
            edge->group=static_cast<int>(p.events->group);
            auto& keys=edge->keys[p.events->group];
            for (const auto key:p.events->remove) keys.push_back(key);
            for (const auto& row:p.events->put) keys.push_back(row.first);
            std::sort(keys.begin(),keys.end());
            keys.erase(std::unique(keys.begin(),keys.end()),keys.end());
            p.events->install(*next); // path-copy, not installation into the live head
            p.events->core_patch.accept(); // accepts only the private projection fork
        }
        if (p.selection_op || !p.batch_keys[0].empty() || !p.batch_keys[1].empty()) {
            if (p.selection_op && p.selection) edge->range_view=RangeHistory{s.selection,*p.selection};
            const bool notes=!p.batch_keys[0].empty(), controls=!p.batch_keys[1].empty();
            edge->group=notes && controls ? 2 : controls ? 1 : 0;
            for (unsigned g=0;g<2;++g)
                edge->keys[g].assign(p.batch_keys[g].begin(),p.batch_keys[g].end());
        }
        p.target=std::move(next);
        edge->after=p.target;
        if (edge->replace) describe_replacement(*edge);
        else edge->deleted_prefix=std::min(edge->before->deleted.size(),edge->after->deleted.size());
        p.edge=std::move(edge);
        p.revisions=s.revisions;
        for (const auto& dropped:s.redo)
            p.revisions.erase(revision(dropped->after->header.at("revision")));
        p.revisions.set(revision(p.target->header.at("revision")),true);
    }
    p.view=view_changes(*s.doc,*p.target,*p.edge);
}

void publish_memory(aed_session& s,Plan& p) noexcept {
    if (p.content) {
        if (p.step<0) { s.redo.push_back(p.edge); s.undo.pop_back(); }
        else if (p.step>0) { s.undo.push_back(p.edge); s.redo.pop_back(); }
        else {
            s.redo.clear();
            s.undo.push_back(p.edge);
            s.revisions.swap(p.revisions);
        }
        s.doc.swap(p.target);
        ++s.content_sequence;
        s.planner_view.reset();
        s.reopened_lane.reset();
    }
    if (p.selection) s.selection.swap(*p.selection);
    // Keep an existing playback capture valid across transient selection edits.
    if (!p.metadata_noop && (!p.selection_op || p.content)) ++s.stamp.sequence;
    if (!p.index.is_null()) {
        s.index.swap(p.index);
        s.applied_sequence=s.stamp.sequence;
    }
    if (p.clear_draft) { J empty; s.draft.swap(empty); }
}

J playback(const aed_snapshot& f) {
    const auto& d=*f.doc;
    // Use the actual controller value, not the private CC normalization tag.
    J notes=J::array(), cc=J::array();
    for (const auto& item:d.events[0].rows) {
        const auto r=row_for(item.second,item.first,AEC_NOTES,*d.env,f.resolution);
        notes.push_back(J{{"start",r.time},{"end",r.end},{"pitch",r.data1},
                          {"velocity",r.data2},{"channel",r.channel}});
    }
    for (const auto& item:d.events[1].rows) {
        const auto& row=item.second.json;
        cc.push_back(J{{"seconds",sec(row.at("seconds"))},{"number",row.at("number")},
                       {"value",row.at("value")},{"channel",get(row,"channel",J(0))}});
    }
    J out=f.head;
    out.erase("selection_range");
    out["notes"]=std::move(notes); out["controls"]=std::move(cc);
    out["tempo"]=d.header.at("tempo"); out["other_events"]=d.header.at("other_events");
    return out;
}
