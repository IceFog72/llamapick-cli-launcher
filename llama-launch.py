#!/usr/bin/env python3
"""Linux/Windows llama.cpp preset launcher, Python 3.9+, no dependencies."""
import argparse
import ast
import configparser
from collections import deque
import codecs
import runpy
import unicodedata
import operator
import math
import os
from pathlib import Path
import re
import shlex
import signal
import select
import subprocess
import sys
import textwrap
import time
import json
import threading
import urllib.request
import urllib.error
import importlib.util

# Load beside this script, including when executed/reloaded through runpy.
_platform_path = Path(__file__).with_name("launcher_platform.py")
_platform_spec = importlib.util.spec_from_file_location("launcher_platform", _platform_path)
platform = importlib.util.module_from_spec(_platform_spec)
_platform_spec.loader.exec_module(platform)
curses = platform.Keys
if not platform.WINDOWS:
    import termios
    import tty


OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
       ast.FloorDiv: operator.floordiv}


class ReloadRequested(Exception):
    """Leave the menu, restore the terminal, then restart the launcher."""


class EditRequested(Exception):
    """Leave the menu and open the active configuration in an editor."""


def edit_config(path, configured_editor=None):
    editor = configured_editor or os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if not editor and platform.WINDOWS:
        os.startfile(str(path.resolve()))
        return
    command = platform.split_command(editor) if editor else ["xdg-open"]
    if not command:
        raise ValueError("Editor command is empty")
    subprocess.run([*platform.command_argv(command), str(path.resolve())], check=True)


def arithmetic(expression):
    def evaluate(node):
        if isinstance(node, ast.Constant) and type(node.value) is int:
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in OPS:
            return OPS[type(node.op)](evaluate(node.left), evaluate(node.right))
        raise ValueError("Arithmetic supports integers and +, -, *, // only")
    if len(expression) > 100:
        raise ValueError("Arithmetic expression is too long")
    return str(evaluate(ast.parse(expression.strip(), mode="eval").body))


def flags(value):
    value = re.sub(r"\$\(\((.*?)\)\)", lambda m: arithmetic(m[1]), value)
    return platform.split_command(value)


def ensure_config(path, announce=True):
    """Create a portable starter configuration without replacing existing files."""
    if path.exists():
        return False
    binary = "build/bin/Release/llama-server.exe" if platform.WINDOWS else "build/bin/llama-server"
    editor = "editor = notepad.exe" if platform.WINDOWS else "# editor = nano"
    template = f'''# Created by llama-launch. Set your fork folder and GGUF path below.
# Press E in the menu to edit this file, then R to reload it.
# Section names are unique IDs; models refer to instances, presets to models.
[settings]
{editor}
filter_request_logs = true

[instance:regular]
# Relative folders resolve against this INI's directory.
folder = ../llama.cpp
binary = {binary}
build_dir = build
jobs = {min(12, os.cpu_count() or 1)}
# Use -DGGML_CUDA=OFF for a build without CUDA.
configure_flags = -DCMAKE_BUILD_TYPE=Release -DGGML_CUDA=ON -DGGML_NATIVE=ON

[model:example]
instance = regular
# Relative model paths resolve against the instance folder.
path = ../models/model.gguf
# Optional host RAM estimate including context/cache:
# ram_gib = 28
# Optional environment flags:
# env = GGML_CUDA_LAUNCH_BLOCKING=1 GGML_CUDA_GRAPH_OPT=1
flags = --host 127.0.0.1 --ctx-size $((32 * 1024)) -ngl 999

[preset:example-default]
model = example
flags = -b $((4 * 512)) -ub $((2 * 512))

[preset:example-large-batch]
model = example
flags = -b $((6 * 512)) -ub $((3 * 512))
'''
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(template)
    except FileExistsError:
        return False
    if announce:
        print(f"Created starter config: {path}")
        print("Set the fork folder and model path before launching. Press E in the menu to edit.")
    return True


def read_config(path, announce=True):
    ensure_config(path, announce=announce)
    config = configparser.ConfigParser(interpolation=None)
    with path.open(encoding="utf-8-sig") as stream:
        config.read_file(stream)
    instances, models, presets, settings = {}, {}, {}, {}
    def resolve(value, base):
        result = Path(os.path.expandvars(os.path.expanduser(value)))
        return (base / result).resolve()
    for section in config.sections():
        kind, _, name = section.partition(":")
        if kind == "settings" and not name:
            settings.update(dict(config[section]))
            continue
        if not name or kind not in {"instance", "model", "preset"}:
            raise ValueError(f"Unknown section: [{section}]")
        values = dict(config[section])
        {"instance": instances, "model": models, "preset": presets}[kind][name] = values
    for name, item in instances.items():
        item["folder"] = resolve(item["folder"], path.parent)
        default_binary = "build/bin/Release/llama-server.exe" if platform.WINDOWS else "build/bin/llama-server"
        item["binary"] = resolve(item.get("binary", default_binary), item["folder"])
        item["source"] = resolve(item.get("source", "."), item["folder"])
        item["build_dir"] = resolve(item.get("build_dir", "build"), item["source"])
        if int(item.get("jobs", os.cpu_count() or 1)) < 1:
            raise ValueError(f"Instance {name}: jobs must be positive")
        flags(item.get("flags", ""))
        flags(item.get("configure_flags", ""))
        for key in ("update_command", "configure_command", "build_full_command", "build_server_command"):
            if key in item and not flags(item[key]):
                raise ValueError(f"Instance {name}: {key} must not be empty")
    for name, item in models.items():
        if item["instance"] not in instances:
            raise ValueError(f"Model {name}: unknown instance {item['instance']}")
        item["path"] = resolve(item["path"], instances[item["instance"]]["folder"])
        if "ram_gib" in item:
            value = float(item["ram_gib"])
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"Model {name}: ram_gib must be positive and finite")
        flags(item.get("flags", ""))
        if "env" in item:
            item["env"] = parse_environment(item["env"], f"model {name}")
    for name, item in presets.items():
        if item["model"] not in models:
            raise ValueError(f"Preset {name}: unknown model {item['model']}")
        flags(item.get("flags", ""))
    if not presets:
        raise ValueError("Config has no presets")
    if "filter_request_logs" in settings:
        settings["filter_request_logs"] = config.getboolean("settings", "filter_request_logs")
    return instances, models, presets, settings


def parse_environment(value, owner):
    result = {}
    for token in platform.split_command(value):
        key, separator, item = token.partition("=")
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ValueError(f"{owner}: env must contain KEY=VALUE entries")
        result[key] = os.path.expandvars(item)
    return result


