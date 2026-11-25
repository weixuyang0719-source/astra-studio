# 针对 Windows x64 构建

## Python 入口与工程边界

在仓库根目录安装 Python 依赖后，可运行 python -m astra --help。检查真实 MIDI 时自行选择有权使用的本地文件，不要把它加入 Git。scripts/run_source.py 是给源码开发使用的启动包装，默认在 %LOCALAPPDATA%\Astra Studio Dev\workspace 下创建数据；--data-dir 可显式指定新目录。--check 只查看依赖与路径，不打开界面、不探测声卡、不写用户工程、不下载。

源码启动不复用桌面正式版 4 的引擎安装或封签，不提供安装器，也不将原版的整包启动脚本照搬到仓库。

## 原生代码

1. 在 Visual Studio 2026 的 x64 Native Tools 命令提示符使用 CMake，并确保 Windows SDK 10.0.26100 或等价工具链可用。
2. 运行 native\Build-Native.cmd 编译编辑核心、音频核心与 VST 宿主等到 native\out\。该命令不会安装到现有 Astra 系统。参见 [原生组件构建矩阵](../native/README.md)。
3. 另运行 native\astra-portaudio-host\build.cmd 编译 Astra 宿主包装 DLL。native\portaudio-asio\rebuild.ps1 构建了不同的底层 PortAudio+ASIO GPLv3 组件。两者不是同一库。机器上的 ASIO 驱动由用户自行合法安装，不由此仓库分发。
4. 本地部署时，DLL 的目标路径请对照 [发布原生组件对照表](../native/NATIVE-BUILD-PROVENANCE.json) 与 [构建输出说明](NATIVE-DEPLOYMENT.md)。散列是参考构建的记录，不是新编译应有的散列；不应从不明来源复制同名 DLL。
5. 内置钢琴依赖另行取得合法 soundfont 和 libsndfile；见 native README。无内置钢琴文件不会影响源码读取、CLI 帮助或 MIDI 文件结构检查。

## 谱面进程

源树是 MuseScore Studio 4.6.5 的 Astra 修改版。见 [谱面构建指引](../native/score-worker/README.md)；编译需要对应的 Qt 6.9.2 C++ 开发套件，不能换成 PySide6 的 Qt 发行件。自行取得同版本的底层树与已记录哈希的 zlib 源档，然后应用仓库的 overlay。

将编译完成的 Windows x64 worker 与匹配的 Qt 6.9.2 运行目录明确传给源码启动器：

    python scripts\run_source.py --score-worker D:\score\bin\AstraNativeScore.exe --score-qt D:\sdk\Qt\6.9.2\msvc2022_64

## 模型、驱动与验证范围

分轨与转录依赖兼容的 GPU/CPU 推理环境、对应代码和被许可的权重安装路径。这个仓库不打包或自动下载权重。音频分析的各类机器与 GPU 驱动组合、真实声卡、外部 VST 及完整整包组装需单独测试。源树不是独立可执行的正式包，也不是全组件逐字节可复现的声明。
