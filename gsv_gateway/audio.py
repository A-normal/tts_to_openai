import asyncio
import json
import math
import struct
import subprocess
import time
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace

from .config import Audio, asset_stamp, fingerprint
from .errors import GatewayError
from .wav import WAVParser
from .windows import LaunchSpec, ManagedProcess


MIME = {
    "mp3": "audio/mpeg",
    "opus": "audio/ogg",
    "aac": "audio/aac",
    "flac": "audio/flac",
    "wav": "audio/wav",
    "pcm": "application/octet-stream",
}


def path_audio_tools():
    # Search PATH explicitly. Python 3.11's Windows which() can include cwd.
    import os

    for entry in os.get_exec_path():
        directory = Path(entry.strip('"'))
        if not directory.is_absolute():
            continue
        ffmpeg, ffprobe = (directory / f"{name}.exe" for name in ("ffmpeg", "ffprobe"))
        if ffmpeg.is_file() and ffprobe.is_file():
            yield ffmpeg.resolve(), ffprobe.resolve()


def _managed_audio_tools(runtime_root):
    installed = runtime_root / "installed.json"
    if not installed.is_file():
        return None
    record = json.loads(installed.read_text(encoding="utf-8-sig"))
    if record.get("project_root") != str(runtime_root.parent):
        raise ValueError("Runtime moved; prepare the environment again")
    locations = record.get("tools", {})
    if not all(locations.get(name) for name in ("ffmpeg", "ffprobe")):
        return None
    pair = tuple(Path(locations[name]).resolve() for name in ("ffmpeg", "ffprobe"))
    if any(not tool.is_relative_to(runtime_root) for tool in pair):
        raise ValueError("Installed tool points outside managed runtime")
    return pair


def select_audio_tools(config, *, dependencies=None, refresh=False):
    if dependencies is None:
        return _select_audio_tools(config, check_audio_pair)

    from .storage import write_state

    cache_path = config.state_dir / "codec-check.json"
    try:
        cached = json.loads(cache_path.read_text(encoding="utf-8")) if not refresh else {}
    except (OSError, ValueError):
        cached = {}
    record = {}

    def probe(audio, ffmpeg, ffprobe):
        key = {
            "ffmpeg": fingerprint(ffmpeg),
            "ffprobe": fingerprint(ffprobe),
            "dependencies": dependencies,
            "audio": audio.model_dump(),
        }
        if (
            isinstance(cached, dict)
            and cached.get("key") == key
            and isinstance(cached.get("results"), list)
            and len(cached["results"]) == 6
        ):
            return _audio_versions(ffmpeg, ffprobe), cached["results"]
        versions, results = check_audio_pair(audio, ffmpeg, ffprobe)
        record.update(key=key, results=results)
        return versions, results

    selected = _select_audio_tools(config, probe)
    if record:
        write_state(cache_path, record)
    return selected


def _select_audio_tools(config, probe):
    audio = config.value.audio
    if audio.ffmpeg_executable or audio.ffprobe_executable:
        # An explicit executable selects its installation, including its companion.
        chosen = audio.ffmpeg_executable or audio.ffprobe_executable
        directory = config.resolve(chosen).parent
        pair = tuple(
            config.resolve(value) if value else (directory / f"{name}.exe").resolve()
            for name, value in (("ffmpeg", audio.ffmpeg_executable), ("ffprobe", audio.ffprobe_executable))
        )
        return pair, probe(audio, *pair)
    selected = select_path_audio_tools(audio, probe)
    if selected is not None:
        return selected
    pair = _managed_audio_tools(config.runtime_root)
    if pair is None:
        raise ValueError("No compatible FFmpeg/FFprobe pair; run scripts/setup.ps1 -Online")
    return pair, probe(audio, *pair)


def select_path_audio_tools(audio, probe=None):
    probe = probe or check_audio_pair
    for pair in path_audio_tools():
        try:
            return pair, probe(audio, *pair)
        except (OSError, ValueError, IndexError, KeyError, subprocess.SubprocessError):
            continue


def resolve_tools(config):
    return select_audio_tools(config)[0]


def check_audio_pair(audio, ffmpeg, ffprobe):
    return _check_audio_pair(
        ffmpeg, ffprobe, asset_stamp(ffmpeg), asset_stamp(ffprobe), audio.model_dump_json()
    )


@lru_cache(maxsize=8)
def _check_audio_pair(ffmpeg, ffprobe, ffmpeg_stamp, ffprobe_stamp, audio_json):
    versions = _audio_versions(ffmpeg, ffprobe)
    config = SimpleNamespace(value=SimpleNamespace(audio=Audio.model_validate_json(audio_json)))
    results = codec_check(config, ffmpeg, ffprobe)
    if asset_stamp(ffmpeg) != ffmpeg_stamp or asset_stamp(ffprobe) != ffprobe_stamp:
        raise ValueError("Audio tools changed during compatibility check")
    return versions, results


