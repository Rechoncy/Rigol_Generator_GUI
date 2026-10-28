# Windows 安装版与便携发行版使用及构建说明

正式发行版本：0.7.23；目标平台：Windows 10/11 x64。

发行版包含运行程序所需的 Python、Qt、PyVISA、pyserial 和 nidaqmx。目标电脑无需安装 Python、pip 或创建虚拟环境，首次启动也无需联网下载 Python 组件。内置运行时只供本软件使用，不修改系统 Python。

## 1. 选择下载文件

| 文件 | 用法 | 适合场景 |
|---|---|---|
| `Lab-Control-Center-0.7.23-windows-x64-setup.exe` | 运行安装向导，之后从开始菜单启动 | 日常使用，后续下载后自动安装并保留用户数据 |
| `Lab-Control-Center-0.7.23-windows-x64-portable.zip` | 完整解压后运行目录内的 `Lab Control Center.exe` | 便携使用、实验室迁移、与文档一起保存 |
| `Lab-Control-Center-0.7.23-windows-x64.exe` | 放在可写目录，直接双击 | 只希望分发一个程序文件 |
| `SHA256SUMS.txt` | 对照下载文件的 SHA-256 | 核对下载是否完整 |
| `release-manifest.json` | 查看版本、构建、依赖与验证信息 | 维护、问题报告和版本追踪 |
| `Lab-Control-Center-0.7.23-windows-x64-source.zip` | 本次构建使用的源码与文档快照 | 维护、依赖许可要求及问题复现 |
| `RELEASE-NOTES.md` | 查看本版改动和验证范围 | 升级前阅读 |

