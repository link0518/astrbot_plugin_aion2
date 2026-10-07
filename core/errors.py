"""上游错误的语义化封装，附带面向用户的中文提示。"""


class Aion2Error(Exception):
    """本插件所有错误的基类。"""

    message = "查询失败"

    def __init__(self, detail: str = ""):
        self.detail = detail
        super().__init__(detail or self.message)

    @property
    def hint(self) -> str:
        return self.message


class RegionError(Aion2Error):
    message = "未知的查询区域"


class NotFound(Aion2Error):
    message = "没有找到对应数据"


class BadRequest(Aion2Error):
    message = "查询条件不完整"


class FeatureUnavailable(Aion2Error):
    message = "当前区域不支持这项数据"


class NoSeason(Aion2Error):
    message = "官方已关闭该排行榜"


class RateLimited(Aion2Error):
    message = "上游限流，请稍后再试"


class UpstreamError(Aion2Error):
    message = "上游接口返回异常"


class NetworkError(Aion2Error):
    message = "连接上游失败"


# 上游以这些状态码表达语义，映射到本模块的异常
STATUS_MAP = {
    400: BadRequest,
    401: BadRequest,
    403: NoSeason,
    404: NotFound,
    429: RateLimited,
}


def from_status(status: int, detail: str = "") -> Aion2Error:
    cls = STATUS_MAP.get(status)
    if cls is None:
        cls = UpstreamError if status >= 500 else UpstreamError
    return cls(detail or f"HTTP {status}")
