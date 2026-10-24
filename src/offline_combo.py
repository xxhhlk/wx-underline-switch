"""版本锁定的离线组合补丁与文件事务；仅使用标准库。"""
import csv
import hashlib
import json
import mmap
import os
from pathlib import Path
import shutil
import stat
import struct
import subprocess
import tempfile
import uuid
import zipfile
import zlib

PACKAGE = {"original": "df6e79f846c818ea5c625da09e434fef5f0dead6",
           "basic": "fa514c5befc8c8fa9cb7f088ecfb5fe9a72e38d0",
           "combo": "c769afeec1325cf0fc497d1e0d6892c8f023f1b7"}
DLL = {"original": "2f43ada7d9119288c29ac0f262a034addc5ea520",
       "combo": "22b3c7e74fa99203885af5eec2a51980b8917a15"}
FLUE = {"original": "ea866b69ae43732d30fd49628a8ca76fc6c80d96",
        "basic": "7a5a0e49658e18369f5ca8670deaae710e270e50",
        "combo": "d9525d8b7d3f6f720924a5e641616ef085f07587"}
ENTRY = "runtime/flue.dll"
CALL_RAW = 0x25F50A8
CALL = bytes.fromhex("e8f3e43000")
CONTEXT = bytes.fromhex("488d0dc8ff660b")
FOLLOWING = bytes.fromhex("4c89e1e87bc2ffff")
CALL_RVA, COMPARE_RVA, CAVE_RVA = 0xB3CC29, 0x22F690, 0x771D100
TEXT_RVA, TEXT_RAW = 0x1000, 0x400
OLD_VSIZE, NEW_VSIZE = 0x771C0F7, 0x771C200
ORIGINAL_CALL = bytes.fromhex("e8622a6fff")
RUNTIME_REL = Path("Tencent/xwechat/xplugin/plugins/RadiumWMPF/25799/extracted/runtime")
WATCH = ("flue.dll", "icudtl.dat", "wmpf_resources.pak", "wmpf_100_percent.pak",
         "wmpf_200_percent.pak", "locales/zh-CN.pak")


def _hashes(path):
    one, two = hashlib.sha1(), hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            one.update(block)
            two.update(block)
    return {"sha1": one.hexdigest(), "sha256": two.hexdigest()}


def _safe(path, kind=None):
    path = Path(os.path.abspath(os.fspath(path)))
    for part in [*reversed(path.parents), path]:
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("拒绝符号链接或重解析路径：" + str(part))
        if part != path and not stat.S_ISDIR(info.st_mode):
            raise ValueError("父路径不是目录：" + str(part))
    if kind == "file" and not path.is_file():
        raise ValueError("文件不存在或类型不正确：" + str(path))
    if kind == "file" and path.stat().st_nlink != 1:
        raise ValueError("拒绝有多个硬链接的目标文件：" + str(path))
    if kind == "dir" and not path.is_dir():
        raise ValueError("目录不存在或类型不正确：" + str(path))
    return path


def _runtime(package, runtime):
    if runtime is not None:
        return _safe(runtime)
    parts = package.parts
    if (package.name.lower() != "radiumwmpf.bin" or package.parent.name != "4.1.15.56"
            or package.parent.parent.name.lower() != "weixin"):
        raise ValueError("无法绑定自动缓存路径，请用 --runtime 明确指定对应目录")
    drives = [index for index, part in enumerate(parts) if part.lower() == "drive"]
    if len(drives) > 1:
        raise ValueError("沙盒路径不明确，请用 --runtime 指定")
    if drives:
        root = Path(*parts[:drives[0]])
        return _safe(root / "user/current/AppData/Roaming" / RUNTIME_REL)
    appdata = os.environ.get("APPDATA")
    if not appdata:
        raise ValueError("未设置 APPDATA，请显式指定 runtime 目录")
    return _safe(Path(appdata) / RUNTIME_REL)


