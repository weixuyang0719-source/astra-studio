#ifndef ASTRA_EDITOR_CORE_H
#define ASTRA_EDITOR_CORE_H

#include <stddef.h>
#include <stdint.h>

#if defined(_WIN32)
#  if defined(ASTRA_EC_BUILD)
#    define AEC_API __declspec(dllexport)
#  else
#    define AEC_API __declspec(dllimport)
#  endif
#  define AEC_CALL __cdecl
#else
#  define AEC_API __attribute__((visibility("default")))
#  define AEC_CALL
#endif
#ifdef __cplusplus
#  define AEC_NOEXCEPT noexcept
extern "C" {
#else
#  define AEC_NOEXCEPT
#endif

#define AEC_ABI 1u

typedef struct aec_session aec_session;
typedef struct aec_prepared aec_prepared;

enum aec_status {
    AEC_OK = 0, AEC_INVALID, AEC_STALE, AEC_DUPLICATE, AEC_BUSY,
    AEC_CLOSED, AEC_MISSING, AEC_LIMIT, AEC_NOMEM, AEC_INTERNAL
};
enum aec_group { AEC_NOTES = 0, AEC_CONTROLS = 1 };
enum aec_bound_flag { AEC_OPENING = 1, AEC_CLOSING = 2 };
enum aec_field { AEC_TIME = 1, AEC_END = 2, AEC_DATA1 = 4, AEC_DATA2 = 8 };
enum aec_change_flag { AEC_BEFORE = 1, AEC_AFTER = 2, AEC_MANUAL = 4 };

typedef struct aec_bytes {
    const char *data;
    size_t count;
} aec_bytes;

typedef struct aec_bounds {
    uint32_t flags;
    double opening; /* inclusive note-onset minimum when enabled */
    double closing; /* exclusive note-onset maximum when enabled */
    double extent;  /* inclusive note-end / control-time maximum */
} aec_bounds;

typedef struct aec_row {
    uint64_t key;   /* host's stable ID in this group; never a list offset */
    uint64_t order; /* output only; seed/add input order is assigned by the core */
    uint64_t tick;  /* host's mapped, ties-to-even tick; only used for CC64 */
    double time;    /* note start or control time, in seconds */
    double end;     /* note end, in seconds; unused for controls */
    int32_t data1;  /* pitch or controller number */
    int32_t data2;  /* velocity or controller value */
    int32_t channel;
} aec_row;

typedef struct aec_seed {
    uint32_t size, abi;
    aec_bytes revision;
    aec_bounds bounds;
    const aec_row *notes;
    size_t note_count;
    const aec_row *controls;
    size_t control_count;
} aec_seed;

typedef struct aec_stamp { uint64_t epoch, sequence; } aec_stamp;
typedef struct aec_token { aec_stamp base; uint64_t request; } aec_token;
typedef struct aec_update { aec_row row; uint32_t mask; } aec_update;

typedef struct aec_command {
    uint32_t size, abi;
    aec_bytes revision;
    aec_token token;
    uint32_t group;
    const uint64_t *deletes;
    size_t delete_count;
    const aec_update *updates;
    size_t update_count;
    const aec_row *adds;
    size_t add_count;
} aec_command;

/* One record per materialized affected ID, in key order. BEFORE -> AFTER is
   the forward delta; AFTER -> BEFORE is its inverse, including original order
   and displaced CCs. Presence is explicit. MANUAL describes the after side;
   the host retains opaque payload and provenance on both sides as needed.
   An equal assignment can be present in a changed batch so the host can retain
   its exact scalar representation. Dropped new IDs are in normalized only. */
typedef struct aec_change {
    uint64_t key;
    uint32_t flags;
    aec_row before, after;
} aec_change;

typedef struct aec_result {
    uint32_t size, abi;
    aec_token token;
    uint32_t group, changed;
    const aec_change *changes;
    size_t change_count;
    const uint64_t *normalized; /* CC losers, in pre-removal performance-list order */
    size_t normalized_count;
} aec_result;

/* v1 envelopes require exact sizeof and AEC_ABI. Inputs are borrowed for the
   call only. Nonempty arrays must be readable for their declared length. The
   host serializes calls on a session, including close/free. There is at most
   one pending transaction; successful prepares consume a strictly increasing,
   nonzero host attempt number. Rejected prepares do not consume it.

   The prepared handle owns immutable result buffers, independent of session
   lifetime or token disposition. get_result borrows them until free_prepared.
   No caller may free the array pointers. Free each handle once (NULL is OK).
   close is idempotent and leaves a handle that returns CLOSED until freed. */
AEC_API uint32_t AEC_CALL aec_version(void) AEC_NOEXCEPT;
AEC_API int32_t AEC_CALL aec_open(const aec_seed *, aec_session **) AEC_NOEXCEPT;
/* Fork the accepted projection roots into a new, independently fenced session. */
AEC_API int32_t AEC_CALL aec_fork(const aec_session *, aec_session **) AEC_NOEXCEPT;
AEC_API int32_t AEC_CALL aec_get_stamp(const aec_session *, aec_stamp *) AEC_NOEXCEPT;
AEC_API int32_t AEC_CALL aec_prepare(aec_session *, const aec_command *, aec_prepared **) AEC_NOEXCEPT;
AEC_API int32_t AEC_CALL aec_get_result(const aec_prepared *, aec_result *) AEC_NOEXCEPT;
/* Bind the new projection label before accepting a changed prepare. This
   layer does not require persistence. Accept/discard allocate nothing. An
   accepted no-op advances sequence and needs no new label. */
AEC_API int32_t AEC_CALL aec_bind_revision(aec_session *, const aec_token *, aec_bytes) AEC_NOEXCEPT;
AEC_API int32_t AEC_CALL aec_accept(aec_session *, const aec_token *) AEC_NOEXCEPT;
AEC_API int32_t AEC_CALL aec_discard(aec_session *, const aec_token *) AEC_NOEXCEPT;
AEC_API int32_t AEC_CALL aec_close(aec_session *) AEC_NOEXCEPT;
AEC_API void AEC_CALL aec_free_session(aec_session *) AEC_NOEXCEPT;
AEC_API void AEC_CALL aec_free_prepared(aec_prepared *) AEC_NOEXCEPT;

#ifdef __cplusplus
}
#endif
#endif
