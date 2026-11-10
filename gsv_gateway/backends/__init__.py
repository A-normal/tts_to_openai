from dataclasses import dataclass
from typing import Literal

from ..config import GatewayConfig, VoiceRegistry
from .base import BackendAdapter
from .gsv import GSVAdapter
from .gsv_config import TYPE, GSVGatewayConfig, GSVVoiceRegistry


@dataclass(frozen=True)
class BackendRegistration:
    gateway_config: type[GatewayConfig]
    voice_registry: type[VoiceRegistry]
    adapter: type[BackendAdapter]
    streaming: Literal["segmented", "buffered", "incremental"]


DEFAULT_BACKEND = TYPE
BACKENDS = {TYPE: BackendRegistration(GSVGatewayConfig, GSVVoiceRegistry, GSVAdapter, "segmented")}


def get_registration(name):
    if not isinstance(name, str) or name not in BACKENDS:
        raise ValueError("Unknown backend type")
    return BACKENDS[name]


def create_adapter(config) -> BackendAdapter:
    return config.backend_registration.adapter(config)
