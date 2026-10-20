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
    不写死文件偏移。每次都在目标文件里按特征串
    `support_mp_underline`（UTF-16LE / ASCII 双形态）重新定位，
    再解析其后的布尔字面量，因此微信升级换版本后依然可用。

用法：
    wx_underline_switch.py                 启动图形界面
    wx_underline_switch.py --cli status     命令行：查看状态
    wx_underline_switch.py --cli apply      命令行：关闭划线
    wx_underline_switch.py --cli revert     命令行：还原
    以上可附加 --target <RadiumWMPF.bin 路径>
"""

import argparse
import ctypes
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
            ["tasklist", "/FI", "IMAGENAME eq Weixin.exe", "/NH"],
            capture_output=True, timeout=20,
            encoding="mbcs", errors="replace")
    except (OSError, subprocess.SubprocessError):
        return None
    return "Weixin.exe" in (proc.stdout or "")


# ---------------------------------------------------------------- ZIP 完整性

# RadiumWMPF.bin 本身是一个 ZIP 包（46 个条目，全部 stored 不压缩），
# 第一个条目是 file_component.xml，带逐文件 md5 与 ECDSA 签名。
# 划线开关那条 JS 落在 runtime/flue.dll 条目里，所以改写之后：
#   ① 该条目的 CRC32 与本地文件头 / 中央目录里记的值对不上 —— 这一层可以重算修好；
#   ② file_component.xml 里 flue.dll 的 md5 也变了，而清单有签名 —— 这一层修不了。
# 这里只处理第 ① 层，让包至少是一个结构合法的 ZIP。

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

def apply_patch(path, log=print, progress=None):
    if not os.path.isfile(path):
        return False, "目标文件不存在"
    running = weixin_running()
    if running is None:
        return False, "无法确认微信是否在运行，已中止"
    if running:
        return False, "微信正在运行，请先完全退出微信（含托盘图标）"

    sites = scan_sites(path, progress)
    if not sites:
        return False, "未在文件中找到划线开关特征串，可能是新版本改了写法"
    todo = [s for s in sites if s[3] == "original"]
    if not todo:
        # 已经关闭，但 CRC 可能还是旧版本工具留下的失配值，补一遍
        fixed = 0
        for off, enc, width, state, literal in sites:
            if state != "patched":
                continue
            info = zip_refresh_crc(path, off, log)
            if info and info[3]:
                log("ZIP 条目 %s CRC32: 0x%08X -> 0x%08X（已写回）" % (info[0], info[1], info[2]))
                fixed += 1
        if fixed:
            return True, "已经是关闭状态，另修正了 %d 处 ZIP CRC" % fixed
        return True, "已经是关闭状态，无需操作"

    cfg = load_config()
    backups = cfg.setdefault("backups", {})
    entry = {
        "size": os.path.getsize(path),
        "sites": [],
        "sha1_before": sha1_of_file(path, progress),
    }
    with open(path, "r+b") as fh:
        for off, enc, width, state, literal in todo:
            if len(literal) < LITERAL_LEN:
                continue
            fh.seek(off)
            fh.write(PATCHED_LITERAL.encode(enc))
            entry["sites"].append({"offset": off, "enc": enc,
                                   "literal_before": literal})
            log("已改写 0x%X (%s) %r -> %r" % (off, enc, literal, PATCHED_LITERAL))
        fh.flush()
        os.fsync(fh.fileno())

    # 改写落在 ZIP 条目内部，条目 CRC32 已失配，重算写回（本地头 + 中央目录）
    for site in entry["sites"]:
        info = zip_refresh_crc(path, site["offset"], log)
        if info:
            name, old, new, changed = info
            log("ZIP 条目 %s CRC32: 0x%08X -> 0x%08X%s"
                % (name, old, new, "（已写回）" if changed else "（一致，无需改）"))

    entry["sha1_after"] = sha1_of_file(path)
    backups[path] = entry
    cfg["target"] = path
    save_config(cfg)
    log("原始 SHA1: %s" % entry["sha1_before"])
    log("改后 SHA1: %s" % entry["sha1_after"])
    return True, "已关闭划线，共改写 %d 处" % len(entry["sites"])


def revert_patch(path, log=print, progress=None):
    if not os.path.isfile(path):
        return False, "目标文件不存在"
    running = weixin_running()
    if running is None:
        return False, "无法确认微信是否在运行，已中止"
    if running:
        return False, "微信正在运行，请先完全退出微信（含托盘图标）"

    sites = scan_sites(path, progress)
    if not sites:
        return False, "未在文件中找到划线开关特征串"
    todo = [s for s in sites if s[3] == "patched" and len(s[4]) >= LITERAL_LEN]
    if not todo:
        return True, "当前已是原始状态，无需还原"

    with open(path, "r+b") as fh:
        for off, enc, width, state, literal in todo:
            fh.seek(off)
            fh.write(ORIGINAL_LITERAL.encode(enc))
            log("已还原 0x%X (%s) %r -> %r" % (off, enc, literal, ORIGINAL_LITERAL))
        fh.flush()
        os.fsync(fh.fileno())

    # 还原后数据回到原始状态，CRC32 重算应当正好回到原值
    for off, enc, width, state, literal in todo:
        info = zip_refresh_crc(path, off, log)
        if info:
            name, old, new, changed = info
            log("ZIP 条目 %s CRC32: 0x%08X -> 0x%08X%s"
                % (name, old, new, "（已写回）" if changed else "（一致，无需改）"))

    cfg = load_config()
    entry = cfg.get("backups", {}).get(path)
    after = sha1_of_file(path)
    if entry and entry.get("sha1_before"):
        log("还原后 SHA1: %s" % after)
        log("原始 SHA1  : %s" % entry["sha1_before"])
        log("一致性      : %s" % ("一致" if after == entry["sha1_before"] else "不一致"))
    return True, "已还原，共恢复 %d 处" % len(todo)


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

    root.title("微信划线开关")
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

    ttk.Button(path_row, text="浏览", width=8,
               command=browse).pack(side="left", padx=(px(6), 0))

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
        for b in (btn_apply, btn_revert, btn_refresh):
            b.configure(state="disabled" if flag else "normal")
        if hint:
            status_var.set(hint)

    def finish(summary, detail):
        status_var.set(summary)
        detail_var.set(detail)
        set_busy(False)
        bar.configure(value=0)

    def refresh():
        path = state["path"]
        if not path or not os.path.isfile(path):
            status_var.set("未找到 RadiumWMPF.bin")
            detail_var.set("请用「浏览」手动指定微信安装目录下的该文件")
            return
        set_busy(True, "扫描中…")
        log("扫描 %s" % path)

        def work():
            try:
                sites = scan_sites(path, ui_progress)
                summary = summarize(sites)
                size_mb = os.path.getsize(path) / 1048576.0
                detail = "%s ｜ %.1f MB ｜ 命中 %d 处" % (
                    os.path.basename(os.path.dirname(path)), size_mb, len(sites))
                for off, enc, width, st, lit in sites:
                    log("  0x%X  %-10s  %-8s  %r" % (off, enc, st, lit))
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
        path = state["path"]
        if not path:
            messagebox.showwarning("未选择文件", "请先指定 RadiumWMPF.bin")
            return
        if not messagebox.askyesno(
                "确认", "将修改微信内置浏览器运行时，关闭公众号文章里的划线显示。\n\n"
                        "要求：微信已完全退出。\n修改前会自动记录原始校验值，可随时还原。\n\n继续吗？"):
            return
        set_busy(True, "写入中…")

        def work():
            ok, msg = apply_patch(path, log, ui_progress)

            def done():
                set_busy(False)
                bar.configure(value=0)
                _write_log(msg)
                messagebox.showinfo("完成" if ok else "失败", msg)
                refresh()

            post(done)

        threading.Thread(target=work, daemon=True).start()

    def do_revert():
        path = state["path"]
        if not path:
            messagebox.showwarning("未选择文件", "请先指定 RadiumWMPF.bin")
            return
        if not messagebox.askyesno("确认", "将把划线开关恢复为原始状态。\n\n要求：微信已完全退出。\n\n继续吗？"):
            return
        set_busy(True, "还原中…")

        def work():
            ok, msg = revert_patch(path, log, ui_progress)

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


def run_cli(action, target):
    _ensure_console()
    if sys.stdout is None:
        return 2
    path = target or auto_target()
    if not path:
        print("未找到 RadiumWMPF.bin，请用 --target 指定")
        return 2
    print("目标: %s" % path)
    if action == "status":
        sites = scan_sites(path)
        print("状态: %s" % summarize(sites))
        for off, enc, width, st, lit in sites:
            print("  0x%X  %-10s  %-8s  %r" % (off, enc, st, lit))
        return 0
    if action == "apply":
        ok, msg = apply_patch(path)
        print(msg)
        return 0 if ok else 1
    if action == "revert":
        ok, msg = revert_patch(path)
        print(msg)
        return 0 if ok else 1
    print("未知动作: %s" % action)
    return 2


def main():
    parser = argparse.ArgumentParser(description="微信划线开关")
    parser.add_argument("--cli", choices=["status", "apply", "revert"],
                        help="命令行模式")
    parser.add_argument("--target", help="RadiumWMPF.bin 路径")
    args = parser.parse_args()
    if args.cli:
        return run_cli(args.cli, args.target)
    run_gui()
    return 0


if __name__ == "__main__":
    sys.exit(main())
