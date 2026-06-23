#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mtime incremental capture + debounce + force-rehash."""
import os
import time
import unittest

from tests import helpers
from git_safepoint import engine
from git_safepoint.mtimecache import MtimeCache

import json
import stat

from git_safepoint import gitutil, mtimecache


class IncrementalTest(unittest.TestCase):
    def setUp(self):
        self.repo = helpers.make_repo()

    def tearDown(self):
        helpers.cleanup(self.repo)

    def _make_files(self, n):
        for i in range(n):
            helpers.write_file(self.repo, "f{0}.txt".format(i), "v0-{0}".format(i))

    def test_cold_then_no_change_then_one_change(self):
        self._make_files(20)
        # Pass 1: cold -> everything hashed, reused 0.
        r1 = engine.capture(self.repo, via="manual", debounce=0)
        self.assertEqual(r1.status, 0)
        self.assertEqual(r1.reused, 0)
        self.assertEqual(r1.files, 20)

        # Pass 2: no content change -> all reused, rehashed 0.
        r2 = engine.capture(self.repo, via="manual", debounce=0, force=True)
        self.assertEqual(r2.status, 0)
        self.assertEqual(r2.reused, 20)

        # Pass 3: change exactly one file -> exactly one re-hash (19 reused).
        path = os.path.join(self.repo, "f3.txt")
        # ensure mtime advances even on coarse clocks
        time.sleep(0.01)
        with open(path, "wb") as fh:
            fh.write(b"changed-content-different-length")
        r3 = engine.capture(self.repo, via="manual", debounce=0, force=True)
        self.assertEqual(r3.status, 0)
        self.assertEqual(r3.reused, 19)

    def test_force_rehash_ignores_cache(self):
        self._make_files(5)
        engine.capture(self.repo, via="manual", debounce=0)
        r = engine.capture(self.repo, via="manual", debounce=0, force=True,
                           force_rehash=True)
        self.assertEqual(r.status, 0)
        self.assertEqual(r.reused, 0)  # forced: nothing reused

    def test_via_hook_forces_rehash(self):
        self._make_files(5)
        engine.capture(self.repo, via="manual", debounce=0)
        # Change one file so debounce does not skip; via=hook then force-rehashes
        # ALL files (reused 0), proving it ignores the mtime cache.
        time.sleep(0.01)
        helpers.write_file(self.repo, "f1.txt", "changed-different-length")
        r = engine.capture(self.repo, via="hook")
        self.assertEqual(r.status, 0)
        self.assertEqual(r.reused, 0)

    def test_debounce_skips_unchanged(self):
        self._make_files(3)
        r1 = engine.capture(self.repo, via="manual", debounce=5)
        self.assertEqual(r1.status, 0)
        # Immediate re-capture, no change -> debounced skip (status 1).
        r2 = engine.capture(self.repo, via="manual", debounce=5)
        self.assertEqual(r2.status, 1)
        self.assertIn("debounce", r2.reason)

    def test_debounce_captures_on_change(self):
        self._make_files(3)
        engine.capture(self.repo, via="manual", debounce=5)
        time.sleep(0.01)
        helpers.write_file(self.repo, "f1.txt", "now-different-length-content")
        r = engine.capture(self.repo, via="manual", debounce=5)
        self.assertEqual(r.status, 0)

    def test_corrupt_cache_falls_back_to_cold(self):
        self._make_files(4)
        engine.capture(self.repo, via="manual", debounce=0)
        cache_path = os.path.join(self.repo, ".git", "snap", "mtime-cache.json")
        with open(cache_path, "w", encoding="utf-8") as fh:
            fh.write("{ this is not valid json ")
        # Should not raise; treats as cold.
        cache = MtimeCache.load(cache_path)
        self.assertEqual(cache.entries, {})
        r = engine.capture(self.repo, via="manual", debounce=0, force=True)
        self.assertEqual(r.status, 0)


