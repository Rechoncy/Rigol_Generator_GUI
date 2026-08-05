# DG4000 + NI 多设备联动控制

这是一个面向 RIGOL DG4000 系列信号发生器与 NI DAQ 的多设备桌面控制程序。首页用于保存和管理多台设备；点击设备卡片后，会打开独立的设备设置窗口，多台设备窗口可同时存在。

## 当前已实现

- 多设备首页：添加、编辑、删除、保存和打开设备。
- 首页设备卡片支持连接和断开；VISA 会话在首页与设置窗口之间共享，关闭设置窗口不会自动断开。
- 添加/编辑设备时自动搜索 VISA 资源或 NI-DAQmx 设备，并通过可编辑下拉框选择；未安装驱动时仍可手动输入。
- 支持 DG4000、NI DAQ 和通用 SCPI 三类设备记录。
- 本地版本化 JSON 配置和原子写入，防止重复保存同一资源。
- 后台设备检测；没有安装仪器驱动时，界面仍可正常启动和配置。
- DG4000 设置窗口：总览、通道与波形、AM/FM/PM/ASK/FSK/PSK/PWM、扫频、脉冲串、触发步进、状态与 SCPI 控制台。
- NI 设置窗口：触发输入、计数器、路由和诊断；界面不使用 AO，也不采集 DG4000 输出。
- 循环步进核心语义：第一个触发应用起始点，之后逐点推进，末点后回到起始点。
- 无最小触发间隔模型：`received_count - applied_count` 是有序积压量，不主动丢弃触发。
- PyVISA 会话：连接/断开、后台命令执行、原始查询和写入、单会话串行化。
- 常用 DG4000 命令已接线：双通道基本波形与输出、调制、扫频、脉冲串和系统命令。
- 完整 SCPI 通道：系统没有屏蔽命令，编程手册中的其他命令可从 SCPI 控制台直接执行。
- NI-DAQmx 累计边沿计数器和双线程联动控制器已实现，频率、幅度、相位、PSK 相位、FSK 频率均可作为步进目标。
- 步进任务具备独立的版本化持久化模型，便于后续在界面中复用任务模板。
- 内置虚拟实验室：无需 PyVISA、NI-DAQmx 或实体设备即可体验 DG4000 控制和 NI 触发联动。
- 虚拟 NI 支持单次、批量、连续触发；虚拟 DG 支持可调 SCPI 延迟、实时状态和积压调试。

没有安装驱动时，硬件操作会给出明确错误，不会假装已经下发命令。当前开发机没有连接实体 DG4000/NI 设备，所以自动测试使用了可替换的模拟 VISA、模拟硬件计数器和慢速 SCPI 写入器；实机通信仍需按下文安装厂商驱动后验证资源名、PFI 和计数器能力。

## 运行环境

- Windows 10/11
- Python 3.10 或更高版本
- PyQt6
- 实机控制时需要 NI-VISA 或其他 VISA Backend、NI-DAQmx 驱动，以及可选 Python 包 `PyVISA`、`nidaqmx`

> NI 设备资源名以 NI MAX 中显示的名称为准，例如 `Dev1`。软件不依赖用户口述的具体 USB 型号来猜测设备能力，连接后将读取驱动报告的真实产品类型。

## 安装与启动

在项目目录执行：

```powershell
python -m pip install -e .
python -m rigol_control
```

需要实机控制依赖时：

```powershell
python -m pip install -e ".[instrument]"
```

也可以不安装项目，直接从源码启动：

```powershell
$env:PYTHONPATH = "src"
python -m rigol_control
```

设备配置默认保存在 `%APPDATA%\RigolGeneratorControl\devices.json`。开发和测试时可指定独立目录：

```powershell
python -m rigol_control --data-dir .\runtime-data
```

也可以直接双击或在终端中运行（不依赖 PowerShell 脚本执行策略）：

```powershell
.\run_app.cmd
```

## 无硬件快速体验

1. 启动软件并点击首页“虚拟实验室”。
2. 等待虚拟 DG4162 和虚拟 NI USB-6363 显示“已连接”。
3. 同时打开两个设置窗口，在 DG 的“触发步进”页启动任务。
4. 在 NI 总览中发送单次、批量或连续触发。
5. 调高 DG 触发页的“模拟下发延迟”，可观察积压增长并最终追平。

虚拟资源为 `SIM::DG4000::1` 和 `SIM::NI_DAQ::1`，不产生任何物理输出。

详细操作、实机接线、时序解释和故障排查见 [中文使用说明 PDF](output/pdf/DG4000_NI_Control_User_Manual_CN.pdf)，可编辑文本版见 [docs/USER_MANUAL_CN.md](docs/USER_MANUAL_CN.md)。

## 首次实机配置

1. 在 NI MAX 中确认 DG4000 的 VISA 资源地址可以执行 `*IDN?`，并记下 NI 采集卡设备名（例如 `Dev1`）。
2. 首页分别添加一台 `RIGOL DG4000` 和一台 `NI 数据采集卡`。NI 资源地址填写设备名，而不是 USB 产品描述。
3. 在首页设备卡片点击“连接”，或打开 DG4000 卡片后在“总览”页连接。成功后设置窗口会显示 `*IDN?` 返回值；两处复用同一个 VISA 会话。
4. 在“通道与波形”等页面配置普通输出；编程手册的任意其他功能可在“存储与系统 → SCPI 控制台”执行。
5. 进入“触发步进”，选择参数目标、DG 通道、NI 设备、计数器、PFI 端子和边沿，编辑循环序列后点击“启动联动”。
6. 第一个外部边沿到达前，软件不会提前下发起始点；第一个边沿到达后才下发并保持序列第 1 点。后续边沿逐点推进并永久循环。

## 测试

```powershell
$env:PYTHONPATH = "src"
$env:QT_QPA_PLATFORM = "offscreen"
python -m unittest discover -s tests -v
python -m rigol_control --smoke-test --data-dir .\runtime-data\smoke
```

## 联动时序原则

假设序列为 `P0, P1, ... Pn`：

- 收到触发 1：下发并保持 `P0`。
- 收到触发 2：下发 `P1`。
- 收到触发 `n+1`：下发 `Pn`。
- 收到触发 `n+2`：重新下发 `P0`，持续循环。
- NI 硬件计数器记录收到的触发总数；DG4000 写入线程记录完成下发的总数。
- 不设置最小触发间隔。控制器读取的是硬件累计计数，因此两次软件读取之间出现多个边沿时会看到计数跳变，并逐个补齐所有序号；读取周期不是触发死区，也不会合并序列点。
- 若触发速度高于 VISA/SCPI 的实际下发速度，差值形成有序积压；参数仍按触发顺序补发，但物理生效时刻会相对触发滞后。积压不会凭空提高 DG4000 的 SCPI 吞吐量。

触发步进的目标命令包括：

| 参数 | SCPI 模板 |
|---|---|
| 输出频率 | `:SOUR1:FREQ <value>` |
| 输出幅度 | `:SOUR1:VOLT <value>` |
| 输出相位 | `:SOUR1:PHAS <value>` |
| PSK 调制相位 | `:SOUR1:MOD:PSK:PHAS <value>` |
| FSK 跳跃频率 | `:SOUR1:MOD:FSK:FREQ <value>` |

项目内的 `DG4000_ProgrammingGuide_CN.chm` 是原始手册，不会被程序修改。
