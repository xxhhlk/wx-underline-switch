#!/usr/bin/env python3
"""离线组合补丁回归测试；所有写入仅发生在临时副本。"""
import argparse
from contextlib import ExitStack
import hashlib
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest import mock
import zipfile

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))
import offline_combo as combo

REAL_FIXTURES = None
WORK_DIR = None
PAYLOAD_SHA256 = "b8fd7394e62838edd8e93177aa5b826eaed156a3391ce7346c8b4088808ce1fc"
RESOURCE_NAMES = (
    "icudtl.dat", "wmpf_100_percent.pak", "wmpf_200_percent.pak",
    "wmpf_resources.pak", "locales/zh-CN.pak",
)
RESOURCES = {name: ("资源：" + name).encode("utf-8") for name in RESOURCE_NAMES}


def digest(data, algorithm="sha256"):
    return hashlib.new(algorithm, data).hexdigest()


def package_bytes(flue):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_STORED) as archive:
        for name, data in {"flue.dll": flue, **RESOURCES}.items():
            info = zipfile.ZipInfo("runtime/" + name, date_time=(2020, 1, 1, 0, 0, 0))
            archive.writestr(info, data)
    return stream.getvalue()


class SyntheticComboTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory(prefix="wxcombo-unit-")
        self.addCleanup(self.work.cleanup)
        self.root = Path(self.work.name)
        self.package = self.root / "install" / "RadiumWMPF.bin"
        self.package.parent.mkdir()
        self.dll = self.package.parent / "Weixin.dll"
        self.runtime = self.root / "custom-runtime"
        self.runtime.mkdir()
        self.flue = self.runtime / "flue.dll"
        self.backups = self.root / "transactions"
        self.content = {
            "flue": {state: ("flue:" + state).encode().ljust(32, b".") for state in ("original", "basic", "combo")},
            "dll": {state: ("dll:" + state).encode().ljust(32, b".") for state in ("original", "combo")},
        }
        self.content["package"] = {state: package_bytes(data) for state, data in self.content["flue"].items()}
        for name, data in RESOURCES.items():
            resource = self.runtime / name
            resource.parent.mkdir(parents=True, exist_ok=True)
            resource.write_bytes(data)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for constant, kind in (("PACKAGE", "package"), ("DLL", "dll"), ("FLUE", "flue")):
            self.stack.enter_context(mock.patch.object(combo, constant, {
                state: digest(data, "sha1") for state, data in self.content[kind].items()
            }))
        self.transform = self.stack.enter_context(mock.patch.object(combo, "_transform", side_effect=self.fake_transform))
        self.seed()

    def seed(self, state="original", dll_state=None):
        self.package.write_bytes(self.content["package"][state])
        self.dll.write_bytes(self.content["dll"][dll_state or ("combo" if state == "combo" else "original")])
        self.flue.write_bytes(self.content["flue"][state])

    def fake_transform(self, path, kind, state, target):
        self.assertEqual(Path(path).read_bytes(), self.content[kind][state])
        Path(path).write_bytes(self.content[kind][target])

    def change(self, **kwargs):
        options = dict(runtime=self.runtime, backup_root=self.backups, log=lambda text: None,
                       process_check=lambda: False)
        options.update(kwargs)
        return combo.change_combo(self.package, **options)

    def snapshot(self):
        return {str(path.relative_to(self.root)): path.read_bytes()
                for path in (self.package, self.dll, *self.runtime.rglob("*")) if path.is_file()}

    def journals(self):
        return sorted(self.backups.glob("*/journal.json"))

    def assert_rejected_unchanged(self, before, result):
        self.assertFalse(result[0], result[1])
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.backups / "transaction.lock").exists())

    def assert_state(self, state):
        for kind, path in (("package", self.package), ("dll", self.dll), ("flue", self.flue)):
            self.assertEqual(path.read_bytes(), self.content[kind][state], kind)

    def test_payload_matches_verified_experiment(self):
        self.assertEqual(len(combo._payload()), 221)
        self.assertEqual(digest(combo._payload()), PAYLOAD_SHA256)

    def test_unknown_content_is_strictly_rejected(self):
        for path, field in ((self.package, "package_state"), (self.dll, "dll_state"),
                            (self.flue, "runtime_state")):
            with self.subTest(component=field):
                self.seed()
                path.write_bytes(b"unknown content")
                before = self.snapshot()
                report = combo.inspect_combo(self.package, self.runtime)
                self.assertFalse(report["supported"])
                self.assertEqual(report[field], "unknown")
                self.assert_rejected_unchanged(before, self.change())
        self.transform.assert_not_called()

    def test_running_or_indeterminate_process_is_rejected(self):
        for value in (True, None):
            with self.subTest(process=value):
                before = self.snapshot()
                self.assert_rejected_unchanged(before, self.change(process_check=lambda: value))
        self.transform.assert_not_called()
        self.assertEqual(self.journals(), [])

    def test_process_starts_after_preparation(self):
        before = self.snapshot()
        check = mock.Mock(side_effect=[False, True])
        self.assert_rejected_unchanged(before, self.change(process_check=check))
        self.assertEqual(check.call_count, 2)

    def test_process_starts_after_first_write_rolls_back(self):
        before = self.snapshot()
        check = mock.Mock(side_effect=[False, False, False, True])
        self.assert_rejected_unchanged(before, self.change(process_check=check))
        receipt = json.loads(self.journals()[0].read_text(encoding="utf-8"))
        self.assertEqual(receipt["status"], "rolled_back")
        self.assertTrue(any(item["status"] == "restored" for item in receipt["files"]))

    def test_apply_revert_all_three_files_and_exact_originals(self):
        before = self.snapshot()
        ok, message = self.change()
        self.assertTrue(ok, message)
        self.assert_state("combo")
        receipt = json.loads(self.journals()[0].read_text(encoding="utf-8"))
        self.assertEqual(receipt["status"], "committed")
        self.assertEqual({item["kind"] for item in receipt["files"]}, {"package", "dll", "flue"})
        for item in receipt["files"]:
            self.assertEqual(digest(Path(item["backup"]).read_bytes()), item["before_sha256"])
        ok, message = self.change(restore=True)
        self.assertTrue(ok, message)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(len(self.journals()), 2)

    def test_basic_input_upgrades_and_reverts_to_full_original(self):
        self.seed("basic")
        ok, message = self.change()
        self.assertTrue(ok, message)
        self.assert_state("combo")
        ok, message = self.change(restore=True)
        self.assertTrue(ok, message)
        self.assert_state("original")

    def test_revert_is_idempotent_on_original(self):
        before = self.snapshot()
        with mock.patch.object(combo.os, "replace", wraps=combo.os.replace) as replace:
            ok, message = self.change(restore=True)
        self.assertTrue(ok, message)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.journals(), [])
        self.transform.assert_not_called()
        replace.assert_not_called()

    def test_mixed_known_states_converge_to_combo(self):
        self.seed("basic", dll_state="combo")
        self.flue.write_bytes(self.content["flue"]["original"])
        ok, message = self.change()
        self.assertTrue(ok, message)
        self.assert_state("combo")
        receipt = json.loads(self.journals()[0].read_text(encoding="utf-8"))
        self.assertEqual({item["kind"] for item in receipt["files"]}, {"package", "flue"})

    def test_apply_is_idempotent(self):
        self.assertTrue(self.change()[0])
        before, receipts, transforms = self.snapshot(), self.journals(), self.transform.call_count
        with mock.patch.object(combo.os, "replace", wraps=combo.os.replace) as replace:
            ok, message = self.change()
        self.assertTrue(ok, message)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.journals(), receipts)
        self.assertEqual(self.transform.call_count, transforms)
        replace.assert_not_called()

    def test_failure_on_second_target_write_restores_first(self):
        before = self.snapshot()
        real_replace = combo.os.replace
        observed = []
        def replace(source, destination):
            if Path(destination) == self.package:
                observed.append(self.dll.read_bytes())
                raise OSError("注入第二个目标写入失败")
            return real_replace(source, destination)
        with mock.patch.object(combo.os, "replace", side_effect=replace):
            result = self.change()
        self.assert_rejected_unchanged(before, result)
        self.assertEqual(observed, [self.content["dll"]["combo"]])
        self.assertEqual(json.loads(self.journals()[0].read_text(encoding="utf-8"))["status"], "rolled_back")
        self.assertFalse(list(self.package.parent.glob(".wxcombo-*.stage")))

    def test_revert_second_write_failure_restores_combo(self):
        self.seed("combo")
        before = self.snapshot()
        real_replace = combo.os.replace
        observed = []
        def replace(source, destination):
            if Path(destination) == self.package:
                observed.append(self.dll.read_bytes())
                raise OSError("注入回退第二个目标写入失败")
            return real_replace(source, destination)
        with mock.patch.object(combo.os, "replace", side_effect=replace):
            result = self.change(restore=True)
        self.assert_rejected_unchanged(before, result)
        self.assertEqual(observed, [self.content["dll"]["original"]])
        self.assert_state("combo")

    def test_backup_copy_failure_leaves_targets_unchanged(self):
        before = self.snapshot()
        with mock.patch.object(combo.shutil, "copyfileobj", side_effect=OSError("注入备份失败")):
            self.assert_rejected_unchanged(before, self.change())
        self.transform.assert_not_called()

    def test_prepared_journal_failure_leaves_targets_unchanged(self):
        before = self.snapshot()
        save = combo._save
        def fail_prepared(path, receipt):
            if receipt["status"] == "prepared":
                raise OSError("注入准备日志写入失败")
            return save(path, receipt)
        with mock.patch.object(combo, "_save", side_effect=fail_prepared):
            self.assert_rejected_unchanged(before, self.change())
        self.assertFalse(list(self.package.parent.glob(".wxcombo-*.stage")))

    def test_stage_copy_failure_leaves_targets_unchanged(self):
        before = self.snapshot()
        with mock.patch.object(combo, "_stage_copy", side_effect=OSError("注入暂存失败")):
            self.assert_rejected_unchanged(before, self.change())
        self.transform.assert_not_called()

    def test_later_stage_failure_preserves_all_targets(self):
        before = self.snapshot()
        real_copy = combo._stage_copy
        copies = []
        def fail_later(source, destination):
            copies.append(Path(destination))
            if len(copies) == 2:
                raise OSError("注入第二个暂存失败")
            return real_copy(source, destination)
        with mock.patch.object(combo, "_stage_copy", side_effect=fail_later):
            self.assert_rejected_unchanged(before, self.change())
        self.assertEqual(len(copies), 2)
        self.assertEqual(self.transform.call_count, 1)
        self.assertFalse(list(self.package.parent.glob(".wxcombo-*.stage")))

    def test_bad_transform_output_never_reaches_targets(self):
        before = self.snapshot()
        with mock.patch.object(combo, "_transform", side_effect=lambda path, *args: Path(path).write_bytes("坏候选".encode())):
            self.assert_rejected_unchanged(before, self.change())
        self.assertFalse(list(self.package.parent.glob(".wxcombo-*.stage")))

    def test_corrupted_backup_copy_never_reaches_targets(self):
        before = self.snapshot()
        real_copy = combo.shutil.copyfileobj
        def corrupt_copy(source, destination, *args):
            result = real_copy(source, destination, *args)
            destination.write(b"injected corruption")
            return result
        with mock.patch.object(combo.shutil, "copyfileobj", side_effect=corrupt_copy):
            self.assert_rejected_unchanged(before, self.change())
        self.transform.assert_not_called()

    def test_pending_transaction_is_rejected(self):
        pending = self.backups / "pending"
        pending.mkdir(parents=True)
        journal = pending / "journal.json"
        journal.write_text(json.dumps({"status": "writing", "files": []}), encoding="utf-8")
        before = self.snapshot()
        self.assert_rejected_unchanged(before, self.change())
        self.assertEqual(json.loads(journal.read_text(encoding="utf-8"))["status"], "writing")
        self.transform.assert_not_called()

    def test_existing_transaction_lock_is_not_removed(self):
        self.backups.mkdir()
        lock = self.backups / "transaction.lock"
        lock.write_bytes(b"another transaction")
        before = self.snapshot()
        result = self.change()
        self.assertFalse(result[0], result[1])
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(lock.read_bytes(), b"another transaction")

    def test_tampered_stage_is_rejected_before_writes(self):
        for index in range(3):
            with self.subTest(index=index):
                self.check_prepared_tamper("stage", index)

    def test_tampered_backup_is_rejected_before_writes(self):
        for index in range(3):
            with self.subTest(index=index):
                self.check_prepared_tamper("backup", index)

    def check_prepared_tamper(self, field, index):
        self.seed()
        before = self.snapshot()
        save = combo._save
        tampered = []
        def corrupt(path, receipt):
            save(path, receipt)
            if receipt["status"] == "prepared" and not tampered:
                target = Path(receipt["files"][index][field])
                target.write_bytes("篡改后的事务文件".encode())
                tampered.append(target)
        with mock.patch.object(combo, "_save", side_effect=corrupt), \
                mock.patch.object(combo.os, "replace", wraps=combo.os.replace) as replace:
            self.assert_rejected_unchanged(before, self.change())
        self.assertEqual(len(tampered), 1)
        targets = {self.package, self.dll, self.flue}
        self.assertFalse(any(Path(call.args[1]) in targets for call in replace.call_args_list))

    def test_missing_cache_waits_without_creating_runtime(self):
        missing = self.root / "not-extracted" / "runtime"
        report = combo.inspect_combo(self.package, missing)
        self.assertTrue(report["supported"], report)
        self.assertEqual(report["runtime_state"], "missing")
        for restore in (False, False, True):
            ok, message = self.change(runtime=missing, restore=restore)
            self.assertTrue(ok, message)
            self.assertIn("等待", message)
            self.assertFalse(missing.exists())
        self.assertEqual(self.package.read_bytes(), self.content["package"]["original"])
        self.assertEqual(self.dll.read_bytes(), self.content["dll"]["original"])
        for journal in self.journals():
            self.assertEqual(len(json.loads(journal.read_text(encoding="utf-8"))["files"]), 2)

    def test_missing_runtime_resource_is_rejected(self):
        for name in RESOURCE_NAMES:
            with self.subTest(resource=name):
                resource = self.runtime / name
                original = resource.read_bytes()
                resource.unlink()
                try:
                    before = self.snapshot()
                    report = combo.inspect_combo(self.package, self.runtime)
                    self.assertFalse(report["supported"], report)
                    self.assert_rejected_unchanged(before, self.change())
                finally:
                    resource.write_bytes(original)

    def test_runtime_resource_wrong_size_is_rejected(self):
        for name in RESOURCE_NAMES:
            with self.subTest(resource=name):
                resource = self.runtime / name
                original = resource.read_bytes()
                resource.write_bytes(original + b"x")
                try:
                    before = self.snapshot()
                    self.assertFalse(combo.inspect_combo(self.package, self.runtime)["supported"])
                    self.assert_rejected_unchanged(before, self.change())
                finally:
                    resource.write_bytes(original)

    def test_runtime_resource_read_failure_is_rejected(self):
        resource = self.runtime / "icudtl.dat"
        before = self.snapshot()
        real_open = Path.open
        for failure in ("denied", "empty"):
            with self.subTest(failure=failure):
                def open_resource(path, *args, **kwargs):
                    if path == resource and args and args[0] == "rb":
                        if failure == "denied":
                            raise PermissionError("注入资源读取权限错误")
                        return io.BytesIO(b"")
                    return real_open(path, *args, **kwargs)
                with mock.patch.object(Path, "open", new=open_resource):
                    self.assertFalse(combo.inspect_combo(self.package, self.runtime)["supported"])
                    result = self.change()
                self.assert_rejected_unchanged(before, result)

    def test_parent_reparse_attribute_is_rejected(self):
        before = self.snapshot()
        real_lstat = Path.lstat
        def lstat(path, *args, **kwargs):
            info = real_lstat(path, *args, **kwargs)
            if path == self.package.parent:
                return mock.Mock(st_mode=info.st_mode, st_file_attributes=0x400)
            return info
        with mock.patch.object(Path, "lstat", new=lstat):
            self.assertFalse(combo.inspect_combo(self.package, self.runtime)["supported"])
            result = self.change()
        self.assert_rejected_unchanged(before, result)

    def test_runtime_resource_directory_is_rejected(self):
        resource = self.runtime / "icudtl.dat"
        resource.unlink()
        resource.mkdir()
        before = self.snapshot()
        self.assertFalse(combo.inspect_combo(self.package, self.runtime)["supported"])
        self.assert_rejected_unchanged(before, self.change())

    def test_cache_appears_during_preparation_without_target_writes(self):
        missing = self.root / "newly-extracted-runtime"
        before_package, before_dll = self.package.read_bytes(), self.dll.read_bytes()
        save = combo._save
        def extract_cache(path, receipt):
            save(path, receipt)
            if receipt["status"] == "prepared" and not missing.exists():
                missing.mkdir()
                (missing / "flue.dll").write_bytes(self.content["flue"]["original"])
                for name, data in RESOURCES.items():
                    resource = missing / name
                    resource.parent.mkdir(parents=True, exist_ok=True)
                    resource.write_bytes(data)
        with mock.patch.object(combo, "_save", side_effect=extract_cache):
            ok, message = self.change(runtime=missing)
        self.assertFalse(ok, message)
        self.assertEqual(self.package.read_bytes(), before_package)
        self.assertEqual(self.dll.read_bytes(), before_dll)
        self.assertEqual((missing / "flue.dll").read_bytes(), self.content["flue"]["original"])
        self.assertFalse((self.backups / "transaction.lock").exists())

    def test_explicit_runtime_overrides_environment(self):
        other = self.root / "environment-runtime"
        other.mkdir()
        (other / "flue.dll").write_bytes("环境目录不应被读取或修改".encode())
        with mock.patch.dict("os.environ", {"APPDATA": str(other)}):
            report = combo.inspect_combo(self.package, self.runtime)
            self.assertTrue(report["supported"], report)
            self.assertEqual(Path(report["runtime_path"]), self.runtime)
            ok, message = self.change()
        self.assertTrue(ok, message)
        self.assert_state("combo")
        self.assertEqual((other / "flue.dll").read_bytes(), "环境目录不应被读取或修改".encode())

    def test_process_query_failure_is_indeterminate(self):
        for output, code in (("", 0), ("invalid CSV", 0), ('"Weixin.exe","123"', 1)):
            with self.subTest(stdout=output, returncode=code):
                result = mock.Mock(stdout=output, returncode=code)
                with mock.patch.object(combo.subprocess, "run", return_value=result):
                    self.assertIsNone(combo._process_check())
        with mock.patch.object(combo.subprocess, "run", side_effect=OSError("查询失败")):
            self.assertIsNone(combo._process_check())

    def test_process_query_known_names_and_clean_result(self):
        for image, expected in (("Weixin.exe", True), ("WeChat.exe", True),
                                ("WeChatAppEx.exe", True), ("explorer.exe", False)):
            with self.subTest(image=image):
                result = mock.Mock(stdout='"' + image + '","123","Console","1","100 K"', returncode=0)
                with mock.patch.object(combo.subprocess, "run", return_value=result):
                    self.assertIs(combo._process_check(), expected)

    def test_target_changed_during_prepare_is_not_overwritten(self):
        save = combo._save
        external = "外部程序更新".encode()
        def mutate(path, receipt):
            save(path, receipt)
            if receipt["status"] == "prepared":
                self.package.write_bytes(external)
        with mock.patch.object(combo, "_save", side_effect=mutate):
            ok, message = self.change()
        self.assertFalse(ok, message)
        self.assertEqual(self.package.read_bytes(), external)
        self.assertEqual(self.dll.read_bytes(), self.content["dll"]["original"])
        self.assertEqual(self.flue.read_bytes(), self.content["flue"]["original"])

    def test_hard_link_target_is_rejected_when_available(self):
        alias = self.root / "hardlinked.dll"
        try:
            alias.hardlink_to(self.dll)
        except (OSError, NotImplementedError) as error:
            self.skipTest("当前环境无法创建硬链接：" + str(error))
        before = self.snapshot()
        self.assertFalse(combo.inspect_combo(self.package, self.runtime)["supported"])
        self.assert_rejected_unchanged(before, self.change())
        self.assertEqual(alias.read_bytes(), self.content["dll"]["original"])

    def test_symbolic_link_resource_is_rejected_when_available(self):
        resource = self.runtime / "icudtl.dat"
        actual = self.root / "actual-icu.dat"
        resource.rename(actual)
        try:
            resource.symlink_to(actual)
        except (OSError, NotImplementedError) as error:
            self.skipTest("当前环境无法创建符号链接：" + str(error))
        before = self.snapshot()
        self.assertFalse(combo.inspect_combo(self.package, self.runtime)["supported"])
        self.assert_rejected_unchanged(before, self.change())
        self.assertEqual(actual.read_bytes(), RESOURCES["icudtl.dat"])

    def test_symbolic_link_backup_root_is_rejected_when_available(self):
        actual = self.root / "actual-backups"
        actual.mkdir()
        alias = self.root / "linked-backups"
        try:
            alias.symlink_to(actual, target_is_directory=True)
        except (OSError, NotImplementedError) as error:
            self.skipTest("当前环境无法创建符号链接：" + str(error))
        before = self.snapshot()
        self.assert_rejected_unchanged(before, self.change(backup_root=alias))
        self.assertEqual(list(actual.iterdir()), [])
        self.transform.assert_not_called()

    def test_symbolic_link_runtime_is_rejected_when_available(self):
        alias = self.root / "linked-runtime"
        try:
            alias.symlink_to(self.runtime, target_is_directory=True)
        except (OSError, NotImplementedError) as error:
            self.skipTest("当前环境无法创建符号链接：" + str(error))
        before = self.snapshot()
        self.assertFalse(combo.inspect_combo(self.package, alias)["supported"])
        self.assert_rejected_unchanged(before, self.change(runtime=alias))

    def test_symbolic_link_target_is_rejected_when_available(self):
        alias = self.root / "linked-package.bin"
        try:
            alias.symlink_to(self.package)
        except (OSError, NotImplementedError) as error:
            self.skipTest("当前环境无法创建符号链接：" + str(error))
        before = self.snapshot()
        report = combo.inspect_combo(alias, self.runtime)
        self.assertFalse(report["supported"], report)
        result = combo.change_combo(alias, runtime=self.runtime, backup_root=self.backups,
                                    log=lambda text: None, process_check=lambda: False)
        self.assert_rejected_unchanged(before, result)


