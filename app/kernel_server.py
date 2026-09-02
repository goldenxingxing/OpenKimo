#!/usr/bin/env python3
"""
Jupyter Kernel Management Server
提供 kernel 的 reset、interrupt 和 connectionFile 查询接口
"""

import logging
import os
from contextlib import asynccontextmanager
from typing import Dict, Any, Optional

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from jupyter_kernel import JupyterKernel

# 配置日志
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

# 全局 kernel 实例
kernel_instance: Optional[JupyterKernel] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    global kernel_instance
    try:
        logger.info("正在初始化 Jupyter Kernel...")
        kernel_instance = JupyterKernel()
        logger.info("Jupyter Kernel 初始化完成")
        yield
    except Exception as e:
        logger.error(f"初始化失败: {str(e)}")
        raise
    finally:
        if kernel_instance:
            logger.info("正在关闭 Jupyter Kernel...")
            kernel_instance.shutdown()
            logger.info("Jupyter Kernel 已关闭")


# 创建 FastAPI 应用
app = FastAPI(
    title="Jupyter Kernel Management Server",
    description="提供 Jupyter Kernel 的管理接口，包括重置、中断和连接信息查询",
    version="1.0.0",
    lifespan=lifespan,
)

# CORS 中间件。allow_credentials 关闭：浏览器本来就拒绝
# "allow_origins=* + credentials" 这个组合，所以打开它从未生效过，只是让这个
# 无鉴权 API 看起来比实际更开放。本服务只监听回环，也没有浏览器端调用方。
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# 响应模型
class ApiResponse(BaseModel):
    """API 响应基础模型"""

    success: bool
    message: str
    data: Optional[Dict[str, Any]] = None


class KernelStatusResponse(BaseModel):
    """Kernel 状态响应模型"""

    success: bool
    kernel_alive: bool
    kernel_pid: Optional[int] = None
    connection_file: Optional[str] = None
    client_connected: bool


class ConnectionInfoResponse(BaseModel):
    """连接信息响应模型"""

    success: bool
    connection_file: Optional[str] = None
    connection_info: Optional[Dict[str, Any]] = None
    kernel_alive: bool
    kernel_pid: Optional[int] = None
    message: Optional[str] = None


# API 路由
@app.get("/", response_model=Dict[str, str])
async def root():
    """根路径，返回服务信息"""
    return {
        "service": "Jupyter Kernel Management Server",
        "version": "1.0.0",
        "status": "running",
    }


@app.get("/health")
async def health_check():
    """健康检查接口"""
    global kernel_instance
    if not kernel_instance:
        raise HTTPException(status_code=503, detail="Kernel not initialized")

    status = kernel_instance.get_kernel_status()
    # `success` only reports that the status *query* worked — get_kernel_status
    # returns success=True with kernel_alive=False for a kernel that has died.
    # Gating on it alone made a dead kernel read as healthy to every probe,
    # including Dockerfile.sandbox's HEALTHCHECK, so the container stayed
    # "healthy" while no code could run in it.
    healthy = bool(status.get("success")) and bool(status.get("kernel_alive"))
    return JSONResponse(status_code=200 if healthy else 503, content=status)


@app.post("/kernel/reset", response_model=ApiResponse)
async def reset_kernel():
    """重置 kernel"""
    global kernel_instance
    if not kernel_instance:
        raise HTTPException(status_code=503, detail="Kernel not initialized")

    try:
        logger.info("收到 kernel 重置请求")
        result = kernel_instance.reset_kernel()

        if result.get("success"):
            logger.info(f"Kernel 重置成功: {result.get('message')}")
            return ApiResponse(
                success=True,
                message=result.get("message", "Kernel reset successfully"),
                data={
                    "old_connection_file": result.get("old_connection_file"),
                    "new_connection_file": result.get("new_connection_file"),
                },
            )
        else:
            logger.error(f"Kernel 重置失败: {result.get('message')}")
            raise HTTPException(
                status_code=500, detail=result.get("message", "Failed to reset kernel")
            )
    except HTTPException:
        # The failure branch above raises this deliberately; catching it here
        # rewrapped the specific message as "Internal server error: 500: ..."
        # and discarded the original traceback.
        raise
    except Exception as e:
        logger.exception("重置 kernel 时发生错误")
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")


@app.post("/kernel/interrupt", response_model=ApiResponse)
async def interrupt_kernel():
    """中断 kernel 执行"""
    global kernel_instance
    if not kernel_instance:
        raise HTTPException(status_code=503, detail="Kernel not initialized")

    try:
        logger.info("收到 kernel 中断请求")
        result = kernel_instance.interrupt_kernel()

        if result.get("success"):
            logger.info(f"Kernel 中断成功: {result.get('message')}")
            return ApiResponse(
                success=True,
                message=result.get("message", "Kernel interrupted successfully"),
                data={"kernel_pid": result.get("kernel_pid")},
            )
        else:
            logger.error(f"Kernel 中断失败: {result.get('message')}")
            raise HTTPException(
                status_code=500,
                detail=result.get("message", "Failed to interrupt kernel"),
            )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("中断 kernel 时发生错误")
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")


