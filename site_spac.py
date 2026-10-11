# -*- coding: utf-8 -*-
"""作物生长模型 —— 独立网站启动器。

微服务版是「一个网关 + 两个子服务」合成一个网站；本启动器只起 SPAC 一个
服务，并且**根路径对外**（不再挂网关的 /spac 子路径），用于「两个模块拆成
两个独立网站」的部署形态。

    本地   python site_spac.py             → http://127.0.0.1:8761
    云端   平台注入 $PORT，自动绑 0.0.0.0   → 例如 Render 免费实例

两个环境变量必须在 import 服务**之前**设置（服务模块在导入时读取）：
    SPAC_BASE_PATH=""       内部路由前缀 → 根路径
    SPAC_PUBLIC_PREFIX=""   页面/资源前缀 → 根路径
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# 必须先于 services.spac.main 的 import —— 服务模块导入时就读这两个变量
os.environ["SPAC_BASE_PATH"] = ""
os.environ["SPAC_PUBLIC_PREFIX"] = ""

from core.config import CONFIG                                # noqa: E402
from services.spac.main import app                            # noqa: E402

DEFAULT_PORT = 8761


def main() -> None:
    import argparse
    import threading
    import webbrowser

    import uvicorn

    ap = argparse.ArgumentParser(description="作物生长模型独立网站")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--host", default="")
    args = ap.parse_args()

    # 云平台（Render 等）注入 PORT 并要求监听 0.0.0.0；本地默认只绑回环
    cloud_port = os.environ.get("PORT")
    host = args.host or ("0.0.0.0" if cloud_port else "127.0.0.1")
    port = int(cloud_port) if cloud_port else args.port

    if not cloud_port and CONFIG.auto_open_browser:
        url = f"http://127.0.0.1:{port}/"
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()

    print(f"基于 SPAC 的典型作物生长模型  →  http://{host}:{port}/")
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
