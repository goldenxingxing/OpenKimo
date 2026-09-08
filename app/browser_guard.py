import asyncio
import re
import sys
import time
import json
import subprocess
import requests
import websockets
# Explicitly, not via `websockets.exceptions` off the bare package. websockets
# 15 resolves submodules lazily and `import websockets` alone leaves
# `websockets.exceptions` unresolvable — verified on 15.0.1:
#     >>> import websockets; websockets.exceptions
#     AttributeError: module 'websockets' has no attribute 'exceptions'
# It happens to work once websockets.connect() has pulled the submodule in,
# but the `except websockets.exceptions.ConnectionClosed:` clauses below are
# evaluated *while handling an exception*, so on any path that fails before a
# connect the AttributeError would replace the error being handled.
# kimi-cli pins only `websockets>=14.0`, so the resolved version varies.
import websockets.exceptions
from typing import Dict, Optional, List
from loguru import logger
from playwright.async_api import async_playwright, Page, BrowserContext
import os
from utils import get_screensize
from shutil import which


def _get_chromium_version(executable_path: str = "/usr/bin/chromium") -> Optional[str]:
    try:
        exe = executable_path if os.path.exists(executable_path) else (which("chromium") or which("chromium-browser") or executable_path)
        result = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=3)
        text = (result.stdout or result.stderr or "").strip()
        # e.g. Chromium 120.0.6099.109
        m = re.search(r"(Chromium|Google Chrome)\s+([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+)", text)
        return m.group(2) if m else None
    except Exception:
        return None


def _chrome_major(version: Optional[str]) -> str:
    if not version:
        return "120"
    return version.split(".")[0]


def _build_user_agent(version: Optional[str], locale: str) -> str:
    # Example: Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.6099.109 Safari/537.36
    chrome_ver = version or "120.0.6099.109"
    return (
        f"Mozilla/5.0 (X11; Linux x86_64; {locale}) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        f"Chrome/{chrome_ver} Safari/537.36"
    )


def _build_ua_ch_headers(version: Optional[str]) -> Dict[str, str]:
    major = _chrome_major(version)
    return {
        "Sec-CH-UA": f'"Not A(Brand)";v="99", "Chromium";v="{major}", "Google Chrome";v="{major}"',
        "Sec-CH-UA-Platform": '"Linux"',
        "Sec-CH-UA-Mobile": "?0",
    }
logger.remove()
logger.add(sys.stderr, level="INFO")

init_url = os.getenv("CHROME_INIT_URL", "chrome://newtab/")
resolution_pattern = re.compile(r"^(\d+)x(\d+)$")
SCREEN_RESOLUTION = os.getenv("SCREEN_RESOLUTION", "1920x1080")
match = resolution_pattern.match(SCREEN_RESOLUTION)
if not match:
    logger.warning(
        f"Invalid screen resolution: {SCREEN_RESOLUTION}, using default 1920x1080"
    )
    SCREEN_RESOLUTION = "1920x1080"

SCREEN_WIDTH = int(match.group(1) if match else "1920")
SCREEN_HEIGHT = int(match.group(2) if match else "1080")


