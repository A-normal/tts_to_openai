# 本地 TTS 后端接入指南

网关实例通过 `backend.type` 选择一个适配器。适配器调用本地 TTS 的原生接口，网关统一提供 OpenAI Speech 请求校验、Bearer 鉴权、FIFO、超时控制、六种输出格式和启动自检。现有实现为 `gpt_sovits`。开发其他后端时可以先实现 `connect`，由部署者负责启动上游服务。

## 1. 定义配置并注册

在 `gsv_gateway/backends/` 中添加适配器模块。以下例子用 `example_tts` 表示接入者自己的后端。

```python
from typing import Literal

from pydantic import Field, StrictInt

from ..config import Backend, GatewayConfig, Model


class ExampleBackend(Backend):
    type: Literal["example_tts"]
    port: StrictInt = Field(ge=1, le=65535)


class ExampleConfig(GatewayConfig):
    backend: ExampleBackend


class ExampleVoiceOptions(Model):
    speaker: StrictInt = Field(ge=0)
```

`Backend` 默认使用 `connect`。上例的适配器可以用固定回环地址与 `config.value.backend.port` 构造 HTTP 客户端。如果服务和网关共用本机地址，配置模型也应检查端口冲突。模型校验器可用 `ConfigConstraint(field, reason)` 向 `doctor` 提供字段和固定说明，说明中避免带入输入值。选择互斥键时以实际独占资源为准，同一后端端点被不同配置引用时应得到同一键。

在 [适配器工厂](../gsv_gateway/backends/__init__.py) 中导入实现并加入 `BACKENDS`。

```python
"example_tts": BackendRegistration(ExampleConfig, VoiceRegistry, ExampleAdapter, "buffered"),
```

`VoiceRegistry` 可直接复用，也可继承它校验适配器特有的声音表字段。注册通过源码显式完成，`backend.type` 只选择已有条目。配置的类型标识和 `backend_type` 必须一致。仓库中的 `config/*.schema.json` 针对随仓库交付的 GSV 配置。新增后端可从自己的模型导出 JSON Schema，供编辑器使用。

主配置的最小结构如下，其他网关字段按实际部署填写。

```jsonc
{
  "backend": { "type": "example_tts", "mode": "connect", "port": 9000 },
  "voices_file": "example-voices.jsonc",
  "startup_test": { "voice": "narrator" }
}
```

对应的声音表如下。`engine.id` 是客户端传入的 `model`。`voices[].id` 或 `aliases` 用于选择声音。`backend_options` 只交给适配器，`/voices` 和 CLI 的 `voices` 输出会排除该字段。

```jsonc
{
  "backend_type": "example_tts",
  "engine": { "id": "example-tts" },
  "voices": [
    {
      "id": "narrator",
      "name": "旁白",
      "declared_languages": ["zh"],
      "backend_options": { "speaker": 0 }
    }
  ]
}
```

## 2. 实现适配器契约

适配器接收已校验的 `LoadedConfig`。构造阶段只初始化本地对象，实际 I/O 放入对应方法。完整签名见 [BackendAdapter](../gsv_gateway/backends/base.py)。

| 接口 | 接入要求 |
| --- | --- |
| `reservation_key` | 全局互斥键，例如 `backend-example-127.0.0.1-9000`。同一独占资源使用同一键。 |
| `endpoint_label` | 用于诊断的固定描述，避免包含令牌、请求数据和带密钥的 URL。 |
| `describe()` | 返回 OpenAI 模型元数据，例如 `config.registry.engine.model_dump() \| {"object": "model"}`。 |
| `validate()` | 校验每个声音的 `backend_options` 和适配器依赖。`doctor` 和启动都会调用。需要本地文件时解析路径并验证，避免在这里合成或切换模型。 |
| `probe()` | 用短时、低副作用的请求区分 `ready`、`incompatible`、`uncertain`、`unreachable`。`unreachable` 用于明确无监听的情形。 |
| `prepare(voice, operation)` | 完成必要的模型、speaker 或参考资源准备。请求直接传 speaker 的后端可以只更新 `state` 和 `loaded_voice`。需要检测声音表变更时调用 `config.assert_registry_unchanged()`，以 `config_changed` 报告。 |
| `synthesize(voice, text, speed, operation)` | 映射文本、speaker 和语速到上游接口，把输出转换为 `AudioSession`。 |
| `snapshot(voice)` | 返回可用安全 YAML 表示的字典，含 `voice`、`backend_type` 和 `registry_sha256`。可以加版本及稳定的配置或资产指纹。避免把 Token、请求文本和音频写入快照。 |
| `close()` | 释放 HTTP 客户端等适配器资源。外部服务的进程生命周期由部署者管理。 |

