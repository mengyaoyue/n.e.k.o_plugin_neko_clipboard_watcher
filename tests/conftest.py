"""pytest stub：本地无宿主 SDK 时注入最小 plugin.sdk.plugin。

官方 CI 不跑 pytest（只有 ruff + check），本文件只服务本地开发：
让仓库根的 __init__.py（依赖宿主 SDK）在 pytest 9 的包收集下可导入。
"""

import sys
import types
from typing import Any

if "plugin" not in sys.modules:
    plugin_mod = types.ModuleType("plugin")
    sdk_mod = types.ModuleType("plugin.sdk")
    sdk_plugin = types.ModuleType("plugin.sdk.plugin")

    class NekoPluginBase:
        def __init__(self, ctx: Any = None) -> None:
            self.ctx = ctx

        def enable_file_logging(self, log_level: str = "INFO"):
            import logging

            logging.basicConfig(level=log_level)
            return logging.getLogger("neko_stub")

        def data_path(self, *parts):
            import tempfile
            from pathlib import Path

            base = Path(tempfile.mkdtemp(prefix="neko_stub_data_"))
            return base.joinpath(*parts) if parts else base

    def Ok(result: Any = None) -> dict[str, Any]:
        return {"ok": True, "result": result}

    def Err(err: Any = None) -> dict[str, Any]:
        return {"ok": False, "error": err}

    class SdkError(Exception):
        pass

    def _decorator(*args: Any, **kwargs: Any):
        # 同时支持裸装饰 @deco 与带参 @deco(...)：裸用时直接原样返回
        if len(args) == 1 and not kwargs and callable(args[0]):
            return args[0]

        def decorator(fn):
            return fn

        return decorator

    sdk_plugin.NekoPluginBase = NekoPluginBase
    sdk_plugin.Ok = Ok
    sdk_plugin.Err = Err
    sdk_plugin.SdkError = SdkError
    sdk_plugin.lifecycle = _decorator
    sdk_plugin.llm_tool = _decorator
    sdk_plugin.plugin_entry = _decorator
    sdk_plugin.neko_plugin = _decorator

    plugin_mod.sdk = sdk_mod
    sdk_mod.plugin = sdk_plugin
    plugin_mod.__path__ = []  # 命名空间化，避免解析到真实宿主
    sys.modules["plugin"] = plugin_mod
    sys.modules["plugin.sdk"] = sdk_mod
    sys.modules["plugin.sdk.plugin"] = sdk_plugin
