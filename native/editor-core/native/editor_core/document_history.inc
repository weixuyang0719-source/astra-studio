// Included inside document_detail. Checkpoint history is a head layout plus
// two walks away from that head. Only changed rows and header values are stored.
// A walk is nearest first on disk; a session stack is nearest last.

using HistoryStack = std::vector<std::shared_ptr<const History>>;

const std::shared_ptr<const J>* history_field(const Header& h,const std::string& name) {
    for (const auto& f:h.fields) if (f.first==name) return &f.second;
    return nullptr;
}
bool history_table_field(std::string_view key) {
    return key=="notes" || key=="controls" || key=="deleted_ids";
}

// Replacements and legacy imports already visit the whole document. Compute
// their sparse descriptions once, not every time an autosave walks the stacks.
void describe_replacement(History& edge) {
    for (unsigned g=0;g<2;++g) {
        const auto& a=edge.before->events[g].rows;
        const auto& b=edge.after->events[g].rows;
        auto& keys=edge.keys[g];
        for (const auto& v:a) if (!b.count(v.first)) keys.push_back(v.first);
        for (const auto& v:b) {
            const auto* old=a.find(v.first);
            if (!old || old->cc_tag!=v.second.cc_tag || !disk_equal(old->json,v.second.json))
                keys.push_back(v.first);
        }
        std::sort(keys.begin(),keys.end());
    }
    edge.deleted_prefix=deleted_prefix(*edge.before,*edge.after);
}

J history_layout(const Document& d) {
    J layout=J::array();
    for (const auto& g:d.events) {
        J keys=J::array();
        keys.get_ref<J::array_t&>().reserve(g.rows.size());
        for (const auto& v:g.rows) keys.push_back(J::array({v.first,v.second.cc_tag}));
        layout.push_back(J::array({g.next,std::move(keys)}));
    }
    return layout;
}

J history_delta(const Document& from,const Document& to,const History& edge) {
    J fields=J::array(), partitions=J::array(), tail=J::array();
    // The descriptor list carries key order, including unset fields and the
    // array placeholders. A one-element descriptor reuses the preceding value.
    for (const auto& f:to.header.fields) {
        J field=J::array({f.first});
        if (f.second) {
            const auto* old=history_field(from.header,f.first);
            if (!old || !*old || !disk_equal(**old,*f.second)) field.push_back(*f.second);
        }
        fields.push_back(std::move(field));
    }
    for (unsigned g=0;g<2;++g) {
        J changes=J::array();
        for (const K key:edge.keys[g]) {
            if (const auto* item=to.events[g].rows.find(key))
                changes.push_back(J::array({key,item->cc_tag,item->json}));
            else if (from.events[g].rows.count(key)) changes.push_back(J::array({key}));
        }
        partitions.push_back(J::array({to.events[g].next,std::move(changes)}));
    }
    for (size_t i=edge.deleted_prefix;i<to.deleted.size();++i) tail.push_back(to.deleted.at(i));
    return J{{"fields",std::move(fields)},{"groups",std::move(partitions)},
             {"deleted",J::array({edge.deleted_prefix,std::move(tail)})},
             {"group",edge.group<0?2u:static_cast<unsigned>(edge.group)},
             {"replace",edge.replace || edge.group==2}, {"mixed_range",edge.group==2}};
}

J encode_history(const aed_snapshot& frozen) {
    need(static_cast<bool>(frozen.history),"Snapshot does not contain checkpoint history");
    const auto walk=[](const HistoryStack& stack,bool undo) {
        J out=J::array();
        out.get_ref<J::array_t&>().reserve(stack.size());
        for (auto i=stack.rbegin();i!=stack.rend();++i) {
            const auto& e=**i;
            out.push_back(undo?history_delta(*e.after,*e.before,e):history_delta(*e.before,*e.after,e));
        }
        return out;
    };
    return J{{"version",1},{"layout",history_layout(*frozen.doc)},
             {"undo",walk(frozen.history->undo,true)},{"redo",walk(frozen.history->redo,false)}};
}

