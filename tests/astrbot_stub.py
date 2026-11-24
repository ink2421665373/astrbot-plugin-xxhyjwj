"""测试用的 astrbot 最小替身。

本机没有安装 astrbot（也无法联网安装真实包），但 tools.py 顶层要
``from astrbot.api import logger`` 与 ``from astrbot.api.event import ...``，
因此这里提供一个只够导入用的桩：llm_tool 装饰器原样返回函数，
同时把工具名记在函数上，便于测试断言。
"""

import sys
from types import ModuleType


def _llm_tool(name=None):
    """模拟 @filter.llm_tool(name=...)。

    真实实现会把工具注册进 AstrBot Core 并解析 docstring 的 Args 生成 schema；
    这里只保留必要的标记：原样返回函数 + 记录工具名。
    """

    def _wrap(fn):
        fn.__llm_tool_name__ = name or fn.__name__
        return fn

    return _wrap


def _build():
    astrbot = ModuleType("astrbot")
    api = ModuleType("astrbot.api")
    api.__path__ = []
    event = ModuleType("astrbot.api.event")
    star = ModuleType("astrbot.api.star")

    class _Logger:
        def info(self, *a, **k):
            pass

        def warning(self, *a, **k):
            pass

        def error(self, *a, **k):
            pass

        def debug(self, *a, **k):
            pass

    api.logger = _Logger()

    class AstrBotConfig(dict):  # main.py 需要
        pass

    api.AstrBotConfig = AstrBotConfig

    api.register = lambda *a, **k: (lambda fn: fn)
    api.web = None

    class AstrMessageEvent:  # 仅作类型占位
        pass

    class _FilterNS(ModuleType):
        pass

    filter_ns = _FilterNS("astrbot.api.event.filter")
    filter_ns.llm_tool = _llm_tool  # @filter.llm_tool
    filter_ns.command = lambda *a, **k: (lambda fn: fn)
    filter_ns.command_group = lambda *a, **k: (lambda fn: fn)
    filter_ns.event_message_type = lambda *a, **k: (lambda fn: fn)
    filter_ns.platform_adapter_type = lambda *a, **k: (lambda fn: fn)
    filter_ns.permission_type = lambda *a, **k: (lambda fn: fn)

    class EventMessageType:
        ALL = "ALL"
        PRIVATE_MESSAGE = "PRIVATE_MESSAGE"
        GROUP_MESSAGE = "GROUP_MESSAGE"

    filter_ns.EventMessageType = EventMessageType

    class PlatformAdapterType:
        AIOCQHTTP = "aiocqhttp"
        ALL = "ALL"

    filter_ns.PlatformAdapterType = PlatformAdapterType

    class PermissionType:
        ADMIN = "admin"
        MEMBER = "member"

    filter_ns.PermissionType = PermissionType

    event.AstrMessageEvent = AstrMessageEvent
    event.filter = filter_ns
    event.MessageEventResult = type("MessageEventResult", (), {})

    class StarTools:  # astrbot >= 4.5.1
        pass

    star.StarTools = StarTools
    star.Context = type("Context", (), {})
    star.Star = type("Star", (), {})
    star.register = lambda *a, **k: (lambda fn: fn)

    comp = ModuleType("astrbot.api.message_components")
    comp.Plain = type("Plain", (), {})
    comp.At = type("At", (), {})
    comp.Image = type("Image", (), {"fromBytes": staticmethod(lambda data: ("image", data))})
    comp.Reply = type("Reply", (), {"__init__": lambda self, **kw: setattr(self, "kw", kw)})
    comp.File = type("File", (), {})
    comp.Node = type("Node", (), {})
    star_comp = comp

    api.event = event
    api.star = star
    astrbot.api = api

    return {
        "astrbot": astrbot,
        "astrbot.api": api,
        "astrbot.api.event": event,
        "astrbot.api.event.filter": filter_ns,
        "astrbot.api.star": star,
        "astrbot.api.message_components": star_comp,
    }


def install():
    """把桩装进 sys.modules（幂等）。"""
    if "astrbot" in sys.modules and getattr(sys.modules["astrbot"], "_is_stub", False):
        return sys.modules["astrbot"]
    mods = _build()
    for mod in mods.values():
        mod._is_stub = True
    sys.modules.update(mods)
    return mods["astrbot"]


install()
