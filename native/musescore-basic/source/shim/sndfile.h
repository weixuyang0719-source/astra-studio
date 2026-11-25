/* SPDX-License-Identifier: GPL-3.0-only
 * The subset of libsndfile's public C API (sndfile.h, LGPL) used by
 * FluidSynth's SF3 Vorbis sample reader. Layouts match libsndfile 1.x.
 * The functions are resolved at run time from the libsndfile DLL bundled
 * next to the plug-in module (sndfile_loader.c). */
#ifndef ASTRA_MSBASIC_SNDFILE_H
#define ASTRA_MSBASIC_SNDFILE_H
#include <stdint.h>
#ifdef __cplusplus
extern "C" {
#endif

typedef int64_t sf_count_t;
typedef struct SNDFILE_tag SNDFILE;

enum { SFM_READ = 0x10, SFM_WRITE = 0x20, SFM_RDWR = 0x30 };
enum { SF_FORMAT_OGG = 0x200000, SF_FORMAT_VORBIS = 0x0060 };

typedef struct SF_INFO {
    sf_count_t frames;
    int samplerate;
    int channels;
    int format;
    int sections;
    int seekable;
} SF_INFO;

typedef sf_count_t (*sf_vio_get_filelen)(void* user_data);
typedef sf_count_t (*sf_vio_seek)(sf_count_t offset, int whence, void* user_data);
typedef sf_count_t (*sf_vio_read)(void* ptr, sf_count_t count, void* user_data);
typedef sf_count_t (*sf_vio_write)(const void* ptr, sf_count_t count, void* user_data);
typedef sf_count_t (*sf_vio_tell)(void* user_data);

typedef struct SF_VIRTUAL_IO {
    sf_vio_get_filelen get_filelen;
    sf_vio_seek seek;
    sf_vio_read read;
    sf_vio_write write;
    sf_vio_tell tell;
} SF_VIRTUAL_IO;

SNDFILE* sf_open_virtual(SF_VIRTUAL_IO* sfvirtual, int mode, SF_INFO* sfinfo, void* user_data);
const char* sf_strerror(SNDFILE* sndfile);
sf_count_t sf_readf_short(SNDFILE* sndfile, short* ptr, sf_count_t frames);
int sf_close(SNDFILE* sndfile);

#ifdef __cplusplus
}
#endif
#endif