Header restore_header(const Header& old,const J& fields) {
    Header h;
    for (const auto& f:t::array(fields)) {
        t::array(f);
        need(f.size()==1 || f.size()==2,"Invalid history field");
        const auto& name=t::text(f.at(0),"Invalid history field name");
        need(!view_key(name) && !h.has(name),"Duplicate or transient history field");
        std::shared_ptr<const J> value;
        if (history_table_field(name)) need(f.size()==1,"Invalid history table field");
        else if (f.size()==2) value=std::make_shared<const J>(f.at(1));
        else {
            const auto* previous=history_field(old,name);
            need(previous && *previous,"Missing history field reference");
            value=*previous;
        }
        h.fields.emplace_back(name,std::move(value));
    }
    for (const char* k:{"notes","controls","deleted_ids"}) need(h.has(k),"Missing history table field");
    (void)revision(h.at("revision"));
    return h;
}

std::shared_ptr<const Document> restore_delta(const Document& from,const J& raw,int resolution,History& edge) {
    t::object(raw);
    edge.replace=required(raw,"replace").get<bool>();
    const K group=history_u64(required(raw,"group"));
    need(group<=2 && (!edge.replace || group==2),"Invalid history view group");
    // Older readers can use their existing replacement view for this edge.
    const bool mixed=get(raw,"mixed_range",J(false)).get<bool>();
    need(!mixed || (group==2 && edge.replace),"Invalid mixed history edge");
    if (mixed) { edge.group=2; edge.replace=false; }
    else edge.group=group==2?-1:static_cast<int>(group);
    auto to=std::make_shared<Document>(from);
    to->header=restore_header(from.header,required(raw,"fields"));
    to->lane.reset(); // A historical clock/root is usable without an AEC projection.
    const bool same=same_clock(from,to->header);
    if (!same) to->env=clock_environment(to->header);
    const auto& partitions=t::array(required(raw,"groups"));
    need(partitions.size()==2,"Invalid history row partitions");
    for (unsigned g=0;g<2;++g) {
        const auto& part=t::array(partitions.at(g));
        need(part.size()==2,"Invalid history row partition");
        auto& out=to->events[g];
        const K next=history_u64(part.at(0));
        const auto& changes=t::array(part.at(1));
        need(next!=0,"Invalid next history row key");
        if (!edge.replace && !mixed && group!=g)
            need(changes.empty() && next==out.next,"History view omits row changes");
        K previous=0;
        // Remove old identities first. A replacement may move an identity
        // from one key to another without deleting that identity in the view.
        for (const auto& change:changes) {
            t::array(change);
            need(change.size()==1 || change.size()==3,"Invalid history row change");
            const K key=history_u64(change.at(0));
            need(key>previous && key<limit,"Unordered history row keys");
            previous=key;
            edge.keys[g].push_back(key);
            if (const auto* old=out.rows.find(key)) {
                out.ids.erase(old->ident);
                out.rows.erase(key);
            } else need(change.size()==3,"History deletes a missing row");
        }
        for (const auto& change:changes) if (change.size()==3) {
            const K key=history_u64(change.at(0));
            need(key<next && change.at(1).is_boolean(),"Invalid history row layout");
            put_slot(out,key,change.at(2),change.at(1).get<bool>());
            if (same) (void)row_for(out.rows.at(key),key,g,*to->env,resolution);
        }
        const auto* last=out.rows.last_key();
        need(!last || *last<next,"Invalid history row key frontier");
        out.next=next;
    }
    const auto& deleted=t::array(required(raw,"deleted"));
    need(deleted.size()==2,"Invalid history tombstone change");
    const auto keep=history_u64(deleted.at(0));
    need(keep<=from.deleted.size(),"Invalid history tombstone prefix");
    edge.deleted_prefix=static_cast<size_t>(keep);
    truncate_deleted(*to,edge.deleted_prefix);
    for (const auto& id:t::array(deleted.at(1))) append_deleted(*to,id);
    if (!edge.replace) {
        need(keep==std::min(from.deleted.size(),to->deleted.size()),"History view omits tombstone changes");
        need(edge.group>=0 || from.deleted.size()==to->deleted.size(),"Unexpected history tombstone change");
    }
    if (!same) validate_rows(to->events,*to->env,resolution);
    return to;
}

