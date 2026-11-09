# 验证指南

## 环境和配置

在项目根目录使用 Windows PowerShell 运行下列命令。报告里的 `PYTHON` 和 `AUDIO_TOOLS` 会显示选中的工具和版本。`SIX_FORMATS` 检查实际编码和解码。`READY_FOR_STARTUP_TEST` 表示可进入启动自检。

```powershell
powershell.exe -NoProfile -File .\gateway.ps1 check-config --config .\config\gateway.jsonc
```

`connect` 模式需要先让 GSV 在 `127.0.0.1:9880` 就绪。`auto` 模式下，GSV 未运行时 `BACKEND_ENDPOINT` 可以显示 `unreachable`，后续由网关在满足启动条件时创建受管后端。

## 启动和转发

1. 使用 `run` 或 `start` 启动。确认网关在完成 FLAC 合成和解码自检后显示 `ready`，再用 `status --json` 确认就绪。失败时查看命令错误和 `logging.directory` 下的 `startup.log`。
2. 在部署地址以正确 Token 请求 `/v1/models` 和 `/v1/audio/speech`，确认模型 ID、目标音频格式和完整播放。使用两条启用凭据分别请求 `/v1/models`，再将其中一条设为 `enabled: false`。通过 `check-config` 后重启，确认停用凭据返回 401，另一条凭据仍可使用。分别检查各注册声音的音色和参考音频匹配情况。
3. 通过转发入口重复请求，验证 `Authorization` 透传、错误 Token 返回 401，以及长音频可以持续接收。若 401 由网关返回，用 `X-Request-ID` 查私有日志。
4. 若部署使用后台模式，验证 `start`、`restart`、`stop` 和关闭终端后的运行状态。若由网关管理 GSV，在独立测试环境验证受管后端异常退出后的恢复和进程清理。使用不同 Windows 会话或账户的部署也应在目标账户下验收。

## 开发回归

准备开发依赖和兼容音频工具后，从项目根目录运行测试。以下以 PATH Python 创建的虚拟环境为例。使用受管 Python 时，将 `venv-path` 换成 `venv`。原生命名管道测试需要允许本机管道访问的 Windows 环境。

```powershell
.\runtime\venv-path\Scripts\python.exe -m pytest -q
.\runtime\venv-path\Scripts\python.exe -m ruff check gsv_gateway tests
```

自动测试覆盖配置、鉴权、队列、流式编码、启动门禁和本机控制。面向实际 GSV、系统环境和转发链路的验收应在目标部署中执行。测试记录和生成音频应保存在本地。
