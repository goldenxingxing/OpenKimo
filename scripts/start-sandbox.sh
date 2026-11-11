#!/bin/bash
set -e

echo "========================================" >&2
echo "  kimo-agent-sandbox starting up..." >&2
echo "========================================" >&2

# -----------------------------------------------------------
# 1. 启动虚拟显示 (Xvfb)
# -----------------------------------------------------------
if [ "$ENABLE_BROWSER" != "false" ]; then
    echo "[1/4] Starting Xvfb virtual display..." >&2
    export DISPLAY=:99
    Xvfb :99 -screen 0 1920x1080x24 -ac +extension GLX +render -noreset &
    XVFB_PID=$!
    # `sleep 1` was the only gate here, so a display that died immediately
    # (:99 already in use, missing binary) was reported as started and every
    # later browser step failed against nothing.
    if ! kill -0 "$XVFB_PID" 2>/dev/null; then
        echo "      ERROR: Xvfb exited immediately; browser tools unavailable" >&2
        exit 1
    fi
    echo "      Xvfb started on DISPLAY=:99 (PID $XVFB_PID)" >&2
fi

# -----------------------------------------------------------
# 2. 启动 Jupyter Kernel Server
# -----------------------------------------------------------
if [ "$ENABLE_JUPYTER" != "false" ]; then
    echo "[2/4] Starting Jupyter Kernel Server..." >&2
    # 127.0.0.1, not 0.0.0.0: the kernel API has no authentication, and the
    # only callers (the worker, the container healthcheck) are in this
    # container. On 0.0.0.0 it was reachable from every other sandbox on
    # kimo-agent-network.
    python /app/kernel_server.py \
        --host 127.0.0.1 \
        --port "${JUPYTER_KERNEL_PORT:-8888}" \
        --log-level info > /app/logs/kernel_server.log 2>&1 &
    KERNEL_PID=$!
    echo "      Kernel Server PID: $KERNEL_PID" >&2
fi

# -----------------------------------------------------------
# 3. 启动 Browser Guard (后台)
# -----------------------------------------------------------
if [ "$ENABLE_BROWSER" != "false" ]; then
    echo "[3/4] Starting Browser Guard..." >&2
    # Wait for display using xdotool (lightweight, no .Xauthority needed).
    # The loop used to fall through silently after 30 tries, starting Browser
    # Guard against a display that never came up.
    display_ready=false
    for _ in $(seq 1 30); do
        if DISPLAY=:99 xdotool getdisplaygeometry > /dev/null 2>&1; then
            display_ready=true
            echo "      Display is ready" >&2
            break
        fi
        sleep 0.5
    done
    if [ "$display_ready" != "true" ]; then
        echo "      ERROR: DISPLAY=:99 not usable after 15s; not starting Browser Guard" >&2
        exit 1
    fi
    # BrowserGuard 在 monitor 模式下持续运行
    python /app/browser_guard.py --monitor > /app/logs/browser_guard.log 2>&1 &
    BROWSER_PID=$!
    echo "      Browser Guard PID: $BROWSER_PID" >&2
fi

# -----------------------------------------------------------
# 4. 启动 KimoCLI Worker (前台)
# -----------------------------------------------------------
echo "[4/4] Starting KimoCLI Worker..." >&2
echo "      Session ID: ${KIMO_SESSION_ID:-unknown}" >&2
echo "      Work Dir: ${KIMO_WORK_DIR:-/app}" >&2
echo "" >&2

# 等待必要的服务就绪。
# 这里原本是固定的 `sleep 2`：负载高时 kernel server 还没监听，worker 的第一次
# notebook 调用就 connection refused；负载低时又白等 2 秒。改成真的探测端口。
if [ "$ENABLE_JUPYTER" != "false" ]; then
    kernel_port="${JUPYTER_KERNEL_PORT:-8888}"
    kernel_ready=false
    for _ in $(seq 1 60); do
        if ! kill -0 "$KERNEL_PID" 2>/dev/null; then
            echo "      ERROR: Kernel Server exited during startup; see /app/logs/kernel_server.log" >&2
            exit 1
        fi
        if python -c "import socket,sys; s=socket.socket(); s.settimeout(0.5); sys.exit(0 if s.connect_ex(('127.0.0.1', $kernel_port))==0 else 1)" 2>/dev/null; then
            kernel_ready=true
            break
        fi
        sleep 0.5
    done
    if [ "$kernel_ready" != "true" ]; then
        echo "      ERROR: Kernel Server not listening on $kernel_port after 30s; see /app/logs/kernel_server.log" >&2
        exit 1
    fi
    echo "      Kernel Server ready on 127.0.0.1:$kernel_port" >&2
fi

# 启动 Worker (JSON-RPC over stdin/stdout，由 Gateway 管道代理)
#
# 保持 exec：worker 成为 PID 1，它退出时容器随之销毁，后台的 Xvfb /
# kernel server / Browser Guard 由容器生命周期回收，不需要 trap。
exec python -m kimo_cli.web.runner.worker "${KIMO_SESSION_ID:-default}"
