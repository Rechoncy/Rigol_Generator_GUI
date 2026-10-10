# Lab Control Center 0.7.11 发行说明

日期：2026-10-10；平台：Windows 10/11 x64。

## 内置 Vaunix DLL

本地便携 ZIP 和单文件 EXE 直接包含项目主目录提供的 `vnx_fmsynth.dll`，SDK 版本为 1.15（Windows x64），无需再安装 SDK、复制外置 DLL 或设置环境变量。

- 目录 ZIP 完整解压后，DLL 位于 `_internal/vendor/vaunix/vnx_fmsynth.dll`。
- 单文件 EXE 已包含 DLL，启动时随 Python/Qt 运行时自动释放到临时目录。
- 迁移目录版时保留完整 `_internal/`，迁移单文件版时携带 EXE；原配置仍需复制同级 `data/`。
- Windows 硬件驱动要求沿用设备规定；本版不代替实体设备连接验收。
- 源码构建自动检测主目录 DLL；`-VaunixDll` / `--vaunix-dll` 仍可指定其他来源。缺失显式路径、非 x64 DLL 和损坏文件会在打包前报错。源码 ZIP 不包含厂商二进制，重建时需提供 DLL。

清单记录 `vaunix_dll_bundled=true`、DLL 大小和 SHA-256。原文件为 130048 字节，校验和为 `53545d870d13a3504e975f1070a5ebd4bf9371112ad5d5ba0c4aa47bc63ee3ba`。

保留 v0.7.10 的输入框滚轮保护、独立 STEP/LIST 页面及鼎阳通信恢复。源码、便携 ZIP 和单文件 EXE 同步更新。

本次 GitHub 更新同时包含自 v0.7.0 之后的鼎阳 SSG3021X 通信与停止恢复、dBm/mV/mVpp 幅度单位、当前值非阻塞读取、独立扫描页面及输入框滚轮保护，历史变更见 `release/NOTES-0.7.1.md` 至 `release/NOTES-0.7.10.md`。

修正 Windows 快捷方式回归在 GitHub Runner 的长路径/8.3 短路径别名下误报失败的问题，按真实文件身份核对迁移结果。自动发布发现同版本 Release 已有全部附件时保留已上传构建，避免重复创建或替换已验证的成品。

## 验证

新增 4 项构建/SDK 加载回归，全量 **273 项测试通过**。目录包、单文件 EXE 和 ZIP 二次解压自检验证包内 DLL 的路径、校验和、全部驱动接口绑定以及 SDK 1.15 版本读取；无外部 Python 搜索路径下的自检和 Windows 普通界面启动检查通过。自检不枚举、连接或操作实体仪器。

退出旧版后完整解压新 ZIP，复制原 `data/` 或导入配置，核对窗口标题为 v0.7.11。
