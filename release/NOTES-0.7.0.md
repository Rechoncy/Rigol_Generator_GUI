# Lab Control Center 0.7.0 发行说明

日期：2026-09-21

平台：Windows 10/11 x64

## 本版重点

新增内置独立 Python 运行时的目录便携 ZIP 与单文件 EXE。目标电脑无需 Python、pip、Conda 或项目源码，首次运行无需联网安装 Python 依赖；解压目录包后即可启动。系统硬件驱动仍按连接的仪器安装。

## 交付文件

- `Lab-Control-Center-0.7.0-windows-x64-portable.zip`：含 EXE、`_internal/`、说明文档及组件许可材料，日常使用推荐。
- `Lab-Control-Center-0.7.0-windows-x64.exe`：内置运行时的单文件入口，启动时解压到临时目录。
- `SHA256SUMS.txt`：产物校验和。
- `release-manifest.json`：构建、依赖与验证信息。
- `Lab-Control-Center-0.7.0-windows-x64-source.zip`：本次构建的源码与文档快照；目录包另含 `source.zip`。
- `RELEASE-NOTES.md`：本版本说明。

两种格式均内置 Python、Qt、PyVISA、pyserial 和 nidaqmx。本次默认发行包不包含 Vaunix 厂商 DLL；可按使用说明外置提供，或在自定义构建时显式携带，清单会记录是否包含。发行包不包含 NI-VISA、NI-DAQmx 或 USB 转串口等系统驱动安装程序。

## 0.6.0 之后纳入的功能

- 鼎阳 SIGLENT SSG 独立窗口：频率、功率、RF、仪器原生 STEP/LIST 扫描、独立扫描与点触发、模拟调制与单脉冲。SSG3021X / SSG3032X 加入脉冲 BUS、实际回读和全局 `*TRG` 操作；不使用 NI 逐点控制鼎阳扫描。
- Newport SMC100CC / PR50CC：兼容现场控制器/旋转台身份回包；小角度循环步进、P0 配置、点表及状态显示；忙时触发和计数跳增停止，可在同一任务记录 NI 连续模拟输入。
- 完整配置导入导出：保存设备、窗口参数、扫描任务及连接恢复列表，迁移保留设备 ID；恢复配置不会开启输出、移动或启动扫描。
- DG4000 型号范围识别与 Vaunix LMS/BLX 分型、失效连接清理等兼容改进。
- 以 SSG3021X 为主的详细使用说明和开发交接资料。

各功能的阶段开发、测试及边界保留在 `docs/DEVELOPMENT_LOG_CN.md`；该文档中的旧“未发布”条目描述当时的状态。

## 保存与迁移变化

- 编译版默认将配置保存在 EXE 旁的 `data/`，目录包和单文件版一致。
- `--data-dir` 优先于 `RIGOL_GUI_DATA_DIR`；未设置两者时使用便携默认目录。
- 源码启动仍沿用 `%APPDATA%\RigolGeneratorControl`；编译版不自动读取或迁移该目录。
- 原用户可先在旧版导出配置，再在新版导入；实验 CSV/TXT、旋转任务记录和外置厂商 DLL 要另外复制。
- 升级和迁移前关闭软件，保留 `data/` 或外部数据目录备份。不要仅复制目录包内的 EXE，不要让两个实例同时写同一份配置。

## 构建维护

发行构建使用官方 Windows x64 CPython，推荐 3.13；脚本拒绝 Conda 构建环境，避免 Qt/DLL 冲突，不改变源码运行支持。新增 `tools/build_release.ps1` 作为 Windows 构建入口，自动建立 `build/release-venv/`；`tools/build_release.py` 负责两种 PyInstaller 产物、依赖与许可材料、清单及校验和。构建环境仅用于开发机，发行运行不依赖它。增加 GitHub Actions 流程：手动触发输出构建附件，版本标签触发在验证通过后创建 GitHub Release。

仓库原有小型 `Lab Control Center.exe`、`run_app.cmd` 和源码启动方式继续保留。它们依赖外部 Python，与本版发行附件应明确区分。

## 验证与边界

2026-09-21，在正式构建环境 CPython 3.13.15 中完成全量回归：**215 项测试通过，耗时 23.021 秒**；本次新增 12 项运行路径测试与 2 项构建测试。

实际编译及运行验证通过：

- 目录包、单文件 EXE、发行 ZIP 二次解压后的目录包，三次冻结产物自检均成功。
- PyQt6、PyVISA、pyserial、nidaqmx、numpy 和 Qt 插件均从包内加载；图标可用。
- 每次自检连接六台虚拟设备、打开六个设备窗口并正常关闭，临时配置清理成功。
- 将程序迁移至含中文、空格、撇号的路径，清除 PATH 中的外部 Python，并将 PYTHONHOME/PYTHONPATH 指向无效位置后仍可运行。
- 目录 EXE 与单文件 EXE 的普通 Windows Qt GUI 启动检查均通过，便携数据默认位置与参数覆盖另有运行路径回归覆盖。
- 构建时清理可能引入外部 DLL 的 PATH，检查二进制依赖来源；修复开发环境中的 Conda/Poppler ICU 冲突，发行包使用自身的 Qt 依赖。

本轮产物大小约为目录 ZIP **52.5 MiB**、单文件 EXE **51.5 MiB**，最终字节数与校验值以 `release-manifest.json` 和 `SHA256SUMS.txt` 为准。检查在当前 Windows 机器的隔离搜索环境下完成，未执行实体仪器测试。

实体 DG4000、鼎阳、Vaunix、Newport 与 NI 的连接、电气触发和机械稳定仍需现场验收；虚拟与协议测试不能替代实机结果。当前不提供 32 位或其他操作系统的编译版本。旧 PDF 尚未重建，新增操作以 Markdown 文档为准。

具体安装、迁移、驱动及构建步骤见 [Windows 便携发行说明](README.md)。