def running_processes(instances):
    binaries = {item["binary"] for item in instances.values()}
    found = []
    if platform.WINDOWS:
        for pid, executable in platform.windows_processes():
            try:
                if any(os.path.normcase(str(executable)) == os.path.normcase(str(binary))
                       or (binary.exists() and executable.samefile(binary)) for binary in binaries):
                    found.append((pid, executable, str(executable)))
            except OSError:
                continue
        return found
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            executable = (entry / "exe").resolve(strict=True)
            # Compare inode too, to cover symlinks/hard links to configured servers.
            if any(executable == binary or (binary.exists() and executable.samefile(binary))
                   for binary in binaries):
                command = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
                found.append((int(entry.name), executable, command))
        except (OSError, RuntimeError):
            continue
    return found


def confirm(message):
    try:
        return input(message + " [y/N] ").strip().lower() in {"y", "yes"}
    except EOFError:
        return False


def still_running(pid, executable):
    if platform.WINDOWS:
        current = platform.windows_executable(pid)
        return current is not None and os.path.normcase(str(current)) == os.path.normcase(str(executable))
    try:
        return Path(f"/proc/{pid}/exe").resolve(strict=True) == executable
    except OSError:
        return False


def stop_processes(processes):
    for pid, executable, _ in processes:
        if still_running(pid, executable):
            if platform.WINDOWS:
                platform.stop_external(pid)
            else:
                os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        remaining = [p for p in processes if still_running(p[0], p[1])]
        if not remaining:
            return True
        time.sleep(0.1)
    if not confirm("Some servers did not stop after 10 seconds. Force kill them?"):
        return False
    for pid, executable, _ in remaining:
        if still_running(pid, executable):
            if platform.WINDOWS:
                platform.stop_external(pid, force=True)
            else:
                os.kill(pid, platform.FORCE_KILL)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if not any(still_running(p[0], p[1]) for p in remaining):
            return True
        time.sleep(0.1)
    raise ValueError("Servers are still running; launch cancelled")


def memory():
    if platform.WINDOWS:
        return platform.windows_memory()
    values = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, value = line.split(":", 1)
        values[key] = int(value.split()[0]) * 1024
    return values["MemTotal"], values["MemAvailable"]


BASE = "\x1b[48;2;0;0;0m\x1b[38;2;220;225;230m"
BORDER = "\x1b[38;2;80;145;160m"
MUTED = "\x1b[38;2;130;145;155m"
GREEN = "\x1b[38;2;135;220;145m"
GOLD = "\x1b[38;2;225;190;110m"
ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\)|[@-_])")


def clean_text(value):
    # Child output cannot change cursor position, background, or terminal modes.
    return "".join(c for c in ANSI.sub("", value).expandtabs(4) if c.isprintable())


def cell_width(char):
    if unicodedata.combining(char):
        return 0
    return 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1


def fit_text(text, width):
    result, cells = [], 0
    for char in clean_text(text):
        size = cell_width(char)
        if cells + size > width:
            break
        result.append(char)
        cells += size
    return "".join(result) + " " * max(0, width - cells)


def wrap_cells(text, width):
    """Yield source offsets as well as text so resize preserves scroll anchors."""
    text = clean_text(text)
    offset, cells, part = 0, 0, []
    for index, char in enumerate(text):
        size = cell_width(char)
        if part and cells + size > width:
            yield offset, "".join(part)
            offset, cells, part = index, 0, []
        part.append(char)
        cells += size
    yield offset, "".join(part)


def framed(text, inner, color=BASE):
    return BORDER + "│ " + color + fit_text(text, inner) + BORDER + " │"


def edge(width, left, right, title=""):
    heading = fit_text(title, min(width - 2, sum(cell_width(c) for c in title))).rstrip()
    return BORDER + left + heading + "─" * max(0, width - 2 - sum(cell_width(c) for c in heading)) + right


class TerminalScreen:
    """One balanced alternate screen; only changed rows are written."""
    def __init__(self, mouse=False):
        self.mouse = mouse
        self.previous = ()
        self.size = None

    def __enter__(self):
        if platform.WINDOWS:
            self.console = platform.WindowsConsole(self.mouse)
            self.console.enter()
            self.descriptor = self.console
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        else:
            self.descriptor = sys.stdin.fileno()
            self.original = termios.tcgetattr(self.descriptor)
            tty.setcbreak(self.descriptor, termios.TCSANOW)
        modes = "\x1b[?1002h\x1b[?1006h" if self.mouse and not platform.WINDOWS else ""
        sys.stdout.write("\x1b[?1049h" + BASE + "\x1b[2J\x1b[H\x1b[?25l" + modes)
        sys.stdout.flush()
        return self

    def draw(self, lines, size, force=False):
        width, height = size
        lines = tuple(lines[:height])
        resized = size != self.size
        chunks = [BASE]
        for row in range(max(len(lines), min(height, len(self.previous)))):
            current = lines[row] if row < len(lines) else ""
            old = self.previous[row] if row < len(self.previous) else None
            if force or resized or current != old:
                chunks.append(f"\x1b[{row + 1};1H\x1b[2K " + current)
        if len(chunks) > 1:
            sys.stdout.write("".join(chunks))
            sys.stdout.flush()
        self.previous, self.size = lines, size

    def __exit__(self, *_):
        try:
            if not platform.WINDOWS:
                termios.tcsetattr(self.descriptor, termios.TCSADRAIN, self.original)
        finally:
            modes = "\x1b[?1002l\x1b[?1006l" if self.mouse and not platform.WINDOWS else ""
            sys.stdout.write(modes + "\x1b[0m\x1b[?25h\x1b[?1049l")
            sys.stdout.flush()
            if platform.WINDOWS:
                self.console.restore()


