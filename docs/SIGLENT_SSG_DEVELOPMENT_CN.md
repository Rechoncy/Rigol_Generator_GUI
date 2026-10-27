# 鼎阳 SIGLENT SSG 开发说明与变更记录

更新日期：2026-10-27

对应源码：0.7.18（独立的频率、功率与 RF 状态只读回读；保留原生 STEP/LIST 与恢复规则）

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
| [siglent_window.py](../src/rigol_control/ui/siglent_window.py) | 五页窗口、后台提交、实际状态、参数持久化 |
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

`units.py` 新增 `rf_power` 非线性单位族，基准为 dBm，电压按 50 Ω 正弦载波换算。`RfPowerWidget` 缓存基准 dBm，单位切换只改变显示，避免 STEP 准备状态因舍入而失效。LIST 对未编辑单元格通过 UserRole 保留基准，单位切换先校验全部幅度再更新；任何非法行不部分换算。

`set_power(value, unit="dBm")` 在通信前转换单位，随后复用原型号限值。`SsgPoint.power_dbm`、`SsgSweep.start_dbm/stop_dbm` 仍是 dBm，仪器列表也保持 dBm。新快照记录单位，旧 `double` 参数迁移为 dBm 编辑器，旧 custom 列表未记录单位时默认 dBm。

`_power_close()` 按 0.01 dB 设置分辨率比较幅度回读，容差为一个步长加 1e-9 浮点余量；仅作用于固定幅度、STEP 幅度端点及 LIST 幅度列。100 mV 换算后有多位小数，量化为 −6.99 dBm 不应误报失败。频率/驻留仍使用原容差，RF 输出准确度不作为读回容差，超过一个步长仍失败。

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
| `set_power(value, unit="dBm")` | mV/mVpp 换算为 dBm 后发 `:POWer`，校验当前频率 |
| `set_output(enabled)` | `:OUTPut`，独立于扫描与调制 |
| `read_output_parameters()` | 事务内只查询 `:FREQuency?`、`:POWer?`、`:OUTPut?`，返回 Hz、dBm、RF 与连接代次；不自动恢复连接 |
| `prepare_sweep(config)` | 下表参数、完整回读，扫描须为 OFF |
| `start()` | 重新准备，再写 `:SWEep:STATe FREQUENCY/LEVEL/LEV_FREQ` |
| `execute_single()` | 核对已启用且 SINGLE，再发 `:SWEep:EXECute` |
| `stop()` | `:SWEep:STATe OFF` 并回读 |
| `bus_trigger()` | 单次发送全局 `*TRG`，失败不自动重发 |
| `pulse_bus_trigger()` | 验证实际脉冲条件后调用全局触发 |
| `apply_modulation(config)` | 参数配置、回读，再启用所选调制与 MOD |
| `snapshot(include_modulation=True, include_current_point=False)` | RF/扫描；显式当前值开关读取频率与幅度，UI 基础任务均使用 False |
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
| 当前扫描值 | `:SWEep:CURRent:FREQuency?` / `:SWEep:CURRent:LEVel?`；不查询 DATA 或推断点号 |

表中的斜杠表示可选枚举，不是实际发送的字符串。命令使用 SCPI 基准单位。AM 编辑值为百分比，`:AM:DEPTh` 下发及回读比较值为 `am_depth / 100`（0～1）。

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

缩短周期时先写宽度，其他情况先写周期，避免中间状态的宽度超过周期允许值。详细查询也按实际脉冲源、类型和触发模式选择内部周期/宽度、EXT 延时/沿、EGATE 极性；从 PTRain 改为单脉冲前先确认内部源和 SINGLE，再查周期。

### 6.3 为什么增加 SSG3021X 脉冲 BUS

