# 鼎阳 SIGLENT SSG 开发说明与变更记录

更新日期：2026-09-21

对应源码：0.7.0（包含此前开发的 SSG 扩展及便携发行支持）

用户操作见[详细使用说明](SIGLENT_SSG_CN.md)，全项目历史见[开发日志](DEVELOPMENT_LOG_CN.md)。

## 1. 需求与实现边界

用户的实际仪器为 **SSG3021X**。核心需求是通过 Python 控制固定射频、仪器原生触发步进、模拟调制及脉冲 BUS，并区分可以同时使用的功能。

扫描参数预装到仪器，外部信号直接触发仪器。实现不接入 NI 累计计数、软件逐点下发或 DG/Vaunix 的 backlog 控制器。NI-VISA 只承担通信后端职责。

当前支持 SSG3021X、SSG3032X、SSG6083A、SSG6085A、SSG6087A、SSG6082A-V 的明确分支；未知型号只读。完整 IQ/ARB 编辑、波形上传、双脉冲/脉冲串编辑、可选高功率范围、逐点实验数据记录均未实现。实机固件和电气时序尚未验收。

## 2. 文件与依赖关系

| 文件 | 责任 |
| --- | --- |
| [models.py](../src/rigol_control/models.py) | `DeviceKind.SIGLENT_SSG`，序列化值 `siglent_ssg` |
| [services.py](../src/rigol_control/services.py) | USB/LAN 资源发现及虚拟资源；实际连接后确认身份 |
| [connections.py](../src/rigol_control/connections.py) | 共享连接、虚拟后端缓存和设备租约 |
| [scpi.py](../src/rigol_control/drivers/scpi.py) | VISA 会话、可重入锁、`transaction()` |
| [siglent_ssg.py](../src/rigol_control/drivers/siglent_ssg.py) | 能力、参数模型、命令、校验、状态与虚拟后端 |
| [siglent_window.py](../src/rigol_control/ui/siglent_window.py) | 四页窗口、后台提交、实际状态、参数持久化 |
| [dialogs.py](../src/rigol_control/ui/dialogs.py) | 设备类型与 USB/LAN 输入 |
| [device_windows.py](../src/rigol_control/ui/device_windows.py) | 窗口工厂与通用参数保存基类 |
| [main_window.py](../src/rigol_control/ui/main_window.py) | 首页及六台虚拟设备入口 |
| [test_siglent_ssg.py](../tests/test_siglent_ssg.py) | 通用 SSG 驱动、触发层级、错误行为 |
| [test_ssg3021x_compatibility.py](../tests/test_ssg3021x_compatibility.py) | 普通 3000X 命令约束及 BUS 专项 |
| [test_siglent_ui.py](../tests/test_siglent_ui.py) | 窗口、配置、忙状态、租约与恢复 |

一个设备只使用管理器中的共享 `ScpiSession`。窗口不得另建一个 VISA 会话来轮询，也不能通过独立线程绕过会话锁。

## 3. 参数模型与单位

以下模型为 frozen dataclass，下发前由 UI 线程完整构造，后台不直接读取 Qt 控件。

| 模型 | 主要字段 | 约定 |
| --- | --- | --- |
| `SsgCapabilities` | model、serial、firmware、频率上下限、vector、fm_pm | `known` 决定写权限；`pulse_bus` 独立判断脉冲 BUS |
| `SsgPoint` | frequency_hz、power_dbm、dwell_s | Hz、dBm、秒 |
| `SsgSweep` | kind、target、起止值、points、dwell_s、shape、spacing、direction、mode、两级 trigger、slope、table | LIST 的 table 为点的 tuple |
| `SsgModulation` | master；AM/FM/PM 开关、源、波形、速率、深度/偏移；脉冲源、触发、周期、宽度、延时、沿、门极性 | AM 深度为百分比；FM 偏移 Hz；PM 偏移 rad；脉冲时间秒 |

`SsgSweep` 默认 STEP、FREQUENCY、1.0–1.1 GHz、11 点、30 ms、连续、扫描 AUTO、点 EXT。`SsgModulation` 默认所有开关关闭；恢复的编辑值不代表实机状态。

数值必须有限且在范围内。STEP 为 2–65535 点，LIST 为 2–500 行，驻留 0.01–100 s。SSG3021X 的频率为 9 kHz–2.1 GHz，功率按频段校验。扫描会同时检查每个实际生效点的固定频率或固定功率，不能只验证编辑框的端点。