def _audio_versions(ffmpeg, ffprobe):
    if ffmpeg.parent != ffprobe.parent or not ffmpeg.is_file() or not ffprobe.is_file():
        raise ValueError("FFmpeg and FFprobe must come from the same installation")
    versions = [tool_version(path) for path in (ffmpeg, ffprobe)]
    if len({line.split(" version ")[1].split(" Copyright")[0] for line in versions}) != 1:
        raise ValueError("FFmpeg/FFprobe version mismatch")
    return versions


def tool_version(path):
    result = subprocess.run([str(path), "-version"], capture_output=True, timeout=10, check=True)
    return result.stdout.decode(errors="replace").splitlines()[0]


def codec_check(config, ffmpeg, ffprobe):
    signal = b"".join(
        struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / 48000))) for i in range(48000)
    )
    results = []
    for fmt in ("mp3", "opus", "aac", "flac", "wav", "pcm"):
        encoded = subprocess.run(
            [str(ffmpeg), *encode_arguments(config.value.audio, 48000, fmt)],
            input=signal,
            capture_output=True,
            timeout=15,
            check=True,
        )
        raw = ["-f", "s16le", "-ar", "24000", "-ch_layout", "mono"] if fmt == "pcm" else []
        probed = subprocess.run(
            [
                str(ffprobe),
                "-v",
                "error",
                *raw,
                "-show_entries",
                "stream=codec_name,sample_rate,channels",
                "-of",
                "json",
                "pipe:0",
            ],
            input=encoded.stdout,
            capture_output=True,
            timeout=10,
            check=True,
        )
        info = json.loads(probed.stdout)["streams"][0]
        decoded = subprocess.run(
            [str(ffmpeg), "-v", "error", *raw, "-i", "pipe:0", "-c:a", "pcm_s16le", "-f", "s16le", "pipe:1"],
            input=encoded.stdout,
            capture_output=True,
            timeout=15,
            check=True,
        )
        if not any(decoded.stdout) or len(decoded.stdout) % 2 or info["channels"] != 1:
            raise ValueError("Codec capability probe failed")
        if fmt == "pcm" and (int(info["sample_rate"]) != 24000 or info["codec_name"] != "pcm_s16le"):
            raise ValueError("PCM compatibility probe failed")
        if fmt == "flac" and decoded.stdout != signal:
            raise ValueError("FLAC sample preservation probe failed")
        results.append(
            {"format": fmt, **info, "samples": len(decoded.stdout) // 2, "encode_exit": 0, "decode_exit": 0}
        )
    return results


def encode_arguments(audio, rate, fmt):
    formats = {
        "mp3": ["-c:a", "libmp3lame", "-b:a", f"{audio.mp3_bitrate_kbps}k", "-f", "mp3"],
        "opus": [
            "-c:a",
            "libopus",
            "-b:a",
            f"{audio.opus_bitrate_kbps}k",
            "-ar",
            "48000",
            "-page_duration",
            "20000",
            "-f",
            "opus",
        ],
        "aac": ["-c:a", "aac", "-b:a", f"{audio.aac_bitrate_kbps}k", "-f", "adts"],
        "flac": ["-c:a", "flac", "-f", "flac"],
        "wav": ["-c:a", "pcm_s16le", "-f", "wav"],
        "pcm": ["-c:a", "pcm_s16le", "-ar", "24000", "-f", "s16le"],
    }
    return [
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-probesize",
        "32",
        "-analyzeduration",
        "1",
        "-f",
        "s16le",
        "-ar",
        str(rate),
        "-ac",
        "1",
        "-i",
        "pipe:0",
        *formats[fmt],
        "-flush_packets",
        "1",
        "pipe:1",
    ]


def _crc(data, bits, polynomial):
    value = 0
    mask = (1 << bits) - 1
    for byte in data:
        value ^= byte << (bits - 8)
        for _ in range(8):
            value = ((value << 1) ^ polynomial if value & (1 << (bits - 1)) else value << 1) & mask
    return value


def _flac_header(data, offset):
    if len(data) < offset + 6 or data[offset] != 255 or data[offset + 1] & 254 != 248:
        return None
    if data[offset + 3] & 1:
        return None
    block, rate = data[offset + 2] >> 4, data[offset + 2] & 15
    i = offset + 4
    lead = data[i]
    length = 1
    if lead >= 192:
        length = 0
        bit = 128
        while lead & bit:
            length += 1
            bit >>= 1
        if not 2 <= length <= 7:
            return None
    elif lead >= 128:
        return None
    i += (
        length
        + (1 if block == 6 else 2 if block == 7 else 0)
        + (1 if rate == 12 else 2 if rate in (13, 14) else 0)
    )
    if len(data) <= i or _crc(data[offset : i + 1], 8, 7):
        return None
    return i + 1


class EncodedPrefix:
    """Frame/container-aware first-payload gate, never just a file magic check."""

    def __init__(self, fmt, limit):
        self.format, self.limit = fmt, limit
        self.buffer = bytearray()

    def feed(self, block, eof=False):
        self.buffer.extend(block)
        if len(self.buffer) > self.limit:
            raise GatewayError("encoder_buffer_limit", "编码器未在有限缓冲内产生有效音频。")
        data = self.buffer
        if self.format == "pcm":
            return len(data) >= 2
        if self.format == "wav":
            parser = WAVParser()
            return bool(parser.feed(data))
        if self.format == "flac":
            if len(data) < 8 or data[:4] != b"fLaC":
                return False
            pos = 4
            while pos + 4 <= len(data):
                last = bool(data[pos] & 128)
                length = int.from_bytes(data[pos + 1 : pos + 4], "big")
                pos += 4 + length
                if pos > len(data):
                    return False
                if last:
                    break
            else:
                return False
            if _flac_header(data, pos) is None:
                return False
            for end in range(pos + 6, len(data) - 5):
                if _flac_header(data, end) is not None and _crc(data[pos:end], 16, 0x8005) == 0:
                    return True
            return eof and len(data) > pos + 8 and _crc(data[pos:], 16, 0x8005) == 0
        if self.format == "opus":
            pos = 0
            while pos + 27 <= len(data):
                if data[pos : pos + 4] != b"OggS":
                    return False
                count = data[pos + 26]
                if len(data) < pos + 27 + count:
                    return False
                lacing = data[pos + 27 : pos + 27 + count]
                end = pos + 27 + count + sum(lacing)
                if end > len(data):
                    return False
                payload = data[pos + 27 + count : end]
                if (
                    payload
                    and not payload.startswith((b"OpusHead", b"OpusTags"))
                    and any(x < 255 for x in lacing)
                ):
                    return True
                pos = end
            return False
        if self.format == "aac":
            if len(data) < 7 or data[0] != 255 or data[1] & 246 != 240:
                return False
            length = ((data[3] & 3) << 11) | (data[4] << 3) | (data[5] >> 5)
            return length > 7 and len(data) >= length
        # MPEG layer III: skip a complete ID3 tag and require a complete audio frame.
        pos = 0
        if data[:3] == b"ID3":
            if len(data) < 10:
                return False
            pos = 10 + sum((data[6 + i] & 127) << (21 - 7 * i) for i in range(4))
        if len(data) < pos + 4:
            return False
        h = int.from_bytes(data[pos : pos + 4], "big")
        version, layer, bitrate, rate = h >> 19 & 3, h >> 17 & 3, h >> 12 & 15, h >> 10 & 3
        if h >> 21 != 2047 or version == 1 or layer != 1 or bitrate in (0, 15) or rate == 3:
            return False
        rates = [44100, 48000, 32000]
        sample_rate = rates[rate] // (1 if version == 3 else 2 if version == 2 else 4)
        table = (
            [0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320]
            if version == 3
            else [0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160]
        )
        length = (144000 if version == 3 else 72000) * table[bitrate] // sample_rate + (h >> 9 & 1)
        second = pos + length
        if len(data) < second + 4:
            return False
        h2 = int.from_bytes(data[second : second + 4], "big")
        bitrate2, rate2 = h2 >> 12 & 15, h2 >> 10 & 3
        if (
            h2 >> 21 != 2047
            or (h2 >> 19 & 3) != version
            or (h2 >> 17 & 3) != 1
            or bitrate2 in (0, 15)
            or rate2 != rate
        ):
            return False
        length2 = (144000 if version == 3 else 72000) * table[bitrate2] // sample_rate + (h2 >> 9 & 1)
        return len(data) >= second + length2


class AudioPipeline:
    def __init__(self, config, executable, process_factory=ManagedProcess):
        self.config, self.executable, self.process_factory = config, executable, process_factory
        self.process = None
        self.metrics = {}

    async def run(self, session, fmt, operation, emit, on_error=None):
        audio, timeouts = self.config.value.audio, self.config.value.timeouts
        error = None
        encoder_error = None
        prefix = EncodedPrefix(fmt, audio.max_buffer_bytes)
        started = session.first_pcm_monotonic or time.monotonic()
        prefix_done = False
        stderr_kept = bytearray()
        tasks = []
        notified = False

        def notify(exception):
            nonlocal notified
            if on_error and not notified:
                error = (
                    exception
                    if isinstance(exception, GatewayError)
                    else GatewayError(
                        "audio_timeout" if isinstance(exception, TimeoutError) else "encoder_failed",
                        "音频处理未能完整完成。",
                        504 if isinstance(exception, TimeoutError) else 502,
                    )
                )
                on_error(error)
                notified = True

        try:
            self.process = self.process_factory(
                LaunchSpec(
                    str(self.executable),
                    encode_arguments(audio, session.sample_rate, fmt),
                    str(Path(self.executable).parent),
                )
            )

            async def feed():
                nonlocal encoder_error
                async for block in session.pcm:
                    if not encoder_error:
                        try:
                            await operation.wait(self.process.write(block), timeouts.downstream_stall_seconds)
                        except (OSError, TimeoutError):
                            encoder_error = GatewayError("encoder_input_failed", "音频编码输入失败。")
                            operation.begin_drain()
                self.metrics["upstream_eof_monotonic"] = time.monotonic()
                self.process.end_input()

            async def output():
                nonlocal prefix_done
                while True:
                    timeout = (
                        timeouts.upstream_idle_seconds
                        if prefix_done
                        else max(0.001, started + timeouts.first_encoded_audio_seconds - time.monotonic())
                    )
                    block = await operation.wait(self.process.read(), timeout)
                    if not block:
                        if not prefix_done:
                            if not prefix.feed(b"", eof=True):
                                raise GatewayError("encoder_no_audio", "编码器未产生完整的目标格式音频。")
                            await emit(bytes(prefix.buffer))
                            prefix_done = True
                        return
                    if prefix_done:
                        await emit(block)
                    elif prefix.feed(block):
                        self.metrics["first_encoded_seconds"] = time.monotonic() - started
                        prefix_done = True
                        await emit(bytes(prefix.buffer))
                        prefix.buffer.clear()

            async def stderr():
                while block := await self.process.read("stderr"):
                    remaining = audio.stderr_capture_bytes - len(stderr_kept)
                    if remaining > 0:
                        stderr_kept.extend(block[:remaining])

            tasks = [
                asyncio.create_task(feed()),
                asyncio.create_task(output()),
                asyncio.create_task(stderr()),
            ]
            # Encoder/output errors must not cancel a still-running upstream.
            # Mark discard mode, let feed drain, then propagate the original error.
            pending = set(tasks)
            while pending:
                done, pending = await asyncio.wait(
                    pending, timeout=operation.remaining(0.2), return_when=asyncio.FIRST_COMPLETED
                )
                for task in done:
                    try:
                        task.result()
                    except BaseException as exc:
                        error = error or exc
                        notify(exc)
                        operation.begin_drain()
                        self.process.terminate()
                if (
                    time.monotonic() >= operation.deadline
                    or operation.cancelled_at is not None
                    and time.monotonic() >= operation.cancelled_at + operation.drain_seconds
                ):
                    raise TimeoutError()
            code = await operation.wait(self.process.wait(), timeouts.upstream_idle_seconds)
            self.metrics.update(
                encoder_exit=code, input_samples=session.samples, sample_rate=session.sample_rate
            )
            if error:
                raise error
            if encoder_error:
                raise encoder_error
            if code != 0 or not session.complete or not prefix_done:
                raise GatewayError(
                    "audio_incomplete", "音频生成或编码未完整完成。", uncertain=not session.complete
                )
        except GatewayError as exc:
            notify(exc)
            raise
        except BaseException as exc:
            error = GatewayError(
                "audio_timeout" if isinstance(exc, TimeoutError) else "encoder_failed",
                "音频处理未能完整完成。",
                504 if isinstance(exc, TimeoutError) else 502,
                uncertain=not session.complete,
            )
            notify(error)
            raise error from None
        finally:
            if self.process:
                self.process.terminate()
                await asyncio.gather(*tasks, return_exceptions=True)
                await self.process.wait()
                self.process.close()


async def decode_check(executable, path, expected_samples, expected_rate, deadline):
    from .backends.base import Operation

    operation = Operation(deadline)
    args = [
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-i",
        str(path),
        "-c:a",
        "pcm_s16le",
        "-f",
        "wav",
        "pipe:1",
    ]
    process = ManagedProcess(LaunchSpec(str(executable), args, str(Path(executable).parent)))
    parser = WAVParser(expected_rate)
    non_silent = False
    count = 0

    async def drain_error():
        while await process.read("stderr"):
            pass

    stderr = asyncio.create_task(drain_error())
    try:
        process.end_input()
        while block := await operation.wait(process.read(), 30):
            pcm = parser.feed(block)
            count += len(pcm) // 2
            non_silent |= any(pcm)
        parser.finish()
        code = await operation.wait(process.wait(), 30)
        await stderr
        if code or count != expected_samples or not non_silent:
            raise GatewayError("startup_decode_failed", "启动音频解码校验失败。")
        return {"decoder_exit": code, "decoded_samples": count}
    finally:
        process.terminate()
        await asyncio.gather(stderr, return_exceptions=True)
        await process.wait()
        process.close()
