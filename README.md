# tts_to_openai

Windows 本地 GPT-SoVITS 网关，提供 OpenAI Speech 核心协议。固定 `model="gpt-sovits"`，用 `voice` 选择声音；支持 MP3、Ogg Opus、ADTS AAC、FLAC、流式 WAV、24 kHz s16le PCM，默认 MP3。

网关支持 CPython 3.11 和 3.12 x64，不加载 Torch。GSV、GPU 驱动和模型由使用者准备，GSV API 固定 `http://127.0.0.1:9880`。V4 是分段生成，单句内部不保证细粒度输出。

## 准备环境

在项目目录使用 Windows PowerShell 5.1 或更高版本：

```powershell
.\scripts\setup.ps1 -Online -InitConfig
.\scripts\doctor.ps1 -Config .\config\gateway.jsonc -Json
```

`setup` 优先复用兼容的 PATH 工具。`-Online` 允许下载缺失的工具和锁定依赖，放在项目 `runtime/`。普通启动不安装、不下载。开发依赖使用 `-Dev`。离线安装使用现有工具和本地缓存。工具来源与 SHA-256 见 `runtime-manifest.json`，Python 依赖见 `uv.lock`。

PowerShell 入口优先选择兼容的 PATH Python 或由它创建的项目虚拟环境。FFmpeg 和 FFprobe 按「显式配置 → 当前进程 PATH → 项目 runtime」成对选择。完整规则和确认命令见 [运行环境与工具优先级](docs/runtime.md)。

`-InitConfig` 只补齐缺失的 `config/gateway.jsonc`、`config/voices.jsonc` 和 `config/secrets/api-tokens.jsonc`，并为新建的凭据文件生成一个随机 Token。请调整声音资产路径。安装成功后仍须通过 doctor 和真实启动自检。

项目移动后重新准备环境；修复共享 runtime 前停止全部网关实例，setup 不代为终止进程。失败/中断的安装保留不完整标记，普通启动拒绝使用，显式重跑 setup 修复。

执行策略受限时遵从组织要求；非组策略限制可使用进程级命令，不修改系统策略：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\setup.ps1 -Online -InitConfig
```

## 配置

- [网关配置示例](config/gateway.example.jsonc)：逐项中文注释、用途、单位、默认值及边界。
- [声音配置示例](config/voices.example.jsonc)：四角色 ID/别名、语言、配套权重及参考参数。
- [网关 schema](config/gateway.schema.json)、[声音 schema](config/voices.schema.json)。

JSONC 支持 `//`、`/* */` 注释，不支持尾随逗号、重复键或 NaN/Infinity。程序只读用户配置，不回写或删除注释。网关相对路径以其配置目录解析，声音资产相对于 `assets_root`。修改后重启生效；显式 CLI 覆盖优先。

doctor 只需传入主配置，按 `voices_file` 自动读取声音表；无需逐个传全部配置文件。`CONFIG` 错误的 `details` 会列出出错文件、字段或 JSONC 行列，不打印配置值。Token 文件另列 `TOKEN` 检查，不要将 Token 内容贴到诊断消息中。

默认 API `127.0.0.1:16681`；FIFO 等待 8 位，排队 120 秒、启动 300 秒、任务 600 秒、首 PCM 180 秒、首编码音频 30 秒。HTTP 并发 32、正文最大 64 KiB、读取期限 15 秒。期限为上限，有音频立即发送。

`connect` 只连接现有 GSV；`auto` 仅在明确连接拒绝且无监听时创建自有后端，超时/初始化中/不兼容占用不会重复启动。后端地址和端口不可覆盖；网关 API host/port 可配置。

后端必须独占：不要同时通过 WebUI/其他客户端推理或切权重。外部实例的 YAML 写回位置由其启动方式决定；声音切换可能触发 GSV 原配置写回，网关不会终止该外部进程。

## 运行和控制

