# ⛏️ LlamaPick CLI Launcher

<img width="1121" height="896" alt="image" src="https://github.com/user-attachments/assets/5dceb2b7-b8a2-4d75-89e3-62d63e0fc1fb" />


LlamaPick CLI Launcher manages multiple llama.cpp forks, GGUF models, and flag
presets from your terminal. Pick a setup with the arrow keys, run its server
inside a log pane, and update or build each fork using its own commands.

Works on Linux and Windows 10/11 with **Python 3.9+** and no Python dependencies.
Keep `llama-launch.py` and `launcher_platform.py` in the same folder. For the
interactive UI, use a terminal with Unicode and truecolor support, at least
40 columns wide and 10 rows tall.

## Getting started

Run from the launcher folder:

**Linux**

```sh
python3 ./llama-launch.py
```

**Windows — PowerShell or cmd**

```powershell
py -3 .\llama-launch.py
```

On Windows, you can also run `llama-launch.cmd`. It uses `py -3`, falls back to
`python`, and forwards command-line arguments. WSL uses the Linux backend.

Both platforms default to **`llama.ini` beside the launcher**, regardless of the
working directory. If it is missing, the launcher creates a starter with
platform-specific defaults. Press **E** to set your actual fork folder, binary,
model path, and flags, then **R** to reload. Existing INIs are never overwritten,
including empty or invalid files.

For a fuller starting configuration, copy the appropriate example to `llama.ini`
before your first launch, then edit its paths and build settings:

- [exemplar_linux.llama.ini](exemplar_linux.llama.ini)
- [exemplar_windows.llama.ini](exemplar_windows.llama.ini)

Both contain regular and ik-ft forks, Qwen and Gemma models, multiple batch
presets, editor settings, and log filtering. Their paths are examples. The
launcher does not load example files automatically.

## Controls

### Preset selection and maintenance menus

| Key | Action |
| --- | --- |
| ↑ / ↓ | Select an entry |
| Enter | Launch the preset, select a fork, or run the highlighted maintenance action |
| U | Open update/build actions for the highlighted preset's fork |
| M | Choose from all configured forks for maintenance |
| V | Show/hide the full launch command; hidden by default |
| E | Open the active INI in your editor |
| R | Restart the launcher and reread its code and INI |
| ← | Return to the previous menu where `← back` is shown |
| Esc / q | Quit |

Each screen shows only the shortcuts available there. The main preset screen
has no back action. Command previews and shortcut lines wrap to fit the menu.
The list scrolls when there are more entries than fit on screen.

### Running server

| Key / mouse action | Action |
| --- | --- |
| ↑ / ↓, mouse wheel | Scroll output |
| Drag scrollbar | Move through retained output |
| Click scrollbar track | Jump to that position |
| Home / End | Go to oldest retained output / resume live following |
| PgUp / PgDn | Scroll one page |
| S | Stop the server and return to preset selection |
| C | Open the model/preset selection popup |
| R | Reload launcher code and configuration while keeping the server running |
| Esc / q | Stop the server and quit |

In the **C popup**, use ↑/↓ or the mouse wheel to select, then Enter to change.
Press ←, Esc, q, or C to cancel and keep the current server running. Logs continue
being collected while the popup is open.

Changing a model stops the current server, waits for shutdown, and starts the
selected preset through the normal process and RAM checks. Replacement paths
are checked before stopping the current server. Switching or pressing S
interrupts an active generation. This is a manual process switch; the launcher
provides no HTTP proxy or automatic request routing.

## INI configuration

A setup has three levels:

```text
instance → model → preset
fork/binary → GGUF/common flags → launch variation
```

Section names are unique IDs. `model.instance` refers to an instance ID, and
`preset.model` refers to a model ID. To use the same GGUF with two forks, define
two model sections with different IDs.

```ini
[settings]
editor = nano
filter_request_logs = true

[instance:regular]
folder = /home/user/LLM/llama.cpp
binary = build/bin/llama-server
build_dir = build
jobs = 12
configure_flags = -G Ninja -DCMAKE_BUILD_TYPE=Release -DGGML_CUDA=ON

[model:my-model]
instance = regular
path = /mnt/models/model.gguf
flags = --alias "my model" --host 127.0.0.1 --ctx-size $((32 * 1024)) -ngl 999
    --temp 0.6 --top-k 24 --top-p 0.95
# Optional host RAM estimate including context/cache, in GiB:
# ram_gib = 28
# Optional environment variables for this server:
# env = GGML_CUDA_LAUNCH_BLOCKING=1 GGML_CUDA_GRAPH_OPT=1

[preset:my-model-b2048]
model = my-model
flags = -b $((4 * 512)) -ub $((2 * 512))

[preset:my-model-b3072]
model = my-model
flags = -b $((6 * 512)) -ub $((3 * 512))
```

