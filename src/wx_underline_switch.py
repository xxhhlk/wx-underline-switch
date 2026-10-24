#!/usr/bin/env python3
"""微信划线开关 —— 关闭 / 还原公众号文章里的「划线评论」绿色虚线。

原理：
    微信内置浏览器运行时 RadiumWMPF.bin 内嵌一段原生注入的 JS，
    内容是 `WeixinJSBridge.support_mp_underline = true;`。
    页面侧 underline_action.js 用该标志决定是否启用划线能力：
        ne = support_mp_underline && (isWindows && cpVersion("3.9.6") || ...)
        se = android || ios || ne
        re = harmony || android || ios || ne || isMpapp
    Windows 桌面端 android/ios/harmony 恒为 false，故 se/re 就等于 ne。
    把该字面量 true 改成同长度的 null（falsy），Windows 侧整条特性链即断开。

实现要点：
    1.2.0 的写入使用 offline_combo 的严格文件摘要白名单，组合处理划线开关、
    组件初始化和主程序定向摘要比较。未知版本拒绝写入，已有解压副本同步。
    修改前完整备份，失败回滚；不使用常驻内存注入。
    特征串扫描与 ZIP CRC 辅助函数仍供离线侦察脚本使用。

用法：
    wx_underline_switch.py                 启动图形界面
    wx_underline_switch.py --cli status     命令行：查看状态
    wx_underline_switch.py --cli apply      命令行：关闭划线
    wx_underline_switch.py --cli revert     命令行：还原
    以上可附加 --target <RadiumWMPF.bin 路径>
"""

import argparse
import ctypes
import csv
import glob
import hashlib
import json
import os
import queue
import re
import struct
import subprocess
import sys
import threading
import zlib

APP_NAME = "WxUnderlineSwitch"
APP_VERSION = "1.2.0"
BIN_NAME = "RadiumWMPF.bin"
NEEDLE = "support_mp_underline"
LITERAL_LEN = 4
ORIGINAL_LITERAL = "true"
PATCHED_LITERAL = "null"

ENCODINGS = (("utf-16-le", 2), ("ascii", 1))

CHUNK = 8 * 1024 * 1024
LOOKAHEAD = 128


# ---------------------------------------------------------------- 路径与配置

def config_dir():
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    path = os.path.join(base, APP_NAME)
    os.makedirs(path, exist_ok=True)
    return path


def config_path():
    return os.path.join(config_dir(), "config.json")


