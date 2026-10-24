"""GUI 冒烟测试：用桩件替换 tkinter，跑通 run_gui 的完整构造路径，不弹窗口。

覆盖点：
  - 控件创建与布局调用是否合法
  - StringVar / Progressbar / Text 的读写接口
  - 隔离扫描任务与用户配置，验证队列投递
  - 忙时拒绝关闭窗口，扫描完成后允许关闭
"""
import os
import sys
import types
from unittest.mock import MagicMock, patch

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
    return tk


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    tk = install_stub()
    import wx_underline_switch as app
    import offline_combo

    root = tk.Tk.return_value
    root.winfo_fpixels.return_value = 96.0
    root.winfo_screenwidth.return_value = 1920
    root.winfo_screenheight.return_value = 1080
    target = os.path.abspath("gui-smoke-fixture/RadiumWMPF.bin")
    workers = []

    def make_thread(target, daemon):
        return types.SimpleNamespace(start=lambda: workers.append(target))

    def exercise_close():
        callbacks = [call.args[1] for call in root.protocol.call_args_list
                     if call.args[0] == "WM_DELETE_WINDOW"]
        assert len(callbacks) == 1, "必须且只能注册一个窗口关闭回调"
        close = callbacks[0]
        assert len(workers) == 1, "初始扫描必须提交一个后台任务"
        close()
        root.destroy.assert_not_called()
        tk.messagebox.showwarning.assert_called_once()

        workers.pop()()
        root.after.call_args.args[1]()
        tk.messagebox.showwarning.reset_mock()
        close()
        root.destroy.assert_called_once()
        tk.messagebox.showwarning.assert_not_called()

    root.mainloop.side_effect = exercise_close
    report = {"summary": "测试组合", "package_state": "original",
              "dll_state": "original", "runtime_state": "missing"}
    with patch.object(app, "auto_target", return_value=target), \
            patch.object(app, "load_config", return_value={}), \
            patch.object(app, "save_config") as save, \
            patch.object(app.os.path, "isfile", return_value=True), \
            patch.object(app, "enable_dpi_awareness"), \
            patch.object(app.threading, "Thread", side_effect=make_thread), \
            patch.object(offline_combo, "inspect_combo", return_value=report) as scan:
        app.run_gui()
        scan.assert_called_once_with(target)
        save.assert_called_once_with({"target": target})
    print("SMOKE OK：构造、扫描队列、忙时拒绝关闭及空闲关闭均通过；未读写用户配置")


if __name__ == "__main__":
    main()
