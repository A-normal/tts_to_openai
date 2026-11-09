from .gsv import GSVAdapter


def create_adapter(config):
    if config.value.backend.type != "gpt_sovits":
        raise ValueError("Unknown backend")
    return GSVAdapter(config)
