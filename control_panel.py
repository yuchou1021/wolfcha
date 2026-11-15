"""WolfCha V1 控制台（Tkinter，无第三方依赖）。

功能：
- 一键启动 / 停止 / 重启游戏服务（端口可改）
- 显示局域网地址、游戏链接、总控制台链接，一键打开/复制
- 编辑 .env 里的 API Key / 接口地址 / 模型 / 管理口令，支持测试 Key
- 实时看服务日志
- 端口被别的进程占用时，可以一键结束它

启动方式：双击 V1\\控制台.bat，或 pythonw control_panel.py
"""

from __future__ import annotations

import os
import queue
import re
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from tkinter import messagebox, scrolledtext, ttk
import tkinter as tk

APP_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(APP_DIR, ".env")
LOG_DIR = os.path.join(APP_DIR, "logs")
DEFAULT_PORT = 8777

# 关键：pythonw 启动时没有控制台，任何 subprocess 调用默认都会弹一个黑窗口。
# 全部子进程都必须带上这个标志，否则会每秒闪一次黑框。
CREATE_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

CONFIG_FIELDS = [
    ("DEEPSEEK_API_KEY", "API Key", True),
    ("DEEPSEEK_API_BASE", "接口地址", False),
    ("DEEPSEEK_MODEL", "模型", False),
    ("ADMIN_TOKEN", "管理口令（可留空）", False),
]

BG = "#11141d"
PANEL = "#171b27"
FG = "#e8ecf5"
MUTED = "#8b93a7"
GOLD = "#d8b26a"
GREEN = "#5cc98b"
RED = "#e0675e"
CYAN = "#59b7d6"


# ---------------------------------------------------------------- 基础工具

def read_env(path: str = ENV_PATH):
    """返回 (原始行列表, {key: value})"""
    if not os.path.exists(path):
        return [], {}
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        lines = handle.read().splitlines()
    values = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip()
    return lines, values


def write_env(updates: dict, path: str = ENV_PATH):
    """更新 .env：保留注释和顺序，已有 key 改值，没有的追加。"""
    lines, _ = read_env(path)
    if not lines:
        lines = ["# LLM API Configuration"]
    remaining = dict(updates)
    output = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            key = stripped.partition("=")[0].strip()
            if key in remaining:
                output.append(f"{key}={remaining.pop(key)}")
                continue
        output.append(line)
    for key, value in remaining.items():
        output.append(f"{key}={value}")
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(output).rstrip("\n") + "\n")


def lan_ip() -> str:
    """拿本机在局域网里的 IPv4 地址"""
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.settimeout(0.3)
        probe.connect(("8.8.8.8", 80))
        address = probe.getsockname()[0]
        probe.close()
        if address and not address.startswith("127."):
            return address
    except Exception:
        pass
    try:
        return socket.gethostbyname(socket.gethostname())
    except Exception:
        return "127.0.0.1"


def port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.25) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def pids_on_port(port: int):
    """找监听该端口的进程 PID。

    优先用 Win32 API（GetExtendedTcpTable），不启动任何子进程 ——
    之前每秒跑一次 netstat，会每秒钟闪一个黑色控制台窗口。
    """
    pids = _pids_on_port_ctypes(port)
    if pids is not None:
        return pids

    # 兜底：netstat（必须隐藏窗口，否则会闪黑框）
    pids = []
    try:
        result = subprocess.run(
            ["netstat", "-ano"], capture_output=True, text=True, timeout=6,
            encoding="utf-8", errors="replace", creationflags=CREATE_NO_WINDOW,
        )
        pattern = re.compile(rf":{port}\s+\S+\s+LISTENING\s+(\d+)", re.IGNORECASE)
        for line in result.stdout.splitlines():
            match = pattern.search(line)
            if match:
                pid = int(match.group(1))
                if pid not in pids:
                    pids.append(pid)
    except Exception:
        pass
    return pids


