"""Hostile ownership, admission and persistence contracts for the TMDB store."""

import contextlib
import hashlib
import json
import logging
import os
import secrets
import shutil
import stat
import tempfile
import threading
import unittest
from unittest import mock

from yawsp_app import tmdb_service_protocol as protocol
from yawsp_app import tmdb_service_store as store_module
from yawsp_app.tmdb_service_worker import ServiceRunner


SERVICE_EPOCH = "a" * 32
OTHER_EPOCH = "b" * 32
CLAIM_TOKEN = "c" * 32
OTHER_TOKEN = "d" * 32
WAITER = "0123456789abcdef0123456789abcdef"
LARGE_RESULT_CHARS = 1048400
RESERVED_ABORT_FIXTURE_BYTES = 16384


def request(number):
    return protocol.trailer_request("movie", number)


def watchlist_request(generation=0, force=False, account="42"):
    scope = {
        "account_id": account,
        "media": "movies",
        "language": "cs-CZ",
        "schema": 1,
    }
    return protocol.watchlist_request(
        scope, generation, force, False, "v1:" + "a" * 64
    )


def result(job_id, value="ok"):
    return protocol.make_result(
        job_id, "succeeded", {"succeeded": True, "value": value}
    )


def waiter(number):
    return "%032x" % number


