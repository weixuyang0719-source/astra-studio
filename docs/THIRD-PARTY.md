# 第三方许可与可选资源

- Astra 原創代码：GPL-3.0-only。参见根目录许可说明。
- MuseScore Studio 4.6.5：GPL-3.0-only，参考提交 653f38d8ea80ebd2b084c2b5e5521a32b00983c7。精确修改在 native/score-worker/overlay/，原有声明不改动。上游：<https://github.com/musescore/MuseScore/releases/tag/v4.6.5>。
- PortAudio 19.7.0 和 Steinberg ASIO 2.3.4：PortAudio 原始许可证以及 ASIO 的 GPLv3 分支和相应 BSD 版权声明随 native/portaudio-asio/ 保存。Windows ASIO 驱动不是本仓库软件。
- Steinberg VST3 SDK：MIT，固定组件提交见 native/README.md。用户安装的任何第三方 VST 或预设不在仓库分发。
- 原生构建引用的 nlohmann/json、Signalsmith、FluidSynth、HarfBuzz、FreeType：分别保留其组件自有许可或引向确切的上游基础树。
- OpenSheetMusicDisplay：BSD-3-Clause，嵌入资源、原作者及依赖许可位于 src/astra/audio_output/studio_web/live_score/ 对应目录。
- 界面字体：按随附 SIL OFL 1.1 及出处说明使用，未改变原有归属。见两套 studio_web/fonts/SOURCES.md。
- Qt/PySide/Shiboken/WebEngine、Python 及其他 PyPI 依赖不从本仓库再分发；开发者自行经官方发行渠道取得并遵守各自条款。编译谱面和 Python GUI 的 Qt 版本不同。
- 本项目代码不携带经商业 DAW 安装的二进制、驱动或厂商模型。旧项目兼容路径中出现的产品名称不表示隶属或授权其资源。

## 不随源代码分发

模型权重和运行时依赖与源代码授权分别处理。钢琴分轨 X-LANCE 权重的作者来源是 <https://huggingface.co/chenxie95/xlance-msr-ckpt>；DuET 权重有独立的非商业条款。现有 BS-Roformer-SW 吉他权重未确认原始再分发许可，本仓库不提供该权重或镜像下载。不要把模型当作 GPL 项目代码。内置钢琴的 MS Basic.sf3 也不附带；使用自己的合规资源。