def choose(names, title, details, manage=False, enter="select", update=None, back=False):
    if not names:
        raise ValueError("No items to select")
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        for index, name in enumerate(names, 1):
            print(f"{index:2}. {name} | {' | '.join(details(name))}")
        shortcuts = ", r to reload, e to edit config"
        shortcuts += ", m to manage" if manage else ""
        shortcuts += ", u to update first item" if update else ""
        shortcuts += ", b to go back" if back else ""
        selected = input("Select (number or name, q to quit" + shortcuts + "): ").strip()
        if selected.lower() == "e":
            raise EditRequested
        if selected.lower() == "r":
            raise ReloadRequested
        if back and selected.lower() == "b":
            return "@back"
        if update and selected.lower() == "u":
            return "@update:" + update(names[0])
        if manage and selected.lower() == "m":
            return "@manage"
        if selected.lower() == "q":
            return None
        if selected.isdigit() and 1 <= int(selected) <= len(names):
            return names[int(selected) - 1]
        return selected

    selected, first, show_command = 0, 0, False
    with TerminalScreen() as screen:
        while True:
            width, height = os.get_terminal_size(sys.stdout.fileno())
            frame_width, count = min(110, width - 2), 1
            inner = max(1, frame_width - 4)
            items = list(details(names[selected]))
            has_command = any(item.startswith("Run: ") for item in items)
            detail_lines = []
            for item in items:
                if item.startswith("Run: ") and not show_command:
                    continue
                detail_lines.extend(textwrap.wrap(clean_text(item), width=inner,
                                                  subsequent_indent="     " if item.startswith("Run: ") else "",
                                                  break_on_hyphens=False) or [""])
            shortcuts = (("U update/build   " if update else "")
                         + ("M all forks   " if manage else "")
                         + ("V command   " if has_command else "") + "E edit   R reload")
            shortcut_lines = textwrap.wrap(shortcuts, width=inner, break_on_hyphens=False)
            if height < 10 or width < 40:
                lines = [MUTED + fit_text("Enlarge terminal (40x10). q quits.", max(1, width - 2))]
            else:
                # Keep controls and one selected item visible even with a huge command.
                detail_budget = max(0, height - len(shortcut_lines) - 6)
                if len(detail_lines) > detail_budget:
                    detail_lines = detail_lines[:detail_budget]
                    if detail_lines:
                        detail_lines[-1] = "… enlarge terminal for more details"
                count = max(1, min(20, height - len(detail_lines) - len(shortcut_lines) - 5))
                first = max(min(first, selected), selected - count + 1)
                lines = [edge(frame_width, "╭", "╮", f" {title}  [{selected + 1}/{len(names)}] "),
                         framed(f"↑/↓ select   Enter {enter}" + ("   ← back" if back else "")
                                + "   Esc/q quit", inner, MUTED),
                         edge(frame_width, "├", "┤")]
                for index in range(first, min(first + count, len(names))):
                    active = index == selected
                    lines.append(framed(f"{'>' if active else ' '} {names[index]}", inner,
                                        "\x1b[1m" + GREEN if active else BASE) + "\x1b[22m")
                lines += [edge(frame_width, "├", "┤"),
                          *[framed(item, inner) for item in detail_lines],
                          *[framed(item, inner, GOLD) for item in shortcut_lines],
                          edge(frame_width, "╰", "╯")]
            screen.draw(lines, (width, height))
            key = read_key(screen.descriptor)
            if key in (ord("e"), ord("E")):
                raise EditRequested
            if has_command and key in (ord("v"), ord("V")):
                show_command = not show_command
            elif key in (ord("r"), ord("R")):
                raise ReloadRequested
            elif key in (27, ord("q"), ord("Q")):
                return None
            elif key == curses.KEY_LEFT and back:
                return "@back"
            elif manage and key in (ord("m"), ord("M")):
                return "@manage"
            elif update and key in (ord("u"), ord("U")):
                return "@update:" + update(names[selected])
            elif key in (10, 13) and height >= 10 and width >= 40:
                return names[selected]
            elif key in (curses.KEY_DOWN, ord("j")):
                selected = min(len(names) - 1, selected + 1)
            elif key in (curses.KEY_UP, ord("k")):
                selected = max(0, selected - 1)
            elif key == curses.KEY_NPAGE:
                selected = min(len(names) - 1, selected + count)
            elif key == curses.KEY_PPAGE:
                selected = max(0, selected - count)
            elif key == curses.KEY_HOME:
                selected = 0
            elif key == curses.KEY_END:
                selected = len(names) - 1


def read_key(descriptor):
    if platform.WINDOWS:
        return descriptor.read()
    if not select.select([descriptor], [], [], 0.05)[0]:
        return None
    key = os.read(descriptor, 1)
    if not key:
        return ord("q")
    if key != b"\x1b":
        return key[0]
    sequence = b""
    while len(sequence) < 64 and select.select([descriptor], [], [], 0.04)[0]:
        sequence += os.read(descriptor, 1)
        if len(sequence) >= 2 and 0x40 <= sequence[-1] <= 0x7e:
            break
    if not sequence:
        return 27
    mouse = re.fullmatch(rb"\[<(\d+);(\d+);(\d+)([Mm])", sequence)
    if mouse:
        button, column, row = map(int, mouse.group(1, 2, 3))
        return ("mouse", button, column, row, mouse[4] == b"m")
    return {
        b"[A": curses.KEY_UP, b"OA": curses.KEY_UP,
        b"[B": curses.KEY_DOWN, b"OB": curses.KEY_DOWN,
        b"[D": curses.KEY_LEFT, b"OD": curses.KEY_LEFT,
        b"[C": curses.KEY_RIGHT, b"OC": curses.KEY_RIGHT,
        b"[5~": curses.KEY_PPAGE, b"[6~": curses.KEY_NPAGE,
        b"[H": curses.KEY_HOME, b"OH": curses.KEY_HOME, b"[1~": curses.KEY_HOME,
        b"[F": curses.KEY_END, b"OF": curses.KEY_END, b"[4~": curses.KEY_END,
    }.get(sequence)  # Unsupported escape sequences must not quit the launcher.


def server_address(command, environment=None):
    environment = environment or {}
    host = environment.get("LLAMA_ARG_HOST", "127.0.0.1")
    port = environment.get("LLAMA_ARG_PORT", "8080")
    for index, value in enumerate(command):
        name, separator, inline = value.partition("=")
        if name in {"--host", "--port"}:
            argument = inline if separator else command[index + 1] if index + 1 < len(command) else ""
            if name == "--host":
                host = argument
            else:
                port = argument
    if host in {"0.0.0.0", "::", "[::]"}:
        host = "127.0.0.1"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return host, port


def log_color(line):
    upper = line.upper()
    if re.search(r"\b(ERROR|FATAL|FAILED|FAIL)\b|(?:^|\s)E\s", upper):
        return "\x1b[38;2;255;105;105m"
    if re.search(r"\b(WARN|WARNING)\b|(?:^|\s)W\s", upper):
        return GOLD
    if re.search(r"tokens? (?:per second|/s)|tok/s|eval time", line, re.I):
        return "\x1b[38;2;190;145;255m"
    if re.search(r"\b(CUDA\w*|GPU|VRAM|OFFLOADING)\b", upper):
        return "\x1b[38;2;100;205;220m"
    if re.search(r"\b(INFO|NOTICE|STARTING|READY|HTTP)\b|(?:^|\s)I\s", upper):
        return GREEN
    return BASE