def inspect_combo(package, runtime=None):
    report = {"supported": False, "summary": "不支持此组合", "package_state": "unknown",
              "dll_state": "unknown", "runtime_state": "unknown", "runtime_path": None}
    try:
        package = _safe(package, "file")
        dll = _safe(package.parent / "Weixin.dll", "file")
        runtime = _runtime(package, runtime)
        report.update(package_path=str(package), dll_path=str(dll), runtime_path=str(runtime))
        for name, path, known in (("package", package, PACKAGE), ("dll", dll, DLL)):
            hashes = _hashes(path)
            report.update({name + "_" + key: value for key, value in hashes.items()})
            report[name + "_state"] = next((state for state, value in known.items()
                                                   if value == hashes["sha1"]), "unknown")
        try:
            runtime.lstat()
            runtime_exists = True
        except FileNotFoundError:
            runtime_exists = False
        if runtime_exists:
            _safe(runtime, "dir")
            flue = _safe(runtime / "flue.dll", "file")
            hashes = _hashes(flue)
            report.update({"runtime_" + key: value for key, value in hashes.items()})
            report["runtime_state"] = next((state for state, value in FLUE.items()
                                             if value == hashes["sha1"]), "unknown")
            if report["package_state"] != "unknown":
                with zipfile.ZipFile(package) as archive:
                    for name in WATCH:
                        resource = _safe(runtime / name, "file")
                        expected_size = archive.getinfo("runtime/" + name).file_size
                        with resource.open("rb") as stream:
                            if not stream.read(1) or resource.stat().st_size != expected_size:
                                raise ValueError("现有缓存资源大小异常：" + str(resource))
                report["resources_readable"] = True
        else:
            report["runtime_state"] = "missing"
        report["supported"] = all(report[key] not in ("unknown", "invalid")
                                  for key in ("package_state", "dll_state", "runtime_state"))
        if report["supported"]:
            report["summary"] = "支持此版本组合；组件包：%s，Weixin.dll：%s，缓存：%s" % (
                report["package_state"], report["dll_state"], report["runtime_state"])
    except (OSError, ValueError, KeyError, zipfile.BadZipFile) as error:
        report["summary"] = str(error)
    return report


def _payload():
    out, branches = bytearray(), []
    def emit(value):
        out.extend(bytes.fromhex(value))
    def reject():
        emit("0f85")
        branches.append(len(out))
        emit("00000000")
    emit("4883791020")
    reject()
    emit("48837a1020")
    reject()
    for code in ("4883791820 0f82", "48837a1820 0f82"):
        emit(code)
        branches.append(len(out))
        emit("00000000")
    emit("4c8b01 4c8b0a")
    for reg, value in ((8, b"966e57b3d3af82572918113c43ce185d"),
                       (9, b"b256f0a03bf1bc8613af8e860b62717c")):
        for offset in range(0, 32, 8):
            emit("48b8")
            out.extend(value[offset:offset + 8])
            out.extend(bytes((0x49, 0x39, 0x40 if reg == 8 else 0x41, offset)))
            reject()
    emit("b801000000 c3")
    fallback = len(out)
    emit("e9")
    out.extend(struct.pack("<i", COMPARE_RVA - (CAVE_RVA + len(out) + 4)))
    for offset in branches:
        struct.pack_into("<i", out, offset, fallback - offset - 4)
    if len(out) > 0x100:
        raise ValueError("候选指令超出已验证填充区")
    return bytes(out)


def _locations(data):
    peoff = struct.unpack_from("<I", data, 0x3C)[0]
    if data[peoff:peoff + 4] != b"PE\0\0":
        raise ValueError("不是 PE 文件")
    count = struct.unpack_from("<H", data, peoff + 6)[0]
    optlen = struct.unpack_from("<H", data, peoff + 20)[0]
    for index in range(count):
        offset = peoff + 24 + optlen + index * 40
        if data[offset:offset + 8].rstrip(b"\0") == b".text":
            vsize, rva, rawsize, raw = struct.unpack_from("<IIII", data, offset + 8)
            if (rva, raw, rawsize) != (TEXT_RVA, TEXT_RAW, NEW_VSIZE):
                raise ValueError(".text 映射不匹配")
            return offset + 8, vsize
    raise ValueError("缺少 .text 节")


