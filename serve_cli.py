#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TraceableMetrics 工作台启动器（唯一推荐入口）。

存在的理由（两条真实事故，不是设计洁癖）：
1. 本机有全局 HTTP 代理（http_proxy=127.0.0.1:xxxxx）且 NO_PROXY 为空时，
   浏览器/脚本访问 127.0.0.1:8765 会被代理劫持，页面出现 404/502 等随机报错
   ——真人测试（D12）就是倒在这里。本启动器在进程内强制清掉代理变量，
   保证子进程与本机通信一律直连。
2. Windows 上 SO_REUSEADDR 允许第二个进程"成功"绑定同一端口，
   于是新旧两份代码同时监听、请求随机分发，表现为"功能时有时无"。
   本启动器先探活：已有健康实例就直接复用，不再起第二个。

用法：双击「启动工作台.bat」；或命令行 python serve_cli.py
"""
from __future__ import annotations

import os
import socket
import sys
import time
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent
HOST, PORT = "127.0.0.1", 8765
URL = f"http://{HOST}:{PORT}"


def kill_local_proxy() -> None:
    """清掉所有代理变量——本机回环通信绝不能走代理。"""
    for k in list(os.environ):
        if k.lower() in ("http_proxy", "https_proxy", "all_proxy", "ftp_proxy"):
            os.environ.pop(k, None)
    os.environ["NO_PROXY"] = "127.0.0.1,localhost,<-loopback>"
    os.environ["no_proxy"] = os.environ["NO_PROXY"]


def port_alive(host: str = HOST, port: int = PORT, timeout: float = 1.0) -> bool:
    """探活：能连通且拿得到首页，才算真的活着。"""
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.sendall(b"GET / HTTP/1.1\r\nHost: %s\r\nConnection: close\r\n\r\n" % host.encode())
            data = s.recv(2048)
    except OSError:
        return False
    return b"TRACEABLE" in data.upper() or b"200" in data.split(b"\r\n", 1)[0]


def main() -> int:
    kill_local_proxy()
    os.environ["PYTHONUTF8"] = "1"
    os.environ["PYTHONIOENCODING"] = "utf-8"
    sys.path.insert(0, str(ROOT))

    if port_alive():
        print("=" * 56)
        print("已有工作台实例在运行 —— 直接复用，不再启动第二个。")
        print("（同时跑两个实例会导致请求随机分发、功能时有时无）")
        print(f"  {URL}")
        print("如需重启：关掉那个黑窗口后，再双击启动。")
        print("=" * 56)
        webbrowser.open(URL)
        return 0

    print("=" * 56)
    print("TraceableMetrics 本地工作台 · 启动中")
    print(f"  解释器: {sys.executable}")
    print(f"  仓库根: {ROOT}")
    print(f"  已清除代理变量，本机通信直连")
    print("=" * 56)

    # 延迟到确认无实例后再开浏览器，避免打开的是旧页面
    import threading

    threading.Timer(1.2, lambda: webbrowser.open(URL)).start()

    try:
        import plugins.commerce.webapp as webapp
        webapp.serve()
    except OSError as e:
        print(f"\n[启动失败] 端口 {PORT} 被占用或无法绑定：{e}")
        print("处理办法：关掉其它 TraceableMetrics 黑窗口后重试。")
        return 1
    except KeyboardInterrupt:
        print("\n服务已停止。")
    return 0


if __name__ == "__main__":
    time.sleep(0)
    raise SystemExit(main())