class LogHistory:
    """Bounded source lines with persistent IDs and an incremental UTF-8 decoder."""
    def __init__(self, limit=2000):
        self.lines = deque(maxlen=limit)
        self.decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self.pending = ""
        self.sequence = 0
        self.version = 0
        self.state = "Loading"
        self.prompt_rate = None
        self.generation_rate = None
        self.build_progress = None
        self.rows_cache = None
        self.quiet_status_until = 0
        self.hidden_status_record = False
        self.request_record = []
        self.filter_request_logs = False

    def add(self, text):
        line = clean_text(text)
        lower = line.lower()
        if "slot is processing task" in lower or "new prompt" in lower:
            self.state = "Prompt"
        elif "slot released" in lower or "all slots are idle" in lower:
            self.state = "Idle"
        monitoring = time.monotonic() < getattr(self, "quiet_status_until", 0)
        new_record = bool(re.match(r"\s*(?:INFO|WARN|ERROR|DEBUG|TRACE)\s*\[|\[\d+\]|(?:\s*\d+\.\d+\s+)?[IWE]\s", line))
        if new_record:
            self.hidden_status_record = False
        if monitoring and ("slot data" in lower or "all slots are idle" in lower):
            self.hidden_status_record = True
            return
        if getattr(self, "hidden_status_record", False) and not new_record:
            return
        # This fork splits HTTP access entries over two physical lines.
        request_record = getattr(self, "request_record", [])
        if "log_server_request" in lower and "path=" not in lower:
            if request_record:
                for pending in request_record:
                    self._store(pending)
            self.request_record = [line]
            return
        if request_record:
            if not new_record:
                request_record.append(line)
                if "path=" not in lower:
                    return
                if "launcher_status" not in " ".join(request_record):
                    for pending in request_record:
                        self._store(pending)
                self.request_record = []
                return
            for pending in request_record:
                self._store(pending)
            self.request_record = []
        if "launcher_status" in lower and "/slots" in lower:
            return
        self._store(line)

    def _store(self, line):
        self.lines.append((self.sequence, line, log_color(line)))
        self.sequence += 1
        self.version += 1
        progress = re.match(r"\s*\[\s*(\d+)%\]", line)
        ninja = re.match(r"\s*\[(\d+)/(\d+)\]", line)
        if progress:
            self.build_progress = min(100, int(progress[1]))
        elif ninja and int(ninja[2]) > 0:
            self.build_progress = min(100, int(ninja[1]) * 100 // int(ninja[2]))
        match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*(?:tokens? per second|tokens?/s|tok/s)", line, re.I)
        if match and ("eval time" in line.lower() or "prompt processing" in line.lower()):
            if "prompt" in line.lower():
                self.prompt_rate = float(match[1])
            else:
                self.generation_rate = float(match[1])
        live_generation = re.search(r"\btg_3s\s*=\s*([0-9.]+)\s*t/s", line, re.I)
        if not live_generation:
            live_generation = re.search(r"\btg\s*=\s*([0-9.]+)\s*t/s", line, re.I)
        if live_generation:
            self.generation_rate = float(live_generation[1])
            self.state = "Generating"
        lower = line.lower()
        if "all slots are idle" in lower or "server is listening" in lower or "server listening" in lower:
            self.state = "Idle"
        elif "prompt processing" in lower or "processing prompt" in lower:
            self.state = "Prompt"
        elif "prompt eval time" in lower or "prompt done" in lower:
            self.state = "Generating"
        elif "total time" in lower or "release slot" in lower or "stop processing" in lower:
            self.state = "Idle"

    def feed(self, data, final=False):
        self.pending += self.decoder.decode(data, final=final)
        # Bound an unterminated line as well as the number of retained lines.
        while True:
            match = re.search(r"[\r\n]", self.pending)
            if len(self.pending) > 16384 and (match is None or match.start() > 16384):
                self.add(self.pending[:16384])
                self.pending = self.pending[16384:]
            elif match:
                self.add(self.pending[:match.start()])
                end = match.end()
                if self.pending[match.start():end] == "\r" and self.pending[end:end + 1] == "\n":
                    end += 1
                self.pending = self.pending[end:]
            else:
                break
        if final and self.pending:
            self.add(self.pending)
            self.pending = ""
        if final and getattr(self, "request_record", []):
            for line in self.request_record:
                self._store(line)
            self.request_record = []

    def rows(self, width):
        filtering = getattr(self, "filter_request_logs", False)
        key = (self.version, self.pending, width, filtering)
        if self.rows_cache and self.rows_cache[0] == key:
            return self.rows_cache[1]
        rows = []
        source = list(self.lines)
        if self.pending:
            source.append((self.sequence, clean_text(self.pending), log_color(clean_text(self.pending))))
        request_continuation = False
        for identity, line, color in source:
            if filtering:
                if "log_server_request" in line.lower():
                    request_continuation = True
                    continue
                if request_continuation and re.match(
                    r"\s*(?:remote_addr|remote_port|status|method|path|params|request|response)\s*=", line):
                    continue
                request_continuation = False
            rows.extend((identity, offset, text, color) for offset, text in wrap_cells(line, width))
        self.rows_cache = key, rows
        return rows


class LogViewport:
    def __init__(self):
        self.anchor = None  # None follows live output; otherwise (line ID, character offset).
        self.drag_offset = None
        self.drag_origin = None

    def position(self, rows, height):
        maximum = max(0, len(rows) - height)
        if self.anchor is None:
            return maximum
        identity, offset = self.anchor
        candidates = [i for i, row in enumerate(rows) if row[0] == identity and row[1] <= offset]
        if candidates:
            return min(maximum, candidates[-1])
        return min(maximum, next((i for i, row in enumerate(rows) if row[0] > identity), 0))

    def move_to(self, top, rows, height):
        maximum = max(0, len(rows) - height)
        top = max(0, min(maximum, top))
        self.anchor = (rows[top][0], rows[top][1]) if rows and top < maximum else None

    def thumb(self, top, total, height):
        length = max(min(2, height), min(height, height * height // max(1, total)))
        travel = height - length
        start = round(top * travel / max(1, total - height))
        return start, length

    def handle(self, key, rows, height, column, first_row):
        top = self.position(rows, height)
        maximum = max(0, len(rows) - height)
        if isinstance(key, tuple):
            _, button, x, y, released = key
            if released:
                self.drag_offset = None
                self.drag_origin = None
                return
            if button & 64:
                if first_row <= y < first_row + height:
                    self.move_to(top + (-3 if button & 1 == 0 else 3), rows, height)
                return
            track = y - first_row
            # Include both track cells, padding, and the right border as grab targets.
            if button == 0 and column <= x <= column + 3 and 0 <= track < height:
                start, length = self.thumb(top, len(rows), height)
                on_thumb = start <= track < start + length
                self.drag_offset = track - start if on_thumb else length // 2
                self.drag_origin = (track, top)
                if on_thumb:
                    return
            elif button != 32 or self.drag_offset is None:
                return
            _, length = self.thumb(top, len(rows), height)
            if button == 32:
                origin_row, origin_top = self.drag_origin
                target = origin_top + round((track - origin_row) * maximum / max(1, height - length))
            else:
                target = round((track - self.drag_offset) * maximum / max(1, height - length))
                self.drag_origin = (track, max(0, min(maximum, target)))
            self.move_to(target, rows, height)
        elif key in (curses.KEY_UP, ord("k")):
            self.move_to(top - 1, rows, height)
        elif key in (curses.KEY_DOWN, ord("j")):
            self.move_to(top + 1, rows, height)
        elif key == curses.KEY_PPAGE:
            self.move_to(top - height, rows, height)
        elif key == curses.KEY_NPAGE:
            self.move_to(top + height, rows, height)
        elif key == curses.KEY_HOME:
            self.move_to(0, rows, height)
        elif key == curses.KEY_END:
            self.anchor = None


class SpeedSampler:
    """Sample token counters only while busy, without blocking terminal input."""
    def __init__(self, command, environment):
        host, port = server_address(command, environment)
        self.url = f"http://{host}:{port}/slots?launcher_status=1&only_metrics=1"
        self.headers = {}
        api_key = environment.get("LLAMA_ARG_API_KEY", "")
        for index, value in enumerate(command):
            name, separator, inline = value.partition("=")
            if name in {"--api-key", "--api-key-file"}:
                value = inline if separator else command[index + 1] if index + 1 < len(command) else ""
                if name == "--api-key":
                    api_key = value
                else:
                    try:
                        api_key = Path(value).read_text().splitlines()[0].strip()
                    except (OSError, IndexError):
                        pass
        if api_key:
            self.headers["Authorization"] = "Bearer " + api_key.split(",")[0]
        self.thread = None
        self.result = None
        self.next_poll = 0.0
        self.previous = {}
        self.live_prompt = None
        self.live_generation = None
        self.disabled = False
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def fetch(self):
        try:
            request = urllib.request.Request(self.url, headers=self.headers)
            with self.opener.open(request, timeout=.5) as response:
                slots = json.loads(response.read(2 * 1024 * 1024))
            self.result = (time.monotonic(), slots, None)
        except urllib.error.HTTPError as error:
            self.result = (time.monotonic(), None, error.code)
        except (OSError, ValueError):
            self.result = (time.monotonic(), None, None)

    def measure(self, when, slots):
        if not isinstance(slots, list):
            return
        current, prompt_rates, generation_rates = {}, [], []
        generating = False
        for slot in slots:
            if not isinstance(slot, dict):
                continue
            token = slot.get("next_token", {})
            if isinstance(token, list):
                token = token[0] if token else {}
            if not isinstance(token, dict):
                continue
            decoded = token.get("n_decoded")
            prompt = slot.get("n_prompt_tokens_processed")
            if not isinstance(decoded, (int, float)) or decoded < 0:
                continue
            if slot.get("is_processing") is False:
                continue
            if "is_processing" not in slot and slot.get("state") == 0:
                decoded = 0  # Idle/queued ik slots can retain the previous token count.
            # Old ik forks expose state 0 during prompt loading, with no
            # is_processing field; the task-start log controls active polling.
            key = (slot.get("id"), slot.get("id_task"))
            current[key] = (when, prompt, decoded)
            generating |= decoded > 0
            previous = self.previous.get(key)
            if previous:
                elapsed = when - previous[0]
                if elapsed <= 0:
                    continue
                if decoded >= previous[2] and decoded > 0:
                    generation_rates.append((decoded - previous[2]) / elapsed)
                if isinstance(prompt, (int, float)) and isinstance(previous[1], (int, float)):
                    if prompt >= previous[1] and decoded == 0:
                        prompt_rates.append((prompt - previous[1]) / elapsed)
        self.previous = current
        self.live_prompt = sum(prompt_rates) if prompt_rates else None
        self.live_generation = sum(generation_rates) if generation_rates else None
        return generating

    def step(self, history):
        busy = history.state in {"Prompt", "Generating"}
        if self.thread is not None and not self.thread.is_alive():
            result, self.result, self.thread = self.result, None, None
            if result:
                when, slots, error = result
                if error in {401, 403, 404, 501}:
                    self.disabled = True
                if busy:
                    if slots is None:
                        self.live_prompt = self.live_generation = None
                    elif self.measure(when, slots):
                        history.state = "Generating"
        if not busy:
            self.previous.clear()
            self.live_prompt = self.live_generation = None
            return
        now = time.monotonic()
        if self.thread is None and not self.disabled and now >= self.next_poll:
            history.quiet_status_until = now + 2.0
            self.next_poll = now + 2.0
            self.thread = threading.Thread(target=self.fetch, daemon=True)
            self.thread.start()

    def close(self):
        if self.thread is not None:
            self.thread.join(timeout=1)


class ServerSession:
    """Keep subprocess, output, and shutdown ownership outside reloadable UI code."""
    def __init__(self, command, cwd, environment, title, stage=None):
        self.command, self.environment, self.title = command, environment, title
        self.stage = stage or "Running"
        self.process, self.job = platform.start_process(command, cwd, environment, stdin=subprocess.DEVNULL,
                                                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.reader = platform.PipeReader(self.process.stdout)
        self.history, self.viewport = LogHistory(), LogViewport()
        self.eof = False
        self.stop_deadline = None
        self.notice = ""
        self.sampler = SpeedSampler(command, environment) if title == "llama-server output" else None

    def drain(self):
        # Bound each cycle so a busy logger cannot starve keys or resize events.
        reader = getattr(self, "reader", None)
        if reader is None:
            # Upgrade a running session created by an older launcher on R.
            os.set_blocking(self.process.stdout.fileno(), True)
            self.reader = reader = platform.PipeReader(self.process.stdout)
        for data in reader.available():
            if not data:
                if not self.eof:
                    self.history.feed(b"", final=True)
                self.eof = True
                break
            self.history.feed(data)

    def signal_group(self, sig):
        platform.signal_process(self.process, sig, getattr(self, "job", None))

    def stop(self):
        if self.stop_deadline is None:
            self.stop_deadline = time.monotonic() + 5
            if self.process.poll() is None:
                self.signal_group(signal.SIGTERM)

    def cleanup(self):
        if getattr(self, "sampler", None) is not None:
            self.sampler.close()
        if self.process.poll() is None:
            self.signal_group(signal.SIGTERM)
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.signal_group(platform.FORCE_KILL)
                self.process.wait(timeout=2)
        # Reap descendants even when the leader has exited and closed its pipe.
        self.signal_group(signal.SIGTERM)
        if getattr(self, "job", None) is not None:
            self.job.close()
        if getattr(self, "reader", None) is not None:
            self.reader.close()
        self.process.stdout.close()


def server_frame(session, width, height):
    if width < 40 or height < 10:
        return [MUTED + fit_text("Enlarge terminal (40x10). q stops.", max(1, width - 2))], [], 1, 0
    frame_width = width - 2
    inner, log_width = frame_width - 4, frame_width - 6
    actions = "S stop/select   " if session.title == "llama-server output" else ""
    if session.title == "llama-server output" and getattr(session, "config_path", True):
        actions += "C change model   "
    controls = textwrap.wrap("↑/↓ scroll   " + actions + "R reload   q/Esc quit", width=inner)
    rate = lambda value: f"{value:.1f}" if value is not None else "—"
    history = session.history
    state = "Stopping" if session.stop_deadline is not None else history.state
    sampler = getattr(session, "sampler", None)
    live_prompt = getattr(sampler, "live_prompt", None)
    live_generation = getattr(sampler, "live_generation", None)
    prompt = live_prompt if live_prompt is not None else history.prompt_rate
    generation = live_generation if live_generation is not None else history.generation_rate
    prompt_source = "live" if live_prompt is not None else "last"
    generation_source = "live" if live_generation is not None else "last"
    if session.title == "llama-server output":
        status = (f"{state}  prompt {rate(prompt)} ({prompt_source})  "
                  f"gen {rate(generation)} tok/s ({generation_source})")
    else:
        status = "Stopping" if session.stop_deadline is not None else getattr(session, "stage", "Running")
        progress = getattr(history, "build_progress", None)
        if progress is not None and status.startswith("Building"):
            status += f"  {progress}%"
    if session.process.poll() is not None:
        status = f"Exited: {session.process.returncode}   q/Esc close"
    if not session.notice:
        status += f" | {'LIVE' if session.viewport.anchor is None else 'PAUSED'}"
    status_lines = textwrap.wrap(status, width=inner, break_on_hyphens=False)
    budget = max(1, height - len(controls) - 5)
    status_lines = status_lines[:budget]
    notice_lines = textwrap.wrap(session.notice, width=inner, break_on_hyphens=False)
    status_lines = notice_lines[:max(0, budget - len(status_lines))] + status_lines
    header = [edge(frame_width, "╭", "╮", f" {session.title} "),
              *[framed(line, inner, MUTED) for line in controls],
              edge(frame_width, "├", "┤")]
    available = max(1, height - len(header) - len(status_lines) - 2)
    rows = history.rows(log_width)
    top = session.viewport.position(rows, available)
    thumb_start, thumb_size = session.viewport.thumb(top, len(rows), available)
    lines = list(header)
    for index in range(available):
        row = rows[top + index] if top + index < len(rows) else None
        text, color = (row[2], row[3]) if row else ("", BASE)
        scroll = BASE + "██" if thumb_start <= index < thumb_start + thumb_size else MUTED + "│ "
        lines.append(BORDER + "│ " + color + fit_text(text, log_width)
                     + scroll + BORDER + " │")
    lines += [edge(frame_width, "├", "┤"), *[framed(line, inner, GREEN) for line in status_lines],
              edge(frame_width, "╰", "╯")]
    return lines, rows, available, len(header) + 1


def cell_segment(text, start, end):
    """Crop a background row by cells, including boundaries through wide glyphs."""
    result, position = [], 0
    for char in clean_text(text):
        size = cell_width(char)
        following = position + size
        if start <= position and following <= end:
            result.append(char)
        elif following > start and position < end:
            result.append(" " * (min(end, following) - max(start, position)))
        position = following
        if position >= end:
            break
    return fit_text("".join(result), end - start)


def popup_frame(session, width, height, presets, models, selected):
    background, *_ = server_frame(session, width, height)
    if width < 40 or height < 10:
        return background
    names = list(presets)
    popup_width = min(100, width - 6)
    inner = popup_width - 4
    controls = textwrap.wrap("↑/↓ select   Enter change   ←/Esc/q cancel", width=inner)
    model = models[presets[names[selected]]["model"]]
    details = textwrap.wrap(f"Fork: {model['instance']}   Model: {model['path'].name}", width=inner)
    detail_budget = max(1, height - len(controls) - 7)
    details = details[:detail_budget]
    count = max(1, min(20, height - len(controls) - len(details) - 6))
    first = max(0, min(selected - count // 2, len(names) - count))
    popup = [edge(popup_width, "╭", "╮", f" Change model / preset [{selected + 1}/{len(names)}] "),
             *[framed(line, inner, MUTED) for line in controls], edge(popup_width, "├", "┤")]
    for index in range(first, min(first + count, len(names))):
        instance_name = models[presets[names[index]]["model"]]["instance"]
        popup.append(framed(f"{'>' if index == selected else ' '} {names[index]} [{instance_name}]", inner,
                            GREEN if index == selected else BASE))
    popup += [edge(popup_width, "├", "┤"), *[framed(line, inner) for line in details],
              edge(popup_width, "╰", "╯")]
    frame_width = width - 2
    left, top = (frame_width - popup_width) // 2, (height - len(popup)) // 2
    lines = [MUTED + fit_text(clean_text(line), frame_width) for line in background]
    while len(lines) < height:
        lines.append(MUTED + " " * frame_width)
    for index, row in enumerate(popup, top):
        old = lines[index]
        lines[index] = (MUTED + cell_segment(old, 0, left) + row + MUTED
                        + cell_segment(old, left + popup_width, frame_width))
    return lines


def model_popup(session, screen):
    instances, models, presets, _ = read_config(Path(session.config_path).resolve(), announce=False)
    names, selected = list(presets), 0
    while True:
        # Keep draining the old process while the popup is open.
        session.drain()
        if session.process.poll() is None and getattr(session, "sampler", None) is not None:
            session.sampler.step(session.history)
        size = os.get_terminal_size(sys.stdout.fileno())
        screen.draw(popup_frame(session, *size, presets, models, selected), size)
        key = read_key(screen.descriptor)
        if key in (27, curses.KEY_LEFT, ord("q"), ord("Q"), ord("c"), ord("C")):
            return None
        if key in (10, 13) and size.columns >= 40 and size.lines >= 10:
            name = names[selected]
            model = models[presets[name]["model"]]
            validate_launch(instances[model["instance"]], model)
            return name
        if key in (curses.KEY_DOWN, ord("j")):
            selected = min(len(names) - 1, selected + 1)
        elif key in (curses.KEY_UP, ord("k")):
            selected = max(0, selected - 1)
        elif key == curses.KEY_HOME:
            selected = 0
        elif key == curses.KEY_END:
            selected = len(names) - 1
        elif isinstance(key, tuple) and key[1] & 64:
            selected = max(0, min(len(names) - 1, selected + (-1 if key[1] & 1 == 0 else 1)))
        elif key == 3:
            raise KeyboardInterrupt


def server_view(session, screen):
    """Return on R; owner loads new code without losing the server or log pipe."""
    if not hasattr(session, "config_path"):
        session.config_path = Path(__file__).with_name("llama.ini")
        for index, argument in enumerate(sys.argv):
            if argument == "--config" and index + 1 < len(sys.argv):
                session.config_path = Path(sys.argv[index + 1])
            elif argument.startswith("--config="):
                session.config_path = Path(argument.partition("=")[2])
    if session.config_path is not None and getattr(session, "log_settings_view", None) is not server_view:
        _, _, _, settings = read_config(Path(session.config_path).resolve(), announce=False)
        session.history.filter_request_logs = settings.get("filter_request_logs", False)
        session.log_settings_view = server_view
    while True:
        session.drain()
        code = session.process.poll()
        if session.title == "llama-server output":
            if not hasattr(session, "sampler"):
                session.sampler = SpeedSampler(session.command, session.environment)
            if code is None and session.stop_deadline is None:
                session.sampler.step(session.history)
        if session.stop_deadline and time.monotonic() >= session.stop_deadline:
            session.signal_group(platform.FORCE_KILL)
        size = os.get_terminal_size(sys.stdout.fileno())
        lines, rows, available, first_row = server_frame(session, *size)
        screen.draw(lines, size)
        if code is not None and session.eof and session.title == "llama.cpp update/build":
            return 130 if session.stop_deadline else code if code >= 0 else 128 - code
        key = read_key(screen.descriptor)
        if key in (ord("r"), ord("R")):
            return "reload"
        if session.title == "llama-server output" and session.stop_deadline is None:
            if key in (ord("s"), ord("S")):
                session.exit_action = "@select"
                session.stop()
            elif key in (ord("c"), ord("C")) and session.config_path is not None:
                try:
                    selected = model_popup(session, screen)
                    if selected is not None:
                        session.exit_action = ("@change", selected)
                        session.stop()
                except (ValueError, KeyError, OSError, configparser.Error) as error:
                    session.notice = f"Model change cancelled: {error}"
        if key in (27, ord("q"), ord("Q"), 3):
            session.exit_action = 0
            if code is not None:
                return 0 if session.stop_deadline else max(0, code) if code >= 0 else 128 - code
            session.stop()
        if session.stop_deadline and session.process.poll() is not None and session.eof:
            action = getattr(session, "exit_action", 0)
            if action and not getattr(session, "supports_launch_actions", False):
                # An older owner can load this view with R. Restart only after its
                # child has stopped, so S/C also work without restarting it first.
                session.cleanup()
                screen.__exit__()
                arguments = [sys.executable, str(Path(__file__).resolve()), "--config", str(session.config_path)]
                if isinstance(action, tuple):
                    arguments.append(action[1])
                os.execv(sys.executable, arguments)
            return action
        session.viewport.handle(key, rows, available, size.columns - 5, first_row)


def run_server(command, cwd, environment, title="llama-server output", config_path=None, stage=None):
    # Normal launcher termination must also clean up the process it owns.
    def terminate(signum, frame):
        raise KeyboardInterrupt
    previous = signal.signal(signal.SIGTERM, terminate)
    try:
        return run_process(command, cwd, environment, title, config_path, stage)
    finally:
        signal.signal(signal.SIGTERM, previous)


def run_process(command, cwd, environment, title, config_path, stage=None):
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        process, job = platform.start_process(command, cwd, environment)
        try:
            code = process.wait()
            return code if code >= 0 else 128 - code
        except BaseException:
            try:
                platform.signal_process(process, signal.SIGTERM, job)
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                platform.signal_process(process, platform.FORCE_KILL, job)
                process.wait(timeout=2)
            except ProcessLookupError:
                pass
            raise
        finally:
            if job is not None:
                job.close()

    session = ServerSession(command, cwd, environment, title, stage)
    session.config_path = config_path
    session.supports_launch_actions = True
    try:
        with TerminalScreen(mouse=True) as screen:
            view = server_view
            while True:
                result = view(session, screen)
                if result != "reload":
                    return result
                try:
                    namespace = runpy.run_path(str(Path(__file__).resolve()), run_name="launcher_reload")
                    if config_path is not None:
                        namespace["read_config"](Path(config_path).resolve(), announce=False)
                    # Reclass existing state: reload methods, retain every live resource.
                    session.history.__class__ = namespace["LogHistory"]
                    session.viewport.__class__ = namespace["LogViewport"]
                    session.__class__ = namespace["ServerSession"]
                    if getattr(session, "sampler", None) is not None:
                        session.sampler.__class__ = namespace["SpeedSampler"]
                    screen.__class__ = namespace["TerminalScreen"]
                    view = namespace["server_view"]
                    session.history.rows_cache = None
                    session.notice = ("Launcher code reloaded; server PID unchanged" if title == "llama-server output"
                                      else "Launcher code reloaded; process PID unchanged")
                    screen.previous = ()
                except Exception as error:
                    session.notice = f"Reload failed: {error}"
                    screen.previous = ()
    finally:
        session.cleanup()


def launch_command(instance, model, preset):
    command = [str(instance["binary"]), "-m", str(model["path"])]
    for item in (instance, model, preset):
        command.extend(flags(item.get("flags", "")))
    return command


def format_invocation(command, environment):
    if platform.WINDOWS:
        prefix = "".join(f'set "{key}={value}" && ' for key, value in environment.items())
        return prefix + platform.command_text(command)
    prefix = ["env", *[f"{key}={value}" for key, value in environment.items()]] if environment else []
    return shlex.join([*prefix, *command])


def select_preset(presets, models, instances=None):
    def details(name):
        model = models[presets[name]["model"]]
        if instances is None:
            return f"Fork: {model['instance']}", f"Model: {model['path'].name}"
        instance = instances[model["instance"]]
        command = launch_command(instance, model, presets[name])
        invocation = format_invocation(command, model.get("env", {}))
        return (f"Fork: {model['instance']}", f"Model: {model['path'].name}",
                f"Run: {invocation}")
    return choose(list(presets), "llama.cpp presets", details, manage=True, enter="launch",
                  update=lambda name: models[presets[name]["model"]]["instance"])


ACTIONS = {
    "update": "Update only",
    "update-full": "Update + build all targets",
    "update-server": "Update + build server only",
    "build-full": "Build all targets",
    "build-server": "Build server only",
}


def maintenance(instance, action, dry_run=False):
    source, build = instance["source"], instance["build_dir"]
    jobs = str(int(instance.get("jobs", os.cpu_count() or 1)))
    if int(jobs) < 1:
        raise ValueError("jobs must be positive")
    def command(key, default):
        # Expand placeholders per token so paths containing spaces stay one argument.
        return [arg.replace("{source}", str(source)).replace("{build}", str(build)).replace("{jobs}", jobs)
                for arg in flags(instance[key])] if key in instance else default
    commands = []
    updating = action.startswith("update")
    if updating:
        commands.append(("Updating", command("update_command", ["git", "pull", "--ff-only"])))
    if action != "update":
        commands.append(("Configuring", command("configure_command", ["cmake", "-S", str(source), "-B", str(build)]
                                + flags(instance.get("configure_flags", "-DCMAKE_BUILD_TYPE=Release")))))
        server = action.endswith("server")
        default = ["cmake", "--build", str(build), "--parallel", jobs]
        if platform.WINDOWS:
            default += ["--config", instance.get("build_config", "Release")]
        if server:
            default += ["--target", instance.get("server_target", "llama-server")]
        commands.append(("Building server" if server else "Building all targets",
                         command("build_server_command" if server else "build_full_command", default)))
    print("Source directory:", source)
    for _, cmd in commands:
        print("Command:", platform.command_text(cmd))
    if dry_run:
        return 0
    if not source.is_dir():
        raise ValueError(f"Source folder does not exist: {source}")
    env = os.environ.copy()
    if "path_prefix" in instance:
        env["PATH"] = os.path.expandvars(os.path.expanduser(instance["path_prefix"])) + os.pathsep + env.get("PATH", "")
    if updating:
        status = subprocess.run(platform.command_argv(["git", "status", "--porcelain", "--untracked-files=normal"], env),
                                cwd=source, env=env, text=True, capture_output=True, check=True)
        if status.stdout.strip():
            raise ValueError("Working tree has local changes. Commit or stash them before updating; nothing was changed.")
    processes = running_processes({"selected": instance})
    if processes:
        for pid, _, cmd in processes:
            print(f"Running server PID {pid}: {cmd}")
        if not confirm("Stop this instance's servers before maintenance?") or not stop_processes(processes):
            print("Maintenance cancelled.")
            return 1
    for stage, cmd in commands:
        print("Running:", platform.command_text(cmd), flush=True)
        code = run_server(cmd, source, env, title="llama.cpp update/build", stage=stage)
        if code:
            raise subprocess.CalledProcessError(code, cmd)
    print("Maintenance completed.")
    return 0


def manage_instances(instances, instance_name=None, action=None, dry_run=False):
    while True:
        name = instance_name or choose(list(instances), "Manage llama.cpp instances",
                                      lambda n: (f"Source: {instances[n]['source']}", f"Build: {instances[n]['build_dir']}"),
                                      update=lambda n: n, back=True)
        if name is None:
            return 0
        if name == "@back":
            return "@back"
        name = name.removeprefix("@update:")
        if name not in instances:
            raise ValueError(f"Unknown instance: {name}")
        selected_action = action
        if selected_action is None:
            labels = list(ACTIONS.values())
            label = choose(labels, f"Manage {name}", lambda n: (f"Source: {instances[name]['source']}", n),
                           enter="run", back=True)
            if label is None:
                return 0
            if label == "@back":
                if instance_name:
                    return "@back"
                continue
            selected_action = next(key for key, value in ACTIONS.items() if value == label)
        if selected_action not in ACTIONS:
            raise ValueError(f"Unknown maintenance action: {selected_action}")
        result = maintenance(instances[name], selected_action, dry_run)
        if result == 0 and not dry_run and sys.stdin.isatty() and sys.stdout.isatty():
            return "@select"
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("preset", nargs="?", help="Preset name; omit for interactive menu")
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("llama.ini"))
    parser.add_argument("--list", action="store_true", help="List available presets")
    parser.add_argument("--dry-run", action="store_true", help="Print command without stopping or launching anything")
    parser.add_argument("--manage", action="store_true", help="Open instance update/build menu")
    parser.add_argument("--instance", help="Instance to update/build")
    parser.add_argument("--action", choices=ACTIONS, help="Maintenance action")
    args = parser.parse_args()
    if (args.manage or args.instance or args.action) and (args.preset or args.list):
        parser.error("Maintenance options cannot be combined with a preset or --list")
    while True:
        try:
            return run(args)
        except EditRequested:
            # Restore the terminal before handing control to a terminal editor.
            _, _, _, settings = read_config(args.config.resolve())
            if settings.get("editor"):
                edit_config(args.config, settings["editor"])
            else:
                edit_config(args.config)
        except ReloadRequested:
            # Re-exec Python so changes to this file are loaded too.
            sys.stdout.flush()
            sys.stderr.flush()
            arguments = sys.argv[1:]
            if getattr(args, "_returned_to_selection", False):
                arguments = ["--config", str(args.config)]
                if args.dry_run:
                    arguments.append("--dry-run")
            os.execv(sys.executable, [sys.executable, str(Path(__file__).resolve()), *arguments])
            return 0


def run(args):
    # Preserve navigation state when main resumes after the editor closes.
    current = args
    while True:
        result = run_once(current)
        if result == "@select":
            current._returned_to_selection = True
            current.preset = None
            current.manage = False
            current.instance = None
            current.action = None
        elif isinstance(result, tuple) and result[0] == "@change":
            current.preset = result[1]
        else:
            return result


def validate_launch(instance, model):
    if not instance["folder"].is_dir():
        raise ValueError(f"Instance folder does not exist: {instance['folder']}")
    if not instance["binary"].is_file() or not os.access(instance["binary"], os.X_OK):
        raise ValueError(f"Server binary missing or not executable: {instance['binary']}")
    if not model["path"].is_file():
        raise ValueError(f"Model file does not exist: {model['path']}")


def run_once(args):
    instances, models, presets, settings = read_config(args.config.resolve())
    if args.manage or args.instance or args.action:
        result = manage_instances(instances, args.instance, args.action, args.dry_run)
        return 0 if result == "@back" else result
    names = list(presets)
    if args.list:
        for index, name in enumerate(names, 1):
            model = models[presets[name]["model"]]
            print(f"{index:2}. {name} | {model['instance']} | {model['path'].name}")
        return 0
    selected = args.preset
    while selected is None:
        selected = select_preset(presets, models, instances)
        if selected is None:
            return 0
        if selected == "@back":
            return 0
        if selected == "@manage":
            result = manage_instances(instances, dry_run=args.dry_run)
            if result != "@back":
                return result
            selected = None
            continue
        elif selected.startswith("@update:"):
            result = manage_instances(instances, selected.removeprefix("@update:"), dry_run=args.dry_run)
            if result != "@back":
                return result
            selected = None
    if selected not in presets:
        raise ValueError(f"Unknown preset: {selected}")
    preset = presets[selected]
    model = models[preset["model"]]
    instance = instances[model["instance"]]
    command = launch_command(instance, model, preset)
    print("Working directory:", instance["folder"])
    print("Command:", format_invocation(command, model.get("env", {})), flush=True)
    if args.dry_run:
        return 0
    validate_launch(instance, model)
    processes = running_processes(instances)
    if processes:
        print("Configured servers already running:")
        for pid, _, command_line in processes:
            print(f"  PID {pid}: {command_line}")
        if not confirm("Stop these servers before launching?") or not stop_processes(processes):
            print("Launch cancelled.")
            return 1
    total, available = memory()
    used_percent = 100 * (1 - available / total)
    estimate = float(model["ram_gib"]) * 2**30 if "ram_gib" in model else model["path"].stat().st_size
    print(f"RAM: {used_percent:.1f}% used; {available / 2**30:.1f} GiB available; "
          f"model estimate {estimate / 2**30:.1f} GiB")
    warnings = []
    if used_percent > 50:
        warnings.append("RAM usage is above 50%")
    if estimate > available:
        warnings.append("estimated model RAM exceeds available RAM")
    if warnings and not confirm("; ".join(warnings) + ". Continue?"):
        print("Launch cancelled.")
        return 1
    if running_processes(instances):
        raise ValueError("A configured server started during checks; launch cancelled. Try again.")
    launch_env = os.environ.copy()
    launch_env.update(model.get("env", {}))
    sys.stdout.flush()
    sys.stderr.flush()
    return run_server(command, instance["folder"], launch_env, config_path=args.config)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, KeyError, OSError, configparser.Error, SyntaxError, ZeroDivisionError, curses.error, subprocess.CalledProcessError) as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)
    except (KeyboardInterrupt, EOFError):
        print("\nLaunch cancelled.", file=sys.stderr)
        sys.exit(130)