def _entry(path):
    with zipfile.ZipFile(path) as archive:
        matches = [info for info in archive.infolist() if info.filename == ENTRY]
        if len(matches) != 1:
            raise ValueError("组件包必须恰含一个 runtime/flue.dll")
        info = matches[0]
        if info.compress_type != zipfile.ZIP_STORED or info.flag_bits & 9:
            raise ValueError("组件条目存储方式不受支持")
        central = archive.start_dir
    with open(path, "rb") as stream:
        stream.seek(info.header_offset)
        header = stream.read(30)
        if header[:4] != b"PK\x03\x04":
            raise ValueError("ZIP 本地头无效")
        nlen, elen = struct.unpack_from("<HH", header, 26)
        start = info.header_offset + 30 + nlen + elen
        stream.seek(central)
        while True:
            offset = stream.tell()
            header = stream.read(46)
            if header[:4] != b"PK\x01\x02":
                raise ValueError("ZIP 中央目录缺少目标条目")
            nlen, elen, clen = struct.unpack_from("<HHH", header, 28)
            local = struct.unpack_from("<I", header, 42)[0]
            if local == info.header_offset:
                return start, info.file_size, info.header_offset + 14, offset + 16
            stream.seek(nlen + elen + clen, 1)


def _transform(path, kind, state, target):
    start, size, local_crc, central_crc = _entry(path) if kind == "package" else (0, path.stat().st_size, 0, 0)
    with open(path, "r+b") as stream, mmap.mmap(stream.fileno(), 0) as data:
        if kind == "dll":
            vsize_off, vsize = _locations(data)
            call_off, cave_off = TEXT_RAW + CALL_RVA - TEXT_RVA, TEXT_RAW + CAVE_RVA - TEXT_RVA
            code = _payload()
            call = b"\xe8" + struct.pack("<i", CAVE_RVA - CALL_RVA - 5)
            if (vsize != (OLD_VSIZE if state == "original" else NEW_VSIZE)
                    or data[call_off:call_off + 5] != (ORIGINAL_CALL if state == "original" else call)
                    or data[cave_off:cave_off + 0x100] != (b"\xcc" * 0x100 if state == "original"
                                                                         else code + b"\xcc" * (0x100 - len(code)))):
                raise ValueError("Weixin.dll 指令或填充区不匹配")
            data[call_off:call_off + 5] = ORIGINAL_CALL if target == "original" else call
            data[cave_off:cave_off + len(code)] = b"\xcc" * len(code) if target == "original" else code
            struct.pack_into("<I", data, vsize_off, OLD_VSIZE if target == "original" else NEW_VSIZE)
        else:
            old = ("support_mp_underline = " + ("true" if state == "original" else "null")).encode("utf-16-le")
            new = ("support_mp_underline = " + ("true" if target == "original" else "null")).encode("utf-16-le")
            literal = data.find(old, start, start + size)
            if literal < 0 or data.find(old, literal + len(old), start + size) >= 0:
                raise ValueError("划线字面量不唯一或缺失")
            site = start + CALL_RAW
            if (data[site:site + 5] != (b"\x90" * 5 if state == "combo" else CALL)
                    or data[site - len(CONTEXT):site] != CONTEXT
                    or data[site + 5:site + 5 + len(FOLLOWING)] != FOLLOWING):
                raise ValueError("flue.dll 指令上下文不匹配")
            data[literal:literal + len(old)] = new
            data[site:site + 5] = CALL if target == "original" else b"\x90" * 5
            if kind == "package":
                crc = 0
                for offset in range(start, start + size, 8 * 1024 * 1024):
                    crc = zlib.crc32(data[offset:min(offset + 8 * 1024 * 1024, start + size)], crc)
                for offset in (local_crc, central_crc):
                    struct.pack_into("<I", data, offset, crc & 0xFFFFFFFF)
        data.flush()
        os.fsync(stream.fileno())


def _process_check():
    try:
        result = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True,
                                timeout=20, encoding="mbcs", errors="replace",
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode or not result.stdout.strip():
            return None
        names = ("weixin.exe", "wechat.exe", "wechatappex.exe")
        rows = list(csv.reader(result.stdout.splitlines(), strict=True))
        if any(len(row) < 2 or not row[1].isdigit() for row in rows):
            return None
        return any(row[0].lower() in names for row in rows)
    except (OSError, LookupError, csv.Error, subprocess.SubprocessError):
        return None