The launcher runs the instance's binary with `-m MODEL_PATH`, followed by
instance, model, and preset `flags`, in that order. Each fork receives its own
configured flags; the launcher does not translate options between forks.

### Paths, flags, and environment

- Relative instance `folder` paths resolve against the INI directory.
- Relative `binary` and model `path` values resolve against the instance folder.
- `source` defaults to the instance folder; relative values resolve there.
- `build_dir` defaults to `build`; relative values resolve against `source`.
- Paths support `~` and environment variables. Windows also supports `%VARIABLE%`.
- Folder/path values need no quotes, even with spaces. Quote individual arguments
  inside `flags` and command values. Indent INI continuation lines.
- Save the INI as UTF-8; a UTF-8 BOM is accepted.

Flags support integer expressions such as `$((4 * 512))`, with `+`, `-`, `*`,
and `//`. Commands run as argument lists. Shell substitutions, pipes, redirects,
and shell variable expansion are not executed in flags.

A model's `env` contains space-separated `KEY=VALUE` assignments. Quote an
assignment containing spaces, for example `env = "MY_VALUE=two words"`.
These values extend the environment of that server only; they are not applied
to update/build commands.

### Editor and log filtering

Set `[settings] editor` to a command such as `nano`, `notepad.exe`, or
`code --wait`. If omitted, the launcher tries `VISUAL`, then `EDITOR`, then
`xdg-open` on Linux or the file association on Windows.

The terminal is restored before opening the editor. Closing a terminal editor
returns to the menu and rereads the INI. For a desktop editor, save your changes
and press R in the launcher.

`filter_request_logs = true` hides `log_server_request` entries and their
continuation lines. The default when omitted is `false`; the examples enable it.
Filtering changes the display, so retained entries can be shown again by setting
it to `false`. In the server view, R applies the saved filter to retained and new
logs without restarting the server. The launcher's own monitoring chatter is
hidden independently of this setting.

## Server output and status

Combined stdout/stderr appears in a colored, full-width log pane. The pane
rewraps when the terminal is resized and retains up to 2,000 source lines.
The scrollbar supports clicks and dragging, including its adjacent right border.

The footer shows **LIVE** while following new output and **PAUSED** while
inspecting history. New logs do not pull a paused view to the bottom. End resumes
following. If old history is evicted, the viewport moves to the oldest remaining
line. Terminal control sequences from child output are stripped before display.

The bottom status bar shows Loading, Idle, Prompt, Generating, or Stopping,
along with prompt and generation throughput:

- **live**: measured during a request from `/slots` token counters, sampled at
  most once every two seconds while the server is busy.
- **last**: the most recent measurement parsed from server timing logs.
- **—**: no measurement is available.

Live measurements depend on the fork's slot endpoint and counters. Some forks
provide generation counters without prompt counters. If the endpoint is
unavailable or authentication fails, the launcher falls back to timing logs.
Rates can differ from the web UI because the launcher measures counter changes
between samples rather than that client's streamed request timings.

R in this view reloads saved launcher code and checks the INI while preserving
the server process, output pipe, history, and scroll position. Changed launch
flags, model paths, and environment variables take effect on the next launch.
A failed reload leaves the current server and view running and reports the error
in the footer. If the INI was deleted, reload recreates a starter.

If the server exits by itself, its final output and exit code remain visible
until Esc/q closes the view. With redirected input or output, the server runs
in foreground pass-through mode instead of the interactive wrapper.

## Update and build

Press U for the selected preset's fork, or M to choose any configured fork.
Use ← to return without running an action.

| Action | Command-line value | Steps |
| --- | --- | --- |
| Update only | `update` | Update source |
| Update + build all targets | `update-full` | Update, configure, build all default targets |
| Update + build server only | `update-server` | Update, configure, build server target |
| Build all targets | `build-full` | Configure, build all default targets |
| Build server only | `build-server` | Configure, build server target |

Defaults are `git pull --ff-only`, `cmake -S SOURCE -B BUILD`, and
`cmake --build BUILD --parallel JOBS`. Server-only builds add
`--target llama-server`; set `server_target` to use another target.
`configure_flags` are appended to the configure command.

