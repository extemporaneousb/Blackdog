from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from blackdog import preparation_cache as cache
from blackdog.preparation_files import PreparationError, _digest


class ArtifactPublicationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="blackdog-artifact-")
        self.root = Path(self.temporary.name)
        self.identity = {"recipe": "reviewed", "lock": "pinned", "abi": "declared"}
        self.builds = 0

    def tearDown(self):
        self.temporary.cleanup()

    def build(self, payload):
        self.builds += 1
        (payload / "dependency.whl").write_bytes(b"verified dependency bytes")

    def acquire(self, *, identity=None, build=None, timeout=10):
        return cache.acquire(self.root / "cache", identity or self.identity,
                             build=build or self.build, deadline=time.monotonic() + timeout)

    def test_identity_changes_rebuild_and_materialization_owns_distinct_bytes(self):
        payload, first, disposition = self.acquire()
        self.assertEqual(disposition, "built")
        destination = self.root / "task-one" / "dependencies"
        cache.materialize(payload, destination, first["artifact"])
        second_payload, second, disposition = self.acquire()
        self.assertEqual((payload, first, "reused"), (second_payload, second, disposition))
        self.assertEqual(self.builds, 1)
        self.assertNotEqual((payload / "dependency.whl").stat().st_ino, (destination / "dependency.whl").stat().st_ino)
        for field in self.identity:
            with self.subTest(field=field):
                changed_payload, changed, disposition = self.acquire(identity={**self.identity, field: "changed"})
                self.assertEqual(disposition, "built")
                self.assertNotEqual(first["input_sha256"], changed["input_sha256"])
                self.assertNotEqual(payload, changed_payload)
        self.assertEqual(cache.inventory(destination, immutable=True), first["artifact"])

    def test_corrupt_generation_rebuilds_without_repairing_old_or_owned_copy(self):
        payload, pointer, _ = self.acquire()
        destination = self.root / "task-copy"
        cache.materialize(payload, destination, pointer["artifact"])
        dependency = payload / "dependency.whl"
        dependency.chmod(0o644)
        dependency.write_bytes(b"corrupt")
        replacement, current, disposition = self.acquire()
        self.assertEqual(disposition, "rebuilt")
        self.assertNotEqual(pointer["generation"], current["generation"])
        self.assertEqual(dependency.read_bytes(), b"corrupt")
        self.assertEqual(cache.inventory(destination, immutable=True), pointer["artifact"])
        self.assertEqual(cache.inventory(replacement, immutable=True), pointer["artifact"])

    def test_concurrent_consumers_never_observe_candidate_and_build_once(self):
        entered, release = threading.Event(), threading.Event()
        def build(payload):
            (payload / "dependency.whl").write_bytes(b"partial")
            entered.set()
            self.assertTrue(release.wait(5))
            self.build(payload)
        with ThreadPoolExecutor(max_workers=4) as pool:
            first = pool.submit(self.acquire, build=build)
            self.assertTrue(entered.wait(5))
            others = [pool.submit(self.acquire) for _ in range(3)]
            self.assertFalse(list((self.root / "cache").glob("*/current.json")))
            self.assertTrue(all(not result.done() for result in others))
            release.set()
            results = [first.result(), *(result.result() for result in others)]
        self.assertEqual(self.builds, 1)
        self.assertEqual(len({result[1]["generation"] for result in results}), 1)
        self.assertEqual(sorted(result[2] for result in results), ["built", "reused", "reused", "reused"])

    def test_lock_wait_respects_preparation_budget(self):
        entered, release = threading.Event(), threading.Event()
        def build(payload):
            entered.set()
            release.wait(5)
            self.build(payload)
        with ThreadPoolExecutor(max_workers=1) as pool:
            first = pool.submit(self.acquire, build=build)
            self.assertTrue(entered.wait(5))
            try:
                with self.assertRaisesRegex(PreparationError, "lock budget"):
                    self.acquire(timeout=.05)
            finally:
                release.set()
            first.result()

    def test_interrupted_build_and_publication_recover_only_with_fresh_candidates(self):
        original_write = cache.atomic_write_text
        for fault in ("build", "rename", "before-pointer", "after-pointer"):
            with self.subTest(fault=fault):
                identity = {**self.identity, "fault": fault}
                def build(payload):
                    self.build(payload)
                    if fault == "build":
                        raise KeyboardInterrupt()
                def write(path, text):
                    if path.name == "current.json" and fault == "before-pointer":
                        raise OSError("injected publication interruption")
                    original_write(path, text)
                    if path.name == "current.json" and fault == "after-pointer":
                        raise OSError("injected publication acknowledgement loss")
                with patch.object(cache, "atomic_write_text", side_effect=write):
                    if fault == "rename":
                        with patch.object(cache.os, "rename", side_effect=OSError("injected rename")):
                            with self.assertRaises(OSError):
                                self.acquire(identity=identity, build=build)
                    else:
                        with self.assertRaises((KeyboardInterrupt, OSError)):
                            self.acquire(identity=identity, build=build)
                before = self.builds
                _, _, disposition = self.acquire(identity=identity)
                self.assertEqual(disposition, "reused" if fault == "after-pointer" else "built")
                self.assertEqual(self.builds - before, 0 if fault == "after-pointer" else 1)

    def test_malformed_pointer_and_missing_generation_rebuild(self):
        _, pointer, _ = self.acquire()
        path = self.root / "cache" / pointer["input_sha256"] / "current.json"
        for contents in ("{", "[]", json.dumps({**pointer, "generation": "../escape"}),
                         json.dumps({**pointer, "generation": "0" * 32})):
            with self.subTest(contents=contents):
                path.write_text(contents)
                _, rebuilt, disposition = self.acquire()
                self.assertEqual(disposition, "rebuilt")
                self.assertEqual(rebuilt["input_sha256"], _digest(self.identity))

    def test_links_and_special_files_never_publish(self):
        outside = self.root / "outside"
        outside.write_bytes(b"do not change")
        for kind in ("symlink", "directory-link", "hardlink", "fifo"):
            with self.subTest(kind=kind):
                def build(payload):
                    target = payload / "dependency"
                    if kind == "symlink":
                        target.symlink_to(outside)
                    elif kind == "directory-link":
                        target.symlink_to(self.root, target_is_directory=True)
                    elif kind == "hardlink":
                        os.link(outside, target)
                    else:
                        os.mkfifo(target)
                with self.assertRaises(PreparationError):
                    self.acquire(identity={"kind": kind}, build=build)
        self.assertFalse(list((self.root / "cache").glob("*/current.json")))
        self.assertEqual(outside.stat().st_mode & 0o777, 0o644)
        self.assertEqual(outside.read_bytes(), b"do not change")

    def test_tampering_during_copy_cannot_materialize(self):
        payload, pointer, _ = self.acquire()
        original = cache.shutil.copytree
        def copy(*args, **kwargs):
            result = original(*args, **kwargs)
            dependency = payload / "dependency.whl"
            dependency.chmod(0o644)
            dependency.write_bytes(b"changed")
            return result
        with patch.object(cache.shutil, "copytree", side_effect=copy):
            with self.assertRaisesRegex(PreparationError, "immutable|changed"):
                cache.materialize(payload, self.root / "task-copy", pointer["artifact"])


if __name__ == "__main__":
    unittest.main()
