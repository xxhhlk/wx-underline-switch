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
from ctypes import wintypes

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


# ---------------------------------------------------------------- 运行时注入

# 为什么需要这一节：
#   RadiumWMPF.bin 里的 flue.dll 带 Authenticode 签名，组件完整性校验
#   （ComponentVerification.dll，导入 WinVerifyTrust）会验它。改文件必坏签名，
#   而签名无法重签（私钥在腾讯），于是微信会弹「微信遇到问题，将影响网页正常浏览」。
#   改内存不动文件，签名/CRC/清单全部保持原样，弹窗就没有触发条件。
#
# 做法：轮询新出现的 WeChatAppEx.exe，找到已加载的 flue.dll 映像，
#       在映像内按特征串定位 `support_mp_underline = <x>;`，把字面量改成 null。

TARGET_EXE = "WeChatAppEx.exe"
TARGET_DLL = "flue.dll"

PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_VM_READ = 0x0010
PROCESS_VM_WRITE = 0x0020
PROCESS_VM_OPERATION = 0x0008
TH32CS_SNAPPROCESS = 0x00000002
TH32CS_SNAPMODULE = 0x00000008
TH32CS_SNAPMODULE32 = 0x00000010
PAGE_READWRITE = 0x04
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class _PROCESSENTRY32(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                ("szExeFile", ctypes.c_char * 260)]


class _MODULEENTRY32(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("th32ModuleID", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD), ("GlblcntUsage", wintypes.DWORD),
                ("ProccntUsage", wintypes.DWORD), ("modBaseAddr", ctypes.c_void_p),
                ("modBaseSize", wintypes.DWORD), ("hModule", wintypes.HMODULE),
                ("szModule", ctypes.c_char * 256), ("szExePath", ctypes.c_char * 260)]


class _MBI(ctypes.Structure):
    _fields_ = [("BaseAddress", ctypes.c_void_p), ("AllocationBase", ctypes.c_void_p),
                ("AllocationProtect", wintypes.DWORD), ("PartitionId", wintypes.WORD),
                ("RegionSize", ctypes.c_size_t), ("State", wintypes.DWORD),
                ("Protect", wintypes.DWORD), ("Type", wintypes.DWORD)]


_K32 = None
_INJECTED = set()          # 已经成功改写的 pid；pid 会被复用，退出后必须移除


def _k32():
    """按需初始化 kernel32 的函数签名（64 位下必须显式声明，否则句柄会被截断）。"""
    global _K32
    if _K32 is not None:
        return _K32
    if sys.platform != "win32":
        return None
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.OpenProcess.restype = wintypes.HANDLE
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    k.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k.Module32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(_MODULEENTRY32)]
    k.Module32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(_MODULEENTRY32)]
    k.Process32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32)]
    k.Process32Next.argtypes = [wintypes.HANDLE, ctypes.POINTER(_PROCESSENTRY32)]
    k.ReadProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                    ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
    k.ReadProcessMemory.restype = wintypes.BOOL
    k.WriteProcessMemory.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                     ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]
    k.WriteProcessMemory.restype = wintypes.BOOL
    k.VirtualProtectEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_size_t,
                                   wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    k.VirtualProtectEx.restype = wintypes.BOOL
    k.VirtualQueryEx.argtypes = [wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p,
                                 ctypes.c_size_t]
    k.VirtualQueryEx.restype = ctypes.c_size_t
    _K32 = k
    return k


def target_pids(exe=TARGET_EXE):
    k = _k32()
    if not k:
        return []
    snap = k.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == INVALID_HANDLE_VALUE or not snap:
        return []
    out = []
    pe = _PROCESSENTRY32()
    pe.dwSize = ctypes.sizeof(_PROCESSENTRY32)
    ok = k.Process32First(snap, ctypes.byref(pe))
    while ok:
        if pe.szExeFile.decode("mbcs", "replace").lower() == exe.lower():
            out.append(pe.th32ProcessID)
        ok = k.Process32Next(snap, ctypes.byref(pe))
    k.CloseHandle(snap)
    return out


