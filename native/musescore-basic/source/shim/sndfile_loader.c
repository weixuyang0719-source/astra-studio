/* SPDX-License-Identifier: GPL-3.0-only
 * Resolve the four libsndfile calls FluidSynth uses from libsndfile_x64.dll in
 * the plug-in's own folder. Absolute path only; never a search-path lookup. */
#include <windows.h>
#include "sndfile.h"

typedef SNDFILE* (*open_virtual_t)(SF_VIRTUAL_IO*, int, SF_INFO*, void*);
typedef const char* (*strerror_t)(SNDFILE*);
typedef sf_count_t (*readf_short_t)(SNDFILE*, short*, sf_count_t);
typedef int (*close_t)(SNDFILE*);

static INIT_ONCE once = INIT_ONCE_STATIC_INIT;
static open_virtual_t p_open;
static strerror_t p_strerror;
static readf_short_t p_readf;
static close_t p_close;

static BOOL CALLBACK resolve(PINIT_ONCE o, PVOID p, PVOID* c)
{
    HMODULE self = NULL, lib;
    wchar_t path[MAX_PATH * 4];
    DWORD n;
    wchar_t* slash;
    (void)o; (void)p; (void)c;
    GetModuleHandleExW(GET_MODULE_HANDLE_EX_FLAG_FROM_ADDRESS | GET_MODULE_HANDLE_EX_FLAG_UNCHANGED_REFCOUNT,
                       (LPCWSTR)&resolve, &self);
    n = GetModuleFileNameW(self, path, MAX_PATH * 4 - 32);
    if (!n) return TRUE;
    path[n] = 0;
    slash = wcsrchr(path, L'\\');
    if (!slash) return TRUE;
    lstrcpyW(slash + 1, L"libsndfile_x64.dll");
    lib = LoadLibraryExW(path, NULL, LOAD_WITH_ALTERED_SEARCH_PATH);
    if (!lib) return TRUE;
    p_open = (open_virtual_t)GetProcAddress(lib, "sf_open_virtual");
    p_strerror = (strerror_t)GetProcAddress(lib, "sf_strerror");
    p_readf = (readf_short_t)GetProcAddress(lib, "sf_readf_short");
    p_close = (close_t)GetProcAddress(lib, "sf_close");
    return TRUE;
}
static void load(void) { InitOnceExecuteOnce(&once, resolve, NULL, NULL); }

SNDFILE* sf_open_virtual(SF_VIRTUAL_IO* v, int mode, SF_INFO* info, void* user)
{
    load();
    return p_open ? p_open(v, mode, info, user) : NULL;
}
const char* sf_strerror(SNDFILE* f)
{
    load();
    return p_strerror ? p_strerror(f) : "libsndfile_x64.dll is not available";
}
sf_count_t sf_readf_short(SNDFILE* f, short* ptr, sf_count_t frames)
{
    load();
    return p_readf ? p_readf(f, ptr, frames) : 0;
}
int sf_close(SNDFILE* f)
{
    load();
    return (p_close && f) ? p_close(f) : 0;
}

/* fluid_filerenderer.c is excluded (no file output); fluid_settings_init()
 * still registers its "audio.file.*" options, which nothing here reads. */
struct _fluid_hashtable_t;
void fluid_file_renderer_settings(struct _fluid_hashtable_t* settings) { (void)settings; }
