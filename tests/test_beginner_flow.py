"""User journeys for readable commands, safe defaults and explicit role matching."""
import copy
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import main
from friendly import error_text, layout_text, report_summary, template_embed, template_label, totals
from storage import Store
from structure import Copier, capture, validate_snapshot
from test_bot import guild, raw, role


def interaction(server_id=200, user_id=42):
    return SimpleNamespace(
        guild_id=server_id, user=SimpleNamespace(id=user_id),
        permissions=SimpleNamespace(administrator=True),
        response=SimpleNamespace(defer=AsyncMock(), edit_message=AsyncMock()),
        edit_original_response=AsyncMock(),
    )


class BeginnerFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = Store(Path(folder.name))
        self.copier = Copier(self.store)
        self.source, _ = guild(channels=[raw(1, permission_overwrites=[
            {"id": "110", "type": 0, "allow": "1024", "deny": "2048"}])],
            roles=[role(100, 8), {**role(110), "name": "Moderators"}])
        self.destination, self.http = guild(200, roles=[role(200, 8), role(210), role(220), role(230, managed=True)])
        self.snapshot = capture(self.source, self.source.channels)
        self.snapshot["label"] = "Gaming layout"
        self.store.write("snapshots", self.snapshot["id"], self.snapshot)
        for item in (patch.object(main, "store", self.store), patch.object(main, "copier", self.copier)):
            item.start()
            self.addCleanup(item.stop)

    async def test_copy_from_receiving_server_checks_both_memberships(self):
        click = interaction()
        async def admin(user, server):
            return self.destination if server == 200 else self.source
        with patch.object(main, "require_admin", AsyncMock(side_effect=admin)) as check, \
             patch.object(main.bot, "get_guild", return_value=self.source), \
             patch.object(main, "require_bot", AsyncMock(return_value=self.destination)):
            snapshot, target = await main.copy_access(click, self.snapshot["id"], None)
        self.assertEqual(target.id, 200)
        self.assertEqual(snapshot["id"], self.snapshot["id"])
        self.assertIn((42, 100), [call.args for call in check.await_args_list])
        self.assertGreaterEqual(sum(call.args == (42, 200) for call in check.await_args_list), 1)
        self.assertEqual(self.http.calls, [])

    async def test_missing_source_membership_is_not_bypassed_after_auto_leave(self):
        with patch.object(main, "require_admin", AsyncMock(return_value=self.destination)), \
             patch.object(main.bot, "get_guild", return_value=None):
            with self.assertRaisesRegex(ValueError, "original server"):
                await main.source_snapshot(interaction(), self.snapshot["id"])

    async def test_source_administrator_revocation_still_blocks_access(self):
        async def admin(user, server):
            if server == 100:
                raise ValueError("Source administrator access removed")
            return self.destination
        with patch.object(main, "require_admin", AsyncMock(side_effect=admin)), \
             patch.object(main.bot, "get_guild", return_value=self.source):
            with self.assertRaisesRegex(ValueError, "access removed"):
                await main.source_snapshot(interaction(), self.snapshot["id"])

    async def test_lists_and_autocomplete_exclude_unauthorized_source_templates(self):
        async def admin(user, server):
            if server == 100:
                raise ValueError("not administrator")
            return self.destination
        with patch.object(main, "require_admin", AsyncMock(side_effect=admin)), \
             patch.object(main.bot, "get_guild", return_value=self.source):
            self.assertEqual(await main.accessible_templates(interaction()), [])
            self.assertEqual(await main.snapshot_choices(interaction(), "Gaming"), [])

    async def test_default_copy_uses_latest_other_server_template(self):
        local = copy.deepcopy(self.snapshot)
        local["source"]["id"] = "200"
        local["created_at"] = "2026-10-25T00:00:00+00:00"
        with patch.object(main, "accessible_templates", AsyncMock(return_value=[local, self.snapshot])), \
             patch.object(main, "source_snapshot", AsyncMock(return_value=(self.snapshot, self.source))) as select, \
             patch.object(main, "require_admin", AsyncMock(return_value=self.destination)), \
             patch.object(main, "require_bot", AsyncMock(return_value=self.destination)):
            await main.copy_access(interaction(), None, None)
        self.assertEqual(select.await_args.args[1], self.snapshot["id"])

    async def test_no_templates_explains_scan_and_reinvite(self):
        with patch.object(main, "accessible_templates", AsyncMock(return_value=[])):
            with self.assertRaisesRegex(ValueError, "/scan.*re-invite"):
                await main.copy_access(interaction(), None, None)

    async def test_copy_plan_can_create_all_channels_without_manual_role_matching(self):
        with patch.object(main, "copy_access", AsyncMock(return_value=(self.snapshot, self.destination))), \
             patch.object(main, "reply", AsyncMock()) as send:
            await main.present_copy_plan(interaction(), None, None, {})
        view = send.call_args.kwargs["view"]
        self.assertFalse(view.confirm.disabled)
        self.assertIn("Use existing roles (optional)", [item.label for item in view.children])
        self.assertEqual(self.http.calls, [])
        self.assertIn("Gaming layout", send.call_args.kwargs["embed"].description)

    async def test_copy_plan_reuses_saved_role_matches_without_retyping(self):
        job = self.copier.job(self.snapshot, self.destination)
        job["role_map"] = {"110": "210"}
        self.store.write("jobs", self.copier.key(self.snapshot, self.destination), job)
        with patch.object(main, "copy_access", AsyncMock(return_value=(self.snapshot, self.destination))), \
             patch.object(main, "reply", AsyncMock()) as send:
            await main.present_copy_plan(interaction(), None, None, {})
        view = send.call_args.kwargs["view"]
        self.assertFalse(view.confirm.disabled)
        self.assertNotIn("Use existing roles (optional)", [item.label for item in view.children])
        self.assertEqual(self.http.calls, [])

    async def test_new_confirmation_rechecks_authorization_before_background_copy(self):
        access = AsyncMock(side_effect=[(self.snapshot, self.destination), ValueError("Administrator removed")])
        with patch.object(main, "copy_access", access), patch.object(main, "reply", AsyncMock()) as send, \
             patch.object(main, "run_copy", AsyncMock()) as run:
            await main.present_copy_plan(interaction(), self.snapshot["id"], "200", {"110": "210"})
            view = send.call_args.kwargs["view"]
            await view.confirm.callback(interaction())
        run.assert_not_awaited()
        self.assertEqual(self.http.calls, [])

    def matcher(self, missing=None):
        return main.RoleMatcher(42, self.snapshot["id"], "200", {}, missing or ["110"], {"110": "Moderators", "120": "Members"})

    async def test_role_picker_matches_explicit_choice_then_returns_to_preview(self):
        view = self.matcher()
        with patch.object(main, "require_admin", AsyncMock(return_value=self.destination)), \
             patch.object(main, "source_snapshot", AsyncMock(return_value=(self.snapshot, self.source))), \
             patch.object(main, "present_copy_plan", AsyncMock()) as plan:
            await view.apply_role(interaction(), 210)
        self.assertEqual(view.mapping, {"110": "210"})
        self.assertTrue(view.used)
        self.assertEqual(plan.await_args.args[-1], {"110": "210"})
        self.assertEqual(self.http.calls, [])

    async def test_role_picker_rejects_default_managed_and_missing_roles(self):
        for selected_id in (200, 230, 999):
            with self.subTest(selected_id=selected_id):
                view = self.matcher()
                with patch.object(main, "require_admin", AsyncMock(return_value=self.destination)), \
                     patch.object(main, "source_snapshot", AsyncMock(return_value=(self.snapshot, self.source))):
                    with self.assertRaisesRegex(ValueError, "normal role"):
                        await view.apply_role(interaction(), selected_id)
                self.assertEqual(view.mapping, {})
                self.assertEqual(view.index, 0)

    async def test_role_picker_rechecks_original_server_access(self):
        view = self.matcher()
        with patch.object(main, "require_admin", AsyncMock(return_value=self.destination)), \
             patch.object(main, "source_snapshot", AsyncMock(side_effect=ValueError("access removed"))):
            with self.assertRaisesRegex(ValueError, "access removed"):
                await view.apply_role(interaction(), 210)
        self.assertEqual(view.mapping, {})

    async def test_old_role_step_cannot_match_the_next_role(self):
        view = self.matcher(["110", "120"])
        click = interaction()
        with patch.object(main, "require_admin", AsyncMock(return_value=self.destination)), \
             patch.object(main, "source_snapshot", AsyncMock(return_value=(self.snapshot, self.source))), \
             patch.object(main, "present_copy_plan", AsyncMock()) as plan, \
             patch.object(main, "reply", AsyncMock()):
            await view.apply_role(click, 210)
            next_view = click.edit_original_response.call_args.kwargs["view"]
            await view.apply_role(click, 220)
            self.assertEqual(view.mapping, {"110": "210"})
            await next_view.apply_role(interaction(), 220)
        self.assertEqual(plan.await_args.args[-1], {"110": "210", "120": "220"})

    async def test_cannot_match_two_source_roles_to_one_destination_role(self):
        view = self.matcher()
        view.mapping = {"120": "210"}
        with patch.object(main, "require_admin", AsyncMock(return_value=self.destination)), \
             patch.object(main, "source_snapshot", AsyncMock(return_value=(self.snapshot, self.source))):
            with self.assertRaisesRegex(ValueError, "already matched"):
                await view.apply_role(interaction(), 210)

    async def test_other_users_cannot_use_template_or_role_menus(self):
        with patch.object(main, "reply", AsyncMock()):
            self.assertFalse(await self.matcher().interaction_check(interaction(user_id=99)))
            self.assertFalse(await main.TemplateList(42, [self.snapshot]).interaction_check(interaction(user_id=99)))

    async def test_permission_picker_does_not_open_for_another_server_override(self):
        with patch.object(main, "copy_access", AsyncMock(return_value=(self.snapshot, self.destination))), \
             patch.object(main, "reply", AsyncMock()) as send:
            await main.present_copy_plan(interaction(server_id=100), self.snapshot["id"], "200", {})
        self.assertNotIn("Use existing roles (optional)", [item.label for item in send.call_args.kwargs["view"].children])
        fields = send.call_args.kwargs["embed"].fields
        self.assertTrue(any("automatically" in field.value for field in fields))

    async def test_skipping_roles_requires_a_new_plan_before_creation(self):
        view = self.matcher()
        with patch.object(main, "present_copy_plan", AsyncMock()) as plan:
            await view.skip_remaining.callback(interaction())
        self.assertTrue(view.used)
        self.assertEqual(plan.await_args.args[-1], {})
        self.assertEqual(self.http.calls, [])

    async def test_role_matching_cancel_is_single_use(self):
        view = self.matcher()
        click = interaction()
        with patch.object(main, "present_copy_plan", AsyncMock()) as plan, \
             patch.object(main, "reply", AsyncMock()):
            await view.cancel_matching.callback(click)
            await view.skip_remaining.callback(interaction())
        plan.assert_not_awaited()
        self.assertIn("Cancelled", click.response.edit_message.call_args.kwargs["content"])

    async def test_named_scan_persists_name_before_departing(self):
        source = SimpleNamespace(id=100, fetch_channels=AsyncMock(return_value=[]), leave=AsyncMock())
        with patch.object(main, "require_admin", AsyncMock(return_value=source)), \
             patch.object(main, "require_bot", AsyncMock(return_value=source)), \
             patch.object(main, "capture", return_value=self.snapshot), \
             patch.object(main, "reply", AsyncMock()):
            await main.snapshot_command.callback(interaction(server_id=100), name="  Weekend layout  ")
        self.assertEqual(self.store.read("snapshots", self.snapshot["id"])["label"], "Weekend layout")
        source.leave.assert_awaited_once()

    async def test_empty_template_list_shows_next_steps(self):
        with patch.object(main, "accessible_templates", AsyncMock(return_value=[])), \
             patch.object(main, "reply", AsyncMock()) as send:
            await main.snapshots_command.callback(interaction())
        self.assertIn("/scan", send.call_args.args[1])
        self.assertIn("Re-invite", send.call_args.args[1])

    async def test_template_picker_pages_more_than_discord_menu_limit(self):
        snapshots = []
        for index in range(32):
            item = copy.deepcopy(self.snapshot)
            item["id"] = f"{index:032x}"
            snapshots.append(item)
        view = main.TemplateList(42, snapshots)
        self.assertEqual(len(view.choose.options), 10)
        self.assertTrue(view.previous.disabled)
        self.assertFalse(view.next_page.disabled)
        last = main.TemplateList(42, snapshots, page=3)
        self.assertEqual(len(last.choose.options), 2)
        self.assertTrue(last.next_page.disabled)
        click = interaction()
        with patch.object(main, "accessible_templates", AsyncMock(return_value=snapshots)) as check:
            await view.turn_page(click, 1)
        check.assert_awaited_once()
        self.assertEqual(click.edit_original_response.call_args.kwargs["view"].page, 1)


