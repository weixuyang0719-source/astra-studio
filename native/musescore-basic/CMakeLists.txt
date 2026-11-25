# SPDX-License-Identifier: GPL-3.0-only
# MuseScore Basic Piano (Astra): MuseScore Studio 4.6.5's FluidSynth + MS Basic.sf3 as a VST3 instrument.
cmake_minimum_required(VERSION 3.25)

set(VST3_SDK_ROOT "${CMAKE_CURRENT_LIST_DIR}/../vendor/vst3sdk" CACHE PATH "VST3 SDK")
set(MUSESCORE_SRC "${CMAKE_CURRENT_LIST_DIR}/../vendor/musescore-fluidsynth" CACHE PATH "MuseScore Studio 4.6.5 source tree")
set(MS_BASIC_SF3 "${MUSESCORE_SRC}/share/sound/MS Basic.sf3" CACHE FILEPATH "MuseScore sound font")
set(SNDFILE_DLL "" CACHE FILEPATH "libsndfile_x64.dll to bundle")

# Build only what a plug-in needs; never touch the system VST3 folder.
set(SMTG_ENABLE_VST3_PLUGIN_EXAMPLES OFF CACHE BOOL "" FORCE)
set(SMTG_ENABLE_VST3_HOSTING_EXAMPLES OFF CACHE BOOL "" FORCE)
set(SMTG_ENABLE_VSTGUI_SUPPORT OFF CACHE BOOL "" FORCE)
set(SMTG_CREATE_PLUGIN_LINK OFF CACHE BOOL "" FORCE)
set(SMTG_RUN_VST_VALIDATOR OFF CACHE BOOL "" FORCE)
set(SMTG_ADD_VSTGUI OFF CACHE BOOL "" FORCE)

project(AstraMuseScoreBasic VERSION 4.6.5.1 LANGUAGES C CXX)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_MSVC_RUNTIME_LIBRARY "MultiThreaded$<$<CONFIG:Debug>:Debug>")

add_subdirectory(${VST3_SDK_ROOT} ${PROJECT_BINARY_DIR}/vst3sdk EXCLUDE_FROM_ALL)
smtg_enable_vst3_sdk()

# MuseScore's patched FluidSynth 2.3.3 (no glib), exactly its source list and defines.
include(${MUSESCORE_SRC}/src/framework/audio/thirdparty/fluidsynth/fluidsynth.cmake)
list(FILTER FLUIDSYNTH_SRC EXCLUDE REGEX "\\.h$")
# The file renderer (libsndfile writer API) is not used by a real-time instrument.
list(FILTER FLUIDSYNTH_SRC EXCLUDE REGEX "fluid_filerenderer\\.c$")
add_library(msfluid STATIC ${FLUIDSYNTH_SRC} source/shim/sndfile_loader.c)
target_include_directories(msfluid BEFORE PUBLIC ${CMAKE_CURRENT_LIST_DIR}/source/shim ${FLUIDSYNTH_INC})
target_compile_definitions(msfluid PUBLIC ${FLUIDSYNTH_DEF} _CRT_SECURE_NO_WARNINGS)
target_compile_options(msfluid PRIVATE /W0 /wd4013)

smtg_add_vst3plugin(AstraMuseScoreBasic
    source/ids.h
    source/version.h
    source/msbasic_synth.h
    source/msbasic_synth.cpp
    source/plugin.cpp
)
target_link_libraries(AstraMuseScoreBasic PRIVATE sdk msfluid)
smtg_target_add_plugin_resources(AstraMuseScoreBasic RESOURCES "${MS_BASIC_SF3}")
if(SNDFILE_DLL)
    add_custom_command(TARGET AstraMuseScoreBasic POST_BUILD
        COMMAND ${CMAKE_COMMAND} -E copy_if_different "${SNDFILE_DLL}"
                "$<TARGET_FILE_DIR:AstraMuseScoreBasic>/libsndfile_x64.dll")
endif()