```powershell
.\gateway.ps1 check-config --config .\config\gateway.jsonc
.\gateway.ps1 voices --config .\config\gateway.jsonc --json
.\gateway.ps1 run --config .\config\gateway.jsonc
```

`run` 在当前终端持续运行，Ctrl+C 走统一关闭路径。后台命令由使用者明确执行：

```powershell
.\gateway.ps1 start --config .\config\gateway.jsonc
.\gateway.ps1 status --config .\config\gateway.jsonc --json
.\gateway.ps1 restart --config .\config\gateway.jsonc --port 16682
.\gateway.ps1 restart --config .\config\gateway.jsonc --reset-overrides
.\gateway.ps1 stop --config .\config\gateway.jsonc
```

部署时可按 [验证指南](docs/verification.md) 检查本机运行和转发链路。

所有实例命令必须显式 `--config`。restart 读取最新文件，保留上次 CLI 覆盖，新参数替换对应项；`--reset-overrides` 清空覆盖。控制状态的私有索引在配置目录 `.gsv-gateway/`，即使配置被错误编辑，status/stop 仍可通过已验证身份控制原实例；运行时不要删除索引。

退出码：0 成功、1 运行失败、2 配置错误；status 另有 3 运行但未 ready、4 未运行。高级入口为 `python -m gsv_gateway`，可显式 `--runtime-root`。

`start` 和 `restart` 在终端显示当前启动阶段和已等待时间，例如等待受管 GSV 或执行 FLAC 自检。`stop` 和 `restart` 会提示正在等待旧进程退出。进度写入 stderr，`ready`、`already_running`、`stopped` 仍写入 stdout。

启动身份校验失败、配置在启动期间变更、初始化退出或超时分别报告 `startup_identity_conflict`、`startup_config_changed`、`startup_exited`、`startup_timeout`。`check-config` 通过表示可执行启动测试，仍需真实启动自检成功；`auto` 模式下后端尚未运行可出现 `BACKEND_ENDPOINT` 警告。

## API 凭据

所有 HTTP 路由均需 Bearer Token，包括健康检查和错误路径。`auth.token_file` 默认指向私有的 `secrets/api-tokens.jsonc`。每个客户端发送自己的 `Authorization: Bearer <token>`。不同凭据共享网关权限和队列，分别启停和轮换。

```jsonc
{
  "tokens": [
    // 桌面客户端
    { "id": "desktop", "token": "<替换为随机 Token A>", "enabled": true },
    // 测试客户端已停用
    { "id": "test-client", "token": "<替换为随机 Token B>", "enabled": false }
  ]
}
```

最多配置 128 项，至少一项启用。`id` 和 `token` 各自唯一，包括停用项。`id` 以英文字母或数字开头，最多 64 个字符，其余字符可用字母、数字、点、下划线或连字符。每个 Token 独立生成，使用至少 32 个安全随机字节，例如 `python -c "import secrets; print(secrets.token_urlsafe(32))"`。`enabled` 必须明确填写布尔值。保存后先运行 `check-config`，通过后重启生效。请保持文件 ACL 为私有，避免将凭据提交 Git 或放入 URL。

**从单行 TXT 升级**。在现有私有 `secrets` 目录创建 `api-tokens.jsonc`，将原 Token 值填入一条启用记录，然后把主配置的 `auth.token_file` 改为 `secrets/api-tokens.jsonc`。如果上次通过 CLI 覆盖了 Token 路径，重启时显式更新 `--token-file`。先用 `check-config` 验证，再用客户端原凭据验收。确认迁移完成后即可清理旧 TXT。

## API

排查转发 401 时，可对比本机直连和转发后的 `/v1/models`。网关拒绝时返回 `invalid_api_key` 和 `X-Request-ID`。用请求 ID 在私有 `gateway.log` 中查找 `auth_rejected`，查看缺少请求头、重复请求头、格式错误或 Token 不匹配等原因。相同原因每五秒最多记录一次，日志不保存请求头的值。

