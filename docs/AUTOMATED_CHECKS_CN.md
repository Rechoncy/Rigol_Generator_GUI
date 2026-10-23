# 自动检查与持续维护

普通提交使用 [Windows checks](https://github.com/Rechoncy/Rigol_Generator_GUI/actions/workflows/ci.yml)，发布构建使用原有 `Windows portable release`，两者分别记录结果。

## GitHub 自动检查

- `main` 分支推送、PR 和手动触发会运行 Windows x64 / 官方 Python 3.13 检查。
- 安装 `release/requirements-build.txt` 的锁定依赖和本项目，不在普通提交中运行完整 PyInstaller 打包。
- 运行完整 `unittest` 回归及 `tools/frozen_entry.py --bundle-self-test`。自检只使用虚拟设备，覆盖界面、DG/Vaunix 触发以及 SSG 断线停止恢复，不连接实体仪器。
- 校验仓库内的源码启动器和 Vaunix DLL 存在，避免缺失测试输入被静默跳过。
- 回归失败后仍运行源码自检，并上传安装日志、测试日志、自检 JSON 及运行诊断，保留 14 天。新提交会取消同一分支或 PR 的旧检查。

进入仓库 Actions 页面可查看每个步骤的结论；失败运行底部可下载诊断附件。检查通过代表源码与虚拟路径通过，不代表实体仪器时序、幅度或运动已经验收。

## 本地复现

在官方 64 位 Python 3.13 创建的开发环境中安装依赖，再运行：

```powershell
python -m pip install -r release/requirements-build.txt
python -m pip install --no-deps --no-build-isolation -e ".[instrument]"
$env:QT_QPA_PLATFORM = "offscreen"
python -X faulthandler -m unittest discover -s tests -v
python -X faulthandler tools/frozen_entry.py --bundle-self-test dist/verification/source-self-test.json
```

本项目已有的隔离构建解释器位于 `build/release-venv/Scripts/python.exe`，可将命令中的 `python` 换成该路径。0.7.15 本地验收记录位于 `dist/verification/0.7.15/`；后续版本应使用各自的记录目录。

## 持续维护规则

定期维护先检查 GitHub CI、新提交与新增问题，再复查尚未覆盖的代码路径。只修复能复现的缺陷或有测量依据的瓶颈，不为产生提交而调整界面或重构。

修复须保留用户当前变更，先运行对应回归，再按影响范围执行完整测试和源码自检。改动应用程序时同步说明、版本及本地发行包；不复用已经交付版本的构建身份。通过验证后正常提交和推送，不强推、不自动连接真实仪器，也不把原始命令执行成功当作物理输出已稳定。

GitHub CI 随推送即时触发；本地定期维护依赖本机 Codex 任务，检查频率和启停在应用的定时任务中管理。