def _pids_on_port_ctypes(port: int):
    """用 GetExtendedTcpTable 直接读监听表，成功返回列表，失败返回 None"""
    if os.name != "nt":
        return None
    try:
        import ctypes
        import ctypes.wintypes as wt

        class MIB_TCPROW_OWNER_PID(ctypes.Structure):
            _fields_ = [
                ("dwState", wt.DWORD),
                ("dwLocalAddr", wt.DWORD),
                ("dwLocalPort", wt.DWORD),
                ("dwRemoteAddr", wt.DWORD),
                ("dwRemotePort", wt.DWORD),
                ("dwOwningPid", wt.DWORD),
            ]

        AF_INET = 2
        # 3 = TCP_TABLE_OWNER_PID_LISTENER（注意不是 4，4 是 CONNECTIONS）
        TCP_TABLE_OWNER_PID_LISTENER = 3
        ERROR_INSUFFICIENT_BUFFER = 122

        size = wt.DWORD(0)
        result = ctypes.windll.iphlpapi.GetExtendedTcpTable(
            None, ctypes.byref(size), False, AF_INET, TCP_TABLE_OWNER_PID_LISTENER, 0
        )
        if result not in (0, ERROR_INSUFFICIENT_BUFFER) or size.value == 0:
            return None

        buffer = ctypes.create_string_buffer(size.value)
        result = ctypes.windll.iphlpapi.GetExtendedTcpTable(
            buffer, ctypes.byref(size), False, AF_INET, TCP_TABLE_OWNER_PID_LISTENER, 0
        )
        if result != 0:
            return None

        count = ctypes.cast(buffer, ctypes.POINTER(wt.DWORD))[0]
        row_size = ctypes.sizeof(MIB_TCPROW_OWNER_PID)
        base = ctypes.addressof(buffer) + ctypes.sizeof(wt.DWORD)

        pids = []
        for index in range(count):
            row = MIB_TCPROW_OWNER_PID.from_address(base + index * row_size)
            local_port = socket.ntohs(row.dwLocalPort & 0xFFFF)
            if local_port == port and row.dwOwningPid not in pids:
                pids.append(int(row.dwOwningPid))
        return pids
    except Exception:
        return None


def kill_pid(pid: int) -> bool:
    try:
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/F"],
            capture_output=True, timeout=8, creationflags=CREATE_NO_WINDOW,
        )
        return True
    except Exception:
        return False


def process_name(pid: int) -> str:
    """进程名，例如 python / chrome（优先 Win32 API，不弹窗口）"""
    name = _process_image_name(pid)
    if name:
        return name
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             f"(Get-Process -Id {pid}).ProcessName"],
            capture_output=True, text=True, timeout=8, encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
        lines = [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]
        if lines:
            return lines[0]
    except Exception:
        pass
    try:
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
            capture_output=True, text=True, timeout=6, encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
        line = (result.stdout or "").strip().splitlines()
        if line:
            return line[0].split(",")[0].strip('"')
    except Exception:
        pass
    return ""


def _process_image_name(pid: int) -> str:
    """QueryFullProcessImageNameW：拿可执行文件名，零子进程、零窗口"""
    if os.name != "nt":
        return ""
    try:
        import ctypes
        import ctypes.wintypes as wt

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return ""
        try:
            buffer = ctypes.create_unicode_buffer(1024)
            size = wt.DWORD(1024)
            if ctypes.windll.kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                return os.path.basename(buffer.value)
            return ""
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    except Exception:
        return ""


def process_cmdline(pid: int) -> str:
    """进程命令行（拿不到就返回空）"""
    try:
        script = (
            "(Get-CimInstance Win32_Process -Filter \"ProcessId=%d\").CommandLine" % pid
        )
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=8, encoding="utf-8", errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
        return (result.stdout or "").strip()
    except Exception:
        return ""


def is_wolfcha_cmdline(cmdline: str) -> bool:
    """命令行是不是我们自己的游戏服务（python main.py，可能带绝对路径）"""
    text = (cmdline or "").lower().replace("/", "\\")
    return "main.py" in text


# ---------------------------------------------------------------- 主窗口

