"""Settings loading and the `Next:` line, both of which ended setups silently.

A non-technical reader was told to keep keys in a `.env` file that nothing
read, and was then told after every step to run a command that was never
installed. Neither produced an error anyone could act on: the first looked like
a mistyped key, the second like a broken tool.
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest import mock

from openrecruiter import config


class DotenvIsParsedForgivingly(unittest.TestCase):
    def test_plain_pairs(self):
        pairs, bad = config.parse_dotenv("A=1\nB=two\n")
        self.assertEqual(pairs, {"A": "1", "B": "two"})
        self.assertEqual(bad, ())

    def test_export_prefix_is_tolerated(self):
        """START-HERE shows `export` lines first and the `.env` note says to drop
        the word. Someone who pastes the earlier block is not wrong enough to
        deserve silence."""
        pairs, _ = config.parse_dotenv("export A=1\nEXPORT B=2\n")
        self.assertEqual(pairs, {"A": "1", "B": "2"})

    def test_quotes_blank_lines_and_comments(self):
        pairs, bad = config.parse_dotenv('\n# note\nA = "1"\nB = \'2\'\n')
        self.assertEqual(pairs, {"A": "1", "B": "2"})
        self.assertEqual(bad, ())

    def test_a_hash_inside_a_value_is_kept(self):
        """An API secret may contain '#'. Truncating a key at a character the
        reader cannot see is worse than an inline comment that does not work."""
        pairs, _ = config.parse_dotenv("SECRET=ab#cd\n")
        self.assertEqual(pairs["SECRET"], "ab#cd")

    def test_an_unreadable_line_is_reported_by_number(self):
        pairs, bad = config.parse_dotenv("A=1\nthis is not a setting\nB=2\n")
        self.assertEqual(pairs, {"A": "1", "B": "2"})
        self.assertEqual(bad, (2,))


class TheEnvironmentWinsOverTheFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, ".env")
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("KEY_A=from_file\nKEY_B=from_file\n")

    def _load(self, environ):
        with mock.patch.object(config, "candidate_paths", lambda: [self.path]):
            return config.load_dotenv(environ=environ)

    def test_a_missing_key_is_filled_from_the_file(self):
        env = {}
        res = self._load(env)
        self.assertEqual(env["KEY_A"], "from_file")
        self.assertEqual(set(res.applied), {"KEY_A", "KEY_B"})

    def test_an_exported_key_is_never_overwritten(self):
        """Someone testing a new key with `export` would otherwise be served a
        stale one out of a file they forgot about, and the symptom is an auth
        error pointing at the wrong thing."""
        env = {"KEY_A": "from_terminal"}
        res = self._load(env)
        self.assertEqual(env["KEY_A"], "from_terminal")
        self.assertEqual(res.already_set, ("KEY_A",))
        self.assertEqual(res.applied, ("KEY_B",))

    def test_no_file_is_not_an_error(self):
        with mock.patch.object(config, "candidate_paths",
                               lambda: [os.path.join(self.tmp.name, "nope")]):
            res = config.load_dotenv(environ={})
        self.assertFalse(res.found)
        self.assertEqual(res.error, "")

    def test_an_unreadable_file_is_reported_not_raised(self):
        with mock.patch.object(config, "candidate_paths", lambda: [self.path]), \
             mock.patch("builtins.open", side_effect=OSError("permission denied")):
            res = config.load_dotenv(environ={})
        self.assertTrue(res.found)
        self.assertIn("permission denied", res.error)


class TheNextLineIsCopyable(unittest.TestCase):
    """The tool printed `openrecruiter setup` in ten places. That command only
    exists if the package was pip-installed, and the documented setup has no
    install step, so every handoff was a `command not found`."""

    def _inv(self, argv0, executable="/opt/homebrew/bin/python3.12"):
        with mock.patch.object(sys, "argv", [argv0, "doctor"]), \
             mock.patch.object(sys, "executable", executable):
            return config.invocation()

    def test_module_form_names_the_interpreter_the_reader_typed(self):
        """START-HERE tells most Mac users to install and use python3.12. A
        hardcoded `python3` would be the wrong copyable line for them."""
        self.assertEqual(self._inv("/somewhere/openrecruiter/cli.py"),
                         "python3.12 -m openrecruiter.cli")

    def test_an_installed_console_script_is_named_as_itself(self):
        self.assertEqual(self._inv("/opt/homebrew/bin/openrecruiter"), "openrecruiter")

    def test_dash_c_and_empty_argv_fall_back_to_the_module_form(self):
        for argv0 in ("-c", ""):
            self.assertEqual(self._inv(argv0), "python3.12 -m openrecruiter.cli")

    def test_a_missing_executable_still_produces_something_runnable(self):
        self.assertEqual(self._inv("/x/cli.py", executable=""),
                         "python3 -m openrecruiter.cli")


class TheStateDirectoryExistsBeforeAnyoneNeedsIt(unittest.TestCase):
    def test_ensure_home_makes_the_stop_file_possible_on_a_fresh_install(self):
        """`touch ~/.openrecruiter/STOP` is offered as the emergency brake. It
        used to fail until some other command had created the directory, so the
        one command a nervous reader tests early was the one that errored."""
        with tempfile.TemporaryDirectory() as tmp:
            home = os.path.join(tmp, "state")
            with mock.patch.dict(os.environ, {"OPENRECRUITER_HOME": home}):
                self.assertFalse(os.path.isdir(home))
                config.ensure_home()
                self.assertTrue(os.path.isdir(home))
                open(os.path.join(home, "STOP"), "w").close()   # would raise before


if __name__ == "__main__":
    unittest.main(verbosity=2)