class PackageEntryTests(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.TemporaryDirectory(prefix="wxcombo-zip-")
        self.addCleanup(self.work.cleanup)
        self.package = Path(self.work.name) / "package.bin"

    def test_stored_entry_offsets_and_crc(self):
        data = b"runtime payload"
        self.package.write_bytes(package_bytes(data))
        start, size, local_crc, central_crc = combo._entry(self.package)
        raw = self.package.read_bytes()
        self.assertEqual(raw[start:start + size], data)
        with zipfile.ZipFile(self.package) as archive:
            expected_crc = archive.getinfo(combo.ENTRY).CRC.to_bytes(4, "little")
        self.assertEqual(raw[local_crc:local_crc + 4], expected_crc)
        self.assertEqual(raw[central_crc:central_crc + 4], expected_crc)

    def test_missing_or_compressed_entry_is_rejected(self):
        for name, compression in (("runtime/other.dll", zipfile.ZIP_STORED),
                                  (combo.ENTRY, zipfile.ZIP_DEFLATED)):
            with self.subTest(name=name, compression=compression):
                with zipfile.ZipFile(self.package, "w", compression) as archive:
                    archive.writestr(name, b"test")
                with self.assertRaises(ValueError):
                    combo._entry(self.package)


@unittest.skipUnless(REAL_FIXTURES, "未指定 --real-fixtures，跳过大型真实文件")
class RealFixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        outputs = Path(WORK_DIR) if WORK_DIR else (PROJECT / "outputs")
        outputs.mkdir(parents=True, exist_ok=True)
        cls.work = tempfile.TemporaryDirectory(prefix="combo-real-", dir=outputs)
        cls.addClassCleanup(cls.work.cleanup)
        cls.root = Path(cls.work.name)
        cls.combo_source = REAL_FIXTURES / "wx-component-verification-trial" / "RadiumWMPF.bin"
        cls.basic_source = REAL_FIXTURES / "wx-component-verification-trial" / "RadiumWMPF.rollback.bin"
        cls.dll_source = REAL_FIXTURES / "wx-weixin-gate-trial" / "Weixin.rollback.dll"
        for path, expected in ((cls.combo_source, combo.PACKAGE["combo"]),
                               (cls.basic_source, combo.PACKAGE["basic"]),
                               (cls.dll_source, combo.DLL["original"])):
            if not path.is_file():
                raise AssertionError("缺少真实 fixture：" + str(path))
            if combo._hashes(path)["sha1"] != expected:
                raise AssertionError("真实 fixture 摘要不匹配：" + str(path))
        cls.original_source = cls.root / "original.bin"
        shutil.copyfile(cls.combo_source, cls.original_source)
        combo._transform(cls.original_source, "package", "combo", "original")
        if combo._hashes(cls.original_source)["sha1"] != combo.PACKAGE["original"]:
            raise AssertionError("真实原始组件包构造未通过固定 SHA1")
        cls.original_package_hash = combo._hashes(cls.original_source)
        cls.original_dll_hash = combo._hashes(cls.dll_source)
        with zipfile.ZipFile(cls.original_source) as archive:
            with archive.open(combo.ENTRY) as stream:
                sha1, sha256 = hashlib.sha1(), hashlib.sha256()
                for block in iter(lambda: stream.read(8 * 1024 * 1024), b""):
                    sha1.update(block)
                    sha256.update(block)
                cls.original_flue_hash = {"sha1": sha1.hexdigest(), "sha256": sha256.hexdigest()}
        if cls.original_flue_hash["sha1"] != combo.FLUE["original"]:
            raise AssertionError("真实原始缓存摘要不匹配")

    def run_round_trip(self, state):
        with tempfile.TemporaryDirectory(prefix=state + "-", dir=self.root) as temporary:
            directory = Path(temporary)
            package, dll = directory / "RadiumWMPF.bin", directory / "Weixin.dll"
            runtime = directory / "custom-runtime"
            runtime.mkdir()
            source = {"original": self.original_source, "basic": self.basic_source,
                      "combo": self.combo_source}[state]
            shutil.copyfile(source, package)
            shutil.copyfile(self.dll_source, dll)
            if state == "combo":
                combo._transform(dll, "dll", "original", "combo")
            with zipfile.ZipFile(package) as archive:
                for name in ("flue.dll", *RESOURCE_NAMES):
                    destination = runtime / name
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open("runtime/" + name) as stream, destination.open("wb") as output:
                        shutil.copyfileobj(stream, output, 8 * 1024 * 1024)
            resources_before = {name: combo._hashes(runtime / name) for name in RESOURCE_NAMES}
            report = combo.inspect_combo(package, runtime)
            self.assertTrue(report["supported"], report)
            self.assertEqual(report["package_state"], state)
            kwargs = dict(runtime=runtime, backup_root=directory / "transactions",
                          process_check=lambda: False, log=lambda text: None)
            ok, message = combo.change_combo(package, **kwargs)
            self.assertTrue(ok, message)
            for path, expected in ((package, combo.PACKAGE["combo"]), (dll, combo.DLL["combo"]),
                                   (runtime / "flue.dll", combo.FLUE["combo"])):
                self.assertEqual(combo._hashes(path)["sha1"], expected, str(path))
            with zipfile.ZipFile(package) as archive:
                self.assertIsNone(archive.testzip())
            applied_hashes = [combo._hashes(path) for path in (package, dll, runtime / "flue.dll")]
            ok, message = combo.change_combo(package, **kwargs)
            self.assertTrue(ok, message)
            self.assertEqual([combo._hashes(path) for path in (package, dll, runtime / "flue.dll")], applied_hashes)
            ok, message = combo.change_combo(package, restore=True, **kwargs)
            self.assertTrue(ok, message)
            for path, expected in ((package, self.original_package_hash), (dll, self.original_dll_hash),
                                   (runtime / "flue.dll", self.original_flue_hash)):
                self.assertEqual(combo._hashes(path), expected, str(path))
            self.assertEqual({name: combo._hashes(runtime / name) for name in RESOURCE_NAMES}, resources_before)
            with zipfile.ZipFile(package) as archive:
                self.assertIsNone(archive.testzip())

    def test_original_input(self):
        self.run_round_trip("original")

    def test_basic_input(self):
        self.run_round_trip("basic")

    def test_combo_input(self):
        self.run_round_trip("combo")


def main():
    global REAL_FIXTURES, WORK_DIR
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--real-fixtures", type=Path, help="父工作区 outputs 目录；仅复制已验证 fixture")
    parser.add_argument("--work-dir", type=Path, help="大型真实副本的临时父目录，默认项目 outputs")
    args, remaining = parser.parse_known_args()
    REAL_FIXTURES = args.real_fixtures.resolve() if args.real_fixtures else None
    WORK_DIR = str(args.work_dir.resolve()) if args.work_dir else None
    RealFixtureTests.__unittest_skip__ = REAL_FIXTURES is None
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    unittest.main(argv=[sys.argv[0], *remaining], verbosity=2)


if __name__ == "__main__":
    main()