void remember_revision(aed_session& s,const std::string& name) {
    need(!s.revisions.count(name),"Duplicate revision in document history",AEC_DUPLICATE);
    s.revisions.set(name,true);
}

void restore_stack(aed_session& s,const J& walk,HistoryStack& stack,bool undo) {
    t::array(walk);
    stack.reserve(walk.size());
    auto from=s.doc;
    for (const auto& raw:walk) {
        auto edge=std::make_shared<History>();
        auto to=restore_delta(*from,raw,s.resolution,*edge);
        remember_revision(s,revision(to->header.at("revision")));
        edge->before=undo?to:from;
        edge->after=undo?from:to;
        stack.push_back(std::move(edge));
        from=std::move(to);
    }
    std::reverse(stack.begin(),stack.end());
}

void open_history(aed_session& s,J& packet) {
    s.index=std::move(packet.at("index"));
    t::object(s.index);
    const auto current=revision(required(s.index,"revision"));
    const auto& undo=list(s.index,"undo");
    const auto& redo=list(s.index,"redo");
    const bool legacy=get(packet,"legacy_history",J(false)).get<bool>();
    const auto* compact=t::find(packet,"history");
    if (!undo.empty() || !redo.empty()) {
        need(legacy && !compact,"Indexed history requires the legacy import stream");
        auto source=std::make_unique<LegacyImport>();
        source->cursor=undo.size();
        const auto add=[&](const J& id) {
            const auto& name=revision(id);
            remember_revision(s,name);
            source->labels.push_back(name);
        };
        for (const auto& id:undo) add(id);
        add(J(current));
        for (size_t i=redo.size();i!=0;--i) add(redo.at(i-1));
        s.undo.reserve(undo.size());
        s.redo.reserve(redo.size());
        s.doc=load(std::move(packet.at("document")),s.resolution,nullptr,nullptr,source->cursor==0);
        need(rev(s)==source->labels.front(),"Legacy revision body does not match its index");
        if (source->cursor==0) source->center=s.doc;
        s.legacy_import=std::move(source);
    } else {
        const J* layout=nullptr;
        if (compact) {
            t::object(*compact);
            need(!legacy && history_u64(required(*compact,"version"))==1,"Unsupported history codec");
            layout=&t::array(required(*compact,"layout"));
            need(layout->size()==2,"Invalid checkpoint head layout");
        }
        s.doc=load(std::move(packet.at("document")),s.resolution,layout);
        need(current==rev(s),stale_message,AEC_STALE);
        remember_revision(s,current);
        if (compact) {
            restore_stack(s,required(*compact,"undo"),s.undo,true);
            restore_stack(s,required(*compact,"redo"),s.redo,false);
        }
    }
    // Consumed by native history (or its unopened import builder). Do not copy
    // the legacy index arrays into every range/edit receipt.
    s.index.erase("undo");
    s.index.erase("redo");
}

void append_legacy(aed_session& s,J raw) {
    auto& import=*s.legacy_import;
    const size_t p=import.position+1;
    t::object(raw);
    need(revision(required(raw,"revision"))==import.labels.at(p),"Legacy revision body does not match its index");
    std::shared_ptr<const Document> next=load(std::move(raw),s.resolution,nullptr,s.doc.get(),p==import.cursor);
    auto edge=std::make_shared<History>();
    edge->before=s.doc;
    edge->after=next;
    edge->replace=true;
    describe_replacement(*edge);
    // All fallible work precedes these updates. Both vectors were reserved at open.
    (p<=import.cursor?s.undo:s.redo).push_back(std::move(edge));
    s.doc.swap(next);
    import.position=p;
    if (p==import.cursor) import.center=s.doc;
    if (p+1==import.labels.size()) {
        std::reverse(s.redo.begin(),s.redo.end());
        s.doc.swap(import.center);
        s.legacy_import.reset();
    }
}
