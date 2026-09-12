import logging
import logging.handlers
import os
import sys

# 同上：stderr 也要切到 UTF-8，否则 logging.handleError 打印 traceback 时
# 会在 GBK 上二次抛错并穿透出去（这是上面那段注释里的第 3 步）。
try:
    if sys.stderr is not None:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "storage", "logs")
os.makedirs(LOG_DIR, exist_ok=True)

_logger = logging.getLogger("learning_agent")
_logger.setLevel(logging.INFO)
_logger.handlers.clear()

_formatter = logging.Formatter(
    "%(asctime)s [%(levelname)s] %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)


def _utf8_stream(stream):
    """把标准流切到 UTF-8 + errors=replace。

    ## 为什么必须有这个（2026-09-11 实测定位）

    在中文 Windows 上 `sys.stdout` / `sys.stderr` 的默认编码是 **GBK**。
    于是写下一条**包含 GBK 编不出的字符**的日志时（实测触发字符是 `²`，
    来自模型回复里的 `a²+b²=c²`）会连锁失败：

    1. `StreamHandler.emit` 里 `stream.write()` 抛 `UnicodeEncodeError`；
    2. logging 按设计把它交给 `handleError()`，而 `handleError` 会
       `traceback.print_exception(..., file=sys.stderr)`；
    3. **stderr 也是 GBK**，打印带那个字符的 traceback 时**再次抛错**；
    4. 这第二次的异常不受 logging 保护，直接**穿透到调用方**。

    实测后果：`ai_service.deepseek_chat()` 抛 `UnicodeEncodeError`，
    而调用方（备课的 `_write_section`）用 `except Exception` 兜住并返回空串 ——
    **表现成"课稿一片都写不出来"，跟编码问题毫无字面关联**，排查代价极高。

    `errors="replace"` 保证日志永远写得出去（坏字符变 `?`），
    日志是诊断用的，绝不能因为一个字符把业务流程搞崩。
    """
    if stream is None:
        return None
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
        return stream
    except Exception:
        return stream


_stream_handler = logging.StreamHandler(_utf8_stream(sys.stdout))
_stream_handler.setLevel(logging.INFO)
_stream_handler.setFormatter(_formatter)
_logger.addHandler(_stream_handler)

_file_handler = logging.handlers.RotatingFileHandler(
    os.path.join(LOG_DIR, "app.log"), encoding="utf-8",
    maxBytes=10 * 1024 * 1024, backupCount=3
)
_file_handler.setLevel(logging.DEBUG)
_file_handler.setFormatter(_formatter)
_logger.addHandler(_file_handler)

# 测试环境可通过 DSH_ERR_LOG_PATH 重定向 Err.log（含子进程：pytest conftest
# 设置的环境变量会被子进程继承），生产环境不受影响。
ERR_LOG_PATH = (
    os.environ.get("DSH_ERR_LOG_PATH")
    or os.path.join(os.path.dirname(os.path.abspath(__file__)), "Err.log")
)


def log_error(module: str, message: str):
    _logger.error("[%s] %s", module, message)
    try:
        from datetime import datetime
        ts = datetime.now().strftime('%m-%d %H:%M')
        with open(ERR_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"[{ts}] [{module}] {message[:500]}\n")
    except Exception:
        _logger.exception("Failed to write Err.log for module %s", module)


def get_logger() -> logging.Logger:
    return _logger
