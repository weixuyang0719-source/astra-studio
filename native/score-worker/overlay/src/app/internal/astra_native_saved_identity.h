#pragma once

#include <cstddef>
#include <cstdint>
#include <vector>

namespace mu::app::astra_native_probe {

// A transport revision identifies an event, not score contents. Undo and
// redo revisit identities; a new macro after undo cannot revisit the deleted
// redo branch even if it brings the native stack to the same numeric index.
class SavedScoreIdentity final
{
public:
    SavedScoreIdentity(std::size_t nativeIndex, bool hasSavedFile)
    {
        reset(nativeIndex, hasSavedFile);
    }

    void reset(std::size_t nativeIndex, bool hasSavedFile)
    {
        m_states.clear();
        fillTo(nativeIndex);
        m_saved = hasSavedFile ? m_states[nativeIndex] : 0;
    }

    void observe(std::size_t before, std::size_t after, bool navigation, bool sameIndexMutation)
    {
        if (!navigation && (before != after || sameIndexMutation)) {
            // Native macros are usually appended. A grouped transaction can
            // finish at/below its starting index, notably after text merges.
            const std::size_t firstInvalid = after > before ? before + 1 : after;
            if (firstInvalid < m_states.size()) {
                m_states.resize(firstInvalid);
            }
        }
        fillTo(after);
    }

    void markSaved(std::size_t nativeIndex)
    {
        fillTo(nativeIndex);
        m_saved = m_states[nativeIndex];
    }

    bool dirty(std::size_t nativeIndex) const
    {
        return nativeIndex >= m_states.size() || m_saved == 0 || m_states[nativeIndex] != m_saved;
    }

private:
    void fillTo(std::size_t index)
    {
        while (m_states.size() <= index) {
            m_states.push_back(++m_next);
        }
    }

    std::vector<std::uint64_t> m_states;
    std::uint64_t m_next = 0;
    std::uint64_t m_saved = 0;
};

} // namespace mu::app::astra_native_probe

