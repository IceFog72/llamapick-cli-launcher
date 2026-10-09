"""Run with: python3 -m unittest -v test_launcher.py"""
import importlib.util
import json
import os
import select
import struct
import time
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
from unittest.mock import patch

if os.name != "nt":
    import fcntl
    import pty
    import termios

SCRIPT = Path(__file__).with_name("llama-launch.py")
spec = importlib.util.spec_from_file_location("launcher", SCRIPT)
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


@unittest.skipIf(os.name == "nt", "Unix fixture/PTY suite; native Windows checks are in test_platform.py")
class LauncherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.server = self.root / "server"
        self.server.write_text(f'#!{sys.executable}\nimport json, os, sys\nprint(json.dumps([os.getcwd(), sys.argv[1:]]))\n')
        self.server.chmod(0o755)
        (self.root / "model.gguf").write_bytes(b"model")
        self.sleeper = self.root / "other-server"
        shutil.copyfile(shutil.which("sleep"), self.sleeper)
        self.sleeper.chmod(0o755)
        self.config = self.root / "test.ini"
        self.config.write_text(f'''[instance:test]
folder = {self.root}
binary = server
[instance:other]
folder = {self.root}
binary = other-server
[model:model]
instance = test
path = model.gguf
flags = --alias "two words"
[preset:small]
model = model
flags = -b $((4 * 512)) --samplers "a;b;c"
''')

    def cli(self, *args, answer=""):
        return subprocess.run([sys.executable, str(SCRIPT), "--config", str(self.config), *args],
                              input=answer, text=True, capture_output=True, timeout=20)

    def sleeper_process(self):
        process = subprocess.Popen([str(self.sleeper), "60"])
        def cleanup():
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)
        self.addCleanup(cleanup)
        return process

    def test_launch_preserves_arguments_and_cwd(self):
        result = self.cli(answer="1\ny\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        cwd, argv = json.loads(result.stdout[result.stdout.index('["'):].strip())
        self.assertEqual(cwd, str(self.root))
        self.assertEqual(argv, ["-m", str(self.root / "model.gguf"), "--alias", "two words",
                                "-b", "2048", "--samplers", "a;b;c"])

    def test_other_fork_decline_and_dry_run(self):
        process = self.sleeper_process()
        result = self.cli("small", answer="n\n")
        self.assertEqual(result.returncode, 1)
        self.assertIn(f"PID {process.pid}", result.stdout)
        self.assertIsNone(process.poll())
        result = self.cli("small", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNone(process.poll())

    def test_other_fork_termination_and_launch(self):
        process = self.sleeper_process()
        result = self.cli("small", answer="y\ny\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(process.wait(timeout=5), -15)
        self.assertIn('"--alias", "two words"', result.stdout)

    def test_memory_warning_and_decline(self):
        for total, available, warning in [(100, 49, "above 50%"), (100, 80, "exceeds available RAM")]:
            with self.subTest(warning=warning):
                if available == 80:
                    with self.config.open("a") as stream:
                        stream.write("\n[model:large]\ninstance=test\npath=model.gguf\nram_gib=100\n")
                    content = self.config.read_text().replace("model = model", "model = large")
                    self.config.write_text(content)
                with patch.object(sys, "argv", [str(SCRIPT), "--config", str(self.config), "small"]), \
                     patch.object(launcher, "memory", return_value=(total * 2**30, available * 2**30)), \
                     patch("builtins.input", return_value="n") as prompt, \
                     patch.object(launcher.os, "execv") as execute:
                    self.assertEqual(launcher.main(), 1)
                    self.assertIn(warning, prompt.call_args.args[0])
                    execute.assert_not_called()

    def test_shell_code_is_never_evaluated(self):
        self.assertEqual(launcher.flags('--x "$(touch /tmp/should-not-exist)"'),
                         ["--x", "$(touch /tmp/should-not-exist)"])
        with self.assertRaises((ValueError, SyntaxError)):
            launcher.flags('$((__import__("os")))')

    def test_missing_model_prevents_stopping_existing_server(self):
        process = self.sleeper_process()
        (self.root / "model.gguf").unlink()
        result = self.cli("small", answer="y\n")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Model file does not exist", result.stderr)
        self.assertIsNone(process.poll())

    def terminal_menu(self, keys, resize=None):
        master, slave = pty.openpty()
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 12, 80, 0, 0))
        before = termios.tcgetattr(slave)
        process = subprocess.Popen([sys.executable, str(SCRIPT), "--config", str(self.config), "--dry-run"],
                                   stdin=slave, stdout=slave, stderr=slave,
                                   env={**os.environ, "TERM": "xterm"})
        self.addCleanup(lambda: process.kill() if process.poll() is None else None)
        output = b""
        deadline = time.monotonic() + 5
        while b"Enter launch" not in output and time.monotonic() < deadline:
            if select.select([master], [], [], 0.1)[0]:
                output += os.read(master, 65536)
        self.assertIn(b"Enter launch", output)
        if resize:
            fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", *resize, 0, 0))
            while b"Enlarge terminal" not in output and time.monotonic() < deadline:
                if select.select([master], [], [], 0.1)[0]:
                    output += os.read(master, 65536)
            self.assertIn(b"Enlarge terminal", output)
        os.write(master, keys)
        while process.poll() is None and time.monotonic() < deadline:
            if select.select([master], [], [], 0.1)[0]:
                output += os.read(master, 65536)
        self.assertEqual(process.wait(timeout=2), 0, output.decode(errors="replace"))
        while select.select([master], [], [], 0.05)[0]:
            output += os.read(master, 65536)
        self.assertEqual(termios.tcgetattr(slave), before)
        return output.decode(errors="replace")

    def test_arrow_menu_scrolls_twenty_presets_and_selects(self):
        with self.config.open("a") as stream:
            for index in range(1, 20):
                stream.write(f"\n[preset:setup-{index}]\nmodel=model\nflags=--chosen {index}\n")
        output = self.terminal_menu(b"\x1bOB" * 19 + b"\x1bOA\r")
        self.assertIn("--chosen 18", output)

    def test_arrow_menu_quit_restores_terminal(self):
        output = self.terminal_menu(b"q")
        self.assertNotIn("Command:", output)

    def test_left_returns_one_management_level_at_a_time(self):
        output = self.terminal_menu(b"m\r\x1b[D\x1b[Dq")
        self.assertIn("Manage test", output)
        self.assertEqual(output.count("Manage llama.cpp instances"), 2)
        self.assertEqual(output.count("llama.cpp presets"), 2)
        self.assertNotIn("Command:", output)

    def prepare_build_repo(self):
        def git(*args, cwd=self.root):
            return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
        remote = self.root / "remote.git"
        seed = self.root / "seed"
        source = self.root / "checkout"
        git("init", "--bare", str(remote))
        git("clone", str(remote), str(seed))
        (seed / "CMakeLists.txt").write_text('''cmake_minimum_required(VERSION 3.16)
project(LauncherProof NONE)
add_custom_target(llama-server COMMAND ${CMAKE_COMMAND} -E touch ${CMAKE_BINARY_DIR}/server-built)
add_custom_target(full-proof ALL COMMAND ${CMAKE_COMMAND} -E touch ${CMAKE_BINARY_DIR}/full-built)
''')
        git("add", ".", cwd=seed)
        git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "Initial", cwd=seed)
        git("push", "origin", "HEAD", cwd=seed)
        git("clone", str(remote), str(source))
        (seed / "upstream-change").write_text("new upstream revision")
        git("add", ".", cwd=seed)
        git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "Update", cwd=seed)
        git("push", "origin", "HEAD", cwd=seed)
        with self.config.open("a") as stream:
            stream.write(f"\n[instance:build-test]\nfolder={source}\nbuild_dir={self.root / 'build'}\njobs=2\n")
        return source, self.root / "build"

    @unittest.skipUnless(shutil.which("git") and shutil.which("cmake"), "git and cmake required")
    def test_update_server_and_full_build_with_real_git_cmake(self):
        source, build = self.prepare_build_repo()
        result = self.cli("--instance", "build-test", "--action", "update-server")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((source / "upstream-change").exists())
        self.assertTrue((build / "server-built").exists())
        self.assertFalse((build / "full-built").exists())
        result = self.cli("--instance", "build-test", "--action", "build-full")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((build / "full-built").exists())

    @unittest.skipUnless(shutil.which("git"), "git required")
    def test_dirty_update_refuses_without_building(self):
        source, build = self.prepare_build_repo()
        (source / "local-work").write_text("keep this")
        result = self.cli("--instance", "build-test", "--action", "update-full")
        self.assertEqual(result.returncode, 1)
        self.assertIn("local changes", result.stderr)
        self.assertFalse((source / "upstream-change").exists())
        self.assertFalse(build.exists())
        self.assertEqual((source / "local-work").read_text(), "keep this")

    def test_maintenance_menu_and_dry_run(self):
        result = self.cli(answer="m\n1\n5\n")
        # No source CMake project in this fixture: it should reach build and report failure.
        self.assertEqual(result.returncode, 1)
        self.assertIn("Running: cmake", result.stdout)
        result = self.cli("--manage", "--dry-run", answer="1\n3\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("git pull --ff-only", result.stdout)
        self.assertIn("--target llama-server", result.stdout)
        self.assertFalse((self.root / "build").exists())

    def test_custom_build_commands_and_failure_stop(self):
        helper = self.root / "custom command.py"
        marker = self.root / "called"
        helper.write_text('import pathlib, sys\npathlib.Path(sys.argv[1]).write_text(sys.argv[2])\n')
        config = self.config.read_text().replace("binary = server", f'''binary = server
configure_command = "{sys.executable}" -c "import sys; sys.exit(7)"
build_server_command = "{sys.executable}" "{helper}" "{marker}" "{{source}}"''')
        self.config.write_text(config)
        result = self.cli("--instance", "test", "--action", "build-server")
        self.assertEqual(result.returncode, 1)
        self.assertFalse(marker.exists())
        self.config.write_text(config.replace('import sys; sys.exit(7)', 'pass'))
        result = self.cli("--instance", "test", "--action", "build-server")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(marker.read_text(), str(self.root))

    def test_real_maintenance_ui_shows_configure_and_build_status(self):
        helper = self.root / "build.py"
        helper.write_text('''import sys, time
if sys.argv[1] == "configure":
    print("Configuring fixture", flush=True)
    time.sleep(.3)
else:
    for line in ("[ 17%] Building CUDA object", "[40/100] Linking server"):
        print(line, flush=True)
        time.sleep(.3)
''')
        config = self.config.read_text().replace("binary = server", f'''binary = server
configure_command = "{sys.executable}" "{helper}" configure
build_server_command = "{sys.executable}" "{helper}" build''')
        self.config.write_text(config)
        for route in ("command", "U", "M"):
            with self.subTest(route=route):
                master, slave = pty.openpty()
                self.addCleanup(os.close, master)
                self.addCleanup(os.close, slave)
                fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 18, 100, 0, 0))
                before = termios.tcgetattr(slave)
                args = ["--instance", "test", "--action", "build-server"] if route == "command" else []
                process = subprocess.Popen([sys.executable, str(SCRIPT), "--config", str(self.config), *args],
                                           stdin=slave, stdout=slave, stderr=slave)
                self.addCleanup(lambda p=process: p.kill() if p.poll() is None else None)
                output, deadline = b"", time.monotonic() + 5
                if route != "command":
                    while b"Enter launch" not in output and time.monotonic() < deadline:
                        if select.select([master], [], [], .05)[0]:
                            output += os.read(master, 65536)
                    self.assertIn(b"Enter launch", output)
                    keys = b"u" if route == "U" else b"m\r"
                    os.write(master, keys + b"\x1b[B" * 4 + b"\r")
                while time.monotonic() < deadline:
                    if select.select([master], [], [], .05)[0]:
                        output += os.read(master, 65536)
                    elif process.poll() is not None:
                        break
                    if b"Maintenance completed." in output:
                        after = output.split(b"Maintenance completed.", 1)[1]
                        if b"Enter launch" in after:
                            break
                self.assertIn(b"Maintenance completed.", output)
                self.assertIn(b"Enter launch", output.split(b"Maintenance completed.", 1)[1])
                self.assertIsNone(process.poll())  # Success must keep the launcher open.
                if route == "command":
                    # Reload from the returned menu must not repeat the CLI build.
                    os.write(master, b"r")
                    reloaded, deadline = b"", time.monotonic() + 5
                    while b"Enter launch" not in reloaded and time.monotonic() < deadline:
                        if select.select([master], [], [], .05)[0]:
                            reloaded += os.read(master, 65536)
                    self.assertIn(b"Enter launch", reloaded)
                    self.assertNotIn(b"Running:", reloaded)
                    self.assertNotIn(b"Building server", reloaded)
                    self.assertIsNone(process.poll())
                os.write(master, b"q")
                self.assertEqual(process.wait(timeout=2), 0, output.decode(errors="replace"))
                text = launcher.clean_text(output.decode())
                self.assertIn("Configuring", text)
                self.assertIn("Building server  17%", text)
                self.assertIn("Building server  40%", text)
                self.assertNotIn("Loading", text)
                self.assertNotIn("prompt", text)
                self.assertNotIn("tok/s", text)
                self.assertEqual(termios.tcgetattr(slave), before)

    def test_true_black_background_in_terminal_output(self):
        output = self.terminal_menu(b"q")
        self.assertIn("\x1b[48;2;0;0;0m", output)
        self.assertNotIn("\x1b[7m", output)
        self.assertEqual(output.count("\x1b[?1049h"), 1)
        self.assertEqual(output.count("\x1b[?1049l"), 1)
        self.assertIn("╭", output)
        self.assertIn("╰", output)
        self.assertIn("R reload", output)
        self.assertNotIn("\x1b[40m", output)
        self.assertIn("\x1b[38;2;80;145;160m", output)

    def test_update_shortcut_routes_to_selected_fork(self):
        with patch.object(sys, "argv", [str(SCRIPT), "--config", str(self.config), "--dry-run"]), \
             patch.object(launcher, "select_preset", return_value="@update:other"), \
             patch.object(launcher, "manage_instances", return_value=0) as manage:
            self.assertEqual(launcher.main(), 0)
        self.assertEqual(manage.call_args.args[1], "other")
        result = self.cli("--dry-run", answer="u\n1\n")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("git pull --ff-only", result.stdout)

    def test_redraw_uses_one_screen_without_repeated_clearing(self):
        with self.config.open("a") as stream:
            stream.write("\n[preset:second]\nmodel=model\nflags=--second\n")
        output = self.terminal_menu(b"\x1b[B\x1b[Aq")
        self.assertEqual(output.count("\x1b[?1049h"), 1)
        self.assertEqual(output.count("\x1b[?1049l"), 1)
        self.assertEqual(output.count("\x1b[2J"), 1)
        self.assertGreaterEqual(output.count("\x1b[1;1H\x1b[2K"), 3)

    def test_resize_refreshes_without_a_keypress(self):
        output = self.terminal_menu(b"q", resize=(6, 25))
        self.assertIn("Enlarge terminal", output)

    def test_unchanged_selection_does_not_redraw(self):
        output = self.terminal_menu(b"\x1b[B\x1b[Aq")
        self.assertEqual(output.count("\x1b[1;1H\x1b[2K"), 1)

    def test_preset_details_include_full_invocation(self):
        config = self.config.read_text().replace(
            'flags = --alias "two words"',
            'env = GGML_CUDA_LAUNCH_BLOCKING=1 GGML_CUDA_GRAPH_OPT=1\nflags = --alias "two words"')
        self.config.write_text(config)
        instances, models, presets, _ = launcher.read_config(self.config)
        model = models["model"]
        self.assertEqual(model["env"]["GGML_CUDA_GRAPH_OPT"], "1")
        with patch.object(launcher, "choose") as choose:
            launcher.select_preset(presets, models, instances)
        details = choose.call_args.args[2]("small")
        invocation = details[-1].removeprefix("Run: ")
        self.assertIn("GGML_CUDA_GRAPH_OPT=1", invocation)
        self.assertEqual(launcher.shlex.split(invocation)[-6:],
                         ["--alias", "two words", "-b", "2048", "--samplers", "a;b;c"])

    def test_command_is_hidden_by_default_and_v_toggles_it(self):
        initial = self.terminal_menu(b"q")
        self.assertNotIn("Run:", initial)
        output = self.terminal_menu(b"v q".replace(b" ", b""))
        self.assertIn("Run:", output)

    def test_editor_uses_config_path_and_editor_precedence(self):
        path = self.root / "config with spaces.ini"
        with patch.dict(os.environ, {"VISUAL": 'my-editor --wait', "EDITOR": "nano"}), \
             patch.object(launcher.subprocess, "run") as execute:
            launcher.edit_config(path)
        execute.assert_called_once_with(["my-editor", "--wait", str(path.resolve())], check=True)
        with patch.dict(os.environ, {}, clear=True), \
             patch.object(launcher.subprocess, "run") as execute:
            launcher.edit_config(path)
        execute.assert_called_once_with(["xdg-open", str(path.resolve())], check=True)

    def test_edit_key_returns_to_menu_with_updated_config(self):
        def editor(path):
            self.assertEqual(path, self.config)
            with path.open("a") as stream:
                stream.write("\n[preset:edited]\nmodel=model\nflags=--edited yes\n")
        with patch.object(sys, "argv", [str(SCRIPT), "--config", str(self.config), "--dry-run"]), \
             patch.object(launcher, "select_preset", side_effect=[launcher.EditRequested, "edited"]), \
             patch.object(launcher, "edit_config", side_effect=editor) as edit, \
             patch("builtins.print") as output:
            self.assertEqual(launcher.main(), 0)
        edit.assert_called_once()
        self.assertTrue(any("--edited yes" in str(call) for call in output.call_args_list))

    def test_edit_after_maintenance_keeps_preset_selection(self):
        with patch.object(sys, "argv", [str(SCRIPT), "--config", str(self.config),
                                        "--instance", "test", "--action", "build-server"]), \
             patch.object(launcher, "manage_instances", return_value="@select") as manage, \
             patch.object(launcher, "select_preset", side_effect=[launcher.EditRequested, None]) as select, \
             patch.object(launcher, "edit_config") as edit:
            self.assertEqual(launcher.main(), 0)
        manage.assert_called_once()
        self.assertEqual(select.call_count, 2)
        edit.assert_called_once_with(self.config)

    def test_reload_reexecutes_launcher_with_original_arguments(self):
        arguments = [str(SCRIPT), "--config", str(self.config), "--dry-run"]
        with patch.object(sys, "argv", arguments), \
             patch.object(launcher, "select_preset", side_effect=launcher.ReloadRequested), \
             patch.object(launcher.os, "execv") as restart:
            self.assertEqual(launcher.main(), 0)
        restart.assert_called_once_with(sys.executable, [sys.executable, str(SCRIPT.resolve()), *arguments[1:]])

    def test_reload_key_from_launch_and_manage_menus(self):
        output = self.terminal_menu(b"rq")
        self.assertNotIn("Command:", output)
        self.assertEqual(output.count("\x1b[?1049h"), 2)
        self.assertEqual(output.count("\x1b[?1049l"), 2)
        arguments = [str(SCRIPT), "--config", str(self.config), "--manage", "--dry-run"]
        with patch.object(sys, "argv", arguments), \
             patch.object(launcher, "choose", side_effect=launcher.ReloadRequested), \
             patch.object(launcher.os, "execv") as restart:
            self.assertEqual(launcher.main(), 0)
        self.assertEqual(restart.call_args.args[1][-2:], ["--manage", "--dry-run"])


