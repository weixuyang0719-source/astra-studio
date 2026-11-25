# Astra Studio

Astra Studio 是一个在本机运行的扒谱与编辑工具。它把音频分轨、钢琴 MIDI 转录、拍点与小节编辑、谱面预览与出版连接在同一个工作台里。

这个仓库是 **正式版 4 对应的源码快照**，不是双击即用的 Windows 安装包。首次检出不会带用户工程、录音、模型权重、Python 环境、驱动、VST 插件或已编译程序。无须登录即可读代码、运行源码检查和无模型的 MIDI 命令。

## 从源码开始

环境：Windows x64、Python 3.10–3.12。编译原生组件另需 Visual Studio C++ 工具、CMake 和 Windows SDK。请在 PowerShell 中运行：

    py -3.10 -m venv .venv
    .\.venv\Scripts\python.exe -m pip install -e .
    .\.venv\Scripts\python.exe -m astra --help
    .\.venv\Scripts\python.exe scripts\run_source.py --check

安装命令通过 Python 的包管理器获取公共依赖，不会安装模型或 ASIO 驱动。若只是查看 CLI 的帮助或检查 MIDI，可使用已安装依赖的解释器并将 src 放入 PYTHONPATH。

原生音频/VST、录拍 C++ 内核、MuseScore 谱面引擎和分轨/转录模型各有独立前置条件，详见 [构建与验证](docs/BUILD-WINDOWS.md)。准备相应部件后，使用源码启动器运行：

    .\.venv\Scripts\python.exe scripts\run_source.py

它默认使用独立的开发数据目录，不会接管正式版的工程。自带乐谱引擎需要另行构建和接入；未接入时不要把源码模式当作正式版完整体验。

## 项目结构

| 路径 | 内容 |
| --- | --- |
| src/astra/ | 正式版 4 所选 Python 应用代码与两套界面 |
| native/ | 编辑器、音频、VST 及谱面相关的原生源码和构建脚本 |
| native/score-worker/ | MuseScore 4.6.5 的 Astra 精确差异与独立构建配置 |
| provenance/ | 参考版本和已核对的源代码摘要；不包含用户数据 |
| docs/ | 构建、第三方依赖、功能边界和验证记录 |

## 许可证

Astra Studio 原创软件采用 GPL-3.0-only。参见 [LICENSE](LICENSE) 与 [LICENSE-SCOPE.md](LICENSE-SCOPE.md)。第三方源码、字体和依赖各自遵循其授权；具体说明见 [第三方许可与可选资源](docs/THIRD-PARTY.md)。
