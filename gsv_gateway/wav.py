"""Incremental RIFF/PCM normalization, independent of network chunk boundaries."""

import struct


class WAVParser:
    def __init__(self, expected_rate=None):
        self.buffer = bytearray()
        self.expected_rate = expected_rate
        self.sample_rate = None
        self.data = False
        self.started = False
        self.format_seen = False
        self.samples = 0

    def feed(self, block):
        self.buffer.extend(block)
        if not self.started:
            if len(self.buffer) < 12:
                return b""
            if self.buffer[:4] != b"RIFF" or self.buffer[8:12] != b"WAVE":
                raise ValueError("Invalid WAV signature")
            del self.buffer[:12]
            self.started = True
        while not self.data and len(self.buffer) >= 8:
            tag = self.buffer[:4]
            length = struct.unpack_from("<I", self.buffer, 4)[0]
            if tag == b"data":
                if not self.format_seen:
                    raise ValueError("Missing WAV fmt chunk")
                del self.buffer[:8]
                self.data = True
                break
            if length > 65536:
                raise ValueError("WAV header chunk too large")
            needed = 8 + length + length % 2
            if len(self.buffer) < needed:
                break
            if tag == b"fmt ":
                if length < 16:
                    raise ValueError("Short WAV fmt chunk")
                code, channels, rate, byte_rate, align, bits = struct.unpack_from("<HHIIHH", self.buffer, 8)
                if code != 1 or channels != 1 or bits != 16 or align != 2 or byte_rate != rate * 2:
                    raise ValueError("Expected mono s16le PCM")
                if (
                    rate < 8000
                    or rate > 192000
                    or self.expected_rate is not None
                    and rate != self.expected_rate
                ):
                    raise ValueError("Unexpected sample rate")
                self.sample_rate, self.format_seen = rate, True
            del self.buffer[:needed]
        if not self.data:
            if len(self.buffer) > 65544:
                raise ValueError("WAV header too large")
            return b""
        count = len(self.buffer) // 2 * 2
        result = bytes(self.buffer[:count])
        del self.buffer[:count]
        self.samples += count // 2
        return result

    def finish(self):
        if not self.data or not self.samples or self.buffer:
            raise ValueError("Empty or incomplete PCM stream")
