from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_efficiency.claude_folder import is_trusted
from agent_efficiency.cli import _digest_key_status, main
from agent_efficiency.permission_report import coverage, interpret_rule
from agent_efficiency.paths import PLUGIN_ROOT
from tests.test_doctor import isolated_environment, isolated_home, write_json

VALID_TEXT = "0123456789abcdef" * 4 + "\n"


class DoctorCase(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.home = Path(temp.name).resolve()
        self.data = self.home / "data"
        self.project = self.home / "work" / "app"
        self.project.mkdir(parents=True)
        self.other = str(self.home / "other-data")

    def doctor(
        self, *extra: str, text: bool = False, env: dict[str, str] | None = None
    ) -> tuple[int, object]:
        arguments = ["--data-dir", str(self.data), "doctor", "--cwd", str(self.project)]
        if not text:
            arguments.append("--json")
        with (
            isolated_home(self.home),
            mock.patch.dict(os.environ, env or {}),
            contextlib.redirect_stdout(io.StringIO()) as output,
        ):
            code = main([*arguments, *extra])
        value = output.getvalue()
        return code, (value if text else json.loads(value))


class DigestKeyDoctorTests(DoctorCase):
    def write_key(self, mode: int) -> Path:
        self.data.mkdir(mode=0o700)
        path = self.data / "digest.key"
        path.write_text(VALID_TEXT, encoding="ascii")
        os.chmod(path, mode)
        return path

    def test_a_missing_key_is_reported_and_not_created(self) -> None:
        code, doctor = self.doctor()
        self.assertEqual(code, 0)
        self.assertEqual(doctor["digest_key"]["state"], "missing")
        self.assertTrue(doctor["digest_key"]["ok"])
        self.assertFalse((self.data / "digest.key").exists())

    @unittest.skipIf(os.geteuid() == 0, "root can write to a read-only folder")
    def test_a_key_that_cannot_be_created_is_reported(self) -> None:
        self.data.mkdir(mode=0o500)
        self.addCleanup(os.chmod, self.data, 0o700)
        status = _digest_key_status(self.data)
        self.assertEqual(status["state"], "missing")
        self.assertTrue(status["ok"])
        self.assertIn("cannot be created", status["message"])
        self.assertIn("built-in key", status["message"])
        self.assertNotIn("the first hook event creates it", status["message"])
        self.assertFalse((self.data / "digest.key").exists())

    def test_a_valid_key_is_reported_and_never_printed(self) -> None:
        self.write_key(0o600)
        code, doctor = self.doctor()
        self.assertEqual(code, 0)
        self.assertEqual(doctor["digest_key"]["state"], "valid")
        self.assertNotIn(VALID_TEXT.strip(), json.dumps(doctor))
        _, text = self.doctor(text=True)
        self.assertIn("digest_key:", text)
        self.assertNotIn(VALID_TEXT.strip(), text)

    def test_an_invalid_key_fails_and_is_left_alone(self) -> None:
        path = self.write_key(0o644)
        code, doctor = self.doctor()
        self.assertEqual(code, 1)
        self.assertFalse(doctor["ok"])
        self.assertEqual(doctor["digest_key"]["state"], "mode")
        self.assertIn("Move it aside", doctor["digest_key"]["message"])
        self.assertNotIn(VALID_TEXT.strip(), json.dumps(doctor))
        self.assertEqual(path.read_text(encoding="ascii"), VALID_TEXT)
        self.assertEqual(path.stat().st_mode & 0o777, 0o644)


class DataFolderSettingsTests(DoctorCase):
    """Settings that send hook or status line writes to another data folder."""

    def user_settings(self, value: dict, folder: Path | None = None) -> Path:
        path = (folder or self.home / ".claude") / "settings.json"
        write_json(path, value)
        return path

    def report(
        self, *extra: str, env: dict[str, str] | None = None
    ) -> tuple[int, dict]:
        code, doctor = self.doctor(*extra, env=env)
        assert isinstance(doctor, dict)
        return code, doctor["data_dir_settings"]

    def test_no_settings_is_one_store(self) -> None:
        code, report = self.report()
        self.assertEqual(code, 0)
        self.assertTrue(report["ok"])
        self.assertEqual(report["findings"], [])
        self.assertEqual(report["data_dir"], str(self.data))

    def test_env_for_another_folder_is_a_split(self) -> None:
        path = self.user_settings({"env": {"AGENT_EFFICIENCY_DATA": self.other}})
        code, report = self.report()
        self.assertEqual(code, 1)
        self.assertFalse(report["ok"])
        self.assertEqual(
            report["findings"],
            [
                {
                    "file": str(path),
                    "setting": "env.AGENT_EFFICIENCY_DATA",
                    "folder": self.other,
                    "fix": (
                        f"change env.AGENT_EFFICIENCY_DATA in {path} to {self.data}"
                    ),
                }
            ],
        )

    def test_env_for_the_same_folder_is_one_store(self) -> None:
        self.user_settings({"env": {"AGENT_EFFICIENCY_DATA": "~/data"}})
        code, report = self.report()
        self.assertEqual(code, 0)
        self.assertEqual(report["findings"], [])

    def test_status_line_data_dir_for_another_folder_is_a_split(self) -> None:
        commands = (
            f"agent-efficiency --data-dir {self.other} statusline",
            f"/opt/tools/bin/agent-efficiency --data-dir={self.other} statusline",
            f"AGENT_EFFICIENCY_DATA={self.other} agent-efficiency statusline",
            "env AGENT_EFFICIENCY_DATA=$HOME/other agent-efficiency statusline",
        )
        for command in commands:
            with self.subTest(command=command):
                self.user_settings(
                    {"statusLine": {"type": "command", "command": command}}
                )
                code, report = self.report()
                self.assertEqual(code, 1)
                self.assertEqual(len(report["findings"]), 1)
                finding = report["findings"][0]
                self.assertTrue(finding["setting"].startswith("statusLine.command"))
                self.assertIn(finding["folder"], {self.other, str(self.home / "other")})
                self.assertIn("statusLine.command", finding["fix"])

    def test_a_status_line_for_another_tool_is_ignored(self) -> None:
        self.user_settings(
            {"statusLine": {"type": "command", "command": "other-tool --data-dir /x"}}
        )
        code, report = self.report()
        self.assertEqual(code, 0)
        self.assertEqual(report["findings"], [])

    def status_line(self, command: str) -> tuple[int, dict]:
        self.user_settings({"statusLine": {"type": "command", "command": command}})
        return self.report()

    def test_another_tool_after_a_control_operator_is_ignored(self) -> None:
        commands = [
            f"agent-efficiency statusline{operator} other-tool --data-dir /x"
            for operator in ("; ", " ; ", " && ", " || ", " | ", "|")
        ]
        for command in commands:
            with self.subTest(command=command):
                code, report = self.status_line(command)
                self.assertEqual(code, 0)
                self.assertEqual(report["findings"], [])
                self.assertEqual(report["problems"], [])

    def test_an_export_before_the_tool_applies_to_it(self) -> None:
        code, report = self.status_line(
            "export AGENT_EFFICIENCY_DATA=$HOME/data; agent-efficiency statusline"
        )
        self.assertEqual(code, 0)
        self.assertEqual(report["findings"], [])
        self.assertEqual(report["problems"], [])
        code, report = self.status_line(
            f"export AGENT_EFFICIENCY_DATA={self.other}; agent-efficiency statusline"
        )
        self.assertEqual(code, 1)
        self.assertEqual([item["folder"] for item in report["findings"]], [self.other])

    def test_a_variable_that_starts_with_home_is_not_checked(self) -> None:
        for command in (
            "AGENT_EFFICIENCY_DATA=$HOMEDIR/data agent-efficiency statusline",
            "agent-efficiency --data-dir $HOME_DATA statusline",
        ):
            with self.subTest(command=command):
                code, report = self.status_line(command)
                self.assertEqual(code, 0)
                self.assertEqual(report["findings"], [])
                self.assertIn("not checked", report["problems"][0]["status"])

    def test_braced_home_is_the_home_folder(self) -> None:
        code, report = self.status_line(
            "agent-efficiency --data-dir ${HOME}/data statusline"
        )
        self.assertEqual(code, 0)
        self.assertEqual(report["findings"], [])
        self.assertEqual(report["problems"], [])

    def test_two_profiles_that_split_the_store_are_both_named(self) -> None:
        first = self.home / "profile-one"
        second = self.home / "profile-two"
        self.user_settings(
            {"env": {"AGENT_EFFICIENCY_DATA": str(self.home / "one")}}, first
        )
        self.user_settings(
            {
                "statusLine": {
                    "command": f"agent-efficiency --data-dir {self.home}/two statusline"
                }
            },
            second,
        )
        code, report = self.report(
            "--host-config", str(first), "--host-config", str(second)
        )
        self.assertEqual(code, 1)
        self.assertEqual(
            [(item["file"], item["folder"]) for item in report["findings"]],
            [
                (str(first / "settings.json"), str(self.home / "one")),
                (str(second / "settings.json"), str(self.home / "two")),
            ],
        )

    def test_the_claude_config_dir_profile_is_checked(self) -> None:
        config = self.home / "config"
        path = self.user_settings(
            {"env": {"AGENT_EFFICIENCY_DATA": self.other}}, config
        )
        code, report = self.report()
        self.assertEqual(report["findings"], [])
        code, report = self.report(env={"CLAUDE_CONFIG_DIR": str(config)})
        self.assertEqual(code, 1)
        self.assertEqual([item["file"] for item in report["findings"]], [str(path)])

    def test_project_settings_are_checked(self) -> None:
        path = self.project / ".claude" / "settings.local.json"
        write_json(path, {"env": {"AGENT_EFFICIENCY_DATA": self.other}})
        code, report = self.report()
        self.assertEqual(code, 1)
        self.assertEqual(report["findings"][0]["file"], str(path))

    def test_settings_that_cannot_be_read_are_named_not_failed(self) -> None:
        settings = self.home / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text("{not json", encoding="utf-8")
        code, report = self.report()
        self.assertEqual(code, 0)
        self.assertEqual(
            report["problems"],
            [
                {
                    "file": str(settings),
                    "status": "could not be read: it is not valid JSON",
                }
            ],
        )

    def test_a_link_out_of_the_config_folder_is_not_followed(self) -> None:
        outside = self.home / "elsewhere.json"
        write_json(outside, {"env": {"AGENT_EFFICIENCY_DATA": self.other}})
        settings = self.home / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.symlink_to(outside)
        code, report = self.report()
        self.assertEqual(code, 0)
        self.assertEqual(report["findings"], [])
        self.assertIn("outside", report["problems"][0]["status"])

    def test_a_large_settings_file_is_not_read(self) -> None:
        settings = self.home / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text(
            json.dumps(
                {"env": {"AGENT_EFFICIENCY_DATA": self.other}, "pad": "x" * 1_100_000}
            ),
            encoding="utf-8",
        )
        code, report = self.report()
        self.assertEqual(code, 0)
        self.assertIn("larger than", report["problems"][0]["status"])

    def test_a_folder_doctor_cannot_resolve_is_named_not_guessed(self) -> None:
        self.user_settings({"env": {"AGENT_EFFICIENCY_DATA": "$DATA_ROOT/store"}})
        code, report = self.report()
        self.assertEqual(code, 0)
        self.assertEqual(report["findings"], [])
        self.assertIn("so it was not checked", report["problems"][0]["status"])

    def test_doctor_never_edits_a_settings_file(self) -> None:
        path = self.user_settings({"env": {"AGENT_EFFICIENCY_DATA": self.other}})
        before = path.read_bytes()
        self.report()
        self.assertEqual(path.read_bytes(), before)

    def test_other_hosts_are_reported_as_not_checked(self) -> None:
        _, report = self.report()
        self.assertIn("not checked", report["not_checked"]["codex"])
        self.assertIn("not checked", report["not_checked"]["cursor"])

    def test_text_output_names_the_split_and_the_fix(self) -> None:
        self.user_settings({"env": {"AGENT_EFFICIENCY_DATA": self.other}})
        code, text = self.doctor(text=True)
        self.assertEqual(code, 1)
        self.assertIn(f"sets env.AGENT_EFFICIENCY_DATA to {self.other}", text)
        self.assertIn("    fix: change env.AGENT_EFFICIENCY_DATA", text)


class HostileSettingsTests(DoctorCase):
    """Settings content that doctor must survive and report as not read."""

    def settings_text(self, text: str) -> Path:
        path = self.project / ".claude" / "settings.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_deeply_nested_settings_are_reported_as_not_read(self) -> None:
        path = self.settings_text("[" * 200_000 + "]" * 200_000)
        code, doctor = self.doctor()
        assert isinstance(doctor, dict)
        self.assertEqual(code, 0)
        self.assertIn(
            {"file": str(path), "status": "could not be read: it is not valid JSON"},
            doctor["data_dir_settings"]["problems"],
        )
        files = doctor["permission_settings"]["profiles"][0]["files"]
        self.assertIn(
            {"file": str(path), "status": "could not be read: it is not valid JSON"},
            files,
        )

    def test_a_nul_in_a_data_folder_setting_is_not_checked(self) -> None:
        values = (
            {"env": {"AGENT_EFFICIENCY_DATA": "/tmp/a\u0000b"}},
            {"statusLine": {"command": "agent-efficiency --data-dir /a\u0000b x"}},
        )
        for value in values:
            with self.subTest(value=value):
                path = self.project / ".claude" / "settings.json"
                write_json(path, value)
                code, doctor = self.doctor()
                assert isinstance(doctor, dict)
                report = doctor["data_dir_settings"]
                self.assertEqual(code, 0)
                self.assertEqual(report["findings"], [])
                self.assertEqual(len(report["problems"]), 1)
                self.assertEqual(report["problems"][0]["file"], str(path))
                self.assertIn("not checked", report["problems"][0]["status"])

    def test_a_deeply_nested_trust_file_means_trust_is_not_known(self) -> None:
        trust_file = self.home / ".claude.json"
        trust_file.write_text("[" * 200_000 + "]" * 200_000, encoding="utf-8")
        self.assertFalse(is_trusted(self.project, None, trust_file))

    @unittest.skipUnless(hasattr(os, "mkfifo"), "needs named pipes")
    def test_a_named_pipe_as_the_trust_file_does_not_stall_doctor(self) -> None:
        self.home.joinpath(".claude").mkdir()
        os.mkfifo(self.home / ".claude.json")
        environment = isolated_environment(self.home)
        environment["PYTHONPATH"] = str(PLUGIN_ROOT / "src")
        arguments = ["--data-dir", str(self.data), "doctor", "--json"]
        try:
            completed = subprocess.run(
                [sys.executable, "-m", "agent_efficiency", *arguments]
                + ["--cwd", str(self.project)],
                env=environment,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
        except subprocess.TimeoutExpired:
            self.fail("doctor did not return with a named pipe as the trust file")
        self.assertEqual(completed.stderr, "")
        doctor = json.loads(completed.stdout)
        profile = doctor["permission_settings"]["profiles"][0]
        self.assertFalse(profile["workspace_trusted"])


class PermissionRuleTests(unittest.TestCase):
    def covers(self, rule: str, action: str) -> str | None:
        reading = interpret_rule(rule)
        assert reading is not None and reading[0] != "unknown", rule
        return coverage(*reading, action)

    def test_rules_that_cover_every_form_of_an_action(self) -> None:
        cases = (
            ("Bash", "gh"),
            ("Bash(*)", "gh"),
            ("Bash(git merge:*)", "git merge"),
            ("Bash(git merge *)", "git merge"),
            ("Bash(git:*)", "git merge"),
            ("Bash(git *)", "git merge"),
            ("Bash(gh*)", "gh"),
            ("Bash(agent-efficiency:*)", "agent-efficiency"),
        )
        for rule, action in cases:
            with self.subTest(rule=rule):
                self.assertEqual(self.covers(rule, action), "all")

    def test_rules_that_cover_some_forms_of_an_action(self) -> None:
        cases = (
            ("Bash(gh pr view:*)", "gh"),
            ("Bash(gh pr create *)", "gh"),
            ("Bash(git merge)", "git merge"),
            ("Bash(agent-efficiency check unit)", "agent-efficiency"),
        )
        for rule, action in cases:
            with self.subTest(rule=rule):
                self.assertEqual(self.covers(rule, action), "some")

    def test_rules_for_other_commands_do_not_match(self) -> None:
        cases = (
            ("Bash(git status:*)", "git merge"),
            ("Bash(ghost *)", "gh"),
            ("Bash(git mergetool:*)", "git merge"),
            ("Bash(npm test)", "agent-efficiency"),
        )
        for rule, action in cases:
            with self.subTest(rule=rule):
                self.assertIsNone(self.covers(rule, action))

    def test_rules_for_other_tools_are_ignored(self) -> None:
        for rule in ("Read", "Edit(src/**)", "WebFetch(domain:example.com)"):
            with self.subTest(rule=rule):
                self.assertIsNone(interpret_rule(rule))

    def test_rules_with_other_whitespace_are_not_guessed(self) -> None:
        for rule in (
            "Bash(git\tmerge:*)",
            "Bash(git\nmerge:*)",
            "Bash(git  merge:*)",
            "Bash(gh\t*)",
            "Bash(git merge\u00a0*)",
        ):
            with self.subTest(rule=rule):
                self.assertEqual(interpret_rule(rule), ("unknown", ""))

    def test_rules_this_reader_cannot_read_are_not_guessed(self) -> None:
        for rule in ("Bash(git -C * merge *)", "Bash()", "Bash(git merge", "Bashful"):
            with self.subTest(rule=rule):
                self.assertEqual(interpret_rule(rule), ("unknown", ""))


class PermissionReportTests(DoctorCase):
    def settings(self, path: Path, **lists: list[str]) -> Path:
        write_json(path, {"permissions": lists})
        return path

    def profile(self, *extra: str) -> tuple[int, dict]:
        code, doctor = self.doctor(*extra)
        assert isinstance(doctor, dict)
        report = doctor["permission_settings"]
        self.assertTrue(report["informational"])
        return code, report["profiles"][0]

    def test_nothing_set_is_reported_as_not_set(self) -> None:
        code, profile = self.profile()
        self.assertEqual(code, 0)
        for action in ("git merge", "gh", "agent-efficiency"):
            self.assertEqual(profile["actions"][action]["decision"], "not set")

    def test_each_action_names_its_rule_and_file(self) -> None:
        user = self.settings(
            self.home / ".claude" / "settings.json", allow=["Bash(git merge:*)"]
        )
        shared = self.settings(
            self.project / ".claude" / "settings.json", ask=["Bash(gh *)"]
        )
        local = self.settings(
            self.project / ".claude" / "settings.local.json",
            deny=["Bash(agent-efficiency:*)"],
            allow=["Bash(agent-efficiency *)"],
        )
        code, profile = self.profile()
        self.assertEqual(code, 0)
        actions = profile["actions"]
        self.assertEqual(actions["git merge"]["decision"], "allow")
        self.assertEqual(
            actions["git merge"]["rules"],
            [
                {
                    "rule": "Bash(git merge:*)",
                    "list": "allow",
                    "file": str(user),
                    "covers": "all",
                }
            ],
        )
        self.assertEqual(actions["gh"]["decision"], "ask")
        self.assertEqual(actions["gh"]["rules"][0]["file"], str(shared))
        self.assertEqual(actions["agent-efficiency"]["decision"], "deny")
        self.assertEqual(
            {rule["file"] for rule in actions["agent-efficiency"]["rules"]},
            {str(local)},
        )

    def test_deny_wins_over_ask_and_ask_over_allow_across_files(self) -> None:
        self.settings(
            self.home / ".claude" / "settings.json",
            allow=["Bash"],
            ask=["Bash(gh:*)"],
        )
        self.settings(self.project / ".claude" / "settings.json", deny=["Bash(gh *)"])
        _, profile = self.profile()
        self.assertEqual(profile["actions"]["gh"]["decision"], "deny")
        self.assertEqual(profile["actions"]["git merge"]["decision"], "allow")

    def test_a_rule_for_some_forms_is_listed_but_does_not_decide(self) -> None:
        self.settings(
            self.home / ".claude" / "settings.json", allow=["Bash(gh pr view:*)"]
        )
        _, profile = self.profile()
        self.assertEqual(profile["actions"]["gh"]["decision"], "not set")
        self.assertEqual(profile["actions"]["gh"]["rules"][0]["covers"], "some")

    def test_a_rule_it_cannot_read_is_listed_as_not_interpreted(self) -> None:
        path = self.settings(
            self.home / ".claude" / "settings.json", allow=["Bash(git -C * merge *)"]
        )
        _, profile = self.profile()
        self.assertEqual(profile["actions"]["git merge"]["decision"], "not set")
        self.assertEqual(
            profile["not_interpreted"],
            [{"rule": "Bash(git -C * merge *)", "list": "allow", "file": str(path)}],
        )

    def test_a_default_mode_is_reported(self) -> None:
        path = self.home / ".claude" / "settings.json"
        write_json(path, {"permissions": {"defaultMode": "acceptEdits"}})
        _, profile = self.profile()
        self.assertEqual(
            profile["default_mode"], [{"file": str(path), "value": "acceptEdits"}]
        )

    def test_each_profile_is_reported_on_its_own(self) -> None:
        other = self.home / "profile-two"
        self.settings(other / "settings.json", deny=["Bash(git merge *)"])
        code, doctor = self.doctor("--host-config", str(other))
        assert isinstance(doctor, dict)
        profiles = doctor["permission_settings"]["profiles"]
        self.assertEqual(
            [profile["config_folder"] for profile in profiles],
            [str(self.home / ".claude"), str(other)],
        )
        self.assertEqual(
            [p["actions"]["git merge"]["decision"] for p in profiles],
            ["not set", "deny"],
        )
        self.assertEqual(code, 0)

    def test_the_report_never_changes_settings_or_doctor_ok(self) -> None:
        path = self.settings(
            self.home / ".claude" / "settings.json", deny=["Bash(agent-efficiency *)"]
        )
        before = path.read_bytes()
        code, doctor = self.doctor()
        assert isinstance(doctor, dict)
        self.assertEqual(code, 0)
        self.assertTrue(doctor["ok"])
        self.assertEqual(path.read_bytes(), before)
        self.assertIn(
            "not checked", doctor["permission_settings"]["not_checked"]["codex"]
        )

    def test_text_output_shows_settings_text_on_one_line(self) -> None:
        forged = (
            "default\n    agent-efficiency: deny (deny Bash(agent-efficiency:*) "
            "in settings.json)"
        )
        write_json(
            self.project / ".claude" / "settings.json",
            {
                "permissions": {
                    "allow": ["Bash(agent-efficiency:*)"],
                    "defaultMode": forged,
                }
            },
        )
        _, text = self.doctor(text=True)
        assert isinstance(text, str)
        lines = text.splitlines()
        self.assertEqual(len([line for line in lines if "defaultMode" in line]), 1)
        decisions = [
            line for line in lines if line.lstrip().startswith("agent-efficiency:")
        ]
        self.assertEqual(len(decisions), 1)
        self.assertTrue(decisions[0].lstrip().startswith("agent-efficiency: allow"))
        self.assertIn("defaultMode default\\n    agent-efficiency: deny", text)

    def test_text_output_shows_an_escape_character_escaped(self) -> None:
        self.settings(
            self.home / ".claude" / "settings.json",
            deny=["Bash(gh \u001b[1A\u001b[2K:*)"],
        )
        _, text = self.doctor(text=True)
        assert isinstance(text, str)
        self.assertNotIn("\x1b", text)
        self.assertIn("deny Bash(gh \\x1b[1A\\x1b[2K:*) in", text)

    def test_text_output_cuts_a_long_settings_value(self) -> None:
        long_value = "/" + "a" * 300_000
        write_json(
            self.home / ".claude" / "settings.json",
            {"env": {"AGENT_EFFICIENCY_DATA": long_value}},
        )
        code, doctor = self.doctor()
        assert isinstance(doctor, dict)
        self.assertEqual(
            doctor["data_dir_settings"]["findings"][0]["folder"], long_value
        )
        _, text = self.doctor(text=True)
        assert isinstance(text, str)
        self.assertFalse("a" * 201 in text, "the long value was not cut")
        self.assertIn("[cut", text)

    def test_the_report_says_which_tool_commands_it_checks(self) -> None:
        self.settings(
            self.home / ".claude" / "settings.json", allow=["Bash(python3:*)"]
        )
        _, doctor = self.doctor()
        assert isinstance(doctor, dict)
        note = doctor["permission_settings"]["tool_command_check"]
        self.assertIn("bare command name agent-efficiency", note)
        self.assertIn("not evaluated", note)
        _, text = self.doctor(text=True)
        self.assertIn(f"  note: {note}", text)

    def test_text_output_lists_each_action(self) -> None:
        self.settings(self.home / ".claude" / "settings.json", ask=["Bash(gh *)"])
        _, text = self.doctor(text=True)
        self.assertIn("permission_settings in", text)
        self.assertIn("    gh: ask (ask Bash(gh *) in", text)
        self.assertIn("    git merge: not set", text)
