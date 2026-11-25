#ifndef ASTRA_DOCUMENT_H
#define ASTRA_DOCUMENT_H
#include "astra_editor_core.h"

#ifdef __cplusplus
extern "C" {
#endif

#define AED_ABI 2u

typedef struct aed_session aed_session;
typedef struct aed_snapshot aed_snapshot;
typedef struct aed_buffer { char *data; size_t count; } aed_buffer;
enum aed_mode { AED_QUERY, AED_PREVIEW, AED_PREPARE, AED_DRAFT, AED_APPLY };
enum aed_snapshot_mode { AED_BODY, AED_PLAYBACK, AED_HISTORY };

/* This is a separate ABI from aec (the event projection). Every input span
   is borrowed for the call, is length-delimited UTF-8 JSON unless specified
   otherwise, and may be released immediately after the call. No input pointer
   escapes. Empty trusted span means {}. No callbacks. Serialize calls on a
   handle, including teardown. Use only live handles from this library.

   Each non-null output buffer is separately owned and must be freed with
   aed_free_buffer. On failure the buffer is empty or {"error": string}.
   Do not reuse an output slot without freeing its previous contents. Status
   codes are aec_status. Close discards unpublished work, is idempotent, and
   leaves a handle that can still be freed. Free functions accept NULL.

   seed: {ppq, initialize: trusted_import} -> initial document without revision;
         {ppq, validate: document} -> {duration}. Neither opens a live head.
   open: {project_id, ppq, document, index, saved_sequence?, history?},
         with matching revision strings. history is the AED_HISTORY payload.
         For a legacy index with nonempty undo/redo, set legacy_history=true,
         pass the earliest body, and append the remaining bodies in timeline
         order: undo array order, current, then reverse redo array order.
         Until complete the handle is an import builder, not a live session.
         Do not register or expose it as a project owner until import finishes.
   run: {base_revision, op, proposed_revision?} or
        {base_revision, query, args?}. The ABI caller, not the UI, supplies the
        separate trusted object. For a new revision, proposed_revision is a
        fresh 32-digit lowercase hex UUID. It also namespaces manual IDs.

   Query and preview use request=0. Preview does not stage, reserve durable
   IDs, or consume a public request number. Prepare and draft commands use
   strictly increasing nonzero attempt numbers. A successful prepare consumes
   its number even if discarded; a failed prepare does not consume it. The
   result is a delta/receipt; only export materializes a prepared revision.

   APPLY validates and constructs its response before atomically installing
   the memory head. PREPARE/finish are retained for staged ABI clients only;
   observed_revision is a label assertion, not an I/O or durability contract.
   No-head acceptance and discard use an empty span. Close cannot invalidate
   an already captured snapshot. A snapshot may be read on another thread;
   the caller must retain it until all reads finish. Capture is small; reading
   or freeing a large snapshot is NOT a real-time operation. */
AEC_API uint32_t AEC_CALL aed_version(void) AEC_NOEXCEPT;
/* Stateless read-only projection allowlist: editor_clock, editor_axis,
   to_seconds, preview_meter_edit, preview_roll_edit. Export timing is
   supplied by the caller. No document/history or mutation. */
AEC_API int32_t AEC_CALL aed_timing_query_v1(aec_bytes, aed_buffer *) AEC_NOEXCEPT;
AEC_API int32_t AEC_CALL aed_seed_v1(aec_bytes, aed_buffer *) AEC_NOEXCEPT;
AEC_API int32_t AEC_CALL aed_open_v1(aec_bytes, aed_session **, aed_buffer *) AEC_NOEXCEPT;
AEC_API int32_t AEC_CALL aed_append_legacy_v2(aed_session *, aec_bytes body, aed_buffer *) AEC_NOEXCEPT;
AEC_API int32_t AEC_CALL aed_stamp_v1(const aed_session *, aec_stamp *, aed_buffer *) AEC_NOEXCEPT;
AEC_API int32_t AEC_CALL aed_run_v1(aed_session *, const aec_token *, uint32_t mode,
                                    aec_bytes packet, aec_bytes trusted, aed_buffer *) AEC_NOEXCEPT;
AEC_API int32_t AEC_CALL aed_export_v1(const aed_session *, const aec_token *, aed_buffer *) AEC_NOEXCEPT;
AEC_API int32_t AEC_CALL aed_finish_v1(aed_session *, const aec_token *, uint32_t accept,
                                       aec_bytes observed_revision, aed_buffer *) AEC_NOEXCEPT;
AEC_API int32_t AEC_CALL aed_head_v2(const aed_session *, aed_buffer *) AEC_NOEXCEPT;
AEC_API int32_t AEC_CALL aed_capture_v2(const aed_session *, aed_snapshot **, aed_buffer *) AEC_NOEXCEPT;
/* Checkpoint capture copies only edge references, not bodies. Ordinary
   capture remains independent of history size. AED_HISTORY and indexed
   history body reads require a checkpoint capture. Side 0 is undo, side 1
   is redo; index 0 is the bottom of that stack (legacy index order). */
AEC_API int32_t AEC_CALL aed_capture_checkpoint_v2(const aed_session *, aed_snapshot **, aed_buffer *) AEC_NOEXCEPT;
AEC_API int32_t AEC_CALL aed_read_snapshot_v2(const aed_snapshot *, uint32_t mode, aed_buffer *) AEC_NOEXCEPT;
AEC_API int32_t AEC_CALL aed_read_history_body_v2(const aed_snapshot *, uint32_t side, size_t index, aed_buffer *) AEC_NOEXCEPT;
AEC_API void AEC_CALL aed_free_snapshot(aed_snapshot *) AEC_NOEXCEPT;
AEC_API int32_t AEC_CALL aed_close_v1(aed_session *) AEC_NOEXCEPT;
AEC_API void AEC_CALL aed_free_session(aed_session *) AEC_NOEXCEPT;
AEC_API void AEC_CALL aed_free_buffer(char *) AEC_NOEXCEPT;

#ifdef __cplusplus
}
#endif
#endif
