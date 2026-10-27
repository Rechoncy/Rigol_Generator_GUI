# Lab Control Center 0.7.18

日期：2026-10-27。
状态：本地开发构建；未创建 v0.7.18 标签或 GitHub Release。公开正式下载仍为 [v0.7.15](https://github.com/Rechoncy/Rigol_Generator_GUI/releases/tag/v0.7.15)，此前产物和验证记录保留。

## 其他信号源的频率与功率回读

在 0.7.17 的 DG4000 CH1/CH2 手动回读基础上，将独立显示扩展到鼎阳 SIGLENT SSG 与 Vaunix LMS/BLX。各信号源均可按需回读频率、幅度/绝对功率与输出开关，显示本机回读完成时间，不覆盖编辑框或持久化参数，不自动打开输出或启动扫描。

回读在后台完成，重复点击合并为一次；失败、无效回复、断连或连接更换后，旧值不继续表示当前状态。不增加自动轮询或 NI 每触发点查询。专用只读回读与正常写入共用串行访问，会占用一段通信时间；普通手动设置继续受设备占用保护。

输出关闭时，读到的频率和功率是仪器保存的参数。扫频、序列、脉冲或调制运行时，这些查询不能替代瞬时电气测量，也不提供硬件触发时间戳；频率、功率及开关的顺序查询不是同时采样。

## 查询依据与入口

- **鼎阳 SSG**：“连接与射频”页底部 → “回读频率 / 幅度”。按正确的普通 `SSG3000X_ProgrammingGuide_C01A.pdf` 查询 `:FREQuency?`（Hz）、`:POWer?`（dBm）、`:OUTPut?`（0/1）；与既有扫描页当前值预览分开，不附加 ESR、扫描或调制查询。已接入的 6000A/-V 基础头也按官方手册核对，页码和来源见[SSG 开发说明](../docs/SIGLENT_SSG_DEVELOPMENT_CN.md#73-专用参数回读0718)。
- **Vaunix LMS/BLX**：“射频输出”页底部 → “回读频率 / 幅度”；“总览”提供同名跳转入口。SDK API 手册 V1.13 第 3.6 节规定 `GetFrequencyEx` 为 10 Hz 编码，`GetAbsPowerLevel` 为 0.25 dB 编码的绝对功率，`GetRF_On` 为 0/1；回读同时检查句柄连接/打开状态。`GetPowerLevel` 是相对衰减，不能直接当作 dBm。

SSG 的功率查询固定返回 dBm，Vaunix 显示 SDK 绝对功率 dBm，保留 DG4000 的仪器实际 Vpp/Vrms/dBm 单位。三种回读均不靠写入单位、负载或参考源来获得显示值。

## 项目设备范围

| 设备 | 本版处理 |
|---|---|
| RIGOL DG4000 | 保留双通道频率、实际 Vpp/Vrms/dBm 和输出开关回读 |
| 鼎阳 SIGLENT SSG | 新增独立载波频率、dBm 功率和 RF 开关回读；原生扫描当前值预览独立保留 |
| Vaunix LMS/BLX | 新增唯一射频通道的频率、dBm 绝对功率和 RF 开关回读 |
| NI DAQ | 项目内负责模拟输入与边沿计数，没有实现 AO 信号源 |
| Newport SMC100CC / PR50CC | 已有角度、速度和运动状态回读，不适用射频频率/幅度 |
| 通用 SCPI | 保留原始命令查询，不对未知型号自动发送猜测的频率/幅度命令 |

保持 DG/Vaunix 首触发 P0、有序累计计数、未知接受结果不自动重放、停止失败保留占用和 DG CH2 隔离；SSG 继续使用仪器自身原生扫描及既有停止恢复规则。

## 验证与本地交付

完整源码回归 **495 项通过（69.579 秒）**。新增 30 项专用回读回归：SSG 14 项、Vaunix 16 项。源码虚拟自检通过，实际点击两种窗口的回读按钮、注入异常回复并重试；新增 `siglent_output_readback` / `vaunix_output_readback` 组的六项检查均为 true，且未访问实体硬件。默认 1180×760 窗口截图确认两个回读区的三张状态卡片可读。

自检中复现并修复 Vaunix 外部重连后立即回读的界面代次竞态：新请求开始时同步界面所观察的连接代次，避免后续定时状态刷新误丢弃该请求；真正旧连接的迟到结果仍被拒绝。新增最小回归先失败后通过，诊断保存在 `build/vaunix-readback-reconnect-repro-0718.log`。

本版完整 unittest 日志与源码虚拟自检分别保存在 `dist/verification/0.7.18/unittest.log`、`source-self-test.json`。目录包、单文件 EXE、ZIP 解压后的冻结运行检查逐项以本版生成的 `release-manifest.json` 为准，不以旧版结果代替本版验收。

构建文件为 `Lab-Control-Center-0.7.18-windows-x64-portable.zip`、`Lab-Control-Center-0.7.18-windows-x64.exe` 和同组源码快照、`release-manifest.json`、`SHA256SUMS.txt`。内置 Python、Qt 与 Vaunix DLL 的内容、实际运行与源提交一致性以本版发行清单和校验结果为准。

本轮不操作实体仪器，未测量实机查询延迟、触发吞吐或输出稳定时间。完整操作见[使用说明](../docs/USER_MANUAL_CN.md#13-各类设备的回读范围)，既有 DG4000 回读边界见[0.7.17 说明](NOTES-0.7.17.md)。