## 4. 型号识别与兼容性

身份来自 `*IDN?`，不是用户输入的设备名称。重新识别会清除 `prepared`，避免沿用上一连接的准备状态。

- 普通 3000X 使用 `:FM:...`，不发送 FM1、IQ 或矢量调制查询。
- SSG6082A-V 的 FM 参数走 FM1，内部源为 INT1 / INT1,EXT；脉冲 KEY 映射为 SINGLE。其功率暂采用 −110 至 +10 dBm 保守范围。
- 6000A 的 FM/PM 依据官方硬件批次条件，仅匹配序列号模式 `SSG6[A-Z0-9]{2}[3-9][A-Z0-9]+` 时开放。
- 3000X 和 -V 开放脉冲 BUS；6083A/6085A/6087A 暂未开放。这不影响这些型号的扫描 BUS。
- IQE、Plus 或其他未列型号不按名称相似度自动继承写权限。
- 不猜测统一选件枚举命令。`*OPT?` 未被作为连接前提，界面显示选件未查询。

软件能力表是当前接入范围，不是所有型号/选件能力的完整声明。增加型号应先获得对应官方资料，再扩展能力和命令分支。

## 5. API 与命令映射

以下是项目内部 Python API，不是鼎阳官方 Python SDK。应用层应复用它，避免重复散落 SCPI 字符串。

| API | 主要命令 / 行为 |
| --- | --- |
| `identify()` | `*IDN?` |
| `set_frequency(hz)` | `:FREQuency`，校验当前功率 |
| `set_power(dbm)` | `:POWer`，校验当前频率 |
| `set_output(enabled)` | `:OUTPut`，独立于扫描与调制 |
| `prepare_sweep(config)` | 下表参数、完整回读，扫描须为 OFF |
| `start()` | 重新准备，再写 `:SWEep:STATe FREQUENCY/LEVEL/LEV_FREQ` |
| `execute_single()` | 核对已启用且 SINGLE，再发 `:SWEep:EXECute` |
| `stop()` | `:SWEep:STATe OFF` 并回读 |
| `bus_trigger()` | 单次发送全局 `*TRG`，失败不自动重发 |
| `pulse_bus_trigger()` | 验证实际脉冲条件后调用全局触发 |
| `apply_modulation(config)` | 参数配置、回读，再启用所选调制与 MOD |
| `snapshot()` | 快速实际状态；扫描启用时查询当前点 |
| `configuration_snapshot()` | 较完整的扫描/调制查询 |
| `read_list()` | 点数与分批列表查询 |
| `raw(command)` | 单条查询或写入，不接受分号和多行 |

### 5.1 扫描命令

| 参数 | SCPI |
| --- | --- |
| 类型 | `:SWEep:TYPE STEP/LIST` |
| 连续 / 单次 | `:SWEep:MODE CONTINUE/SINGLE` |
| 方向 | `:SWEep:DIRect FWD/REV` |
| 扫描触发 | `:SWEep:SWEep:TRIGger:TYPE AUTO/KEY/BUS/EXT` |
| 点触发 | `:SWEep:POINt:TRIGger:TYPE AUTO/KEY/BUS/EXT` |
| 外部沿 | `:INPut:TRIGger:SLOPe POSITIVE/NEGATIVE` |
| STEP 端点 | `:SWEep:STEP:STARt:FREQuency`、`STOP:FREQuency`、`STARt:LEVel`、`STOP:LEVel`（后面三项省略共同前缀） |
| 点数 / 驻留 | `:SWEep:STEP:POINts`、`:SWEep:STEP:DWELl` |
| 形状 / 间隔 | `:SWEep:STEP:SHAPe`、`:SWEep:STEP:SPACe` |
| LIST 清空 | `:SWEep:LIST:INITialize:PRESet` |
| LIST 编辑 | `:SWEep:LIST:CHANGe 1,f,p,t` 或 `:SWEep:LIST:ADDList f,p,t` |
| LIST 点数 / 内容 | `:SWEep:LIST:CPOint?`、`:SWEep:LIST:LIST? start,end` |
| 当前点 | `:SWEep:CURRent:DATA?` |

表中的斜杠表示可选枚举，不是实际发送的字符串。命令使用 SCPI 基准单位。

