"""微信二进制划线相关符号侦察（分块流式扫描，避免大文件占满内存）。"""
import os
import sys

BASE = os.environ.get("WX_BASE") or \
    r"C:\Program Files\Tencent\Weixin\4.1.15.55"

TARGETS = ["Weixin.dll", "RadiumWMPF.bin", "XNet.dll", "ilink2.dll", "wxpublic.dll"]

WORDS = ["underline", "划线"]
SHOW = 10
CHUNK = 16 * 1024 * 1024


def variants(word):
    out = []
    for enc in ("ascii", "utf-16-le", "utf-8"):
        try:
            out.append((enc, word.encode(enc).lower()))
        except UnicodeEncodeError:
            continue
    return out


def scan_stream(path, pat):
    total = 0
    offs = []
    overlap = max(len(pat) - 1, 1)
    consumed = 0
    prev = b""
    with open(path, "rb") as fh:
        while True:
            buf = fh.read(CHUNK)
            if not buf:
                break
            data = prev + buf
            data_low = data.lower()
            data_start = consumed - len(prev)
            start = 0
            while True:
                idx = data_low.find(pat, start)
                if idx < 0:
                    break
                total += 1
                if len(offs) < SHOW:
                    offs.append(data_start + idx)
                start = idx + len(pat)
            prev = data[-overlap:]
            consumed += len(buf)
    return total, offs


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    for name in TARGETS:
        path = os.path.join(BASE, name)
        if not os.path.exists(path):
            print("MISSING %s" % name)
            continue
        print("### %s (%.1f MB)" % (name, os.path.getsize(path) / 1048576.0))
        for word in WORDS:
            for enc, pat in variants(word):
                total, offs = scan_stream(path, pat)
                if total:
                    print("  %-12s %-10s total=%-6d first=%s"
                          % (word, enc, total,
                             ", ".join("0x%X" % o for o in offs)))
        print()


if __name__ == "__main__":
    main()
