"""邮件工作台 · 启动器

双击桌面快捷方式时实际执行的就是这个文件。

做三件事：
  1. 探一下 8080 是否已经在服务（已开就直接用，不重复起）
  2. 没开就后台拉起 server.py（无窗口、不占终端）
  3. 等它真的能响应了，再打开浏览器

为什么要这么绕：普通快捷方式只打开网址是不够的 ——
服务器没在跑的话，页面根本打不开（会显示"无法访问"）。
所以「启动服务」这步必须包进入口里。

用 pythonw.exe 执行本文件即可无窗口运行：
  pythonw.exe launch.py
"""

import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("MAIL_WORKBENCH_PORT", "8080"))
URL = "http://127.0.0.1:%d" % PORT
LOG = os.path.join(HERE, "_launch.log")
WATCHDOG = os.path.join(HERE, "watchdog.py")
PIDFILE = os.path.join(HERE, "_watchdog.pid")
# server.py 自己的输出（原来丢 DEVNULL，进程一崩就什么线索都没有）
SERVER_LOG = os.path.join(HERE, "_server.log")
WATCHDOG_LOG = os.path.join(HERE, "_watchdog.log")

# Windows: 脱离父进程 + 不创建窗口。
# DETACHED_PROCESS      = 0x00000008  让子进程自己活，启动器退出后服务器继续在
# CREATE_NEW_PROCESS_GROUP = 0x00000200
# CREATE_NO_WINDOW      = 0x08000000  别弹黑框
CREATE_FLAGS = 0x00000008 | 0x00000200 | 0x08000000 if os.name == "nt" else 0

# 探活必须绕开系统代理 —— 这个坑花了一整轮排查。
#
# 本机装了代理时 `http_proxy=http://127.0.0.1:50896`，且 `no_proxy` 是空的，
# 于是 urllib 对 `http://127.0.0.1:8080/api/health` 的请求会被代理接走，
# 代理连不上就回 502。结果是：**服务明明好好地跑着，alive() 却永远 False**。
# 而 alive() 是启动器唯一的判据，于是它反复拉起新实例、等满 20 秒、
# 最后弹一个「启动超时」的框 —— 用户看到的是「页面打不开」，
# 真正的原因却完全在别处。凡是探本机端口，一律走这个不带代理的 opener。
_PROXY_FREE = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def openlog(path):
    """子进程输出落到文件。

    为什么不用 DEVNULL：进程崩溃时 traceback 全被吞掉，只剩「服务没了」
    一个现象，无从下手。落到文件才能事后看到到底报了什么。
    """
    try:
        return open(path, "ab")
    except OSError:
        return subprocess.DEVNULL


def log(msg):
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write("%s  %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except OSError:
        pass


def alive(timeout=0.8):
    """8080 上是否已经有能正常响应的服务（绕开系统代理，见 _PROXY_FREE）"""
    try:
        with _PROXY_FREE.open(URL + "/api/health", timeout=timeout) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def message_box(title, text):
    """失败时弹个真正的对话框 —— pythonw 没有终端，print 是看不见的"""
    if os.name != "nt":
        return
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, text, title, 0x10)
    except Exception:
        pass


def watchdog_running():
    try:
        pid = int(open(PIDFILE, encoding="utf-8").read().strip())
    except (OSError, ValueError):
        return False
    try:
        # errors="replace"：中文 Windows 的 tasklist 没有匹配任务时输出中文（GBK），
        # text=True 默认按 UTF-8 解码会抛 UnicodeDecodeError，把这个判断整个打断 ——
        # 于是就变成「每次都以为守护进程没在跑」，反复启动新的。
        out = subprocess.run(
            ["tasklist", "/FI", "PID eq %d" % pid, "/NH"],
            capture_output=True, text=True, errors="replace", timeout=10,
        ).stdout
        return str(pid) in out
    except Exception:
        return False


def ensure_watchdog():
    """保证守护进程在跑。

    守护进程才是「服务不会挂掉」的保证 —— 它每 15 秒探活一次。
    它必须脱离父进程（DETACHED_PROCESS），否则父进程一退它就跟着死。
    """
    if watchdog_running():
        return
    wf = openlog(WATCHDOG_LOG)
    try:
        subprocess.Popen(
            [sys.executable, WATCHDOG],
            cwd=HERE,
            creationflags=CREATE_FLAGS,
            stdin=subprocess.DEVNULL,
            stdout=wf,
            stderr=wf,
            close_fds=True,
        )
        log("已拉起守护进程 watchdog.py")
    except OSError as e:
        log("拉起守护进程失败：%s" % e)
    finally:
        if wf is not subprocess.DEVNULL:
            wf.close()


def main():
    # --no-open：只保证服务和守护进程在跑，不打开浏览器
    no_open = "--no-open" in sys.argv

    ensure_watchdog()

    if alive():
        log("服务已在运行，直接打开浏览器")
    else:
        log("服务未运行，启动 server.py --port %d" % PORT)
        sf = openlog(SERVER_LOG)
        try:
            subprocess.Popen(
                [sys.executable, os.path.join(HERE, "server.py"), "--port", str(PORT)],
                cwd=HERE,
                creationflags=CREATE_FLAGS,
                stdin=subprocess.DEVNULL,
                stdout=sf,
                stderr=sf,
                close_fds=True,
            )
        except OSError as e:
            log("启动失败：%s" % e)
            message_box("邮件工作台", "无法启动服务器：\n%s\n\n请检查 %s" % (e, HERE))
            return
        finally:
            if sf is not subprocess.DEVNULL:
                sf.close()

        # 最多等 20 秒（IMAP 首次连接有时慢）
        for _ in range(40):
            if alive(1.0):
                break
            time.sleep(0.5)
        else:
            log("等待超时，服务器没起来")
            message_box(
                "邮件工作台",
                "服务器启动超时（等了 20 秒）。\n\n请看日志：\n%s" % LOG,
            )
            return
        log("服务已就绪")

    # 服务就绪之后，主动预热一次 V2。
    #
    # V2 是懒加载的：第一次请求 /api/v2/* 才做初始化，实测要 ~7 秒
    # （/api/v2/health 4.65s + 首次 /api/v2/buckets 2.4s，之后 0.1s）。
    # 不预热的话，用户打开页面的第一屏就得白等这几秒 ——
    # 而他刚刚才经历过「页面打不开」，一片空白极容易被当成「又坏了」。
    # 宁可让启动器多花几秒，也不要让人对着白屏猜。
    try:
        with _PROXY_FREE.open(URL + "/api/v2/buckets?limit=1", timeout=60) as r:
            r.read()
        log("V2 预热完成")
    except Exception as e:
        log("V2 预热失败（不影响使用，只是首屏会慢几秒）：%s" % e)

    if no_open:
        log("--no-open，不打开浏览器")
        return
    webbrowser.open(URL)
    log("已打开 %s" % URL)


if __name__ == "__main__":
    # pythonw.exe 没有控制台 —— 这里一旦抛异常，进程会静默消失，
    # 用户只看到「页面打不开」，连一条线索都没有（这一轮就是这么被卡住的）。
    # 所以必须兜住：落盘 + 弹一个真窗口。有窗口，才知道出过事。
    try:
        main()
    except Exception:
        import traceback

        tb = traceback.format_exc()
        log("启动器异常：\n%s" % tb)
        message_box("邮件工作台 · 启动失败",
                    "启动器出错，详情见：\n%s\n\n%s" % (LOG, tb[-900:]))
        sys.exit(1)