class ConservativeSkipDeletionAwareTest(unittest.TestCase):
    """A staged-but-deleted tracked file is not silently dropped by the
    conservative / debounce fast-skip; the deletion-aware injection still fires."""

    def setUp(self):
        self.repo = helpers.make_repo()

    def tearDown(self):
        helpers.cleanup(self.repo)

    def _newest_paths(self):
        ref = engine.resolve_ref(engine.list_refs(self.repo)[0][0])
        return [rel for _m, _s, rel in engine._paths_under(self.repo, ref, "")]

    def _newest_blob(self, rel):
        ref = engine.resolve_ref(engine.list_refs(self.repo)[0][0])
        return helpers.git(
            self.repo, "cat-file", "blob", "{0}:{1}".format(ref, rel)
        ).stdout

    def _setup_baseline(self):
        helpers.write_file(self.repo, "a.txt", "a")
        helpers.git(self.repo, "add", "a.txt")
        helpers.git(self.repo, "commit", "-qm", "init")
        # Baseline snapshot so the cache has entries (enables the fast-skip gates).
        engine.capture(self.repo, via="manual", force=True, debounce=0)

    def _stage_then_delete(self, rel, content):
        helpers.write_file(self.repo, rel, content)
        helpers.git(self.repo, "add", rel)
        os.unlink(os.path.join(self.repo, rel))

    def test_conservative_capture_preserves_staged_deleted_file(self):
        self._setup_baseline()
        # A NEW file, staged but never snapshotted, then deleted from the work
        # tree -- its only durable copy is the staged blob.
        self._stage_then_delete("b.txt", "PRECIOUS")
        # Conservative path (Write/Edit, or an allowlist-missed destructive Bash).
        res = engine.capture(self.repo, via="hook", conservative=True, debounce=0)
        self.assertEqual(res.status, 0, res.reason)  # not skipped
        self.assertIn("b.txt", self._newest_paths())
        self.assertEqual(self._newest_blob("b.txt"), b"PRECIOUS")

    def test_debounce_path_also_preserves_staged_deleted_file(self):
        self._setup_baseline()
        self._stage_then_delete("b.txt", "PRECIOUS2")
        # Plain debounced manual path (no force, no force_rehash, debounce on).
        res = engine.capture(self.repo, via="manual", debounce=5.0)
        self.assertEqual(res.status, 0, res.reason)
        self.assertIn("b.txt", self._newest_paths())
        self.assertEqual(self._newest_blob("b.txt"), b"PRECIOUS2")

    def test_deleted_secret_alone_still_skips(self):
        # A deleted tracked SECRET must NOT force a capture: it stays excluded and
        # nothing else changed, so the fast-skip still fires (no spurious rehash).
        helpers.write_file(self.repo, "a.txt", "a")
        helpers.write_file(self.repo, ".env", "TOKEN=x")
        helpers.git(self.repo, "add", "a.txt", ".env")
        helpers.git(self.repo, "commit", "-qm", "init")
        engine.capture(self.repo, via="manual", force=True, debounce=0)
        os.unlink(os.path.join(self.repo, ".env"))  # delete the tracked secret
        res = engine.capture(self.repo, via="hook", conservative=True, debounce=0)
        self.assertEqual(res.status, 1, "deleted secret should not force a capture")


class MalformedCacheTest(unittest.TestCase):
    """A version-matching but malformed cache cold-rehashes instead
    of crashing the capture."""

    def test_malformed_entries_do_not_crash_capture(self):
        repo = helpers.make_repo()
        try:
            helpers.write_file(repo, "f.txt", "hello")
            snap = gitutil.snap_dir(repo)
            with open(os.path.join(snap, "mtime-cache.json"), "w") as fh:
                json.dump(
                    {"version": mtimecache.CACHE_VERSION,
                     "entries": {"f.txt": 999, "g.txt": [1, 2]}},  # int + too-short
                    fh,
                )
            # Conservative path consults signature_set + reuse_sha; the malformed
            # entries must degrade to a cold rehash, not raise.
            res = engine.capture(repo, via="hook", conservative=True, debounce=0)
            self.assertEqual(res.status, 0, res.reason)
            ref = engine.resolve_ref(engine.list_refs(repo)[0][0])
            names = [rel for _m, _s, rel in engine._paths_under(repo, ref, "")]
            self.assertIn("f.txt", names)
        finally:
            helpers.cleanup(repo)