Full builds are incremental builds of all default targets, not clean rebuilds.
Existing CMake caches remain in place. Changing a compiler or generator may
require choosing a different build directory. Update requires a clean working
tree, including untracked files, and a configured Git upstream. It does not
stash changes or reset the repository. A failed command stops the sequence;
an update that already succeeded remains applied if a later build fails.

Maintenance runs from `source` and checks only the selected instance's running
servers, asking before stopping them. In a terminal, its output uses the log
wrapper. The bottom bar shows Updating, Configuring, Building server, or Building
all targets. CMake/Make percentages and Ninja completed/total counts update build
progress when available. Esc/q cancels maintenance; R reloads the launcher
without restarting the active command. After successful maintenance in a
terminal, the launcher returns to preset selection so you can launch a model.
Dry runs and commands with redirected input/output exit after completing.

For custom fork commands, set these optional instance fields:

```ini
update_command = git pull --ff-only
configure_command = cmake -S "{source}" -B "{build}" -DCMAKE_BUILD_TYPE=Release
build_full_command = cmake --build "{build}" --parallel {jobs}
build_server_command = cmake --build "{build}" --parallel {jobs} --target llama-server
```

Each override replaces its corresponding default command. Placeholders
`{source}`, `{build}`, and `{jobs}` are expanded inside arguments. To run a shell
script on Linux, specify `bash /path/to/script.sh` explicitly.

`path_prefix` prepends directories to PATH for maintenance commands. Separate
multiple directories with `:` on Linux or `;` on Windows. Build tools must be
installed separately: Git, CMake, the selected build tool and C++ toolchain,
and CUDA when enabled. Set `-DGGML_CUDA=OFF` for a build without CUDA.

The Windows example uses Ninja and `build/bin/llama-server.exe`. With a Visual
Studio generator, set the binary to `build/bin/Release/llama-server.exe` and use
the corresponding build directory. Windows build defaults add `--config Release`;
set `build_config` to choose another configuration. Use a developer terminal
with the required toolchain on PATH. Custom command overrides must include any
configuration options they need.

## Command-line usage

These examples use preset and instance IDs from the supplied example INIs.
Replace them with the IDs in your `llama.ini`. On Windows, replace `python3` with
`py -3`, or pass the same arguments to `llama-launch.cmd`.

```sh
python3 ./llama-launch.py                                      # preset menu
python3 ./llama-launch.py --list                               # list presets
python3 ./llama-launch.py gemma4-b2048                          # launch preset
python3 ./llama-launch.py gemma4-b2048 --dry-run                # print command
python3 ./llama-launch.py --manage                             # fork menu
python3 ./llama-launch.py --instance ik-ft --action build-server
python3 ./llama-launch.py --instance regular --action update-full --dry-run
python3 ./llama-launch.py --config /path/to/another.ini --list
```

`--config` explicitly selects a different INI. Missing files and parent
directories are created, including with `--list` and `--dry-run`. A dry run
prints commands without checking that example paths exist, stopping processes,
or starting the server/build. Maintenance options cannot be combined with a
preset or `--list`.

## Launch checks and shutdown

Before starting a server, the launcher:

1. Validates the instance folder, server binary, and model file.
2. Checks for processes using any configured server binary, including other
   forks, and asks before stopping them. Declining cancels launch.
3. Checks host RAM after shutdown. It asks whether to continue if RAM usage is
   above 50% or the estimated model requirement exceeds available RAM.
4. Checks again for a configured server before launching the selected preset.

On Linux, available RAM uses `MemAvailable`, including reclaimable cache;
Windows uses the native available physical-memory value. The default model
estimate is GGUF file size. Set model `ram_gib` for an estimate including
context/cache. This remains a heuristic: GPU offloading and runtime buffers
change requirements. GPU VRAM is not checked.

External servers get a normal stop request, followed by a separate force-stop
confirmation if they remain after ten seconds. For a process owned by the
wrapper, stopping escalates after five seconds. Linux uses process groups;
Windows uses a process group and Job Object to clean up descendants.

Process detection requires permission to inspect executable paths. Inaccessible
processes and processes in other containers can escape detection. Use one
launcher at a time; the final recheck is not a lock against simultaneous launches.

## Verification

From the launcher folder on Linux:

```sh
python3 -m unittest -v test_launcher.py test_platform.py
```

On Windows:

```powershell
py -3 -m unittest -v test_platform.py
```

Native Windows checks are skipped on Linux. Interactive Windows verification
should also cover arrow selection, the editor, scrollbar dragging, model popup
cancel/switch, S returning to selection, resizing, and R during generation.