@unittest.skipIf(os.name == "nt", "Unix fixture/PTY suite; native Windows checks are in test_platform.py")
class WrapperTests(unittest.TestCase):
    def test_request_filter_hides_whole_entries_and_can_restore_history(self):
        history = launcher.LogHistory()
        for line in [
            'INFO [log_server_request] request | tid="10"',
            'remote_addr="127.0.0.1" method="GET" path="/slots" params={}',
            'INFO [log_server_request] request | method="POST" path="/v1/chat/completions"',
            'ERROR [decode] real failure',
            'eval time = 200 ms (20 ms per token, 50.00 tokens per second)',
        ]:
            history.add(line)
        shown = "\n".join(row[2] for row in history.rows(120))
        self.assertIn("log_server_request", shown)
        self.assertIn('remote_addr=', shown)
        history.filter_request_logs = True
        hidden = "\n".join(row[2] for row in history.rows(120))
        self.assertNotIn("log_server_request", hidden)
        self.assertNotIn('remote_addr=', hidden)
        self.assertIn("real failure", hidden)
        self.assertIn("eval time", hidden)
        self.assertEqual(history.generation_rate, 50)
        history.filter_request_logs = False
        self.assertEqual("\n".join(row[2] for row in history.rows(120)), shown)
        history.filter_request_logs = True
        history.feed(b'INFO [log_server_request] unfinished request')
        self.assertNotIn("unfinished", str(history.rows(120)))

    def test_live_speed_resets_across_tasks_and_supports_slot_formats(self):
        sampler = launcher.SpeedSampler(["server", "--port", "0"], {})
        def slot(task, decoded, prompt=None):
            result = {"id": 0, "id_task": task, "next_token": {"n_decoded": decoded}, "state": 1}
            if prompt is not None:
                result["n_prompt_tokens_processed"] = prompt
            return result
        sampler.measure(1, [slot(10, 0)])
        sampler.measure(3, [slot(10, 40)])
        self.assertEqual(sampler.live_generation, 20)
        sampler.measure(5, [slot(11, 10)])
        self.assertIsNone(sampler.live_generation)
        current = slot(11, 30)
        current["next_token"] = [current["next_token"]]
        current["is_processing"] = True
        sampler.measure(7, [current])
        self.assertEqual(sampler.live_generation, 10)
        sampler.measure(9, [slot(12, 0, 100)])
        sampler.measure(11, [slot(12, 0, 500)])
        self.assertEqual(sampler.live_prompt, 200)
        self.assertIsNone(sampler.live_generation)

    def test_live_log_formats_and_monitor_chatter_filter(self):
        history = launcher.LogHistory()
        history.add("srv slot print_timing: prompt processing, n_tokens = 100, progress = 0.4, t = 2.0 s / 50.00 tokens per second")
        self.assertEqual(history.prompt_rate, 50)
        history.add("slot print_timing: n_gen = 123, tg = 40.00 t/s, tg_3s = 35.00 t/s")
        self.assertEqual(history.generation_rate, 35)
        self.assertEqual(history.state, "Generating")
        count = len(history.lines)
        history.quiet_status_until = time.monotonic() + 5
        for line in [
            'INFO [process_single_task] slot data | tid="10"',
            'id_task=7 n_idle_slots=0 n_processing_slots=1',
            'INFO [log_server_request] request | tid="10"',
            'remote_addr="127.0.0.1" method="GET" path="/slots" params={"launcher_status":"1"}',
        ]:
            history.add(line)
        self.assertEqual(len(history.lines), count)
        history.add('INFO [log_server_request] request | tid="10"')
        history.add('remote_addr="127.0.0.1" method="POST" path="/v1/chat/completions" params={}')
        self.assertEqual(len(history.lines), count + 2)
        history.add("ERROR [decode] actual generation error")
        self.assertIn("actual generation error", history.lines[-1][1])

    def test_busy_sampling_updates_before_completion_and_stops_when_idle(self):
        requests = []
        started = time.monotonic()
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append((time.monotonic(), self.path))
                count = int((time.monotonic() - started) * 20)
                data = json.dumps([{"id": 0, "id_task": 1, "state": 1,
                                    "next_token": {"n_decoded": count}}]).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            def log_message(self, *_):
                pass
        http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.addCleanup(http.server_close)
        self.addCleanup(http.shutdown)
        worker = threading.Thread(target=http.serve_forever, daemon=True)
        worker.start()
        self.start_wrapper('''import os, sys, time
from pathlib import Path
Path(sys.argv[1]).write_text(str(os.getpid()))
print("INFO [launch_slot_with_task] slot is processing task | id_slot=0 id_task=1", flush=True)
done = False
while True:
    if not done and Path(sys.argv[1] + "-idle").exists():
        print("INFO [release_slot] slot released | id_slot=0 id_task=1", flush=True)
        print("INFO [slots_idle] all slots are idle", flush=True)
        done = True
    time.sleep(.05)
''', port=http.server_port)
        self.wait_for("tok/s (live)")
        self.assertGreaterEqual(len(requests), 2)
        self.assertGreaterEqual(requests[1][0] - requests[0][0], 1.9)
        self.assertTrue(all("launcher_status=1" in path for _, path in requests))
        self.assertNotIn(b"eval time", self.output)
        Path(str(self.pid_file) + "-idle").touch()
        self.wait_for("Idle  prompt")
        self.collect(.6)
        count = len(requests)
        self.collect(2.1)
        self.assertEqual(len(requests), count)
        os.write(self.master, b"q")
        self.assertEqual(self.process.wait(timeout=3), 0)

    def test_log_anchor_survives_new_lines_eviction_and_resize(self):
        history, view = launcher.LogHistory(limit=12), launcher.LogViewport()
        for number in range(12):
            history.add(f"line {number:02d} " + "word " * 10)
        rows = history.rows(30)
        view.move_to(15, rows, 5)
        anchor = view.anchor
        first = rows[view.position(rows, 5)][:2]
        for number in range(12, 15):
            history.add(f"line {number:02d} " + "word " * 10)
        rows = history.rows(30)
        self.assertEqual(rows[view.position(rows, 5)][:2], first)
        rows = history.rows(18)
        self.assertEqual(rows[view.position(rows, 5)][0], anchor[0])
        self.assertLessEqual(rows[view.position(rows, 5)][1], anchor[1])
        view.anchor = None
        self.assertEqual(view.position(rows, 5), len(rows) - 5)
        for number in range(30):
            history.add(f"replacement {number}")
        view.anchor = first
        self.assertEqual(view.position(history.rows(30), 5), 0)

    def test_scrollbar_thumb_drag_release_wheel_and_hover(self):
        history, view = launcher.LogHistory(), launcher.LogViewport()
        for number in range(100):
            history.add(f"line {number}")
        rows = history.rows(60)
        view.move_to(27, rows, 10)
        top = view.position(rows, 10)
        start, length = view.thumb(top, len(rows), 10)
        view.handle(("mouse", 35, 78, 5, False), rows, 10, 78, 5)
        self.assertEqual(view.position(rows, 10), top)  # Hover cannot scroll.
        view.handle(("mouse", 0, 78, 5 + start, False), rows, 10, 78, 5)
        self.assertEqual(view.position(rows, 10), top)  # Grabbing thumb has no jump.
        view.handle(("mouse", 32, 78, 5 + start + 2, False), rows, 10, 78, 5)
        self.assertGreater(view.position(rows, 10), top)
        view.handle(("mouse", 0, 78, 5, True), rows, 10, 78, 5)
        top = view.position(rows, 10)
        view.handle(("mouse", 32, 78, 5, False), rows, 10, 78, 5)
        self.assertEqual(view.position(rows, 10), top)
        view.handle(("mouse", 64, 50, 8, False), rows, 10, 78, 5)
        self.assertEqual(view.position(rows, 10), top - 3)
        view.handle(("mouse", 65, 50, 8, False), rows, 10, 78, 5)
        self.assertEqual(view.position(rows, 10), top)

    def test_utf8_partial_lines_ansi_background_and_real_timings(self):
        history = launcher.LogHistory()
        data = "\x1b[41mCUDA café 模型\x1b[0m\n".encode()
        for byte in data:
            history.feed(bytes([byte]))
        history.feed(b"prompt eval time = 100 ms / 20 tokens (5 ms per token, 123.40 tokens per second)\n")
        history.feed(b"eval time = 200 ms / 10 tokens (20 ms per token, 50.00 tokens per second)\n")
        history.feed(b"partial")
        self.assertEqual(history.lines[0][1], "CUDA café 模型")
        self.assertEqual((history.prompt_rate, history.generation_rate), (123.4, 50))
        self.assertEqual(history.rows(80)[-1][2], "partial")
        history.feed(b"", final=True)
        self.assertEqual(history.lines[-1][1], "partial")
        self.assertNotIn("\x1b[41m", str(history.rows(80)))
        self.assertNotIn("\x1b[0m", str(history.rows(80)))

    def test_server_frames_fit_small_and_large_terminals(self):
        session = SimpleNamespace(history=launcher.LogHistory(), viewport=launcher.LogViewport(),
                                  command=["server", "--host=::", "--port=9000"], environment={},
                                  title="llama-server output", notice="", stop_deadline=None,
                                  process=SimpleNamespace(poll=lambda: None))
        session.history.add("漢字" * 60 + "\x1b[31munsafe")
        for width, height in [(25, 6), (40, 10), (80, 24), (180, 50)]:
            lines, *_ = launcher.server_frame(session, width, height)
            self.assertLessEqual(len(lines), height)
            for line in lines:
                self.assertLessEqual(sum(launcher.cell_width(c) for c in launcher.clean_text(line)) + 1, width)
            if width >= 40:
                self.assertTrue(launcher.clean_text(lines[-1]).endswith("╯"))
                plain = [launcher.clean_text(line) for line in lines]
                self.assertTrue(all(sum(launcher.cell_width(c) for c in line) == width - 2
                                    for line in plain))
                self.assertFalse(any("prompt" in line or "tok/s" in line for line in plain[:3]))
                status_row = next(index for index, line in enumerate(plain) if "prompt" in line)
                self.assertGreater(status_row, len(lines) // 2)
                self.assertIn("tok/s", " ".join(plain[status_row:-1]))
        self.assertEqual(launcher.server_address(session.command), ("127.0.0.1", "9000"))
        self.assertEqual(launcher.server_address(["server", "--host", "::1"]), ("[::1]", "8080"))

    def test_scrollbar_has_large_thumb_and_border_grab_target(self):
        history = launcher.LogHistory()
        for index in range(1000):
            history.add(f"line {index}")
        rows = history.rows(170)
        for column_offset in range(4):
            with self.subTest(column_offset=column_offset):
                view = launcher.LogViewport()
                top = view.position(rows, 12)
                start, length = view.thumb(top, len(rows), 12)
                self.assertEqual(length, 2)
                view.handle(("mouse", 0, 175 + column_offset, 4 + start, False), rows, 12, 175, 4)
                self.assertEqual(view.position(rows, 12), top)
                view.handle(("mouse", 32, 170, 4, False), rows, 12, 175, 4)
                self.assertEqual(view.position(rows, 12), 0)
                view.handle(("mouse", 0, 170, 4, True), rows, 12, 175, 4)
                view.handle(("mouse", 32, 175, 15, False), rows, 12, 175, 4)
                self.assertEqual(view.position(rows, 12), 0)

    def test_terminal_resize_uses_full_width_and_border_drag(self):
        self.start_wrapper()
        self.wait_for("gen 50.0")
        pid = int(self.pid_file.read_text())
        fcntl.ioctl(self.slave, termios.TIOCSWINSZ, struct.pack("HHHH", 18, 180, 0, 0))
        self.collect(.3)
        # At 180 columns, the scrollbar starts at 175 and the border is at 178.
        os.write(self.master, b"\x1b[<0;178;14M\x1b[<32;173;4M\x1b[<0;173;4m")
        self.wait_for("PAUSED")
        self.assertEqual(int(self.pid_file.read_text()), pid)
        os.write(self.master, b"q")
        self.assertEqual(self.process.wait(timeout=3), 0)

    def start_model_launcher(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        server = self.root / "server"
        server.write_text(f'''#!{sys.executable}
import os, signal, sys, time
from pathlib import Path
root = Path(__file__).parent
model = Path(sys.argv[sys.argv.index("-m") + 1]).stem
(root / (model + ".pid")).write_text(str(os.getpid()))
def event(action):
    with (root / "events").open("a") as log:
        log.write(action + " " + model + "\\n")
def stop(*_):
    event("stop")
    sys.exit(0)
signal.signal(signal.SIGTERM, stop)
event("start")
print("READY " + model, flush=True)
while True:
    print("heartbeat " + model, flush=True)
    time.sleep(.1)
''')
        server.chmod(0o755)
        config = self.root / "test.ini"
        config.write_text(f'''[instance:test]
folder = {self.root}
binary = server
[model:first]
instance = test
path = first.gguf
flags = --port 0
[model:second]
instance = test
path = second.gguf
flags = --port 0
[model:missing]
instance = test
path = absent.gguf
[preset:first-setup]
model = first
[preset:second-setup]
model = second
[preset:invalid-setup]
model = missing
''')
        for name in ("first", "second"):
            (self.root / (name + ".gguf")).write_bytes(b"model")
        master, slave = pty.openpty()
        self.master, self.slave = master, slave
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 18, 100, 0, 0))
        self.before = termios.tcgetattr(slave)
        harness = ("import runpy,sys; m=runpy.run_path(sys.argv.pop(1)); "
                   "m['main'].__globals__['memory']=lambda:(100*2**30,90*2**30); "
                   "sys.exit(m['main']())")
        self.process = subprocess.Popen([sys.executable, "-c", harness, str(SCRIPT),
                                         "--config", str(config), "first-setup"],
                                        stdin=slave, stdout=slave, stderr=slave)
        def cleanup():
            if self.process.poll() is None:
                self.process.send_signal(2)
                try:
                    self.process.wait(timeout=4)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=2)
        self.addCleanup(cleanup)
        self.output = b""
        self.wait_for("READY first")
        return int((self.root / "first.pid").read_text())

    def test_stop_returns_to_selection_and_keeps_launcher_alive(self):
        pid = self.start_model_launcher()
        os.write(self.master, b"S")
        self.wait_for("llama.cpp presets")
        self.assertIsNone(self.process.poll())
        self.assertFalse(Path(f"/proc/{pid}/exe").exists())
        self.assertEqual((self.root / "events").read_text().splitlines(), ["start first", "stop first"])
        os.write(self.master, b"\x1b[B\r")
        self.wait_for("READY second")
        second = int((self.root / "second.pid").read_text())
        self.output = b""
        os.write(self.master, b"s")
        self.wait_for("llama.cpp presets")
        self.assertFalse(Path(f"/proc/{second}/exe").exists())
        os.write(self.master, b"q")
        self.assertEqual(self.process.wait(timeout=3), 0)
        self.assertEqual(termios.tcgetattr(self.slave), self.before)

    def test_change_popup_cancel_validation_and_no_overlapping_models(self):
        pid = self.start_model_launcher()
        os.write(self.master, b"c")
        self.wait_for("Change model / preset")
        self.assertTrue(Path(f"/proc/{pid}/exe").exists())
        self.collect(.2)
        self.assertEqual(self.output.count(b"\x1b[?1049h"), 1)  # Popup shares the screen.
        os.write(self.master, b"\x1b[D")
        self.collect(.3)
        self.assertTrue(Path(f"/proc/{pid}/exe").exists())
        self.assertEqual((self.root / "events").read_text().splitlines(), ["start first"])
        # Validate before stopping the old model.
        os.write(self.master, b"c\x1b[B\x1b[B\r")
        self.wait_for("Model change cancelled:")
        self.assertTrue(Path(f"/proc/{pid}/exe").exists())
        os.write(self.master, b"r")
        self.wait_for("Launcher code reloaded; server PID unchanged")
        self.assertTrue(Path(f"/proc/{pid}/exe").exists())
        os.write(self.master, b"C\x1b[B\r")
        self.wait_for("READY second")
        second = int((self.root / "second.pid").read_text())
        self.assertFalse(Path(f"/proc/{pid}/exe").exists())
        self.assertNotEqual(pid, second)
        self.assertEqual((self.root / "events").read_text().splitlines(),
                         ["start first", "stop first", "start second"])
        os.write(self.master, b"q")
        self.assertEqual(self.process.wait(timeout=3), 0)
        self.assertFalse(Path(f"/proc/{second}/exe").exists())
        self.assertEqual(termios.tcgetattr(self.slave), self.before)

    def test_stop_after_server_exit_still_returns_to_selection(self):
        pid = self.start_model_launcher()
        os.kill(pid, 15)
        self.wait_for("Exited: 0")
        os.write(self.master, b"s")
        self.wait_for("llama.cpp presets")
        self.assertIsNone(self.process.poll())
        os.write(self.master, b"q")
        self.assertEqual(self.process.wait(timeout=3), 0)

    def test_reload_recreates_deleted_ini_without_stopping_server(self):
        pid = self.start_model_launcher()
        config = self.root / "test.ini"
        config.unlink()
        self.output = b""
        os.write(self.master, b"r")
        self.wait_for("Launcher code reloaded; server PID unchanged")
        self.assertTrue(config.is_file())
        self.assertIn("[preset:example-default]", config.read_text())
        self.assertNotIn(b"Created starter config:", self.output)
        self.assertTrue(Path(f"/proc/{pid}/exe").exists())
        os.write(self.master, b"s")
        self.wait_for("example-default")
        os.write(self.master, b"q")
        self.assertEqual(self.process.wait(timeout=3), 0)

    def test_request_filter_ini_reload_updates_display_without_stopping_model(self):
        pid = self.start_model_launcher()
        server = self.root / "server"
        source = server.read_text().replace('print("heartbeat " + model, flush=True)',
            'print("INFO [log_server_request] request | method=GET path=/status", flush=True)')
        server.write_text(source)
        # Switch once to run the modified fixture; tests never edit the real launcher/server.
        os.write(self.master, b"c\x1b[B\r")
        self.wait_for("READY second")
        pid = int((self.root / "second.pid").read_text())
        self.wait_for("log_server_request")
        config = self.root / "test.ini"
        source = config.read_text()
        config.write_text("[settings]\nfilter_request_logs = true\n" + source)
        self.output = b""
        os.write(self.master, b"r")
        self.wait_for("Launcher code reloaded; server PID unchanged")
        self.collect(.3)
        self.assertNotIn(b"log_server_request", self.output)
        self.assertTrue(Path(f"/proc/{pid}/exe").exists())
        # Invalid booleans are rejected; the current filtered view remains usable.
        config.write_text("[settings]\nfilter_request_logs = broken\n" + source)
        os.write(self.master, b"r")
        self.wait_for("Reload failed:")
        self.assertIsNone(self.process.poll())
        self.assertTrue(Path(f"/proc/{pid}/exe").exists())
        config.write_text("[settings]\nfilter_request_logs = false\n" + source)
        self.output = b""
        os.write(self.master, b"r")
        self.wait_for("log_server_request")
        self.assertTrue(Path(f"/proc/{pid}/exe").exists())
        os.write(self.master, b"q")
        self.assertEqual(self.process.wait(timeout=3), 0)

    def test_model_popup_centers_and_fits_resized_terminals(self):
        session = SimpleNamespace(history=launcher.LogHistory(), viewport=launcher.LogViewport(),
                                  title="llama-server output", notice="", stop_deadline=None,
                                  process=SimpleNamespace(poll=lambda: None))
        session.history.add("漢字 log " * 20)
        models = {"model": {"instance": "test", "path": Path("模型.gguf")}}
        presets = {f"setup-{index:02d}": {"model": "model"} for index in range(30)}
        for width, height in [(40, 10), (80, 18), (180, 40)]:
            with self.subTest(width=width, height=height):
                lines = launcher.popup_frame(session, width, height, presets, models, 25)
                self.assertEqual(len(lines), height)
                plain = [launcher.clean_text(line) for line in lines]
                self.assertTrue(all(sum(launcher.cell_width(c) for c in row) == width - 2 for row in plain))
                self.assertIn("> setup-25", "\n".join(plain))
                self.assertIn("Change model", "\n".join(plain))

    def start_wrapper(self, body=None, port=0):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.copy = self.root / "launcher.py"
        shutil.copyfile(SCRIPT, self.copy)
        shutil.copyfile(SCRIPT.with_name("launcher_platform.py"), self.root / "launcher_platform.py")
        server = self.root / "server.py"
        server.write_text(body or '''import os, sys, time
from pathlib import Path
Path(sys.argv[1]).write_text(str(os.getpid()))
for i in range(80):
    print(f"entry-{i:03d}", flush=True)
print("prompt eval time = 100 ms (5 ms per token, 123.40 tokens per second)", flush=True)
print("eval time = 200 ms (20 ms per token, 50.00 tokens per second)", flush=True)
while True:
    time.sleep(.1)
''')
        self.pid_file = self.root / "pid"
        master, slave = pty.openpty()
        self.master, self.slave = master, slave
        self.addCleanup(os.close, master)
        self.addCleanup(os.close, slave)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 18, 80, 0, 0))
        self.before = termios.tcgetattr(slave)
        harness = ("import runpy,sys,os; "
                   "m=runpy.run_path(sys.argv[1]); "
                   "sys.exit(m['run_server']([sys.executable,'-u',sys.argv[2],sys.argv[3],'--port',sys.argv[5]],sys.argv[4],os.environ.copy()))")
        self.process = subprocess.Popen([sys.executable, "-c", harness, str(self.copy),
                                         str(server), str(self.pid_file), str(self.root), str(port)],
                                        stdin=slave, stdout=slave, stderr=slave)
        def cleanup():
            if self.process.poll() is None:
                self.process.send_signal(2)
                try:
                    self.process.wait(timeout=4)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=2)
        self.addCleanup(cleanup)
        self.output = b""
        return self.wait_for("llama-server output")

    def collect(self, duration=.2):
        deadline = time.monotonic() + duration
        result = b""
        while time.monotonic() < deadline:
            if select.select([self.master], [], [], min(.05, max(0, deadline - time.monotonic())))[0]:
                result += os.read(self.master, 65536)
        self.output += result
        return result

    def wait_for(self, text):
        deadline = time.monotonic() + 5
        while text.encode() not in self.output and time.monotonic() < deadline:
            self.collect(.05)
        self.assertIn(text.encode(), self.output)
        return self.output

    def test_real_wrapper_reload_same_pid_logs_idle_redraw_and_mouse_cleanup(self):
        self.start_wrapper()
        self.wait_for("prompt 123.4")
        self.wait_for("gen 50.0")
        pid = int(self.pid_file.read_text())
        self.collect(.2)
        self.assertEqual(self.collect(.25), b"")  # Idle UI writes nothing.
        # Prove R loads changed code from disk, rather than merely clearing logs.
        source = self.copy.read_text()
        self.copy.write_text(source.replace('f"{state}  prompt', 'f"NEWCODE {state}  prompt'))
        os.write(self.master, b"r")
        self.wait_for("NEWCODE")
        self.assertEqual(int(self.pid_file.read_text()), pid)
        self.assertTrue(Path(f"/proc/{pid}/exe").exists())
        # Corrupt reload leaves the current child and usable view running.
        self.copy.write_text(source + "\n(\n")
        os.write(self.master, b"r")
        self.wait_for("Reload failed:")
        self.assertTrue(Path(f"/proc/{pid}/exe").exists())
        fcntl.ioctl(self.slave, termios.TIOCSWINSZ, struct.pack("HHHH", 10, 40, 0, 0))
        self.collect(.2)
        os.write(self.master, b"\x1b[<64;20;6M")  # Mouse wheel, then release.
        os.write(self.master, b"\x1b[<0;38;6m")
        os.write(self.master, b"q")
        deadline = time.monotonic() + 5
        while self.process.poll() is None and time.monotonic() < deadline:
            self.collect(.05)
        self.assertEqual(self.process.wait(timeout=3), 0)
        self.collect(.05)
        self.assertEqual(termios.tcgetattr(self.slave), self.before)
        self.assertFalse(Path(f"/proc/{pid}/exe").exists())
        self.assertEqual(self.output.count(b"\x1b[?1049h"), 1)
        self.assertEqual(self.output.count(b"\x1b[?1049l"), 1)
        self.assertEqual(self.output.count(b"\x1b[2J"), 1)
        self.assertNotIn(b"\x1b[3J", self.output)
        self.assertIn(b"\x1b[?1002h", self.output)
        self.assertIn(b"\x1b[?1002l", self.output)
        self.assertNotIn(b"\x1b[?1003h", self.output)

    def test_child_failure_is_kept_visible_and_exit_code_preserved(self):
        self.start_wrapper('import sys\nprint("FINAL ERROR LINE", flush=True)\nsys.exit(7)\n')
        self.wait_for("Exited: 7")
        self.wait_for("FINAL ERROR LINE")
        self.assertIsNone(self.process.poll())
        os.write(self.master, b"q")
        self.assertEqual(self.process.wait(timeout=3), 7)

    def test_wrapper_interrupt_cleans_up_child_group(self):
        self.start_wrapper()
        self.wait_for("gen 50.0")
        pid = int(self.pid_file.read_text())
        self.process.send_signal(2)
        self.process.wait(timeout=4)
        self.assertFalse(Path(f"/proc/{pid}/exe").exists())
        self.assertEqual(termios.tcgetattr(self.slave), self.before)

    def test_sigterm_cleans_up_owned_server(self):
        self.start_wrapper()
        self.wait_for("gen 50.0")
        pid = int(self.pid_file.read_text())
        self.process.terminate()
        self.process.wait(timeout=4)
        self.assertFalse(Path(f"/proc/{pid}/exe").exists())
        self.assertEqual(termios.tcgetattr(self.slave), self.before)

    def test_stop_escalates_for_entire_stubborn_process_group(self):
        self.start_wrapper('''import os, signal, subprocess, sys, time
from pathlib import Path
signal.signal(signal.SIGTERM, signal.SIG_IGN)
Path(sys.argv[1]).write_text(str(os.getpid()))
child = subprocess.Popen([sys.executable, "-c",
    "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)"])
Path(sys.argv[1] + "-child").write_text(str(child.pid))
print("CHILD GROUP READY", flush=True)
while True:
    time.sleep(.1)
''')
        self.wait_for("CHILD GROUP READY")
        parent = int(self.pid_file.read_text())
        child = int(Path(str(self.pid_file) + "-child").read_text())
        os.write(self.master, b"q")
        deadline = time.monotonic() + 8
        while self.process.poll() is None and time.monotonic() < deadline:
            self.collect(.05)
        self.assertEqual(self.process.wait(timeout=2), 0)
        self.assertFalse(Path(f"/proc/{parent}/exe").exists())
        self.assertFalse(Path(f"/proc/{child}/exe").exists())


if __name__ == "__main__":
    unittest.main()