def _modules(pid):
    k = _k32()
    snap = k.CreateToolhelp32Snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pid)
    if snap == INVALID_HANDLE_VALUE or not snap:
        return []
    me = _MODULEENTRY32()
    me.dwSize = ctypes.sizeof(_MODULEENTRY32)
    out = []
    ok = k.Module32First(snap, ctypes.byref(me))
    while ok:
        out.append((me.modBaseAddr, me.modBaseSize,
                    me.szExePath.decode("mbcs", "replace")))
        ok = k.Module32Next(snap, ctypes.byref(me))
    k.CloseHandle(snap)
    return out


def _read(h, addr, size):
    k = _k32()
    buf = ctypes.create_string_buffer(size)
    got = ctypes.c_size_t(0)
    if not k.ReadProcessMemory(h, ctypes.c_void_p(addr), buf, size, ctypes.byref(got)):
        return None
    return buf.raw[:got.value]


def _write(h, addr, data):
    """写入前临时把页面改成可写，写完还原原属性。返回 (是否成功, 说明)。"""
    k = _k32()
    mbi = _MBI()
    if not k.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(_MBI)):
        return False, "VirtualQueryEx 失败"
    old_protect = mbi.Protect

    changed = wintypes.DWORD(0)
    need_restore = old_protect != PAGE_READWRITE
    if need_restore:
        if not k.VirtualProtectEx(h, ctypes.c_void_p(addr), len(data),
                                  PAGE_READWRITE, ctypes.byref(changed)):
            return False, "VirtualProtectEx 失败 err=%d" % ctypes.get_last_error()

    written = ctypes.c_size_t(0)
    buf = ctypes.create_string_buffer(data, len(data))
    ok = k.WriteProcessMemory(h, ctypes.c_void_p(addr), buf, len(data),
                              ctypes.byref(written))

    if need_restore:
        back = wintypes.DWORD(0)
        k.VirtualProtectEx(h, ctypes.c_void_p(addr), len(data),
                           old_protect, ctypes.byref(back))
    if not ok:
        return False, "WriteProcessMemory 失败 err=%d" % ctypes.get_last_error()
    return True, "写入 %d 字节" % written.value


def _module_sections(h, base):
    """读 PE 节表，返回 [(名字, 虚拟偏移, 虚拟大小)]。"""
    head = _read(h, base, 0x1000)
    if not head or len(head) < 0x40:
        return []
    e_lfanew = struct.unpack_from("<I", head, 0x3C)[0]
    if e_lfanew + 24 > len(head) or head[e_lfanew:e_lfanew + 4] != b"PE\x00\x00":
        return []
    nsec = struct.unpack_from("<H", head, e_lfanew + 6)[0]
    opt_size = struct.unpack_from("<H", head, e_lfanew + 20)[0]
    sec_off = e_lfanew + 24 + opt_size
    need = sec_off + nsec * 40
    if need > len(head):
        more = _read(h, base + len(head), need - len(head))
        if not more:
            return []
        head += more
    if nsec <= 0 or nsec > 96:
        return []
    secs = []
    for i in range(nsec):
        o = sec_off + i * 40
        name = head[o:o + 8].rstrip(b"\x00").decode("ascii", "replace")
        vsize, vaddr = struct.unpack_from("<II", head, o + 8)
        secs.append((name, vaddr, vsize))
    return secs


