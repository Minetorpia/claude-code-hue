from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from claude_code_hue import cli as INDICATOR


class FakeHueClient:
    def __init__(self):
        self.light_states = {
            "1": {
                "on": True,
                "bri": 170,
                "hue": 8000,
                "sat": 120,
                "colormode": "hs",
                "effect": "none",
            },
            "2": {"on": False, "bri": 90, "colormode": "ct", "ct": 350},
        }
        self.actions = []

    def group(self, group_id):
        return {"lights": ["1", "2"]}

    def light(self, light_id):
        return {"state": dict(self.light_states[str(light_id)])}

    def lights(self):
        return {
            light_id: {"state": dict(state)}
            for light_id, state in self.light_states.items()
        }

    def group_action(self, group_id, action):
        self.actions.append(("group", str(group_id), dict(action)))
        for state in self.light_states.values():
            state.update(action)

    def light_state(self, light_id, action):
        self.actions.append(("light", str(light_id), dict(action)))
        self.light_states[str(light_id)].update(action)


class IndicatorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        os.environ["CLAUDE_CODE_HUE_DATA_DIR"] = self.temporary.name
        os.environ["CLAUDE_CONFIG_DIR"] = str(Path(self.temporary.name) / "claude-home")
        config = {
            "version": 1,
            "bridge_host": "127.0.0.1",
            "bridge_port": 443,
            "certificate_sha256": "00" * 32,
            "username": "test-user",
            "group_id": "7",
            "group_name": "Office",
            "busy": dict(INDICATOR.DEFAULT_BUSY),
            "busy_mode": "pulse",
            "complete": dict(INDICATOR.DEFAULT_COMPLETE),
            "completion_hold_seconds": 0,
            "completion_gap_seconds": 0,
            "restore_transitiontime": 0,
            "restore_request_gap_seconds": 0,
            "stale_after_seconds": 86400,
        }
        INDICATOR.write_json_atomic(INDICATOR.config_path(), config)
        self.client = FakeHueClient()
        self.original = {
            key: dict(value) for key, value in self.client.light_states.items()
        }

    def tearDown(self):
        os.environ.pop("CLAUDE_CODE_HUE_DATA_DIR", None)
        os.environ.pop("CLAUDE_CONFIG_DIR", None)
        self.temporary.cleanup()

    def test_each_completion_pulses_and_last_completion_restores_room(self):
        INDICATOR.process_event("UserPromptSubmit", "session-a", "turn-a", self.client)
        INDICATOR.process_event("UserPromptSubmit", "session-b", "turn-b", self.client)

        state = INDICATOR.load_state()
        self.assertEqual(2, len(state["active"]))
        self.assertIsNotNone(state["snapshot"])

        before_first_stop = len(self.client.actions)
        INDICATOR.process_event("Stop", "session-a", "turn-a", self.client)
        first_stop_actions = self.client.actions[before_first_stop:]
        self.assertEqual(INDICATOR.DEFAULT_COMPLETE, first_stop_actions[0][2])
        self.assertEqual("light", first_stop_actions[1][0])
        self.assertEqual(self.original["1"]["bri"], self.client.light_states["1"]["bri"])
        self.assertIsNone(INDICATOR.load_state()["snapshot"])
        self.assertEqual(1, len(INDICATOR.load_state()["active"]))

        before_second_stop = len(self.client.actions)
        INDICATOR.process_event("Stop", "session-b", "turn-b", self.client)
        second_stop_actions = self.client.actions[before_second_stop:]
        self.assertEqual(INDICATOR.DEFAULT_COMPLETE, second_stop_actions[0][2])
        self.assertTrue(any(action[0] == "light" for action in second_stop_actions[1:]))

        state = INDICATOR.load_state()
        self.assertEqual({}, state["active"])
        self.assertIsNone(state["snapshot"])
        self.assertEqual(self.original["1"]["on"], self.client.light_states["1"]["on"])
        self.assertEqual(self.original["1"]["bri"], self.client.light_states["1"]["bri"])
        self.assertEqual(self.original["2"]["on"], self.client.light_states["2"]["on"])

    def test_duplicate_stop_does_not_pulse_twice(self):
        INDICATOR.process_event("UserPromptSubmit", "session", "turn-a", self.client)
        INDICATOR.process_event("Stop", "session", "turn-a", self.client)
        action_count = len(self.client.actions)
        INDICATOR.process_event("Stop", "session", "turn-a", self.client)
        self.assertEqual(action_count, len(self.client.actions))

    def test_stop_without_observed_start_restores_room(self):
        INDICATOR.process_event("Stop", "session", "turn-a", self.client)
        self.assertEqual(self.original["1"]["on"], self.client.light_states["1"]["on"])
        self.assertEqual(self.original["1"]["bri"], self.client.light_states["1"]["bri"])
        self.assertEqual(self.original["2"]["on"], self.client.light_states["2"]["on"])

    def test_new_turn_supersedes_interrupted_turn_in_same_session(self):
        INDICATOR.process_event("UserPromptSubmit", "session", "turn-a", self.client)
        INDICATOR.process_event("UserPromptSubmit", "session", "turn-b", self.client)

        state = INDICATOR.load_state()
        self.assertEqual(
            ["session:turn-b"], sorted(state["active"].keys())
        )
        self.assertIsNotNone(state["snapshot"])

        INDICATOR.process_event("Stop", "session", "turn-b", self.client)
        self.assertEqual({}, INDICATOR.load_state()["active"])
        self.assertEqual(self.original["1"]["bri"], self.client.light_states["1"]["bri"])

    def test_session_start_recovers_interrupted_turn_and_restores_room(self):
        INDICATOR.process_event("UserPromptSubmit", "session", "turn-a", self.client)
        INDICATOR.process_event("SessionStart", "session", "", self.client)

        state = INDICATOR.load_state()
        self.assertEqual({}, state["active"])
        self.assertIsNone(state["snapshot"])
        self.assertEqual(self.original["1"]["bri"], self.client.light_states["1"]["bri"])
        self.assertEqual(self.original["2"]["on"], self.client.light_states["2"]["on"])

    def test_session_end_clears_only_that_sessions_active_turns(self):
        INDICATOR.process_event("UserPromptSubmit", "session-a", "turn-a", self.client)
        INDICATOR.process_event("UserPromptSubmit", "session-b", "turn-b", self.client)
        before_end = len(self.client.actions)

        INDICATOR.process_event("SessionEnd", "session-a", "", self.client)

        state = INDICATOR.load_state()
        self.assertEqual(["session-b:turn-b"], sorted(state["active"].keys()))
        self.assertIsNotNone(state["snapshot"])
        self.assertEqual(before_end, len(self.client.actions))

        INDICATOR.process_event("Stop", "session-b", "turn-b", self.client)
        self.assertEqual({}, INDICATOR.load_state()["active"])
        self.assertEqual(self.original["1"]["bri"], self.client.light_states["1"]["bri"])

    def test_session_end_restores_room_without_completion_pulse(self):
        INDICATOR.process_event("UserPromptSubmit", "session", "turn-a", self.client)
        before_end = len(self.client.actions)

        INDICATOR.process_event("SessionEnd", "session", "", self.client)

        end_actions = self.client.actions[before_end:]
        self.assertFalse(
            any(
                action[0] == "group" and action[2] == INDICATOR.DEFAULT_COMPLETE
                for action in end_actions
            )
        )
        self.assertTrue(any(action[0] == "light" for action in end_actions))
        state = INDICATOR.load_state()
        self.assertEqual({}, state["active"])
        self.assertIsNone(state["snapshot"])

    def test_event_queue_preserves_start_then_stop_order(self):
        INDICATOR.enqueue_event("UserPromptSubmit", "session", "turn-a")
        INDICATOR.enqueue_event("Stop", "session", "turn-a")
        queued = INDICATOR.read_json(INDICATOR.queue_path(), [])
        self.assertEqual(
            ["UserPromptSubmit", "Stop"], [item["event_name"] for item in queued]
        )

    def run_hook(self, payload):
        output = io.StringIO()
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), mock.patch.object(
            INDICATOR, "dispatch_event"
        ) as dispatch, redirect_stdout(output):
            self.assertEqual(0, INDICATOR.hook_command())
        # Claude Code adds hook stdout to the model's context; print nothing.
        self.assertEqual("", output.getvalue())
        return dispatch

    def test_hook_dispatches_prompt_id_as_turn_without_output(self):
        for event_name in ("SessionStart", "UserPromptSubmit", "Stop", "StopFailure"):
            dispatch = self.run_hook({
                "hook_event_name": event_name,
                "session_id": "session",
                "prompt_id": "turn-a",
                "prompt": "private prompt text",
            })
            dispatch.assert_called_once_with(event_name, "session", "turn-a", None)

    def test_session_end_hook_dispatches_without_output(self):
        dispatch = self.run_hook({
            "hook_event_name": "SessionEnd",
            "session_id": "session",
            "reason": "prompt_input_exit",
        })
        dispatch.assert_called_once_with("SessionEnd", "session", "", None)

    def test_subagent_and_unsupported_hooks_are_ignored(self):
        self.run_hook({"hook_event_name": "Stop", "session_id": "s", "prompt_id": "t",
                       "agent_id": "agent-1", "agent_type": "Explore"}).assert_not_called()
        self.run_hook({"hook_event_name": "PreToolUse", "session_id": "s"}).assert_not_called()

    def test_hook_queue_never_stores_prompt_or_assistant_text(self):
        payload = {"hook_event_name": "Stop", "session_id": "s", "prompt_id": "t",
                   "prompt": "secret prompt", "last_assistant_message": "secret reply"}
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), \
             mock.patch.object(INDICATOR, "spawn_drain_worker"), redirect_stdout(io.StringIO()):
            INDICATOR.hook_command()
        self.assertNotIn("secret", INDICATOR.queue_path().read_text())

    def test_stop_failure_clears_turn_without_completion_pulse(self):
        INDICATOR.process_event("UserPromptSubmit", "session-a", "turn-a", self.client)
        INDICATOR.process_event("UserPromptSubmit", "session-b", "turn-b", self.client)
        before = len(self.client.actions)
        INDICATOR.process_event("StopFailure", "session-a", "turn-a", self.client)
        self.assertEqual(["session-b:turn-b"], list(INDICATOR.load_state()["active"]))
        self.assertEqual(before, len(self.client.actions))
        # A late Stop for the failed turn must not report a completion.
        INDICATOR.process_event("Stop", "session-a", "turn-a", self.client)
        self.assertEqual(before, len(self.client.actions))

    def test_last_stop_failure_restores_room_without_green(self):
        INDICATOR.process_event("UserPromptSubmit", "session-a", "turn-a", self.client)
        INDICATOR.process_event("StopFailure", "session-a", "turn-a", self.client)
        INDICATOR.advance_busy_pulse(self.client)
        self.assertIsNone(INDICATOR.load_state()["snapshot"])
        for field in ("on", "bri", "hue", "sat"):
            self.assertEqual(self.original["1"][field], self.client.light_states["1"][field])
        self.assertNotIn(INDICATOR.DEFAULT_COMPLETE,
                         [a[2] for a in self.client.actions if a[0] == "group"])

    def test_hook_install_is_idempotent_and_preserves_other_settings(self):
        config_dir = Path(self.temporary.name) / "claude-home"
        config_dir.mkdir()
        os.environ["CLAUDE_CONFIG_DIR"] = str(config_dir)
        existing = {
            "permissions": {"allow": ["Bash(ls:*)"]},
            "model": "opus",
            "hooks": {
                "UserPromptSubmit": [
                    {"hooks": [{"type": "command", "command": "other-tool"}]}
                ],
                "PreToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [{"type": "command", "command": "safety-check"}],
                    }
                ],
            },
        }
        INDICATOR.settings_path().write_text(json.dumps(existing))
        os.chmod(INDICATOR.settings_path(), 0o644)

        with redirect_stdout(io.StringIO()):
            INDICATOR.install_hooks_command()
            INDICATOR.install_hooks_command()

        installed = INDICATOR.read_json(INDICATOR.settings_path(), {})
        self.assertEqual(["permissions", "model", "hooks"], list(installed))
        self.assertEqual(0o644, INDICATOR.settings_path().stat().st_mode & 0o777)
        prompt_handlers = [
            handler
            for group in installed["hooks"]["UserPromptSubmit"]
            for handler in group.get("hooks", [])
        ]
        self.assertEqual(
            1, sum(INDICATOR.is_our_hook_handler(item) for item in prompt_handlers)
        )
        self.assertEqual(
            "startup|resume|clear",
            installed["hooks"]["SessionStart"][0]["matcher"],
        )
        self.assertNotIn("matcher", installed["hooks"]["SessionEnd"][0])
        self.assertEqual(
            3, installed["hooks"]["SessionEnd"][0]["hooks"][0]["timeout"]
        )
        for event_name in INDICATOR.SUPPORTED_HOOK_EVENTS:
            ours = [handler for group in installed["hooks"][event_name]
                    for handler in group["hooks"] if INDICATOR.is_our_hook_handler(handler)]
            self.assertEqual(1, len(ours))
        self.assertTrue(any(item.get("command") == "other-tool" for item in prompt_handlers))
        self.assertEqual(
            "safety-check",
            installed["hooks"]["PreToolUse"][0]["hooks"][0]["command"],
        )

        with redirect_stdout(io.StringIO()):
            INDICATOR.uninstall_hooks_command()
        uninstalled = INDICATOR.read_json(INDICATOR.settings_path(), {})
        self.assertEqual(existing, uninstalled)

    def test_hook_install_follows_symlinked_settings(self):
        config_dir = Path(self.temporary.name) / "claude-home"
        dotfiles = Path(self.temporary.name) / "dotfiles"
        config_dir.mkdir()
        dotfiles.mkdir()
        os.environ["CLAUDE_CONFIG_DIR"] = str(config_dir)
        (dotfiles / "settings.json").write_text("{}")
        INDICATOR.settings_path().symlink_to(dotfiles / "settings.json")
        with redirect_stdout(io.StringIO()):
            INDICATOR.install_hooks_command()
        self.assertTrue(INDICATOR.settings_path().is_symlink())
        self.assertIn("Stop", INDICATOR.read_json(dotfiles / "settings.json", {})["hooks"])
        with redirect_stdout(io.StringIO()):
            INDICATOR.uninstall_hooks_command()
        self.assertEqual({}, INDICATOR.read_json(dotfiles / "settings.json", None))

    def transcript(self, session="session-a", turn="turn-a", interrupted=False,
                   project="-Users-example-project", name=None):
        root = INDICATOR.claude_config_dir() / "projects" / project
        root.mkdir(parents=True, exist_ok=True)
        path = root / ((name or session) + ".jsonl")
        records = [
            {"type": "user", "sessionId": session, "promptId": turn, "isSidechain": False,
             "message": {"role": "user", "content": "please work on [Request interrupted by user]"}},
            {"type": "assistant", "sessionId": session, "isSidechain": False,
             "message": {"role": "assistant", "content": [
                 {"type": "text", "text": "[Request interrupted by user]"}]}},
        ]
        path.write_text("".join(json.dumps(record) + "\n" for record in records))
        if interrupted:
            IndicatorTests.append_interrupt(self, path, session=session, turn=turn)
        return path

    def append_interrupt(self, path, session="session-a", turn="turn-a",
                         text="[Request interrupted by user]", sidechain=False, as_list=True):
        content = [{"type": "text", "text": text}] if as_list else text
        record = {"type": "user", "sessionId": session, "promptId": turn,
                  "isSidechain": sidechain, "message": {"role": "user", "content": content}}
        with path.open("a") as handle:
            handle.write(json.dumps(record) + "\n")

    def test_recovery_finds_interruption_by_session_discovery(self):
        self.transcript(interrupted=True)
        INDICATOR.process_event("UserPromptSubmit", "session-a", "turn-a", self.client)
        self.client.actions.clear()
        INDICATOR.reconcile_active_turns(INDICATOR.LifecycleReader(), self.client)
        self.assertEqual({}, INDICATOR.load_state()["active"])
        self.assertIsNone(INDICATOR.load_state()["snapshot"])
        self.assertEqual(self.original["1"]["bri"], self.client.light_states["1"]["bri"])
        self.assertFalse(any(action[0] == "group" for action in self.client.actions))

    def test_reader_recognizes_only_genuine_interruption_records(self):
        path = self.transcript()
        details = {"session_id": "session-a", "turn_id": "turn-a", "transcript_path": str(path)}
        self.assertIsNone(INDICATOR.LifecycleReader().terminal_event(details))
        self.append_interrupt(path, sidechain=True)
        self.append_interrupt(path, text="[Request interrupted by user] and more")
        self.append_interrupt(path, session="session-b")
        self.append_interrupt(path, turn="other-turn")
        self.assertIsNone(INDICATOR.LifecycleReader().terminal_event(details))
        self.append_interrupt(path, text="[Request interrupted by user for tool use]", as_list=False)
        self.assertEqual("interrupted", INDICATOR.LifecycleReader().terminal_event(details))

    def test_reader_keeps_progress_on_explicit_transcript_with_other_name(self):
        path = self.transcript(name="renamed-transcript")
        INDICATOR.process_event("UserPromptSubmit", "session-a", "turn-a", self.client,
                                transcript_path=str(path))
        reader = INDICATOR.LifecycleReader()
        INDICATOR.reconcile_active_turns(reader, self.client)
        offset = reader.files[path]["offset"]
        INDICATOR.reconcile_active_turns(reader, self.client)
        self.assertEqual(offset, reader.files[path]["offset"])
        self.append_interrupt(path)
        INDICATOR.reconcile_active_turns(reader, self.client)
        self.assertEqual({}, INDICATOR.load_state()["active"])

    def test_recovery_preserves_other_running_turn_and_saved_snapshot(self):
        self.transcript(interrupted=True)
        INDICATOR.process_event("UserPromptSubmit", "session-a", "turn-a", self.client)
        INDICATOR.process_event("UserPromptSubmit", "session-b", "turn-b", self.client)
        snapshot = INDICATOR.load_state()["snapshot"]
        before = len(self.client.actions)
        INDICATOR.reconcile_active_turns(INDICATOR.LifecycleReader(), self.client)
        self.assertEqual(["session-b:turn-b"], list(INDICATOR.load_state()["active"]))
        self.assertEqual(snapshot, INDICATOR.load_state()["snapshot"])
        self.assertEqual(before, len(self.client.actions))
        INDICATOR.process_event("Stop", "session-a", "turn-a", self.client)
        self.assertEqual(before, len(self.client.actions))

    def test_blue_pulses_once_per_ten_seconds_with_normal_lighting_between(self):
        with mock.patch.object(INDICATOR.time, "time", return_value=100) as now:
            INDICATOR.process_event("UserPromptSubmit", "session-a", "turn-a", self.client)
            self.assertEqual(INDICATOR.DEFAULT_BUSY, self.client.actions[-1][2])
            now.return_value = 100.5
            INDICATOR.advance_busy_pulse(self.client)
            self.assertEqual(1, len(self.client.actions))
            now.return_value = 101
            INDICATOR.advance_busy_pulse(self.client)
            self.assertIsNone(INDICATOR.load_state()["snapshot"])
            self.assertEqual(self.original["1"]["bri"], self.client.light_states["1"]["bri"])
            now.return_value = 109.9
            INDICATOR.advance_busy_pulse(self.client)
            self.assertEqual(3, len(self.client.actions))
            now.return_value = 110
            INDICATOR.advance_busy_pulse(self.client)
            self.assertEqual(INDICATOR.DEFAULT_BUSY, self.client.actions[-1][2])
            self.assertEqual(4, len(self.client.actions))

    def test_next_pulse_preserves_manual_lighting_changes(self):
        with mock.patch.object(INDICATOR.time, "time", return_value=100) as now:
            INDICATOR.process_event("UserPromptSubmit", "session-a", "turn-a", self.client)
            now.return_value = 101
            INDICATOR.advance_busy_pulse(self.client)
            self.client.light_states["1"].update(bri=73, hue=12000, sat=80)
            self.client.light_states["2"].update(on=True, bri=150, ct=400)
            now.return_value = 110
            INDICATOR.advance_busy_pulse(self.client)
            now.return_value = 111
            INDICATOR.advance_busy_pulse(self.client)
            self.assertEqual(73, self.client.light_states["1"]["bri"])
            self.assertEqual(12000, self.client.light_states["1"]["hue"])
            self.assertTrue(self.client.light_states["2"]["on"])
            self.assertEqual(400, self.client.light_states["2"]["ct"])

    def test_concurrent_starts_share_schedule(self):
        with mock.patch.object(INDICATOR.time, "time", return_value=100) as now:
            INDICATOR.process_event("UserPromptSubmit", "session-a", "turn-a", self.client)
            now.return_value = 101
            INDICATOR.advance_busy_pulse(self.client)
            now.return_value = 105
            INDICATOR.process_event("UserPromptSubmit", "session-b", "turn-b", self.client)
            INDICATOR.process_event("UserPromptSubmit", "session-a", "turn-new", self.client)
            self.assertEqual(110, INDICATOR.load_state()["next_busy_at"])
            self.assertEqual(1, len([a for a in self.client.actions if a[0] == "group"]))

    def test_green_preempts_blue_and_defers_next_busy_pulse(self):
        with mock.patch.object(INDICATOR.time, "time", return_value=100) as now:
            INDICATOR.process_event("UserPromptSubmit", "session-a", "turn-a", self.client)
            INDICATOR.process_event("UserPromptSubmit", "session-b", "turn-b", self.client)
            now.return_value = 100.5
            INDICATOR.process_event("Stop", "session-a", "turn-a", self.client)
            self.assertIsNone(INDICATOR.load_state()["snapshot"])
            self.assertEqual(self.original["1"]["bri"], self.client.light_states["1"]["bri"])
            count = len(self.client.actions)
            now.return_value = 110
            INDICATOR.advance_busy_pulse(self.client)
            self.assertEqual(count, len(self.client.actions))
            now.return_value = 110.5
            INDICATOR.advance_busy_pulse(self.client)
            self.assertEqual(INDICATOR.DEFAULT_BUSY, self.client.actions[-1][2])

    def test_queued_completion_has_priority_over_due_blue(self):
        with mock.patch.object(INDICATOR.time, "time", return_value=100) as now:
            INDICATOR.process_event("UserPromptSubmit", "session-a", "turn-a", self.client)
            now.return_value = 101
            INDICATOR.advance_busy_pulse(self.client)
            now.return_value = 110
            INDICATOR.enqueue_event("Stop", "session-a", "turn-a")
            count = len(self.client.actions)
            INDICATOR.advance_busy_pulse(self.client)
            self.assertEqual(count, len(self.client.actions))

    def test_last_completion_between_pulses_uses_current_lighting_and_stops_blue(self):
        with mock.patch.object(INDICATOR.time, "time", return_value=100) as now:
            INDICATOR.process_event("UserPromptSubmit", "session-a", "turn-a", self.client)
            now.return_value = 101
            INDICATOR.advance_busy_pulse(self.client)
            self.client.light_states["1"]["bri"] = 66
            now.return_value = 105
            INDICATOR.process_event("Stop", "session-a", "turn-a", self.client)
            self.assertEqual(66, self.client.light_states["1"]["bri"])
            count = len(self.client.actions)
            now.return_value = 200
            INDICATOR.advance_busy_pulse(self.client)
            self.assertEqual(count, len(self.client.actions))

    def test_legacy_solid_blue_state_is_restored_before_fresh_pulse(self):
        with mock.patch.object(INDICATOR.time, "time", return_value=100) as now:
            INDICATOR.process_event("UserPromptSubmit", "session-a", "turn-a", self.client)
            state = INDICATOR.load_state()
            state.pop("pulse")
            INDICATOR.save_state(state)
            now.return_value = 200
            INDICATOR.advance_busy_pulse(self.client)
            self.assertEqual(self.original["1"]["bri"], self.client.light_states["1"]["bri"])
            self.assertIsNone(INDICATOR.load_state()["snapshot"])
            self.assertEqual(210, INDICATOR.load_state()["next_busy_at"])

    def test_pulse_restore_failure_keeps_baseline_and_blocks_new_pulse(self):
        with mock.patch.object(INDICATOR.time, "time", return_value=100) as now:
            INDICATOR.process_event("UserPromptSubmit", "session-a", "turn-a", self.client)
            snapshot = INDICATOR.load_state()["snapshot"]
            now.return_value = 120
            with mock.patch.object(self.client, "light_state", side_effect=INDICATOR.HueError("offline")):
                with self.assertRaises(INDICATOR.HueError):
                    INDICATOR.advance_busy_pulse(self.client)
            self.assertEqual(snapshot, INDICATOR.load_state()["snapshot"])
            self.assertEqual(1, len([a for a in self.client.actions if a[0] == "group"]))
            INDICATOR.advance_busy_pulse(self.client)
            self.assertIsNone(INDICATOR.load_state()["snapshot"])

    def test_busy_pulse_timing_is_configurable(self):
        self.assertEqual((1, 10), INDICATOR.busy_timing({}))
        self.assertEqual((2, 20), INDICATOR.busy_timing({"busy_hold_seconds": 2, "busy_interval_seconds": 20}))
        self.assertEqual((2, 3), INDICATOR.busy_timing({"busy_hold_seconds": 2, "busy_interval_seconds": 1}))

    def test_worker_repeats_busy_pulses_without_further_hooks(self):
        path = self.transcript()
        INDICATOR.enqueue_event("UserPromptSubmit", "session-a", "turn-a", str(path))
        current = [100.0]
        def tick(_):
            current[0] += 0.5
            if current[0] == 112:
                self.append_interrupt(path)
            if current[0] > 120:
                self.fail("worker did not stop after interruption")
        with mock.patch.object(INDICATOR.time, "time", side_effect=lambda: current[0]), \
             mock.patch.object(INDICATOR.time, "monotonic", side_effect=lambda: current[0]), \
             mock.patch.object(INDICATOR.time, "sleep", side_effect=tick), \
             mock.patch.object(INDICATOR, "client_from_config", return_value=self.client):
            INDICATOR.drain_events()
        self.assertEqual([INDICATOR.DEFAULT_BUSY, INDICATOR.DEFAULT_BUSY],
                         [a[2] for a in self.client.actions if a[0] == "group"])
        self.assertEqual(self.original["1"]["bri"], self.client.light_states["1"]["bri"])
        self.assertEqual({}, INDICATOR.load_state()["active"])

    def test_old_quiet_turn_is_not_expired(self):
        self.transcript()
        INDICATOR.process_event("UserPromptSubmit", "session-a", "turn-a", self.client)
        state = INDICATOR.load_state()
        state["active"]["session-a:turn-a"]["started_at"] = 1
        INDICATOR.save_state(state)
        INDICATOR.reconcile_active_turns(INDICATOR.LifecycleReader(), self.client)
        INDICATOR.process_event("SessionEnd", "unrelated", "", self.client)
        self.assertIn("session-a:turn-a", INDICATOR.load_state()["active"])

    def test_wrong_turn_missing_file_and_wrong_session_do_not_clear_work(self):
        path = self.transcript(session="different", interrupted=True)
        self.transcript(turn="older-turn", interrupted=True)
        INDICATOR.process_event("UserPromptSubmit", "session-a", "turn-a", self.client,
                                transcript_path=str(path))
        INDICATOR.reconcile_active_turns(INDICATOR.LifecycleReader(), self.client)
        path.unlink()
        INDICATOR.reconcile_active_turns(INDICATOR.LifecycleReader(), self.client)
        self.assertIn("session-a:turn-a", INDICATOR.load_state()["active"])

    def test_incremental_reader_handles_partial_lines_and_file_replacement(self):
        path = self.transcript()
        details = {"session_id": "session-a", "turn_id": "turn-a", "transcript_path": str(path)}
        reader = INDICATOR.LifecycleReader()
        self.assertIsNone(reader.terminal_event(details))
        terminal = json.dumps({"type": "user", "sessionId": "session-a", "promptId": "turn-a",
                               "message": {"content": [{"type": "text",
                                                        "text": "[Request interrupted by user]"}]}})
        with path.open("a") as handle:
            handle.write(terminal[:30])
        self.assertIsNone(reader.terminal_event(details))
        with path.open("a") as handle:
            handle.write(terminal[30:] + "\n")
        self.assertEqual("interrupted", reader.terminal_event(details))
        path.unlink()
        self.transcript()
        self.assertIsNone(reader.terminal_event(details))

    def test_reader_skips_large_content_and_waits_until_caught_up(self):
        path = self.transcript()
        with path.open("a") as handle:
            handle.write("x" * 2000 + "\n")
        self.append_interrupt(path)
        reader = INDICATOR.LifecycleReader()
        reader.READ_BYTES = 128
        reader.MAX_LINE_BYTES = 256
        details = {"session_id": "session-a", "turn_id": "turn-a"}
        self.assertIsNone(reader.terminal_event(details))
        for _ in range(30):
            result = reader.terminal_event(details)
        self.assertEqual("interrupted", result)

    def test_restore_failure_retains_snapshot_and_retries(self):
        self.transcript(interrupted=True)
        INDICATOR.process_event("UserPromptSubmit", "session-a", "turn-a", self.client)
        reader = INDICATOR.LifecycleReader()
        with mock.patch.object(self.client, "light_state", side_effect=INDICATOR.HueError("offline")):
            with self.assertRaises(INDICATOR.HueError):
                INDICATOR.reconcile_active_turns(reader, self.client)
        self.assertEqual({}, INDICATOR.load_state()["active"])
        self.assertIsNotNone(INDICATOR.load_state()["snapshot"])
        INDICATOR.reconcile_active_turns(reader, self.client)
        self.assertIsNone(INDICATOR.load_state()["snapshot"])

    def test_worker_recovers_without_another_hook_and_exits_when_idle(self):
        path = self.transcript()
        INDICATOR.enqueue_event("UserPromptSubmit", "session-a", "turn-a", str(path))
        clock_value = [0]
        def tick(_):
            clock_value[0] += 3
            if clock_value[0] == 3:
                self.append_interrupt(path)
            if clock_value[0] > 30:
                self.fail("worker failed to recover and exit")
        with mock.patch.object(INDICATOR, "client_from_config", return_value=self.client), \
             mock.patch.object(INDICATOR.time, "sleep", side_effect=tick), \
             mock.patch.object(INDICATOR.time, "monotonic", side_effect=lambda: clock_value[0]):
            INDICATOR.drain_events()
        self.assertEqual({}, INDICATOR.load_state()["active"])
        self.assertIsNone(INDICATOR.load_state()["snapshot"])
        with INDICATOR.FileLock(INDICATOR.worker_lock_path(), nonblocking=True) as acquired:
            self.assertIsNotNone(acquired)

    def test_hook_passes_transcript_path_to_queue(self):
        payload = {"hook_event_name": "UserPromptSubmit", "session_id": "session-a",
                   "prompt_id": "turn-a", "transcript_path": "/example/session-a.jsonl"}
        with mock.patch.object(sys, "stdin", io.StringIO(json.dumps(payload))), \
             mock.patch.object(INDICATOR, "spawn_drain_worker"), redirect_stdout(io.StringIO()):
            INDICATOR.hook_command()
        queued = INDICATOR.read_json(INDICATOR.queue_path(), [])[0]
        self.assertEqual(payload["transcript_path"], queued["transcript_path"])
        self.assertEqual("turn-a", queued["turn_id"])

    def test_delayed_start_cannot_reactivate_completed_turn(self):
        INDICATOR.process_event("Stop", "session-a", "turn-a", self.client)
        self.client.actions.clear()
        INDICATOR.process_event("UserPromptSubmit", "session-a", "turn-a", self.client)
        self.assertEqual({}, INDICATOR.load_state()["active"])
        self.assertEqual([], self.client.actions)

    def test_worker_releases_singleton_before_queue_lock(self):
        release = INDICATOR.FileLock.release
        observed = []
        def checked_release(lock):
            if lock.path == INDICATOR.worker_lock_path() and lock.descriptor is not None:
                with INDICATOR.FileLock(INDICATOR.queue_lock_path(), nonblocking=True) as queue_lock:
                    observed.append(queue_lock is None)
            release(lock)
        with mock.patch.object(INDICATOR.FileLock, "release", checked_release):
            INDICATOR.drain_events()
        self.assertEqual([True], observed)

    def test_session_cleanup_restore_failure_can_be_retried_by_worker(self):
        INDICATOR.process_event("UserPromptSubmit", "session-a", "turn-a", self.client)
        with mock.patch.object(self.client, "light_state", side_effect=INDICATOR.HueError("offline")):
            with self.assertRaises(INDICATOR.HueError):
                INDICATOR.process_event("SessionEnd", "session-a", "", self.client)
        self.assertEqual({}, INDICATOR.load_state()["active"])
        self.assertIsNotNone(INDICATOR.load_state()["snapshot"])
        INDICATOR.reconcile_active_turns(INDICATOR.LifecycleReader(), self.client)
        self.assertIsNone(INDICATOR.load_state()["snapshot"])


