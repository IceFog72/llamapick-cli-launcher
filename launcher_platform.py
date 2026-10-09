"""Native OS services for the launcher; standard library only."""
import ctypes as ct
import os
from pathlib import Path
import queue
import shlex
import shutil
import signal
import subprocess
import threading
import time

WINDOWS = os.name == "nt"
FORCE_KILL = getattr(signal, "SIGKILL", 9)


class Keys:
    # Stable values shared by Unix escape sequences and Windows console events.
    KEY_UP, KEY_DOWN, KEY_LEFT, KEY_RIGHT = 259, 258, 260, 261
    KEY_HOME, KEY_END, KEY_PPAGE, KEY_NPAGE = 262, 360, 339, 338
    class error(Exception):
        pass


def split_command(value, windows=None):
    """INI quoting, preserving Windows path backslashes and quoted arguments."""
    if not (WINDOWS if windows is None else windows):
        return shlex.split(value)
    arguments, token, quote, started, index = [], [], None, False, 0
    while index < len(value):
        char = value[index]
        if char.isspace() and quote is None:
            if started:
                arguments.append("".join(token))
                token, started = [], False
        elif char == "\\" and quote != "'":
            end = index
            while end < len(value) and value[end] == "\\":
                end += 1
            count = end - index
            if end < len(value) and value[end] == '"':
                token.extend("\\" * (count // 2))
                if count % 2:
                    token.append('"')
                else:
                    quote = None if quote == '"' else '"'
                index = end
            else:
                token.extend("\\" * count)
                index = end - 1
            started = True
        elif ((char == '"' and (quote is None or quote == char)) or
              (char == "'" and (quote == "'" or
               (quote is None and (not started or (token and token[-1] == "=")))))):
            quote = None if quote == char else char
            started = True
        else:
            token.append(char)
            started = True
        index += 1
    if quote is not None:
        raise ValueError("No closing quotation")
    if started:
        arguments.append("".join(token))
    return arguments


def command_text(command):
    return subprocess.list2cmdline(command) if WINDOWS else shlex.join(command)


def command_argv(command, environment=None):
    if WINDOWS:
        # CreateProcess does not use Popen's env PATH to find the executable.
        path = (os.environ if environment is None else environment).get("PATH", "")
        resolved = shutil.which(command[0], path=path)
        if resolved:
            return [resolved, *command[1:]]
    return command


DWORD, WORD, SHORT, BOOL = ct.c_uint32, ct.c_uint16, ct.c_int16, ct.c_int32
HANDLE = ct.c_void_p


class Coord(ct.Structure):
    _fields_ = [("X", SHORT), ("Y", SHORT)]


class Rect(ct.Structure):
    _fields_ = [(name, SHORT) for name in ("Left", "Top", "Right", "Bottom")]


class ScreenInfo(ct.Structure):
    _fields_ = [("size", Coord), ("cursor", Coord), ("attributes", WORD),
                ("window", Rect), ("maximum", Coord)]


class KeyEvent(ct.Structure):
    _fields_ = [("down", BOOL), ("repeat", WORD), ("virtual", WORD),
                ("scan", WORD), ("char", WORD), ("controls", DWORD)]


class MouseEvent(ct.Structure):
    _fields_ = [("position", Coord), ("buttons", DWORD), ("controls", DWORD), ("flags", DWORD)]


class EventData(ct.Union):
    _fields_ = [("key", KeyEvent), ("mouse", MouseEvent), ("padding", ct.c_byte * 16)]


class InputRecord(ct.Structure):
    _fields_ = [("kind", WORD), ("data", EventData)]


class ProcessEntry(ct.Structure):
    _fields_ = [("size", DWORD), ("usage", DWORD), ("pid", DWORD), ("heap", ct.c_size_t),
                ("module", DWORD), ("threads", DWORD), ("parent", DWORD),
                ("priority", ct.c_int32), ("flags", DWORD), ("name", WORD * 260)]


class MemoryStatus(ct.Structure):
    _fields_ = [("size", DWORD), ("load", DWORD)] + [
        (name, ct.c_uint64) for name in ("total", "available", "total_page", "available_page",
                                        "total_virtual", "available_virtual", "extended")]


class JobLimits(ct.Structure):
    _fields_ = [("process_time", ct.c_int64), ("job_time", ct.c_int64), ("flags", DWORD),
                ("minimum", ct.c_size_t), ("maximum", ct.c_size_t), ("active", DWORD),
                ("affinity", ct.c_size_t), ("priority", DWORD), ("scheduling", DWORD)]


class IoCounters(ct.Structure):
    _fields_ = [(name, ct.c_uint64) for name in ("reads", "writes", "others", "read_bytes",
                                               "write_bytes", "other_bytes")]


class ExtendedJobLimits(ct.Structure):
    _fields_ = [("basic", JobLimits), ("io", IoCounters)] + [
        (name, ct.c_size_t) for name in ("process_memory", "job_memory", "peak_process", "peak_job")]


_kernel = None


def kernel():
    global _kernel
    if _kernel is None:
        lib = ct.WinDLL("kernel32", use_last_error=True)
        signatures = {
            "GetStdHandle": ([DWORD], HANDLE),
            "GetConsoleMode": ([HANDLE, ct.POINTER(DWORD)], BOOL),
            "SetConsoleMode": ([HANDLE, DWORD], BOOL),
            "ReadConsoleInputW": ([HANDLE, ct.POINTER(InputRecord), DWORD, ct.POINTER(DWORD)], BOOL),
            "WaitForSingleObject": ([HANDLE, DWORD], DWORD),
            "GetConsoleScreenBufferInfo": ([HANDLE, ct.POINTER(ScreenInfo)], BOOL),
            "CreateToolhelp32Snapshot": ([DWORD, DWORD], HANDLE),
            "Process32FirstW": ([HANDLE, ct.POINTER(ProcessEntry)], BOOL),
            "Process32NextW": ([HANDLE, ct.POINTER(ProcessEntry)], BOOL),
            "OpenProcess": ([DWORD, BOOL, DWORD], HANDLE),
            "QueryFullProcessImageNameW": ([HANDLE, DWORD, ct.c_wchar_p, ct.POINTER(DWORD)], BOOL),
            "GetExitCodeProcess": ([HANDLE, ct.POINTER(DWORD)], BOOL),
            "GlobalMemoryStatusEx": ([ct.POINTER(MemoryStatus)], BOOL),
            "CreateJobObjectW": ([ct.c_void_p, ct.c_wchar_p], HANDLE),
            "SetInformationJobObject": ([HANDLE, ct.c_int, ct.c_void_p, DWORD], BOOL),
            "AssignProcessToJobObject": ([HANDLE, HANDLE], BOOL),
            "TerminateJobObject": ([HANDLE, DWORD], BOOL),
            "CloseHandle": ([HANDLE], BOOL),
        }
        for name, (args, result) in signatures.items():
            function = getattr(lib, name)
            function.argtypes, function.restype = args, result
        _kernel = lib
    return _kernel


def check(result):
    if not result:
        raise ct.WinError(ct.get_last_error())
    return result


class WindowsConsole:
    def __init__(self, mouse=False):
        self.api, self.mouse = kernel(), mouse
        self.input = self.api.GetStdHandle(DWORD(-10))
        self.output = self.api.GetStdHandle(DWORD(-11))
        self.input_mode, self.output_mode = DWORD(), DWORD()
        self.repeats = []
        self.buttons = 0

    def enter(self):
        check(self.api.GetConsoleMode(self.input, ct.byref(self.input_mode)))
        check(self.api.GetConsoleMode(self.output, ct.byref(self.output_mode)))
        # Native input events; disable Quick Edit so dragging cannot freeze output.
        mode = (self.input_mode.value | 0x80 | 0x08) & ~(0x01 | 0x02 | 0x04 | 0x40 | 0x200)
        mode = mode | 0x10 if self.mouse else mode & ~0x10
        try:
            check(self.api.SetConsoleMode(self.input, mode))
            check(self.api.SetConsoleMode(self.output, self.output_mode.value | 0x01 | 0x04))
        except BaseException:
            self.restore()
            raise

    def restore(self):
        self.api.SetConsoleMode(self.input, self.input_mode.value)
        self.api.SetConsoleMode(self.output, self.output_mode.value)

    def translate(self, record, left=0, top=0):
        if record.kind == 1:
            event = record.data.key
            if not event.down:
                return None
            keys = {0x26: Keys.KEY_UP, 0x28: Keys.KEY_DOWN, 0x25: Keys.KEY_LEFT, 0x27: Keys.KEY_RIGHT,
                    0x21: Keys.KEY_PPAGE, 0x22: Keys.KEY_NPAGE, 0x24: Keys.KEY_HOME, 0x23: Keys.KEY_END}
            key = keys.get(event.virtual, event.char or None)
            if key is not None:
                self.repeats.extend([key] * min(32, max(0, event.repeat - 1)))
            return key
        if record.kind == 2 and self.mouse:
            event = record.data.mouse
            x, y = event.position.X - left + 1, event.position.Y - top + 1
            if event.flags & 4:
                delta = ct.c_int16(event.buttons >> 16).value
                return ("mouse", 64 if delta > 0 else 65, x, y, False)
            previous, self.buttons = self.buttons, event.buttons & 0xffff
            if previous & 1 and not self.buttons & 1:
                return ("mouse", 0, x, y, True)
            if self.buttons & 1:
                return ("mouse", 32 if event.flags & 1 else 0, x, y, False)
        return None

    def read(self, timeout=.05):
        if self.repeats:
            return self.repeats.pop(0)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            wait = max(0, round((deadline - time.monotonic()) * 1000))
            if self.api.WaitForSingleObject(self.input, wait) == 258:
                return None
            record, count = InputRecord(), DWORD()
            check(self.api.ReadConsoleInputW(self.input, ct.byref(record), 1, ct.byref(count)))
            screen = ScreenInfo()
            check(self.api.GetConsoleScreenBufferInfo(self.output, ct.byref(screen)))
            key = self.translate(record, screen.window.Left, screen.window.Top)
            if key is not None:
                return key
        return None


def windows_executable(pid):
    api = kernel()
    handle = api.OpenProcess(0x1000, False, pid)
    if not handle:
        return None
    try:
        code = DWORD()
        if not api.GetExitCodeProcess(handle, ct.byref(code)) or code.value != 259:
            return None
        buffer, size = ct.create_unicode_buffer(32768), DWORD(32768)
        if api.QueryFullProcessImageNameW(handle, 0, buffer, ct.byref(size)):
            return Path(buffer.value).resolve()
        return None
    finally:
        api.CloseHandle(handle)


def windows_processes():
    api = kernel()
    snapshot = api.CreateToolhelp32Snapshot(2, 0)
    if snapshot == ct.c_void_p(-1).value:
        raise ct.WinError(ct.get_last_error())
    try:
        entry = ProcessEntry()
        entry.size = ct.sizeof(entry)
        exists = api.Process32FirstW(snapshot, ct.byref(entry))
        while exists:
            if entry.pid != os.getpid():
                executable = windows_executable(entry.pid)
                if executable is not None:
                    yield entry.pid, executable
            exists = api.Process32NextW(snapshot, ct.byref(entry))
    finally:
        api.CloseHandle(snapshot)


def windows_memory():
    status = MemoryStatus()
    status.size = ct.sizeof(status)
    check(kernel().GlobalMemoryStatusEx(ct.byref(status)))
    return status.total, status.available


def stop_external(pid, force=False):
    # External processes might not belong to our console or process group.
    command = ["taskkill", "/PID", str(pid), "/T"] + (["/F"] if force else [])
    subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


class WindowsJob:
    def __init__(self, process):
        self.api = kernel()
        self.handle = check(self.api.CreateJobObjectW(None, None))
        limits = ExtendedJobLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        try:
            check(self.api.SetInformationJobObject(self.handle, 9, ct.byref(limits), ct.sizeof(limits)))
            check(self.api.AssignProcessToJobObject(self.handle, int(process._handle)))
        except BaseException:
            self.close()
            raise

    def kill(self):
        if self.handle is not None:
            check(self.api.TerminateJobObject(self.handle, 1))

    def close(self):
        if self.handle is not None:
            self.api.CloseHandle(self.handle)
            self.handle = None


def start_process(command, cwd, environment, **streams):
    options = {"creationflags": 0x200} if WINDOWS else {"start_new_session": True}
    process = subprocess.Popen(command_argv(command, environment), cwd=cwd, env=environment, **streams, **options)
    try:
        job = WindowsJob(process) if WINDOWS else None
        return process, job
    except BaseException:
        process.kill()
        process.wait(timeout=2)
        if process.stdout is not None:
            process.stdout.close()
        raise


def signal_process(process, sig, job=None):
    try:
        if WINDOWS:
            if sig == FORCE_KILL:
                job.kill() if job is not None else process.kill()
            elif process.poll() is None:
                try:
                    process.send_signal(signal.CTRL_BREAK_EVENT)
                except OSError:
                    # A process with no attached console cannot receive Ctrl+Break.
                    process.terminate()
        else:
            os.killpg(process.pid, sig)
    except ProcessLookupError:
        pass


class PipeReader:
    """Blocking pipe read in a bounded worker, usable on Python 3.9 on Windows."""
    def __init__(self, pipe):
        self.pipe, self.queue, self.closed = pipe, queue.Queue(maxsize=64), threading.Event()
        self.thread = threading.Thread(target=self.read, daemon=True)
        self.thread.start()

    def read(self):
        try:
            while not self.closed.is_set():
                data = os.read(self.pipe.fileno(), 65536)
                self.offer(data)
                if not data:
                    return
        except (OSError, ValueError):
            self.offer(b"")

    def offer(self, data):
        while not self.closed.is_set():
            try:
                self.queue.put(data, timeout=.1)
                return
            except queue.Full:
                continue

    def available(self):
        for _ in range(16):
            try:
                yield self.queue.get_nowait()
            except queue.Empty:
                return

    def close(self):
        self.closed.set()
        self.thread.join(timeout=1)