def find_literal_in_process(h, base):
    """在已加载的 flue.dll 映像里定位划线开关字面量。

    返回 (绝对地址, 节名, 字面量)；找不到返回 (None, 说明, None)。
    """
    secs = _module_sections(h, base)
    if not secs:
        return None, "无法解析节表", None

    needle = NEEDLE.encode("utf-16-le")
    lit_off = len("support_mp_underline = ") * 2
    chunk = 4 * 1024 * 1024

    for name, vaddr, vsize in secs:
        if vsize <= 0:
            continue
        pos = 0
        tail = b""
        while pos < vsize:
            n = min(chunk, vsize - pos)
            d = _read(h, base + vaddr + pos, n)
            if not d:
                break
            buf = tail + d
            i = buf.find(needle)
            if i != -1:
                rel = pos - len(tail) + i
                addr = base + vaddr + rel
                raw = _read(h, addr + lit_off, 8) or b""
                lit = raw.decode("utf-16-le", errors="replace").rstrip("\x00")
                return addr + lit_off, name, lit
            tail = buf[-len(needle):]
            pos += n
    return None, "映像中未找到特征串", None


def poke_process(pid, log=print, dry_run=False):
    """对单个进程做一次内存改写。返回 True 表示该进程已是关闭状态。"""
    k = _k32()
    mods = _modules(pid)
    hit = None
    for base, size, path in mods:
        if path.lower().endswith(TARGET_DLL):
            hit = (base, size, path)
            break
    if not hit:
        return False
    base, size, path = hit

    h = k.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ
                      | PROCESS_VM_WRITE | PROCESS_VM_OPERATION, False, pid)
    if not h:
        return False
    try:
        addr, sec, lit = find_literal_in_process(h, base)
        if not addr:
            return False
        if lit == PATCHED_LITERAL:
            _INJECTED.add(pid)
            return True
        if lit != ORIGINAL_LITERAL:
            log("pid=%d 字面量既不是 %r 也不是 %r，跳过（%r）"
                % (pid, ORIGINAL_LITERAL, PATCHED_LITERAL, lit))
            return False
        if dry_run:
            log("pid=%d 找到 %s@0x%X 字面量=%r（dry-run，未写入）"
                % (pid, sec, addr - base, lit))
            return False
        ok, why = _write(h, addr, PATCHED_LITERAL.encode("utf-16-le"))
        if ok:
            _INJECTED.add(pid)
            log("pid=%d %s.%s@0x%X  %r -> %r" % (pid, sec, TARGET_DLL,
                                                 addr - base, lit, PATCHED_LITERAL))
        else:
            log("pid=%d 写入失败: %s" % (pid, why))
        return ok
    finally:
        k.CloseHandle(h)