class CommandAndSummaryTests(unittest.TestCase):
    def test_beginner_commands_have_plain_optional_arguments(self):
        commands = {command.name: command for command in main.bot.tree.get_commands()}
        self.assertEqual(set(commands), {"help", "scan", "copy", "templates", "preview", "delete"})
        self.assertTrue(all(not parameter.required for parameter in commands["copy"].parameters))
        self.assertEqual([p.display_name for p in commands["copy"].parameters], ["template", "server", "roles"])
        self.assertTrue(commands["delete"].parameters[0].required)

    def test_named_and_old_templates_are_both_readable(self):
        source, _ = guild(channels=[raw(1), raw(2, 4)])
        snapshot = capture(source, source.channels)
        self.assertIn(source.name, template_label(snapshot))
        snapshot["label"] = "My gaming server"
        validate_snapshot(snapshot)
        self.assertIn("My gaming server", template_label(snapshot))
        self.assertEqual(totals(snapshot), "1 category and 1 channel")
        self.assertNotIn(snapshot["id"], template_embed(snapshot).description)
        snapshot["label"] = " "
        with self.assertRaises(ValueError):
            validate_snapshot(snapshot)

    def test_large_preview_stays_inside_discord_embed_limit(self):
        source, _ = guild(channels=[raw(index + 1, name="x" * 100) for index in range(100)])
        snapshot = capture(source, source.channels)
        self.assertLess(len(layout_text(snapshot)), 4096)
        self.assertIn("attached file", layout_text(snapshot))

    def test_copy_report_distinguishes_success_skips_and_failures(self):
        self.assertEqual(report_summary("Created/tracked 4/4 entries")[0], "Copy finished")
        self.assertIn("skipped", report_summary("BLOCKED 123: Missing mapping")[0])
        self.assertIn("attention", report_summary("POSITION/VERIFY FAILED")[0])
        self.assertIn("attention", report_summary("outcome uncertain")[0])

    def test_uncertain_result_explains_next_step_without_journal_editing_jargon(self):
        message = error_text("This copy has an uncertain create request. Resolve its pending journal entry locally before resuming.")
        self.assertIn("paused to avoid duplicates", message)
        self.assertIn("person running CAPS", message)
        self.assertNotIn("journal", message)
