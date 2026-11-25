import copy
import json
import math
import os
import tempfile
import threading
import unittest

from yawsp_app.service_queue import (
    DuplicateJob,
    QueueValidationError,
    ServiceJobQueue,
    build_metadata_document,
    build_shared_document,
    build_watchlist_document,
    session_binding_digest,
    validate_document,
)


JOB_ID = "0123456789abcdef0123456789abcdef"
OTHER_JOB_ID = "fedcba9876543210fedcba9876543210"


def metadata_item(family="trailer", media_id=42, attempts=0, retry_at=0.0):
    if family == "trailer":
        return {
            "family": "trailer",
            "cache_key": "movie:{0}".format(media_id),
            "media": "movie",
            "media_id": media_id,
            "attempts": attempts,
            "retry_at": retry_at,
        }
    return {
        "family": "show",
        "cache_key": str(media_id),
        "media": None,
        "media_id": media_id,
        "attempts": attempts,
        "retry_at": retry_at,
    }


def metadata_document(items=None, **changes):
    document = build_metadata_document([metadata_item()] if items is None else items, job_id=JOB_ID, now=100.0)
    document.update(changes)
    return document


def shared_document(**changes):
    document = build_shared_document(("trailer", "movie", 42), job_id=JOB_ID, now=100.0)
    document.update(changes)
    return document


def trailer_result_for_test():
    return {
        "trailer/movie/42": {
            "kind": "trailer",
            "status": "hit",
            "youtube_id": "video-42",
        }
    }


def watchlist_document(**changes):
    scope = {"account_id": "account-1", "media": "movies", "language": "en-US"}
    document = build_watchlist_document(
        scope,
        "session-secret-value",
        generation=7,
        refresh_due=True,
        allow_missing_account=False,
        job_id=JOB_ID,
        now=100.0,
    )
    document.update(changes)
    return document