def load_config():
    try:
        with open(config_path(), "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    try:
        with open(config_path(), "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, ensure_ascii=False, indent=2)
    except OSError:
        pass


# ---------------------------------------------------------------- 目标发现

def candidate_paths():
    """按常见安装位置与 Sandboxie 沙盒布局枚举 RadiumWMPF.bin。

    Sandboxie 的实际层级因版本/配置而异，常见形如：
        Sandbox\\<用户>\\<沙盒名>\\drive\\C\\Program Files\\Tencent\\Weixin\\<版本>\\
    因此对 1~3 层中间目录逐级试探，并兼容无盘符段与自定义安装目录。
    """
    patterns = []
    for env in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        base = os.environ.get(env)
        if base:
            patterns.append(os.path.join(base, "Tencent", "Weixin", "*", BIN_NAME))
    for drive in ("C", "D", "E", "F", "G"):
        sandbox = "%s:\\Sandbox" % drive
        if not os.path.isdir(sandbox):
            continue
        for depth in (1, 2, 3):
            for drive_seg in ("*", None):
                for prog in ("Program Files*", "*"):
                    segs = [sandbox] + ["*"] * depth + ["drive"]
                    if drive_seg:
                        segs.append(drive_seg)
                    segs += [prog, "Tencent", "Weixin", "*", BIN_NAME]
                    patterns.append("\\".join(segs))
    found = []
    for pat in patterns:
        for hit in glob.glob(pat):
            if os.path.isfile(hit) and hit not in found:
                found.append(hit)
    return found


def auto_target():
    cfg = load_config()
    remembered = cfg.get("target")
    if remembered and os.path.isfile(remembered):
        return remembered
    hits = candidate_paths()
    if not hits:
        return None
    hits.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    return hits[0]


# ---------------------------------------------------------------- 扫描与解析

def _needle_patterns():
    out = []
    for enc, width in ENCODINGS:
        try:
            out.append((enc, width, NEEDLE.encode(enc)))
        except UnicodeEncodeError:
            continue
    return out


def _classify(window, enc, width):
    """从特征串之后的窗口里解析布尔字面量，返回 (相对偏移, 状态, 字面量)。"""
    if enc == "utf-16-le":
        text = window.decode("utf-16-le", "replace")
    else:
        text = window.decode("latin-1", "replace")
    match = re.search(r"=\s*([^\s;]{1,8})", text)
    if not match:
        return None, "unknown", ""
    literal = match.group(1)
    rel = match.start(1) * width
    if literal.startswith(ORIGINAL_LITERAL):
        return rel, "original", literal
    if literal.startswith(PATCHED_LITERAL) or literal.startswith("!1"):
        return rel, "patched", literal
    if literal.startswith("false"):
        return rel, "patched", literal
    return rel, "unknown", literal


def scan_sites(path, progress=None):
    """流式扫描，返回 [(literal_offset, enc, width, state, literal)]。"""
    sites = []
    size = os.path.getsize(path)
    patterns = _needle_patterns()
    overlap = max(len(p) for _, _, p in patterns) + LOOKAHEAD
    consumed = 0
    prev = b""
    with open(path, "rb") as fh:
        while True:
            buf = fh.read(CHUNK)
            if not buf:
                break
            data = prev + buf
            base = consumed - len(prev)
            for enc, width, pat in patterns:
                start = 0
                while True:
                    idx = data.find(pat, start)
                    if idx < 0:
                        break
                    pat_end = idx + len(pat)
                    window = data[pat_end:pat_end + LOOKAHEAD]
                    rel, state, literal = _classify(window, enc, width)
                    if rel is not None:
                        abs_off = base + pat_end + rel
                        if not any(s[0] == abs_off for s in sites):
                            sites.append((abs_off, enc, width, state, literal))
                    start = idx + len(pat)
            prev = data[-overlap:]
            consumed += len(buf)
            if progress and size:
                progress(min(100.0, consumed * 100.0 / size))
    return sites


def summarize(sites):
    if not sites:
        return "未找到"
    states = set(s[3] for s in sites)
    if states == {"original"}:
        return "未修改（划线开启）"
    if states == {"patched"}:
        return "已关闭划线"
    if "original" in states:
        return "部分修改"
    return "未知"


def sha1_of_file(path, progress=None):
    h = hashlib.sha1()
    size = os.path.getsize(path)
    done = 0
    with open(path, "rb") as fh:
        while True:
            buf = fh.read(CHUNK)
            if not buf:
                break
            h.update(buf)
            done += len(buf)
            if progress and size:
                progress(min(100.0, done * 100.0 / size))
    return h.hexdigest()


# ---------------------------------------------------------------- 进程守卫

def weixin_running():
    try:
        proc = subprocess.run(
            ["tasklist", "/FO", "CSV", "/NH"],
            capture_output=True, timeout=20, check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            encoding="mbcs", errors="replace")
        rows = list(csv.reader(proc.stdout.splitlines(), strict=True))
        if not rows or any(len(row) < 2 or not row[1].isdigit() for row in rows):
            return None
    except (OSError, subprocess.SubprocessError, csv.Error):
        return None
    names = {"weixin.exe", "wechat.exe", "wechatappex.exe"}
    return any(row and row[0].lower() in names for row in rows)


# ---------------------------------------------------------------- ZIP 完整性

# RadiumWMPF.bin 本身是一个 ZIP 包（46 个条目，全部 stored 不压缩），
# 第一个条目是 file_component.xml，带逐文件 md5 与 ECDSA 签名。
# 划线开关那条 JS 落在 runtime/flue.dll 条目里，所以改写之后有三层对不上：
#   ① 该条目的 CRC32 与本地文件头 / 中央目录里记的值对不上 —— 这一层可以重算修好；
#   ② flue.dll 实际 md5 与签名清单记录不符；直接修改清单的实验未稳定运行；
#   ③ flue.dll 的 Authenticode 签名摘要失配，不能在没有私钥时恢复官方签名。
# 签名失配不等于已证明周期弹窗的唯一直接根因，导入 WinVerifyTrust 也不足以证明。
# 下方 helpers 只处理第 ① 层。GUI/CLI 通过 offline_combo 组合修改两处校验行为，
# 本机超过 9 小时验证无资源失效；版本白名单、签名状态与回退范围见 README。

ZIP_EOCD = b"PK\x05\x06"
ZIP_CD = b"PK\x01\x02"
ZIP_LH = b"PK\x03\x04"


def zip_locate_entry(path, inner_offset):
    """找出数据区包含 inner_offset 的 ZIP 条目，返回其 CRC 字段位置。"""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            fh.seek(max(0, size - 65536))
            tail = fh.read()
            pos = tail.rfind(ZIP_EOCD)
            if pos < 0:
                return None
            (_s, _d1, _d2, _n1, n_total, cd_size, cd_off, _cl) = struct.unpack_from("<IHHHHIIH", tail, pos)
            if not (0 < n_total < 10000) or cd_off + cd_size > size:
                return None
            fh.seek(cd_off)
            cd = fh.read(cd_size)
            p = 0
            for _ in range(n_total):
                if cd[p:p + 4] != ZIP_CD:
                    return None
                (_sig, _vm, _vn, _fl, method, _mt, _md, crc, csize, _us,
                 nlen, elen, clen, _ds, _ia, _ea, loff) = struct.unpack_from("<IHHHHHHIIIHHHHHII", cd, p)
                fh.seek(loff)
                lh = fh.read(30)
                if lh[:4] != ZIP_LH:
                    return None
                (l_nlen, l_elen) = struct.unpack_from("<HH", lh, 26)
                data_off = loff + 30 + l_nlen + l_elen
                if data_off <= inner_offset < data_off + csize:
                    return {"name": cd[p + 46:p + 46 + nlen].decode("utf-8", "replace"),
                            "method": method, "data_off": data_off, "csize": csize,
                            "crc": crc, "crc_local_off": loff + 14, "crc_cd_off": cd_off + p + 16}
                p += 46 + nlen + elen + clen
    except (OSError, struct.error):
        return None
    return None


def zip_refresh_crc(path, inner_offset, log=print):
    """重算被改条目的 CRC32，写回本地文件头与中央目录。返回 (名字, 旧值, 新值, 是否改动)。"""
    ent = zip_locate_entry(path, inner_offset)
    if not ent:
        log("不是 ZIP 包或未定位到条目，跳过 CRC 修复")
        return None
    if ent["method"] != 0:
        log("条目 %s 是压缩存储，跳过 CRC 修复" % ent["name"])
        return None

    crc = 0
    with open(path, "rb") as fh:
        fh.seek(ent["data_off"])
        remain = ent["csize"]
        while remain > 0:
            buf = fh.read(min(CHUNK, remain))
            if not buf:
                break
            crc = zlib.crc32(buf, crc)
            remain -= len(buf)
    crc &= 0xFFFFFFFF
    changed = crc != ent["crc"]
    if changed:
        with open(path, "r+b") as fh:
            for off in (ent["crc_local_off"], ent["crc_cd_off"]):
                fh.seek(off)
                fh.write(struct.pack("<I", crc))
            fh.flush()
            os.fsync(fh.fileno())
    return ent["name"], ent["crc"], crc, changed


# ---------------------------------------------------------------- 动作

def apply_patch(path, log=print, progress=None, runtime=None):
    from offline_combo import change_combo
    if progress:
        progress(0)
    result = change_combo(path, runtime=runtime, log=log,
                          backup_root=os.path.join(config_dir(), "transactions"),
                          process_check=weixin_running)
    if progress:
        progress(100)
    return result


def revert_patch(path, log=print, progress=None, runtime=None):
    from offline_combo import change_combo
    if progress:
        progress(0)
    result = change_combo(path, restore=True, runtime=runtime, log=log,
                          backup_root=os.path.join(config_dir(), "transactions"),
                          process_check=weixin_running)
    if progress:
        progress(100)
    return result


# ---------------------------------------------------------------- 高 DPI

def enable_dpi_awareness():
    """声明进程 DPI 感知。必须在创建 Tk 之前调用。

    不声明的话，Windows 会把整个窗口按位图拉伸到缩放比例，界面发虚。
    优先级：per-monitor v2 > per-monitor v1 > system aware。
    """
    if sys.platform != "win32":
        return False
    try:
        # -4 = DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2（Win10 1703+）
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return True
    except Exception:                                     # noqa: BLE001
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)     # per-monitor v1
        return True
    except Exception:                                     # noqa: BLE001
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()          # system aware
        return True
    except Exception:                                     # noqa: BLE001
        return False


def apply_dpi_scaling(root):
    """按实际 DPI 设置 Tk 的磅值换算，返回像素缩放比（96 DPI = 1.0）。"""
    try:
        dpi = float(root.winfo_fpixels("1i"))
    except Exception:                                     # noqa: BLE001
        dpi = 96.0
    if dpi <= 0:
        dpi = 96.0
    try:
        # tk scaling 的单位是「每磅像素数」，1 磅 = 1/72 英寸
        root.tk.call("tk", "scaling", dpi / 72.0)
    except Exception:                                     # noqa: BLE001
        pass
    return max(1.0, dpi / 96.0)


# ---------------------------------------------------------------- 图形界面

def run_gui():
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox

    enable_dpi_awareness()
    root = tk.Tk()
    scale = apply_dpi_scaling(root)

    def px(value):
        """把 96 DPI 下的设计像素换算成当前 DPI 的实际像素。"""
        return int(round(value * scale))

    root.title("微信划线开关 v%s" % APP_VERSION)
    # 窗口按 DPI 放大，但不超过屏幕九成，避免高缩放下超出可视区
    win_w = min(px(760), int(root.winfo_screenwidth() * 0.92))
    win_h = min(px(520), int(root.winfo_screenheight() * 0.88))
    root.geometry("%dx%d" % (win_w, win_h))
    root.minsize(min(px(680), win_w), min(px(460), win_h))

    style = ttk.Style()
    try:
        style.theme_use("vista")
    except tk.TclError:
        pass

    state = {"path": auto_target(), "busy": False}

    outer = ttk.Frame(root, padding=px(12))
    outer.pack(fill="both", expand=True)

    ttk.Label(outer, text="微信内置浏览器运行时",
              font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w")

    path_row = ttk.Frame(outer)
    path_row.pack(fill="x", pady=(px(6), 0))
    path_var = tk.StringVar(value=state["path"] or "")
    entry = ttk.Entry(path_row, textvariable=path_var)
    entry.pack(side="left", fill="x", expand=True)

    def browse():
        chosen = filedialog.askopenfilename(
            title="选择 RadiumWMPF.bin",
            filetypes=[("RadiumWMPF", "RadiumWMPF.bin"), ("所有文件", "*.*")])
        if chosen:
            state["path"] = chosen
            path_var.set(chosen)
            refresh()

    btn_browse = ttk.Button(path_row, text="浏览", width=8, command=browse)
    btn_browse.pack(side="left", padx=(px(6), 0))

    info = ttk.LabelFrame(outer, text=" 状态 ", padding=px(10))
    info.pack(fill="x", pady=px(10))
    status_var = tk.StringVar(value="检测中…")
    detail_var = tk.StringVar(value="")
    ttk.Label(info, textvariable=status_var,
              font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w")
    ttk.Label(info, textvariable=detail_var,
              foreground="#555").pack(anchor="w", pady=(px(4), 0))

    bar = ttk.Progressbar(outer, mode="determinate", maximum=100)
    bar.pack(fill="x")

    btn_row = ttk.Frame(outer)
    btn_row.pack(fill="x", pady=px(10))
    btn_apply = ttk.Button(btn_row, text="关闭划线")
    btn_apply.pack(side="left")
    btn_revert = ttk.Button(btn_row, text="还原")
    btn_revert.pack(side="left", padx=px(8))
    btn_refresh = ttk.Button(btn_row, text="重新检测")
    btn_refresh.pack(side="left")
    ttk.Button(btn_row, text="打开配置文件目录",
               command=lambda: os.startfile(config_dir())).pack(side="right")

    log_box = ttk.LabelFrame(outer, text=" 日志 ", padding=px(6))
    log_box.pack(fill="both", expand=True)
    text = tk.Text(log_box, height=10, wrap="word", state="disabled",
                   font=("Consolas", 9), background="#fbfbfb", relief="flat")
    scroll = ttk.Scrollbar(log_box, orient="vertical", command=text.yview)
    text.configure(yscrollcommand=scroll.set)
    text.pack(side="left", fill="both", expand=True)
    scroll.pack(side="right", fill="y")

    # Tkinter 不是线程安全的：后台线程一律通过队列把更新丢回主线程执行
    ui_queue = queue.Queue()

    def _write_log(msg):
        text.configure(state="normal")
        text.insert("end", msg + "\n")
        text.see("end")
        text.configure(state="disabled")

    def log(msg):
        ui_queue.put(("log", msg))

    def ui_progress(value):
        ui_queue.put(("progress", value))

    def post(fn):
        ui_queue.put(("call", fn))

    def drain():
        try:
            while True:
                kind, payload = ui_queue.get_nowait()
                if kind == "log":
                    _write_log(payload)
                elif kind == "progress":
                    bar["value"] = payload
                else:
                    try:
                        payload()
                    except Exception as exc:              # noqa: BLE001
                        _write_log("界面回调异常: %s" % exc)
        except queue.Empty:
            pass
        root.after(60, drain)

    def set_busy(flag, hint=""):
        state["busy"] = flag
        for b in (btn_apply, btn_revert, btn_refresh, btn_browse, entry):
            b.configure(state="disabled" if flag else "normal")
        if hint:
            status_var.set(hint)

    def finish(summary, detail):
        status_var.set(summary)
        detail_var.set(detail)
        set_busy(False)
        bar.configure(value=0)

    def refresh():
        path = path_var.get().strip()
        state["path"] = path
        if not path or not os.path.isfile(path):
            status_var.set("未找到 RadiumWMPF.bin")
            detail_var.set("请用「浏览」手动指定微信安装目录下的该文件")
            return
        set_busy(True, "扫描中…")
        log("扫描 %s" % path)

        def work():
            try:
                from offline_combo import inspect_combo
                report = inspect_combo(path)
                summary = report["summary"]
                detail = "组件包：%s ｜ 主程序：%s ｜ 缓存：%s" % (
                    report["package_state"], report["dll_state"], report["runtime_state"])
                log(json.dumps(report, ensure_ascii=False, indent=2))
                post(lambda: finish(summary, detail))
            except Exception as exc:                      # noqa: BLE001
                post(lambda e=exc: (status_var.set("扫描失败"),
                                    detail_var.set(str(e)),
                                    set_busy(False),
                                    bar.configure(value=0),
                                    _write_log("扫描异常: %s" % e)))

        state["path"] = path
        cfg = load_config()
        cfg["target"] = path
        save_config(cfg)
        threading.Thread(target=work, daemon=True).start()

    def do_apply():
        path = path_var.get().strip()
        state["path"] = path
        if not path:
            messagebox.showwarning("未选择文件", "请先指定 RadiumWMPF.bin")
            return
        if not messagebox.askyesno(
                "确认", "将关闭划线并应用本机验证过的离线组合。\n\n"
                        "仅支持已识别的 4.1.15.56 / RadiumWMPF 25799 文件基线。\n"
                        "微信及内置浏览器必须完全退出。修改前完整备份，失败自动回滚。\n"
                        "已有解压副本会同步；不运行常驻注入。\n\n"
                        "组件包：%s\n同目录 Weixin.dll 将一并处理。\n\n继续吗？" % os.path.abspath(path)):
            return
        set_busy(True, "写入中…")

        def work():
            try:
                ok, msg = apply_patch(path, log, ui_progress)
            except Exception as exc:                      # noqa: BLE001
                ok, msg = False, "操作异常：%s" % exc

            def done():
                set_busy(False)
                bar.configure(value=0)
                _write_log(msg)
                messagebox.showinfo("完成" if ok else "失败", msg)
                refresh()

            post(done)

        threading.Thread(target=work, daemon=True).start()

    def do_revert():
        path = path_var.get().strip()
        state["path"] = path
        if not path:
            messagebox.showwarning("未选择文件", "请先指定 RadiumWMPF.bin")
            return
        if not messagebox.askyesno("确认", "将还原划线开关、组件初始化及主程序定向比较补丁，\n并同步已识别的解压副本。保留主程序基线原有的其他修改。\n\n要求：微信及内置浏览器完全退出。修改前完整备份。\n\n组件包：%s\n同目录 Weixin.dll 将一并处理。\n\n继续吗？" % os.path.abspath(path)):
            return
        set_busy(True, "还原中…")

        def work():
            try:
                ok, msg = revert_patch(path, log, ui_progress)
            except Exception as exc:                      # noqa: BLE001
                ok, msg = False, "操作异常：%s" % exc

            def done():
                set_busy(False)
                bar.configure(value=0)
                _write_log(msg)
                messagebox.showinfo("完成" if ok else "失败", msg)
                refresh()

            post(done)

        threading.Thread(target=work, daemon=True).start()

    btn_apply.configure(command=do_apply)
    btn_revert.configure(command=do_revert)
    btn_refresh.configure(command=refresh)

    def on_close():
        if state["busy"]:
            messagebox.showwarning("操作进行中", "请等待当前检测、写入或还原完成后再关闭工具。")
            return
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    log("提示：修改前请完全退出微信，修改后重新启动微信生效。")
    drain()
    refresh()
    root.mainloop()


# ---------------------------------------------------------------- 命令行

def _ensure_console():
    """打包成无控制台窗口的 exe 后，命令行模式下 stdout 为 None，需要挂回控制台。"""
    if sys.stdout is not None:
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        if not kernel32.AttachConsole(-1):
            kernel32.AllocConsole()
        sys.stdout = open("CONOUT$", "w", encoding="utf-8", buffering=1)
        sys.stderr = open("CONOUT$", "w", encoding="utf-8", buffering=1)
    except Exception:                                     # noqa: BLE001
        pass


def run_cli(action, target, runtime=None):
    _ensure_console()
    if sys.stdout is None:
        return 2
    path = target or auto_target()
    if not path:
        print("未找到 RadiumWMPF.bin，请用 --target 指定")
        return 2
    print("目标: %s" % path)
    try:
        if action == "status":
            from offline_combo import inspect_combo
            report = inspect_combo(path, runtime=runtime)
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report["supported"] else 1
        if action in ("apply", "revert"):
            operation = apply_patch if action == "apply" else revert_patch
            ok, msg = operation(path, runtime=runtime)
            print(msg)
            return 0 if ok else 1
    except (OSError, ValueError, RuntimeError) as exc:
        print("操作失败：%s" % exc)
        return 1
    print("未知动作: %s" % action)
    return 2


def main():
    parser = argparse.ArgumentParser(description="微信划线开关：版本锁定离线组合")
    parser.add_argument("--version", action="version", version=APP_VERSION)
    parser.add_argument("--cli", choices=["status", "apply", "revert"],
                        help="命令行模式；apply/revert处理完整组合")
    parser.add_argument("--target", help="RadiumWMPF.bin 路径")
    parser.add_argument("--runtime", help="手动指定对应 extracted/runtime 目录，仅用于CLI")
    if sys.argv[1:]:
        _ensure_console()
    args = parser.parse_args()
    if args.runtime and not args.cli:
        parser.error("--runtime 必须与 --cli 一起使用")
    if args.cli:
        return run_cli(args.cli, args.target, args.runtime)
    run_gui()
    return 0


if __name__ == "__main__":
    sys.exit(main())