class SignatureSkipsDirsTest(unittest.TestCase):
    """N2: an untracked embedded git repo must not defeat the conservative
    fast-skip (which would otherwise re-hash on every call)."""

    def test_embedded_repo_does_not_defeat_fast_skip(self):
        repo = helpers.make_repo()
        try:
            helpers.write_file(repo, "f.txt", "x")
            emb = os.path.join(repo, "embedded")
            os.makedirs(emb)
            helpers.git(emb, "init", "-q")
            helpers.write_file(emb, "g.txt", "y")
            engine.capture(repo, via="hook", conservative=True, debounce=0)
            # Nothing changed: the 2nd capture must hit the fast mtime skip, NOT
            # fall through to a re-hash + tree dedup (distinguished by the reason).
            res = engine.capture(repo, via="hook", conservative=True, debounce=0)
            self.assertEqual(res.status, 1)
            self.assertIn("conservative mtime check", res.reason)
        finally:
            helpers.cleanup(repo)


class ForceRehashByteContentTest(unittest.TestCase):
    """N12: a same-size, mtime-restored content swap is captured with the NEW
    bytes on the destructive-moment (force_rehash) path -- asserted on content,
    not just snapshot count."""

    def test_same_size_swap_captures_new_bytes(self):
        repo = helpers.make_repo()
        try:
            p = helpers.write_file(repo, "f.txt", "AAAA")
            engine.capture(repo, via="manual", force=True, debounce=0)
            st = os.lstat(p)
            with open(p, "wb") as fh:
                fh.write(b"BBBB")  # same size, different content
            os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))  # restore mtime
            res = engine.capture(repo, via="hook", conservative=False, debounce=0)
            self.assertEqual(res.status, 0, res.reason)
            ref = engine.resolve_ref(engine.list_refs(repo)[0][0])
            blob = helpers.git(repo, "cat-file", "blob", "{0}:f.txt".format(ref)).stdout
            self.assertEqual(blob, b"BBBB")
        finally:
            helpers.cleanup(repo)


class CachePoisonSelfHealTest(unittest.TestCase):
    def setUp(self):
        self.repo = helpers.make_repo()

    def tearDown(self):
        helpers.cleanup(self.repo)

    def _cache(self):
        with open(os.path.join(self.repo, ".git", "snap",
                               "mtime-cache.json")) as fh:
            return json.load(fh)

    def _obj_exists(self, sha):
        return helpers.git(
            self.repo, "cat-file", "-e", sha, check=False
        ).returncode == 0

    def test_missing_reused_blob_is_rehashed_not_fatal(self):
        helpers.write_file(self.repo, "f.txt", "stable")
        helpers.write_file(self.repo, "g.txt", "v1")
        self.assertEqual(engine.capture(self.repo, force=True).status, 0)

        # Orphan every snapshot blob: drop the refs + reflog, then gc --prune=now.
        for _sid, ref in engine.list_refs(self.repo):
            helpers.git(self.repo, "update-ref", "-d", ref)
        helpers.git(self.repo, "reflog", "expire", "--all", "--expire=now")
        helpers.git(self.repo, "gc", "--prune=now", "--quiet", check=False)
        dead = self._cache()["entries"]["f.txt"][-1]
        self.assertFalse(self._obj_exists(dead), "precondition: blob is gone")

        # Change g.txt (bypass the conservative skip); f.txt unchanged would reuse
        # the dead SHA. Capture must self-heal (succeed), not raise.
        helpers.write_file(self.repo, "g.txt", "v2")
        r2 = engine.capture(self.repo, via="hook", conservative=True)
        self.assertEqual(r2.status, 0, r2.reason)

        rows = engine.list_refs(self.repo)
        names = {rel for _m, _s, rel in
                 engine._paths_under(self.repo, engine.resolve_ref(rows[0][0]), "")}
        self.assertIn("f.txt", names)
        healed = self._cache()["entries"]["f.txt"][-1]
        self.assertTrue(self._obj_exists(healed), "cache repaired with a live blob")


