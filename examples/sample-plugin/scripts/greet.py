#!/usr/bin/env python3
"""Python tool: generate a greeting message."""

import json
import sys

GREETINGS = {
    "en": "Hello, {name}! Welcome!",
    "zh": "你好，{name}！欢迎！",
    "ja": "こんにちは、{name}さん！ようこそ！",
}

try:
    params = json.loads(sys.stdin.read()) if not sys.stdin.isatty() else {}
except json.JSONDecodeError:
    params = {}
if not isinstance(params, dict):
    params = {}
name = params.get("name", "World")
lang = params.get("lang", "en")

template = GREETINGS.get(lang, GREETINGS["en"])
print(template.format(name=name))
