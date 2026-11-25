# 本地原生部署位置

这些位置描述源码树的本机开发安装。只放入自己刚构建或从官方项目合法取得的产物，不复制到已有的正式版安装目录，也不要将 DLL 提交至 Git。

| 构建产物 | 源码模式预期位置 |
| --- | --- |
| astra_editor_core.dll、astra_recovery_recycle.dll | src/astra/ |
| astra_audio_core.dll、astra_kept_endpoint.dll、astra_stream_cache.dll、astra_midi_scheduler.dll、astra_midi_winmm.dll、astra_pcm_loader.dll、astra_tempo_cache.dll、native_transport_panel.dll、astra_portaudio_host.dll、astra_wasapi_host.dll | src/astra/audio_output/_native/ |
| astra_instrument_start.dll、astra_vst_host.dll、astra_vst_isolated_fx_host.dll、astra_vst_rt_bridge.dll | src/astra/vst_host/_native/ |
| AstraNativeScore.exe | 按独立谱面构建说明放置，并用 --score-worker 指向该文件 |
| AstraMuseScoreBasic.vst3 | 只有在准备好对应资源以后，按 native/musescore-basic 的 CMake 结果，独立安装其完整 VST3 包，勿拷第三方商用插件 |

在普通 Windows 解释器中安装的外部运行时包可能会附带自己的底层库。正式外测包使用许可已核对的独立 PortAudio ASIO 版本，不能拿 sounddevice wheel 搭载的历史 ASIO DLL 冒充同一构建或覆盖对应许可。构建和配置具体设备链路须在你有权限的设备上进行。