def _guard(check):
    if check() is not False:
        raise ValueError("微信可能正在运行或进程查询失败，请完全退出微信后重试")


def _save(path, receipt):
    descriptor, stage = tempfile.mkstemp(prefix=".receipt-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(receipt, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(stage, path)
    finally:
        if os.path.exists(stage):
            os.unlink(stage)


def _pending(root):
    for directory in root.iterdir():
        _safe(directory)
        if directory.is_dir():
            journal = _safe(directory / "journal.json")
            if journal.exists():
                with journal.open(encoding="utf-8") as stream:
                    receipt = json.load(stream)
                if receipt.get("status") not in ("committed", "rolled_back"):
                    raise ValueError("存在未完成事务，请先检查备份恢复：" + str(journal))


def _stage_copy(source, destination):
    _safe(destination, "file")
    descriptor, name = tempfile.mkstemp(prefix=".wxcombo-", suffix=".stage", dir=destination.parent)
    os.close(descriptor)
    try:
        shutil.copyfile(source, name)
        with open(name, "r+b") as stream:
            stream.flush()
            os.fsync(stream.fileno())
        return Path(name)
    except BaseException:
        os.unlink(name)
        raise


def change_combo(package, restore=False, runtime=None, backup_root=None, log=print, process_check=None):
    receipt, journal, stages, lock = None, None, [], None
    check = process_check if process_check is not None else _process_check
    def emit(message):
        try:
            log(message)
        except Exception:
            pass
    try:
        _guard(check)
        report = inspect_combo(package, runtime)
        if not report["supported"]:
            raise ValueError("不支持修改：" + report["summary"])
        if backup_root is None:
            local = os.environ.get("LOCALAPPDATA")
            if not local:
                raise ValueError("未设置 LOCALAPPDATA，请显式指定备份目录")
            backup_root = Path(local) / "WxUnderlineSwitch/transactions"
        root = _safe(backup_root)
        root.mkdir(parents=True, exist_ok=True)
        _safe(root, "dir")
        lock_path = _safe(root / "transaction.lock")
        try:
            with lock_path.open("xb") as stream:
                lock = lock_path
                stream.write(str(os.getpid()).encode("ascii"))
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            raise ValueError("存在事务锁，可能有未完成事务，请先检查备份目录：" + str(root))
        _pending(root)
        target = "original" if restore else "combo"
        todo = []
        for kind, prefix, known in (("dll", "dll", DLL), ("package", "package", PACKAGE),
                                    ("flue", "runtime", FLUE)):
            state = report[prefix + "_state"]
            if state in (target, "missing"):
                continue
            path = Path(report[prefix + "_path"]) if kind != "flue" else Path(report["runtime_path"]) / "flue.dll"
            todo.append({"kind": kind, "path": str(path), "state": state,
                         "before_sha256": report[prefix + "_sha256"], "after_sha1": known[target],
                         "status": "pending"})
        if not todo:
            return True, "已处于目标状态，无需修改" + ("；缓存尚未解压，等待微信自解压" if report["runtime_state"] == "missing" else "")
        directory = root / uuid.uuid4().hex
        directory.mkdir()
        journal = directory / "journal.json"
        receipt = {"status": "preparing", "restore": bool(restore), "files": todo}
        _save(journal, receipt)
        for index, item in enumerate(todo):
            path = _safe(item["path"], "file")
            backup = directory / (str(index) + ".backup")
            with path.open("rb") as source, backup.open("xb") as output:
                shutil.copyfileobj(source, output, 8 * 1024 * 1024)
                output.flush()
                os.fsync(output.fileno())
            item["backup"] = str(backup)
            if _hashes(backup)["sha256"] != item["before_sha256"]:
                raise ValueError("备份回读校验失败：" + str(path))
            _save(journal, receipt)
            stage = _stage_copy(backup, path)
            stages.append(stage)
            _transform(stage, item["kind"], item["state"], target)
            hashes = _hashes(stage)
            if hashes["sha1"] != item["after_sha1"]:
                raise ValueError("候选完整文件哈希不匹配：" + str(path))
            item.update(stage=str(stage), after_sha256=hashes["sha256"])
        receipt["status"] = "prepared"
        _save(journal, receipt)
        _guard(check)
        second = inspect_combo(package, runtime)
        if not second["supported"] or any(second.get(key) != report.get(key) for key in
                ("package_sha256", "dll_sha256", "runtime_sha256", "runtime_state", "runtime_path")):
            raise ValueError("准备期间目标文件或缓存状态发生变化")
        # All rollback material must still be valid before the first target write.
        for item in todo:
            backup = _safe(item["backup"], "file")
            stage = _safe(item["stage"], "file")
            if _hashes(backup)["sha256"] != item["before_sha256"]:
                raise ValueError("写入前备份文件已变化：" + str(backup))
            if _hashes(stage)["sha256"] != item["after_sha256"]:
                raise ValueError("写入前候选文件已变化：" + str(stage))
        for item in todo:
            path = _safe(item["path"], "file")
            if _hashes(path)["sha256"] != item["before_sha256"]:
                raise ValueError("写入前目标文件已变化：" + str(path))
            stage = _safe(item["stage"], "file")
            if _hashes(stage)["sha256"] != item["after_sha256"]:
                raise ValueError("候选暂存文件发生变化：" + str(stage))
            _guard(check)
            item["status"] = "writing"
            receipt["status"] = "writing"
            _save(journal, receipt)
            os.replace(item["stage"], path)
            item["status"] = "written"
            if _hashes(path)["sha256"] != item["after_sha256"]:
                raise ValueError("写入后回读校验失败：" + str(path))
            _save(journal, receipt)
        final = inspect_combo(package, runtime)
        if (not final["supported"] or final["package_state"] != target or final["dll_state"] != target
                or final["runtime_state"] != ("missing" if report["runtime_state"] == "missing" else target)):
            raise ValueError("提交前组合状态回读失败")
        receipt["status"] = "committed"
        _save(journal, receipt)
        message = ("已还原原始组件与主程序本机基线，保留基线原有其他修改" if restore
                   else "已应用版本锁定组合补丁")
        if report["runtime_state"] == "missing":
            message += "；缓存尚未解压，等待微信自解压"
        emit(message + "；事务备份：" + str(directory))
        return True, message + "；备份：" + str(directory)
    except Exception as error:
        failures = []
        if receipt is not None:
            for item in reversed(receipt["files"]):
                if item["status"] not in ("writing", "written"):
                    continue
                stage = None
                try:
                    path, backup = _safe(item["path"], "file"), _safe(item["backup"], "file")
                    if _hashes(backup)["sha256"] != item["before_sha256"]:
                        raise ValueError("备份文件哈希已变化")
                    current = _hashes(path)["sha256"]
                    if current != item["before_sha256"]:
                        if current != item["after_sha256"]:
                            raise ValueError("目标出现第三种内容，拒绝覆盖")
                        stage = _stage_copy(backup, path)
                        if _hashes(stage)["sha256"] != item["before_sha256"]:
                            raise ValueError("恢复暂存回读失败")
                        os.replace(stage, path)
                    if _hashes(path)["sha256"] != item["before_sha256"]:
                        raise ValueError("恢复回读校验失败")
                    item["status"] = "restored"
                except Exception as rollback_error:
                    item["rollback_error"] = str(rollback_error)
                    failures.append(item["path"] + "：" + str(rollback_error))
                finally:
                    try:
                        if stage is not None and stage.exists():
                            stage.unlink()
                    except OSError as cleanup_error:
                        failures.append("恢复暂存清理失败：" + str(cleanup_error))
            receipt.update(status="rollback_failed" if failures else "rolled_back", error=str(error))
            try:
                _save(journal, receipt)
            except Exception as journal_error:
                failures.append("事务日志写入失败：" + str(journal_error))
        message = "操作失败：" + str(error)
        if receipt is not None:
            message += "；" + ("回退失败：" + "；".join(failures) if failures else "已回退所有已写文件")
            message += "；事务记录：" + str(journal)
        emit(message)
        return False, message
    finally:
        for stage in stages:
            try:
                if stage.exists():
                    stage.unlink()
            except OSError:
                pass
        if lock is not None:
            try:
                lock.unlink()
            except OSError:
                pass