class ControlPanel:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.proc: subprocess.Popen | None = None
        self.log_queue: "queue.Queue[str]" = queue.Queue()
        self.busy = False

        _, env = read_env()
        self.port_var = tk.StringVar(value=str(DEFAULT_PORT))
        self.status_var = tk.StringVar(value="正在检查…")
        self.tip_var = tk.StringVar(value="")
        self.show_key_var = tk.BooleanVar(value=False)
        self.auto_open_var = tk.BooleanVar(value=True)

        self.config_vars = {}
        for key, _label, _secret in CONFIG_FIELDS:
            self.config_vars[key] = tk.StringVar(value=env.get(key, ""))

        # PID -> ('wolfcha'|'python'|'other'|'unknown', 显示名)，避免每秒重复查进程
        self._pid_kind_cache: dict[int, tuple[str, str]] = {}
        self._probe_inflight = False
        # 服务日志走文件，控制台退出后服务也能继续跑
        self._server_log = os.path.join(LOG_DIR, "server.log")
        self._log_pos = 0

        self.root.title("WolfCha V1 · 控制台")
        self.root.geometry("820x720")
        self.root.minsize(760, 620)
        self.root.configure(bg=BG)
        self._set_window_icon()
        self._build_style()
        self._build_ui()

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.log("控制台已就绪。点「启动服务」开始。")
        self._poll()

    # ------------------------------------------------------------ 样式

    def _set_window_icon(self):
        """窗口/任务栏用狼头图标（ico + png 都设，兼容不同 Windows）"""
        # Windows 任务栏默认会用 pythonw.exe 的图标，显式声明 AppUserModelID 才会用窗口图标
        try:
            import ctypes

            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("WolfCha.ControlPanel")
        except Exception:
            pass

        icon = os.path.join(APP_DIR, "static", "wolfcha.ico")
        png = os.path.join(APP_DIR, "static", "wolfcha.png")
        try:
            if os.path.exists(icon):
                self.root.iconbitmap(default=icon)
        except Exception:
            pass
        try:
            if os.path.exists(png):
                self._icon_photo = tk.PhotoImage(file=png)
                self.root.iconphoto(True, self._icon_photo)
        except Exception:
            pass

    def _build_style(self):
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(".", background=BG, foreground=FG, fieldbackground=PANEL)
        style.configure("TFrame", background=BG)
        style.configure("Panel.TFrame", background=PANEL)
        style.configure("TLabel", background=BG, foreground=FG)
        style.configure("Muted.TLabel", background=BG, foreground=MUTED)
        style.configure("Panel.TLabel", background=PANEL, foreground=FG)
        style.configure("Tip.TLabel", background=BG, foreground=MUTED)
        style.configure("TLabelframe", background=BG, foreground=MUTED, bordercolor="#2a3040")
        style.configure("TLabelframe.Label", background=BG, foreground=GOLD)
        style.configure("TButton", background="#232a3a", foreground=FG, borderwidth=0, padding=(12, 7))
        style.map("TButton", background=[("active", "#2d3547"), ("disabled", "#1c2130")],
                  foreground=[("disabled", "#5b6272")])
        style.configure("Primary.TButton", background="#d8b26a", foreground="#241a08")
        style.map("Primary.TButton", background=[("active", "#e6c582"), ("disabled", "#4a3f28")])
        style.configure("Danger.TButton", background="#3a2323", foreground="#ffb3ad")
        style.map("Danger.TButton", background=[("active", "#4a2b2b")])
        style.configure("TEntry", fieldbackground="#0d1017", foreground=FG, insertcolor=FG,
                        bordercolor="#2a3040", lightcolor="#2a3040", darkcolor="#2a3040")
        style.configure("TCheckbutton", background=BG, foreground=MUTED)
        style.map("TCheckbutton", background=[("active", BG)])
        style.configure("TSpinbox", fieldbackground="#0d1017", foreground=FG, arrowcolor=FG)

    # ------------------------------------------------------------ 布局

    def _build_ui(self):
        outer = ttk.Frame(self.root, padding=16)
        outer.pack(fill="both", expand=True)

        # 标题 + 状态
        header = ttk.Frame(outer)
        header.pack(fill="x")
        ttk.Label(header, text="WolfCha V1 控制台", font=("Microsoft YaHei UI", 16, "bold")).pack(side="left")
        self.status_label = tk.Label(header, textvariable=self.status_var, bg=BG, fg=MUTED,
                                     font=("Microsoft YaHei UI", 11))
        self.status_label.pack(side="right")

        # 服务区
        service = ttk.LabelFrame(outer, text="  服务  ", padding=12)
        service.pack(fill="x", pady=(12, 8))

        row1 = ttk.Frame(service)
        row1.pack(fill="x")
        ttk.Label(row1, text="端口").pack(side="left")
        self.port_entry = ttk.Entry(row1, textvariable=self.port_var, width=8)
        self.port_entry.pack(side="left", padx=(8, 16))
        self.start_btn = ttk.Button(row1, text="启动服务", style="Primary.TButton", command=self.start_server)
        self.start_btn.pack(side="left", padx=4)
        self.stop_btn = ttk.Button(row1, text="停止", command=self.stop_server, state="disabled")
        self.stop_btn.pack(side="left", padx=4)
        self.restart_btn = ttk.Button(row1, text="重启", command=self.restart_server, state="disabled")
        self.restart_btn.pack(side="left", padx=4)
        self.kill_btn = ttk.Button(row1, text="结束占用端口的进程", style="Danger.TButton",
                                   command=self.kill_port_owner, state="disabled")
        self.kill_btn.pack(side="left", padx=(16, 4))
        ttk.Checkbutton(row1, text="启动后打开浏览器", variable=self.auto_open_var).pack(side="right")

        self.url_text = tk.Text(service, height=3, bg="#0d1017", fg=CYAN, bd=0, relief="flat",
                                font=("Consolas", 10), cursor="hand2", padx=10, pady=8)
        self.url_text.pack(fill="x", pady=(10, 8))
        self.url_text.tag_configure("muted", foreground=MUTED)
        self.url_text.config(state="disabled")

        row2 = ttk.Frame(service)
        row2.pack(fill="x")
        ttk.Button(row2, text="打开游戏", command=lambda: webbrowser.open(self._base_url())).pack(side="left", padx=(0, 6))
        ttk.Button(row2, text="打开总控制台", command=lambda: webbrowser.open(self._base_url() + "/admin")).pack(side="left", padx=6)
        ttk.Button(row2, text="复制手机地址", command=self.copy_url).pack(side="left", padx=6)
        ttk.Button(row2, text="手机连不上？放行防火墙", command=self.open_firewall_helper).pack(side="left", padx=6)
        ttk.Button(row2, text="打开日志目录", command=lambda: self._open_path(LOG_DIR)).pack(side="left", padx=6)

        # 配置区
        config = ttk.LabelFrame(outer, text="  API 配置（保存到 .env，重启后生效）  ", padding=12)
        config.pack(fill="x", pady=8)

        for index, (key, label, secret) in enumerate(CONFIG_FIELDS):
            line = ttk.Frame(config)
            line.pack(fill="x", pady=4)
            ttk.Label(line, text=label, width=18).pack(side="left")
            entry = ttk.Entry(line, textvariable=self.config_vars[key],
                              show="•" if secret else "")
            entry.pack(side="left", fill="x", expand=True)
            if secret:
                self.key_entry = entry

                def toggle(entry=entry):
                    entry.config(show="" if self.show_key_var.get() else "•")

                ttk.Checkbutton(line, text="显示", variable=self.show_key_var, command=toggle).pack(side="left", padx=(8, 0))

        buttons = ttk.Frame(config)
        buttons.pack(fill="x", pady=(8, 0))
        ttk.Button(buttons, text="保存配置", style="Primary.TButton", command=self.save_config).pack(side="left")
        ttk.Button(buttons, text="保存并重启", command=self.save_and_restart).pack(side="left", padx=6)
        ttk.Button(buttons, text="测试 Key", command=self.test_key).pack(side="left", padx=6)
        ttk.Button(buttons, text="打开 .env", command=lambda: self._open_path(ENV_PATH)).pack(side="left", padx=6)
        ttk.Label(config, textvariable=self.tip_var, style="Tip.TLabel").pack(anchor="w", pady=(8, 0))

        # 日志区
        log_box = ttk.LabelFrame(outer, text="  服务日志  ", padding=10)
        log_box.pack(fill="both", expand=True, pady=(8, 0))
        self.log_text = scrolledtext.ScrolledText(
            log_box, height=12, bg="#0b0e15", fg="#cbd3e1", insertbackground=FG,
            bd=0, relief="flat", font=("Consolas", 9), state="disabled", wrap="word",
        )
        self.log_text.pack(fill="both", expand=True)
        log_actions = ttk.Frame(log_box)
        log_actions.pack(fill="x", pady=(8, 0))
        ttk.Button(log_actions, text="清空日志", command=self.clear_log).pack(side="left")

    # ------------------------------------------------------------ 小工具

    def _base_url(self) -> str:
        port = self._port()
        return f"http://127.0.0.1:{port}"

    def _port(self) -> int:
        try:
            port = int(self.port_var.get().strip())
            return port if 1 <= port <= 65535 else DEFAULT_PORT
        except ValueError:
            return DEFAULT_PORT

    def _open_path(self, path: str):
        if os.path.exists(path):
            os.startfile(path)  # type: ignore[attr-defined]
        else:
            messagebox.showinfo("提示", f"文件/目录还不存在：\n{path}")

    def copy_url(self):
        address = f"http://{lan_ip()}:{self._port()}/"
        self.root.clipboard_clear()
        self.root.clipboard_append(address)
        self.set_tip(f"已复制手机地址：{address}")

    def open_firewall_helper(self):
        """打开放行防火墙的脚本（会弹 UAC）"""
        helper = os.path.join(os.path.dirname(APP_DIR), "放行防火墙.bat")
        if not os.path.exists(helper):
            messagebox.showinfo("提示", f"找不到放行脚本：\n{helper}")
            return
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 f"Start-Process -FilePath '{helper}' -Verb RunAs"],
                capture_output=True, timeout=20, creationflags=CREATE_NO_WINDOW,
            )
            self.set_tip("已请求管理员权限，请在弹窗里点「是」")
        except Exception as error:
            messagebox.showerror("打开失败", str(error))

    def set_tip(self, text: str):
        self.tip_var.set(text)

    def log(self, text: str):
        self.log_queue.put(text)

    def _drain_log(self):
        try:
            while True:
                line = self.log_queue.get_nowait()
                self.log_text.config(state="normal")
                stamp = time.strftime("%H:%M:%S")
                self.log_text.insert("end", f"[{stamp}] {line}\n")
                self.log_text.see("end")
                self.log_text.config(state="disabled")
        except queue.Empty:
            pass
        self.root.after(120, self._drain_log)

    def clear_log(self):
        self.log_text.config(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.config(state="disabled")

    # ------------------------------------------------------------ 服务控制

    def _child_env(self) -> dict:
        env = os.environ.copy()
        env["PYTHONPATH"] = os.path.join(APP_DIR, ".pylibs")
        env["PORT"] = str(self._port())
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        return env

    def _pid_kind(self, pid: int) -> tuple[str, str]:
        """判断占用端口的到底是谁：wolfcha（我们自己的服务）/ python / other / unknown"""
        cached = self._pid_kind_cache.get(pid)
        if cached:
            return cached
        name = process_name(pid)
        cmdline = process_cmdline(pid)
        if is_wolfcha_cmdline(cmdline):
            kind = "wolfcha"
        elif name.lower().startswith("python"):
            kind = "python"
        elif not name and not cmdline:
            kind = "unknown"
        else:
            kind = "other"
        info = (kind, name or cmdline[:40] or "未知进程")
        self._pid_kind_cache[pid] = info
        return info

    def _owners(self, blocking: bool = True) -> list[dict]:
        """当前端口的监听进程列表（带身份判断）。

        blocking=False 时，第一次遇到的 PID 先标成「确认中」，后台线程去查，
        避免在 UI 线程里起 PowerShell 造成窗口卡一下。
        """
        owners = []
        missing = []
        for pid in pids_on_port(self._port()):
            cached = self._pid_kind_cache.get(pid)
            if cached:
                kind, name = cached
            elif blocking:
                kind, name = self._pid_kind(pid)
            else:
                kind, name = "probing", "确认中…"
                missing.append(pid)
            owners.append({"pid": pid, "kind": kind, "name": name})

        if missing and not self._probe_inflight:
            self._probe_inflight = True
            threading.Thread(target=self._probe_owners, args=(missing,), daemon=True).start()
        return owners

    def _probe_owners(self, pids: list[int]):
        try:
            for pid in pids:
                if pid not in self._pid_kind_cache:
                    self._pid_kind(pid)
        except Exception:
            pass
        finally:
            self._probe_inflight = False

    def _describe_owners(self, owners: list[dict]) -> str:
        return "、".join(f"PID {o['pid']} {o['name']}" for o in owners)

    def start_server(self):
        if self.proc and self.proc.poll() is None:
            self.set_tip("服务已经在运行（本窗口启动）；要重新加载配置请点「重启」")
            return
        port = self._port()
        owners = self._owners() if port_open(port) else []
        if owners:
            kinds = {owner["kind"] for owner in owners}
            if "wolfcha" in kinds:
                title = "服务已经在运行"
                message = (
                    f"你自己的 WolfCha 服务在跑（{self._describe_owners(owners)}，不是从这个窗口启动的，"
                    f"可能是 启动.bat 或上次遗留的进程）。\n\n要重启它吗？"
                )
            elif "python" in kinds:
                title = "端口被 Python 占用"
                message = (
                    f"端口 {port} 被一个 Python 进程占着（{self._describe_owners(owners)}），"
                    f"很可能就是你之前启动的游戏服务。\n\n要结束它并重新启动吗？"
                )
            elif "unknown" in kinds:
                title = "端口被占用"
                message = (
                    f"端口 {port} 被占用（{self._describe_owners(owners)}），但查不到进程信息。\n\n"
                    f"要强制结束它再启动吗？"
                )
            else:
                title = "端口被占用"
                message = (
                    f"端口 {port} 被其他程序占用（{self._describe_owners(owners)}）。\n\n要结束它再启动吗？"
                )
            if not messagebox.askyesno(title, message):
                return
            for owner in owners:
                kill_pid(owner["pid"])
            time.sleep(0.8)
            self._pid_kind_cache.clear()

        self.log(f"启动命令：python main.py（PORT={port}）")
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
            # 输出写到文件而不是管道：这样关掉控制台窗口，服务也能继续跑
            if os.path.exists(self._server_log) and os.path.getsize(self._server_log) > 2 * 1024 * 1024:
                try:
                    os.replace(self._server_log, self._server_log + ".old")
                except Exception:
                    pass
            self._log_pos = 0
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            with open(self._server_log, "a", encoding="utf-8") as log_file:
                self.proc = subprocess.Popen(
                    [sys.executable, "main.py"],
                    cwd=APP_DIR,
                    env=self._child_env(),
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    creationflags=flags,
                )
        except Exception as error:
            self.log(f"启动失败：{error}")
            messagebox.showerror("启动失败", str(error))
            return

        self.set_tip("正在启动，等端口就绪…")
        if self.auto_open_var.get():
            self.root.after(2500, self._open_when_ready)

    def _open_when_ready(self):
        if port_open(self._port()):
            webbrowser.open(self._base_url() + "/")

    def _tail_server_log(self):
        """把服务日志文件的新内容读进日志面板"""
        try:
            if not os.path.exists(self._server_log):
                return
            size = os.path.getsize(self._server_log)
            if size < self._log_pos:  # 文件被截断/轮转
                self._log_pos = 0
            if size == self._log_pos:
                return
            with open(self._server_log, "r", encoding="utf-8", errors="replace") as handle:
                handle.seek(self._log_pos)
                chunk = handle.read()
                self._log_pos = handle.tell()
            for line in chunk.splitlines():
                if line.strip():
                    self.log(line)
        except Exception:
            pass

    def stop_server(self, ask: bool = False):
        if ask and not messagebox.askyesno("停止服务", "确定要停止服务吗？正在游戏中的房间会中断。"):
            return
        if self.proc and self.proc.poll() is None:
            self.log("正在停止服务…")
            try:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=6)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
            except Exception as error:
                self.log(f"停止失败：{error}")
            self.log("服务已停止。")
            return

        owners = self._owners()
        if not owners:
            self.log("当前没有在运行的服务。")
            return

        kinds = {owner["kind"] for owner in owners}
        if "wolfcha" in kinds:
            title = "停止你的服务"
            message = f"要停止你自己的 WolfCha 服务吗？\n{self._describe_owners(owners)}"
        elif "python" in kinds:
            title = "停止 Python 进程"
            message = f"要结束端口 {self._port()} 上的 Python 进程吗？\n{self._describe_owners(owners)}"
        elif "unknown" in kinds:
            title = "结束占用进程"
            message = f"查不到占用端口 {self._port()} 的进程信息，要强制结束它吗？\n{self._describe_owners(owners)}"
        else:
            title = "结束其他程序"
            message = f"端口 {self._port()} 被别的程序占着，要强制结束它吗？\n{self._describe_owners(owners)}"
        if not messagebox.askyesno(title, message):
            return
        for owner in owners:
            kill_pid(owner["pid"])
        self._pid_kind_cache.clear()
        self.log(f"已结束：{self._describe_owners(owners)}")
        self.set_tip("已停止外部启动的服务")

    def restart_server(self):
        self.log("—— 重启服务 ——")
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=6)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
        port = self._port()
        for pid in pids_on_port(port):
            kill_pid(pid)
        self._pid_kind_cache.clear()
        time.sleep(0.8)
        self.start_server()

    def kill_port_owner(self):
        owners = self._owners()
        if not owners:
            self.set_tip("端口没有被占用")
            return
        if not messagebox.askyesno("结束进程", f"强制结束占用端口 {self._port()} 的进程？\n{self._describe_owners(owners)}"):
            return
        for owner in owners:
            kill_pid(owner["pid"])
        self._pid_kind_cache.clear()
        self.log(f"已结束：{self._describe_owners(owners)}")
        self.set_tip("已结束占用端口的进程")

    # ------------------------------------------------------------ 配置

    def _collect_config(self) -> dict:
        return {key: self.config_vars[key].get().strip() for key, _label, _secret in CONFIG_FIELDS}

    def save_config(self, quiet: bool = False) -> bool:
        values = self._collect_config()
        if not values.get("DEEPSEEK_API_KEY"):
            if not messagebox.askyesno("提示", "API Key 还是空的，确定要保存吗？"):
                return False
        try:
            write_env(values)
        except Exception as error:
            messagebox.showerror("保存失败", str(error))
            return False
        self.log("配置已保存到 .env：" + ", ".join(key for key in values if values[key]))
        if not quiet:
            self.set_tip("配置已保存，重启服务后生效")
            messagebox.showinfo("已保存", "配置已写入 .env。\n重启服务后生效。")
        return True

    def save_and_restart(self):
        if self.save_config(quiet=True):
            self.set_tip("配置已保存，正在重启…")
            self.restart_server()

    def test_key(self):
        key = self.config_vars["DEEPSEEK_API_KEY"].get().strip()
        base = self.config_vars["DEEPSEEK_API_BASE"].get().strip() or "https://api.deepseek.com"
        model = self.config_vars["DEEPSEEK_MODEL"].get().strip() or "deepseek-chat"
        if not key:
            messagebox.showwarning("提示", "先填 API Key")
            return
        self.log(f"测试接口：{base} / {model} …")
        self.set_tip("正在测试接口…")

        def worker():
            try:
                import httpx

                response = httpx.post(
                    base.rstrip("/") + "/chat/completions",
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                    json={"model": model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 1},
                    timeout=25,
                )
                if response.status_code == 200:
                    self.log("接口测试通过 ✓")
                    self.root.after(0, lambda: messagebox.showinfo("测试通过", "接口可用，Key 有效。"))
                else:
                    snippet = response.text[:300]
                    self.log(f"接口返回 {response.status_code}：{snippet}")
                    self.root.after(0, lambda: messagebox.showerror("测试失败", f"HTTP {response.status_code}\n{snippet}"))
            except Exception as error:
                self.log(f"测试出错：{error}")
                self.root.after(0, lambda: messagebox.showerror("测试失败", str(error)))

        threading.Thread(target=worker, daemon=True).start()

    # ------------------------------------------------------------ 状态轮询

    def _poll(self):
        port = self._port()
        self._tail_server_log()
        ours = self.proc is not None and self.proc.poll() is None
        listening = port_open(port)
        owners = self._owners(blocking=False) if (listening and not ours) else []

        if ours and listening:
            self._set_status(f"● 运行中 · 端口 {port} · PID {self.proc.pid}（本窗口启动）", GREEN)
        elif ours:
            self._set_status("● 启动中，等待端口就绪…", GOLD)
        elif owners:
            kinds = {owner["kind"] for owner in owners}
            detail = self._describe_owners(owners)
            if "probing" in kinds:
                self._set_status(f"● 端口 {port} 有进程在监听，正在确认是谁…", MUTED)
            elif "wolfcha" in kinds:
                # 是你自己的服务，只是不是这个窗口启动的（比如 启动.bat / 上次遗留）
                self._set_status(f"● 运行中 · 端口 {port} · {detail}（外部启动）", GREEN)
            elif "python" in kinds:
                self._set_status(f"● 端口 {port} 被 Python 进程占用 · {detail}（可能是你的服务）", GOLD)
            elif "unknown" in kinds:
                self._set_status(f"● 端口 {port} 被占用 · {detail}（读不到进程信息）", GOLD)
            else:
                self._set_status(f"● 端口 {port} 被其他程序占用 · {detail}", RED)
        elif listening:
            self._set_status(f"● 端口 {port} 被占用（PID 未知，可能权限不足）", GOLD)
        else:
            self._set_status("○ 未运行", MUTED)

        running = ours or listening
        self.start_btn.config(state="disabled" if ours else "normal")
        self.stop_btn.config(state="normal" if running else "disabled")
        self.restart_btn.config(state="normal" if running else "disabled")
        # 「强制结束」只留给真正的外来程序；自己的服务用「停止/重启」
        foreign = [owner for owner in owners if owner["kind"] == "other"]
        self.kill_btn.config(state="normal" if foreign else "disabled")
        self._render_urls(port, listening, starting=ours and not listening)
        self.root.after(1000, self._poll)

    def _set_status(self, text: str, color: str):
        self.status_var.set(text)
        self.status_label.config(fg=color)

    def _render_urls(self, port: int, running: bool, starting: bool = False):
        address = lan_ip()
        self.url_text.config(state="normal")
        self.url_text.delete("1.0", "end")
        if running or starting:
            self.url_text.insert("end", f"手机访问   http://{address}:{port}\n")
            self.url_text.insert("end", f"本机访问   http://127.0.0.1:{port}\n")
            self.url_text.insert("end", f"总控制台   http://127.0.0.1:{port}/admin\n")
        else:
            self.url_text.insert("end", "服务未启动，启动后这里会显示手机访问地址。", "muted")
        self.url_text.config(state="disabled")

    # ------------------------------------------------------------ 关闭

    def stop_service_quietly(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=5)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
        for pid in pids_on_port(self._port()):
            kill_pid(pid)
        self._pid_kind_cache.clear()

    def on_close(self):
        running = (self.proc is not None and self.proc.poll() is None) or port_open(self._port())
        if running:
            keep = messagebox.askyesno(
                "退出",
                "服务还在运行。\n\n"
                "退出控制台后要继续保留服务吗？\n"
                "「是」= 服务继续在后台跑（手机还能玩）\n"
                "「否」= 顺便把服务停掉",
            )
            if not keep:
                self.stop_service_quietly()
        self.root.destroy()