| 路径 | 用途 |
|---|---|
| `GET /v1/models` | 仅列 `gpt-sovits` |
| `GET /v1/models/gpt-sovits` | 固定引擎元数据 |
| `POST /v1/audio/speech` | 连续二进制音频流 |
| `GET /voices` | ID、显示名、别名、语言，不泄漏资产路径/参考文本 |
| `GET /health/live` | 网关存活 |
| `GET /health/ready` | 自检后能否安全受理 |

```json
{
  "model": "gpt-sovits",
  "voice": "aimisi",
  "input": "你好，这是第一段。接下来检查连续音频返回。",
  "response_format": "mp3",
  "speed": 1.0,
  "stream_format": "audio"
}
```

voice 可用 ID、显式别名或 `{"id":"aimisi"}`。input 非空、最多 4096 Unicode 码点；speed 为 0.25–4.0。SSE、非空 instructions、stream=true、云端声音 ID 和未知字段明确拒绝；null 不等于省略。

官方 SDK 示例（在客户端环境安装 `openai`，或 setup 使用 `-Dev`）：

```python
import os

from openai import OpenAI

token = os.environ["GSV_GATEWAY_TOKEN"]  # 在客户端环境中设置分配给它的 Token
client = OpenAI(base_url="http://127.0.0.1:16681/v1", api_key=token, max_retries=0, timeout=750)
with client.audio.speech.with_streaming_response.create(
    model="gpt-sovits", voice="aimisi", input="你好，这是语音测试。", response_format="mp3"
) as response:
    response.stream_to_file("speech.mp3")
```

## 可靠性与边界

一个消费者持有模型准备、参考缓存、合成、编码和发送的完整所有权。客户端断开后有界排空，不能因为 handler 结束就放行下一任务。结果不明不重放，只恢复自己创建的后端；外部实例不确定时需人工恢复。

每次启动/自有后端恢复必须通过 FLAC 真实编码解码、非静音和样本数校验；成功快照和 startup.log 落盘后才 ready。doctor/check-config 不合成、不切权重。

成功快照与 GSV 可写工作 YAML 分离，只有完整成功才提交。失败、取消或仅排空不提交；磁盘提交失败会阻止后续任务，已经正常发送的音频不会事后追改。日志不保留 Token、input、参考文本或原始 GSV 输出。

响应开始后无法再改 HTTP 状态，失败中断流，不追加 JSON 或伪造正常 EOF。WAV 未知长度头有播放器兼容边界；FLAC 总采样数未知合法。保存成功不等于已证明增量播放或听感。

公网 TLS、来源限制及反代由使用者管理，参考 [Nginx 示例](docs/nginx.conf.example)。不要公开 GSV 的 9880 端口或控制通道。不交付 SCM/NSSM/WinSW 安装、WebUI、其他具体 TTS 适配器、TTS/CUDA/模型自动安装或实际公网发布。

## 开发检查

开发本地 TTS 后端可参考 [后端接入指南](docs/backend-adapter.md)，其中包括配置注册、音频会话、故障语义和测试方式。

以下以 PATH Python 创建的虚拟环境为例。使用受管 Python 时，将 `venv-path` 换成 `venv`。

```powershell
.\scripts\setup.ps1 -Online -Dev -InitConfig
.\runtime\venv-path\Scripts\python.exe -m pytest -q
.\runtime\venv-path\Scripts\python.exe -m ruff check gsv_gateway tests
```

默认测试使用受控后端和有限前台 FFmpeg，不调用真实 GSV。原生命名管道在受限文件沙箱中可能不可访问，应在允许本机管道的环境验证，不因此放宽 ACL。真实集成脚本需使用者明确执行，会切换现有 GSV 的声音并触发其配置写回。

项目 `runtime`、实际配置、Token、运行状态和生成音频由部署者在本地管理。外部工具来源见 [说明](docs/third-party-tools.md)。