# --- prune's gc pins its grace ---------------------------------------


class C7FastSkipGuardTest(unittest.TestCase):
    def setUp(self):
        self.repo = helpers.make_repo()

    def tearDown(self):
        helpers.cleanup(self.repo)

    def test_skip_declines_when_recorded_snapshot_pruned(self):
        helpers.write_file(self.repo, "f.txt", "content")
        res1 = engine.capture(
            self.repo, via="hook", conservative=True, debounce=0
        )
        self.assertEqual(res1.status, 0, res1.reason)

        # Simulate a prune from another worktree deleting that snapshot.
        helpers.git(
            self.repo, "update-ref", "-d", engine.resolve_ref(res1.id)
        )

        # File unchanged: pre-fix the conservative fast-skip would SKIP (status 1)
        # trusting the now-deleted snapshot. The C7 guard must take a fresh one.
        res2 = engine.capture(
            self.repo, via="hook", conservative=True, debounce=0
        )
        self.assertEqual(
            res2.status, 0,
            "fast-skip must decline when the recorded snapshot is gone (C7)",
        )
        self.assertTrue(engine.list_refs(self.repo), "a fresh snapshot exists")


# -------------------------------------------------------------------------- C11


class StagedFastSkipTest(unittest.TestCase):
    def setUp(self):
        self.repo = helpers.make_repo()

    def tearDown(self):
        helpers.cleanup(self.repo)

    def _commit(self, rel, content):
        helpers.write_file(self.repo, rel, content)
        helpers.git(self.repo, "add", rel)
        helpers.git(self.repo, "commit", "-qm", "add " + rel)

    def test_staged_only_change_defeats_fast_skip(self):
        # Build a C3 shape WITHOUT touching the work tree file (stage a blob via
        # update-index --cacheinfo): index=STAGED_V1, work tree=BASE, HEAD=BASE.
        # The work-tree signature is unchanged since the first capture, so pre-fix
        # the debounce/conservative skip dropped it and the staged variant was lost.
        self._commit("f.txt", "BASE")
        r1 = engine.capture(self.repo, via="manual", debounce=5.0)
        self.assertEqual(r1.status, 0)

        sha = helpers.git(
            self.repo, "hash-object", "-w", "--stdin", input_bytes=b"STAGED_V1"
        ).stdout.decode().strip()
        helpers.git(self.repo, "update-index", "--cacheinfo",
                    "100644,{0},f.txt".format(sha))

        # work tree f.txt is byte-identical to r1; only the index changed.
        r2 = engine.capture(self.repo, via="manual", debounce=5.0)
        self.assertEqual(r2.status, 0, "staged-only change must NOT be skipped (B)")
        meta = engine.snapshot_meta(self.repo, r2.id, engine.resolve_ref(r2.id))
        self.assertTrue(meta["has_staged"], "staged variant must be captured")
        st, msg = engine.restore_file(
            self.repo, r2.id, "f.txt", no_backup=True, staged=True
        )
        self.assertEqual(st, 0, msg)
        self.assertEqual(helpers.read_file(self.repo, "f.txt"), b"STAGED_V1")

    def test_conservative_hook_also_staged_aware(self):
        self._commit("f.txt", "BASE")
        engine.capture(self.repo, via="hook", conservative=True, debounce=0)
        sha = helpers.git(
            self.repo, "hash-object", "-w", "--stdin", input_bytes=b"STAGED_X"
        ).stdout.decode().strip()
        helpers.git(self.repo, "update-index", "--cacheinfo",
                    "100644,{0},f.txt".format(sha))
        r = engine.capture(self.repo, via="hook", conservative=True, debounce=0)
        self.assertEqual(r.status, 0, "conservative skip must be staged-aware (B)")
        meta = engine.snapshot_meta(self.repo, r.id, engine.resolve_ref(r.id))
        self.assertTrue(meta["has_staged"])


