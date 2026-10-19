"""打印指定偏移附近的字符串上下文（ASCII / UTF-16LE 双视图，可打印化）。"""
import os
import sys

BASE = os.environ.get("WX_BASE") or \
    r"C:\Program Files\Tencent\Weixin\4.1.15.55"

JOBS = [
    ("Weixin.dll", 0x903F440, "ascii"),
    ("Weixin.dll", 0x90DC1E6, "ascii"),
    ("Weixin.dll", 0x90DC1FC, "ascii"),
    ("Weixin.dll", 0x90E47CF, "ascii"),
    ("Weixin.dll", 0x93CDE6C, "ascii"),
    ("Weixin.dll", 0x93E4721, "ascii"),
    ("Weixin.dll", 0x93EFABC, "ascii"),
    ("Weixin.dll", 0x93EFB0D, "ascii"),
    ("Weixin.dll", 0x93EFB25, "ascii"),
    ("Weixin.dll", 0x9668200, "ascii"),
    ("Weixin.dll", 0x8EDBFE7, "utf16"),
    ("Weixin.dll", 0x8EDCF2D, "utf16"),
    ("Weixin.dll", 0x8EDE747, "utf16"),
    ("Weixin.dll", 0xB2E9226, "utf8"),
    ("Weixin.dll", 0xB2E923E, "utf8"),
    ("Weixin.dll", 0xB2E9252, "utf8"),
    ("RadiumWMPF.bin", 0x9DF95E, "utf16"),
    ("RadiumWMPF.bin", 0xF07B0EA, "utf16"),
    ("RadiumWMPF.bin", 0xF7EEACA, "utf16"),
    ("RadiumWMPF.bin", 0xF6A3673, "ascii"),
    ("RadiumWMPF.bin", 0xF7BBE14, "ascii"),
    ("RadiumWMPF.bin", 0xF8091E4, "ascii"),
]

CTX = 160


def clean(text):
    out = []
    for ch in text:
        code = ord(ch)
        if code in (9, 10, 13) or 32 <= code < 127 or 0x4E00 <= code <= 0x9FFF:
            out.append(ch)
        else:
            out.append(".")
    return "".join(out)


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    cache = {}
    for name, off, mode in JOBS:
        if name not in cache:
            cache[name] = open(os.path.join(BASE, name), "rb")
        fh = cache[name]
        lo = max(0, off - CTX)
        fh.seek(lo)
        raw = fh.read(CTX * 2)
        if mode == "utf16":
            if lo % 2:
                lo += 1
                raw = raw[1:]
            txt = raw.decode("utf-16-le", "replace")
        elif mode == "utf8":
            txt = raw.decode("utf-8", "replace")
        else:
            txt = raw.decode("latin-1", "replace")
        print("### %s @0x%X (%s)" % (name, off, mode))
        print(clean(txt))
        print()
    for fh in cache.values():
        fh.close()


if __name__ == "__main__":
    main()
