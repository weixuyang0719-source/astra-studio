#pragma once
#include <algorithm>
#include <cstddef>
#include <memory>
#include <stdexcept>
#include <utility>
#include <vector>

namespace astra::editor::document_detail {
// Path-copy AVL. Rotations share the payload as well as the unchanged subtrees.
// In particular, copying a path does not copy a vendor JSON object on that path.
template<class Key,class Value> class Table {
    using Item=std::pair<Key,Value>;
    using Entry=std::shared_ptr<const Item>;
    struct Node;
    using Root=std::shared_ptr<const Node>;
    struct Node {
        Entry entry;
        Root left,right;
        int height;
        std::size_t size;
        Node(const Entry& e,const Root& l,const Root& r):entry(e),left(l),right(r),
            height(1+std::max(h(l),h(r))),size(1+n(l)+n(r)) {}
    };
    Root root_;
    static int h(const Root& p) { return p?p->height:0; }
    static std::size_t n(const Root& p) { return p?p->size:0; }
    static Root node(const Entry& e,const Root& l,const Root& r) {
        return std::make_shared<const Node>(e,l,r);
    }
    static Root balance(const Entry& e,const Root& l,const Root& r) {
        if (h(l)>h(r)+1) {
            if (h(l->left)>=h(l->right)) return node(l->entry,l->left,node(e,l->right,r));
            const auto& p=l->right;
            return node(p->entry,node(l->entry,l->left,p->left),node(e,p->right,r));
        }
        if (h(r)>h(l)+1) {
            if (h(r->right)>=h(r->left)) return node(r->entry,node(e,l,r->left),r->right);
            const auto& p=r->left;
            return node(p->entry,node(e,l,p->left),node(r->entry,p->right,r->right));
        }
        return node(e,l,r);
    }
    static Root put(const Root& p,const Entry& e) {
        if (!p) return node(e,{},{});
        if (e->first<p->entry->first) return balance(p->entry,put(p->left,e),p->right);
        if (p->entry->first<e->first) return balance(p->entry,p->left,put(p->right,e));
        return node(e,p->left,p->right);
    }
    static Root drop(const Root& p,const Key& k) {
        if (!p) return {};
        if (k<p->entry->first) return balance(p->entry,drop(p->left,k),p->right);
        if (p->entry->first<k) return balance(p->entry,p->left,drop(p->right,k));
        if (!p->left) return p->right;
        if (!p->right) return p->left;
        auto q=p->right.get();
        while (q->left) q=q->left.get();
        return balance(q->entry,p->left,drop(p->right,q->entry->first));
    }
public:
    std::size_t size() const { return n(root_); }
    void swap(Table& other) noexcept { root_.swap(other.root_); }
    const Key* last_key() const noexcept {
        auto p=root_.get();
        if (!p) return nullptr;
        while (p->right) p=p->right.get();
        return &p->entry->first;
    }
    const Value* find(const Key& k) const {
        auto p=root_.get();
        while (p) {
            if (k<p->entry->first) p=p->left.get();
            else if (p->entry->first<k) p=p->right.get();
            else return &p->entry->second;
        }
        return nullptr;
    }
    bool count(const Key& k) const { return find(k)!=nullptr; }
    const Value& at(const Key& k) const {
        if (const auto* v=find(k)) return *v;
        throw std::out_of_range("Absent persistent table key");
    }
    void set(Key k,Value v) { root_=put(root_,std::make_shared<const Item>(std::move(k),std::move(v))); }
    void erase(const Key& k) { root_=drop(root_,k); }
    std::size_t rank(const Key& k) const {
        std::size_t offset=0;
        auto p=root_.get();
        while (p) {
            if (k<p->entry->first) p=p->left.get();
            else if (p->entry->first<k) { offset+=n(p->left)+1; p=p->right.get(); }
            else return offset+n(p->left);
        }
        throw std::out_of_range("Absent persistent table rank");
    }
    class Cursor {
        std::vector<const Node*> stack_;
        void left(const Node* p) { while (p) { stack_.push_back(p); p=p->left.get(); } }
    public:
        explicit Cursor(const Node* p) { left(p); }
        const Item& operator*() const { return *stack_.back()->entry; }
        Cursor& operator++() { auto p=stack_.back(); stack_.pop_back(); left(p->right.get()); return *this; }
        bool operator!=(const Cursor& r) const {
            return (stack_.empty()?nullptr:stack_.back())!=(r.stack_.empty()?nullptr:r.stack_.back());
        }
    };
    Cursor begin() const { return Cursor(root_.get()); }
    Cursor end() const { return Cursor(nullptr); }
};
}
