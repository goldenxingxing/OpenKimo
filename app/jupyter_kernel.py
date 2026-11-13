import json
import os
import queue
from typing import List, Optional, Dict, Any
from pydantic import BaseModel
import time
import signal
import psutil

from jupyter_client.manager import KernelManager

#: How long a single iopub read blocks. Short so the loop can
#: re-check the caller's deadline between reads.
_IOPUB_POLL_SECONDS = 1.0

#: How long a timed-out cell is given to actually stop after an interrupt
#: before the kernel is restarted out from under it.
_INTERRUPT_GRACE_SECONDS = 2.0

#: Budget for the kernel's own init cell. Longer than a user cell's default:
#: it rebuilds the matplotlib font cache, which on a cold container takes well
#: past 30 seconds and used to come back as a silent failure.
_INIT_TIMEOUT_SECONDS = 180


class ExecutionResult(BaseModel):
    success: bool
    output: str
    error: Optional[str] = None
    images: Optional[List[str]] = None


class JupyterKernel:
    def __init__(self):
        self.km = None
        self.kc = None
        self.connection_file = None
        self._starting = False
        self._start_kernel()

    def _start_kernel(self):
        # _start_kernel() ends by running init_code through execute(), and
        # execute() calls _ensure_kernel_alive(), which calls _start_kernel()
        # again on any failure. With no guard that is unbounded mutual
        # recursion: a kernel that reliably fails its init — the font-cache
        # rebuild in init_code outrunning the timeout on a cold container, say
        # — respawned a kernel process per level and ended in RecursionError,
        # while kernel_server's lifespan never yielded and the container never
        # passed its healthcheck.
        if self._starting:
            raise RuntimeError("kernel start already in progress; refusing to recurse")
        self._starting = True
        try:
            self._start_kernel_locked()
        finally:
            self._starting = False

    def _start_kernel_locked(self):
        try:
            if self.km:
                self.shutdown()

            # Loopback, which is jupyter_client's own default. This used to
            # bind the container's routable bridge IP (and "0.0.0.0" whenever
            # the lookup raised), which puts all five kernel sockets on
            # kimo-agent-network: any other sandbox could read this kernel's
            # connection file — HMAC key included — off the unauthenticated
            # /kernel/connection endpoint and sign its own execute_request
            # into another user's session. jupyter_client documents the hazard
            # on the ip argument itself. Everything that legitimately talks to
            # this kernel (the worker, kernel_server) runs in this container.
            self.km = KernelManager()
            self.km.start_kernel()
            self.connection_file = self.km.connection_file
            self.kc = self.km.client()
            self.kc.start_channels()

            # 等待 kernel 完全准备好
            timeout = 30
            start_time = time.time()
            while True:
                if time.time() - start_time > timeout:
                    raise Exception("Timeout waiting for kernel to start")
                try:
                    self.kc.wait_for_ready(timeout=timeout)
                    break
                except Exception as e:
                    if "Timeout" not in str(e):
                        raise
                    continue

            # 初始化必要的包和配置
            init_code = """
import matplotlib
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm
import numpy as np
from IPython.display import display
%matplotlib inline

# 设置 matplotlib 中文字体支持
plt.style.use('default')
plt.rcParams['figure.figsize'] = [8, 6]
plt.rcParams['figure.dpi'] = 100
plt.rcParams['savefig.dpi'] = 100
plt.rcParams['font.size'] = 10
plt.rcParams['axes.grid'] = True
plt.rcParams['figure.facecolor'] = 'white'
plt.rcParams['axes.facecolor'] = 'white'
plt.rcParams['savefig.facecolor'] = 'white'
plt.rcParams['savefig.edgecolor'] = 'none'

# 配置中文字体
# 强制重新初始化字体管理器以识别新安装的字体
fm.fontManager.__init__()

# 查找可用的 CJK 字体
cjk_fonts = [f.name for f in fm.fontManager.ttflist if 'CJK' in f.name]
if cjk_fonts:
    # 优先使用简体中文字体，如果没有则使用日文字体（也支持中文）
    preferred_fonts = ['Noto Sans CJK SC', 'Noto Sans CJK TC', 'Noto Sans CJK JP']
    selected_font = None
    for font in preferred_fonts:
        if font in cjk_fonts:
            selected_font = font
            break

    if selected_font:
        plt.rcParams['font.family'] = selected_font
    else:
        # 使用找到的第一个 CJK 字体
        plt.rcParams['font.family'] = list(set(cjk_fonts))[0]
else:
    # 回退到默认配置
    plt.rcParams['font.family'] = 'sans-serif'
    plt.rcParams['font.sans-serif'] = ['Noto Sans CJK SC', 'Noto Sans CJK TC', 'DejaVu Sans']

plt.rcParams['axes.unicode_minus'] = False
"""
            # 看一眼结果。execute() 超时不抛异常，只是返回 success=False，
            # 而 init_code 里那句 fontManager.__init__() 是一次完整的字体缓存
            # 重建 —— 冷容器上经常越过 30 秒的默认超时。丢掉这个返回值，
            # lifespan 照样打印「初始化完成」、/health 照样报 kernel_alive，
            # 而内核要么没有 matplotlib/中文字体配置，要么队首还卡着那条
            # 没跑完的 init cell。
            init_result = self.execute(init_code, timeout=_INIT_TIMEOUT_SECONDS)
            if not init_result.success:
                print(f"Kernel initialization did not complete: {init_result.error}")
        except Exception as e:
            print(f"Kernel initialization error: {str(e)}")
            self.shutdown()
            raise

    def _ensure_kernel_alive(self):
        """确保 kernel 是活跃的，如果不是则重启"""
        try:
            if not self.kc or not self.km:
                raise Exception("Kernel not initialized")

            # 检查 kernel 是否还在运行
            if not self.km.is_alive():
                raise Exception("Kernel is not alive")

            # 检查 kernel 是否响应。
            # 这里原本调 self.kc.kernel_info()，但那是 send-only 的
            # (jupyter_client 8.10 KernelClient.kernel_info: 发一条
            # kernel_info_request 就返回 msg_id，不等回复)，所以它几乎不会抛，
            # 这个「响应性检查」从来没生效过，只是每次调用都往队列里堆一条没人
            # 读的回复。心跳通道才是真正的存活信号。
            hb = getattr(self.kc, "hb_channel", None)
            if hb is not None and not hb.is_beating():
                raise Exception("Kernel is not responding (heartbeat stopped)")

        except Exception as e:
            print(f"Kernel check failed: {str(e)}")
            self._start_kernel()

    def _wait_for_idle(self, msg_id: str, grace: float) -> bool:
        """True when this execution's kernel status reaches idle within `grace`.

        An interrupt request only reports that it was *sent*: jupyter_client's
        ``KernelClient.interrupt()`` returns as soon as the message is on the
        control channel, and a kernel wedged in native code never processes it.
        The kernel says the cell is actually done by publishing
        ``status: idle`` for the request that was running — that is the only
        answer worth acting on.
        """
        deadline = time.time() + grace
        while time.time() < deadline:
            if self.km is None or not self.km.is_alive():
                # 内核都没了，谁也不会再发 idle 出来。等满 grace 只是把重启
                # 往后推了两秒。
                return False
            try:
                msg = self.kc.get_iopub_msg(timeout=_IOPUB_POLL_SECONDS)
            except queue.Empty:
                continue
            except Exception:
                # 读通道抖一下不算「没停下来」。这里返回 False 的唯一后果是
                # 上层重启内核、清掉用户的全部变量 —— 那是给「真的等不到
                # idle」准备的，不是给一次读失败准备的。继续等到期限为止。
                time.sleep(0.05)
                continue
            try:
                if msg.get("parent_header", {}).get("msg_id") != msg_id:
                    continue
                if (
                    msg["header"]["msg_type"] == "status"
                    and msg["content"].get("execution_state") == "idle"
                ):
                    return True
            except Exception:
                continue
        return False

    def execute(self, code: str, timeout: int = 30) -> ExecutionResult:
        try:
            # 确保 kernel 是活跃的
            self._ensure_kernel_alive()

            # 执行用户代码
            if not self.kc:
                raise Exception("Kernel not initialized")

            msg_id = self.kc.execute(code)

            # 等待执行结果
            output = []
            error = None
            images = []
            start_time = time.time()

            while True:
                if time.time() - start_time > timeout:
                    # 超时不能只是返回：cell 还在同一个共享 kernel 里跑，
                    # 继续吃 CPU、继续往 iopub 里灌消息，而下一次 execute()
                    # 会排在它后面 —— 一个死循环就能让之后每一次执行都超时。
                    interrupted = self.interrupt_kernel()
                    recovery = ""
                    # 以内核自己的 status=idle 为准，而不是中断请求「发出去
                    # 了」。发出去不等于收到了：卡在原生代码里的内核根本轮不到
                    # Python 层处理信号。等不到 idle 就重启 —— 那个 cell 还排在
                    # 队首，之后每一次 execute() 都会撞上它。这也是唯一值得用
                    # 「清空用户状态」换回一个可用内核的时候。
                    if not self._wait_for_idle(msg_id, _INTERRUPT_GRACE_SECONDS):
                        print(
                            "Interrupt did not stop the cell "
                            f"({interrupted.get('message')}); restarting the kernel"
                        )
                        try:
                            self._start_kernel()
                        except Exception as restart_error:
                            # 说出来。否则调用方收到的是一条普通的超时，而内核
                            # 其实还卡着上一个 cell，之后每一次执行都会撞上它。
                            recovery = (
                                f"; the kernel could not be interrupted or "
                                f"restarted ({restart_error})"
                            )
                            print(f"Failed to restart kernel: {restart_error}")
                    return ExecutionResult(
                        success=False,
                        output="",
                        error=(
                            f"Executing code timed out, timeout: {timeout} "
                            f"seconds{recovery}"
                        ),
                        images=[],
                    )

                try:
                    # Poll in short slices. The read used to block for
                    # `timeout + 1`, i.e. past the deadline checked above, so
                    # the clean timeout return was unreachable: the read
                    # outlived the budget and the loop only ever left through
                    # the exception path.
                    msg = self.kc.get_iopub_msg(timeout=_IOPUB_POLL_SECONDS)
                except queue.Empty:
                    # A quiet interval, not a failure. The old test was
                    # `str(e) == "Timeout waiting for message"`, which can never
                    # be true — get_iopub_msg raises queue.Empty, whose str()
                    # is "" — so every quiet interval was re-raised, the outer
                    # handler restarted the kernel, and the user's state was
                    # wiped mid-execution.
                    continue

                try:
                    msg_type = msg["header"]["msg_type"]

                    # Only this execution's output. Nothing filtered by parent
                    # before, so leftover messages from an abandoned earlier
                    # run — a cell that timed out and kept printing — were
                    # attributed to whatever ran next.
                    if msg.get("parent_header", {}).get("msg_id") != msg_id:
                        continue

                    if msg_type == "stream":
                        # 直接添加 stream 输出，不需要清理
                        output.append(msg["content"]["text"])
                    elif msg_type == "error":
                        error = "\n".join(msg["content"]["traceback"])
                    elif msg_type == "execute_result":
                        if isinstance(msg["content"]["data"], dict):
                            if "text/plain" in msg["content"]["data"]:
                                text = msg["content"]["data"]["text/plain"]
                                # # 去除第一个行号字符
                                # if not removed_prefix:
                                #     text = text[1:]
                                #     removed_prefix = True
                                output.append(text)
                            if "image/png" in msg["content"]["data"]:
                                images.append(msg["content"]["data"]["image/png"])
                    elif msg_type == "display_data":
                        if isinstance(msg["content"]["data"], dict):
                            if "image/png" in msg["content"]["data"]:
                                images.append(msg["content"]["data"]["image/png"])
                            elif "text/plain" in msg["content"]["data"]:
                                text = msg["content"]["data"]["text/plain"]
                                output.append(text)

                    if (
                        msg_type == "status"
                        and msg["content"]["execution_state"] == "idle"
                    ):
                        break
                except (KeyError, TypeError):
                    # A message shaped unlike anything handled above is not a
                    # reason to tear down the kernel; skip it.
                    continue

            # 合并输出，保持换行符
            final_output = "".join(output).strip()
            return ExecutionResult(
                success=error is None,  # 如果有错误，则 success 为 False
                output=final_output,
                error=error,
                images=images,
            )
        except Exception as e:
            import traceback

            traceback.print_exc()
            print(f"Execution error: {e.__class__.__name__} {str(e)}")
            # 只有内核真的死了才重启。重启会清空用户的全部变量、导入和图，
            # 而落到这里的异常大多是客户端侧的（一次通道读取抖动、一条没见过
            # 的消息类型），为这些把会话推倒重来，代价远大于那个错误本身。
            try:
                if self.km is None or not self.km.is_alive():
                    self._start_kernel()
            except Exception as restart_error:
                print(f"Failed to restart kernel: {str(restart_error)}")
            raw_error = f"{e.__class__.__name__}: {str(e)}"
            if "empty" in raw_error.lower():
                return ExecutionResult(
                    success=False,
                    output="",
                    error=f"Executing code timed out, timeout: {timeout} seconds",
                    images=[],
                )
            else:
                return ExecutionResult(
                    success=False,
                    output="",
                    error=f"{e.__class__.__name__}: {str(e)}",
                    images=[],
                )

    def reset_kernel(self) -> Dict[str, Any]:
        """重置 kernel"""
        try:
            print("Resetting kernel...")
            old_connection_file = self.connection_file
            self.shutdown()
            self._start_kernel()
            return {
                "success": True,
                "message": "Kernel reset successfully",
                "old_connection_file": old_connection_file,
                "new_connection_file": self.connection_file,
            }
        except Exception as e:
            return {"success": False, "message": f"Failed to reset kernel: {str(e)}"}

    def interrupt_kernel(self) -> Dict[str, Any]:
        """中断 kernel 执行"""
        try:
            if not self.km:
                return {"success": False, "message": "Kernel not initialized"}

            # 获取 kernel 进程 ID
            kernel_id = self._get_kernel_pid()

            # 中断只发一次。SIGINT 和控制通道的 interrupt 各自都会在内核里
            # 产生一个 KeyboardInterrupt，两个背对背发出去，第二个往往落在
            # 第一个的异常处理中间 —— 表现为一条莫名其妙的二次异常，或者内核
            # 停在一个说不清的状态里。控制通道是官方 API，也不需要 PID，
            # 所以先走它，拿不到再退回信号。
            #
            # 失败要报出去：`except (AttributeError, Exception)` 等价于
            # `except Exception`，而且把失败直接丢掉 —— 控制通道已死时这里
            # 静默失败，接口却仍然回 "Kernel interrupted successfully"，
            # 调用方以为正在运行的 cell 已被中断。
            # interrupt() 抛异常并不能证明「一条都没发出去」：控制通道上可能
            # 已经躺着一个 interrupt_request 了。这里仍然退回信号 —— 中断彻底
            # 没送到（内核继续跑那个 cell）比理论上多送一次要糟得多，而多送的
            # 那次最坏也就是一条多余的 KeyboardInterrupt。
            client_error: str | None = None
            interrupted = False
            try:
                if self.kc and hasattr(self.kc, "interrupt"):
                    self.kc.interrupt()  # type: ignore
                    interrupted = True
            except Exception as exc:
                client_error = str(exc)
                print(f"Error interrupting via jupyter client: {client_error}")

            signal_error: str | None = None
            if not interrupted and kernel_id:
                # 发送 SIGINT 信号中断 kernel
                try:
                    process = psutil.Process(kernel_id)
                    process.send_signal(signal.SIGINT)
                    print(f"Sent SIGINT to kernel process {kernel_id}")
                    interrupted = True
                except psutil.NoSuchProcess:
                    signal_error = f"Kernel process {kernel_id} not found"
                    print(signal_error)
                except Exception as e:
                    signal_error = str(e)
                    print(f"Error sending signal to kernel process: {signal_error}")

            if not interrupted:
                # 两条路都没走通：控制通道用不了，也没有能发信号的 PID。
                reason = client_error or signal_error or "no interrupt route available"
                return {
                    "success": False,
                    "message": f"Failed to interrupt kernel: {reason}",
                    "kernel_pid": kernel_id,
                }
            return {
                "success": True,
                "message": "Kernel interrupted successfully",
                "kernel_pid": kernel_id,
            }
        except Exception as e:
            return {
                "success": False,
                "message": f"Failed to interrupt kernel: {str(e)}",
            }

    def get_connection_info(self) -> Dict[str, Any]:
        """获取 kernel 连接信息"""
        try:
            if not self.km or not self.connection_file:
                return {"success": False, "message": "Kernel not initialized"}

            # 读取连接文件内容
            connection_info = {}
            if os.path.exists(self.connection_file):
                with open(self.connection_file, "r") as f:
                    connection_info = json.load(f)

            return {
                "success": True,
                "connection_file": self.connection_file,
                "connection_info": connection_info,
                "kernel_alive": self.km.is_alive() if self.km else False,
                "kernel_pid": self._get_kernel_pid(),
            }
        except Exception as e:
            return {
                "success": False,
                "message": f"Failed to get connection info: {str(e)}",
            }

    def debug_kernel_manager(self) -> Dict[str, Any]:
        """调试 KernelManager 的状态和属性"""
        debug_info = {
            "km_exists": self.km is not None,
            "km_type": str(type(self.km)) if self.km else None,
            "km_alive": self.km.is_alive() if self.km else False,
            "attributes": {},
            "methods_tried": {},
        }

        if self.km:
            # 检查各种属性
            for attr in ["kernel", "kernel_id", "connection_file", "provisioner"]:
                if hasattr(self.km, attr):
                    try:
                        value = getattr(self.km, attr)
                        debug_info["attributes"][attr] = {
                            "exists": True,
                            "value": str(value),
                            "type": str(type(value)),
                            "is_none": value is None,
                        }

                        # 如果是 provisioner，进一步检查
                        if attr == "provisioner" and value:
                            debug_info["attributes"]["provisioner"]["process"] = {
                                "exists": hasattr(value, "process"),
                                "value": str(getattr(value, "process", None)),
                                "type": str(type(getattr(value, "process", None))),
                            }
                            if hasattr(value, "process") and value.process:
                                debug_info["attributes"]["provisioner"]["process"][
                                    "pid"
                                ] = {
                                    "exists": hasattr(value.process, "pid"),
                                    "value": getattr(value.process, "pid", None),
                                }

                        # 如果是 kernel，进一步检查
                        if attr == "kernel" and value:
                            debug_info["attributes"]["kernel"]["pid"] = {
                                "exists": hasattr(value, "pid"),
                                "value": getattr(value, "pid", None),
                            }

                    except Exception as e:
                        debug_info["attributes"][attr] = {
                            "exists": True,
                            "error": str(e),
                        }
                else:
                    debug_info["attributes"][attr] = {"exists": False}

        return debug_info

    def _get_kernel_pid(self) -> Optional[int]:
        """安全地获取 kernel 进程 ID"""
        if not self.km:
            return None

        try:
            # 方法1: 通过 provisioner (jupyter-client >= 7.0)
            if hasattr(self.km, "provisioner"):
                provisioner = getattr(self.km, "provisioner", None)
                if provisioner and hasattr(provisioner, "process"):
                    process = getattr(provisioner, "process", None)
                    if process and hasattr(process, "pid"):
                        return getattr(process, "pid", None)

            # 方法2: 直接从 kernel 属性 (旧版本)
            if hasattr(self.km, "kernel"):
                kernel = getattr(self.km, "kernel", None)
                if kernel and hasattr(kernel, "pid"):
                    return getattr(kernel, "pid", None)

            # 方法3: 通过 kernel_id 和系统进程查找
            if hasattr(self.km, "kernel_id") and self.km.kernel_id:
                import psutil

                # 查找包含 kernel_id 的 python 进程
                for proc in psutil.process_iter(["pid", "name", "cmdline"]):
                    try:
                        if proc.info["name"] and "python" in proc.info["name"].lower():
                            cmdline = proc.info["cmdline"] or []
                            for arg in cmdline:
                                if self.km.kernel_id in str(arg):
                                    return proc.info["pid"]
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        continue

            # 方法4: 通过连接文件查找相关进程
            if hasattr(self.km, "connection_file") and self.km.connection_file:
                import os
                import psutil

                connection_file = os.path.basename(self.km.connection_file)
                kernel_name = connection_file.replace("kernel-", "").replace(
                    ".json", ""
                )

                for proc in psutil.process_iter(["pid", "name", "cmdline"]):
                    try:
                        if proc.info["name"] and "python" in proc.info["name"].lower():
                            cmdline = proc.info["cmdline"] or []
                            for arg in cmdline:
                                if kernel_name in str(arg) or connection_file in str(
                                    arg
                                ):
                                    return proc.info["pid"]
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        continue

        except Exception as e:
            print(f"Error getting kernel PID: {str(e)}")

        return None

    def get_kernel_status(self) -> Dict[str, Any]:
        """获取 kernel 状态信息"""
        try:
            status = {
                "kernel_alive": False,
                "kernel_pid": None,
                "connection_file": self.connection_file,
                "client_connected": False,
            }

            if self.km:
                status["kernel_alive"] = self.km.is_alive()
                status["kernel_pid"] = self._get_kernel_pid()

            if self.kc:
                try:
                    # 心跳，而不是 kernel_info()：后者是 send-only 的，永远不
                    # 抛，所以 client_connected 恒为 True —— 一个死 kernel 也
                    # 报「已连接」，而且每次查询都堆一条没人读的回复。
                    hb = getattr(self.kc, "hb_channel", None)
                    status["client_connected"] = bool(hb is not None and hb.is_beating())
                except Exception:
                    # Not a bare `except:` — that also swallowed
                    # KeyboardInterrupt and SystemExit, so a SIGINT delivered
                    # while this ran was absorbed and the shutdown lost.
                    status["client_connected"] = False

            return {"success": True, **status}
        except Exception as e:
            return {
                "success": False,
                "message": f"Failed to get kernel status: {str(e)}",
            }

    def shutdown(self):
        """安全地关闭 kernel"""
        try:
            if self.kc:
                self.kc.stop_channels()
        except Exception as e:
            print(f"Error stopping channels: {str(e)}")
        try:
            if self.km:
                self.km.shutdown_kernel()
        except Exception as e:
            print(f"Error shutting down kernel: {str(e)}")
        finally:
            self.kc = None
            self.km = None
            self.connection_file = None


# Deliberately no module-level instance here. `kernel = JupyterKernel()` used
# to sit at this line: importing this module spawned a kernel subprocess and
# ran init_code (matplotlib's font-cache rebuild), and nothing ever read the
# name. kernel_server.py's `from jupyter_kernel import JupyterKernel` executed
# it, so every sandbox carried two kernels for its whole life and shut down
# one — and an import could hang or raise for reasons unrelated to the server.
