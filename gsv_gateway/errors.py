from dataclasses import dataclass


@dataclass
class GatewayError(Exception):
    code: str
    message: str
    status: int = 502
    param: str | None = None
    uncertain: bool = False

    def payload(self):
        kind = "invalid_request_error" if self.status < 500 else "server_error"
        return {"error": {"message": self.message, "type": kind, "param": self.param, "code": self.code}}


def unavailable(code="backend_unavailable"):
    return GatewayError(code, "服务当前不能安全受理语音请求，请检查本机状态。", 503)
