"""目标定向字符串侦察：在微信二进制里定位划线相关符号。

只用标准库。大文件走 mmap，按 ASCII / UTF-16LE 双形态扫描，命中后打印偏移与上下文。
"""
import mmap
import os
import sys

BASE = os.environ.get("WX_BASE") or \
    r"C:\Program Files\Tencent\Weixin\4.1.15.55"

TARGETS = [
    "Weixin.dll",
    "RadiumWMPF.bin",
    "XNet.dll",
    "ilink2.dll",
    "mmcronet.dll",
    "wxpublic.dll",
]

NEEDLES = [
    "support_mp_underline",
    "get_appmsg_segment",
    "underline_version",
    "underline_anchor",
    "wx_text_underline",
    "wappoc_appmsgcaptcha",
    "underline_container",
    "underline-edu",
]

MAX_HITS_PER_NEEDLE = 12
CTX = 96


def scan(path):
    out = []
    try:
        fh = open(path, "rb")
    except OSError as exc:
        return ["OPEN-FAIL %s: %s" % (path, exc)]
    size = os.path.getsize(path)
    try:
        mm = mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ)
    except (ValueError, OSError) as exc:
        fh.close()
        return ["MMAP-FAIL %s: %s" % (path, exc)]
    try:
        for needle in NEEDLES:
            for enc, tag in (("ascii", "A"), ("utf-16-le", "W")):
                try:
                    pat = needle.encode(enc)
                except UnicodeEncodeError:
                    continue
                hits = 0
                pos = 0
                while hits < MAX_HITS_PER_NEEDLE:
                    pos = mm.find(pat, pos)
                    if pos < 0:
                        break
                    hits += 1
                    lo = max(0, pos - CTX)
                    hi = min(size, pos + len(pat) + CTX)
                    raw = mm[lo:hi]
                    if tag == "W":
                        ctx = raw.decode("utf-16-le", "replace")
                    else:
                        ctx = raw.decode("latin-1", "replace")
                    ctx = "".join(c if 32 <= ord(c) < 127 else "." for c in ctx)
                    out.append("%s | %s | %s | off=0x%X | %s"
                               % (os.path.basename(path), tag, needle, pos, ctx))
                    pos += len(pat)
    finally:
        mm.close()
        fh.close()
    return out


def main():
    lines = []
    for name in TARGETS:
        path = os.path.join(BASE, name)
        if not os.path.exists(path):
            lines.append("MISSING %s" % name)
            continue
        lines.append("### %s (%.1f MB)" % (name, os.path.getsize(path) / 1048576.0))
        lines.extend(scan(path))
        lines.append("")
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
