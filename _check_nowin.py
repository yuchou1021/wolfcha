"""临时验证：端口查询不再起子进程（不会闪黑窗）。"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import control_panel as panel

print("8777 监听者:", panel.pids_on_port(8777))
print("9999 监听者:", panel.pids_on_port(9999))

# 把 subprocess.run 换成一调用就报错，验证 ctypes 路径不依赖子进程
original = panel.subprocess.run
calls = []


def guard(*args, **kwargs):
    calls.append(args[0] if args else kwargs)
    raise AssertionError("pids_on_port 不应该启动子进程: %r" % (args,))


panel.subprocess.run = guard
try:
    print("禁用 subprocess 后 8777:", panel.pids_on_port(8777))
    print("禁用 subprocess 后 9999:", panel.pids_on_port(9999))
finally:
    panel.subprocess.run = original

assert not calls, calls

pid = (panel.pids_on_port(8777) or [None])[0]
if pid:
    print("进程名（Win32 API）:", panel.process_name(pid))
print("OK")
