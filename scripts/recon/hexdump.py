"""hexdump RadiumWMPF.bin 注入 JS 附近，确认精确字节。"""
import os
import sys

BASE = os.environ.get("WX_BASE") or \
    r"C:\Program Files\Tencent\Weixin\4.1.15.55"
PATH = os.path.join(BASE, "RadiumWMPF.bin")
ANCHOR = 0xF07B0D4
BACK = 64
FWD = 160


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    lo = ANCHOR - BACK
    if lo % 2:
        lo += 1
    with open(PATH, "rb") as fh:
        fh.seek(lo)
        raw = fh.read(BACK + FWD)
    print("=== 0x%X 起 %d 字节 ===" % (lo, len(raw)))
    for i in range(0, len(raw), 16):
        chunk = raw[i:i + 16]
        hexs = " ".join("%02X" % b for b in chunk)
        txt = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        print("%08X  %-47s  %s" % (lo + i, hexs, txt))
    print()
    print("=== 同一区域 UTF-16LE 解码 ===")
    text = raw.decode("utf-16-le", "replace")
    print("".join(c if 32 <= ord(c) < 127 else "." for c in text))
    print()
    print("=== 定位 '= true;' 的字节偏移 ===")
    for needle in ("= true;", "true", "support_mp_underline"):
        pat = needle.encode("utf-16-le")
        idx = raw.find(pat)
        print("%-22s -> %s" % (needle, ("0x%X" % (lo + idx)) if idx >= 0 else "not found"))


if __name__ == "__main__":
    main()