class BrowserGuard:
    """
    主要功能是在 Chromium 窗口被关闭后自动打开新标签页。
    """

    def __init__(self, check_interval: float = 1.0):
        self.running: bool = False
        self.check_interval: float = check_interval
        self.browser: Optional[BrowserContext] = None
        self.pages: List[Page] = []
        self.current_page_index: int = 0
        self.width: int = int(match.group(1) if match else "1920")
        self.height: int = int(match.group(2) if match else "1080")
        logger.info(
            f"BrowserGuard initialized with width: {self.width}, height: {self.height}"
        )

    async def start(self):
        try:
            logger.info("Starting browser initialization...")
            # Kept on the instance and stopped in shutdown(). It used to be a
            # local, so each restart left its Node driver process behind:
            # _monitor_loop() restarts on every exception, once per
            # check_interval, so a persistent fault leaked one driver per
            # second until the container hit SANDBOX_PID_LIMIT.
            await self._stop_playwright()
            playwright = await async_playwright().start()
            self._playwright = playwright
            logger.info("Playwright started, launching browser...")

            # Build environment-aware identity
            locale = os.getenv("CHROME_LOCALE", "zh-CN")
            tz = os.getenv("TZ", "Asia/Shanghai")
            chromium_version = _get_chromium_version()
            user_agent = _build_user_agent(chromium_version, locale)
            ua_ch = _build_ua_ch_headers(chromium_version)

            extra_flags = os.getenv("CHROME_FLAGS", "")
            extra_args: List[str] = []
            if extra_flags:
                extra_args = extra_flags.split(" ")

            # Use non-headless mode for testing with slower timeouts
            launch_options = {
                "user_data_dir": "/app/data/chrome_data",
                "viewport": {"width": self.width, "height": self.height},
                "headless": False,
                "timeout": 60000.0,
                "user_agent": user_agent,
                "locale": locale,
                "timezone_id": tz,
                "extra_http_headers": {
                    "Accept-Language": f"{locale},en;q=0.8",
                    **ua_ch,
                },
                "args": [
                    "--window-position=0,0",
                    "--remote-debugging-port=9222",
                    "--single-process",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--disable-background-networking",
                    "--disable-background-timer-throttling",
                    "--disable-backgrounding-occluded-windows",
                    "--disable-breakpad",
                    "--disable-component-extensions-with-background-pages",
                    "--disable-dev-shm-usage",
                    "--disable-features=TranslateUI",
                    "--disable-ipc-flooding-protection",
                    "--disable-renderer-backgrounding",
                    "--enable-features=NetworkServiceInProcess2",
                    "--force-color-profile=srgb",
                    "--metrics-recording-only",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-infobars",
                    "--mute-audio",
                    "--enable-logging",
                    "--log-file=/app/logs/chromium.log",
                    "--no-sandbox",
                    "--disable-gpu",
                    # "--disable-extensions",
                    "--load-extension=/app/pdf-viewer",
                    # 同 CDP 路径：argv 条目不能自带引号，否则 V8 拒绝该 flag。
                    "--js-flags=--max_old_space_size=1024",
                    *extra_args,
                ],
                "ignore_default_args": [
                    "--disable-extensions",
                ],
            }

            count = 0
            while count < 3:
                try:
                    self.browser = await playwright.chromium.launch_persistent_context(
                        executable_path="/usr/bin/chromium", **launch_options
                    )

                    logger.info("Browser launched successfully")
                    width, _ = get_screensize()
                    # 放到线程里，并且带超时。xdotool 阻塞在 X socket 上，而
                    # 「Xvfb 卡住但没死」正是这个守护进程要扛的情况 —— 这里是
                    # 协程：不带超时等于把事件循环永久按死，带了超时也还会按住
                    # 5 秒。摆一下鼠标不值得让整个循环停下来等。
                    await asyncio.to_thread(
                        _run_xdotool, ["mousemove", str(width - 25), "115", "click", "1"]
                    )
                    # await self.browser.pages[0].reload()
                    await self.browser.pages[0].goto(init_url)
                    break
                except Exception as browser_error:
                    logger.info(f"Failed to launch browser: {browser_error}")
                    # 关掉这一轮可能已经起来的那个 context。goto() 的超时也落在
                    # 这个 except 里：浏览器其实已经跑起来了，只是首页慢。不关的
                    # 话它继续攥着 chrome_data 的 profile 锁，后面两次重试必然
                    # 因为锁失败，最后抛「重试 3 次仍失败」—— 而屏幕上明明有一个
                    # 能用的浏览器。
                    if self.browser is not None:
                        try:
                            await self.browser.close()
                        except Exception as close_error:
                            logger.debug(f"Failed to close browser context: {close_error}")
                        self.browser = None
                    count += 1

            if count == 3:
                raise RuntimeError("Failed to launch browser, retried 3 times")

            logger.info("Browser initialization completed successfully")
        except Exception as e:
            logger.info(f"Browser startup error: {str(e)}")
            raise RuntimeError(f"Browser initialization failed: {str(e)}")

    async def _stop_playwright(self):
        """Stop the Playwright driver, if one is running."""
        playwright = getattr(self, "_playwright", None)
        if playwright is None:
            return
        self._playwright = None
        try:
            await playwright.stop()
        except Exception as e:  # noqa: BLE001 - teardown must not mask the caller
            logger.error(f"Playwright stop error: {e}")

    async def shutdown(self):
        """Clean up browser instance on shutdown"""
        try:
            if self.browser:
                await self.browser.close()
                self.browser = None
                self.pages = []
                self.current_page_index = 0
        except Exception as e:
            logger.error(f"Shutdown error: {str(e)}")
        finally:
            await self._stop_playwright()

    async def _monitor_loop(self):
        """监控循环"""
        while self.running:
            try:
                # 检查标签页
                logger.debug(f"浏览器状态: {self.browser}")
                if not self.browser:
                    logger.info("浏览器未启动，重新启动浏览器...")
                    await self.shutdown()
                    await self.start()
                    await asyncio.sleep(self.check_interval)
                    continue
                if not self.browser.pages:
                    logger.info("浏览器没有打开标签页，重新启动浏览器...")
                    await self.shutdown()
                    await self.start()

                logger.debug(f"浏览器标签页: {self.browser.pages}")

            except Exception as e:
                # A failed recovery must not end the loop. start() raises
                # RuntimeError when all three launch attempts fail, and that
                # propagated straight out of _monitor_loop: the guard stopped
                # supervising and the process kept running with no browser and
                # no monitor, exiting 0 whenever the caller finished.
                logger.error(f"监控循环出错: {e}")
                try:
                    await self.shutdown()
                    await self.start()
                except Exception as restart_error:
                    logger.error(
                        f"重启浏览器失败，将在 {self.check_interval}s 后重试: "
                        f"{restart_error}"
                    )

            await asyncio.sleep(self.check_interval)

    async def start_monitoring(self):
        """开始监控"""
        if self.running:
            return

        self.running = True

        try:
            # 运行监控循环
            await self._monitor_loop()
        except Exception as e:
            logger.error(f"监控循环出错: {e}")
        finally:
            await self.shutdown()

    async def stop_async(self):
        """异步停止监控和浏览器"""
        self.running = False

        logger.info("监控已停止")

    async def stop(self):
        """同步停止监控和浏览器"""
        self.running = False

        try:
            await self.stop_async()
        except Exception as e:
            logger.error(f"停止时出错: {e}")

    async def set_screen_resolution(self, width: int, height: int):
        """设置屏幕分辨率"""
        if self.browser:
            await self.browser.pages[0].set_viewport_size(
                {"width": width, "height": height}
            )


