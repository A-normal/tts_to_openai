# 外部工具来源

setup 仅在显式 `-Online` 时下载 `runtime-manifest.json` 中缺失的固定归档，并核对 SHA-256。下载的工具位于项目 runtime。

- uv 0.9.8：[Astral 官方发布](https://github.com/astral-sh/uv/releases/tag/0.9.8)，MIT/Apache-2.0。
- CPython 3.12.12：[python-build-standalone](https://github.com/astral-sh/python-build-standalone)，构建 20251028。来源、哈希固定在 manifest 和 uv 0.9.8 下载元数据中；不是 python.org embeddable 包。
- FFmpeg 7.1.1 essentials：[Gyan 构建发布](https://github.com/GyanD/codexffmpeg/releases/tag/7.1.1)，GPLv3。完整归档及其中的许可/说明保留在 runtime，源码信息按发行方说明获取；本仓库不包含 FFmpeg 二进制。

依赖版本、来源及包哈希见 uv.lock。模型和参考音频由使用者自行准备，不由 setup 下载或分发。
