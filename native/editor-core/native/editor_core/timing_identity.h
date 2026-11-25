#pragma once

#include <algorithm>
#include <array>
#include <cstddef>
#include <cstdint>
#include <cstring>
#include <string>
#include <string_view>

namespace astra::editor::timing_detail {

// FIPS 180-4 SHA-256 over bytes. Kept private to avoid an additional runtime
// dependency for revision-bound bar-line IDs.
class Sha256 {
    using U = std::uint32_t;
    std::array<U, 8> h_{0x6a09e667u, 0xbb67ae85u, 0x3c6ef372u, 0xa54ff53au,
                        0x510e527fu, 0x9b05688cu, 0x1f83d9abu, 0x5be0cd19u};
    std::array<unsigned char, 64> block_{};
    std::uint64_t count_ = 0;
    std::size_t used_ = 0;

    static U rotr(U v, unsigned n) { return (v >> n) | (v << (32 - n)); }
    void compress() {
        static constexpr U k[] = {
            0x428a2f98u,0x71374491u,0xb5c0fbcfu,0xe9b5dba5u,0x3956c25bu,0x59f111f1u,0x923f82a4u,0xab1c5ed5u,
            0xd807aa98u,0x12835b01u,0x243185beu,0x550c7dc3u,0x72be5d74u,0x80deb1feu,0x9bdc06a7u,0xc19bf174u,
            0xe49b69c1u,0xefbe4786u,0x0fc19dc6u,0x240ca1ccu,0x2de92c6fu,0x4a7484aau,0x5cb0a9dcu,0x76f988dau,
            0x983e5152u,0xa831c66du,0xb00327c8u,0xbf597fc7u,0xc6e00bf3u,0xd5a79147u,0x06ca6351u,0x14292967u,
            0x27b70a85u,0x2e1b2138u,0x4d2c6dfcu,0x53380d13u,0x650a7354u,0x766a0abbu,0x81c2c92eu,0x92722c85u,
            0xa2bfe8a1u,0xa81a664bu,0xc24b8b70u,0xc76c51a3u,0xd192e819u,0xd6990624u,0xf40e3585u,0x106aa070u,
            0x19a4c116u,0x1e376c08u,0x2748774cu,0x34b0bcb5u,0x391c0cb3u,0x4ed8aa4au,0x5b9cca4fu,0x682e6ff3u,
            0x748f82eeu,0x78a5636fu,0x84c87814u,0x8cc70208u,0x90befffau,0xa4506cebu,0xbef9a3f7u,0xc67178f2u
        };
        U w[64];
        for (std::size_t i = 0; i < 16; ++i) {
            const auto* b = block_.data() + 4*i;
            w[i] = (U(b[0]) << 24) | (U(b[1]) << 16) | (U(b[2]) << 8) | U(b[3]);
        }
        for (std::size_t i = 16; i < 64; ++i) {
            const U x = w[i-15], y = w[i-2];
            w[i] = w[i-16] + (rotr(x,7)^rotr(x,18)^(x>>3)) + w[i-7] + (rotr(y,17)^rotr(y,19)^(y>>10));
        }
        U a=h_[0], b=h_[1], c=h_[2], d=h_[3], e=h_[4], f=h_[5], g=h_[6], h=h_[7];
        for (std::size_t i = 0; i < 64; ++i) {
            const U t1 = h + (rotr(e,6)^rotr(e,11)^rotr(e,25)) + ((e&f)^(~e&g)) + k[i] + w[i];
            const U t2 = (rotr(a,2)^rotr(a,13)^rotr(a,22)) + ((a&b)^(a&c)^(b&c));
            h=g; g=f; f=e; e=d+t1; d=c; c=b; b=a; a=t1+t2;
        }
        h_[0]+=a; h_[1]+=b; h_[2]+=c; h_[3]+=d; h_[4]+=e; h_[5]+=f; h_[6]+=g; h_[7]+=h;
    }
public:
    void write(std::string_view s) {
        count_ += static_cast<std::uint64_t>(s.size());
        while (!s.empty()) {
            const auto n = std::min(s.size(), block_.size()-used_);
            std::memcpy(block_.data()+used_, s.data(), n);
            s.remove_prefix(n);
            used_ += n;
            if (used_ == block_.size()) { compress(); used_ = 0; }
        }
    }
    std::string finish() {
        const auto bits = count_*8;
        block_[used_++] = 0x80;
        if (used_ > 56) {
            std::fill(block_.begin()+used_, block_.end(), static_cast<unsigned char>(0));
            compress();
            used_ = 0;
        }
        std::fill(block_.begin()+used_, block_.begin()+56, static_cast<unsigned char>(0));
        for (unsigned i=0; i<8; ++i) block_[63-i] = static_cast<unsigned char>(bits >> (8*i));
        compress();
        static constexpr char hex[] = "0123456789abcdef";
        std::string out;
        out.reserve(64);
        for (U v : h_) for (unsigned i=0; i<8; ++i) out += hex[(v >> (28-4*i)) & 15u];
        return out;
    }
};

} // namespace astra::editor::timing_detail