def watch_loop(stop_event, log=print, interval=0.2, dry_run=False):
    """轮询新出现的网页进程并改写内存，直到 stop_event 被设置。"""
    if not _k32():
        log("运行时注入仅支持 Windows")
        return
    log("运行时注入已启动：每 %.1fs 检查一次新的 %s 进程" % (interval, TARGET_EXE))
    if dry_run:
        log("（dry-run 模式：只定位不写入）")
    while not stop_event.is_set():
        try:
            alive = set()
            for pid in target_pids():
                alive.add(pid)
                if pid in _INJECTED:
                    continue
                try:
                    poke_process(pid, log, dry_run)
                except Exception as exc:                  # noqa: BLE001
                    log("pid=%d 处理异常: %s" % (pid, exc))
            # 进程退出后从已处理集合里移除；pid 会被复用，必须清理
            # 用方法调用而非 &= 赋值，避免被当成局部变量重绑定
            _INJECTED.intersection_update(alive)
        except Exception as exc:                          # noqa: BLE001
            log("轮询异常: %s" % exc)
        stop_event.wait(interval)
    log("运行时注入已停止")


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
    btn_apply = ttk.Button(btn_row, text="关闭划线（改文件）")
    btn_apply.pack(side="left")
    btn_revert = ttk.Button(btn_row, text="还原")
    btn_revert.pack(side="left", padx=px(8))
    btn_refresh = ttk.Button(btn_row, text="重新检测")
    btn_refresh.pack(side="left")
    ttk.Button(btn_row, text="打开配置文件目录",
               command=lambda: os.startfile(config_dir())).pack(side="right")

    inject_row = ttk.Frame(outer)
    inject_row.pack(fill="x", pady=(0, px(10)))
    btn_inject = ttk.Button(inject_row, text="内存注入（关一次，不改文件）")
    btn_inject.pack(side="left")
    btn_watch = ttk.Button(inject_row, text="开启常驻注入")
    btn_watch.pack(side="left", padx=px(8))
    inject_var = tk.StringVar(value="常驻注入：未运行")
    ttk.Label(inject_row, textvariable=inject_var,
              foreground="#555").pack(side="left", padx=px(6))

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

    # ---- 运行时注入 ----

    inject_state = {"stop": None, "thread": None}

    def do_inject():
        set_busy(True, "注入中…")

        def work():
            pids = target_pids()
            if not pids:
                post(lambda: (set_busy(False),
                              _write_log("没有正在运行的 %s 进程" % TARGET_EXE),
                              messagebox.showwarning(
                                  "未找到进程",
                                  "微信没有在运行，或还没有打开过公众号文章。\n\n"
                                  "内存注入要求微信正在运行。")))
                return
            log("找到 %d 个 %s 进程" % (len(pids), TARGET_EXE))
            done = 0
            for pid in pids:
                if poke_process(pid, log):
                    done += 1

            def fin():
                set_busy(False)
                bar.configure(value=0)
                msg = "已在 %d / %d 个进程中关闭划线" % (done, len(pids))
                _write_log(msg)
                if done:
                    messagebox.showinfo("完成", msg)
                else:
                    messagebox.showwarning(
                        "未注入成功", msg + "\n\n可能原因：flue.dll 尚未加载，稍后再试。")

            post(fin)

        threading.Thread(target=work, daemon=True).start()

    def toggle_watch():
        if inject_state["thread"] is not None:
            inject_state["stop"].set()
            inject_state["thread"] = None
            inject_state["stop"] = None
            btn_watch.configure(text="开启常驻注入")
            inject_var.set("常驻注入：未运行")
            return
        stop = threading.Event()
        inject_state["stop"] = stop
        inject_state["thread"] = threading.Thread(
            target=watch_loop, args=(stop, log), daemon=True)
        inject_state["thread"].start()
        btn_watch.configure(text="停止常驻注入")
        inject_var.set("常驻注入：运行中")

    btn_inject.configure(command=do_inject)
    btn_watch.configure(command=toggle_watch)

    def on_close():
        if inject_state["stop"] is not None:
            inject_state["stop"].set()
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)

    log("提示：改文件模式需先完全退出微信；内存注入模式需微信正在运行。")
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


def run_cli(action, target, dry_run=False):
    _ensure_console()
    if sys.stdout is None:
        return 2

    # 内存注入不涉及文件，不需要 target
    if action == "inject":
        pids = target_pids()
        if not pids:
            print("没有正在运行的 %s 进程（微信未启动或没有打开过网页）" % TARGET_EXE)
            return 1
        print("找到 %d 个 %s 进程" % (len(pids), TARGET_EXE))
        done = 0
        for pid in pids:
            if poke_process(pid, print, dry_run):
                done += 1
        print("已在 %d/%d 个进程中关闭划线" % (done, len(pids)))
        return 0 if done else 1

    if action == "watch":
        stop = threading.Event()
        try:
            watch_loop(stop, print, dry_run=dry_run)
        except KeyboardInterrupt:
            print("\n收到中断，退出")
        finally:
            stop.set()
        return 0

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
    parser.add_argument("--cli",
                        choices=["status", "apply", "revert", "inject", "watch"],
                        help="命令行模式；inject/watch 走运行时内存注入，不改文件")
    parser.add_argument("--target", help="RadiumWMPF.bin 路径")
    parser.add_argument("--dry-run", action="store_true",
                        help="只定位不写入（配合 --cli inject / watch）")
    args = parser.parse_args()
    if args.cli:
        return run_cli(args.cli, args.target, args.dry_run)
    run_gui()
    return 0


if __name__ == "__main__":
    sys.exit(main())