class SchemaTests(unittest.TestCase):
    def assertInvalid(self, document):
        with self.assertRaises(QueueValidationError):
            validate_document(document)

    def assertInvalidBuild(self, builder, *args, **kwargs):
        with self.assertRaises(QueueValidationError):
            builder(*args, **kwargs)

    def test_metadata_normalizes_only_exact_typed_items(self):
        result = validate_document(metadata_document())
        self.assertEqual(result["operation"], "run_metadata")
        self.assertEqual(result["dedup_keys"], ["trailer/movie/42"])
        self.assertEqual(result["payload"]["items"], [metadata_item()])

        show = build_metadata_document([metadata_item("show", 9)], job_id=JOB_ID)
        self.assertEqual(validate_document(show)["dedup_keys"], ["show/9"])

    def test_all_supported_documents_reject_unknown_or_missing_members(self):
        for document in (metadata_document(), shared_document(), watchlist_document()):
            with self.subTest(operation=document["operation"], change="unknown"):
                changed = copy.deepcopy(document)
                changed["callback"] = "main.router"
                self.assertInvalid(changed)
            with self.subTest(operation=document["operation"], change="missing"):
                changed = copy.deepcopy(document)
                del changed["created_at"]
                self.assertInvalid(changed)
            with self.subTest(operation=document["operation"], change="nested unknown"):
                changed = copy.deepcopy(document)
                changed["payload"]["path"] = "C:/tmp/execute.py"
                self.assertInvalid(changed)

    def test_metadata_rejects_bad_keys_types_ranges_and_duplicate_items(self):
        changes = (
            ("schema", True),
            ("schema", 2),
            ("created_at", True),
            ("created_at", math.inf),
            ("created_at", -1.0),
            ("job_id", "../../escape"),
            ("operation", "exec"),
        )
        for key, value in changes:
            with self.subTest(key=key, value=value):
                self.assertInvalid(metadata_document(**{key: value}))

        bad_items = []
        for key, value in (
            ("media_id", True),
            ("media_id", 0),
            ("media_id", 2**63),
            ("attempts", True),
            ("attempts", 2),
            ("retry_at", True),
            ("retry_at", math.nan),
            ("retry_at", -0.1),
            ("family", "genre"),
            ("media", "tv"),
            ("cache_key", "tv:42"),
        ):
            item = metadata_item()
            item[key] = value
            bad_items.append(item)
        bad_items.extend((None, {"family": "trailer"}, metadata_item(), metadata_item()))
        for item in bad_items:
            with self.subTest(item=item):
                items = [metadata_item(), metadata_item()] if item == metadata_item() else [item]
                self.assertInvalidBuild(build_metadata_document, items, job_id=JOB_ID)

        with self.subTest("too many items"):
            items = []
            for media_id in range(1, 34):
                item = metadata_item(media_id=media_id)
                item["cache_key"] = "movie:{0}".format(media_id)
                items.append(item)
            self.assertInvalidBuild(build_metadata_document, items, job_id=JOB_ID)

    def test_show_identity_and_trailer_tv_identity_are_exact(self):
        bad_show = metadata_item("show", 42)
        bad_show["media"] = "tv"
        self.assertInvalidBuild(build_metadata_document, [bad_show], job_id=JOB_ID)
        bad_show = metadata_item("show", 42)
        bad_show["cache_key"] = "tv:42"
        self.assertInvalidBuild(build_metadata_document, [bad_show], job_id=JOB_ID)

        tv = metadata_item(media_id=42)
        tv.update(media="tv", cache_key="tv:42")
        result = validate_document(build_metadata_document([tv], job_id=JOB_ID))
        self.assertEqual(result["dedup_keys"], ["trailer/tv/42"])

    def test_shared_keys_are_typed_not_raw_operations(self):
        self.assertEqual(
            validate_document(shared_document())["payload"]["key"],
            ["trailer", "movie", 42],
        )
        show = build_shared_document(("show", 42), job_id=JOB_ID)
        self.assertEqual(validate_document(show)["payload"]["key"], ["show", 42])
        for key in (
            ("genre", "movie", 42),
            ("trailer", "tv", True),
            ("trailer", "movie", 0),
            ("show", "42"),
            ("show", 42, 1),
            "trailer/movie/42",
        ):
            with self.subTest(key=key):
                self.assertInvalidBuild(build_shared_document, key, job_id=JOB_ID)

    def test_watchlist_scope_digest_flags_and_generation_are_strict(self):
        document = validate_document(watchlist_document())
        self.assertEqual(document["payload"]["scope"]["account_id"], "account-1")
        self.assertEqual(len(document["payload"]["session_digest"]), 64)
        for key, value in (
            ("generation", True),
            ("generation", -1),
            ("refresh_due", 1),
            ("allow_missing_account", 0),
            ("followup", 1),
            ("session_digest", "0" * 63),
            ("session_digest", "G" * 64),
        ):
            with self.subTest(key=key, value=value):
                changed = watchlist_document()
                changed["payload"][key] = value
                self.assertInvalid(changed)
        for scope in (
            {"account_id": "", "media": "movies", "language": "en-US"},
            {"account_id": "../account", "media": "movies", "language": "en-US"},
            {"account_id": "account", "media": "movie", "language": "en-US"},
            {"account_id": "account", "media": "movies", "language": ""},
            {"account_id": "account", "media": "movies", "language": "en-US", "token": "x"},
        ):
            with self.subTest(scope=scope):
                self.assertInvalidBuild(
                    build_watchlist_document,
                        scope,
                        "secret",
                        generation=0,
                        refresh_due=True,
                        allow_missing_account=False,
                        job_id=JOB_ID,
                    )

    def test_no_auth_material_or_executable_location_is_accepted_or_written(self):
        secret = "session-secret-value"
        with tempfile.TemporaryDirectory() as root:
            queue = ServiceJobQueue(root)
            job_id = queue.publish(watchlist_document())
            with open(os.path.join(root, "pending", job_id + ".json"), encoding="utf-8") as stream:
                persisted = stream.read()
            self.assertNotIn(secret, persisted)
            self.assertNotIn("session-secret", persisted)
            self.assertIn("session_digest", persisted)
            for key in ("token", "password", "authorization", "callback", "url", "path", "session_id"):
                changed = watchlist_document()
                changed["payload"][key] = secret
                self.assertInvalid(changed)

    def test_digest_binds_session_and_complete_scope_without_containing_session(self):
        first = session_binding_digest("secret-one", "a", "movies", "en-US")
        self.assertNotIn("secret-one", first)
        self.assertEqual(first, session_binding_digest("secret-one", "a", "movies", "en-US"))
        for values in (
            ("secret-two", "a", "movies", "en-US"),
            ("secret-one", "b", "movies", "en-US"),
            ("secret-one", "a", "tv", "en-US"),
            ("secret-one", "a", "movies", "cs-CZ"),
        ):
            with self.subTest(values=values):
                self.assertNotEqual(first, session_binding_digest(*values))


class QueueLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.queue = ServiceJobQueue(self.temporary.name)

    def test_invalid_publish_creates_no_files_or_work(self):
        before = set(os.listdir(self.temporary.name))
        with self.assertRaises(QueueValidationError):
            self.queue.publish(metadata_document(operation="invalid"))
        self.assertEqual(set(os.listdir(self.temporary.name)), before)

    def test_publish_claim_complete_and_duplicate_terminal_are_exactly_once(self):
        job_id = self.queue.publish(metadata_document())
        claim = self.queue.claim_one()
        self.assertEqual(claim.job_id, job_id)
        self.assertIsNone(self.queue.claim_one())
        results = {
            "trailer/movie/42": {
                "kind": "trailer",
                "status": "hit",
                "youtube_id": "video-42",
            }
        }
        self.queue.complete(claim, results)
        terminal = self.queue.read_terminal(job_id)
        self.assertEqual(terminal["terminal"]["state"], "completed")
        self.assertEqual(terminal["terminal"]["results"], results)
        with self.assertRaises(DuplicateJob):
            self.queue.publish(metadata_document())
        with self.assertRaises(QueueValidationError):
            self.queue.complete(claim, results)
        self.assertIsNone(self.queue.claim_one())

    def test_second_publisher_cannot_replace_published_claim(self):
        first = self.queue.publish(metadata_document())
        other = ServiceJobQueue(self.temporary.name)
        with self.assertRaises(DuplicateJob):
            other.publish(metadata_document())
        with open(os.path.join(self.temporary.name, "pending", first + ".json"), encoding="utf-8") as stream:
            self.assertEqual(json.load(stream)["job_id"], JOB_ID)

    def test_competing_consumers_have_only_one_claim_owner(self):
        self.queue.publish(metadata_document())
        other = ServiceJobQueue(self.temporary.name)
        claims = []
        barrier = threading.Barrier(3)

        def claim(queue):
            barrier.wait()
            value = queue.claim_one()
            if value is not None:
                claims.append(value)

        threads = [threading.Thread(target=claim, args=(queue,)) for queue in (self.queue, other)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(2)
        self.assertEqual(len(claims), 1)
        self.assertFalse(any(thread.is_alive() for thread in threads))

    def test_startup_aborts_stale_pending_active_and_malformed_without_replay(self):
        pending = self.queue.publish(metadata_document())
        active = self.queue.publish(
            metadata_document(job_id=OTHER_JOB_ID, items=[metadata_item("show", 42)])
        )
        claim = self.queue.claim_one()
        if claim.job_id != active:
            pending, active = active, pending
        pending_root = os.path.join(self.temporary.name, "pending")
        malformed_name = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa.json"
        with open(os.path.join(pending_root, malformed_name), "w", encoding="utf-8") as stream:
            stream.write('{"schema":')

        recovered = self.queue.recover_stale()

        self.assertEqual(recovered, 3)
        self.assertIsNone(self.queue.claim_one())
        for job_id in (JOB_ID, OTHER_JOB_ID, "a" * 32):
            with self.subTest(job_id=job_id):
                path = os.path.join(self.temporary.name, "aborted", job_id + ".json")
                self.assertTrue(os.path.isfile(path))

    def test_rejected_and_aborted_are_not_consumed_or_completed(self):
        for state in ("rejected", "aborted"):
            with self.subTest(state=state):
                root = tempfile.TemporaryDirectory()
                self.addCleanup(root.cleanup)
                queue = ServiceJobQueue(root.name)
                job_id = queue.publish(metadata_document())
                claim = queue.claim_one()
                getattr(queue, "reject" if state == "rejected" else "abort")(claim)
                self.assertEqual(queue.read_terminal(job_id)["terminal"]["state"], state)
                self.assertIsNone(queue.claim_one())
                with self.assertRaises(QueueValidationError):
                    queue.complete(claim, {})

    def test_same_key_cannot_publish_twice_under_different_generated_ids(self):
        self.queue.publish(metadata_document())
        with self.assertRaises(DuplicateJob):
            self.queue.publish(metadata_document(job_id=OTHER_JOB_ID))
        claim = self.queue.claim_one()
        self.queue.complete(claim, trailer_result_for_test())
        # A terminal cache result does not permanently ban a later TTL refresh.
        self.assertEqual(self.queue.publish(metadata_document(job_id=OTHER_JOB_ID)), OTHER_JOB_ID)

    def test_shared_collision_uses_existing_background_manifest(self):
        job_id = self.queue.publish(metadata_document())
        self.assertEqual(self.queue.find_nonterminal("trailer/movie/42"), job_id)
        self.assertEqual(self.queue.find_nonterminal("show/42"), None)
        other = self.queue.publish(metadata_document(job_id=OTHER_JOB_ID, items=[metadata_item("show", 42)]))
        self.assertEqual(other, OTHER_JOB_ID)
        self.assertEqual(self.queue.find_nonterminal("show/42"), OTHER_JOB_ID)

    def test_terminal_result_rejects_malformed_or_conflicting_members(self):
        job_id = self.queue.publish(metadata_document())
        claim = self.queue.claim_one()
        with self.assertRaises(QueueValidationError):
            self.queue.complete(claim, {"trailer/movie/42": {"kind": "trailer", "status": "hit", "youtube_id": 42}})
        self.queue.abort(claim)
        with self.assertRaises(QueueValidationError):
            self.queue.read_terminal(job_id, require_completed=True)


if __name__ == "__main__":
    unittest.main()