清空列表后允许返回 0 或 1 行：若保留一行，先替换第一行，再追加；若为零行，直接追加。上传后先确认行数，再每 50 行分批回读并比较全部三列。数量、解析或值不一致都不能标为准备成功。

### 5.2 扫描准备与启动的状态顺序

1. 本地校验配置；在会话事务内确认扫描 OFF，清除旧 `prepared`。
2. 读取功率传感器闭环、固定频率/功率和适用型号的 IQ 状态。
3. 闭环开启则拒绝；IQ 开启时有效扫描频率不得低于 10 MHz。
4. 写参数并检查 ESR；LIST 另做全量上传和回读。
5. 比较所有设置并确认扫描仍为 OFF，才保存 `prepared=config`。
6. 启动时 UI 比较编辑值与准备值；有变动要求重新应用。
7. 驱动启动再次执行准备，包括重新上传 LIST，复核面板可能修改的固定参数，然后启用扫描。
8. SINGLE 需要额外显式执行；开关保持启用不能解释为“本轮已完成”。

上述流程不改变 RF 或 MOD 总开关，也不关闭已有调制。中途失败可能留下部分参数，软件不承诺仪器级事务回滚。

## 6. 调制配置、组合与脉冲 BUS

### 6.1 调制组合

`apply_modulation()` 要求扫描 OFF，因此先设置调制、再准备并启用扫描。FM 与 PM 互斥；模拟调制最多同时选择两项。AM+Pulse 等组合会提示性能条件。-V 的 IQ 与模拟调制组合单独检查：IQ+Pulse 要求 RF Blanking=NONE，IQ 加两项模拟调制暂不放行。

通用 AM/FM/PM 编辑值即使对应开关未选中也会执行本地字段校验；脉冲内部时序则只在启用内部脉冲时校验。调制切换失败时，应查看具体字段及回读，不能仅凭复选框判断硬件成功。

### 6.2 脉冲命令的条件分支

| 条件 | 写入行为 |
| --- | --- |
| 内部源 | SINGLE 脉冲模式、周期、宽度、触发模式 |
| 内部 + EXT | 另写延时与外部触发沿 |
| 内部 + EGATE | 另写门极性 |
| 外部脉冲源 | 不写内部发生器时序和内部触发模式 |
| -V + KEY | 触发模式使用 SINGLE |
| 3000X + KEY | 触发模式使用 KEY |

缩短周期时先写宽度，其他情况先写周期，避免中间状态的宽度超过周期允许值。这里的“模式相关”指写入；详细参数查询仍会读取手册规定的脉冲参数，不能把它描述为只查询当前模式字段。

### 6.3 为什么增加 SSG3021X 脉冲 BUS

