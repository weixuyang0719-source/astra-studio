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

## 模型权重声明

本源码仓库不托管、不镜像、不随 Git 提交或 GitHub Release 分发模型权重。模型权重不属于 Astra Studio 的 GPL-3.0-only 授权范围。需要运行相应功能时，请从下列上游取得对应权重，并遵守该权重发布时的条款。软件源码可用的许可证不自动适用于同名模型的权重。

| 模型 | Astra Studio 中的用途 | 上游与权重条款 |
| --- | --- | --- |
| DuET | 把钢琴演奏 MIDI 转成谱面 | [Wayne42/DuET 模型卡](https://huggingface.co/Wayne42/DuET)标注权重为 [CC BY-NC 4.0](https://creativecommons.org/licenses/by-nc/4.0/)；使用时须遵守署名及非商业限制。 |
| Transkun 2.0 | 把钢琴音频转为演奏 MIDI | [Transkun 作者仓库与模型卡入口](https://github.com/Yujia-Yan/Transkun)列有不同的 checkpoint；取得权重前请核对所选版本的条款，不以软件的 MIT 标识替代权重许可。 |
| X-LANCE 钢琴分轨 | 从混音中提取钢琴轨（`bs_keys_xlancer.ckpt`） | [X-LANCE 作者的权重页](https://huggingface.co/chenxie95/xlance-msr-ckpt)；本项目不授予该权重的再分发或商业使用许可。 |
| BS-RoFormer-SW 吉他分轨 | 可选的吉他轨提取 | 未核实当前所用权重的原始再分发许可；本仓库不提供该权重或镜像下载。 |

## 其他不随源码分发的资源

内置钢琴使用的 MS Basic.sf3 音色库不随源码提供；请自行取得有权使用的资源。其他运行时依赖的获取方式见 [Windows 构建说明](BUILD-WINDOWS.md)。