def acquire_single_instance():
    """只允许开一个控制台窗口；已经有窗口在跑时返回 None"""
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.CreateMutexW(None, False, "WolfCha.ControlPanel.SingleInstance")
        ERROR_ALREADY_EXISTS = 183
        if kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
            return None
        return handle
    except Exception:
        return True  # 拿不到就直接放行，别把程序卡死


def main():
    # 单实例：避免双击多次弹出一堆一模一样的窗口
    _mutex = acquire_single_instance()
    if _mutex is None:
        # 已经有窗口在跑：把它叫到前面来，然后自己退出
        raised = False
        try:
            import ctypes

            hwnd = ctypes.windll.user32.FindWindowW(None, "WolfCha V1 · 控制台")
            if hwnd:
                ctypes.windll.user32.ShowWindow(hwnd, 9)  # SW_RESTORE
                ctypes.windll.user32.SetForegroundWindow(hwnd)
                raised = True
        except Exception:
            pass

        if not raised:
            # 找不到旧窗口（可能被最小化到托盘），闪一个 2 秒的提示，不用点确定
            notice = tk.Tk()
            notice.withdraw()
            notice.title("WolfCha 控制台")
            label = tk.Label(
                notice, text="控制台已经在运行了，别重复打开", bg="#171b27", fg="#e8ecf5",
                font=("Microsoft YaHei UI", 11), padx=24, pady=14,
            )
            label.pack()
            notice.deiconify()
            notice.after(2200, notice.destroy)
            notice.mainloop()
        return

    root = tk.Tk()
    panel = ControlPanel(root)
    root.mainloop()


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        root = tk.Tk()
        root.withdraw()
        panel = ControlPanel(root)
        root.update()
        checks = {
            "port": panel._port() > 0,
            "env_read": isinstance(read_env()[1], dict),
            "port_scan": isinstance(pids_on_port(1), list),
            "panel_built": hasattr(panel, "log_text"),
            "fields": len(panel.config_vars) == len(CONFIG_FIELDS),
        }
        root.destroy()
        print("selftest:", checks)
        assert all(checks.values()), checks
        print("OK")
    else:
        main()