#: Fields of the Jupyter connection file that are enough to authenticate to
#: the kernel. `key` signs every message on the shell channel, so handing it
#: out is handing out code execution.
_CONNECTION_SECRET_FIELDS = frozenset({"key"})


def _redact_connection_info(info: Dict[str, Any]) -> Dict[str, Any]:
    """Drop the HMAC signing key from a connection-info payload.

    The ports and transport are useful for diagnosing a wedged kernel; the key
    is not, and anything entitled to it can read the connection file off disk
    in this container.
    """
    return {
        k: ("<redacted>" if k in _CONNECTION_SECRET_FIELDS else v)
        for k, v in (info or {}).items()
    }


@app.get("/kernel/connection", response_model=ConnectionInfoResponse)
async def get_connection_info():
    """获取 kernel 连接信息"""
    global kernel_instance
    if not kernel_instance:
        raise HTTPException(status_code=503, detail="Kernel not initialized")

    try:
        logger.info("收到连接信息查询请求")
        result = kernel_instance.get_connection_info()

        if result.get("success"):
            return ConnectionInfoResponse(
                success=True,
                connection_file=result.get("connection_file"),
                connection_info=_redact_connection_info(
                    result.get("connection_info", {})
                ),
                kernel_alive=result.get("kernel_alive", False),
                kernel_pid=result.get("kernel_pid"),
            )
        else:
            logger.error(f"获取连接信息失败: {result.get('message')}")
            return ConnectionInfoResponse(
                success=False,
                message=result.get("message", "Failed to get connection info"),
                kernel_alive=False,
            )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("获取连接信息时发生错误")
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")


@app.get("/kernel/status", response_model=KernelStatusResponse)
async def get_kernel_status():
    """获取 kernel 状态"""
    global kernel_instance
    if not kernel_instance:
        raise HTTPException(status_code=503, detail="Kernel not initialized")

    try:
        result = kernel_instance.get_kernel_status()

        if result.get("success"):
            return KernelStatusResponse(
                success=True,
                kernel_alive=result.get("kernel_alive", False),
                kernel_pid=result.get("kernel_pid"),
                connection_file=result.get("connection_file"),
                client_connected=result.get("client_connected", False),
            )
        else:
            logger.error(f"获取 kernel 状态失败: {result.get('message')}")
            raise HTTPException(
                status_code=500,
                detail=result.get("message", "Failed to get kernel status"),
            )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("获取 kernel 状态时发生错误")
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")


# 为了兼容性，也提供一个简化的连接文件路径接口
@app.get("/kernel/connection-file")
async def get_connection_file_path():
    """获取连接文件路径（简化接口）"""
    global kernel_instance
    if not kernel_instance:
        raise HTTPException(status_code=503, detail="Kernel not initialized")

    try:
        result = kernel_instance.get_connection_info()

        if result.get("success"):
            return {
                "success": True,
                "connection_file": result.get("connection_file"),
                "kernel_alive": result.get("kernel_alive", False),
            }
        else:
            raise HTTPException(
                status_code=500,
                detail=result.get("message", "Failed to get connection file"),
            )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("获取连接文件路径时发生错误")
        raise HTTPException(status_code=500, detail=f"Internal server error: {str(e)}")


@app.get("/kernel/debug")
async def debug_kernel():
    """调试 kernel 管理器状态（开发用）。

    默认关闭：它返回 kernel manager 的原始内部状态（路径、连接细节），
    docstring 一直写着「开发用」，但此前是无条件注册的。设
    ``KERNEL_SERVER_DEBUG=1`` 才启用。
    """
    global kernel_instance
    if os.environ.get("KERNEL_SERVER_DEBUG", "").lower() not in ("1", "true", "yes"):
        raise HTTPException(status_code=404, detail="Not Found")
    if not kernel_instance:
        raise HTTPException(status_code=503, detail="Kernel not initialized")

    try:
        debug_info = kernel_instance.debug_kernel_manager()
        return JSONResponse(content=debug_info)
    except Exception as e:
        logger.error(f"调试 kernel 时发生错误: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Debug error: {str(e)}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Jupyter Kernel Management Server")
    # Loopback by default. Everything that talks to this server — the worker,
    # the container healthcheck — runs inside this container; binding
    # 0.0.0.0 published an unauthenticated kernel-management API to every
    # other sandbox on kimi-agent-network.
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind to")
    parser.add_argument("--port", type=int, default=8888, help="Port to bind to")
    parser.add_argument("--reload", action="store_true", help="Enable auto-reload")
    parser.add_argument("--log-level", default="info", help="Log level")

    args = parser.parse_args()

    logger.info("启动 Jupyter Kernel Management Server...")
    logger.info(f"服务地址: http://{args.host}:{args.port}")
    logger.info(f"API 文档: http://{args.host}:{args.port}/docs")

    uvicorn.run(
        "kernel_server:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level=args.log_level,
        access_log=False,
    )
