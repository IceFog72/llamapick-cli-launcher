"""Portable contract tests and native Windows checks. Run with unittest."""
import ctypes as ct
import builtins
import importlib.util
import io
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import launcher_platform as services

spec = importlib.util.spec_from_file_location("launcher", Path(__file__).with_name("llama-launch.py"))
launcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launcher)


class WindowsContracts(unittest.TestCase):
    def test_launcher_windows_import_does_not_require_unix_modules(self):
        real_import = builtins.__import__
        fake_os = SimpleNamespace(**{**vars(os), "name": "nt"})
        def portable_import(name, globals=None, locals=None, fromlist=(), level=0):
            filename = (globals or {}).get("__file__", "")
            if filename.endswith(("llama-launch.py", "launcher_platform.py")):
                if name in {"curses", "termios", "tty"}:
                    raise ImportError("Unix-only module")
                if name == "os":
                    return fake_os
            return real_import(name, globals, locals, fromlist, level)
        spec = importlib.util.spec_from_file_location("windows_import_probe", Path(__file__).with_name("llama-launch.py"))
        module = importlib.util.module_from_spec(spec)
        with patch("builtins.__import__", side_effect=portable_import):
            spec.loader.exec_module(module)
        self.assertTrue(module.platform.WINDOWS)
        self.assertFalse(hasattr(module, "termios"))

    def test_windows_executable_lookup_uses_configured_environment_path(self):
        with patch.object(services, "WINDOWS", True), patch.object(services.shutil, "which", return_value="C:/tools/ninja.exe") as lookup:
            self.assertEqual(services.command_argv(["ninja", "--version"], {"PATH": "C:/tools"}),
                             ["C:/tools/ninja.exe", "--version"])
        lookup.assert_called_once_with("ninja", path="C:/tools")

    def test_windows_quotes_backslashes_empty_arguments_and_env(self):
        arguments = [r"C:\Program Files\llama\llama-server.exe", "-m", r"D:\GGUF\O'Brien\model name.gguf",
                     "--samplers", "penalties;dry;temperature", "", 'a"b', "C:\\path with spaces\\"]
        self.assertEqual(services.split_command(subprocess.list2cmdline(arguments), windows=True), arguments)
        with patch.object(launcher.platform, "WINDOWS", True):
            self.assertEqual(launcher.flags(r'-b $((4 * 512)) "-DCMAKE_CUDA_COMPILER=C:\CUDA Kit\bin\nvcc.exe"'),
                             ["-b", "2048", r"-DCMAKE_CUDA_COMPILER=C:\CUDA Kit\bin\nvcc.exe"])
            self.assertEqual(launcher.parse_environment(r'CACHE="D:\cache folder" FLAG=1', "model"),
                             {"CACHE": r"D:\cache folder", "FLAG": "1"})
        with self.assertRaises(ValueError):
            services.split_command('"unfinished', windows=True)

    def test_native_structures_have_windows_layout(self):
        for structure, size in [(services.Coord, 4), (services.ScreenInfo, 22),
                                (services.KeyEvent, 16), (services.MouseEvent, 16),
                                (services.InputRecord, 20), (services.MemoryStatus, 64)]:
            self.assertEqual(ct.sizeof(structure), size)
        if ct.sizeof(ct.c_void_p) == 8:
            self.assertEqual(ct.sizeof(services.ProcessEntry), 568)
            self.assertEqual(ct.sizeof(services.ExtendedJobLimits), 144)

    def console(self):
        with patch.object(services, "kernel", return_value=MagicMock()):
            return services.WindowsConsole(mouse=True)

    def test_windows_key_repeats_arrows_escape_and_ctrl_c(self):
        console = self.console()
        event = services.InputRecord()
        event.kind = 1
        event.data.key.down, event.data.key.virtual, event.data.key.repeat = 1, 0x28, 3
        self.assertEqual(console.translate(event), services.Keys.KEY_DOWN)
        self.assertEqual(console.repeats, [services.Keys.KEY_DOWN] * 2)
        event.data.key.virtual, event.data.key.char, event.data.key.repeat = 0, 27, 1
        self.assertEqual(console.translate(event), 27)
        event.data.key.char = 3
        self.assertEqual(console.translate(event), 3)
        event.data.key.down = 0
        self.assertIsNone(console.translate(event))

    def test_windows_mouse_drag_release_wheel_and_window_coordinates(self):
        console = self.console()
        event = services.InputRecord()
        event.kind = 2
        event.data.mouse.position = services.Coord(80, 15)
        event.data.mouse.buttons = 1
        self.assertEqual(console.translate(event, 2, 4), ("mouse", 0, 79, 12, False))
        event.data.mouse.flags = 1
        self.assertEqual(console.translate(event, 2, 4), ("mouse", 32, 79, 12, False))
        event.data.mouse.buttons = 0
        self.assertEqual(console.translate(event, 2, 4), ("mouse", 0, 79, 12, True))
        self.assertIsNone(console.translate(event, 2, 4))
        event.data.mouse.flags, event.data.mouse.buttons = 4, 120 << 16
        self.assertEqual(console.translate(event)[1], 64)
        event.data.mouse.buttons = ((-120) & 0xffff) << 16
        self.assertEqual(console.translate(event)[1], 65)

    def test_console_modes_disable_quick_edit_and_restore(self):
        console = self.console()
        def get_mode(handle, pointer):
            ct.cast(pointer, ct.POINTER(services.DWORD)).contents.value = 0x47
            return True
        console.api.GetConsoleMode.side_effect = get_mode
        console.enter()
        input_mode = console.api.SetConsoleMode.call_args_list[0].args[1]
        self.assertEqual(input_mode & (0x01 | 0x02 | 0x04 | 0x40 | 0x200), 0)
        self.assertTrue(input_mode & 0x10)
        console.restore()
        self.assertEqual([call.args[1] for call in console.api.SetConsoleMode.call_args_list[-2:]], [0x47, 0x47])

    def test_windows_start_creates_group_and_assigns_job(self):
        process = MagicMock()
        with patch.object(services, "WINDOWS", True), \
             patch.object(services, "command_argv", side_effect=lambda c, e: c), \
             patch.object(services.subprocess, "Popen", return_value=process) as spawn, \
             patch.object(services, "WindowsJob") as job:
            child, owner = services.start_process(["server.exe"], "C:/LLM", {})
            self.assertIs(child, process)
            self.assertIs(owner, job.return_value)
            self.assertEqual(spawn.call_args.kwargs["creationflags"], 0x200)
            self.assertNotIn("start_new_session", spawn.call_args.kwargs)
            job.assert_called_once_with(process)
            job.side_effect = OSError("assignment failed")
            with self.assertRaises(OSError):
                services.start_process(["server.exe"], "C:/LLM", {})
            process.kill.assert_called_once()
            process.wait.assert_called_once_with(timeout=2)

    def test_windows_stop_uses_ctrl_break_and_force_terminates_job(self):
        process, job = MagicMock(), MagicMock()
        process.poll.return_value = None
        with patch.object(services, "WINDOWS", True), \
             patch.object(services.signal, "CTRL_BREAK_EVENT", 1, create=True):
            services.signal_process(process, signal.SIGTERM, job)
            process.send_signal.assert_called_once_with(1)
            process.terminate.assert_not_called()
            services.signal_process(process, services.FORCE_KILL, job)
            job.kill.assert_called_once()
            process.send_signal.side_effect = OSError("no console")
            services.signal_process(process, signal.SIGTERM, job)
            process.terminate.assert_called_once()

    def test_windows_job_limit_handle_cleanup_and_assignment_failure(self):
        api = MagicMock()
        api.CreateJobObjectW.return_value = 123
        process = MagicMock(_handle=456)
        with patch.object(services, "kernel", return_value=api):
            job = services.WindowsJob(process)
        info = api.SetInformationJobObject.call_args.args
        self.assertEqual(info[1], 9)
        limits = ct.cast(info[2], ct.POINTER(services.ExtendedJobLimits)).contents
        self.assertEqual(limits.basic.flags, 0x2000)
        api.AssignProcessToJobObject.assert_called_once_with(123, 456)
        job.kill()
        api.TerminateJobObject.assert_called_once_with(123, 1)
        job.close()
        job.close()
        api.CloseHandle.assert_called_once_with(123)

    def test_windows_process_matching_memory_and_editor_routing(self):
        binary = Path("nonexistent-server.exe").resolve()
        with patch.object(launcher.platform, "WINDOWS", True), \
             patch.object(launcher.platform, "windows_processes", return_value=[(10, binary), (11, Path("other.exe"))]), \
             patch.object(launcher.platform, "windows_executable", return_value=binary), \
             patch.object(launcher.platform, "windows_memory", return_value=(100, 25)), \
             patch.object(launcher.os, "startfile", create=True) as editor, \
             patch.dict(os.environ, {}, clear=True):
            self.assertEqual(launcher.running_processes({"a": {"binary": binary}}), [(10, binary, str(binary))])
            self.assertTrue(launcher.still_running(10, binary))
            self.assertEqual(launcher.memory(), (100, 25))
            launcher.edit_config(binary)
            editor.assert_called_once_with(str(binary))

    def test_windows_build_configuration_and_command_preview(self):
        instance = {"source": Path("C:/llama"), "build_dir": Path("C:/build"), "jobs": 2}
        output = io.StringIO()
        with patch.object(launcher.platform, "WINDOWS", True), patch("sys.stdout", output):
            self.assertEqual(launcher.maintenance(instance, "build-server", dry_run=True), 0)
            preview = launcher.format_invocation([r"C:\Program Files\server.exe", "-m", r"D:\model name.gguf"], {"FLAG": "1"})
        self.assertIn("--config Release", output.getvalue())
        self.assertIn("--target llama-server", output.getvalue())
        self.assertIn('set "FLAG=1" && ', preview)
        self.assertIn('"C:\\Program Files\\server.exe"', preview)


