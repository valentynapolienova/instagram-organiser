import csv
import json
from pathlib import Path
import tempfile
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import uuid
import itertools

import organise as app
import collection_ops as collections


class FakeClient:
    user_id = "42"

    def __init__(self):
        self.calls = []
        self.members = []
        self.ignore_writes = False

    def with_action_data(self, data):
        return data

    def private_request(self, endpoint, data=None, params=None):
        if endpoint == "collections/list/":
            return {"status": "ok", "items": [{"collection_id": "99", "collection_name": "Recipes", "collection_type": "MEDIA"}]}
        if endpoint == "feed/collection/99/":
            return {"status": "ok", "items": [{"media": {"pk": pk}} for pk in self.members]}
        if endpoint == "collections/99/edit/":
            ids = json.loads(data["added_media_ids"])
            self.calls.append(("add", ids, 99))
            if not self.ignore_writes:
                for pk in ids:
                    if pk in self.members:
                        self.members.remove(pk)
                    self.members.insert(0, pk)
            return {"status": "ok", "collection_id": "99"}
        raise AssertionError(endpoint)

    def media_unsave(self, item):
        self.calls.append(("unsave", item))
        return True


class OrganiserTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.batch = Path(self.temp.name)
        self.inventory = {"account": "42", "items": ["123", "456"], "order": ["123", "456"]}
        app.save_json(self.batch / "inventory.json", self.inventory)
        self.client = FakeClient()

    def plan(self, rows):
        with (self.batch / "review.csv").open("w", newline="", encoding="utf-8-sig") as file:
            writer = csv.DictWriter(file, fieldnames=app.FIELDS)
            writer.writeheader()
            for item, action, collection in rows:
                writer.writerow({"item": item, "action": action, "collection": collection})
        return app.read_plan(self.batch)[1]

    def test_invalid_later_row_prevents_plan(self):
        with self.assertRaises(ValueError):
            self.plan([("ig:123", "unsave", ""), ("ig:999", "keep", "")])
        self.assertEqual(self.client.calls, [])

    def test_duplicate_and_damaged_ids_rejected(self):
        for rows in [[("ig:123", "keep", ""), ("ig:123", "unsave", "")], [("1.23E+17", "keep", "")]]:
            with self.assertRaises(ValueError):
                self.plan(rows)

    def test_keep_is_no_action(self):
        self.assertEqual(self.plan([("ig:123", "keep", "")]), [])

    def test_unsave_requires_explicit_flag(self):
        actions = self.plan([("ig:123", "unsave", "")])
        with self.assertRaises(ValueError):
            app.apply(self.client, self.batch, self.inventory, actions, False)
        self.assertEqual(self.client.calls, [])

    def test_wrong_account_prevents_writes(self):
        self.client.user_id = "different"
        with self.assertRaises(ValueError):
            app.apply(self.client, self.batch, self.inventory, self.plan([("ig:123", "unsave", "")]), True)
        self.assertEqual(self.client.calls, [])

    def test_unknown_collection_prevents_all_writes(self):
        actions = self.plan([("ig:123", "unsave", ""), ("ig:456", "add", "Missing")])
        with self.assertRaises(ValueError):
            app.apply(self.client, self.batch, self.inventory, actions, True)
        self.assertEqual(self.client.calls, [])

    def test_apply_and_repeat(self):
        actions = self.plan([("ig:123", "add", "Recipes"), ("ig:456", "unsave", "")])
        app.apply(self.client, self.batch, self.inventory, actions, True)
        app.apply(self.client, self.batch, self.inventory, actions, True)
        self.assertEqual(self.client.calls, [("add", ["123"], 99), ("unsave", "456")])

    def test_uncertain_failure_blocks_retry(self):
        actions = self.plan([("ig:123", "unsave", "")])
        def timeout(item):
            raise TimeoutError()
        self.client.media_unsave = timeout
        with self.assertRaises(TimeoutError):
            app.apply(self.client, self.batch, self.inventory, actions, True)
        self.assertIn("pending", json.loads((self.batch / "results.json").read_text()))
        with self.assertRaises(ValueError):
            app.apply(self.client, self.batch, self.inventory, actions, True)

    def test_caption_hints_are_never_decisions(self):
        self.assertIn("Review", app.hint("Vegan chicken with rice"))
        self.assertIn("Review", app.hint("Eggs and cheese"))
        self.assertTrue(app.cell("  =HYPERLINK(1)").startswith("'"))

    def test_missing_dependency_reports_module_and_interpreter(self):
        result = subprocess.run(
            [sys.executable, "-S", str(Path(app.__file__)), "export", "--batch", "dependency-test-" + uuid.uuid4().hex],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("missing Python module 'instagrapi'", result.stderr)
        self.assertIn(sys.executable, result.stderr)
        self.assertIn("-m pip install -r", result.stderr)
        self.assertNotIn("Check login/challenges", result.stderr)

    def test_blank_2fa_does_not_retry_login(self):
        class TwoFactorRequired(Exception):
            pass
        client = Mock()
        client.login.side_effect = TwoFactorRequired()
        modules = {
            "instagrapi": SimpleNamespace(Client=lambda: client),
            "instagrapi.exceptions": SimpleNamespace(TwoFactorRequired=TwoFactorRequired),
        }
        with patch.dict(sys.modules, modules), patch.object(app, "STATE", self.batch), patch("builtins.input", return_value="testuser"), patch.object(app.getpass, "getpass", side_effect=["test-password", ""]):
            with self.assertRaisesRegex(ValueError, "No verification code"):
                app.login()
        self.assertEqual(client.login.call_count, 1)
        client.dump_settings.assert_not_called()

    def test_saved_session_needs_no_credentials_or_login_requests(self):
        (self.batch / "testuser-session.json").write_text("{}")
        client = Mock(user_id=42)
        modules = {
            "instagrapi": SimpleNamespace(Client=lambda: client),
            "instagrapi.exceptions": SimpleNamespace(TwoFactorRequired=RuntimeError),
        }
        with patch.dict(sys.modules, modules), patch.object(app, "STATE", self.batch), patch("builtins.input", side_effect=AssertionError("Unexpected username prompt")), patch.object(app.getpass, "getpass", side_effect=AssertionError("Unexpected credential prompt")):
            self.assertIs(app.login(), client)
        client.load_settings.assert_called_once_with(self.batch / "testuser-session.json")
        client.login.assert_not_called()
        client.account_info.assert_not_called()
        client.dump_settings.assert_not_called()
        self.assertFalse(client.with_challenge_flow)
        client.set_retry_config.assert_called_once_with(session_retry_total=0)

    def test_invalid_saved_session_never_falls_back_to_login(self):
        (self.batch / "testuser-session.json").write_text("{}")
        client = Mock(user_id=None)
        modules = {
            "instagrapi": SimpleNamespace(Client=lambda: client),
            "instagrapi.exceptions": SimpleNamespace(TwoFactorRequired=RuntimeError),
        }
        with patch.dict(sys.modules, modules), patch.object(app, "STATE", self.batch), patch.object(app.getpass, "getpass", side_effect=AssertionError("Unexpected credential prompt")):
            with self.assertRaisesRegex(ValueError, "without attempting a new login"):
                app.login()
        client.login.assert_not_called()

    def test_saved_session_load_error_never_falls_back_to_login(self):
        (self.batch / "testuser-session.json").write_text("{}")
        client = Mock()
        client.load_settings.side_effect = ValueError("Invalid saved session")
        modules = {
            "instagrapi": SimpleNamespace(Client=lambda: client),
            "instagrapi.exceptions": SimpleNamespace(TwoFactorRequired=RuntimeError),
        }
        with patch.dict(sys.modules, modules), patch.object(app, "STATE", self.batch):
            with self.assertRaisesRegex(ValueError, "Invalid saved session"):
                app.login()
        client.login.assert_not_called()

    def test_export_paginated_and_default_keep(self):
        def page(endpoint, params):
            self.assertEqual(endpoint, "feed/saved/posts/")
            pk = "456" if params.get("max_id") else "123"
            return {"items": [{"media": {"pk": pk, "code": "ABC", "caption": {"text": "Chicken soup"}, "user": {"username": "cook"}}}], "next_max_id": "next" if pk == "123" else ""}
        self.client.private_request = page
        destination = self.batch / "export"
        app.export(self.client, destination, 0)
        inventory, actions = app.read_plan(destination)
        self.assertEqual(inventory["items"], ["123", "456"])
        self.assertEqual(inventory["order"], ["123", "456"])
        self.assertEqual(actions, [])

    def test_missing_shortcode_is_derived_without_network(self):
        self.client.media_code_from_pk = Mock(return_value="B7")
        self.client.private_request = Mock(side_effect=AssertionError("Unexpected network request"))
        row = app.saved_row(self.client, {"media": {"id": "123_42", "caption": {"text": "Беседа"}}})
        self.assertEqual(row["url"], "https://www.instagram.com/p/B7/")
        self.assertEqual(row["caption"], "Беседа")
        self.assertEqual(row["action"], "keep")
        self.client.media_code_from_pk.assert_called_once_with("123")
        self.client.private_request.assert_not_called()

    def test_export_resumes_using_cached_pages(self):
        first = {"items": [{"pk": "123", "code": "B7"}], "next_max_id": "next"}
        second = {"items": [{"pk": "456", "code": "HI"}], "next_max_id": ""}
        self.client.private_request = Mock(side_effect=[first, TimeoutError()])
        destination = self.batch / "export"
        with self.assertRaises(TimeoutError):
            app.export(self.client, destination, 0)
        self.assertTrue((destination / "pages" / "000001.json").exists())
        self.assertFalse((destination / "review.csv").exists())
        self.client.private_request = Mock(return_value=second)
        app.export(self.client, destination, 0)
        self.client.private_request.assert_called_once_with("feed/saved/posts/", params={"include_igtv_preview": "false", "max_id": "next"})
        self.assertEqual(app.read_plan(destination)[0]["items"], ["123", "456"])

    def test_unavailable_rows_are_reported(self):
        self.client.private_request = Mock(return_value={"items": [{"media": None}, {"pk": "123", "code": "B7"}]})
        destination = self.batch / "export"
        app.export(self.client, destination, 0)
        skipped = json.loads((destination / "skipped.json").read_text())
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0]["position"], 1)
        self.assertEqual(app.read_plan(destination)[0]["items"], ["123"])

    def test_finished_export_cannot_overwrite_review(self):
        self.client.private_request = Mock(return_value={"items": []})
        destination = self.batch / "export"
        app.export(self.client, destination, 0)
        original = (destination / "review.csv").read_bytes()
        with self.assertRaisesRegex(ValueError, "will not be overwritten"):
            app.export(self.client, destination, 0)
        self.assertEqual((destination / "review.csv").read_bytes(), original)
        self.assertEqual(self.client.private_request.call_count, 1)

    def test_resume_rejects_wrong_account_before_fetch(self):
        self.client.private_request = Mock(side_effect=TimeoutError())
        destination = self.batch / "export"
        with self.assertRaises(TimeoutError):
            app.export(self.client, destination, 0)
        self.client.private_request.reset_mock()
        self.client.user_id = "43"
        with self.assertRaisesRegex(ValueError, "same account"):
            app.export(self.client, destination, 0)
        self.client.private_request.assert_not_called()

    def test_exact_order_ignores_csv_order_and_preserves_members(self):
        self.client.members = ["123"]
        actions = self.plan([("ig:456", "add", "Recipes"), ("ig:123", "keep", "")])
        app.apply(self.client, self.batch, self.inventory, actions, False)
        self.assertEqual(self.client.members, ["123", "456"])
        self.assertEqual(self.client.calls, [("add", ["456"], 99), ("add", ["123"], 99)])

    def test_sorted_collection_requires_no_writes(self):
        self.client.members = ["123", "456"]
        app.apply(self.client, self.batch, self.inventory, [], False, sort_all=True)
        self.assertEqual(self.client.calls, [])

    def test_sort_preserves_manual_removals(self):
        self.client.members = ["456"]
        app.apply(self.client, self.batch, self.inventory, [], False, sort_all=True)
        self.assertEqual(self.client.members, ["456"])
        self.assertEqual(self.client.calls, [])

    def test_unknown_live_members_stop_before_any_write(self):
        self.client.members = ["999"]
        actions = self.plan([("ig:123", "add", "Recipes"), ("ig:456", "unsave", "")])
        with self.assertRaisesRegex(ValueError, "outside this export"):
            app.apply(self.client, self.batch, self.inventory, actions, True)
        self.assertEqual(self.client.calls, [])

    def test_fast_batches_at_most_30_skip_existing_and_verify(self):
        ids = [str(n) for n in range(1, 66)]
        self.client.members = ["65"]
        inventory = {"account": "42", "items": ids}
        actions = [{"id": pk, "action": "add", "collection": "Recipes"} for pk in ids]
        app.apply(self.client, self.batch, inventory, actions, False, fast=True)
        self.assertEqual([len(call[1]) for call in self.client.calls], [30, 30, 4])
        self.assertEqual(set(self.client.members), set(ids))

    def test_silent_success_is_detected_and_cannot_retry(self):
        self.client.ignore_writes = True
        actions = self.plan([("ig:123", "add", "Recipes")])
        with self.assertRaisesRegex(ValueError, "verification failed"):
            app.apply(self.client, self.batch, self.inventory, actions, False, fast=True)
        with self.assertRaisesRegex(ValueError, "Uncertain collection result"):
            app.apply(self.client, self.batch, self.inventory, actions, False, fast=True)
        self.assertEqual(len(self.client.calls), 1)

    def test_collection_timeout_leaves_pending_and_blocks_retry(self):
        request = self.client.private_request
        def fail(endpoint, *args, **kwargs):
            if endpoint.endswith("/edit/"):
                raise TimeoutError()
            return request(endpoint, *args, **kwargs)
        self.client.private_request = fail
        actions = self.plan([("ig:123", "add", "Recipes")])
        with self.assertRaises(TimeoutError):
            app.apply(self.client, self.batch, self.inventory, actions, False)
        with self.assertRaisesRegex(ValueError, "Uncertain collection result"):
            app.apply(self.client, self.batch, self.inventory, actions, False)

    def test_changed_plan_cannot_reuse_collection_journal(self):
        actions = self.plan([("ig:123", "add", "Recipes")])
        app.apply(self.client, self.batch, self.inventory, actions, False)
        changed = self.plan([("ig:456", "add", "Recipes")])
        with self.assertRaisesRegex(ValueError, "plan changed"):
            app.apply(self.client, self.batch, self.inventory, changed, False)

    def test_legacy_completed_additions_cannot_restore_manual_removals(self):
        app.save_json(self.batch / "results.json", {"done": ["add:123:99"]})
        actions = self.plan([("ig:123", "add", "Recipes")])
        with self.assertRaisesRegex(ValueError, "older collection writer"):
            app.apply(self.client, self.batch, self.inventory, actions, False)
        self.assertEqual(self.client.calls, [])

    def test_resume_skips_acknowledged_moves_and_checks_manual_changes(self):
        actions = self.plan([("ig:123", "add", "Recipes"), ("ig:456", "add", "Recipes")])
        real_save = app.save_json
        def interrupt_after_first_ack(path, data):
            real_save(path, data)
            if data.get("records") and data["records"][0]["completed"] == 1 and "pending" not in data:
                raise KeyboardInterrupt()
        with patch.object(app, "save_json", interrupt_after_first_ack), self.assertRaises(KeyboardInterrupt):
            app.apply(self.client, self.batch, self.inventory, actions, False)
        self.assertEqual(self.client.members, ["456"])
        self.client.members.append("999")
        with self.assertRaisesRegex(ValueError, "changed since planning"):
            app.apply(self.client, self.batch, self.inventory, actions, False)
        self.assertEqual(len(self.client.calls), 1)
        self.client.members.remove("999")
        app.apply(self.client, self.batch, self.inventory, actions, False)
        self.assertEqual(self.client.members, ["123", "456"])
        self.assertEqual(len(self.client.calls), 2)

    def test_legacy_rank_uses_cached_pages_not_editable_csv(self):
        inventory = {"account": "42", "items": ["123", "456"]}
        self.plan([("ig:456", "keep", ""), ("ig:123", "keep", "")])
        (self.batch / "pages").mkdir()
        app.save_json(self.batch / "pages/000001.json", {"items": [{"pk": "123"}, {"pk": "456"}]})
        self.assertEqual(collections.saved_order(self.batch, inventory), ["123", "456"])

    def test_missing_original_order_stops_without_requests(self):
        del self.inventory["order"]
        self.client.private_request = Mock(side_effect=AssertionError("Unexpected request"))
        actions = self.plan([("ig:123", "add", "Recipes")])
        with self.assertRaisesRegex(ValueError, "Original saved order"):
            app.apply(self.client, self.batch, self.inventory, actions, False)

    def test_pagination_detects_missing_and_repeated_cursors(self):
        for responses in [
            [{"status": "ok", "items": [], "more_available": True}],
            [{"status": "ok", "items": [{"pk": "123"}], "next_max_id": "same"}] * 2,
        ]:
            self.client.private_request = Mock(side_effect=responses)
            with self.assertRaises(ValueError):
                list(collections.paginated(self.client, "test", {}))

    def test_collection_listing_failure_is_not_partial_success(self):
        self.client.private_request = Mock(side_effect=[
            {"status": "ok", "items": [{"collection_id": "99"}], "next_max_id": "next"},
            TimeoutError(),
        ])
        with self.assertRaises(TimeoutError):
            collections.list_collections(self.client)

    def test_single_attempt_preserves_auth_delay_and_never_retries(self):
        client = Mock(authorization="test-token", delay_range=[3, 6])
        client._send_private_request.side_effect = TimeoutError()
        collections.single_attempt_requests(client)
        with patch.object(collections.time, "sleep") as sleep:
            with self.assertRaises(TimeoutError):
                client.private_request("test/", data={"x": 1})
        sleep.assert_called_once()
        self.assertTrue(3 <= sleep.call_args.args[0] <= 6)
        client._send_private_request.assert_called_once_with("test/", data={"x": 1}, headers={"Authorization": "test-token"})

    def test_logout_removes_only_confirmed_session(self):
        session = self.batch / "testuser-session.json"
        session.write_text("{}")
        review = self.batch / "review.csv"
        review.write_text("keep this review")
        client = Mock(user_id=42)
        client.logout.return_value = True
        with patch.dict(sys.modules, {"instagrapi": SimpleNamespace(Client=lambda: client)}), patch.object(app, "STATE", self.batch):
            app.logout_saved_session()
        client.load_settings.assert_called_once_with(session)
        client.logout.assert_called_once_with()
        client.login.assert_not_called()
        self.assertFalse(session.exists())
        self.assertEqual(review.read_text(), "keep this review")

    def test_logout_failure_keeps_session_without_retry(self):
        session = self.batch / "testuser-session.json"
        session.write_text("{}")
        for outcome in [False, TimeoutError()]:
            client = Mock(user_id=42)
            if isinstance(outcome, Exception):
                client.logout.side_effect = outcome
            else:
                client.logout.return_value = outcome
            with patch.dict(sys.modules, {"instagrapi": SimpleNamespace(Client=lambda: client)}), patch.object(app, "STATE", self.batch):
                with self.assertRaises((RuntimeError, TimeoutError)):
                    app.logout_saved_session()
            client.logout.assert_called_once_with()
            client.login.assert_not_called()
            self.assertTrue(session.exists())

    def test_logout_without_session_never_creates_client(self):
        factory = Mock(side_effect=AssertionError("Unexpected client"))
        with patch.dict(sys.modules, {"instagrapi": SimpleNamespace(Client=factory)}), patch.object(app, "STATE", self.batch):
            app.logout_saved_session()
        factory.assert_not_called()

    def test_versions_select_mode_and_preview_without_login(self):
        self.plan([("ig:123", "add", "Recipes")])
        for variant in ["ordered", "unordered"]:
            with patch.object(app, "STATE", self.batch.parent), patch.object(app, "login", side_effect=AssertionError("Unexpected login")), patch.object(sys, "argv", [variant + ".py", "apply", "--batch", self.batch.name]), patch("builtins.print") as output:
                app.main(variant)
            printed = " ".join(str(call) for call in output.call_args_list)
            self.assertIn("Preview only", printed)
            self.assertIn("Fast mode" if variant == "unordered" else "newest saved first", printed)

    def test_unordered_cannot_sort_and_ordered_cannot_choose_fast(self):
        for variant, arguments in [("unordered", ["sort"]), ("ordered", ["apply", "--fast-collections"])]:
            with patch.object(sys, "argv", [variant + ".py", *arguments]), patch("sys.stderr"), self.assertRaises(SystemExit) as error:
                app.main(variant)
            self.assertEqual(error.exception.code, 2)

    def test_minimum_moves_for_every_small_permutation_and_additions(self):
        desired = ["1", "2", "3", "4", "5"]
        for size in range(6):
            for current_tuple in itertools.permutations(desired, size):
                current = list(current_tuple)
                moves = collections.moves_to_front(current, desired)
                actual = list(current)
                for pk in moves:
                    if pk in actual:
                        actual.remove(pk)
                    actual.insert(0, pk)
                self.assertEqual(actual, desired)
                # A k-write solution leaves a suffix of desired untouched.
                for fewer in range(len(moves)):
                    suffix = desired[fewer:]
                    self.assertNotEqual([pk for pk in current if pk in suffix], suffix)


if __name__ == "__main__":
    unittest.main()
