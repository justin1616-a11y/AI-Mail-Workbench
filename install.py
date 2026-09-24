# -*- coding: utf-8 -*-
"""邮件工作台 · 安装向导

双击 install.cmd 运行（或 python install.py）。

做的事：
  1. 找 Python 解释器（其实跑的这一刻就有了，用 sys.executable）
  2. 自动探测 Foxmail 的本地索引目录 —— 顺便从路径里猜出你的邮箱地址
  3. 问你邮箱、密码，**当场验证能不能登录**
  4. 写 config.json + 凭据文件（凭据放 ~/.workbuddy/secrets/，不进项目目录）
  5. 可选：装开机自启

只依赖标准库。
"""

import os
import re
import sys
import json
import glob
import getpass
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "config.json")
SECRETS_DIR = os.path.join(os.path.expanduser("~"), ".workbuddy", "secrets")
CRED = os.path.join(SECRETS_DIR, "mail.cred")
STARTUP = os.path.join(os.environ.get("APPDATA", ""),
                       r"Microsoft\Windows\Start Menu\Programs\Startup")
TASK_NAME = "邮件工作台-保活"

LINE = "-" * 62


def desktop_dir():
    """真实桌面路径。

    不能直接写 ~\\Desktop —— 装了 OneDrive 并开了「桌面备份」的机器，
    桌面会被重定向到 OneDrive\\Desktop，写错地方用户就找不到图标了。
    """
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "[Environment]::GetFolderPath('Desktop')"],
            capture_output=True, text=True, timeout=20,
        )
        p = (out.stdout or "").strip()
        if p and os.path.isdir(p):
            return p
    except Exception:
        pass
    return os.path.join(os.path.expanduser("~"), "Desktop")


def create_desktop_icon():
    """在桌面放一个转发用的 .cmd：双击 = 起服务 + 弹浏览器。

    为什么放转发文件而不是快捷方式(.lnk)：建 .lnk 要走 COM，某些环境下
    会被安全策略拦掉；一个纯文本的 .cmd 一定建得起来，效果一样。
    """
    desk = desktop_dir()
    if not os.path.isdir(desk):
        return None
    target = os.path.join(desk, "邮件工作台.cmd")
    launcher = os.path.join(HERE, "启动邮件工作台.cmd")
    try:
        with open(target, "w", encoding="utf-8") as f:
            f.write("@echo off\r\n")
            f.write("rem 邮件工作台 —— 双击即可（起服务 + 打开浏览器）\r\n")
            f.write('start "" /min "%s"\r\n' % launcher)
        return target
    except OSError:
        return None


def say(msg=""):
    print(msg)


def ask(prompt, default=""):
    hint = " [%s]" % default if default else ""
    try:
        v = input("%s%s: " % (prompt, hint)).strip()
    except EOFError:
        v = ""
    return v or default


def ask_yes(prompt, default=True):
    d = "Y/n" if default else "y/N"
    try:
        v = input("%s [%s]: " % (prompt, d)).strip().lower()
    except EOFError:
        v = ""
    if not v:
        return default
    return v in ("y", "yes", "是", "好")


def find_foxmail_index():
    """探测 Foxmail 的本地索引，返回 [(索引文件路径, 账号名), ...]

    注意：Foxmail 的 Mails/Index 是个**文件**（几 MB 的索引数据），
    不是目录 —— 用 isdir 判断会永远匹配不到。
    """
    roots = []
    for base in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                 os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                 r"D:\Program Files", r"D:\Program Files (x86)"):
        if not base:
            continue
        roots += glob.glob(os.path.join(base, "Foxmail*"))
    found = []
    for root in roots:
        for storage in glob.glob(os.path.join(root, "Storage", "*")):
            idx = os.path.join(storage, "Mails", "Index")
            if os.path.isfile(idx):
                found.append((idx, os.path.basename(storage)))
    return found