class BreathingTests(unittest.TestCase):
    def setUp(self):
        IndicatorTests.setUp(self)
        config = INDICATOR.load_config()
        config["busy_mode"] = "breathe"
        INDICATOR.write_json_atomic(INDICATOR.config_path(), config)
        self.clock = mock.patch.object(INDICATOR.time, "time", return_value=100)
        self.now = self.clock.start()

    def tearDown(self):
        self.clock.stop()
        IndicatorTests.tearDown(self)

    def start(self):
        INDICATOR.process_event("UserPromptSubmit", "session-a", "turn-a", self.client)

    def step(self):
        self.now.return_value = INDICATOR.load_state()["breathing"]["next_at"]
        INDICATOR.advance_busy_pulse(self.client)

    def test_breathes_relative_to_each_lights_scene_brightness(self):
        self.client.light_states["1"]["bri"] = 80
        self.client.light_states["2"].update(on=True, bri=40)
        self.start()
        self.assertEqual(32, self.client.light_states["1"]["bri"])
        self.assertEqual(16, self.client.light_states["2"]["bri"])
        self.step()
        self.assertEqual(80, self.client.light_states["1"]["bri"])
        self.assertEqual(40, self.client.light_states["2"]["bri"])
        self.assertFalse(any(a[0] == "group" for a in self.client.actions))
        for action in self.client.actions:
            self.assertEqual({"bri", "transitiontime"}, set(action[2]))

    def test_repeated_cycles_never_ratchet_the_baseline_down(self):
        self.client.light_states["1"]["bri"] = 63
        self.start()
        for _ in range(20):
            self.step()
        self.assertEqual(63, INDICATOR.load_state()["breathing"]["lights"]["1"]["base"])
        self.assertEqual({25, 63}, {a[2]["bri"] for a in self.client.actions})

    def test_leaves_off_lights_and_colors_untouched(self):
        self.start()
        self.step()
        self.assertFalse(self.client.light_states["2"]["on"])
        self.assertFalse(any(a[1] == "2" for a in self.client.actions))
        self.assertEqual(self.original["1"]["hue"], self.client.light_states["1"]["hue"])
        self.assertEqual(self.original["1"]["sat"], self.client.light_states["1"]["sat"])

    def test_manual_scene_brightness_becomes_new_baseline(self):
        self.start()
        self.client.light_states["1"].update(bri=50, hue=2000)
        self.step()
        self.assertEqual(50, self.client.light_states["1"]["bri"])
        self.step()
        self.assertEqual(20, self.client.light_states["1"]["bri"])
        self.assertEqual(2000, self.client.light_states["1"]["hue"])
        INDICATOR.process_event("SessionEnd", "session-a", "", self.client)
        self.assertEqual(50, self.client.light_states["1"]["bri"])
        self.assertEqual(2000, self.client.light_states["1"]["hue"])

    def test_manual_off_is_respected_and_newly_on_light_is_adopted(self):
        self.start()
        self.client.actions.clear()
        self.client.light_states["1"]["on"] = False
        self.client.light_states["2"].update(on=True, bri=90)
        self.step()
        self.step()
        self.assertFalse(any(a[1] == "1" for a in self.client.actions))
        self.assertEqual(36, self.client.light_states["2"]["bri"])
        INDICATOR.process_event("SessionEnd", "session-a", "", self.client)
        self.assertFalse(self.client.light_states["1"]["on"])
        self.assertEqual(90, self.client.light_states["2"]["bri"])

    def test_green_restores_full_scene_baseline_then_resumes_breathing(self):
        self.start()
        INDICATOR.process_event("UserPromptSubmit", "session-b", "turn-b", self.client)
        INDICATOR.process_event("Stop", "session-a", "turn-a", self.client)
        self.assertEqual(170, self.client.light_states["1"]["bri"])
        self.assertFalse(self.client.light_states["2"]["on"])
        self.assertEqual(170, INDICATOR.load_state()["breathing"]["lights"]["1"]["base"])
        self.now.return_value += 0.5
        INDICATOR.advance_busy_pulse(self.client)
        self.assertEqual(68, self.client.light_states["1"]["bri"])
        self.assertEqual(170, INDICATOR.load_state()["breathing"]["lights"]["1"]["base"])
        self.assertEqual([INDICATOR.DEFAULT_COMPLETE], [a[2] for a in self.client.actions if a[0] == "group"])

    def test_last_completion_preserves_new_scene_and_stops_breathing(self):
        self.start()
        self.client.light_states["1"].update(bri=60, hue=1000)
        INDICATOR.process_event("Stop", "session-a", "turn-a", self.client)
        self.assertEqual(60, self.client.light_states["1"]["bri"])
        self.assertEqual(1000, self.client.light_states["1"]["hue"])
        count = len(self.client.actions)
        self.now.return_value = 300
        INDICATOR.advance_busy_pulse(self.client)
        self.assertEqual(count, len(self.client.actions))

    def test_concurrent_tasks_share_one_breathing_cycle(self):
        self.start()
        before = list(self.client.actions)
        INDICATOR.process_event("UserPromptSubmit", "session-b", "turn-b", self.client)
        self.assertEqual(before, self.client.actions)
        INDICATOR.process_event("SessionEnd", "session-a", "", self.client)
        self.assertIsNotNone(INDICATOR.load_state()["breathing"])
        self.step()
        self.assertEqual(170, self.client.light_states["1"]["bri"])

    def test_interruption_and_restart_restore_baseline_without_recoloring(self):
        self.start()
        self.client.actions.clear()
        INDICATOR.process_event("TurnAborted", "session-a", "turn-a", self.client)
        INDICATOR.advance_busy_pulse(self.client)
        self.assertEqual(170, self.client.light_states["1"]["bri"])
        self.assertIsNone(INDICATOR.load_state()["breathing"])
        self.assertEqual({"bri", "transitiontime"}, set(self.client.actions[0][2]))

    def test_failed_update_does_not_adopt_own_dimmed_target(self):
        self.start()
        self.now.return_value = 104
        with mock.patch.object(self.client, "light_state", side_effect=INDICATOR.HueError("offline")):
            with self.assertRaises(INDICATOR.HueError):
                INDICATOR.advance_busy_pulse(self.client)
        self.step()
        self.step()
        self.assertEqual(170, self.client.light_states["1"]["bri"])
        self.assertEqual(170, INDICATOR.load_state()["breathing"]["lights"]["1"]["base"])

    def test_failed_restore_retains_brightness_baseline(self):
        self.start()
        with mock.patch.object(self.client, "light_state", side_effect=INDICATOR.HueError("offline")):
            with self.assertRaises(INDICATOR.HueError):
                INDICATOR.process_event("SessionEnd", "session-a", "", self.client)
        self.assertIsNotNone(INDICATOR.load_state()["breathing"])
        INDICATOR.advance_busy_pulse(self.client)
        self.assertEqual(170, self.client.light_states["1"]["bri"])
        self.assertIsNone(INDICATOR.load_state()["breathing"])

    def test_migrates_saved_blue_before_capturing_brightness(self):
        config = INDICATOR.load_config()
        config["busy_mode"] = "pulse"
        INDICATOR.write_json_atomic(INDICATOR.config_path(), config)
        self.start()
        config["busy_mode"] = "breathe"
        INDICATOR.write_json_atomic(INDICATOR.config_path(), config)
        INDICATOR.advance_busy_pulse(self.client)
        self.assertEqual(170, self.client.light_states["1"]["bri"])
        self.now.return_value += 0.5
        INDICATOR.advance_busy_pulse(self.client)
        self.assertEqual(170, INDICATOR.load_state()["breathing"]["lights"]["1"]["base"])

    def test_dim_light_never_exceeds_its_baseline(self):
        self.client.light_states["1"]["bri"] = 1
        self.start()
        self.step()
        self.assertEqual({1}, {a[2]["bri"] for a in self.client.actions})

    def test_breathing_timing_validation(self):
        self.assertEqual((0.4, 2), INDICATOR.breathing_timing({}))
        self.assertEqual((0.85, 3), INDICATOR.breathing_timing({"breathing_min_ratio": 0.85, "breathing_period_seconds": 6.0}))
        config = INDICATOR.base_config("127.0.0.1", 443, "00" * 32, "test-user")
        self.assertEqual((0.4, 2), INDICATOR.breathing_timing(config))
        for config in ({"breathing_min_ratio": 1.1}, {"breathing_min_ratio": 0},
                       {"breathing_period_seconds": float("inf")}, {"breathing_period_seconds": 1}):
            with self.assertRaises(INDICATOR.HueError):
                INDICATOR.breathing_timing(config)

    def test_delayed_intermediate_brightness_never_becomes_baseline(self):
        self.client.light_states["1"]["bri"] = 254
        self.start()
        for reading in (234, 252, 227, 240, 216, 180, 165, 102, 253):
            self.client.light_states["1"]["bri"] = reading
            self.step()
            self.assertEqual(254, INDICATOR.load_state()["breathing"]["lights"]["1"]["base"])
        self.client.light_states["1"]["bri"] = 232
        INDICATOR.process_event("Stop", "session-a", "turn-a", self.client)
        self.assertEqual(254, self.client.light_states["1"]["bri"])

    def test_resume_after_green_does_not_capture_delayed_restore_brightness(self):
        self.client.light_states["1"]["bri"] = 80
        self.start()
        INDICATOR.process_event("UserPromptSubmit", "session-b", "turn-b", self.client)
        INDICATOR.process_event("Stop", "session-a", "turn-a", self.client)
        self.client.light_states["1"]["bri"] = 120  # Green -> scene restore still reported in flight.
        self.step()
        self.assertEqual(80, INDICATOR.load_state()["breathing"]["lights"]["1"]["base"])
        self.assertEqual(32, self.client.light_states["1"]["bri"])


