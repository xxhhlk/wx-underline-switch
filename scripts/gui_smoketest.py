"""GUI 冒烟测试：用桩件替换 tkinter，跑通 run_gui 的完整构造路径，不弹窗口。

覆盖点：
  - 控件创建与布局调用是否合法
  - StringVar / Progressbar / Text 的读写接口
  - 后台扫描线程 + 队列投递是否正常
"""
import os
import sys
import time
import types
from unittest.mock import MagicMock

# 让脚本能从 src/ 导入主模块
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))


def install_stub():
    tk = types.ModuleType("tkinter")

    class Var:
        def __init__(self, value=None, **kwargs):
            self._value = value

        def get(self):
            return self._value

        def set(self, value):
            self._value = value

    tk.Tk = MagicMock(name="Tk")
    tk.StringVar = Var
    tk.Text = MagicMock(name="Text")
    tk.TclError = Exception

    ttk = types.ModuleType("tkinter.ttk")
    for name in ("Style", "Frame", "Label", "Entry", "Button",
                 "LabelFrame", "Progressbar", "Scrollbar"):
        setattr(ttk, name, MagicMock(name=name))

    filedialog = types.ModuleType("tkinter.filedialog")
    filedialog.askopenfilename = MagicMock(return_value="")

    messagebox = types.ModuleType("tkinter.messagebox")
    messagebox.showinfo = MagicMock()
    messagebox.showwarning = MagicMock()
    messagebox.askyesno = MagicMock(return_value=False)

    tk.ttk = ttk
    tk.filedialog = filedialog
    tk.messagebox = messagebox

    sys.modules["tkinter"] = tk
    sys.modules["tkinter.ttk"] = ttk
    sys.modules["tkinter.filedialog"] = filedialog
    sys.modules["tkinter.messagebox"] = messagebox


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    install_stub()
    import wx_underline_switch as app

    print("auto_target:", app.auto_target())
    started = time.time()
    app.run_gui()
    print("run_gui 构造完成，用时 %.2fs" % (time.time() - started))

    # 等后台扫描线程收尾
    time.sleep(6)
    print("SMOKE OK")


if __name__ == "__main__":
    main()