项目地址：[Rigol_Generator_GUI](https://github.com/Rechoncy/Rigol_Generator_GUI)。正式发行文件从 [v0.7.23 GitHub Release](https://github.com/Rechoncy/Rigol_Generator_GUI/releases/tag/v0.7.23) 下载，由版本标签对应的提交构建。发布附件的本地副本位于 `dist/published/v0.7.23/`，自行构建的输出目录为 `dist/release/`；仓库中的 `release/` 保存说明与版本记录。产物生成、实际携带内容及运行验收以同组附件的 `release-manifest.json` 为准，完整性可用 `SHA256SUMS.txt` 核对。

上表公开下载文件名和下文运行示例均使用 0.7.23。本次按用户授权发布 GitHub Release，并将正式附件保存到 `dist/published/v0.7.23/`；已经验收的 `dist/local/v0.7.23/` 开发构建、历史构建与 `dist/published/v0.7.20/` 旧正式附件分别保留。安装版支持下载后自动安装、卸载旧安装版并保留/备份/核对恢复用户数据，详见[安装版操作](../docs/USER_MANUAL_CN.md#45-安装版-exe-与保留数据升级)及[本版记录](NOTES-0.7.23.md)。

仓库根目录原有的小型 `Lab Control Center.exe` 是源码启动器，仍需外部 Python/PyQt6；它与上述编译产物不同。使用免 Python 版本时，应下载发行附件或按第 7 节构建。

从 0.7.5 起，主窗口和每个设备窗口标题均显示真实运行版本（例如 `v0.7.23`）。SSG 的“状态与诊断”页显示可复制的运行程序、模块路径，诊断 JSON 也保存这些信息。压缩包或文件夹的名称不能代替运行版本；升级后请核对标题。

## 2. 解压与首次启动

1. 将 ZIP 解压到有写入权限的位置，例如 `D:\LabTools\Lab-Control-Center`。不要直接从 ZIP 预览窗口运行。
2. 保留 `Lab Control Center.exe` 和 `_internal` 的相对位置，双击 EXE。目录包不能只复制其中的 EXE。
3. 首次启动后点击首页“虚拟实验室”，可以检查六台虚拟设备和界面功能，无需硬件驱动。
4. 实机使用按第 4 节准备对应驱动，再在软件中添加或导入设备配置。
5. 软件启动和配置恢复不会自动开启 RF、移动旋转台或启动扫描；连接、参数应用、输出与任务仍由用户在界面中操作。

0.7.23 的首页保留 GitHub 正式版自动检查、下载与 SHA-256/字节数校验。安装版选择 Setup EXE，默认可在设备空闲后自动安装；便携 ZIP 与独立 EXE 继续采用原有下载后手动迁移方式。支持关闭自动检查、下载或安装，网络异常不影响离线仪器功能，完整操作见[软件更新](../docs/USER_MANUAL_CN.md#44-联网检查软件更新)。

### 2.1 安装程序 EXE

运行 `Lab-Control-Center-0.7.23-windows-x64-setup.exe`，按向导安装。默认程序目录为 `%LOCALAPPDATA%\Programs\LabControlCenter`，仅针对当前用户；用户数据独立保存在 `%LOCALAPPDATA%\RigolGeneratorControl\data`，向导可选择已有独立数据目录。保留程序与数据目录分离，目标电脑无需外部 Python。安装后通过开始菜单启动，卸载项只移除软件，保留独立数据和备份。

首页“下载后自动安装（空闲时）”默认开启，可取消后手动点击“安装更新”。新版 Setup 完整下载并通过校验后，应用等待扫描、采集、后台通信和清理完成，只有正常退出被接受后才交接给安装程序。安装程序先备份用户数据，再卸载登记的旧版、安装新版并核对恢复；不强制结束实验或自动启动 RF、扫描、运动。数据目录内的测量文件与空目录一并备份，`updates` 下载缓存除外；目录外的采集文件不移动或删除。

默认升级备份保存在 `%LOCALAPPDATA%\RigolGeneratorControl\installer-backups`。备份失败阻止卸载；恢复发现同名不同内容时保留原文件和备份并报告，不静默覆盖。安装失败仍保留外部数据与日志，但不保证旧程序自动回滚，可排除权限、磁盘或安装错误后重试。旧便携版首次迁移应先停止并退出旧程序，再选择原数据目录，或在新旧程序均关闭时复制 `data/`、通过首页导入配置；配置包不含采集文件。更多步骤见[安装版保留数据升级](../docs/USER_MANUAL_CN.md#45-安装版-exe-与保留数据升级)。

目录包主要结构：

```text
Lab-Control-Center-0.7.23-windows-x64-portable/
├─ Lab Control Center.exe
├─ _internal/              内置 Python、Qt、Python 依赖与资源，须完整保留
├─ docs/                   操作和开发说明
├─ licenses/               随包组件许可材料
├─ source.zip              本次构建的源码快照
├─ build-info.json         构建身份和依赖版本
└─ data/                   启动时创建，保存设备、参数与任务数据
```

单文件 EXE 每次启动时会将运行时解压到 Windows 临时目录，启动可能比目录包慢；临时目录不是配置目录。它仍将持久化数据放在原 EXE 旁的 `data/`，关闭程序或清理临时目录不会通过该机制删除已保存配置。运行过程中不要移动正在使用的 EXE 或清理它的临时运行目录。

程序目录需可写，推荐使用普通用户自己的工作目录，不必为了日常运行申请管理员权限。若安装位置受保护，按下一节指定可写数据目录。

## 3. 保存、迁移与升级

两种编译版本默认使用 **EXE 所在目录下的 `data/`**。目录位置按以下优先级选择：

1. 启动参数 `--data-dir`；
2. 环境变量 `RIGOL_GUI_DATA_DIR`；
3. 编译版 EXE 旁的 `data/`。

源码运行仍默认使用 `%APPDATA%\RigolGeneratorControl`，除非显式覆盖。编译版不会自动导入旧 AppData，也不会自动扫描电脑上的其他项目副本。

指定数据目录的例子：

```powershell
& ".\Lab Control Center.exe" --data-dir "D:\LabData\ControlCenter"
```

单文件版本同样支持：

```powershell
& ".\Lab-Control-Center-0.7.23-windows-x64.exe" --data-dir "D:\LabData\ControlCenter"
```

也可为当前 PowerShell 会话设置环境变量后启动：

```powershell
$env:RIGOL_GUI_DATA_DIR = "D:\LabData\ControlCenter"
& ".\Lab Control Center.exe"
```

常用数据文件为 `devices.json`、`device_parameters.json`、`step_tasks.json`、`linkage_tasks.json`，以及程序生成的运行记录。采集 CSV/TXT 和 Newport 运行目录可能保存到用户另外选择的位置，不一定在 `data/` 中。

**整套迁移：** 停止正在运行的任务并退出软件，再复制整个解压目录。单文件版本复制 EXE 及同级 `data/`；有外置 `vendor/` 或其他附件时一并复制。若指定过外部数据目录，也要复制该目录并在新电脑调整覆盖设置。新电脑的 VISA 地址、NI 名称、COM 端口可能变化，恢复配置后先核对。

**从旧源码版迁移：** 在旧版首页“导出配置”，然后在新版首页“导入配置”。也可在两边都关闭后备份并复制旧数据目录中的配置。配置导出不包含采集数据、系统驱动或仪器当前运行状态，实验数据需单独迁移。导入可恢复连接，但不会自动开始输出、扫描或运动。

**升级：** 将新版解压到新目录，保留旧版和原数据备份；退出旧版后复制 `data/` 或导入配置，再核对设备地址。不要用空目录覆盖旧数据，也不要让两个程序实例同时写同一个数据目录。数据格式与稳定设备 ID 沿用原有版本，软件版本升级不要求删除配置。

0.7.15 的 DG/Vaunix NI 步进与跨设备任务会等待后台启动、监视、写入及 NI 清理结束，才解除设备占用。出现“等待停止”或“重试停止”时保留当前窗口，等正在执行的通信返回后重试；不能在这种状态下换程序、重启任务或把关闭窗口视为停止成功。已经送入 VISA 的操作无法强制撤回，停止任务也不会自动关闭信号输出。

旧设备列表如果以 DG4000、SSG 或通用 SCPI 不同类型保存了同一个 VISA 地址，0.7.15 仍可读取，但这些记录只能选一个连接，新保存也会拒绝跨类型的重复地址。先停止任务并断开，再保留合适的一条记录；不要通过另一条记录绕过“正在占用”。正在连接/断开时同资源也暂不可启动任务。设备设置窗口因任务尚未结束而拒绝关闭时，首页编辑或删除会停止操作并保留记录；通用 SCPI 原始命令仍在后台执行时也适用。SSG 窗口自身的首次连接/必要停止恢复仍可用，该权限不能由首页或别名记录借用来重连运行扫描。

0.7.15 保留原配置与任务格式。更新后，可编辑点表显示的小数位数可能比旧版多，这是为了保留原始浮点数值；JSON 中原有点值不因这次更新而改写成近似值。DG 手动操作自动恢复连接时若报告型号/序列号变化，请核对资源和仪器身份，通过重新读取身份或断开后重连确认当前设备，再重新应用配置或布防；软件不会继续向变化后的设备发送旧设置。

0.7.16 本地开发构建继续保留配置与任务格式。NI 独立模拟采集停止改为后台执行，读取线程或 NI/文件清理未完成时保留窗口、控制器和占用，失败后可重试停止。独立计数与 Newport 联动的 NI 清理失败也保留任务和租约供重试。连续文本的保存点数仅计入成功写入并刷新完成的数据块；写盘失败后保留文件供核查，不会自动重放失败块。详细恢复步骤见[使用说明第 8 节](../docs/USER_MANUAL_CN.md#87-停止异常与恢复)。

0.7.17 继续使用相同配置与任务格式，新增 DG4000 “通道与波形”页底部的只读回读区。回读频率、幅度、当前幅度单位和输出开关，不替换已保存的待应用编辑值；幅度单位沿用仪器的 Vpp/Vrms/dBm，不通过设置单位或负载来换算。具体操作与回读边界见[使用说明第 7 节](../docs/USER_MANUAL_CN.md#72-通道与波形)。

0.7.18 同样保留配置格式，在 SSG“连接与射频”和 Vaunix“射频输出”页底部增加独立的 Hz、dBm 与 RF 开关回读区。便携版本和源码均使用同一实现；回读结果不作为待应用参数保存。各设备支持范围与操作见[使用说明](../docs/USER_MANUAL_CN.md#13-各类设备的回读范围)。

NI-DAQmx 关闭报错且原生句柄已丢失时，显示“原生清理未确认”，软件不会把第二次无实际动作的关闭当成功，也不恢复旧句柄。此类状态无法在当前进程内用普通停止重试确认，需结束本次实验，在 NI MAX/驱动层核对资源后重新启动软件；原任务及占用持续保留。

## 4. 实机驱动与 Python 依赖的区别

内置 Python 包负责调用接口；Windows 的硬件驱动仍由对应厂商提供。便携包不会自动下载或安装系统驱动。

| 功能 | 发行包已经提供 | 目标电脑仍需准备 |
|---|---|---|
| 界面、配置、六台虚拟设备 | Python、PyQt6/Qt、应用资源 | 无硬件驱动要求 |
| DG4000、SIGLENT SSG、通用 SCPI | PyVISA | 支持设备的 x64 VISA 实现，例如 NI-VISA；按 USB/LAN 配置通信 |
| NI 计数与模拟输入 | nidaqmx Python 接口 | NI-DAQmx 系统驱动及正确识别的 NI 设备 |
| Newport SMC100CC / PR50CC | pyserial | 设备所用 USB 转串口驱动、可用 COM 端口 |
| Vaunix LMS/BLX | Python SDK 绑定；0.7.23 发行包内置 SDK 1.15 的 Windows x64 `vnx_fmsynth.dll` | 设备要求的 Windows USB/系统驱动 |

0.7.15 构建脚本将主目录提供的 Vaunix SDK DLL 内置到便携 ZIP 和单文件 EXE，构建后须在清单中确认 `vaunix_dll_bundled=true`。目录包的位置为 `_internal/vendor/vaunix/vnx_fmsynth.dll`；单文件 EXE 启动时将它随内置运行时解压到临时目录的 `vendor/vaunix/`。清单确认携带后，无需安装 SDK、复制外置 DLL 或设置环境变量，迁移时保留完整目录包或单文件 EXE 即可携带。

构建脚本自动包含项目主目录的 `vnx_fmsynth.dll`，没有该文件的构建仍可使用虚拟 Vaunix。可用 `--vaunix-dll` 显式覆盖来源；打包前拒绝 x86、ARM64 或损坏文件。实际携带情况及 SHA-256 以 `build-info.json`、`release-manifest.json` 为准。源码快照不包含厂商二进制，重新构建时将 DLL 放回主目录或显式提供路径。

运行时仍支持 `VAUNIX_LMS_DLL` 和 EXE 旁 `vendor/vaunix/vnx_fmsynth.dll` 覆盖内置版本；普通使用无需覆盖。DLL 必须为 Windows x64，不能将 x86 DLL 混入 x64 程序。

设置外置 DLL 路径示例：

```powershell
$env:VAUNIX_LMS_DLL = "D:\LabTools\Lab-Control-Center\vendor\vaunix\vnx_fmsynth.dll"
& ".\Lab Control Center.exe"
```

没有连接设备时，虚拟实验室仍能使用。真实设备连接失败应先核对 VISA/NI MAX/设备管理器中的识别和端口，再检查软件中的资源地址。安装 Python 不能替代这些系统驱动。

## 5. 日常操作与说明书

各设备的设置、触发时序和数据保存方式与源码版相同。目录包可以直接打开下列 Markdown 文件；单文件 EXE 的资源位于运行时临时目录，阅读文档可使用目录 ZIP 或 GitHub 中的同版本说明：

- 总使用说明：`docs/USER_MANUAL_CN.md`。
- 鼎阳 SSG3021X/系列兼容、两级扫描触发与脉冲 BUS：`docs/SIGLENT_SSG_CN.md`。
- Newport 旋转步进、P0 与采集：`docs/NEWPORT_ROTATION_CN.md`。
- 配置备份、导入及连接恢复：`docs/CONFIG_BACKUP_CN.md`。
- 开发、测试与维护：`docs/DEVELOPMENT_LOG_CN.md`。

以随包 Markdown 为准；旧 PDF 尚未重建，不能覆盖鼎阳等全部新增功能。软件测试使用虚拟设备和协议替身，便携构建成功不能替代现场仪器与电气时序验收。

## 6. 常见问题

| 现象 | 处理 |
|---|---|
| 报找不到 Python 或 PyQt6 | 核对是否误运行仓库根目录旧启动器；下载发行附件 |
| 目录包 EXE 提示缺少 DLL 或无法载入 Qt | 重新完整解压，保留 `_internal/`；检查文件是否被安全软件隔离 |
| 第一次启动单文件 EXE 较慢 | 等待内置运行时解压；频繁使用可选目录 ZIP |
| 软件可启动，仪器连接失败 | 按第 4 节检查系统驱动、位数、端口与资源地址 |
| 换电脑后原设备列表消失 | 核对是否复制了 `data/`，以及是否设置过外部 `--data-dir`/环境变量 |
| 修改参数后不能保存 | 将程序移到可写目录，或明确指定可写数据目录 |
| Vaunix 搜索失败 | 核对 x64 DLL 路径，关闭占用设备的厂商工具，再读取具体错误 |
| Windows 提示来源未知 | 核对文件来自项目发行页，并与 `SHA256SUMS.txt` 比对；遵循所在单位的软件运行策略 |

启动异常会尝试写入 EXE 旁 `logs/startup-error.log`；无法写入时尝试 `%LOCALAPPDATA%\RigolGeneratorControl\logs/startup-error.log`。反馈问题时附具体弹窗、日志和 `release-manifest.json`，无需反复安装外部 Python。

0.7.15 交互式 EXE 同时将可捕获的原生崩溃线程栈写入 `logs/runtime-fault.log`，不可写时使用相同本机目录。SSG 回读/显示异常保存在 `data/diagnostics/siglent-*.json`。若点击连接后退出，请保留存在的这些日志；单独的启动时间不证明底层驱动已经崩溃。

可运行不接触实体设备的随包自检，报告中会记录依赖加载位置、Qt 插件、图标、六台虚拟设备窗口，以及内置 Vaunix DLL 的路径、SHA-256、接口绑定和版本读取结果：

```powershell
Start-Process -FilePath ".\Lab Control Center.exe" -ArgumentList '--bundle-self-test ".\bundle-report.json"' -Wait -WindowStyle Hidden
Get-Content ".\bundle-report.json"
```

自检使用临时配置，结束后清理；它不枚举或连接真实设备，不能作为实机验收结果。普通使用无需执行该命令。

校验示例：

```powershell
Get-FileHash ".\Lab-Control-Center-0.7.23-windows-x64-setup.exe" -Algorithm SHA256
Get-FileHash ".\Lab-Control-Center-0.7.23-windows-x64-portable.zip" -Algorithm SHA256
Get-FileHash ".\Lab-Control-Center-0.7.23-windows-x64.exe" -Algorithm SHA256
```

## 7. 开发者构建

构建电脑使用 Windows x64 和官方 64 位 CPython，推荐 Python 3.13；目标使用电脑不需要安装 Python。发行构建脚本会拒绝 Conda 解释器及由其创建的构建环境，避免其 DLL 与 Qt 的依赖混用；这不影响原有 Conda 源码运行方式。首次建立构建环境会通过 pip 安装构建及应用依赖，构建电脑需能够访问依赖来源。运行时不会重复此过程。

在仓库根目录运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\build_release.ps1
```

主目录的 `vnx_fmsynth.dll` 会自动内置到目录便携版与独立 EXE，安装程序再使用同一已验证目录载荷。指定构建解释器或覆盖 Vaunix DLL 来源：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\build_release.ps1 -Python "C:\Python313\python.exe"
powershell -NoProfile -ExecutionPolicy Bypass -File .\tools\build_release.ps1 -VaunixDll "D:\SDK\x64\vnx_fmsynth.dll"
```

只有依赖已经按 `release/requirements-build.txt` 完整安装且未变更时，才使用 `-SkipInstall` 跳过依赖准备。传递给 Python 构建脚本的 DLL 参数名为 `--vaunix-dll`。构建脚本要求 Windows x64 和非 Conda 的官方 CPython；推荐与持续集成相同的 Python 3.13 x64。更换构建解释器时，应为该解释器重新创建构建环境，不复用 Conda 创建的旧环境。

入口脚本在 `build/release-venv/` 创建专用构建环境，由 `tools/build_release.py` 执行 PyInstaller 打包并写入 `dist/release/`。不要复制开发机虚拟环境作为便携包；发布的是打包后的运行时和应用。

输出：

```text
dist/release/
├─ Lab-Control-Center-0.7.23-windows-x64-setup.exe
├─ Lab-Control-Center-0.7.23-windows-x64-portable.zip
├─ Lab-Control-Center-0.7.23-windows-x64.exe
├─ Lab-Control-Center-0.7.23-windows-x64-source.zip
├─ RELEASE-NOTES.md
├─ SHA256SUMS.txt
└─ release-manifest.json
```

安装程序由 Inno Setup 编译器和 `release/installer.iss` 生成，`tools/build_installer.py` 可对已经验证的目录载荷构建 Setup。`build_release.ps1` 通过 `tools/ensure_inno_setup.ps1` 准备工作区 `build/tools/inno/` 内的编译器，并核对固定的 SHA-256 与发布者签名，目标使用电脑不需要该工具。源码快照应同时携带安装脚本。构建时拒绝把开发数据、安装标记或既有卸载器混入便携载荷，具体构建参数与安装验收以仓库脚本及本次清单为准。

构建完成后应检查三种格式的启动、中文/空格目录、默认及覆盖数据目录、配置保存重开、虚拟实验室，以及在没有外部 Python 搜索路径时的运行。安装版另在隔离的安装标识和临时目录中验收首次安装、旧版卸载、数据备份/恢复、卸载保留数据与阻止运行中替换；不要使用真实用户配置或实体仪器执行安装测试。校验和与版本记录随产物一起保存；不要将 `build/`、`dist/`、本机 `data/` 或构建环境提交到源码仓库。

`.github/workflows/release.yml` 提供同一构建流程：手动运行会生成可下载的 Actions artifact；推送 `v*` 标签时，在构建和测试成功、标签与应用版本一致后发布 GitHub Release 附件。源码提交和本地构建本身不会自动创建公开发行版。发版前核对版本与变更说明，避免把未验证的构建直接上传。

同一标签已经发布且所需附件齐全时，自动发布保留已上传的构建，不重复创建 Release 或覆盖已验证文件；附件不完整会报错。本仓库主目录的 Vaunix DLL 一并版本管理，远端构建也会自动包含它。

打包原理参考 PyInstaller 官方的[目录与单文件运行模式](https://pyinstaller.org/en/stable/operating-mode.html)和[运行时路径说明](https://pyinstaller.org/en/stable/runtime-information.html)。

当前只提供 Windows x64；没有提供 Windows x86、Linux 或 macOS 发行包。新版本需重新构建安装程序、便携 ZIP 和独立 EXE，不应以更新根目录小启动器替代发行打包。

## 8. 本版记录

0.7.23 新增安装程序 EXE、安装版自动升级与数据备份/恢复，按用户要求作为 [v0.7.23 GitHub Release](https://github.com/Rechoncy/Rigol_Generator_GUI/releases/tag/v0.7.23) 发行，详见[本版记录](NOTES-0.7.23.md)；原自动下载和更新提示实现分别见[0.7.22](NOTES-0.7.22.md)及[0.7.21](NOTES-0.7.21.md)。更新仅选择比当前版本更高的正式版，不会下载旧版进行降级。

此前正式发行的变更范围和验证保留在 [0.7.20 发行说明](NOTES-0.7.20.md)，DG 快速重连修复保留在 [0.7.19 说明](NOTES-0.7.19.md)，更早公开版本记录见 [0.7.18 发行说明](NOTES-0.7.18.md)与[0.7.15 发行说明](NOTES-0.7.15.md)。版本记录应区分自动测试、编译产物运行检查和实体硬件验收，不将任一项成功替代另外两项。

0.7.16 源码完整回归 444 项、0.7.17 源码完整回归 465 项及各版源码虚拟自检通过，作为历史记录保留。0.7.18 回读与冻结产物的逐项验收以本版说明、`release-manifest.json` 及 `dist/verification/0.7.18/` 为准，不能用旧版结果替代。上述版本均未实机验收。