class SolidTests(unittest.TestCase):
    def setUp(self):
        IndicatorTests.setUp(self)
        config = INDICATOR.load_config()
        config.pop("busy_mode")  # Omitted mode defaults to the original steady blue.
        INDICATOR.write_json_atomic(INDICATOR.config_path(), config)

    tearDown = IndicatorTests.tearDown

    def test_steady_blue_does_not_pulse_or_breathe(self):
        INDICATOR.process_event("UserPromptSubmit", "a", "1", self.client)
        snapshot = INDICATOR.load_state()["snapshot"]
        self.assertEqual(INDICATOR.DEFAULT_BUSY, self.client.actions[-1][2])
        for offset in (1, 10, 100):
            with mock.patch.object(INDICATOR.time, "time", return_value=2000000000 + offset):
                INDICATOR.advance_busy_pulse(self.client)
        self.assertEqual(1, len(self.client.actions))
        self.assertEqual(snapshot, INDICATOR.load_state()["snapshot"])
        self.assertIsNone(INDICATOR.load_state().get("breathing"))

    def test_each_completion_goes_green_then_blue_until_last_finishes(self):
        INDICATOR.process_event("UserPromptSubmit", "a", "1", self.client)
        INDICATOR.process_event("UserPromptSubmit", "b", "2", self.client)
        snapshot = INDICATOR.load_state()["snapshot"]
        self.client.actions.clear()
        INDICATOR.process_event("Stop", "a", "1", self.client)
        self.assertEqual([INDICATOR.DEFAULT_COMPLETE, INDICATOR.DEFAULT_BUSY],
                         [a[2] for a in self.client.actions])
        self.assertEqual(snapshot, INDICATOR.load_state()["snapshot"])
        INDICATOR.process_event("Stop", "b", "2", self.client)
        self.assertEqual(170, self.client.light_states["1"]["bri"])
        self.assertFalse(self.client.light_states["2"]["on"])
        self.assertIsNone(INDICATOR.load_state()["snapshot"])
        count = len(self.client.actions)
        INDICATOR.process_event("Stop", "b", "2", self.client)
        self.assertEqual(count, len(self.client.actions))

    def test_migrating_breathing_saves_undimmed_scene(self):
        config = INDICATOR.load_config()
        config["busy_mode"] = "breathe"
        INDICATOR.write_json_atomic(INDICATOR.config_path(), config)
        self.client.light_states["1"]["bri"] = 80
        INDICATOR.process_event("UserPromptSubmit", "a", "1", self.client)
        self.assertEqual(32, self.client.light_states["1"]["bri"])
        config["busy_mode"] = "solid"
        INDICATOR.write_json_atomic(INDICATOR.config_path(), config)
        INDICATOR.advance_busy_pulse(self.client)
        self.assertEqual(80, INDICATOR.load_state()["snapshot"]["lights"]["1"]["bri"])
        self.assertIsNone(INDICATOR.load_state()["breathing"])
        INDICATOR.process_event("Stop", "a", "1", self.client)
        self.assertEqual(80, self.client.light_states["1"]["bri"])

    def test_interruption_recovery_restores_normal_lighting(self):
        path = IndicatorTests.transcript(self, session="a", turn="1", interrupted=True)
        INDICATOR.process_event("UserPromptSubmit", "a", "1", self.client, transcript_path=str(path))
        INDICATOR.reconcile_active_turns(INDICATOR.LifecycleReader(), self.client)
        self.assertEqual({}, INDICATOR.load_state()["active"])
        self.assertIsNone(INDICATOR.load_state()["snapshot"])
        self.assertEqual(170, self.client.light_states["1"]["bri"])

    def test_failed_return_to_blue_retries_without_losing_snapshot(self):
        INDICATOR.process_event("UserPromptSubmit", "a", "1", self.client)
        INDICATOR.process_event("UserPromptSubmit", "b", "2", self.client)
        action = self.client.group_action
        def fail_blue(group, payload):
            if payload == INDICATOR.DEFAULT_BUSY:
                raise INDICATOR.HueError("offline")
            action(group, payload)
        with mock.patch.object(self.client, "group_action", side_effect=fail_blue):
            with self.assertRaises(INDICATOR.HueError):
                INDICATOR.process_event("Stop", "a", "1", self.client)
        self.assertEqual("solid_pending", INDICATOR.load_state()["pulse"])
        INDICATOR.advance_busy_pulse(self.client)
        self.assertEqual("solid", INDICATOR.load_state()["pulse"])
        self.assertEqual(170, INDICATOR.load_state()["snapshot"]["lights"]["1"]["bri"])


if __name__ == "__main__":
    unittest.main()
