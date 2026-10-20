"""CRC 修复逻辑的离线自检：造一个 stored ZIP，改一个字节，修完必须通过 zipfile 校验。"""

import os
import sys
import tempfile
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import wx_underline_switch as tool  # noqa: E402


def build_zip(path):
    """造一个含 stored 大条目的 ZIP，模拟 RadiumWMPF.bin 的结构。"""
    payload = ("x" * 4096 + "WeixinJSBridge.support_mp_underline = true;" + "y" * 4096).encode("utf-8")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_STORED) as zf:
        zf.writestr("file_component.xml", b"<file_component/>")
        zf.writestr("runtime/flue.dll", payload)
    return payload


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    tmp = tempfile.mkdtemp(prefix="wxzip_")
    zp = os.path.join(tmp, "fake.bin")
    build_zip(zp)

    needle = b"support_mp_underline = true;"
    with open(zp, "rb") as fh:
        data = fh.read()
    off = data.find(needle)
    assert off > 0, "测试数据里没找到特征串"
    target = off + len(b"support_mp_underline = ")
    print("特征串位于文件偏移 0x%X" % off)

    # 1. 原始状态应当通过校验
    with zipfile.ZipFile(zp) as zf:
        assert zf.testzip() is None, "原始包就已损坏"
    print("[1] 原始包 zipfile.testzip() = None  OK")

    # 2. 改写 4 字节，模拟打补丁
    with open(zp, "r+b") as fh:
        fh.seek(target)
        fh.write(b"null")
    with zipfile.ZipFile(zp) as zf:
        bad = zf.testzip()
    print("[2] 改写后 zipfile.testzip() = %r  （预期就是坏的）" % bad)
    assert bad == "runtime/flue.dll", "预期 flue.dll 条目损坏，实际 %r" % bad

    # 3. 定位条目
    ent = tool.zip_locate_entry(zp, target)
    assert ent and ent["name"] == "runtime/flue.dll", "定位条目失败: %r" % ent
    print("[3] 定位到条目 %s，data_off=0x%X crc=0x%08X" % (ent["name"], ent["data_off"], ent["crc"]))

    # 4. 修 CRC
    logs = []
    info = tool.zip_refresh_crc(zp, target, log=logs.append)
    assert info, "zip_refresh_crc 返回 None"
    name, old, new, changed = info
    print("[4] CRC 修复: %s 0x%08X -> 0x%08X changed=%s" % (name, old, new, changed))
    assert changed, "CRC 应当发生变化"

    # 5. 修完必须重新合法
    with zipfile.ZipFile(zp) as zf:
        bad = zf.testzip()
        content = zf.read("runtime/flue.dll")
    assert bad is None, "修复后仍损坏: %r" % bad
    assert b"support_mp_underline = null;" in content, "补丁内容没写进去"
    print("[5] 修复后 zipfile.testzip() = None，且补丁内容在位  OK")

    # 6. 还原成 true，CRC 应自动回到原值
    with open(zp, "r+b") as fh:
        fh.seek(target)
        fh.write(b"true")
    info = tool.zip_refresh_crc(zp, target, log=logs.append)
    name, old, new, changed = info
    print("[6] 还原后 CRC 0x%08X -> 0x%08X changed=%s" % (old, new, changed))
    with zipfile.ZipFile(zp) as zf:
        assert zf.testzip() is None, "还原后损坏"
    orig_crc = None
    with zipfile.ZipFile(zp) as zf:
        for i in zf.infolist():
            if i.filename == "runtime/flue.dll":
                orig_crc = i.CRC
    print("[6] 还原后 CRC = 0x%08X（与原始一致即说明可无损回退）  OK" % orig_crc)

    print("\n全部通过")
    print("临时文件:", zp)


main()