class PortableRuntime(unittest.TestCase):
    def test_maintenance_footer_uses_stage_and_build_progress(self):
        history = launcher.LogHistory()
        session = SimpleNamespace(history=history, viewport=launcher.LogViewport(),
                                  title="llama.cpp update/build", notice="", stop_deadline=None,
                                  stage="Updating", process=SimpleNamespace(poll=lambda: None))
        def frame():
            return "\n".join(launcher.clean_text(line) for line in launcher.server_frame(session, 100, 18)[0])
        for stage in ("Updating", "Configuring", "Building server", "Building all targets"):
            session.stage = stage
            text = frame()
            self.assertIn(stage, text)
            self.assertNotIn("Loading", text)
            self.assertNotIn("prompt", text)
            self.assertNotIn("tok/s", text)
        for log, percent in (("[ 17%] Building CUDA object", 17), ("[40/100] Linking server", 40)):
            history.feed((log + "\r").encode())
            self.assertIn(f"Building all targets  {percent}%", frame())
        session.stop_deadline = 1
        self.assertIn("Stopping", frame())
        session.process = SimpleNamespace(poll=lambda: 7, returncode=7)
        self.assertIn("Exited: 7", frame())

    def test_default_config_is_llama_ini_on_both_platforms(self):
        for windows in (False, True):
            with self.subTest(windows=windows), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                # An old Windows sample must not become the active config.
                legacy = root / "llama.windows.ini"
                legacy.write_text("invalid sample\n", encoding="utf-8")
                script = root / "llama-launch.py"
                output = io.StringIO()
                with patch.object(launcher.platform, "WINDOWS", windows), \
                     patch.object(launcher, "__file__", str(script)), \
                     patch.object(sys, "argv", [str(script), "--list"]), \
                     patch("sys.stdout", output):
                    self.assertEqual(launcher.main(), 0)
                config = root / "llama.ini"
                self.assertTrue(config.is_file())
                self.assertIn("example-default", output.getvalue())
                expected = "build/bin/Release/llama-server.exe" if windows else "build/bin/llama-server"
                self.assertIn(f"binary = {expected}", config.read_text(encoding="utf-8"))
                self.assertEqual(legacy.read_text(encoding="utf-8"), "invalid sample\n")

    def test_missing_default_ini_is_created_by_real_cli_without_sample_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("llama-launch.py", "launcher_platform.py"):
                shutil.copyfile(Path(__file__).with_name(name), root / name)
            result = subprocess.run([sys.executable, str(root / "llama-launch.py"), "--list"],
                                    text=True, encoding="utf-8", capture_output=True, timeout=5,
                                    env={**os.environ, "PYTHONIOENCODING": "utf-8"})
            self.assertEqual(result.returncode, 0, result.stderr)
            config = root / "llama.ini"
            self.assertTrue(config.is_file())
            self.assertIn("Created starter config:", result.stdout)
            self.assertIn("example-default", result.stdout)
            instances, models, presets, settings = launcher.read_config(config)
            self.assertTrue(settings["filter_request_logs"])
            self.assertEqual(len(presets), 2)
            self.assertEqual(models["example"]["instance"], "regular")
            suffix = "Release/llama-server.exe" if services.WINDOWS else "bin/llama-server"
            self.assertTrue(instances["regular"]["binary"].as_posix().endswith(suffix))
            original = config.read_bytes()
            again = subprocess.run([sys.executable, str(root / "llama-launch.py"), "--list"],
                                   text=True, capture_output=True, timeout=5)
            self.assertEqual(again.returncode, 0, again.stderr)
            self.assertNotIn("Created starter config:", again.stdout)
            self.assertEqual(config.read_bytes(), original)

    def test_missing_custom_ini_creates_parents_and_windows_template(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "new settings" / "custom.ini"
            with patch.object(launcher.platform, "WINDOWS", True), patch("sys.stdout", io.StringIO()):
                instances, _, presets, settings = launcher.read_config(config)
            self.assertTrue(config.is_file())
            self.assertEqual(settings["editor"], "notepad.exe")
            self.assertTrue(instances["regular"]["binary"].as_posix().endswith("Release/llama-server.exe"))
            self.assertEqual(launcher.flags(presets["example-default"]["flags"]), ["-b", "2048", "-ub", "1024"])

    def test_existing_invalid_ini_is_preserved_and_missing_custom_cli_bootstraps(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "nested" / "custom.ini"
            result = subprocess.run([sys.executable, str(Path(__file__).with_name("llama-launch.py")),
                                     "--config", str(config), "example-default", "--dry-run"],
                                    text=True, capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("--ctx-size 32768", result.stdout)
            for content in (b"", b"not an INI file\n"):
                config.write_bytes(content)
                result = subprocess.run([sys.executable, str(Path(__file__).with_name("llama-launch.py")),
                                         "--config", str(config), "--list"],
                                        text=True, capture_output=True, timeout=5)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(config.read_bytes(), content)

    def test_pipe_worker_preserves_bytes_eof_and_does_not_block_ui(self):
        process = subprocess.Popen([sys.executable, "-u", "-c",
                                    "import sys,time;sys.stdout.buffer.write('café'.encode());sys.stdout.flush();time.sleep(.1)"],
                                   stdout=subprocess.PIPE)
        reader = services.PipeReader(process.stdout)
        self.addCleanup(process.stdout.close)
        self.addCleanup(reader.close)
        data, eof, deadline = [], False, time.monotonic() + 3
        while not eof and time.monotonic() < deadline:
            before = time.monotonic()
            for chunk in reader.available():
                if chunk:
                    data.append(chunk)
                else:
                    eof = True
            self.assertLess(time.monotonic() - before, .1)
            time.sleep(.01)
        process.wait(timeout=2)
        self.assertTrue(eof)
        self.assertEqual(b"".join(data), "café".encode())

    def test_example_config_is_valid_and_has_both_forks(self):
        config = Path(__file__).with_name("exemplar_windows.llama.ini")
        self.assertTrue(config.is_file())
        instances, models, presets, settings = launcher.read_config(config)
        self.assertEqual(set(instances), {"regular", "ik-ft"})
        self.assertEqual(len(presets), 4)
        self.assertTrue(settings["filter_request_logs"])
        self.assertTrue(all(str(item["binary"]).endswith("llama-server.exe") for item in instances.values()))


@unittest.skipUnless(services.WINDOWS, "Requires a native Windows runtime")
class NativeWindowsRuntime(unittest.TestCase):
    def test_windows_config_unicode_paths_and_real_cli_preview(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "模型 with spaces"
            root.mkdir()
            config = root / "launcher.ini"
            config.write_text('''[settings]
filter_request_logs = true
[instance:test]
folder = %LAUNCHER_TEST_ROOT%
binary = build/bin/llama-server.exe
[model:test]
instance = test
path = 模型.gguf
env = CACHE="%LAUNCHER_TEST_ROOT%\\cache folder"
flags = --ctx-size $((64 * 1024))
[preset:test]
model = test
flags = -b 2048
''', encoding="utf-8-sig")
            env = {**os.environ, "LAUNCHER_TEST_ROOT": str(root)}
            with patch.dict(os.environ, env):
                instances, models, _, _ = launcher.read_config(config)
            self.assertEqual(instances["test"]["folder"], root.resolve())
            self.assertEqual(models["test"]["env"]["CACHE"], str(root) + "\\cache folder")
            result = subprocess.run([sys.executable, str(Path(__file__).with_name("llama-launch.py")),
                                     "--config", str(config), "test", "--dry-run"],
                                    env={**env, "PYTHONIOENCODING": "utf-8"}, text=True, encoding="utf-8",
                                    capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("--ctx-size 65536", result.stdout)
            self.assertIn("模型.gguf", result.stdout)
            self.assertIn('set "CACHE=', result.stdout)

    def test_windows_physical_memory(self):
        total, available = services.windows_memory()
        self.assertGreater(total, 0)
        self.assertGreaterEqual(available, 0)
        self.assertLessEqual(available, total)

    def test_job_closes_descendants_and_native_process_detection(self):
        with tempfile.TemporaryDirectory() as directory:
            pid_file = Path(directory) / "child.pid"
            code = ("import subprocess,sys,time,pathlib; "
                    "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']); "
                    "pathlib.Path(sys.argv[1]).write_text(str(p.pid));time.sleep(60)")
            process, job = services.start_process([sys.executable, "-c", code, str(pid_file)], directory, os.environ.copy())
            try:
                deadline = time.monotonic() + 5
                while not pid_file.exists() and time.monotonic() < deadline:
                    time.sleep(.05)
                self.assertTrue(pid_file.exists())
                child = int(pid_file.read_text())
                self.assertIsNotNone(services.windows_executable(process.pid))
                self.assertIn(process.pid, [pid for pid, _ in services.windows_processes()])
                job.close()
                process.wait(timeout=5)
                deadline = time.monotonic() + 5
                while services.windows_executable(child) is not None and time.monotonic() < deadline:
                    time.sleep(.05)
                self.assertIsNone(services.windows_executable(child))
            finally:
                job.close()
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