def load_existing():
    if os.path.exists(CONFIG):
        try:
            with open(CONFIG, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            pass
    return {}


def load_cred():
    out = {}
    if os.path.exists(CRED):
        try:
            for line in open(CRED, encoding="utf-8"):
                line = line.strip()
                if "=" in line and line.startswith("SJTU_MAIL_"):
                    k, v = line.split("=", 1)
                    out[k.strip()] = v.strip()
        except OSError:
            pass
    return out


def verify(host, port, user, pwd):
    """真的连一次 IMAP —— 别等用的时候才发现密码错"""
    import imaplib
    try:
        m = imaplib.IMAP4_SSL(host, int(port), timeout=20)
    except Exception as e:
        return False, "连不上 %s:%s（%s）" % (host, port, e)
    try:
        m.login(user, pwd)
    except imaplib.IMAP4.error as e:
        try:
            m.logout()
        except Exception:
            pass
        return False, "登录被拒绝：%s" % e
    try:
        m.select("INBOX", readonly=True)
        st, d = m.uid("search", None, "ALL")
        n = len(d[0].split()) if (d and d[0]) else 0
    except Exception:
        n = -1
    try:
        m.logout()
    except Exception:
        pass
    return True, "登录成功，收件箱里有 %s 封" % (n if n >= 0 else "若干")


def main():
    say()
    say("=" * 62)
    say("  邮件工作台 · 安装向导")
    say("=" * 62)
    say()
    say("它把「交大邮箱（IMAP）」和「Foxmail 本地索引」合成一个网页工作台。")
    say("只需要装一次，之后双击「启动邮件工作台.cmd」就能用。")
    say()

    old = load_existing()
    oldcred = load_cred()

    # ---------- 1. Python ----------
    say("[1/6] Python")
    say("      用这个解释器：%s" % sys.executable)
    if sys.version_info < (3, 8):
        say("      ✗ 版本太低（需要 3.8+），请换一个 Python。")
        return 1
    say("      版本 %d.%d ✓" % sys.version_info[:2])

    # ---------- 2. Foxmail ----------
    say()
    say("[2/6] Foxmail 本地索引（可选）")
    cands = find_foxmail_index()
    idx, guess_mail = "", ""
    if len(cands) == 1:
        idx, guess_mail = cands[0]
        say("      找到：%s" % guess_mail)
        say("      好处：历史邮件可以毫秒级搜索，而且不联网。")
    elif len(cands) > 1:
        say("      Foxmail 里有多个账号，你要用哪个？")
        for i, (_, acct) in enumerate(cands, 1):
            say("        %d) %s" % (i, acct))
        pick = ask("      填序号", "1")
        try:
            idx, guess_mail = cands[max(0, int(pick) - 1)]
        except (ValueError, IndexError):
            idx, guess_mail = cands[0]
        say("      用：%s" % guess_mail)
        say("      （想换成另一个，重跑本向导即可）")
    else:
        say("      没找到 Foxmail 的索引文件。")
        say("      不影响使用，只是搜旧邮件时会走服务器、慢一些。")
        say()
        say("      想要「毫秒级搜历史邮件」，先装个 Foxmail（免费，两分钟）：")
        say("        1) 下载    https://www.foxmail.com/")
        say("        2) 装好后新建账号，填交大邮箱")
        say("           收件 / 发件服务器都填：mail.sjtu.edu.cn")
        say("           IMAP 993(SSL)      SMTP 465(SSL)")
        say("           密码填邮箱密码；开过二次验证就填授权码")
        say("        3) 等它把信收完（右下角进度走完）")
        say("        4) 再跑一次本向导，索引就自动接上了")
        say("      （详细图文步骤见 INSTALL.md）")

    # ---------- 3. 邮箱 ----------
    say()
    say("[3/6] 邮箱设置")
    say("      （直接回车 = 用括号里的默认值）")
    say()
    default_mail = old.get("user") or oldcred.get("SJTU_MAIL_USER") or guess_mail or ""
    user = ask("      邮箱地址", default_mail)
    if not user or "@" not in user:
        say("      ✗ 邮箱地址不对，中止。")
        return 1
    domain = user.split("@", 1)[1]
    is_sjtu = domain.endswith("sjtu.edu.cn")

    imap_host = ask("      IMAP 服务器", old.get("imap_host") or "mail.sjtu.edu.cn")
    imap_port = ask("      IMAP 端口", str(old.get("imap_port") or 993))
    smtp_host = ask("      SMTP 服务器", old.get("smtp_host") or imap_host)
    smtp_port = ask("      SMTP 端口", str(old.get("smtp_port") or 465))
    from_name = ask("      发件人显示名", old.get("from_name") or user.split("@")[0])

    say()
    if is_sjtu:
        say("      密码提示：交大邮箱如果开了二次验证，这里要填「客户端授权码」")
        say("      而不是网页登录密码。路径：交我办 → 邮箱 → 设置 → 客户端授权码")
    say("      密码只写到 %s" % CRED)
    say("      不会进项目目录、不会被发到任何地方。")
    say()
    pwd = getpass.getpass("      邮箱密码/授权码: ").strip()
    if not pwd:
        pwd = oldcred.get("SJTU_MAIL_PASS", "")
        if pwd:
            say("      （留空，沿用上次保存的密码）")
        else:
            say("      ✗ 密码为空，中止。")
            return 1

    say("      正在验证登录…")
    ok, msg = verify(imap_host, int(imap_port), user, pwd)
    if ok:
        say("      ✓ %s" % msg)
    else:
        say("      ✗ %s" % msg)
        if not ask_yes("      仍然保存这份配置吗？", False):
            return 1

    # ---------- 4. 写配置 ----------
    say()
    say("[4/6] 写配置")
    cfg = dict(old)
    cfg.update({
        "_comment": "邮件工作台配置。密码不在这里，见 ~/.workbuddy/secrets/mail.cred。",
        "imap_host": imap_host,
        "imap_port": int(imap_port),
        "smtp_host": smtp_host,
        "smtp_port": int(smtp_port),
        "user": user,
        "from_name": from_name,
        "index_path": idx or old.get("index_path", ""),
        "drafts_folder": old.get("drafts_folder", "Drafts"),
        # 发送后追加副本到哪个文件夹。SMTP 不负责留副本，写错就会「发出去的信
        # 在已发送邮件里找不到」。老配置里没有这项，补默认值 Sent（交大邮箱就是这个名）。
        "sent_folder": old.get("sent_folder", "Sent"),
        "save_sent_to_server": old.get("save_sent_to_server", True),
        "page_size": old.get("page_size", 60),
    })
    cfg.setdefault("classify", {})
    cfg.pop("pass", None)
    with open(CONFIG, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    say("      config.json ✓")

    os.makedirs(SECRETS_DIR, exist_ok=True)
    with open(CRED, "w", encoding="utf-8") as f:
        f.write("# 邮件工作台凭据 —— 仅当前用户可读，别提交到任何仓库\n")
        f.write("SJTU_MAIL_USER=%s\n" % user)
        f.write("SJTU_MAIL_PASS=%s\n" % pwd)
    try:
        os.chmod(CRED, 0o600)
    except OSError:
        pass
    say("      凭据 mail.cred ✓")

    # ---------- 5. 开机自启 ----------
    say()
    say("[5/6] 开机自启（可选）")
    say("      装上的话，登录 Windows 后工作台会自己在后台跑起来，")
    say("      你随时打开 http://127.0.0.1:8080 就能看邮件。")
    if ask_yes("      要装吗？", True):
        # 先在工作目录放一个「自启.cmd」。用 %~dp0 定位自身目录，不写死绝对路径，
        # 所以整个文件夹搬家也不用改；计划任务只负责调用它。
        starter = os.path.join(HERE, "自启.cmd")
        try:
            with open(starter, "w", encoding="utf-8") as f:
                f.write("@echo off\r\n")
                f.write("chcp 65001 >nul\r\n")
                f.write("rem 邮件工作台 · 开机保活（安装向导生成）\r\n")
                f.write("rem 由 Windows 计划任务在「登录时」调用，静默起服务、不弹浏览器。\r\n")
                f.write("rem 用 %%~dp0 定位自身目录，文件夹搬家也不用改这里。\r\n")
                f.write("set \"MW=%~dp0\"\r\n")
                f.write("\r\n")
                f.write("set \"PYW=\"\r\n")
                f.write("rem 1) 优先 WorkBuddy 内置 pythonw（版本号通配）\r\n")
                f.write("for /d %%v in (\"%%USERPROFILE%%\\.workbuddy\\binaries\\python\\versions\\*\") do (\r\n")
                f.write("  if not defined PYW if exist \"%%~v\\pythonw.exe\" set \"PYW=%%~v\\pythonw.exe\"\r\n")
                f.write(")\r\n")
                f.write("rem 2) 退回 PATH\r\n")
                f.write("for %%i in (pythonw.exe) do if not defined PYW set \"PYW=%%~$PATH:i\"\r\n")
                f.write("\r\n")
                f.write("if not defined PYW (\r\n")
                f.write("  echo %date% %time% 找不到 pythonw.exe，已跳过自启 >> \"%MW%\\_launch.log\"\r\n")
                f.write("  exit /b 1\r\n")
                f.write(")\r\n")
                f.write("if not exist \"%MW%\\launch.py\" (\r\n")
                f.write("  echo %date% %time% 找不到 launch.py，已跳过自启 >> \"%MW%\\_launch.log\"\r\n")
                f.write("  exit /b 1\r\n")
                f.write(")\r\n")
                f.write("\r\n")
                f.write("start \"\" /b \"%PYW%\" \"%MW%\\launch.py\" --no-open\r\n")
                f.write("exit\r\n")
        except OSError as e:
            say("      ✗ 写自启脚本失败：%s" % e)
            starter = ""

        # 首选：注册计划任务。实测启动文件夹在部分机器上根本不触发，计划任务可靠得多。
        done = False
        if starter:
            try:
                r = subprocess.run(
                    ["schtasks", "/create", "/tn", TASK_NAME,
                     "/tr", starter, "/sc", "onlogon", "/rl", "limited", "/f"],
                    capture_output=True, text=True, timeout=60,
                )
                if r.returncode == 0:
                    done = True
                    say("      已注册计划任务「%s」✓" % TASK_NAME)
                    say("      登录 Windows 时自动起服务，不用你管。")
                    say("      想取消：schtasks /delete /tn \"%s\" /f" % TASK_NAME)
            except (OSError, subprocess.SubprocessError) as e:
                say("      注册计划任务失败：%s" % e)

        # 兜底：退回启动文件夹（计划任务没装成时）
        if not done and os.path.isdir(STARTUP):
            bat = os.path.join(STARTUP, "邮件工作台-保活.cmd")
            try:
                with open(bat, "w", encoding="utf-8") as f:
                    f.write("@echo off\r\n")
                    f.write("chcp 65001 >nul\r\n")
                    f.write("rem 邮件工作台 · 开机保活（安装向导生成）\r\n")
                    f.write("rem 登录 Windows 后静默起服务，不弹浏览器（--no-open）。\r\n")
                    f.write("rem 不想要了：在本文件夹删掉本文件即可。\r\n")
                    f.write("set \"MW=%s\"\r\n" % HERE)
                    f.write("\r\n")
                    f.write("set \"PYW=\"\r\n")
                    f.write("for /d %%v in (\"%%USERPROFILE%%\\.workbuddy\\binaries\\python\\versions\\*\") do (\r\n")
                    f.write("  if not defined PYW if exist \"%%~v\\pythonw.exe\" set \"PYW=%%~v\\pythonw.exe\"\r\n")
                    f.write(")\r\n")
                    f.write("for %%i in (pythonw.exe) do if not defined PYW set \"PYW=%%~$PATH:i\"\r\n")
                    f.write("\r\n")
                    f.write("if not defined PYW exit /b 1\r\n")
                    f.write("if not exist \"%MW%\\launch.py\" exit /b 1\r\n")
                    f.write("start \"\" /b \"%PYW%\" \"%MW%\\launch.py\" --no-open\r\n")
                    f.write("exit\r\n")
                say("      已写入启动文件夹：%s ✓" % bat)
                say("      想取消：Win+R 输 shell:startup，删掉本文件。")
                done = True
            except OSError as e:
                say("      ✗ 写启动文件夹失败：%s" % e)

        if not done:
            say("      ✗ 没装上自启。不影响使用 —— 双击桌面图标照样能开，")
            say("        只是开机不会自己跑。")
    else:
        say("      跳过（以后想装，重跑一次本向导即可）")

    # ---------- 6. 桌面图标 ----------
    say()
    say("[6/6] 桌面图标")
    say("      放一个图标在桌面，以后双击就开，不用记目录。")
    if ask_yes("      要放吗？", True):
        icon = create_desktop_icon()
        if icon:
            say("      已创建：%s ✓" % icon)
        else:
            say("      没建成（找不到桌面目录）。可以手动双击 创建桌面图标.cmd")
    else:
        say("      跳过（以后想要，双击 创建桌面图标.cmd）")

    # ---------- 完成 ----------
    say()
    say("=" * 62)
    say("  装好了。")
    say("=" * 62)
    say()
    say("  启动方式：双击  %s" % os.path.join(HERE, "启动邮件工作台.cmd"))
    say("  然后浏览器打开  http://127.0.0.1:8080")
    say()
    say("  如果页面提示「连不上本地服务」，就是服务没跑起来，")
    say("  双击上面那个 .cmd 即可（它会顺便把守护进程也拉起来）。")
    say()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        say()
        say("已取消。")
        sys.exit(1)
