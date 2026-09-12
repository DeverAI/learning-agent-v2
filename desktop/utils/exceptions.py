"""OISystem 自定义异常。"""


class OISystemError(Exception):
    """基类。"""


class ConfigError(OISystemError):
    """配置相关错误。"""


class FocusLockedError(OISystemError):
    """专注模式锁定中，禁止某操作。"""


class AICallError(OISystemError):
    """AI 接口调用失败。"""


class SiteBlockedError(OISystemError):
    """网站被管控拦截。"""


class OJFetchError(OISystemError):
    """OJ 数据抓取失败。"""