#: xdotool 单次调用的上限。它阻塞在 X socket 上，而这些调用点都在协程里。
_XDOTOOL_TIMEOUT_SECONDS = 5


def _run_xdotool(args: list[str]) -> None:
    """跑一条 xdotool 命令，失败或超时都只记一笔，不往上抛。

    这些调用是「摆一下鼠标」这种锦上添花的动作，值不上让浏览器启动失败；
    但没有超时的话，一个卡住的 X server 会把调用它的事件循环一起按死。
    """
    try:
        subprocess.run(
            ["xdotool", *args],
            check=False,
            timeout=_XDOTOOL_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        logger.warning("xdotool %s 超时（X server 可能卡住了）", " ".join(args))
    except OSError as exc:
        logger.warning("xdotool 不可用: %s", exc)


def _ws_is_closed(ws) -> bool:
    """True when a websocket connection is no longer usable.

    websockets has moved this signal around between versions: `.closed` on the
    legacy client protocol, `.close_code`/`.state` on the newer one. Probe in
    order and treat "cannot tell" as open, so a version bump degrades to the
    old behaviour rather than reconnecting on every call.
    """
    closed = getattr(ws, "closed", None)
    if isinstance(closed, bool):
        return closed
    if getattr(ws, "close_code", None) is not None:
        return True
    state = getattr(ws, "state", None)
    name = getattr(state, "name", None)
    if isinstance(name, str):
        return name in ("CLOSED", "CLOSING")
    return False


class BrowserCDPGuard:
    """
    Chromium 监控器，使用 CDP (Chrome DevTools Protocol) 直接控制浏览器。

    主要功能是在 Chromium 窗口被关闭或被最小化后自动打开新标签页或最大化窗口。
    """

    def __init__(self, check_interval: int = 1, executable_path: Optional[str] = None):
        self.running = False
        self.check_interval = check_interval
        self.debugging_port = None
        self.browser_process = None
        self.cdp_url = "http://localhost:9222"
        self.ws_connections = {}  # 存储每个标签页的 WebSocket 连接
        self._cdp_request_id = 0  # 单调递增，见 _send_cdp_command
        self.executable_path = executable_path or "/usr/bin/chromium"

    async def _send_cdp_command(
        self,
        ws,
        command: str,
        params: Optional[Dict] = None,
        timeout: float = 5.0,
    ):
        """发送 CDP 命令。

        每条命令用一个自增 id。原来固定写 ``"id": 1``：一条命令超时后它的回复
        并没有从 socket 里取走，而连接仍被缓存复用，于是那条迟到的回复会被
        当成**下一条**命令的结果 —— ``getWindowForTarget`` 的回复被当作
        ``getWindowBounds`` 的返回，于是 bounds 为空、窗口状态读成 ""，或者在
        ``set_screen_resolution`` 里拿到错的 windowId。id 唯一之后，迟到的回复
        走下面「未匹配」那条分支被跳过。
        """
        if params is None:
            params = {}

        self._cdp_request_id += 1
        message = {"id": self._cdp_request_id, "method": command, "params": params}

        try:
            data = json.dumps(message)
            await ws.send(data)

            # 使用 asyncio.wait_for 来实现超时
            async def wait_for_response():
                while True:
                    response = await ws.recv()
                    response_obj = json.loads(response)

                    # 如果是事件消息，跳过继续等待
                    if "method" in response_obj:
                        logger.debug(f"收到事件消息: {response_obj['method']}")
                        continue

                    # 检查是否是我们发送的命令的响应
                    if "id" in response_obj and response_obj["id"] == message["id"]:
                        if "error" in response_obj:
                            logger.error(f"CDP 命令错误: {response_obj['error']}")
                            return None
                        return response_obj.get("result", {})

                    # 如果不是我们的响应（比如上一条超时命令迟到的回复），
                    # 跳过继续等待。
                    logger.debug(f"跳过未匹配的响应: id={response_obj.get('id')}")

            return await asyncio.wait_for(wait_for_response(), timeout)

        except asyncio.TimeoutError:
            logger.error(f"CDP 命令超时: {command} (id={message['id']})")
            return None
        except websockets.exceptions.ConnectionClosed:
            # 交给调用方，让它把这条死连接从 ws_connections 里剔除。原来这里
            # 和其它异常一起被吞成 None，所以调用方那个
            # `except ConnectionClosed` 永远到不了，一条已关闭的缓存连接会被
            # 反复复用，窗口管理从此静默失效。
            logger.warning(f"CDP 连接已关闭: {command}")
            raise
        except Exception as e:
            logger.error(f"CDP 命令执行失败: {e}")
            return None

    async def start(
        self,
        headless: bool = False,
        debugging_port: int = 9222,
        retry_count: int = 6,
    ):
        """启动 Chromium 浏览器进程"""
        try:
            self.debugging_port = debugging_port
            self.cdp_url = f"http://localhost:{debugging_port}"
            url = os.getenv("CHROME_INIT_URL", "chrome://newtab/")
            # 构建启动参数
            chrome_args = [
                self.executable_path,
                url,
                f"--remote-debugging-port={debugging_port}",
                "--remote-debugging-address=0.0.0.0",
                "--window-position=0,0",
                f"--window-size={SCREEN_WIDTH},{SCREEN_HEIGHT}",
                "--no-first-run",
                "--no-default-browser-check",
                "--start-maximized",
                "--no-sandbox",
                "--disable-dbus",
                "--disable-gpu",
                "--disable-software-rasterizer",
                "--enable-logging=file",
                "--log-file=/tmp/chromium_detailed.log",
                "--disable-infobars",
                "--disable-blink-features=AutomationControlled",
                "--user-data-dir=/tmp/chromium_user_data",
                "--allow-file-access-from-files",  # This is a dangerous flag, use it at your own risk
                "--load-extension=/app/pdf-viewer",
                # 不能带引号：这是 argv 条目而不是 shell 词，带上双引号后 V8
                # 收到的是 '"--max_old_space_size=512"' 并直接拒绝，内存上限
                # 从未生效 —— 失控页面会一路涨到容器的 SANDBOX_MEMORY_LIMIT，
                # 被 OOM 杀掉的是整个 sandbox 而不只是渲染进程。
                "--js-flags=--max_old_space_size=512",
            ]
            chrome_flags = os.getenv("CHROME_FLAGS", "")
            extra_args: List[str] = []
            if chrome_flags:
                extra_args = chrome_flags.split(" ")

            chrome_args.extend(extra_args)

            if headless:
                chrome_args.append("--headless")

            count = 0
            while count < retry_count:
                # 启动浏览器进程。
                # 输出丢给 /dev/null 而不是 PIPE：这两个管道从来没人读，
                # Chromium 用 GPU/DBus/sandbox 的警告填满 ~64KiB 缓冲区后就会
                # 阻塞在 write() 上，浏览器卡在启动中途，而存活检查一直报失败。
                # (--enable-logging=file 只重定向它自己的日志，不含这些。)
                self.browser_process = subprocess.Popen(
                    chrome_args,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )

                # 等待浏览器启动。
                # await，不是 time.sleep：start() 是协程，而监控循环出错重启时
                # 就是从事件循环里调用它的 —— 阻塞式 sleep 会把整个循环停住几
                # 秒，同一个循环上的 set_screen_resolution 之类全部跟着卡死。
                await asyncio.sleep(0.5 * ((1.2) ** (count + 1)))

                # 检查浏览器是否成功启动
                if not self._is_browser_running():
                    count += 1
                    self.browser_process.kill()
                    # wait() 之后进程才真正被回收；不 wait 的话每次重试和每次
                    # 监控循环重启都留下一个僵尸，累积到 SANDBOX_PID_LIMIT。
                    # 放到线程里等：这是协程，直接 wait() 会把事件循环连同上面
                    # 那些 await 一起按住最多 5 秒。
                    try:
                        await asyncio.to_thread(self.browser_process.wait, 5)
                    except Exception:
                        pass
                    self.browser_process = None
                    logger.error(f"浏览器启动失败，尝试第 {count} 次")
                    await asyncio.sleep(0.1 * (2**count))
                    continue
                break

            if count >= retry_count:
                # 原来这里直接落到下面 return True：所有尝试都失败之后仍然
                # 记录「Chromium 已启动并连接」并返回成功，调用方接着进入
                # start_monitoring()，而根本没有浏览器 —— 监控循环于是每一轮
                # 都抛异常并重启，无限刷 Chromium 进程。
                # BrowserGuard.start() 在同样的位置是有这个检查的。
                logger.error(f"Chromium 启动失败，已重试 {retry_count} 次")
                await self.stop_async()
                return False

            width, _ = get_screensize()
            await asyncio.to_thread(
                _run_xdotool, ["mousemove", str(width - 25), "115", "click", "1"]
            )

            logger.info("Chromium 已启动并连接")
            return True

        except Exception as e:
            logger.error(f"启动 Chromium 失败: {e}")
            await self.stop_async()
            return False

    async def connect_to_cdp(self, url: str):
        """连接到 CDP"""
        self.cdp_url = url

    def _is_browser_running(self) -> bool:
        """检查浏览器是否在运行"""
        try:
            response = requests.get(self.cdp_url + "/json/version", timeout=5)
            return response.status_code == 200
        except Exception as e:
            logger.error(f"检查浏览器是否在运行失败: {e}")
            return False

    async def get_cdp_tabs(self):
        """获取所有标签页"""
        try:
            response = requests.get(f"{self.cdp_url}/json/list", timeout=5)
            if response.status_code != 200:
                raise ValueError(f"获取标签页失败，状态码: {response.status_code}")

            tabs = response.json()
            # 过滤出 type 为 "page" 的标签页，这些是正常的网页标签页，而不是其他类型的标签页（如 DevTools 等）
            return [tab for tab in tabs if tab.get("type") == "page"]
        except Exception as e:
            raise ValueError(f"获取标签页失败: {e}")

    async def open_new_tab(self, url: str = "chrome://newtab/"):
        """打开新标签页"""
        try:
            # 使用 PUT 方法创建新标签页
            response = requests.put(
                f"{self.cdp_url}/json/new", json={"url": url}, timeout=5
            )
            if response.status_code != 200:
                logger.error(response.text)
                raise Exception(f"打开新标签页失败，状态码: {response.status_code}")

            # 获取新标签页信息
            tab_info = response.json()
            logger.info(f"已打开新标签页: {tab_info.get('id')}")

            # 不在这里调用 maximize_window，让监控循环处理
            return True
        except Exception as e:
            logger.error(f"打开新标签页失败: {e}")
            return False

    async def _connect_to_tab(self, ws_url: str):
        """连接到标签页的 WebSocket"""
        try:
            # 如果已经存在连接，先关闭
            if ws_url in self.ws_connections:
                try:
                    await self.ws_connections[ws_url].close()
                except Exception as e:
                    logger.error(f"关闭 WebSocket 连接失败: {e}")
                del self.ws_connections[ws_url]

            # 创建新连接
            ws = await websockets.connect(
                ws_url,
                # ping_interval=None,  # 禁用自动ping
                close_timeout=5,
                max_size=None,
            )

            # 启用 Browser 域
            self.ws_connections[ws_url] = ws
            return ws
        except Exception as e:
            logger.error(f"连接到标签页 WebSocket 失败: {e}")
            return None

    async def maximize_window(self, tab_id: str):
        """最大化窗口"""
        ws_url = None
        try:
            # 获取标签页信息
            tabs = await self.get_cdp_tabs()
            tab_info = next((tab for tab in tabs if tab["id"] == tab_id), None)

            if not tab_info:
                logger.warning(f"找不到标签页: {tab_id}")
                return

            # 获取或创建 WebSocket 连接
            ws_url = tab_info["webSocketDebuggerUrl"]
            ws = self.ws_connections.get(ws_url)

            # 检查连接是否有效。`if not ws` 看不出连接是否已关闭 —— 一个已关闭
            # 的 WebSocketClientProtocol 依然是真值，所以这个分支只在缓存里
            # 根本没有条目时才成立，死连接会被一直复用。
            if ws is not None and _ws_is_closed(ws):
                logger.debug("WebSocket 连接已关闭，重新连接")
                ws = None
                self.ws_connections.pop(ws_url, None)

            if not ws:
                logger.debug(f"创建新的 WebSocket 连接: {ws_url}")
                ws = await self._connect_to_tab(ws_url)
                if not ws:
                    logger.error("无法创建 WebSocket 连接")
                    return

            # 先获取当前窗口状态
            window_state = await self._send_cdp_command(
                ws,
                "Browser.getWindowForTarget",
                {"targetId": tab_id},  # 使用标签页ID而不是固定的windowId
                timeout=3.0,
            )

            if not window_state:
                logger.error("获取窗口状态失败")
                return

            window_id = window_state.get("windowId")
            if not window_id:
                logger.error("无法获取窗口ID")
                return

            bounds: Dict = window_state.get("bounds", {})
            current_state = bounds.get("windowState", "")

            # 获取当前窗口状态
            bounds_state = await self._send_cdp_command(
                ws, "Browser.getWindowBounds", {"windowId": window_id}, timeout=3.0
            )

            if not bounds_state:
                logger.error("获取窗口边界失败")
                return

            current_state = bounds_state.get("bounds", {}).get("windowState", "")
            logger.debug(f"当前窗口状态: {current_state}")

            # 如果窗口不是最大化状态，则进行最大化
            if current_state == "minimized":
                # 恢复窗口
                result = await self._send_cdp_command(
                    ws,
                    "Browser.setWindowBounds",
                    {"windowId": window_id, "bounds": {"windowState": "normal"}},
                    timeout=3.0,
                )
                logger.info("窗口已恢复")

            elif current_state != "maximized":
                # 最大化窗口
                result = await self._send_cdp_command(
                    ws,
                    "Browser.setWindowBounds",
                    {"windowId": window_id, "bounds": {"windowState": "maximized"}},
                    timeout=3.0,
                )

                if result is None:
                    logger.error("窗口最大化失败")

        except websockets.exceptions.ConnectionClosed:
            logger.warning("WebSocket 连接已关闭，将在下次循环重试")
            if ws_url in self.ws_connections:
                del self.ws_connections[ws_url]
        except Exception as e:
            logger.error(f"最大化窗口失败: {e}")
            if ws_url and ws_url in self.ws_connections:
                del self.ws_connections[ws_url]

    async def _monitor_loop(self):
        """监控循环"""
        while self.running:
            try:
                # 检查标签页
                tabs = await self.get_cdp_tabs()
                if not tabs:
                    logger.info("没有打开的标签页，创建新标签页...")
                    await self.open_new_tab()
                    await asyncio.sleep(1)  # 等待标签页创建完成
                    continue

                # 只对第一个标签页进行最大化检查
                await self.maximize_window(tabs[0]["id"])

            except Exception as e:
                logger.error(f"监控循环出错: {e}")
                # 清理所有连接
                for ws in self.ws_connections.values():
                    try:
                        await ws.close()
                    except Exception as e:
                        logger.error(f"关闭 WebSocket 连接失败: {e}")
                self.ws_connections.clear()
                try:
                    if self.browser_process:
                        self.browser_process.terminate()
                        self.browser_process.kill()
                        # 和启动重试那里一样：不 wait() 就没有真正回收，
                        # 持续的 CDP 故障会让每一轮监控都留下一个僵尸进程，
                        # 直到撞上 SANDBOX_PID_LIMIT。同样放到线程里等，
                        # 别在监控循环里把事件循环按住 5 秒。
                        try:
                            await asyncio.to_thread(self.browser_process.wait, 5)
                        except Exception:
                            pass
                        self.browser_process = None
                except Exception as e:
                    logger.error(f"停止浏览器进程失败: {e}")
                # start() 失败时返回 False 而不是抛异常，并且已经把 running
                # 置回 False —— 忽略返回值就等于悄悄停掉了监控：既没有浏览器
                # 也没有人再看着它。
                if not await self.start():
                    logger.error("重启 Chromium 失败，监控循环退出")
                    return

            await asyncio.sleep(self.check_interval)

    async def start_monitoring(self):
        """开始监控"""
        if self.running:
            return

        self.running = True

        try:
            # 运行监控循环
            await self._monitor_loop()
        except Exception as e:
            logger.error(f"监控循环出错: {e}")
        finally:
            await self.stop_async()

    async def stop_async(self):
        """异步停止监控和浏览器"""
        self.running = False

        # 关闭所有 WebSocket 连接
        for ws in self.ws_connections.values():
            try:
                await ws.close()
            except Exception as e:
                logger.error(f"关闭 WebSocket 连接失败: {e}")
        self.ws_connections.clear()

        # 关闭浏览器进程
        if self.browser_process:
            try:
                self.browser_process.terminate()
                await asyncio.sleep(0.1)  # 给进程一些时间来终止
                if self.browser_process.poll() is None:
                    self.browser_process.kill()
            except Exception as e:
                logger.error(f"停止浏览器进程失败: {e}")
            self.browser_process = None

        logger.info("监控已停止")

    def stop(self):
        """同步停止监控和浏览器"""
        self.running = False

        try:
            # 获取当前事件循环
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # 如果循环正在运行，使用 run_coroutine_threadsafe
                future = asyncio.run_coroutine_threadsafe(self.stop_async(), loop)
                future.result(timeout=5)  # 等待停止完成
            else:
                # 如果循环没有运行，直接运行
                loop.run_until_complete(self.stop_async())
        except Exception as e:
            logger.error(f"停止时出错: {e}")
            # 强制停止进程
            if self.browser_process:
                self.browser_process.kill()

    async def set_screen_resolution(self, width: int, height: int):
        """通过 CDP 协议设置浏览器视窗大小"""
        tabs = await self.get_cdp_tabs()
        tab_info = next((tab for tab in tabs if tab["type"] == "page"), None)
        if not tab_info:
            raise ValueError("没有找到标签页")
        ws_url = tab_info["webSocketDebuggerUrl"]
        ws = self.ws_connections.get(ws_url)
        # 和 maximize_window 一样先判断连接是不是已经关闭：已关闭的
        # WebSocketClientProtocol 仍然是真值，`if not ws` 看不出来，死连接会
        # 被一直复用，之后每条 CDP 命令都只会静静地超时。
        if ws is not None and _ws_is_closed(ws):
            self.ws_connections.pop(ws_url, None)
            ws = None
        if not ws:
            ws = await self._connect_to_tab(ws_url)
            if not ws:
                raise ValueError("无法连接到标签页的 WebSocket")

        # 先获取窗口ID
        window_state = await self._send_cdp_command(
            ws,
            "Browser.getWindowForTarget",
            {"targetId": tab_info["id"]},
            timeout=3.0,
        )

        if not window_state:
            raise ValueError("获取窗口状态失败")

        window_id = window_state.get("windowId")
        if not window_id:
            raise ValueError("无法获取窗口ID")

        # 设置窗口大小
        await self._send_cdp_command(
            ws,
            "Browser.setWindowBounds",
            {
                "windowId": window_id,
                "bounds": {"width": width, "height": height, "windowState": "normal"},
            },
        )


def wait_for_display(display=None, timeout=60):
    """等待 X11 显示服务器准备就绪（使用 xdotool，无需 Xlib/.Xauthority）"""
    if display is None:
        display = os.getenv("DISPLAY", ":99")

    logger.info(f"等待显示服务器 {display} 准备就绪...")

    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            import subprocess
            result = subprocess.run(
                ["xdotool", "getdisplaygeometry"],
                env={**os.environ, "DISPLAY": display},
                capture_output=True,
                text=True,
                timeout=1,
            )
            if result.returncode == 0:
                logger.info(f"显示服务器 {display} 已准备就绪")
                return True
        except Exception as e:
            logger.debug(f"显示服务器未就绪: {e}")
        time.sleep(0.5)

    logger.error(f"超时：显示服务器 {display} 在 {timeout} 秒内未准备就绪")
    return False


if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser(
        description="Browser Guard - Monitor and manage browser instances"
    )
    parser.add_argument(
        "--wait-display",
        action="store_true",
        help="Wait for X11 display server to be ready",
    )
    parser.add_argument(
        "--monitor", action="store_true", help="Start browser monitoring"
    )
    parser.add_argument(
        "--display",
        type=str,
        default=None,
        help="X11 display to use (default: from DISPLAY env or :99)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=60,
        help="Timeout in seconds for display wait (default: 60)",
    )

    args = parser.parse_args()

    if args.wait_display:
        success = wait_for_display(display=args.display, timeout=args.timeout)
        if not success:
            sys.exit(1)
        logger.info("Display server is ready")

    if args.monitor:

        async def run_monitor():
            if os.getenv("USE_CDP", "false").lower() == "true":
                browser_guard = BrowserCDPGuard()
            else:
                browser_guard = BrowserGuard()

            await browser_guard.start()
            await browser_guard.start_monitoring()

        import asyncio

        try:
            asyncio.run(run_monitor())
        except KeyboardInterrupt:
            logger.info("Monitoring stopped by user")
        except Exception as e:
            logger.error(f"Monitoring error: {e}")
            sys.exit(1)
