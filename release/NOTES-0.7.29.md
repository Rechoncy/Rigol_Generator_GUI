# 0.7.29：DG 回读单位跟随与 VISA 回复同步

日期：2026-10-29。状态：源码与本地修复候选，正式下载仍为 [v0.7.28](https://github.com/Rechoncy/Rigol_Generator_GUI/releases/tag/v0.7.28)。本轮不创建版本标签或 GitHub Release；候选使用 `dist/local/v0.7.29/`，保留 0.7.28 正式包、候选、历史版本和用户数据。候选产物是否生成、源码快照、校验和及打包验收采用本组实际清单，不将源码修改直接视为已交付 EXE。

## 两个已复现的软件问题

用户 DG4162 + USB 截图中，上方幅度选择 mVpp，底部回读仍显示 Vpp；另有 `:SOUR1:VOLT:HIGH?` 数值查询收到 `VPP`。前者是显示选择未联动；后者可由 FIFO 回复替身精准复现：PyVISA 单次 `query()` 发送一次查询并执行一次读取，空结束帧可使当前读取先回空，随后查询误取前一条单位回复。此复现证明软件有错位风险，不能据此确认用户实机空帧的硬件/驱动/固件根因，尚未在该 DG4162 USB 实机验收。

## 显示和恢复操作

- 各通道底部 Vpp/mVpp 在初始化和上方幅度单位改变时跟随。底部仍可单独覆盖，直到上方该通道下次切单位；仅重画有效缓存，不访问仪器、不改上方编辑值、不新增 DATA 持久化，CH1/CH2 独立。
- 只对有效 `VPP` 快照缩放毫前缀，包括原有高低电平差；原生负 dBm 仍为 dBm，Vrms 仍为 Vrms，不假定 50 Ω 或正弦波来转换。电平差来源仍标“仪器当前幅度单位未知”。
- 若显示“回复未同步/回复关联未确认”，或数值查询收到 VPP/VRMS/DBM，整组快照失效、自动回读暂停。后续查询不会继续发送；先停止 NI/联动任务并等待清理，再显式断开、重新连接，最后手动回读。仅重新勾选自动，或关闭再打开共享同一会话的窗口，不能清除未同步状态。
- 普通写入能力保留，已运行任务不自动重放或重连，停止失败仍保留设备占用。读回的是基本波配置快照，不能代替扫频/调制的瞬时物理输出测量；如重连后仍失败，可提供完整错误和已知型号/固件信息。

## 传输处理边界

`VisaBackend` 只对明确必须非空的身份、完成、DG 基础数值/单位/状态及预检查询续读空帧，在同一查询内继续 `read()`，不会再发 query、设置单位、清缓冲或加入猜测的固定延迟。续读受总查询 timeout 预算和最多 16 个空帧约束：从首次 query 起算，后续 read 只使用剩余预算，结束后恢复原 timeout。Python 不能抢断底层已经开始的 I/O 或 PyVISA `query_delay`，这不是绝对墙钟耗时保证。未绑定语义的通用 SCPI 查询仍允许合法空回复；不会跳过非空坏回复以寻找看似正确的数值。

空帧续读失败或回复关联不能确认时标记会话未同步，专用 `ScpiResponseSyncError` 不走自动查询恢复，后续 query 在发送前拒绝。DG 的 HIGH/LOW/FREQ/VOLT 等数字字段收到幅度单位词时也按疑似错位作废整组。原普通 I/O、幂等 write 恢复范围不作无关重构；首触发 P0、有序累计计数、CH2 隔离和停止失败保留占用保持。

原厂 CHM 的命令语义与 PyVISA 官方[资源通信说明](https://pyvisa.readthedocs.io/en/latest/introduction/resources.html)、[MessageBasedResource 源码](https://pyvisa.readthedocs.io/en/latest/_modules/pyvisa/resources/messagebased.html)是本次依据。合法 `UNIT?` 短写未改为“不支持”；原空单位高低电平差兼容保留，但不把未消费完的传输空帧当作仪器不支持幅度单位。

## 已完成验证与交付边界

实际日志使用 `dist/verification/0.7.29/`：

- 新增 6 项单位跟随 UI 回归先复现失败再通过；已有 UI/自动回读等组合 **35 项，24.875 秒，通过**。
- 新增 18 项传输空帧回归，连同相邻恢复和驱动测试共 **111 项，5.795 秒，通过**，见 `visa-framing-adjacent-final.log`。
- DG 回读驱动 **27 项，0.085 秒，通过**；新增错误类型回归覆盖 HIGH/LOW/FREQ/VOLT 各自收到 VPP/VRMS/DBM 的 12 个子场景，确认标记未同步并立即停止整组读取。
- 最终 `full-unittest.log`：**809 项，107.475 秒，OK**，其中 1 项因本机 Windows 符号链接权限跳过。
- `source-self-test.json` 成功，新增 `dg_readback_reply_alignment` 八项标志均为 true、`hardware_access=false`；原自动回读八项、显示单位及电平差标志继续通过。

初次源码自检的 mVpp 场景误用了前段留下的原生 VRMS 配置，保留 `source-self-test-initial-vrms-fixture.json`。仅把该虚拟测试场景明确设为原生 VPP 后重跑成功；没有为通过测试而让应用把 Vrms 错换成 Vpp/mVpp。

远程 CI 与安装/便携/单 EXE 冻结验收另以本版实际记录为准，当前不预写通过，不借用 0.7.28 结果。本版未自动操作真实仪器，不声称已确认用户设备的 USB 根因或实际触发性能。