此前实现因编程手册枚举与说明不一致而未开放。后续核对[用户手册 C01B](https://store.siglent.com/wp-content/uploads/2025/07/SSG3000X_UserManual_C01B.pdf)第 8.4.4.13 节（印刷页 57 / PDF 第 63 页），确认列有 AUTO、KEY、BUS、EXT、EGATE，BUS 由 `*TRG` 触发。因此这是原软件的缺项，已加入 SSG3021X / SSG3032X 能力分支。

应用 BUS 的顺序：

1. 关闭 PULM，并回读确认。
2. 设置内部脉冲源、时序和 `:PULM:TRIGger:MODE BUS`。
3. 检查 ESR，并逐项核对参数；BUS 未被接受时结束，不回退 AUTO。
4. 全部参数验证后，才启用所选调制与 `:OUTPut:MODulation`。
5. 应用过程不发送 `*TRG`，不打开 RF。

触发按钮在同一会话事务内重新查询 `:PULM:SOURce?`、`:PULM:TRIGger:MODE?`、`:PULM:STATe?`、`:OUTPut:MODulation?`，确认内部源、BUS、PULM=1、MOD=1 后，只发送一次 `*TRG`。这样可检测应用后在面板上更改的模式。

`*TRG` 是全局事件，可能同时作用于 BUS 扫描或 BUS 点触发；按钮名称和使用说明明确标注这一点。驱动不要求扫描停止，也不替用户开启 RF。

## 7. 通信、错误和资源占用

### 7.1 会话事务与回读

`ScpiSession.transaction()` 使用现有可重入锁，使“查询前提 → 写入 → 验证”不会被本程序其他线程的命令插入。它不能阻止仪器面板或另一个外部程序修改仪器。

`_batch()` 在写入前后查询 `*ESR?`，用 `0x3C` 掩码检测相关错误，批量写入使用 `recover=False`。ESR 查询会清除已读事件；不能把之后的零值当作完整无错误历史。

数值比较采用 `math.isclose(rel_tol=1e-9, abs_tol=1e-9)`；枚举先作明确缩写归一化再比较，避免前缀匹配把 LEVEL 误当成 LEV_FREQ。真实固件若量化数值导致不一致，应记录实际分辨率并据官方规格调整，不能盲目放宽容差。

停止与 RF 关闭直接写入并验证，不让旧 ESR 阻止关断命令。停止、启动、执行和触发不自动重发。查询/写入返回错误不表示仪器一定没执行。

### 7.2 状态与租约

驱动状态为 `off`、`enabled`、`unknown`。`enabled` 包含等待和运行，不代表本轮仍在推进；不通过点号推断可靠的完成事件。

窗口每次后台操作获取所有者 `siglent-native:<device-id>` 的租约。成功回读扫描 OFF 后释放；已知型号失联或状态未知时保留占用，避免别的任务假定设备空闲。未知型号只读分支不长期保留扫描租约。

UI 的 `_run()` 串行提交工作。一般通信/回读异常将状态设为 unknown 并关闭会话，防止超时旧回复污染下一次查询；本地 ValueError 作为可解释的参数错误返回。重新连接先识别和回读，不重放上次启动/触发。

约 1500 ms 的轮询仅在扫描启用、已连接、窗口不忙时进行。扫描关闭时需手动刷新才能看见面板修改。停止/RF 关闭在忙时排队，不能中断当前 VISA 调用；窗口关闭会检查后台工作、待执行停止和租约。

## 8. 持久化与日志边界

沿用 `DeviceParameterRepository` 和设备稳定 ID，不新增独立的 SSG 配置数据库。控件以 `named/ssg.*` 语义键保存，列表通过窗口 custom 数据保存原始单元格文本。

以下是参数快照的局部示例，不是整个 `device_parameters.json` 的外层结构：

```json
{
  "controls": {
    "named/ssg.mod.pulse_trigger": {
      "type": "combo",
      "data": "BUS",
      "text": "总线 BUS"
    }
  },
  "custom": {
    "list": [
      ["1000000000", "-20", "0.03"],
      ["1050000000", "-18", "0.05"]
    ]
  }
}
```

保留草稿文本可防止尚未合法的列表编辑因关闭窗口而丢失；真正应用时再解析和校验。恢复限制最多 500 行，每行三项。自动保存约 500 ms，关闭前补存；恢复与导入只改 UI，不执行 RF、扫描、调制或触发命令。

配置归档复用原设备/参数导入导出链路。源码数据目录默认为 `%APPDATA%\RigolGeneratorControl`，便携编译版默认为 EXE 旁 `data/`，可用 `RIGOL_GUI_DATA_DIR` 隔离。

诊断页只保留约 300 个文本块，没有持久化 SSG 逐点数据、完整命令审计或硬件触发时间戳。当前点的界面显示时间不能作为真实触发时间。

## 9. 虚拟后端及其局限

`VirtualSsgBackend` 以状态字段、扫描路径和触发层级支持驱动/UI 测试。首页默认资源为 `SIM::SIGLENT::SSG6082A-V`；后端被连接管理器缓存，断开重连不等于复位模拟仪器。

虚拟后端区分扫描等待、点等待及驻留，支持模拟面板事件、外部沿和全局 BUS；自动推进在交互时按时钟更新，不是实时仪器仿真。一次全局 BUS 可同时推进扫描与增加脉冲触发计数。

SSG3021X 专项后端禁止矢量命令，并检查脉冲模式相关写入和周期/宽度顺序。它仍基于虚拟后端，不等同于与驱动完全独立的固件实现。虚拟驻留期间忽略事件等规则不能当作硬件结论；锁定时间、输出功率、真实触发带宽和忙时行为必须实测。

## 10. 测试与验收

### 10.1 自动测试范围

| 测试文件 | 重点 |
| --- | --- |
| `test_siglent_ssg.py` | 能力范围、STEP/LIST、两级触发、单次、调制保留、回读、错误与非重试 |
| `test_ssg3021x_compatibility.py` | 不发 -V 命令；条件脉冲参数；枚举别名；BUS 成功、忽略、ESR 拒绝、面板更改和触发失联 |
| `test_siglent_ui.py` | 窗口、参数恢复、配置归档、脉冲控件、BUS 按钮、失败后的占用与恢复 |
| `test_ui_smoke.py` | 六台虚拟设备集成入口 |
| 全项目回归 | 既有 DG/Vaunix/Newport/NI、配置迁移和启动器 |

最近一次功能回归（脉冲 BUS 加入后）：**201 项测试通过，22.401 s，git diff --check 通过**。这是自动测试记录，不是实机验收。本文档整理本身不增加功能测试数。

在仓库根目录用项目所用的 Python 环境运行：

```powershell
$env:PYTHONPATH = "src"
$env:QT_QPA_PLATFORM = "offscreen"
python -m unittest discover -s tests -p "test_siglent*.py" -v
python -m unittest discover -s tests -p "test_ssg3021x_compatibility.py" -v
python -m unittest discover -s tests -v
git diff --check
```

测试应使用临时参数目录，不能覆盖用户现场配置。只修改文档时检查链接、示例、代码一致性与 diff；修改协议行为后先运行相应专项，再执行需要的全项目回归。

### 10.2 实机交付记录

每次实机验收至少保存：日期、软件提交或源码版本、完整 IDN、固件、实际选件、连接方式、设置/回读、测试接线和测量结果。

按[使用说明第 13 节](SIGLENT_SSG_CN.md#13-虚拟设备与实机验收)确认 STEP/LIST 首末点、KEY/BUS/EXT 两级触发、单次执行、脉冲 BUS 波形、并用组合、断线恢复及不隐式开启 RF。外部触发的电平、脉宽、间隔和忙时行为需要独立记录。尚无这些实机结果。

## 11. 本次开发过程

以下阶段均发生于 2026-09-21，现随 0.7.0 一并发行。便携版本新增的运行/构建测试使全项目回归达到 215 项，原阶段计数仍保留。

| 阶段 | 问题 / 决定 | 实现与验证 |
| --- | --- | --- |
| 基础接入 | 使用仪器内置扫描，不增加 NI 逐点任务 | 新设备类型、共享 VISA、四页窗口、STEP/LIST、两级触发、调制、虚拟机和持久化；全项目 180 项通过 |
| SSG3021X 专项 | 普通 3000X 不能套用 -V 命令；脉冲字段需要按模式处理 | 修正模式相关写入、宽度/周期顺序、枚举比较；增加 12 项驱动和 2 项 UI；全项目 194 项通过 |
| 脉冲 BUS 补齐 | 用户询问是硬件不支持还是软件缺项；官方用户手册确认支持 | 新能力、选择项、验证后启用及全局触发按钮；增加 6 项驱动和 1 项 UI；全项目 201 项通过 |
| 使用与开发文档 | 需要可操作说明与后续维护依据 | 完整使用流程、组合/边界、命令与状态设计、保存结构、故障路径、测试和验收；同步总日志和文档入口 |

历史日志保留最初“BUS 未开放”的记载，以说明当时的软件范围；当前结论以 BUS 补齐记录为准。6000A 普通型号的脉冲 BUS 不因 3000X 的证据自动开放。

## 12. 后续维护规则与待办

扩展协议时依次更新：官方证据与型号能力 → 参数模型/校验 → 类型化命令和回读 → 虚拟/约束后端 → UI/持久化 → 专项测试 → 用户文档和日志。增加型号至少测试不支持分支，不能只测试新型号正常路径。

优先待办：

1. 用用户 SSG3021X 记录固件并完成实机验收，尤其 BUS 回读与真实脉冲输出。
2. 核实首点、驻留期间触发和同源两级触发的具体固件行为，补充可复现记录。
3. 如需开放 6000A 脉冲 BUS、IQE 或 Plus，逐型号核对资料及命令，不扩大通用枚举来绕过能力限制。
4. 若需要逐点采集，另行设计实际硬件同步和数据时间依据；不要把 1.5 s UI 轮询当成采集。
5. 新版 PDF 若用于分发，应另行重建并逐页检查；当前 Markdown 是新增功能的完整说明。

官方手册入口与型号差异统一维护在[使用说明第 14、15 节](SIGLENT_SSG_CN.md#14-其他型号差异)。
