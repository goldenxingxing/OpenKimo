"""飞书 ↔ OpenKimo 直连桥入口。

用法：
    pip install -r requirements.txt
    cp config.example.json config.json   # 填入飞书凭据
    python main.py
"""
from pathlib import Path

from feishu_bridge.bot import FeishuOpenKimoBridge
from feishu_bridge.config import load_config

if __name__ == "__main__":
    base_dir = Path(__file__).parent
    cfg = load_config(base_dir)
    FeishuOpenKimoBridge(cfg, base_dir).run()
