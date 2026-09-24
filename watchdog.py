"""邮件工作台 · 守护进程

内置浏览器面板要「常驻」，前提是服务别再动不动挂掉（今天它已经死过两次，
每次都是面板里直接报「无法访问」）。

这个脚本只干一件事：每 15 秒探一下 8080，挂了就立刻拉起来。
用 pythonw.exe 跑，无窗口、不打扰。

用文件锁避免重复启动多个守护进程。
"""

import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PORT = int(os.environ.get("MAIL_WORKBENCH_PORT", "8080"))
URL = "http://127.0.0.1:%d/api/health" % PORT
LOG = os.path.join(HERE, "_watchdog.log")
LOCK = os.path.join(HERE, "_watchdog.pid")
INTERVAL = 15

CREATE_FLAGS = 0x00000008 | 0x00000200 | 0x08000000 if os.name == "nt" else 0

# 探活必须绕开系统代理。
#
# 本机 `http_proxy=http://127.0.0.1:50896` 且 `no_proxy` 为空时，urllib 对
# `http://127.0.0.1:8080/api/health` 的请求会被代理接走、回 502，
# 于是这里会**永远**判成「服务挂了」—— 而服务一直好好的。
# 对守护进程来说这个 bug 格外危险：它每 15 秒误判一次，就会每 15 秒
# 拉起一个新 server（除了第一个 bind 得上，其余全撞端口失败），
# 日志被刷屏，真正的故障反而被淹没。
_PROXY_FREE = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def log(msg):
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write("%s  %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg))
    except OSError:
        pass


def alive(timeout=1.2):
    try:
        with _PROXY_FREE.open(URL, timeout=timeout) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def already_running():
    """文件锁：拿到锁才准跑，避免起一堆守护进程"""
    if os.path.exists(LOCK):
        try:
            pid = int(open(LOCK, encoding="utf-8").read().strip())
        except (OSError, ValueError):
            pid = 0
        if pid and pid != os.getpid():
            # 这个 pid 真的还活着吗 —— 而且要确认它确实是 python 系进程。
            # 只看 pid 存在是不够的：pid 会被系统复用，同一个号可能早已落到
            # 别的程序头上，于是「已有守护进程在跑」成了一次误判，
            # 真正的 watchdog 反而永远起不来，服务就此没人管。
            try:
                # errors="replace" 是必须的：中文 Windows 的 tasklist 在「没有匹配任务」时
                # 输出的是中文提示（GBK 字节），而 text=True 默认按 UTF-8 解码 →
                # 抛 UnicodeDecodeError。这个异常在 subprocess 的读取线程里抛出，
                # 会把 already_running() 整个打断，于是每次都判成「没有守护进程在跑」，
                # 结果并存出好几个 watchdog，每个都在探活、每个都可能去拉起 server。
                # 我们要匹配的只是 ASCII 的 pid 和 "python"，替换掉坏字节完全够用。
                out = subprocess.run(
                    ["tasklist", "/FI", "PID eq %d" % pid, "/NH", "/FO", "CSV"],
                    capture_output=True, text=True, errors="replace", timeout=10,
                ).stdout
                if ('"%d"' % pid) in out and "python" in out.lower():
                    return True
            except Exception:
                pass
    try:
        with open(LOCK, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
    except OSError:
        pass
    return False


def start_server():
    """拉起 server，**并把它的输出落盘**。

    这里曾经用的是 stdout=DEVNULL。后果很严重：服务崩溃时连 traceback 都没有，
    排查时只剩「服务又没了」这一个现象，完全无从下手 ——
    launch.py 里为同一件事专门写过注释，watchdog 这边不该再踩第二次。
    句柄由本进程（长期存活）持有，所以不存在「启动器退出导致子进程写日志失败」的问题。
    """
    logf = None
    try:
        logf = open(os.path.join(HERE, "_server.log"), "ab")
    except OSError:
        logf = None
    try:
        subprocess.Popen(
            [sys.executable, os.path.join(HERE, "server.py"), "--port", str(PORT)],
            cwd=HERE,
            creationflags=CREATE_FLAGS,
            stdin=subprocess.DEVNULL,
            stdout=logf or subprocess.DEVNULL,
            stderr=logf or subprocess.DEVNULL,
            close_fds=True,
        )
        return True
    except OSError as e:
        log("拉起服务失败：%s" % e)
        return False
    finally:
        if logf is not None:
            logf.close()


def main():
    if already_running():
        log("已有守护进程在跑，本进程退出")
        return
    log("守护进程启动（每 %d 秒探活一次）" % INTERVAL)

    if not alive():
        log("服务未运行，拉起")
        start_server()
        for _ in range(30):
            time.sleep(1)
            if alive():
                log("服务已就绪")
                break

    while True:
        time.sleep(INTERVAL)
        if not alive():
            log("检测到服务不可用，重新拉起")
            start_server()
            for _ in range(30):
                time.sleep(1)
                if alive():
                    log("服务已恢复")
                    break
            else:
                log("30 秒仍未恢复，继续等待下一轮")


if __name__ == "__main__":
    # 同 launch.py：pythonw 下异常是静默的。守护进程若悄悄死掉，
    # 用户只会发现「服务又没人管了」，所以异常必须落盘。
    try:
        main()
    except Exception:
        import traceback

        log("守护进程异常：\n%s" % traceback.format_exc())
        raise
