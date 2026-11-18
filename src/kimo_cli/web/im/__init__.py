"""IM bridge: bridge external IM platforms (Feishu first) into OpenKimo sessions.

Design: output/openkimo/OpenKimo_IM桥接_设计方案_2026-11-18.md
"""

from kimo_cli.web.im.adapter import AdapterContext, ImAdapter
from kimo_cli.web.im.manager import ImBridgeManager
from kimo_cli.web.im.runner import ImTurnRunner

__all__ = ["AdapterContext", "ImAdapter", "ImBridgeManager", "ImTurnRunner"]