例如上游要求 `POST /speak`、JSON 请求和 WAV 响应，可在 `validate()` 中用 `ExampleVoiceOptions` 校验并缓存 speaker。在 `synthesize()` 中构造类似 `{"text": text, "speaker_id": speaker, "rate": speed}` 的请求，按上游实际语义转换语速。回环 HTTPX 客户端使用 `trust_env=False`，连接目标来自受信配置。把网络错误映射为消息固定的 `GatewayError`，避免转发上游正文或异常原文。

### 音频与失败边界

- `AudioSession` 的采样率来自校验后的响应或已确认的上游协议。`pcm` 异步迭代器输出单声道、16 位有符号、小端 PCM，每块按 2 字节样本对齐。WAV 头和压缩音频字节需要先解析或解码。对于符合约束的 PCM WAV，可以复用 [WAVParser](../gsv_gateway/wav.py)。
- 输出 PCM 时累计 `samples`，有非零样本时更新 `non_silent`。收到第一个有效 PCM 时记录 `first_pcm_monotonic`，只有确认合法且完整的上游结束后才设 `complete=True`。流迭代器在 `finally` 中释放响应。截断或无效音频应失败。
- 使用 `Operation.wait()` 约束首音频、块间空闲和准备阶段，受任务总期限与断开排空上限约束。客户断开后，串行队列仍持有该任务，直到排空或确认上游状态。无法确认是否已停止或状态是否干净时，报告 `uncertain=True`。外部后端进入人工恢复状态，避免重复发起不明请求。
- `BackendRegistration.streaming` 描述**上游合成**能力。完整音频生成后才可读用 `buffered`，按段产出用 `segmented`，生成中持续给出音频用 `incremental`。完整 WAV 也能由网关分块发送，首音频仍受上游合成时间约束。

快照由网关在完整成功后原子提交。默认位置为 `runtime_dir/backend/<backend.type>-last-good.yaml`，也可以显式设定 `backend.last_good_config`。为不同网关实例分配各自的 `runtime_dir` 和日志目录。已有 GSV 配置继续使用原快照路径和格式。

## 3. 验收

1. 先用替身或本地测试夹具覆盖真实请求字段、音频解析、首音频超时、截断、断开和资源释放。仓库的 [配置到 HTTP 契约测试](../tests/test_backend_registration.py) 展示了如何仅在测试中注册替身，从 JSONC、工厂和 `doctor` 走到启动自检、鉴权、模型发现、合成和快照。
2. 已有真实上游后，用 `gateway.ps1 check-config --config <file>` 检查声音、凭据、音频工具和上游探测，再用 `run` 在前台启动。`ready` 表示实际 FLAC 合成与解码自检通过。
3. 使用分配的 Bearer Token 请求 `/v1/models`、`/voices` 和 `/v1/audio/speech`。验证角色映射、语速、返回格式、首音频时序和完成状态。按上游协议核对错误和取消行为。

仅当接入确实需要受管进程时，再扩展配置接受 `auto`，并实现 `ManagedBackendAdapter`。`validate_launch()` 供 doctor 做只读检查。`build_launch_spec()` 可准备私有工作文件并返回参数数组，由网关执行进程归属、终止和恢复。通过 `backend_files()` 声明可写路径和只读来源，网关检查运行目录约束和路径冲突。参数与路径来自本地配置，服务启动和停止始终按受管进程归属处理。
