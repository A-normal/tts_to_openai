# 运行环境与工具优先级

| 组件 | 选择规则 |
| --- | --- |
| 网关 Python | 优先选 PATH 中通过检查的 CPython 3.11 或 3.12 x64。全局环境已有锁定依赖时可直接使用，也可使用同一 PATH 解释器创建的 `runtime/venv-path`。两者均不可用时，使用受管的 `runtime/venv`。 |
| FFmpeg 和 FFprobe | 按 `audio.ffmpeg_executable` / `audio.ffprobe_executable` 显式配置 → 当前进程 PATH → `runtime/installed.json` 中的受管工具记录，成对选择。需要同目录、同版本，并通过六格式检查。 |
| GSV Python | 已有可用 GSV 时复用其服务。`auto` 模式需要创建后端时，使用 `backend.auto.python_executable` 与 `backend.auto.working_directory`，通常指向 GSV 安装自带的环境。`connect` 模式只连接已有服务。 |
| uv | `scripts/setup.ps1` 使用项目 `runtime/tools/uv.exe` 安装受管 Python 和锁定依赖。普通启动不调用 uv。 |

FFmpeg 和 FFprobe 的显式相对路径以主配置所在目录为基准。只指定其中一个时，从同一目录找另一个。显式路径无效会直接报错。自动选择会跳过 PATH 上缺少配套工具、版本不一致或能力不足的安装。

PATH 上的 Python 版本兼容但缺少锁定依赖时，`setup` 可以用它创建 `runtime/venv-path`，依赖安装在虚拟环境中。PATH 上没有兼容解释器时，`setup` 使用 `runtime/python` 下的受管解释器准备 `runtime/venv`。普通启动只选择可用环境，缺项时提示显式执行 `setup`。`-Online` 允许获取缺失的归档和依赖。

运行中的网关会共享持有其工具目录下的 `.runtime.lock`。`setup` 修复前独占该文件，仍应先停止使用同一工具目录的网关。锁随进程关闭释放，文件可保留。

PowerShell 入口会为网关临时隔离 `PYTHONHOME`、`PYTHONPATH` 和用户级 site-packages。手工运行 `python -m gsv_gateway` 时，解释器由当前 shell 解析。

在项目根目录运行以下命令，查看 `PYTHON.executable`、`PYTHON.base_executable`、`AUDIO_TOOLS.paths` 和 `AUDIO_TOOLS.versions`，以确认虚拟环境、基础解释器和音频工具。

```powershell
powershell.exe -NoProfile -File .\gateway.ps1 check-config --config .\config\gateway.jsonc
```

下载版本和归档信息见 [外部工具来源](third-party-tools.md)。