此前实现因编程手册枚举与说明不一致而未开放。后续核对[用户手册 C01B](https://store.siglent.com/wp-content/uploads/2025/07/SSG3000X_UserManual_C01B.pdf)第 8.4.4.13 节（印刷页 57 / PDF 第 63 页），确认列有 AUTO、KEY、BUS、EXT、EGATE，BUS 由 `*TRG` 触发。因此这是原软件的缺项，已加入 SSG3021X / SSG3032X 能力分支。

应用 BUS 的顺序：

1. 关闭 PULM，并回读确认。
2. 设置内部脉冲源、时序和 `:PULM:TRIGger:MODE BUS`。
3. 检查 ESR，并逐项核对参数；BUS 未被接受时结束，不回退 AUTO。
4. 全部参数验证后，才启用所选调制与 `modulation_header`（普通 3000X 为 `:MODulation`，其他已接入型号保留 `:OUTPut:MODulation`）。
5. 应用过程不发送 `*TRG`，不打开 RF。

触发按钮在同一会话事务内重新查询 `:PULM:SOURce?`、`:PULM:TRIGger:MODE?`、`:PULM:STATe?`、`modulation_header + "?"`，确认内部源、BUS、PULM=1、MOD=1 后，只发送一次 `*TRG`。普通 3000X 使用 `:MODulation?`；这样可检测应用后在面板上更改的模式。

`*TRG` 是全局事件，可能同时作用于 BUS 扫描或 BUS 点触发；按钮名称和使用说明明确标注这一点。驱动不要求扫描停止，也不替用户开启 RF。

## 7. 通信、错误和资源占用

### 7.1 会话事务与回读

`ScpiSession.transaction()` 使用现有可重入锁，使“查询前提 → 写入 → 验证”不会被本程序其他线程的命令插入。它不能阻止仪器面板或另一个外部程序修改仪器。

`_batch()` 在本批写入前和每条写入后查询 `*ESR?`，用 `0x3C` 掩码检测相关错误，每条写入使用 `recover=False`，首次发现错误即停止。写入前的历史错误与写入后的具体命令错误使用不同提示，明确本批是否已经发送。ESR 查询会清除已读事件；不能把之后的零值当作完整无错误历史。

数值比较采用 `math.isclose(rel_tol=1e-9, abs_tol=1e-9)`；枚举先作明确缩写归一化再比较，避免前缀匹配把 LEVEL 误当成 LEV_FREQ。真实固件若量化数值导致不一致，应记录实际分辨率并据官方规格调整，不能盲目放宽容差。

停止与 RF 关闭直接写入并验证，不让旧 ESR 阻止关断命令。停止、启动、执行和触发不自动重发。查询/写入返回错误不表示仪器一定没执行。

### 7.2 状态与租约

驱动状态为 `off`、`enabled`、`unknown`。`enabled` 包含等待和运行，不代表本轮仍在推进；不通过点号推断可靠的完成事件。

窗口每次后台操作获取所有者 `siglent-native:<device-id>` 的临时租约。是否保留由 `scan_may_be_active` 决定：读到扫描启用，或发送可能启动扫描的命令前设为 true；只有确认 OFF 后才清除。首次状态读取失败而未发现扫描运行证据时释放临时租约，不再因型号已识别就永久锁住窗口。源码状态 unknown 本身不等于存在运行任务。

UI 的 `_run()` 串行提交工作。`SsgCommandError` 表示收到明确的错误/不一致回复，与本地 ValueError 一起保留连接并反馈；传输异常将状态设为 unknown 并关闭会话，防止超时旧回复污染下一次查询。日志区分操作阶段与后续状态读取。重新连接先识别和回读，不重放上次启动/触发。

0.7.4 在基础连接、刷新、RF 操作和扫描轮询中调用 `snapshot(include_modulation=False)`，这些路径不读取 AM/FM/PM/PULM、MOD 或 IQ/ARB。`_run(read_modulation=True)` 只用于独立调制读取、应用调制及显式详细读取。普通型号总开关的实际命令通过 `modulation_header` 选择主命令；快照仍使用规范化键 `:OUTPut:MODulation` 兼容旧 API，此键不代表线上的实际命令。诊断记录实际命令头。调制显示注明读取时间，基础刷新不会冒充再次读取调制。

`_show_identity()` 在成功和失败回调中单独更新型号/序列号/固件。身份已经识别但后续查询失败时保留最近身份，并注明当前未连接。显式查询真实超时仍关闭会话；不能为了“保持连接”直接继续使用可能遗留回复的会话，也不自动尝试所有命令别名。

0.7.3 的完成回调捕获显示/结果处理异常并在 `finally` 中结束忙状态和处理租约。工作线程的异常捕获不能保护 Qt 主线程回调，不能仅因后台成功就假定 UI 转换无错误。`_show_snapshot()` 先保存原始 JSON，再格式化数值；非法幅度保存命令名、原始回复及 traceback，不退出程序。真实扫描启用时仍保留租约，确认 OFF 后释放。

`_run_pending_safety()` 在定时回调真正执行时才出队，避免队列已空但动作尚未开始的间隙允许窗口关闭。回调失败仍推进排队动作。冻结自检须实际点击连接按钮并模拟异常回复；仅打开窗口不能覆盖结果转换路径。交互式冻结入口同时启用 faulthandler 原生崩溃日志，保持文件句柄直到 Qt 退出。

约 1500 ms 的轮询仅在扫描启用、已连接、窗口不忙时进行。扫描关闭时需手动刷新才能看见面板修改。停止/RF 关闭在忙时排队，不能中断当前 VISA 调用；窗口关闭会检查后台工作、待执行停止和租约。

0.7.6 的 `_safety()` 将重连、身份校验、单次关断和对应 OFF 回读打包为排队任务；调用 `_run(read_snapshot=False)` 避免其他状态查询掩盖成功。只有用户点击关断按钮才能走此路径，不自动重试已失败的写命令。重连前 `info()` 清理连接管理器中的失效标志，随后以实际 `*IDN?` 身份比较型号/序列号；新身份验证通过后才更新能力并清空 `prepared`，保留扫描风险证据直至 OFF。未知/不匹配身份拒绝写入。

`stop()` 使用 `read_sweep_state()` 确认停止，若回读仍启用则保留状态与占用并报告失败。RF OFF 不释放尚未确认结束的扫描占用。基础快照传 `include_current_point=False`；v0.7.7 的“刷新当前值预览”及用户勾选的自动刷新传 True。该兼容参数名现表示两个独立数值查询，不再返回点号。不在超时后继续同一会话读取其他回复。

0.7.7 的预览状态保存于 `_current_scan_preview`，含 `frequency_hz`、`power_dbm` 和带时区的本机 `read_at`。两条查询不是原子快照。`scan_preview_auto` 默认关闭并设置 `skipDeviceParameterPersistence=True`；基础刷新不更新时间戳。失败时关闭自动刷新并清空缓存；停止、重连、断开及重新布防使缓存失效。频率必须为正有限数，幅度必须为有限数；异常保留命令和原始回复。

`scan_monitor` 诊断字段中的 `trigger_received`、`trigger_backlog` 均为 null，界面为“未知”。C01A 未提供对应查询，不从采样次数、设定点表或值变化推算。`current_values` 为上述缓存或 null，另保存 `auto_refresh`、`status`；这不是硬件触发时间戳或逐点记录。

### 7.3 专用参数回读（0.7.18）

`read_output_parameters()` 返回不可变的 `SsgOutputReadback`。在一个会话事务内，使用 `recover=False` 依次执行 `:FREQuency?`、`:POWer?`、`:OUTPut?`；验证正有限频率、有限功率与明确 RF 布尔值，保留失败命令和原始回复。读取前后核对连接代次，不调用完整 `snapshot()`、ESR、调制或扫描状态查询，不改变 `prepared`、`state` 或 `scan_may_be_active`。

| 已有型号的依据 | `:OUTPut?` | `:FREQuency?` | `:POWer?` |
|---|---|---|---|
| 用户提供普通 `SSG3000X_ProgrammingGuide_C01A.pdf` | 3.3.1，印刷 30 / PDF 34 页，0/1 | 3.4.3.2，印刷 32 / PDF 36 页，Hz | 3.4.4.2，印刷 34 / PDF 38 页，dBm |
| [官方 SSG6080A 手册](https://www.siglent.com/wp-content/uploads/2024/09/SSG6080A_ProgrammingGuide_C01B.pdf) | 3.3.1，印刷 29 / PDF 33 页，0/1 | 3.4.3.2，印刷 31 / PDF 35 页，Hz | 3.4.4.2，印刷 32 / PDF 36 页，dBm |
| [官方 SSG6082A-V EN01A](https://siglentna.com/wp-content/uploads/dlm_uploads/2025/04/SSG6082A-V_ProgrammingGuide_EN01A.pdf) | 3.3.1，印刷 33 / PDF 37 页，0/1 | 3.4.3.2，印刷 35 / PDF 39 页，Hz | 3.4.4.2，印刷 37 / PDF 41 页，dBm |

上述三种基础查询共用命令头，型号能力表不因此扩展。回读始终标 dBm，不依据编辑框单位假设返回 mV，也不发送单位设置。原生 STEP/LIST 当前值预览继续使用 `SWEep:CURRent:FREQuency?` / `LEVel?`，不把载波配置误作扫描当前点。

UI 在“连接与射频”页底部显示独立的频率、功率及 RF 卡片，只有用户点击时请求，不覆盖编辑控件或保存参数。请求在后台去重；连接代次变化、断连或异常使整组值和时间失效，迟到结果不覆盖新连接。这个专用只读路径不重新获取或释放原生扫描租约，不触发自动恢复或参数重放。它与既有原生扫描监视/停止共用串行访问，因此一次手动读取可能延后排队操作。时间为本机完成时间，顺序参数回读不构成瞬时电气测量。

## 8. 持久化与日志边界

0.7.5 在主窗口与所有基础设备窗口标题使用 `rigol_control.__version__`，移除主窗口写死的 v0.6.0 文案。SSG 诊断页、初始日志及错误 JSON 记录实际版本；JSON 增加 `executable`、`module_path`、`frozen`，从当前进程获取，不从目录名猜测版本。冻结自检校验 7 个窗口标题和错误报告的运行身份。

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

诊断页保留约 300 个文本块，错误自动保存到 `data/diagnostics/siglent-*.json`，也可手动保存；包含身份、版本、资源和最近错误快照。没有持久化 SSG 逐点数据、完整命令审计或硬件触发时间戳。当前值的界面读取时间不能作为真实触发时间。

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
| `test_siglent_readback.py` | 14 项：三项纯查询、各型号共用基础命令头、非法回复、整组失效、慢调用、连接代次、原生扫描租约保留与排队停止边界 |
| `test_ui_smoke.py` | 六台虚拟设备集成入口 |
| 全项目回归 | 既有 DG/Vaunix/Newport/NI、配置迁移和启动器 |

历史脉冲 BUS 加入阶段：201 项测试通过，22.401 s，git diff --check 通过。本轮 0.7.18 的实际回归及自检另见[本版说明](../release/NOTES-0.7.18.md)，不使用历史计数代替本版验收。上述自动测试均不等于实机验收。

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

2026-09-26 的 0.7.1 修复 AM 比例、读取失败后的占用、模式相关查询和错误诊断，全量 226 项回归通过，依据及未确认事项见[专项核对报告](SIGLENT_SSG3021X_AUDIT_20260926_CN.md)。

以下阶段均发生于 2026-09-21，随 0.7.0 一并发行。便携版本新增的运行/构建测试使当时全项目回归达到 215 项，原阶段计数仍保留。

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


## 查询等待与界面响应（0.7.8）

`ScpiSession.is_connected` / `identity` 改读 `_status_lock` 保护的缓存，不获取通信 `_lock`。状态锁仅用于短暂发布/读取，不包含 I/O，也不反向获取通信锁。首次 IDN 成功后发布已连接，close 前发布未连接；串行自动重连保留最近发布状态，成功后更新，失败清空，避免管理器把短暂重连当作最终断开。读写及 transaction 继续串行化。

过去的立即抛异常替身不能覆盖主线程等待同一通信锁的故障。新增测试在真实 session 锁内阻塞后端，检查首页同步状态、Qt 事件循环和实际停止按钮；停止排队至读取退出，再走既有恢复路径。不强杀线程或并发 close/write，底层 DLL 不返回仍是限制。


## STEP/LIST 页面与参数迁移（0.7.9）

源码 `_sweep_page(kind)` 分别创建 STEP 和 LIST 页，STEP 使用原 `sweep.*` 稳定键，LIST 使用 `list_sweep.*`，列表行/幅度单位沿用已有 custom 数据。旧配置没有任何 list_sweep 键时，将旧共用字段复制给 LIST 一次，之后独立持久化；旧 sweep.kind 不再驱动界面。切页只更新编辑模式并移动同一个底部监视 widget，不触发 SCPI、重置预览或创建额外计时器。

底部使用公共 metric_card；频率和幅度分卡显示，仪器接收/积压仍未知。整个预览 widget 标记跳过持久化。apply_sweep(kind)、start_sweep(kind, single=...) 绑定各页，启动/单次必须匹配 driver.prepared，防止当前编辑页误用另一页配置。驱动与硬件状态仍共用，同一仪器只能执行一个扫描。源码、测试、开发日志先修改，再由相同内容生成 EXE 与源码包。
