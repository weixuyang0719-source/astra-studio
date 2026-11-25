#pragma once
#include "native_json.h"
#include <charconv>
#include <cmath>
#include <cstdint>
#include <limits>
#include <map>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>

namespace astra::editor::lossless {
inline constexpr std::uint64_t bigint_subtype = 0x4153545241494e54ULL;
inline bool is_integer(const Json& j) {
    return j.is_binary() && j.get_binary().has_subtype() && j.get_binary().subtype()==bigint_subtype;
}
inline std::string integer_text(const Json& j) {
    if (!is_integer(j)) throw std::invalid_argument("Not a lossless integer");
    const auto& b=j.get_binary();
    return std::string(b.begin(),b.end());
}
inline bool integer_grammar(std::string_view s) {
    if(s.empty()) return false;
    std::size_t i=s[0]=='-' ? 1:0;
    if(i==s.size()) return false;
    if(s[i]=='0') return i+1==s.size();
    if(s[i]<'1'||s[i]>'9') return false;
    for(++i;i<s.size();++i) if(s[i]<'0'||s[i]>'9') return false;
    return true;
}
inline Json integer_atom(std::string_view s) {
    if(!integer_grammar(s)) throw std::invalid_argument("Invalid lossless integer");
    return Json::binary(std::vector<std::uint8_t>(s.begin(),s.end()),bigint_subtype);
}
inline double as_double(const Json& j) {
    const auto s=integer_text(j);
    double v=0;
    const auto r=std::from_chars(s.data(),s.data()+s.size(),v,std::chars_format::general);
    if(r.ec!=std::errc{} || r.ptr!=s.data()+s.size() || !std::isfinite(v))
        throw std::invalid_argument("Integer cannot be represented as a finite number");
    return v;
}
inline std::string float_integer_text(double v) {
    if(!std::isfinite(v) || std::trunc(v)!=v) throw std::invalid_argument("Expected an integral float");
    if(v==0.) return "0";
    char buf[1024];
    const auto r=std::to_chars(buf,buf+sizeof(buf),v,std::chars_format::fixed,0);
    if(r.ec!=std::errc{}) throw std::invalid_argument("Cannot encode integral float");
    return std::string(buf,r.ptr);
}
inline bool numeric_equal(const Json& a,const Json& b) {
    if(is_integer(a) && is_integer(b)) return integer_text(a)==integer_text(b);
    if(!is_integer(a) && !is_integer(b)) return a==b;
    const auto& big=is_integer(a)?a:b;
    const auto& other=is_integer(a)?b:a;
    if(other.is_number_float()) {
        const auto v=other.get<double>();
        return std::isfinite(v) && std::trunc(v)==v && integer_text(big)==float_integer_text(v);
    }
    return false; // atom range is strictly outside every native int/uint64 value
}
struct Input {
    std::string text;
    std::map<std::size_t,std::string> integers;
};
inline Input tokenize(std::string_view s) {
    Input out; out.text.reserve(s.size());
    std::size_t number=0;
    auto digit=[](char c){return c>='0'&&c<='9';};
    for(std::size_t i=0;i<s.size();) {
        const auto start=i;
        if(s[i]=='"') {
            ++i;
            while(i<s.size()) {char c=s[i++]; if(c=='"')break; if(c=='\\'&&i<s.size())++i;}
            out.text.append(s.substr(start,i-start));
        } else if(digit(s[i])||s[i]=='-') {
            ++i;
            while(i<s.size()&&(digit(s[i])||s[i]=='+'||s[i]=='-'||s[i]=='.'||s[i]=='e'||s[i]=='E')) ++i;
            const auto tok=s.substr(start,i-start);
            bool overflow=false;
            if(integer_grammar(tok)) {
                const auto* end=tok.data()+tok.size();
                if(tok[0]=='-') {std::int64_t v;overflow=std::from_chars(tok.data(),end,v).ec==std::errc::result_out_of_range;}
                else {std::uint64_t v;overflow=std::from_chars(tok.data(),end,v).ec==std::errc::result_out_of_range;}
            }
            if(overflow) {out.integers.emplace(number,std::string(tok));out.text+='0';}
            else out.text.append(tok);
            ++number;
        } else out.text+=s[i++];
    }
    return out;
}
class Builder : public nlohmann::json_sax<Json> {
    const std::map<std::size_t,std::string>& integers_;
    struct Frame {Json* value;std::string key;};
    std::vector<Frame> stack_;
    std::size_t number_=0;
    Json* add(Json v) {
        if(stack_.empty()) {root=std::move(v);return &root;}
        auto& f=stack_.back();
        if(f.value->is_array()) {f.value->push_back(std::move(v));return &f.value->back();}
        auto& slot=(*f.value)[f.key];slot=std::move(v);return &slot;
    }
    bool numeric(Json v) {
        const auto i=integers_.find(number_++);
        add(i==integers_.end()?std::move(v):integer_atom(i->second));return true;
    }
public:
    Json root;
    explicit Builder(const Input& in):integers_(in.integers){}
    bool null() override {add(nullptr);return true;}
    bool boolean(bool b) override {add(b);return true;}
    bool number_integer(number_integer_t v) override {return numeric(v);}
    bool number_unsigned(number_unsigned_t v) override {return numeric(v);}
    bool number_float(number_float_t v,const string_t&) override {return numeric(v);}
    bool string(string_t& s) override {add(s);return true;}
    bool binary(binary_t&) override {throw std::invalid_argument("Raw binary JSON input is not accepted");}
    bool start_object(std::size_t) override {auto* p=add(Json::object());stack_.push_back({p,{}});return true;}
    bool key(string_t& s) override {stack_.back().key=s;return true;}
    bool end_object() override {stack_.pop_back();return true;}
    bool start_array(std::size_t) override {auto* p=add(Json::array());stack_.push_back({p,{}});return true;}
    bool end_array() override {stack_.pop_back();return true;}
    bool parse_error(std::size_t,const std::string&,const nlohmann::detail::exception& e) override {
        throw std::invalid_argument(e.what());
    }
};
inline Json parse(std::string_view s) {
    auto input=tokenize(s);Builder builder(input);
    if(!Json::sax_parse(input.text,&builder)) throw std::invalid_argument("Invalid JSON");
    return std::move(builder.root);
}
inline void append(std::string& out,const Json& j,bool ascii=false) {
    if(is_integer(j)) {out+=integer_text(j);return;}
    if(j.is_binary()) throw std::invalid_argument("Unknown internal binary JSON subtype");
    if(j.is_object()) {
        out+='{';bool first=true;
        for(auto it=j.begin();it!=j.end();++it) {
            if(!first)out+=',';first=false;
            out+=Json(it.key()).dump(-1,' ',ascii);out+=':';append(out,it.value(),ascii);
        }
        out+='}';return;
    }
    if(j.is_array()) {
        out+='[';bool first=true;
        for(const auto& v:j){if(!first)out+=',';first=false;append(out,v,ascii);}
        out+=']';return;
    }
    out+=j.dump(-1,' ',ascii);
}
inline std::string dump(const Json& j,bool ascii=false) {std::string out;append(out,j,ascii);return out;}
} // namespace astra::editor::lossless