class PosixLockDouble:
    LOCK_EX = 2
    LOCK_NB = 4
    LOCK_UN = 8

    def __init__(self, fail=False):
        self.calls = []
        self.locked = False
        self.fail = fail

    def lockf(self, fd, operation):
        self.calls.append((fd, operation))
        if operation & self.LOCK_UN:
            self.locked = False
            return
        if self.locked and operation & self.LOCK_NB:
            raise BlockingIOError()
        if self.fail:
            raise OSError("injected POSIX lock failure")
        self.locked = True


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = self.temporary.name
        self.stores = []

    def tearDown(self):
        for instance in self.stores:
            instance.lease.release()
        self.temporary.cleanup()

    def store(self, root=None):
        instance = store_module.ServiceStateStore(root or self.root)
        self.stores.append(instance)
        return instance

    def start(self, instance=None, epoch=SERVICE_EPOCH):
        instance = instance or self.store()
        self.assertTrue(instance.lease.acquire())
        instance.recover(epoch)
        instance.set_ready(epoch)
        return instance

    def submit_one(self, instance, req, interactive=False, waiter_id=None):
        rows = instance.submit([req], interactive, waiter_id)
        self.assertEqual(len(rows), 1)
        return rows[0]

    def complete_next(self, instance, epoch=SERVICE_EPOCH, token=CLAIM_TOKEN):
        claim = instance.claim_next(epoch, token)
        self.assertIsNotNone(claim)
        published = result(claim["job_id"])
        self.assertEqual(instance.complete(claim, published), published)
        return claim

    def manifest(self, root=None):
        path = os.path.join(root or self.root, "tmdb_service_state", "manifest.json")
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)

    def test_lease_is_mutually_exclusive_and_stale_release_is_owner_fenced(self):
        first = self.store()
        second = self.store()
        third = self.store()
        self.assertTrue(first.lease.acquire())
        self.assertFalse(second.lease.acquire())
        first.lease.release()
        self.assertTrue(second.lease.acquire())
        first.lease.release()
        self.assertFalse(third.lease.acquire())
        second.lease.release()
        self.assertTrue(third.lease.acquire())

    def test_identity_transaction_serializes_independent_instances(self):
        first = self.store()
        second = self.store()
        entered = threading.Event()
        second_attempted = threading.Event()
        release = threading.Event()
        active = {"value": False}
        errors = []

        def critical(instance, hold=False):
            try:
                if not hold:
                    second_attempted.set()
                with instance.identity_transaction():
                    if hold:
                        active["value"] = True
                        entered.set()
                        self.assertTrue(release.wait(5), "holder release barrier")
                        active["value"] = False
                    else:
                        self.assertTrue(entered.is_set(), "holder never entered")
                        self.assertFalse(active["value"], "critical sections overlapped")
            except Exception as error:  # pragma: no cover - assertion reported below
                errors.append(error)

        left = threading.Thread(target=critical, args=(first, True))
        right = threading.Thread(target=critical, args=(second,))
        left.start()
        self.assertTrue(entered.wait(5))
        right.start()
        self.assertTrue(second_attempted.wait(5))
        release.set()
        left.join(5)
        right.join(5)
        self.assertFalse(left.is_alive())
        self.assertFalse(right.is_alive())
        self.assertEqual(errors, [])

    def test_admission_requires_ready_epoch_and_a_live_competing_lease(self):
        service = self.store()
        caller = self.store()
        self.assertFalse(caller.service_ready())
        row = self.submit_one(caller, request(1))
        self.assertEqual(row, {"status": "backpressured", "request": request(1), "code": "UNAVAILABLE"})

        self.assertTrue(service.lease.acquire())
        service.recover(SERVICE_EPOCH)
        self.assertFalse(caller.service_ready())
        self.assertEqual(self.submit_one(caller, request(1))["code"], "UNAVAILABLE")
        service.set_ready(SERVICE_EPOCH)
        self.assertTrue(caller.service_ready())
        service.lease.release()
        self.assertFalse(caller.service_ready())
        self.assertEqual(self.submit_one(caller, request(2))["code"], "UNAVAILABLE")

    def test_same_key_reservation_and_visible_owner_are_atomic_across_instances(self):
        service = self.start()
        instances = [self.store(), self.store()]
        barrier = threading.Barrier(3)
        outputs = [[], []]

        def submit(index):
            barrier.wait()
            outputs[index] = instances[index].submit([request(42)])

        threads = [threading.Thread(target=submit, args=(index,)) for index in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(5)
            self.assertFalse(thread.is_alive())
        rows = outputs[0] + outputs[1]
        self.assertEqual(len(rows), 2)
        self.assertEqual({row["status"] for row in rows}, {"accepted", "joined"})
        self.assertEqual(len({row["job_id"] for row in rows}), 1)
        accepted = [row for row in rows if row["status"] == "accepted"]
        self.assertEqual(len(accepted), 1)
        self.assertTrue(accepted[0]["reservation"])
        self.assertEqual(len(self.manifest()["jobs"]), 1)
        claim = service.claim_next(SERVICE_EPOCH, CLAIM_TOKEN)
        self.assertIsNotNone(claim)
        self.assertIsNone(service.claim_next(SERVICE_EPOCH, OTHER_TOKEN))

    def test_global_32_plus_one_and_bounded_64_and_8_backlogs(self):
        self.start()
        callers = [self.store() for _ in range(3)]
        background = [self.submit_one(callers[index % 3], request(index + 1)) for index in range(97)]
        self.assertTrue(all(row["status"] == "accepted" for row in background[:96]))
        self.assertEqual(sum(row["reservation"] for row in background[:96]), 32)
        self.assertEqual(sum(not row["reservation"] for row in background[:96]), 64)
        self.assertEqual(background[96]["status"], "backpressured")
        self.assertEqual(background[96]["code"], "BACKPRESSURED")

        independent = [
            self.submit_one(callers[0], protocol.show_request(index + 1), True, waiter(index + 1))
            for index in range(10)
        ]
        self.assertTrue(all(row["status"] == "accepted" for row in independent[:9]))
        self.assertEqual(sum(row["reservation"] for row in independent[:9]), 1)
        self.assertEqual(sum(not row["reservation"] for row in independent[:9]), 8)
        self.assertEqual(independent[9]["code"], "BACKPRESSURED")

        joined = self.submit_one(callers[1], request(1), True, waiter(1000))
        self.assertEqual(joined["status"], "joined")
        self.assertEqual(joined["job_id"], background[0]["job_id"])
        self.assertNotIn("reservation", joined)

    def test_batch_poison_is_per_key_and_does_not_roll_back_unrelated_winners(self):
        self.start()
        caller = self.store()
        poisoned = request(99)
        poisoned["params"]["media_id"] = True
        rows = caller.submit([request(1), poisoned, request(2), request(1)])
        self.assertEqual([row["status"] for row in rows], ["accepted", "backpressured", "accepted", "joined"])
        self.assertEqual(rows[1]["code"], "FAILED")
        self.assertEqual(rows[0]["job_id"], rows[3]["job_id"])
        self.assertEqual(len(self.manifest()["jobs"]), 2)

    def test_forty_keys_are_executed_exactly_once_through_bounded_promotion(self):
        service = self.start()
        caller = self.store()
        rows = caller.submit([request(number) for number in range(1, 41)])
        self.assertEqual(len(rows), 40)
        self.assertTrue(all(row["status"] == "accepted" for row in rows))
        self.assertEqual(sum(row["reservation"] for row in rows), 32)
        expected = {row["job_id"]: row["request"] for row in rows}
        seen = []
        for index in range(40):
            claim = service.claim_next(SERVICE_EPOCH, "%032x" % (index + 1))
            self.assertIsNotNone(claim)
            self.assertNotIn(claim["job_id"], seen)
            seen.append(claim["job_id"])
            service.complete(claim, result(claim["job_id"], index))
        self.assertIsNone(service.claim_next(SERVICE_EPOCH, CLAIM_TOKEN))
        self.assertEqual(set(seen), set(expected))
        self.assertEqual(len(seen), 40)
        states = {job["job_id"]: job["state"] for job in self.manifest()["jobs"]}
        self.assertEqual(set(states), set(seen[-32:]))
        self.assertEqual(set(states.values()), {"succeeded"})
        self.assertEqual({entry["job_id"] for entry in self.manifest()["tombstones"]}, set(seen[:-32]))

    def test_fifty_key_admission_uses_one_final_validation_and_document_copy(self):
        service = self.start()
        caller = self.store()
        original_deepcopy = store_module.copy.deepcopy
        original_fits = store_module.ServiceStateStore._fits
        original_encode = store_module._json_encode
        document_copies = []
        fits = []
        candidate_bytes = []

        def count_document_copies(value):
            if isinstance(value, dict) and "service_state" in value and "jobs" in value:
                document_copies.append(len(value["jobs"]))
            return original_deepcopy(value)

        def count_fits(*args):
            document = args[-1]
            fits.append(len(document["jobs"]))
            return original_fits(document)

        def count_manifest_encoding(document, limit):
            encoded = original_encode(document, limit)
            if isinstance(document, dict) and "service_state" in document and "jobs" in document:
                candidate_bytes.append((len(document["jobs"]), len(encoded)))
            return encoded

        with mock.patch.object(store_module.copy, "deepcopy", side_effect=count_document_copies), mock.patch.object(
            store_module.ServiceStateStore, "_fits", count_fits
        ), mock.patch.object(store_module, "_json_encode", side_effect=count_manifest_encoding):
            rows = caller.submit([request(number) for number in range(1, 51)])

        self.assertTrue(all(row["status"] == "accepted" for row in rows))
        self.assertEqual(sum(row["reservation"] for row in rows), 32)
        self.assertEqual(len(self.manifest()["jobs"]), 50)
        self.assertEqual(document_copies, [0])
        self.assertEqual(fits, [50])
        self.assertGreater(len(candidate_bytes), 0)
        self.assertTrue(service.service_ready())

    def test_oversized_key_rolls_back_only_its_delta_and_keeps_collision(self):
        self.start()
        caller = self.store()
        first = self.submit_one(caller, request(1))
        original_fits = store_module.ServiceStateStore._fits
        fits = []

        def count_fits(*args):
            document = args[-1]
            fits.append(len(document["jobs"]))
            return original_fits(document)

        huge = protocol.catalog_request("popular_movies", "x" * 261800)
        with mock.patch.object(store_module.ServiceStateStore, "_fits", count_fits):
            rows = caller.submit([huge, request(1), request(2)])

        self.assertEqual(rows[0]["status"], "backpressured")
        self.assertEqual(rows[0]["code"], "BACKPRESSURED")
        self.assertEqual(rows[1]["status"], "joined")
        self.assertEqual(rows[1]["job_id"], first["job_id"])
        self.assertEqual(rows[2]["status"], "accepted")
        self.assertEqual(fits, [2])
        self.assertLessEqual(len(self.read_manifest_bytes()), 262144)

    def test_overlapping_batches_preserve_per_key_ownership(self):
        self.start()
        left = self.store()
        right = self.store()
        shared = [request(number) for number in range(1, 11)]
        left_requests = shared + [request(number) for number in range(11, 21)]
        right_requests = list(reversed(shared)) + [request(number) for number in range(21, 31)]
        barrier = threading.Barrier(3)
        rows = {}

        def submit(name, instance, requests):
            barrier.wait()
            rows[name] = instance.submit(requests)

        threads = [
            threading.Thread(target=submit, args=("left", left, left_requests)),
            threading.Thread(target=submit, args=("right", right, right_requests)),
        ]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(5)
            self.assertFalse(thread.is_alive())
        ownership = {}
        for batch in rows.values():
            for row in batch:
                key = protocol.dedup_key(row["request"])
                self.assertNotEqual(row["status"], "backpressured")
                if key in ownership:
                    self.assertEqual(row["job_id"], ownership[key])
                ownership[key] = row["job_id"]
        self.assertEqual(len(ownership), 30)
        self.assertEqual(len(self.manifest()["jobs"]), 30)

    def test_recovery_quarantines_old_work_then_fresh_barrier_admits_new_epoch(self):
        old = self.start()
        caller = self.store()
        old_row = self.submit_one(caller, request(1))
        old_claim = old.claim_next(SERVICE_EPOCH, CLAIM_TOKEN)
        old.lease.release()

        fresh = self.store()
        self.assertTrue(fresh.lease.acquire())
        entered = threading.Event()
        release = threading.Event()

        def commit_hook(manifest):
            if manifest["service_state"] == "starting" and manifest["service_epoch"] == OTHER_EPOCH:
                entered.set()
                if not release.wait(5):
                    raise AssertionError("recovery barrier timed out")

        fresh._commit_hook = commit_hook
        recovery_errors = []

        def recover():
            try:
                fresh.recover(OTHER_EPOCH)
            except Exception as error:  # pragma: no cover
                recovery_errors.append(error)

        thread = threading.Thread(target=recover)
        thread.start()
        self.assertTrue(entered.wait(5))
        attempted = threading.Event()
        during = []

        def submit_during_recovery():
            attempted.set()
            during.append(caller.submit([request(2)]))

        submitting = threading.Thread(target=submit_during_recovery)
        submitting.start()
        self.assertTrue(attempted.wait(5))
        release.set()
        thread.join(5)
        submitting.join(5)
        self.assertFalse(thread.is_alive())
        self.assertFalse(submitting.is_alive())
        self.assertEqual(recovery_errors, [])
        self.assertEqual(during[0][0]["code"], "UNAVAILABLE")
        self.assertFalse(caller.service_ready())
        with self.assertRaises(protocol.ProtocolError):
            old.complete(old_claim, result(old_row["job_id"]))

        fresh.set_ready(OTHER_EPOCH)
        fresh_row = self.submit_one(caller, request(2))
        self.assertEqual(fresh_row["status"], "accepted")
        claim = fresh.claim_next(OTHER_EPOCH, OTHER_TOKEN)
        self.assertEqual(claim["job_id"], fresh_row["job_id"])
        self.assertIsNone(fresh.claim_next(SERVICE_EPOCH, CLAIM_TOKEN))

    def test_recovery_rejects_reused_epoch_without_touching_active_owner(self):
        service = self.start()
        caller = self.store()
        row = self.submit_one(caller, request(1))
        claim = service.claim_next(SERVICE_EPOCH, CLAIM_TOKEN)
        before = self.manifest()
        with self.assertRaises(protocol.ProtocolError):
            service.recover(SERVICE_EPOCH)
        self.assertEqual(self.manifest(), before)
        self.assertEqual(caller.read_wait_result(row["job_id"], WAITER), None)
        self.assertEqual(service.complete(claim, result(row["job_id"])), result(row["job_id"]))

    def test_replacement_owner_is_fenced_by_epoch_and_claim_token(self):
        service = self.start()
        caller = self.store()
        first = self.submit_one(caller, request(1))
        claim = service.claim_next(SERVICE_EPOCH, CLAIM_TOKEN)
        stale = dict(claim)
        stale["claim_token"] = OTHER_TOKEN
        with self.assertRaises(protocol.ProtocolError):
            service.complete(stale, result(first["job_id"]))
        service.set_stopping(SERVICE_EPOCH)
        service.lease.release()

        replacement = self.store()
        self.assertTrue(replacement.lease.acquire())
        replacement.recover(OTHER_EPOCH)
        replacement.set_ready(OTHER_EPOCH)
        second = self.submit_one(caller, request(1))
        self.assertEqual(second["status"], "accepted")
        self.assertNotEqual(first["job_id"], second["job_id"])
        with self.assertRaises(protocol.ProtocolError):
            service.complete(claim, result(first["job_id"]))
        self.assertEqual(self.manifest()["service_epoch"], OTHER_EPOCH)
        current = [job for job in self.manifest()["jobs"] if job["job_id"] == second["job_id"]]
        self.assertEqual(len(current), 1)
        self.assertEqual(current[0]["state"], "queued")

    def test_stopping_aborts_unstarted_work_and_rejects_admission(self):
        service = self.start()
        caller = self.store()
        for index in range(1, 35):
            self.submit_one(caller, request(index))
        active = service.claim_next(SERVICE_EPOCH, CLAIM_TOKEN)
        service.set_stopping(SERVICE_EPOCH)
        self.assertEqual(self.submit_one(caller, request(100))["code"], "SHUTTING_DOWN")
        self.assertIsNone(service.claim_next(SERVICE_EPOCH, OTHER_TOKEN))
        states = [job["state"] for job in self.manifest()["jobs"]]
        self.assertEqual(states.count("active"), 1)
        self.assertEqual(states.count("aborted") + len(self.manifest()["tombstones"]), 33)
        service.complete(active, result(active["job_id"]))
        service.lease.release()
        self.assertFalse(caller.service_ready())

    def test_complete_is_idempotent_and_conflicting_terminal_is_rejected(self):
        service = self.start()
        caller = self.store()
        row = self.submit_one(caller, request(1), True, WAITER)
        claim = service.claim_next(SERVICE_EPOCH, CLAIM_TOKEN)
        expected = result(row["job_id"], {"text": "Praha ž\u202f🙂"})
        self.assertEqual(service.complete(claim, expected), expected)
        self.assertEqual(service.complete(claim, expected), expected)
        with self.assertRaises(protocol.ProtocolError):
            service.complete(claim, result(row["job_id"], "different"))
        caller.acknowledge(row["job_id"], WAITER)
        caller.acknowledge(row["job_id"], WAITER)

    def test_result_path_digest_unicode_and_detachment_are_validated(self):
        service = self.start()
        caller = self.store()
        req = protocol.catalog_request("popular_movies", "česky-🙂")
        row = self.submit_one(caller, req, True, WAITER)
        claim = service.claim_next(SERVICE_EPOCH, CLAIM_TOKEN)
        expected = result(row["job_id"], {"greeting": "Praha žluťoučký 🙂"})
        service.complete(claim, expected)
        actual = caller.read_wait_result(row["job_id"], WAITER)
        self.assertEqual(actual, expected)
        self.assertIsNot(actual, expected)
        actual["value"]["value"]["greeting"] = "changed"
        self.assertEqual(caller.read_wait_result(row["job_id"], WAITER), expected)

        job = next(job for job in self.manifest()["jobs"] if job["job_id"] == row["job_id"])
        self.assertEqual(job["result_ref"]["path"], "results/%s.json" % row["job_id"])
        self.assertEqual(job["result_ref"]["temp_path"], "results/%s.tmp" % row["job_id"])
        result_path = os.path.join(self.root, "tmdb_service_state", job["result_ref"]["path"])
        with open(result_path, "rb") as handle:
            encoded = handle.read()
        self.assertEqual(hashlib.sha256(encoded).hexdigest(), job["result_ref"]["digest"])
        self.assertEqual(job["result_ref"]["bytes"], len(encoded))
        self.assertIn("Praha".encode("utf-8"), encoded)
        self.assertNotIn(b"Praha\\u", encoded)

    def test_noop_claims_and_drains_write_zero_but_real_mutation_writes_once(self):
        service = self.store()
        caller = self.store()

        def unsupported(_session):
            raise AssertionError("empty drain cannot construct an engine")

        runner = ServiceRunner(
            service,
            unsupported,
            unsupported,
            unsupported,
            lambda: False,
            lambda _seconds: False,
            logging.getLogger("test.service.store.idle"),
        )
        self.assertFalse(runner.run_once())
        original_write = store_module.ServiceStateStore._write_manifest
        writes = []

        def counted_write(instance, document):
            writes.append(instance)
            return original_write(instance, document)

        before = self.read_manifest_bytes()
        digest = hashlib.sha256(before).hexdigest()
        with mock.patch.object(
            store_module.ServiceStateStore, "_write_manifest", counted_write
        ), mock.patch.object(
            store_module.os, "fsync", wraps=store_module.os.fsync
        ) as fsync, mock.patch.object(
            store_module.os, "replace", wraps=store_module.os.replace
        ) as replace:
            for _index in range(4):
                self.assertIsNone(
                    service.claim_next(runner._epoch, "%032x" % (_index + 1))
                )
            for _index in range(3):
                self.assertFalse(runner.run_once())
            self.assertEqual(writes, [])
            self.assertEqual(fsync.call_count, 0)
            self.assertEqual(replace.call_count, 0)
            self.assertEqual(self.read_manifest_bytes(), before)
            self.assertEqual(hashlib.sha256(self.read_manifest_bytes()).hexdigest(), digest)

            accepted = caller.submit([request(1)])
            self.assertEqual(accepted[0]["status"], "accepted")
            self.assertEqual(len(writes), 1)
            self.assertEqual(fsync.call_count, 1)
            self.assertEqual(replace.call_count, 1)
            self.assertTrue(caller.service_ready())
        runner.close()

    def test_temp_creation_failure_leaves_no_reservation_or_key_owner(self):
        self.start()
        caller = self.store()
        before = self.manifest()
        with mock.patch.object(store_module, "_temp_file", side_effect=OSError("temp denied")):
            with self.assertRaises(OSError):
                caller.submit([request(1)])
        self.assertEqual(self.manifest(), before)
        with mock.patch.object(store_module, "_temp_file", side_effect=OSError("temp denied")):
            self.assertEqual(store_module.ServiceStateStore(caller.root).service_ready(), True)
        row = self.submit_one(caller, request(1))
        self.assertEqual(row["status"], "accepted")

    def test_post_commit_temp_cleanup_failure_cannot_reject_or_duplicate_terminal(self):
        service = self.start()
        caller = self.store()
        row = self.submit_one(caller, request(1), True, WAITER)
        claim = service.claim_next(SERVICE_EPOCH, CLAIM_TOKEN)
        expected = result(row["job_id"])
        with mock.patch.object(store_module, "_unlink", side_effect=OSError("cleanup denied")):
            self.assertEqual(service.complete(claim, expected), expected)
            self.assertEqual(service.complete(claim, expected), expected)
        self.assertEqual(caller.read_wait_result(row["job_id"], WAITER), expected)

    def test_invalid_or_digest_mismatched_results_are_never_returned(self):
        service = self.start()
        caller = self.store()
        first = self.submit_one(caller, request(1), True, WAITER)
        claim = service.claim_next(SERVICE_EPOCH, CLAIM_TOKEN)
        invalid = result(first["job_id"])
        invalid["job_id"] = "f" * 32
        with self.assertRaises(protocol.ProtocolError):
            service.complete(claim, invalid)
        self.assertIsNone(caller.read_wait_result(first["job_id"], WAITER))
        service.complete(claim, result(first["job_id"]))
        job = next(job for job in self.manifest()["jobs"] if job["job_id"] == first["job_id"])
        path = os.path.join(self.root, "tmdb_service_state", job["result_ref"]["path"])
        with open(path, "r+b") as handle:
            handle.write(b" ")
        with self.assertRaises(protocol.ProtocolError):
            caller.read_wait_result(first["job_id"], WAITER)

    def test_128_waiter_bound_and_registered_unread_result_pinning(self):
        service = self.start()
        caller = self.store()
        row = self.submit_one(caller, request(1))
        for number in range(128):
            joined = caller.join(request(1), waiter(number + 1))
            self.assertEqual(joined["status"], "joined")
            self.assertEqual(joined["job_id"], row["job_id"])
        full = caller.join(request(1), waiter(129))
        self.assertEqual(full, {"status": "backpressured", "request": request(1), "code": "BACKPRESSURED"})
        claim = service.claim_next(SERVICE_EPOCH, CLAIM_TOKEN)
        service.complete(claim, result(row["job_id"]))
        for _ in range(40):
            other = self.submit_one(caller, protocol.show_request(secrets.randbelow(1000000) + 100))
            if other["status"] == "accepted":
                next_claim = service.claim_next(SERVICE_EPOCH, secrets.token_hex(16))
                if next_claim is not None:
                    service.complete(next_claim, result(next_claim["job_id"], "x" * 100))
            caller.collect_garbage()
        pinned = [job for job in self.manifest()["jobs"] if job["job_id"] == row["job_id"]]
        self.assertEqual(len(pinned), 1)
        self.assertEqual(caller.read_wait_result(row["job_id"], waiter(1)), result(row["job_id"]))

    def test_unwaited_terminal_and_tombstone_bounds_are_oldest_first(self):
        service = self.start()
        caller = self.store()
        job_ids = []
        for index in range(65):
            row = self.submit_one(caller, request(index + 1))
            self.assertEqual(row["status"], "accepted")
            claim = service.claim_next(SERVICE_EPOCH, "%032x" % (index + 1))
            service.complete(claim, result(row["job_id"], index))
            job_ids.append(row["job_id"])
        manifest = self.manifest()
        terminal_ids = [job["job_id"] for job in manifest["jobs"] if job["state"] == "succeeded"]
        self.assertEqual(len(terminal_ids), 32)
        self.assertEqual(set(terminal_ids), set(job_ids[-32:]))
        self.assertEqual(len(manifest["tombstones"]), 32)
        self.assertEqual({entry["job_id"] for entry in manifest["tombstones"]}, set(job_ids[1:-32]))
        self.assertTrue(all(entry["result"]["error"]["code"] == "RESULT_EXPIRED" for entry in manifest["tombstones"]))
        caller.collect_garbage()
        self.assertEqual(self.manifest(), manifest)

    def test_waited_result_does_not_reduce_32_unwaited_result_allowance(self):
        service = self.start()
        caller = self.store()
        retained = []
        for index in range(32):
            row = self.submit_one(caller, request(index + 1))
            retained.append(row["job_id"])
            claim = service.claim_next(SERVICE_EPOCH, "%032x" % (index + 1))
            service.complete(claim, result(row["job_id"]))
        self.assertEqual(len(self.manifest()["tombstones"]), 0)

        waited = self.submit_one(caller, protocol.show_request(100), True, WAITER)
        claim = service.claim_next(SERVICE_EPOCH, CLAIM_TOKEN)
        self.assertEqual(claim["job_id"], waited["job_id"])
        service.complete(claim, result(waited["job_id"]))
        document = self.manifest()
        unwaited = [job["job_id"] for job in document["jobs"] if job["state"] in ("succeeded", "failed", "aborted") and not job["waiters"]]
        self.assertEqual(len(unwaited), 32)
        self.assertEqual(set(unwaited), set(retained))
        self.assertEqual(len(document["tombstones"]), 0)

    def test_aggregate_bytes_gc_never_evicts_registered_unread_results(self):
        service = self.start()
        caller = self.store()
        retained = self.fill_registered_near_capacity(service, caller)
        self.assert_registered_results_intact(caller, retained)
        self.assertLessEqual(sum(job["result_ref"]["bytes"] for job in self.manifest()["jobs"]), 16777216)

        extra = self.submit_one(caller, request(100))
        claim = service.claim_next(SERVICE_EPOCH, CLAIM_TOKEN)
        with self.assertRaises(protocol.ProtocolError):
            service.complete(claim, result(extra["job_id"], "y" * 1048400))
        self.assertIsNone(caller.read_wait_result(extra["job_id"], "unregistered"))
        retained_ids = {row["job_id"] for row in retained}
        self.assertTrue(retained_ids.issubset({job["job_id"] for job in self.manifest()["jobs"]}))
        self.assertIsNone(service.abort_claim(claim, "ABORTED"))
        self.assertLessEqual(sum(job["result_ref"]["bytes"] for job in self.manifest()["jobs"]), 16777216)

    def fill_registered_near_capacity(self, service, caller):
        retained = []
        self.retained_lengths = {}
        target = 16777216 - RESERVED_ABORT_FIXTURE_BYTES
        index = 0
        while True:
            document = self.manifest()
            aggregate = sum(job["result_ref"]["bytes"] for job in document["jobs"])
            if aggregate >= target:
                break
            row = self.submit_one(caller, request(index + 1), True, waiter(index + 1))
            claim = service.claim_next(SERVICE_EPOCH, "%032x" % (index + 1))
            empty = result(row["job_id"], "")
            envelope = len(json.dumps(empty, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))
            value_length = min(LARGE_RESULT_CHARS, target - aggregate - envelope)
            self.assertGreater(value_length, 0)
            published = result(row["job_id"], "x" * value_length)
            service.complete(claim, published)
            retained.append(row)
            self.retained_lengths[row["job_id"]] = value_length
            index += 1
        return retained

    def assert_registered_results_intact(self, caller, retained):
        document = self.manifest()
        job_ids = {job["job_id"] for job in document["jobs"]}
        self.assertTrue({row["job_id"] for row in retained}.issubset(job_ids))
        for index, row in enumerate(retained):
            value = caller.read_wait_result(row["job_id"], waiter(index + 1))
            self.assertEqual(len(value["value"]["value"]), self.retained_lengths[row["job_id"]])
        aggregate = sum(job["result_ref"]["bytes"] for job in document["jobs"] if job["state"] in ("succeeded", "failed", "aborted"))
        self.assertLessEqual(aggregate, 16777216)

    def test_recovery_publishes_bounded_aborts_beside_saturated_registered_results(self):
        service = self.start()
        caller = self.store()
        retained = self.fill_registered_near_capacity(service, caller)
        old = [self.submit_one(caller, request(index + 1000)) for index in range(33)]
        self.assertTrue(all(row["status"] == "accepted" for row in old))
        caller.join(old[0]["request"], waiter(2000))
        service.lease.release()

        fresh = self.store()
        self.assertTrue(fresh.lease.acquire())
        fresh.recover(OTHER_EPOCH)
        self.assertEqual(self.manifest()["service_state"], "starting")
        self.assert_registered_results_intact(caller, retained)
        aborted = caller.read_wait_result(old[0]["job_id"], waiter(2000))
        self.assertEqual(aborted, protocol.make_result(old[0]["job_id"], "aborted", "ABORTED"))
        fresh.set_ready(OTHER_EPOCH)
        self.assertTrue(caller.service_ready())
        self.assertEqual(self.submit_one(caller, request(2000))["status"], "accepted")

    def test_stopping_publishes_bounded_aborts_beside_saturated_registered_results(self):
        service = self.start()
        caller = self.store()
        retained = self.fill_registered_near_capacity(service, caller)
        old = [self.submit_one(caller, request(index + 1000)) for index in range(33)]
        self.assertTrue(all(row["status"] == "accepted" for row in old))
        caller.join(old[0]["request"], waiter(2000))

        service.set_stopping(SERVICE_EPOCH)
        self.assertEqual(self.submit_one(caller, request(2000))["code"], "SHUTTING_DOWN")
        self.assert_registered_results_intact(caller, retained)
        aborted = caller.read_wait_result(old[0]["job_id"], waiter(2000))
        self.assertEqual(aborted, protocol.make_result(old[0]["job_id"], "aborted", "ABORTED"))
        document = self.manifest()
        self.assertLessEqual(sum(job["result_ref"]["bytes"] for job in document["jobs"]), 16777216)

    def assert_eviction_is_failure_atomic(self, fault):
        root = os.path.join(self.root, fault)
        service = self.store(root)
        self.start(service)
        caller = self.store(root)
        retained = []
        expected = {}
        for index in range(32):
            row = self.submit_one(caller, request(index + 1))
            retained.append(row)
            expected[row["job_id"]] = result(row["job_id"], index)
            claim = service.claim_next(SERVICE_EPOCH, "%032x" % (index + 1))
            service.complete(claim, expected[row["job_id"]])
        incoming = self.submit_one(caller, request(1000))
        claim = service.claim_next(SERVICE_EPOCH, CLAIM_TOKEN)
        self.assertEqual(claim["job_id"], incoming["job_id"])
        original_manifest = self.read_manifest_bytes(root)

        if fault == "temp":
            patcher = mock.patch.object(store_module, "_temp_file", side_effect=OSError("injected temp failure"))
        else:
            patcher = mock.patch.object(store_module.os, "fsync", side_effect=OSError("injected fsync failure"))
        published = result(incoming["job_id"], "incoming")
        with patcher:
            with self.assertRaises(OSError):
                service.complete(claim, published)
        self.assertEqual(self.read_manifest_bytes(root), original_manifest)

        for index, row in enumerate(retained):
            joined = caller.join(row["request"], waiter(1000 + index))
            self.assertEqual(joined["job_id"], row["job_id"])
            self.assertEqual(caller.read_wait_result(row["job_id"], waiter(1000 + index)), expected[row["job_id"]])
            caller.acknowledge(row["job_id"], waiter(1000 + index))
        self.assertEqual(self.read_manifest_bytes(root), original_manifest)

        with mock.patch.object(store_module, "_unlink", side_effect=OSError("post-commit cleanup failure")):
            self.assertEqual(service.complete(claim, published), published)
            self.assertEqual(service.complete(claim, published), published)
        self.assertIn(incoming["job_id"], {job["job_id"] for job in self.manifest(root)["jobs"]})
        self.assertIn(retained[0]["job_id"], {entry["job_id"] for entry in self.manifest(root)["tombstones"]})

    def test_eviction_survives_incoming_temp_creation_failure(self):
        self.assert_eviction_is_failure_atomic("temp")

    def test_eviction_survives_incoming_fsync_failure(self):
        self.assert_eviction_is_failure_atomic("fsync")

    def test_manifest_byte_bound_returns_per_key_backpressure(self):
        self.start()
        caller = self.store()
        huge = protocol.catalog_request("popular_movies", "x" * 261800)
        rows = caller.submit([huge, request(1)])
        self.assertEqual(rows[0]["status"], "backpressured")
        self.assertEqual(rows[0]["code"], "BACKPRESSURED")
        self.assertEqual(rows[1]["status"], "accepted")
        self.assertLessEqual(len(self.read_manifest_bytes()), 262144)

    def read_manifest_bytes(self, root=None):
        path = os.path.join(root or self.root, "tmdb_service_state", "manifest.json")
        with open(path, "rb") as handle:
            return handle.read()

    def test_manifest_lstat_mock_and_nofollow_flag_fail_closed_without_touching_bytes(self):
        self.start()
        caller = self.store()
        path = os.path.join(self.root, "tmdb_service_state", "manifest.json")
        original = self.read_manifest_bytes()
        real_lstat = store_module.os.lstat
        real_open = store_module.os.open
        normalized = os.path.normcase(os.path.abspath(path))
        opened = []

        def is_target(candidate):
            return os.path.normcase(os.path.abspath(candidate)) == normalized

        def linked(candidate, *args, **kwargs):
            if is_target(candidate):
                return mock.Mock(st_mode=stat.S_IFLNK)
            return real_lstat(candidate, *args, **kwargs)

        def guarded_open(candidate, flags, *args, **kwargs):
            if is_target(candidate):
                opened.append(candidate)
            return real_open(candidate, flags, *args, **kwargs)

        with mock.patch.object(store_module.os, "lstat", side_effect=linked), mock.patch.object(store_module.os, "open", side_effect=guarded_open):
            with self.assertRaises(protocol.ProtocolError):
                caller.submit([request(1)])
        self.assertEqual(opened, [])
        self.assertEqual(self.read_manifest_bytes(), original)

        nofollow = 1 << 24
        flagged = []

        def nofollow_open(candidate, flags, *args, **kwargs):
            if flags & nofollow:
                flagged.append(candidate)
                flags &= ~nofollow
            return real_open(candidate, flags, *args, **kwargs)

        with mock.patch.object(store_module.os, "O_NOFOLLOW", nofollow, create=True), mock.patch.object(store_module.os, "open", side_effect=nofollow_open):
            self.assertTrue(caller.service_ready())
        self.assertTrue(any(is_target(candidate) for candidate in flagged))
        self.assertEqual(self.read_manifest_bytes(), original)

    def test_result_lstat_mock_and_nofollow_flag_fail_closed_without_touching_bytes(self):
        service = self.start()
        caller = self.store()
        row = self.submit_one(caller, request(1), True, WAITER)
        claim = service.claim_next(SERVICE_EPOCH, CLAIM_TOKEN)
        published = result(row["job_id"], "safe")
        service.complete(claim, published)
        job = next(job for job in self.manifest()["jobs"] if job["job_id"] == row["job_id"])
        path = os.path.join(self.root, "tmdb_service_state", job["result_ref"]["path"])
        with open(path, "rb") as handle:
            original = handle.read()
        real_lstat = store_module.os.lstat
        real_open = store_module.os.open
        normalized = os.path.normcase(os.path.abspath(path))
        opened = []

        def is_target(candidate):
            return os.path.normcase(os.path.abspath(candidate)) == normalized

        def linked(candidate, *args, **kwargs):
            if is_target(candidate):
                return mock.Mock(st_mode=stat.S_IFLNK)
            return real_lstat(candidate, *args, **kwargs)

        def guarded_open(candidate, flags, *args, **kwargs):
            if is_target(candidate):
                opened.append(candidate)
            return real_open(candidate, flags, *args, **kwargs)

        with mock.patch.object(store_module.os, "lstat", side_effect=linked), mock.patch.object(store_module.os, "open", side_effect=guarded_open):
            with self.assertRaises(protocol.ProtocolError):
                caller.read_wait_result(row["job_id"], WAITER)
        self.assertEqual(opened, [])
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), original)

        nofollow = 1 << 24
        flagged = []

        def nofollow_open(candidate, flags, *args, **kwargs):
            if flags & nofollow:
                flagged.append(candidate)
                flags &= ~nofollow
            return real_open(candidate, flags, *args, **kwargs)

        with mock.patch.object(store_module.os, "O_NOFOLLOW", nofollow, create=True), mock.patch.object(store_module.os, "open", side_effect=nofollow_open):
            self.assertEqual(caller.read_wait_result(row["job_id"], WAITER), published)
        self.assertTrue(any(is_target(candidate) for candidate in flagged))
        with open(path, "rb") as handle:
            self.assertEqual(handle.read(), original)

    def test_unsafe_manifest_reads_reject_trailing_duplicate_oversized_and_symlink_input(self):
        service = self.start()
        caller = self.store()
        original = self.read_manifest_bytes()
        path = os.path.join(self.root, "tmdb_service_state", "manifest.json")
        for poisoned in (original + b" ", original.replace(b'"schema":1', b'"schema":1,"schema":1', 1), b"x" * 262145):
            with self.subTest(poison=len(poisoned)):
                with open(path, "wb") as handle:
                    handle.write(poisoned)
                self.assertFalse(caller.service_ready())
        with open(path, "wb") as handle:
            handle.write(original)
        self.assertTrue(caller.service_ready())
        service.lease.release()

        if not hasattr(os, "symlink"):
            self.skipTest("symlinks unavailable")
        sentinel = os.path.join(self.root, "sentinel.json")
        with open(sentinel, "wb") as handle:
            handle.write(original)
        os.unlink(path)
        try:
            os.symlink(sentinel, path)
        except OSError as error:
            self.skipTest("symlink privilege unavailable: %s" % error)
        with self.assertRaises(protocol.ProtocolError):
            caller.submit([request(1)])
        with open(sentinel, "rb") as handle:
            self.assertEqual(handle.read(), original)

    def test_poisoned_one_job_is_quarantined_without_authorizing_it(self):
        service = self.start()
        caller = self.store()
        good = self.submit_one(caller, request(1))
        bad = self.submit_one(caller, request(2))
        path = os.path.join(self.root, "tmdb_service_state", "manifest.json")
        document = self.manifest()
        next(job for job in document["jobs"] if job["job_id"] == bad["job_id"])["unknown"] = "poison"
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(document, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        claim = service.claim_next(SERVICE_EPOCH, CLAIM_TOKEN)
        self.assertEqual(claim["job_id"], good["job_id"])
        self.assertNotEqual(claim["job_id"], bad["job_id"])
        remaining = [job["job_id"] for job in self.manifest()["jobs"]]
        self.assertIn(good["job_id"], remaining)
        self.assertNotIn(bad["job_id"], remaining)

    def test_result_symlink_is_rejected_without_following_or_truncating_target(self):
        if not hasattr(os, "symlink"):
            self.skipTest("symlinks unavailable")
        service = self.start()
        caller = self.store()
        row = self.submit_one(caller, request(1), True, WAITER)
        claim = service.claim_next(SERVICE_EPOCH, CLAIM_TOKEN)
        service.complete(claim, result(row["job_id"], "safe"))
        manifest_job = next(job for job in self.manifest()["jobs"] if job["job_id"] == row["job_id"])
        final = os.path.join(self.root, "tmdb_service_state", manifest_job["result_ref"]["path"])
        sentinel = os.path.join(self.root, "sentinel.txt")
        with open(sentinel, "wb") as handle:
            handle.write(b"do-not-follow-or-truncate")
        os.unlink(final)
        try:
            os.symlink(sentinel, final)
        except OSError as error:
            self.skipTest("symlink privilege unavailable: %s" % error)
            return
        with self.assertRaises(protocol.ProtocolError):
            caller.read_wait_result(row["job_id"], WAITER)
        with open(sentinel, "rb") as handle:
            self.assertEqual(handle.read(), b"do-not-follow-or-truncate")

    def test_surrogate_submission_preserves_original_manifest_and_unicode_files(self):
        self.start()
        caller = self.store()
        original = self.read_manifest_bytes()
        malformed = request(1)
        malformed["params"]["media"] = "movie\ud800"
        row = self.submit_one(caller, malformed)
        self.assertEqual(row["code"], "FAILED")
        self.assertEqual(self.read_manifest_bytes(), original)
        valid = protocol.catalog_request("popular_movies", "Čeština-東京-🙂")
        accepted = self.submit_one(caller, valid, True, WAITER)
        service = self.store()
        self.assertTrue(service.lease.acquire() is False)
        self.stores[0].lease.release()
        self.assertTrue(service.lease.acquire())
        # The original ready state remains readable; publication is epoch-fenced.

    def test_scheduling_uses_interactive_priority_then_metadata_round_robin_and_fifo(self):
        service = self.start()
        caller = self.store()
        trailer_one = self.submit_one(caller, request(1))
        show_one = self.submit_one(caller, protocol.show_request(2))
        trailer_two = self.submit_one(caller, request(3))
        operation = self.submit_one(caller, watchlist_request())
        independent = self.submit_one(caller, protocol.catalog_request("popular_movies", "en"), True, WAITER)
        caller.join(trailer_two["request"], waiter(2))

        expected = [independent, trailer_two, show_one, trailer_one, operation]
        actual = []
        for index in range(5):
            claim = service.claim_next(SERVICE_EPOCH, "%032x" % (index + 1))
            self.assertIsNotNone(claim)
            actual.append(claim["job_id"])
            service.complete(claim, result(claim["job_id"]))
        self.assertEqual(actual, [row["job_id"] for row in expected])
        self.assertIsNone(service.claim_next(SERVICE_EPOCH, CLAIM_TOKEN))

    def test_followup_is_created_and_coalesced_under_owner_fence(self):
        service = self.start()
        caller = self.store()
        row = self.submit_one(caller, watchlist_request(1, False))
        claim = service.claim_next(SERVICE_EPOCH, CLAIM_TOKEN)
        first = service.ensure_followup(claim, watchlist_request(2, False))
        self.assertEqual(first["status"], "accepted")
        second = service.ensure_followup(claim, watchlist_request(3, True))
        self.assertEqual(second["job_id"], first["job_id"])
        self.assertIn(second["status"], ("accepted", "joined"))
        followup = next(job for job in self.manifest()["jobs"] if job["job_id"] == first["job_id"])
        self.assertEqual(followup["request"]["generation"], 3)
        self.assertTrue(followup["request"]["force"])
        self.assertEqual(followup["followup_of"], row["job_id"])
        stale = dict(claim)
        stale["claim_token"] = OTHER_TOKEN
        with self.assertRaises(protocol.ProtocolError):
            service.ensure_followup(stale, watchlist_request(4, True))

    def test_followup_reuses_preexisting_same_key_job_without_second_reservation(self):
        service = self.start()
        caller = self.store()
        active_row = self.submit_one(caller, watchlist_request(1, False))
        claim = service.claim_next(SERVICE_EPOCH, CLAIM_TOKEN)
        queued = self.submit_one(caller, watchlist_request(3, True))
        before_reservations = sum(job["reservation"] for job in self.manifest()["jobs"])

        continuation = service.ensure_followup(claim, watchlist_request(3, True))
        self.assertEqual(continuation["status"], "joined")
        self.assertEqual(continuation["job_id"], queued["job_id"])
        document = self.manifest()
        matching = [job for job in document["jobs"] if job["dedup_key"] == protocol.dedup_key(watchlist_request(3, True))]
        self.assertEqual(len(matching), 1)
        self.assertEqual(matching[0]["followup_of"], active_row["job_id"])
        self.assertEqual(sum(job["reservation"] for job in document["jobs"]), before_reservations)

    def test_followup_generation_and_force_are_monotonic_in_both_orders(self):
        cases = (
            (3, False, 2, True, 3, True),
            (2, False, 3, True, 3, True),
            (3, True, 2, False, 3, True),
            (2, True, 3, False, 3, True),
            (3, False, 2, False, 3, False),
            (2, True, 3, True, 3, True),
        )
        for index, (existing_generation, existing_force, new_generation, new_force, expected_generation, expected_force) in enumerate(cases):
            with self.subTest(existing=(existing_generation, existing_force), new=(new_generation, new_force)):
                root = os.path.join(self.root, "followup-%d" % index)
                service = self.store(root)
                self.start(service)
                caller = self.store(root)
                self.submit_one(caller, watchlist_request(1, False))
                claim = service.claim_next(SERVICE_EPOCH, CLAIM_TOKEN)
                created = service.ensure_followup(claim, watchlist_request(existing_generation, existing_force))
                service.ensure_followup(claim, watchlist_request(new_generation, new_force))
                followup = next(job for job in self.manifest(root)["jobs"] if job["job_id"] == created["job_id"])
                self.assertEqual(followup["request"]["generation"], expected_generation)
                self.assertEqual(followup["request"]["force"], expected_force)

    def test_abort_claim_uses_sanitized_code_and_backlog_owner_changes_atomically(self):
        service = self.start()
        caller = self.store()
        for index in range(33):
            self.submit_one(caller, request(index + 1))
        active = service.claim_next(SERVICE_EPOCH, CLAIM_TOKEN)
        self.assertIsNone(service.abort_claim(active, "ABORTED"))
        self.assertIsNone(service.abort_claim(active, "ABORTED"))
        states = [job["state"] for job in self.manifest()["jobs"]]
        self.assertEqual(states.count("aborted"), 1)
        self.assertEqual(states.count("queued"), 32)
        promoted = service.claim_next(SERVICE_EPOCH, OTHER_TOKEN)
        self.assertNotEqual(promoted["job_id"], active["job_id"])

    def test_checkpoint_of_eight_successes_does_not_starve_interactive_work(self):
        service = self.start()
        caller = self.store()
        for index in range(8):
            row = self.submit_one(caller, request(index + 1))
            claim = service.claim_next(SERVICE_EPOCH, "%032x" % (index + 1))
            self.assertEqual(claim["ordinal"], index + 1)
            self.assertEqual(claim["attempts"], 0)
            self.assertEqual(claim["retry_at"], 0.0)
            service.complete(claim, result(row["job_id"], {"checkpoint": index + 1}))
        interactive = self.submit_one(caller, protocol.show_request(100), True, WAITER)
        background = self.submit_one(caller, request(200))
        caller.join(background["request"], waiter(2))
        self.assertEqual(service.claim_next(SERVICE_EPOCH, CLAIM_TOKEN)["job_id"], interactive["job_id"])

    def test_gc_and_expired_reads_have_bounded_typed_semantics(self):
        service = self.start()
        fake_ids = iter("%032x" % (index + 1) for index in range(40))
        caller = store_module.ServiceStateStore(self.root, id_factory=lambda _size: next(fake_ids))
        self.stores.append(caller)
        first = self.submit_one(caller, request(1), True, WAITER)
        claim = service.claim_next(SERVICE_EPOCH, CLAIM_TOKEN)
        service.complete(claim, result(first["job_id"]))
        caller.acknowledge(first["job_id"], WAITER)
        for index in range(32):
            row = self.submit_one(caller, request(index + 2))
            next_claim = service.claim_next(SERVICE_EPOCH, secrets.token_hex(16))
            service.complete(next_claim, result(row["job_id"]))
        expired = caller.read_wait_result(first["job_id"], WAITER)
        self.assertEqual(expired["outcome"], "failed")
        self.assertEqual(expired["error"]["code"], "RESULT_EXPIRED")


class PosixAdapterContractTests(unittest.TestCase):
    """Static adapter tests only; no live POSIX locking behavior is claimed."""

    def test_posix_adapter_uses_nonblocking_and_blocking_lockf_and_unlocks(self):
        module = PosixLockDouble()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = os.path.join(temporary.name, "identity.lock")
        real_open = os.open
        fd = real_open(path, os.O_CREAT | os.O_RDWR, 0o600)
        os.write(fd, b"\0")
        os.close(fd)
        lock = store_module._PosixFileLock(path, fcntl_module=module)
        try:
            self.assertTrue(lock.acquire(blocking=False))
            self.assertTrue(lock.acquired)
        finally:
            lock.release()
        self.assertTrue(any(call[1] & module.LOCK_NB for call in module.calls[:-1]))
        self.assertEqual(module.calls[-1][1], module.LOCK_UN)
        self.assertFalse(lock.acquired)

    def test_posix_adapter_closes_descriptors_on_nonblocking_contention(self):
        module = PosixLockDouble()
        module.locked = True
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        path = os.path.join(temporary.name, "transaction.lock")
        with open(path, "wb") as handle:
            handle.write(b"\0")
        lock = store_module._PosixFileLock(path, fcntl_module=module)
        self.assertFalse(lock.acquire(blocking=False))
        self.assertFalse(lock.acquired)
        lock.release()


if __name__ == "__main__":
    unittest.main()