# ------------------------------------------------------------------------- C


class SignatureTypeBitTest(unittest.TestCase):
    """I: regular vs symlink are distinguished by the signature type field."""

    def setUp(self):
        self.repo = helpers.make_repo()

    def tearDown(self):
        helpers.cleanup(self.repo)

    def test_signature_type_field_distinguishes_regular_and_symlink(self):
        reg = helpers.write_file(self.repo, "a", "hello")
        link = os.path.join(self.repo, "b")
        os.symlink("hello", link)
        sreg = MtimeCache.signature(os.lstat(reg))
        slink = MtimeCache.signature(os.lstat(link))
        self.assertEqual(sreg[-1], stat.S_IFREG)
        self.assertEqual(slink[-1], stat.S_IFLNK)
        self.assertNotEqual(sreg[-1], slink[-1])
        # reuse_sha rejects a blob recorded under a different type.
        c = MtimeCache("ignored")
        c.record("p", sreg, "deadbeef")
        self.assertIsNone(c.reuse_sha("p", slink))
        self.assertEqual(c.reuse_sha("p", sreg), "deadbeef")

    def test_type_swap_recaptured_with_new_mode(self):
        p = helpers.write_file(self.repo, "f", "hello")
        engine.capture(self.repo, via="manual", force=True, debounce=0)
        os.unlink(p)
        os.symlink("target-x", p)
        res = engine.capture(self.repo, via="hook", conservative=True, debounce=0)
        self.assertEqual(res.status, 0)
        ref = engine.resolve_ref(engine.list_refs(self.repo)[0][0])
        mode, _sha = engine._path_mode_sha_in_ref(self.repo, ref, "f")
        self.assertEqual(mode, "120000")


class CaptureRaceCacheTest(unittest.TestCase):
    """E: a file changed *during* capture is not cached, so the next capture
    cold-rehashes and self-heals (never persists a stale-but-consistent entry)."""

    def setUp(self):
        self.repo = helpers.make_repo()

    def tearDown(self):
        helpers.cleanup(self.repo)

    def test_change_between_hash_and_restat_is_not_cached(self):
        p = helpers.write_file(self.repo, "f.txt", "v1")
        orig = engine.hash_many_blobs

        def racing_hash(repo, paths):
            result = orig(repo, paths)  # hashes the current (v1) bytes
            # Simulate an external writer landing AFTER we hashed but before the
            # post-hash re-stat: changes size + ctime, keeps the returned v1 sha.
            for ap in paths:
                if ap.endswith("f.txt"):
                    time.sleep(0.01)
                    with open(ap, "wb") as fh:
                        fh.write(b"v2-longer-content")
            return result

        engine.hash_many_blobs = racing_hash
        try:
            r1 = engine.capture(self.repo, via="manual", force=True, debounce=0)
        finally:
            engine.hash_many_blobs = orig
        self.assertEqual(r1.status, 0, r1.reason)

        # f.txt was unstable across the capture -> not cached.
        cache = MtimeCache.load(
            os.path.join(self.repo, ".git", "snap", "mtime-cache.json")
        )
        self.assertNotIn("f.txt", cache.entries)

        # Self-heal: the follow-up capture re-hashes f.txt and records v2.
        r2 = engine.capture(self.repo, via="manual", force=True, debounce=0)
        self.assertEqual(r2.status, 0, r2.reason)
        newest = engine.list_refs(self.repo)[0][0]
        ref = engine.resolve_ref(newest)
        blob = helpers.git(
            self.repo, "cat-file", "blob", "{0}:f.txt".format(ref)
        ).stdout
        self.assertEqual(blob, b"v2-longer-content")

if __name__ == "__main__":
    unittest.main()
