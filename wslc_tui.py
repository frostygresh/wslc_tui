# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Brian Frost
#
# This program is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later
# version.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. See the GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License along with
# this program (see the LICENSE file). If not, see <https://www.gnu.org/licenses/>.

"""Terminal UI for managing WSL containers with the ``wslc`` CLI.

Shows containers, images and volumes; creates, starts, stops or deletes
containers, updates or deletes images, and deletes volumes.

Keys:
    r  Refresh        a  Toggle all/running containers
    n  New container (pick an image, map ports, attach volumes)
    e  Edit the selected container (saves a modified copy; the original is kept)
    s  Start          x  Stop
    l  View logs of the selected container
    u  Update (re-pull) the selected image
    d  Delete the selected container, image or volume (depending on the tab)
    D  Show/hide the debug panel of wslc commands (H hides auto-refresh ones)
    q  Quit

Run with ``--debug`` to start with the debug panel open, and/or
``--debug-log FILE`` to append every wslc command and its result to a file.
"""

import argparse
import itertools
import json
import ntpath
import queue
import re
import shlex
import shutil
import subprocess
import threading
import time
from collections import deque
from collections.abc import Generator, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import ClassVar, TextIO

from rich.markup import escape
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.command import DiscoveryHit, Hit, Hits, Provider
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.suggester import SuggestFromList
from textual.widgets import (
    Button,
    Checkbox,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    Log,
    RichLog,
    Select,
    TabbedContent,
    TabPane,
)

WSLC = shutil.which("wslc") or "wslc"
APP_NAME = "wslc_tui"
REFRESH_SECONDS = 5.0
LOG_TAIL_LINES = 500
LOG_MAX_LINES = 10_000
DEBUG_MAX_ENTRIES = 2_000
# Containers created by this app remember their extra options in this label.
OPTIONS_LABEL = "wslc-tui.extra-options"
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
PORT_RE = re.compile(
    r"^(?:(?P<ip>\d{1,3}(?:\.\d{1,3}){3}):)?(?P<host>\d+):(?P<container>\d+)(?:/(?P<proto>tcp|udp))?$",
    re.IGNORECASE,
)


class WslcError(Exception):
    """Raised when a wslc command fails."""


def format_command(args: list[str] | tuple[str, ...]) -> str:
    """Render a wslc command line for display, masking environment variable values.

    Args:
        args: Arguments passed to the wslc command, excluding the executable.

    Returns:
        A displayable command line with environment values masked.
    """
    shown, mask_next = [], False
    for arg in args:
        if mask_next and "=" in arg:
            arg = arg.split("=", 1)[0] + "=***"
        elif arg.startswith(("--env=", "-e=")) and arg.count("=") >= 2:
            flag, rest = arg.split("=", 1)
            arg = f"{flag}={rest.split('=', 1)[0]}=***"
        mask_next = arg in ("--env", "-e")
        shown.append(arg)
    return subprocess.list2cmdline(["wslc", *shown])


@dataclass
class CommandEntry:
    """One line in the debug log."""

    text: str
    style: str = ""
    refresh: bool = False


class CommandTracer:
    """Records every wslc command for debug mode.

    Commands run on worker threads, so entries go into a thread-safe queue that
    the UI drains. Commands still running after a second get an extra
    "running" entry so slow or streaming commands show up immediately.
    """

    SLOW_SECONDS = 1.0

    def __init__(self) -> None:
        """Initialize the command tracer and its synchronized state.

        Returns:
            None.
        """
        self.entries: queue.Queue[CommandEntry] = queue.Queue()
        self.ids = itertools.count(1)
        self.lock = threading.Lock()
        self.local = threading.local()
        self.pending: dict[int, tuple[str, float, bool]] = {}
        self.announced: set[int] = set()
        self.log_file: TextIO | None = None
        self.total = 0
        self.failed = 0

    @contextmanager
    def label(self, name: str) -> Generator[None, None, None]:
        """Tag commands run by this thread inside the block (e.g. auto refresh).

        Args:
            name: Label applied to commands on the current thread within the context.

        Yields:
            None while the thread-local label is active; the previous label is restored on exit.
        """
        previous = getattr(self.local, "label", None)
        self.local.label = name
        try:
            yield
        finally:
            self.local.label = previous

    def write_file(self, text: str) -> None:
        """Append and flush a line to the debug log when one is open.

        Args:
            text: Line to append to the debug log.

        Returns:
            None.
        """
        if self.log_file:
            with self.lock:
                self.log_file.write(text + "\n")
                self.log_file.flush()

    def emit(self, text: str, style: str = "", refresh: bool = False) -> None:
        """Queue a timestamped debug entry and write it to the debug log.

        Args:
            text: Debug message to timestamp and queue.
            style: Rich style applied to the debug entry.
            refresh: Whether the entry belongs to an automatic refresh.

        Returns:
            None.
        """
        line = f"{datetime.now():%H:%M:%S.%f}"[:-3] + "  " + text
        self.entries.put(CommandEntry(line, style, refresh))
        self.write_file(line)

    def start(self, args: list[str] | tuple[str, ...]) -> int:
        """Start tracing a command and record its arguments and start time.

        Args:
            args: Arguments passed to the wslc command, excluding the executable.

        Returns:
            The identifier assigned to the command.
        """
        number = next(self.ids)
        refresh = getattr(self.local, "label", None) == "refresh"
        with self.lock:
            self.pending[number] = (format_command(args), time.monotonic(), refresh)
            self.total += 1
        return number

    def finish(self, number: int, code: int | None, error: str = "", note: str = "") -> None:
        """Finish tracing a command and emit its result.

        Args:
            number: Identifier returned when tracing the command started.
            code: Command exit code, or None if no exit code is available.
            error: Command error output used for a failed result.
            note: Optional status note for a command stopped intentionally.

        Returns:
            None.
        """
        with self.lock:
            command, started, refresh = self.pending.pop(number, ("wslc ?", time.monotonic(), False))
            self.announced.discard(number)
            failed = code != 0 and not note
            self.failed += failed
        elapsed = time.monotonic() - started
        duration = f"{elapsed * 1000:.0f} ms" if elapsed < 1 else f"{elapsed:.1f} s"
        if note:
            status, style = f"■ {note}", "yellow"
        elif failed:
            status, style = f"✗ exit {code}", "red"
        else:
            status, style = "✓ exit 0", "dim" if refresh else "green"
        tag = "  [refresh]" if refresh else ""
        detail = f"  — {error.strip().splitlines()[0]}" if failed and error.strip() else ""
        self.emit(f"#{number} {status} · {duration}  {command}{tag}{detail}", style, refresh)

    def announce_slow(self) -> None:
        """Emit a "running" line, once, for commands that have been going for a while.

        Returns:
            None.
        """
        now = time.monotonic()
        with self.lock:
            slow = [
                (n, command, refresh)
                for n, (command, started, refresh) in self.pending.items()
                if n not in self.announced and now - started >= self.SLOW_SECONDS
            ]
            self.announced.update(n for n, _, _ in slow)
        for n, command, refresh in slow:
            self.emit(f"#{n} … running  {command}", "cyan", refresh)


TRACER = CommandTracer()


def run_wslc(*args: str, timeout: float | None = 120) -> str:
    """Run a wslc command and return its stdout.

    Args:
        *args: Arguments passed to wslc.
        timeout: Seconds to wait before giving up, or None to wait forever.

    Returns:
        The command's standard output.

    Raises:
        WslcError: If wslc is missing, times out, or exits non-zero.
    """
    number = TRACER.start(args)
    try:
        result = subprocess.run(
            [WSLC, *args],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except FileNotFoundError as exc:
        TRACER.finish(number, None, "wslc was not found on PATH.")
        raise WslcError("wslc was not found on PATH.") from exc
    except subprocess.TimeoutExpired as exc:
        TRACER.finish(number, None, f"timed out after {timeout} s")
        raise WslcError(f"'wslc {' '.join(args)}' timed out.") from exc
    TRACER.finish(number, result.returncode, result.stderr or result.stdout)
    if result.returncode != 0:
        message = (result.stderr or result.stdout).strip()
        raise WslcError(message or f"wslc exited with code {result.returncode}")
    return result.stdout


def parse_json_lines(output: str) -> list[dict]:
    """Parse wslc ``--format json`` output, which emits one object per line.

    Args:
        output: JSON array or newline-delimited JSON output from wslc.

    Returns:
        The decoded list of JSON objects.
    """
    output = output.strip()
    if output.startswith("["):
        return json.loads(output)
    return [json.loads(line) for line in output.splitlines() if line.strip()]


def list_containers(show_all: bool) -> list[dict]:
    """Return containers, either all of them or only running ones.

    Args:
        show_all: Whether to include stopped containers.

    Returns:
        Container records, filtered according to show_all.
    """
    args = ["list", "--format", "json"]
    if show_all:
        args.append("--all")
    return parse_json_lines(run_wslc(*args))


def list_images() -> list[dict]:
    """Return locally available images.

    Returns:
        Locally available image records.
    """
    return parse_json_lines(run_wslc("images", "--format", "json"))


def list_volume_details() -> list[dict]:
    """Return existing volumes as ``wslc volume list`` objects, sorted by name.

    Returns:
        Named volume records sorted by name.
    """
    volumes = parse_json_lines(run_wslc("volume", "list", "--format", "json"))
    return sorted((v for v in volumes if v.get("Name")), key=lambda v: v["Name"])


def volume_created_times(names: list[str]) -> dict[str, str]:
    """Return each volume's local creation time, using one ``wslc volume inspect`` call.

    Args:
        names: Names of the volumes to inspect.

    Returns:
        A mapping from volume names to local creation times or original unparseable timestamps.
    """
    if not names:
        return {}
    data = json.loads(run_wslc("volume", "inspect", *names))
    created = {}
    for volume in data if isinstance(data, list) else [data]:
        stamp = volume.get("CreatedAt", "")
        try:
            iso_stamp = stamp[:-1] + "+00:00" if stamp.endswith("Z") else stamp
            stamp = datetime.fromisoformat(iso_stamp).astimezone().strftime("%Y-%m-%d %H:%M")
        except ValueError:
            pass
        created[volume.get("Name", "")] = stamp
    return created


def volume_users(containers: list[dict], volume_names: list[str]) -> dict[str, list[str]]:
    """Map each volume name to the names of the containers that mount it.

    The container list shortens long mount names to ``prefix…``, so those
    match every volume starting with the prefix.

    Args:
        containers: Container records returned by wslc.
        volume_names: Volume names to match against container mounts.

    Returns:
        A mapping from each requested volume name to the names of containers using it.
    """
    users: dict[str, list[str]] = {name: [] for name in volume_names}
    for container in containers:
        for mount in filter(None, (container.get("Mounts") or "").split(",")):
            if mount.endswith("…"):
                matches = [n for n in volume_names if n.startswith(mount[:-1])]
            else:
                matches = [mount] if mount in users else []
            for name in matches:
                if container.get("Names", "") not in users[name]:
                    users[name].append(container.get("Names", ""))
    return users


def normalize_port(spec: str) -> str:
    """Validate a ``[ip:]host:container[/tcp|udp]`` port mapping.

    Args:
        spec: Port mapping in [ip:]host:container[/tcp|udp] form.

    Returns:
        The mapping with surrounding whitespace removed and the protocol lower-cased.

    Raises:
        ValueError: If the mapping is malformed or a port is out of range.
    """
    spec = spec.strip()
    match = PORT_RE.match(spec)
    if not match:
        raise ValueError(f"Invalid port mapping '{spec}'. Use host:container, e.g. 8080:80.")
    for part in ("host", "container"):
        if not 1 <= int(match[part]) <= 65535:
            raise ValueError(f"Port {match[part]} is out of range (1-65535).")
    if match["ip"] and any(int(octet) > 255 for octet in match["ip"].split(".")):
        raise ValueError(f"Invalid IP address '{match['ip']}'.")
    return spec.lower()


# Options `wslc run` / `wslc create` accept, split by whether they take a value.
VALUE_OPTIONS = {
    "--cidfile", "--cpus", "--dns", "--dns-option", "--dns-search", "--domainname",
    "--entrypoint", "--env", "--env-file", "--gpus", "--health-cmd", "--health-interval",
    "--health-retries", "--health-start-period", "--health-timeout", "--hostname", "--ip",
    "--label", "--memory", "--mount", "--network", "--network-alias", "--publish", "--pull",
    "--shm-size", "--stop-signal", "--stop-timeout", "--tmpfs", "--ulimit", "--user",
    "--volume", "--workdir",
}
FLAG_OPTIONS = {"--interactive", "--tty", "--publish-all", "--no-healthcheck", "--rm"}
SHORT_OPTIONS = {
    "e": "--env", "h": "--hostname", "l": "--label", "m": "--memory", "p": "--publish",
    "u": "--user", "v": "--volume", "w": "--workdir",
    "i": "--interactive", "t": "--tty", "P": "--publish-all",
}
# Set by the form itself, so they can't be given as extra options.
RESERVED_OPTIONS = {
    "--name": "Use the Name field instead of --name.",
    "--detach": "Use the Start checkbox instead of --detach.",
    "--help": "--help isn't a container option.",
    "--session": "--session isn't supported here.",
}
RESERVED_SHORT = {"d": "--detach", "?": "--help"}


def split_options(text: str, field_name: str = "Extra options") -> list[str]:
    """Split a command line like a shell, but keep backslashes for Windows paths.

    Args:
        text: Command-line text to split while preserving backslashes.
        field_name: Field name included in a tokenization error.

    Returns:
        The tokenized command-line arguments.
    """
    lexer = shlex.shlex(text, posix=True)
    lexer.whitespace_split = True
    lexer.escape = ""
    lexer.commenters = ""
    try:
        return list(lexer)
    except ValueError as exc:
        raise ValueError(f"{field_name}: {exc}.") from None


def parse_extra_options(text: str) -> list[str]:
    """Check extra ``wslc run``/``create`` options and return them as arguments.

    Options must be known ``wslc`` container options. Anything that isn't an
    option would end up being taken as the image name, so it's rejected.
    ``--opt=value`` is split into ``--opt value``.

    Args:
        text: Extra wslc run or create options entered in the form.

    Returns:
        Validated arguments, with long option values split into separate tokens.

    Raises:
        ValueError: With a message suitable for showing in the form.
    """
    tokens = split_options(text)
    args: list[str] = []
    i = 0

    def take_value(option: str) -> None:
        """Consume the next token as the value for an option.

        Args:
            option: Option whose next token is consumed.

        Returns:
            None.
        """
        nonlocal i
        if i >= len(tokens):
            raise ValueError(f"{option} needs a value.")
        args.extend([option, tokens[i]])
        i += 1

    while i < len(tokens):
        token = tokens[i]
        i += 1
        if token.startswith("--"):
            option, has_value, value = token.partition("=")
            if option in RESERVED_OPTIONS:
                raise ValueError(RESERVED_OPTIONS[option])
            if option in FLAG_OPTIONS:
                if has_value:
                    raise ValueError(f"{option} doesn't take a value.")
                args.append(option)
            elif option in VALUE_OPTIONS:
                if has_value:
                    args.extend([option, value])
                else:
                    take_value(option)
            else:
                raise ValueError(f"Unknown option {option}. See 'wslc run --help'.")
        elif token.startswith("-") and len(token) > 1:
            letters = token[1:]
            for n, letter in enumerate(letters):
                if letter in RESERVED_SHORT:
                    raise ValueError(RESERVED_OPTIONS[RESERVED_SHORT[letter]])
                option = SHORT_OPTIONS.get(letter)
                if option is None:
                    raise ValueError(f"Unknown option -{letter}. See 'wslc run --help'.")
                if option in FLAG_OPTIONS:
                    args.append(option)
                elif n == len(letters) - 1:
                    take_value(option)
                else:
                    raise ValueError(f"Put a space between -{letter} and its value.")
        else:
            raise ValueError(
                f"'{token}' isn't an option. Extra options must start with - or --; "
                "put a command for the container in Command and arguments."
            )
    return args


def is_host_path(source: str) -> bool:
    """Return True when a volume source is a Windows folder rather than a volume name.

    Args:
        source: Volume name or host path to classify.

    Returns:
        True if the source resembles a Windows or other host path; otherwise False.
    """
    return bool(ntpath.splitdrive(source)[0]) or "\\" in source or "/" in source


def option_pairs(options: list[str]) -> Iterator[tuple[str, str | None]]:
    """Yield (option, value) pairs from arguments made by ``parse_extra_options``.

    Args:
        options: Parsed command-line options and their values.

    Yields:
        Pairs of option names and values, with None for flags.
    """
    i = 0
    while i < len(options):
        if options[i] in VALUE_OPTIONS and i + 1 < len(options):
            yield options[i], options[i + 1]
            i += 2
        else:
            yield options[i], None
            i += 1


def join_options(options: list[str]) -> str:
    """Turn arguments back into text for the Extra options field.

    Args:
        options: Parsed command-line options and their values.

    Returns:
        Command-line text representing the provided arguments.
    """
    parts = []
    for arg in options:
        if arg and not re.search(r"[\s'\"]", arg):
            parts.append(arg)
        elif '"' not in arg:
            parts.append(f'"{arg}"')
        else:
            parts.append(shlex.quote(arg))
    return " ".join(parts)


def options_for_label(options: list[str]) -> str:
    """Encode extra options for the container label, leaving out env values.

    Values stay in the container's environment; ``options_from_label`` puts
    them back. That keeps secrets out of the label and the debug panel.

    Args:
        options: Parsed command-line options and their values.

    Returns:
        A JSON string of options with environment values omitted.
    """
    stored = []
    for option, value in option_pairs(options):
        if option == "--env" and value is not None:
            value = value.split("=", 1)[0]
        stored += [option] if value is None else [option, value]
    return json.dumps(stored, separators=(",", ":"))


def options_from_label(labels: dict, env: list[str]) -> list[str]:
    """Recover the extra options a container was created with by this app.

    Args:
        labels: Container labels containing any saved extra options.
        env: Container environment entries used to restore environment values.

    Returns:
        The saved options with available environment values restored, or an empty list.
    """
    try:
        stored = json.loads((labels or {}).get(OPTIONS_LABEL) or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(stored, list) or not all(isinstance(a, str) for a in stored):
        return []
    values = dict(e.split("=", 1) if "=" in e else (e, "") for e in env)
    options = []
    for option, value in option_pairs(stored):
        if option == "--env" and value is not None and "=" not in value:
            if value not in values:
                continue
            value = f"{value}={values[value]}"
        options += [option] if value is None else [option, value]
    return options


def port_key(port: str) -> str:
    """Normalize a --publish value so equivalent mappings compare equal.

    Args:
        port: Published port mapping to process.

    Returns:
        A normalized port mapping for comparisons.
    """
    port = port.strip().lower().removesuffix("/tcp")
    for default_ip in ("127.0.0.1:", "0.0.0.0:"):
        port = port.removeprefix(default_ip)
    return port


def mount_target(option: str, value: str) -> str | None:
    """Return the container path of a --volume or --mount value.

    Args:
        option: The --volume or --mount option name.
        value: The option value containing a container target.

    Returns:
        The container mount path, or None if it cannot be extracted.
    """
    if option == "--volume":
        match = re.search(r":(/[^:]*)(?::(?:ro|rw))?$", value)
        return match[1] if match else None
    match = re.search(r"(?:^|,)(?:dst|destination|target)=([^,]+)", value)
    return match[1] if match else None


def published_ports(options: list[str]) -> list[str]:
    """Extract publish values from parsed extra options.

    Args:
        options: Parsed command-line options and their values.

    Returns:
        Nonempty values of --publish options.
    """
    return [v for o, v in option_pairs(options) if o == "--publish" and v]


def host_port(port: str) -> str:
    """Return the host side (``[ip:]port``) of a --publish value.

    Args:
        port: Published port mapping to process.

    Returns:
        The normalized host side of the port mapping.
    """
    return port_key(port).rsplit(":", 1)[0]


@dataclass
class VolumeMount:
    """A named volume or host folder mounted into a container."""

    source: str
    target: str
    read_only: bool = False

    @property
    def is_bind(self) -> bool:
        """Determine whether the source is a host path.

        Returns:
            True if this mount uses a host path; otherwise False.
        """
        return is_host_path(self.source)

    def arg(self) -> str:
        """Format this mount as a wslc volume argument.

        Returns:
            The source:target argument, with :ro for a read-only mount.
        """
        return f"{self.source}:{self.target}{':ro' if self.read_only else ''}"


@dataclass
class ContainerSpec:
    """Everything needed to create a container from the New container form.

    ``entrypoint`` through ``cpus`` aren't shown as editable fields; they're
    carried over unchanged when cloning an existing container.
    ``extra_options`` are added after everything else, so they win if they
    repeat one of those settings.
    """

    image: str
    name: str = ""
    ports: list[str] = field(default_factory=list)
    volumes: list[VolumeMount] = field(default_factory=list)
    new_volumes: list[str] = field(default_factory=list)
    start: bool = True
    entrypoint: str | None = None
    command: list[str] = field(default_factory=list)
    env: list[str] = field(default_factory=list)
    workdir: str = ""
    user: str = ""
    memory: int = 0
    cpus: float = 0.0
    extra_options: list[str] = field(default_factory=list)

    def args(self) -> list[str]:
        """Build the ``wslc run --detach`` or ``wslc create`` arguments.

        Returns:
            Arguments for wslc run --detach or wslc create, including the image and command.
        """
        args = ["run", "--detach"] if self.start else ["create"]
        if self.name:
            args += ["--name", self.name]
        for port in self.ports:
            args += ["--publish", port]
        for volume in self.volumes:
            args += ["--volume", volume.arg()]
        for variable in self.env:
            args += ["--env", variable]
        if self.entrypoint is not None:
            args += ["--entrypoint", self.entrypoint]
        if self.workdir:
            args += ["--workdir", self.workdir]
        if self.user:
            args += ["--user", self.user]
        if self.memory:
            args += ["--memory", str(self.memory)]
        if self.cpus:
            args += ["--cpus", f"{self.cpus:g}"]
        args += self.extra_options
        if self.extra_options:
            args += ["--label", f"{OPTIONS_LABEL}={options_for_label(self.extra_options)}"]
        args.append(self.image)
        return [*args, *self.command]

    def carried_over(self) -> list[str]:
        """Describe the hidden settings that will be copied, for display.

        Returns:
            Descriptions of the hidden settings carried over into a copy.
        """
        details = []
        if self.entrypoint is not None:
            details.append(f"entrypoint {self.entrypoint}")
        if self.env:
            details.append(f"{len(self.env)} env var(s)")
        if self.workdir:
            details.append(f"workdir {self.workdir}")
        if self.user:
            details.append(f"user {self.user}")
        if self.memory:
            details.append(f"memory {self.memory // (1024 * 1024)}M")
        if self.cpus:
            details.append(f"{self.cpus:g} CPUs")
        return details


def inspect_object(object_id: str, kind: str | None = None) -> dict:
    """Return the ``wslc inspect`` data for one container, image or volume.

    Args:
        object_id: Identifier of the container, image, or volume to inspect.
        kind: Object type to inspect, or None to let wslc determine it.

    Returns:
        The inspected object's data as a dictionary.
    """
    args = ["inspect", *(["--type", kind] if kind else []), object_id]
    data = json.loads(run_wslc(*args))
    return data[0] if isinstance(data, list) else data


def port_from_inspect(container_port: str, binding: dict) -> str:
    """Turn an inspect ``Ports`` entry back into a ``--publish`` value.

    Args:
        container_port: Container port and protocol from inspect output.
        binding: Host binding from inspect output.

    Returns:
        The corresponding --publish value.
    """
    port, _, proto = container_port.partition("/")
    host_ip = binding.get("HostIp", "")
    # wslc binds to 127.0.0.1 by default, so leave it implicit.
    prefix = f"{host_ip}:" if host_ip and host_ip not in ("127.0.0.1", "0.0.0.0") else ""
    suffix = f"/{proto}" if proto and proto != "tcp" else ""
    return f"{prefix}{binding.get('HostPort', '')}:{port}{suffix}"


def spec_from_inspect(info: dict, image_info: dict | None) -> ContainerSpec:
    """Build a ContainerSpec that recreates the inspected container.

    Settings that match the image's defaults are left out so the clone keeps
    following the image (for example, the image's PATH and default command).

    Args:
        info: Container inspect data.
        image_info: Image inspect data, or None if unavailable.

    Returns:
        A specification containing the inspected settings needed to recreate the container.
    """
    config = info.get("Config") or {}
    image_config = (image_info or {}).get("Config") or {}
    host = info.get("HostConfig") or {}

    ports = [
        port_from_inspect(container_port, binding)
        for container_port, bindings in (info.get("Ports") or {}).items()
        for binding in bindings or []
        if binding.get("HostPort")
    ]
    volumes = [
        VolumeMount(
            m.get("Name") or m.get("Source", "") if m.get("Type") == "volume" else m.get("Source", ""),
            m.get("Destination", ""),
            read_only=not m.get("ReadWrite", True),
        )
        for m in info.get("Mounts") or []
        if m.get("Type") in ("volume", "bind") and m.get("Destination")
    ]

    entrypoint_list = config.get("Entrypoint") or []
    command = config.get("Cmd") or []
    entrypoint = None
    if entrypoint_list != (image_config.get("Entrypoint") or []):
        # --entrypoint takes one executable; the rest becomes part of the command.
        entrypoint = entrypoint_list[0] if entrypoint_list else ""
        command = [*entrypoint_list[1:], *command]
    elif command == (image_config.get("Cmd") or []):
        command = []

    image_env = set(image_config.get("Env") or [])
    spec = ContainerSpec(
        image="",
        ports=ports,
        volumes=volumes,
        entrypoint=entrypoint,
        command=list(command),
        env=[e for e in config.get("Env") or [] if e not in image_env],
        workdir=config.get("WorkingDir", "") if config.get("WorkingDir", "") != image_config.get("WorkingDir", "") else "",
        user=config.get("User", "") if config.get("User", "") != image_config.get("User", "") else "",
        memory=int(host.get("Memory") or 0),
        cpus=(host.get("NanoCpus") or 0) / 1e9,
    )
    return with_extra_options(spec, options_from_label(config.get("Labels"), config.get("Env") or []))


def with_extra_options(spec: ContainerSpec, options: list[str]) -> ContainerSpec:
    """Attach extra options to a cloned spec, dropping settings they already cover.

    Inspect reports the effect of every option (a ``-p`` shows up in Ports, a
    ``--memory`` in the memory limit, …). Without this, editing would show
    those settings twice and pass them to wslc twice.

    Args:
        spec: Container specification to process.
        options: Parsed command-line options and their values.

    Returns:
        The original specification if options are empty, otherwise a copy with duplicate settings removed.
    """
    if not options:
        return spec
    pairs = [(o, v or "") for o, v in option_pairs(options)]
    given = {o for o, _ in pairs}
    publish = {port_key(v) for o, v in pairs if o == "--publish"}
    container_only = {v for v in publish if ":" not in v}
    targets = {mount_target(o, v) for o, v in pairs if o in ("--volume", "--mount")}
    env_names = {v.split("=", 1)[0] for o, v in pairs if o == "--env"}
    return replace(
        spec,
        extra_options=options,
        ports=[
            p for p in spec.ports
            if port_key(p) not in publish and port_key(p).rsplit(":", 1)[-1] not in container_only
        ],
        volumes=[v for v in spec.volumes if v.target not in targets],
        env=[e for e in spec.env if e.split("=", 1)[0] not in env_names],
        entrypoint=None if "--entrypoint" in given else spec.entrypoint,
        workdir="" if "--workdir" in given else spec.workdir,
        user="" if "--user" in given else spec.user,
        memory=0 if "--memory" in given else spec.memory,
        cpus=0.0 if "--cpus" in given else spec.cpus,
    )


def image_ref(image: dict) -> str | None:
    """Return ``repository:tag`` for an image, or None for untagged images.

    Args:
        image: Image record returned by wslc.

    Returns:
        The repository:tag reference, or None for an untagged image.
    """
    repo, tag = image.get("Repository", ""), image.get("Tag", "")
    if not repo or repo == "<none>" or not tag or tag == "<none>":
        return None
    return f"{repo}:{tag}"


def container_ports(container: dict) -> str:
    """Return a readable port list, falling back to wslc's port metadata label.

    Args:
        container: Container record returned by wslc.

    Returns:
        A readable port list, or an empty string when no ports can be found.
    """
    if container.get("Ports"):
        return container["Ports"]
    labels = container.get("Labels", "")
    key = "com.microsoft.wsl.container.metadata="
    start = labels.find(key)
    if start == -1:
        return ""
    try:
        metadata, _ = json.JSONDecoder().raw_decode(labels, start + len(key))
        ports = metadata["V1"]["Ports"]
    except (ValueError, KeyError, TypeError):
        return ""
    return ", ".join(
        f"{p.get('BindingAddress', '')}:{p['HostPort']}->{p['ContainerPort']}"
        for p in ports
    )


STATE_STYLES = {
    "running": "bold green",
    "exited": "red",
    "created": "yellow",
    "paused": "cyan",
}


class AboutScreen(ModalScreen[None]):
    """Show application and license information."""

    CSS = """
    AboutScreen { align: center middle; }
    #dialog {
        width: 64; height: auto; padding: 1 2;
        border: thick $primary; background: $surface;
    }
    #dialog Label { width: 100%; margin-bottom: 1; }
    #buttons { width: 100%; height: auto; align-horizontal: center; }
    """
    BINDINGS: ClassVar[list[Binding]] = [Binding("escape", "close", "Close")]

    def compose(self) -> ComposeResult:
        """Build the About dialog."""
        with Vertical(id="dialog"):
            yield Label(f"[b]{APP_NAME}[/b]")
            yield Label("A terminal UI for managing WSL containers with the wslc CLI.")
            yield Label("Licensed under the GNU GPL v3 or later.")
            with Horizontal(id="buttons"):
                yield Button("Close", id="close")

    @on(Button.Pressed, "#close")
    def close(self) -> None:
        """Dismiss the About dialog."""
        self.dismiss(None)

    def action_close(self) -> None:
        """Dismiss the About dialog."""
        self.dismiss(None)


class AboutCommandProvider(Provider):
    """Expose the About dialog in the header command palette."""

    async def search(self, query: str) -> Hits:
        matcher = self.matcher(query)
        score = matcher.match("About")
        if score > 0:
            yield Hit(
                score,
                matcher.highlight("About"),
                lambda: self.app.run_action("about"),
                help="Show application information",
            )

    async def discover(self) -> Hits:
        yield DiscoveryHit(
            "About",
            lambda: self.app.run_action("about"),
            help="Show application information",
        )


class ConfirmDeleteScreen(ModalScreen[str | None]):
    """Ask the user to confirm deleting a container, image or volume.

    Dismisses with ``"delete"``, ``"force"``, or ``None`` when cancelled. The
    force button is only shown when a warning is given and ``can_force`` is set.
    """

    CSS = """
    ConfirmDeleteScreen { align: center middle; }
    #dialog {
        width: 64; height: auto; padding: 1 2;
        border: thick $error; background: $surface;
    }
    #dialog Label { width: 100%; margin-bottom: 1; }
    #buttons { width: 100%; height: auto; align-horizontal: center; }
    #buttons Button { margin: 0 1; }
    """
    BINDINGS: ClassVar[list[Binding]] = [Binding("escape", "cancel", "Cancel")]

    def __init__(
        self, kind: str, target: str, warning: str | None = None, can_force: bool = True
    ) -> None:
        """Initialize the deletion confirmation dialog.

        Args:
            kind: Kind of item being deleted.
            target: Display name of the item being deleted.
            warning: Optional warning displayed in the confirmation dialog.
            can_force: Whether to offer force deletion when a warning is present.

        Returns:
            None.
        """
        super().__init__()
        self.kind = kind
        self.target = target
        self.warning = warning
        self.can_force = can_force

    def compose(self) -> ComposeResult:
        """Build the deletion confirmation dialog widgets.

        Yields:
            Widgets making up this screen or application.
        """
        with Vertical(id="dialog"):
            yield Label(f"Delete {self.kind} [b]{self.target}[/b]?")
            if self.warning:
                yield Label(f"[yellow]{self.warning}[/]")
            with Horizontal(id="buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Delete", id="delete", variant="error")
                if self.warning and self.can_force:
                    yield Button("Force delete", id="force", variant="warning")

    def on_mount(self) -> None:
        """Focus the cancel button when the dialog is mounted.

        Returns:
            None.
        """
        self.query_one("#cancel", Button).focus()

    @on(Button.Pressed)
    def choose(self, event: Button.Pressed) -> None:
        """Dismiss the dialog with the selected deletion action.

        Args:
            event: Textual button event that triggered the handler.

        Returns:
            None.
        """
        self.dismiss(None if event.button.id == "cancel" else event.button.id)

    def action_cancel(self) -> None:
        """Dismiss the dialog without deleting.

        Returns:
            None.
        """
        self.dismiss(None)


class EntryRow(Horizontal):
    """A removable line in the ports or volumes list of the New container form."""

    DEFAULT_CSS = """
    EntryRow { height: auto; }
    EntryRow Label { width: 1fr; padding: 0 1; }
    """

    def __init__(self, text: str, value: object) -> None:
        """Initialize a removable form entry.

        Args:
            text: Label text shown for the entry.
            value: Underlying value stored for the entry.

        Returns:
            None.
        """
        super().__init__()
        self.text = text
        self.value = value

    def compose(self) -> ComposeResult:
        """Build the entry label and remove button.

        Yields:
            Widgets making up this screen or application.
        """
        yield Label(self.text)
        yield Button("Remove", classes="remove", compact=True, variant="error")

    @on(Button.Pressed, ".remove")
    def remove_row(self, event: Button.Pressed) -> None:
        """Stop the button event and remove this entry from the form.

        Args:
            event: Textual button event that triggered the handler.

        Returns:
            None.
        """
        event.stop()
        self.remove()


class CreateContainerScreen(ModalScreen[ContainerSpec | None]):
    """Form for creating a container from an existing image."""

    CSS = """
    CreateContainerScreen { align: center middle; }
    #form {
        width: 96; height: auto; max-height: 90%; padding: 1 2;
        border: thick $primary; background: $surface;
    }
    #form > Label { margin-top: 1; }
    #form .hint { color: $text-muted; }
    .add-row { height: auto; }
    #port { width: 1fr; }
    #vol-source { width: 2fr; }
    #vol-target { width: 1fr; }
    .list { height: auto; }
    #error { color: $error; margin-top: 1; }
    #buttons { height: auto; align-horizontal: center; margin-top: 1; }
    #buttons Button { margin: 0 1; }
    """
    BINDINGS: ClassVar[list[Binding]] = [Binding("escape", "cancel", "Cancel")]

    def __init__(
        self,
        images: list[tuple[str, str]],
        volumes: list[str],
        container_names: set[str],
        selected_image: str | None = None,
        template: ContainerSpec | None = None,
        source_name: str | None = None,
        source_running: bool = False,
    ) -> None:
        """Initialize the container creation or copy form.

        Args:
            images: (label, value) pairs for the image picker.
            volumes: Names of existing volumes, used for suggestions.
            container_names: Names already in use, to catch duplicates early.
            selected_image: Image to preselect, if any.
            template: Settings of a container being edited. When given, the
                form pre-fills from it, locks the image, and creates a copy.
            source_name: Name of the container being edited.
            source_running: Whether that container is running, to warn about
                host port clashes.

        Returns:
            None.
        """
        super().__init__()
        self.images = images
        self.volumes = volumes
        self.container_names = container_names
        self.selected_image = template.image if template else selected_image
        self.template = template
        self.source_name = source_name
        self.source_running = source_running

    def compose(self) -> ComposeResult:
        """Build the container creation or copy form widgets.

        Yields:
            Widgets making up this screen or application.
        """
        if self.template and self.selected_image not in {v for _, v in self.images}:
            self.images = [(self.selected_image, self.selected_image), *self.images]
        values = {value for _, value in self.images}
        with VerticalScroll(id="form"):
            if self.template:
                yield Label(f"[b]Edit container {self.source_name}[/b]")
                yield Label(
                    "Saving creates a new container with these settings; "
                    f"{self.source_name} is left unchanged.",
                    classes="hint",
                )
            else:
                yield Label("[b]Create container[/b]")
            yield Label("Image" + (" [dim](fixed)[/]" if self.template else ""))
            yield Select(
                self.images,
                id="image",
                prompt="Select an image",
                value=self.selected_image if self.selected_image in values else Select.NULL,
                disabled=self.template is not None,
            )
            yield Label("Name [dim](optional)[/]")
            yield Input(id="name", placeholder="my-container", value=self.default_name())
            yield Label("Ports")
            yield Label("host:container, optionally ip:host:container/udp — e.g. 8080:80", classes="hint")
            if self.template and self.source_running and self.source_host_ports():
                yield Label(
                    f"[yellow]{self.source_name} is running and already uses host port(s) "
                    f"{', '.join(sorted(self.source_host_ports()))}; change them or untick Start.[/]"
                )
            with Horizontal(classes="add-row"):
                yield Input(id="port", placeholder="8080:80")
                yield Button("Add port", id="add-port")
            yield Vertical(
                *(EntryRow(p, p) for p in (self.template.ports if self.template else [])),
                id="ports-list",
                classes="list",
            )
            yield Label("Volumes")
            yield Label(
                "Source: a volume name (new ones are created) or a Windows folder path. "
                "Target: an absolute path in the container."
                + (" Named volumes are shared with the original container." if self.template else ""),
                classes="hint",
            )
            with Horizontal(classes="add-row"):
                yield Input(
                    id="vol-source",
                    placeholder="my-data or C:\\path\\to\\folder",
                    suggester=SuggestFromList(self.volumes, case_sensitive=False),
                )
                yield Input(id="vol-target", placeholder="/data")
                yield Checkbox("Read-only", id="vol-ro")
                yield Button("Add volume", id="add-volume")
            yield Vertical(
                *(
                    EntryRow(self.volume_label(v), v)
                    for v in (self.template.volumes if self.template else [])
                ),
                id="volumes-list",
                classes="list",
            )
            if self.template and (details := self.template.carried_over()):
                yield Label("Carried over unchanged", classes="hint")
                yield Label("[dim]" + "; ".join(details) + "[/]", classes="hint")
            yield Label("Extra options [dim](optional)[/]")
            if not self.template:
                hint = "Any other 'wslc run' options, e.g. --rm -e TZ=UTC --label app=web --network mynet"
            elif self.template.extra_options:
                hint = (
                    f"The options {self.source_name} was created with; edit them freely. "
                    "These override the carried-over settings above."
                )
            else:
                hint = (
                    f"No saved options for {self.source_name} (only containers created with "
                    "extra options in this app record them). Options added here override the "
                    "carried-over settings above."
                )
            yield Label(hint, classes="hint")
            yield Input(
                id="options",
                placeholder="--rm --hostname web -e KEY=value",
                value=join_options(self.template.extra_options) if self.template else "",
            )
            yield Label("Command and arguments [dim](optional)[/]")
            yield Label(
                "Added after the image name, e.g. server local. "
                + (
                    f"Pre-filled with the command {self.source_name} runs; clear it to use the image's default."
                    if self.template
                    else "Leave empty to use the image's default command."
                ),
                classes="hint",
            )
            yield Input(
                id="command",
                placeholder="server local",
                value=join_options(self.template.command) if self.template else "",
            )
            yield Checkbox(
                "Start the container after creating it",
                not (self.template and self.source_running and self.source_host_ports()),
                id="start",
            )
            yield Label("", id="error")
            with Horizontal(id="buttons"):
                yield Button("Cancel", id="cancel")
                yield Button(
                    "Save as new container" if self.template else "Create",
                    id="create",
                    variant="primary",
                )

    def source_host_ports(self) -> set[str]:
        """Host ports used by the container being edited (from Ports and extra -p options).

        Returns:
            Host endpoints used by the source container, or an empty set.
        """
        if not self.template:
            return set()
        ports = [*self.template.ports, *published_ports(self.template.extra_options)]
        return {host_port(p) for p in ports if ":" in port_key(p)}

    def default_name(self) -> str:
        """Suggest an unused ``<name>-copy`` name when editing.

        Returns:
            An unused copy name, or an empty string when not editing.
        """
        if not self.template or not self.source_name:
            return ""
        candidate, n = f"{self.source_name}-copy", 2
        while candidate in self.container_names:
            candidate, n = f"{self.source_name}-copy{n}", n + 1
        return candidate

    def volume_label(self, volume: VolumeMount) -> str:
        """Format a mount for display in the volume list.

        Args:
            volume: Volume mount to format.

        Returns:
            A display label including mount kind and read-only status.
        """
        if volume.is_bind:
            kind = "folder"
        else:
            kind = "volume" if volume.source in self.volumes else "new volume"
        return f"{volume.source} → {volume.target}  [dim]({kind}{', read-only' if volume.read_only else ''})[/]"

    def on_mount(self) -> None:
        """Focus the image or name field when the form opens.

        Returns:
            None.
        """
        self.query_one("#image" if self.selected_image is None else "#name").focus()

    def show_error(self, message: str) -> None:
        """Display a validation message in the form.

        Args:
            message: Validation message to display.

        Returns:
            None.
        """
        self.query_one("#error", Label).update(message)

    def entries(self, list_id: str) -> list:
        """Read the values of the entry rows in a form list.

        Args:
            list_id: Selector identifying the list of form entries.

        Returns:
            The stored values of the entry rows in the selected list.
        """
        return [row.value for row in self.query_one(list_id).query(EntryRow)]

    def add_port(self) -> bool:
        """Add the typed port mapping to the list. Returns False if it's invalid.

        Returns:
            True if the input is empty or the port was added; False if invalid.
        """
        port_input = self.query_one("#port", Input)
        if not port_input.value.strip():
            return True
        try:
            port = normalize_port(port_input.value)
        except ValueError as exc:
            self.show_error(str(exc))
            return False
        host_side = port.rsplit(":", 1)[0]
        if any(p.rsplit(":", 1)[0] == host_side for p in self.entries("#ports-list")):
            self.show_error(f"Host port {host_side} is already mapped.")
            return False
        self.query_one("#ports-list").mount(EntryRow(port, port))
        port_input.value = ""
        self.show_error("")
        return True

    def add_volume(self) -> bool:
        """Add the typed volume mount to the list. Returns False if it's invalid.

        Returns:
            True if the input is empty or the volume was added; False if invalid.
        """
        source_input = self.query_one("#vol-source", Input)
        target_input = self.query_one("#vol-target", Input)
        source, target = source_input.value.strip(), target_input.value.strip()
        if not source and not target:
            return True
        if not source or not target:
            self.show_error("A volume needs both a source and a container path.")
            return False
        if not target.startswith("/"):
            self.show_error("The container path must be absolute, e.g. /data.")
            return False
        if target in {v.target for v in self.entries("#volumes-list")}:
            self.show_error(f"{target} is already mounted.")
            return False
        volume = VolumeMount(source, target, self.query_one("#vol-ro", Checkbox).value)
        if volume.is_bind:
            if not ntpath.isabs(source):
                self.show_error("Folder paths must be absolute, e.g. C:\\data.")
                return False
            if not Path(source).exists():
                self.show_error(f"Folder not found: {source}")
                return False
        elif not NAME_RE.match(source):
            self.show_error(
                "Volume names may only contain letters, digits, '_', '.' and '-'."
            )
            return False
        self.query_one("#volumes-list").mount(EntryRow(self.volume_label(volume), volume))
        source_input.value = target_input.value = ""
        self.query_one("#vol-ro", Checkbox).value = False
        self.show_error("")
        return True

    @on(Button.Pressed, "#add-port")
    @on(Input.Submitted, "#port")
    def on_add_port(self) -> None:
        """Add the port mapping and refocus its input on success.

        Returns:
            None.
        """
        if self.add_port():
            self.query_one("#port", Input).focus()

    @on(Button.Pressed, "#add-volume")
    @on(Input.Submitted, "#vol-target")
    def on_add_volume(self) -> None:
        """Add the volume mount and refocus its source on success.

        Returns:
            None.
        """
        if self.add_volume():
            self.query_one("#vol-source", Input).focus()

    @on(Input.Submitted, "#vol-source")
    def on_volume_source_submitted(self) -> None:
        """Focus the mount target after a source is submitted.

        Returns:
            None.
        """
        self.query_one("#vol-target", Input).focus()

    @on(Button.Pressed, "#create")
    @on(Input.Submitted, "#name")
    @on(Input.Submitted, "#options")
    @on(Input.Submitted, "#command")
    def on_create(self) -> None:
        """Validate the form and dismiss it with a container specification.

        Returns:
            None.
        """
        image = self.query_one("#image", Select).value
        if image is Select.NULL:
            self.show_error("Select an image.")
            return
        name = self.query_one("#name", Input).value.strip()
        if name and not NAME_RE.match(name):
            self.show_error("Names may only contain letters, digits, '_', '.' and '-'.")
            return
        if name in self.container_names:
            self.show_error(f"A container named '{name}' already exists.")
            return
        # Include anything typed but not yet added, so it isn't silently dropped.
        if not self.add_port() or not self.add_volume():
            return
        try:
            extra_options = parse_extra_options(self.query_one("#options", Input).value)
        except ValueError as exc:
            self.show_error(str(exc))
            self.query_one("#options", Input).focus()
            return
        try:
            command = split_options(self.query_one("#command", Input).value, "Command")
        except ValueError as exc:
            self.show_error(str(exc))
            self.query_one("#command", Input).focus()
            return
        ports = self.entries("#ports-list")
        start = self.query_one("#start", Checkbox).value
        if self.template and self.source_running and start:
            new_ports = [*ports, *published_ports(extra_options)]
            clashes = {host_port(p) for p in new_ports if ":" in port_key(p)} & self.source_host_ports()
            if clashes:
                self.show_error(
                    f"Host port(s) {', '.join(sorted(clashes))} are in use by {self.source_name}. "
                    "Change them or untick Start."
                )
                return
        volumes = self.entries("#volumes-list")
        new_volumes = list(dict.fromkeys(
            v.source for v in volumes if not v.is_bind and v.source not in self.volumes
        ))
        self.dismiss(
            replace(
                self.template or ContainerSpec(image=""),
                image=str(image),
                name=name,
                ports=ports,
                volumes=volumes,
                new_volumes=new_volumes,
                start=start,
                extra_options=extra_options,
                command=command,
            )
        )

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        """Dismiss the creation form without submitting.

        Returns:
            None.
        """
        self.dismiss(None)


class LogsScreen(ModalScreen[None]):
    """Show (and optionally follow) a container's logs."""

    CSS = """
    LogsScreen { align: center middle; }
    #logs-box {
        width: 96%; height: 92%;
        border: thick $primary; background: $surface;
    }
    #logs-status { width: 100%; padding: 0 1; background: $panel; }
    #logs { height: 1fr; }
    """
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("escape,q", "close", "Close"),
        Binding("f", "toggle_follow", "Follow on/off"),
        Binding("t", "toggle_timestamps", "Timestamps"),
        Binding("a", "toggle_all", "All/Tail"),
        Binding("r", "reload", "Reload"),
        Binding("end", "scroll_end", "Bottom", show=False),
        Binding("home", "scroll_home", "Top", show=False),
    ]

    def __init__(self, cid: str, name: str, follow: bool) -> None:
        """Initialize the log viewer for a container.

        Args:
            cid: Container identifier.
            name: Container name shown in the viewer.
            follow: Whether to follow live container log output.

        Returns:
            None.
        """
        super().__init__()
        self.cid = cid
        self.container_name = name
        self.follow = follow
        self.timestamps = False
        self.show_all = False
        self.lines: queue.Queue[tuple[int, str | None]] = queue.Queue()
        self.generation = 0
        self.process: subprocess.Popen[str] | None = None
        self.state = "loading"

    def compose(self) -> ComposeResult:
        """Build the log viewer widgets.

        Yields:
            Widgets making up this screen or application.
        """
        with Vertical(id="logs-box"):
            yield Label(id="logs-status")
            yield Log(id="logs", max_lines=LOG_MAX_LINES)
            yield Footer()

    def on_mount(self) -> None:
        """Start draining and loading logs, then focus the log widget.

        Returns:
            None.
        """
        self.set_interval(0.1, self.drain)
        self.action_reload()
        self.query_one("#logs", Log).focus()

    def on_unmount(self) -> None:
        """Stop the log stream when the viewer is removed.

        Returns:
            None.
        """
        self.stop_stream()

    def update_status(self) -> None:
        """Update the log viewer status with the current settings and state.

        Returns:
            None.
        """
        parts = [
            f"[b]Logs: {self.container_name}[/b] ({self.cid})",
            "following" if self.follow else "snapshot",
            "all lines" if self.show_all else f"last {LOG_TAIL_LINES} lines",
            "timestamps on" if self.timestamps else "timestamps off",
            self.state,
        ]
        self.query_one("#logs-status", Label).update("  •  ".join(parts))

    def log_args(self) -> list[str]:
        """Build the wslc logs arguments for the current viewer settings.

        Returns:
            The wslc logs arguments, including the container ID.
        """
        args = ["logs"]
        if not self.show_all:
            args += ["--tail", str(LOG_TAIL_LINES)]
        if self.follow:
            args.append("--follow")
        if self.timestamps:
            args.append("--timestamps")
        return [*args, self.cid]

    def stop_stream(self) -> None:
        """Stop the current log process if it is still running.

        Returns:
            None.
        """
        process, self.process = self.process, None
        if process and process.poll() is None:
            process.kill()

    def action_reload(self) -> None:
        """Restart the log stream with the current options.

        Returns:
            None.
        """
        self.stop_stream()
        self.generation += 1
        self.query_one("#logs", Log).clear()
        self.state = "loading…"
        self.update_status()
        self.read_logs(self.generation, self.log_args())

    @work(thread=True, group="logs")
    def read_logs(self, generation: int, args: list[str]) -> None:
        """Read ``wslc logs`` output line by line into the queue.

        Args:
            generation: Log request generation used to discard stale output.
            args: Arguments passed to the wslc command, excluding the executable.

        Returns:
            None.
        """
        number = TRACER.start(args)
        try:
            process = subprocess.Popen(
                [WSLC, *args],
                stdout=subprocess.PIPE,
                # Containers write to both streams; show them together.
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            TRACER.finish(number, None, str(exc))
            self.lines.put((generation, f"[error] could not run wslc: {exc}"))
            self.lines.put((generation, None))
            return
        if generation != self.generation:
            process.kill()
            process.wait()
            TRACER.finish(number, None, note="stopped by log viewer")
            return
        self.process = process
        assert process.stdout is not None
        for line in process.stdout:
            self.lines.put((generation, line.rstrip("\r\n")))
        code = process.wait()
        stopped_by_viewer = self.process is not process
        TRACER.finish(number, code, note="stopped by log viewer" if stopped_by_viewer else "")
        if code not in (0, None) and generation == self.generation and not stopped_by_viewer:
            self.lines.put((generation, f"[wslc logs exited with code {code}]"))
        self.lines.put((generation, None))

    def drain(self) -> None:
        """Move queued lines into the log widget (runs on the UI thread).

        Returns:
            None.
        """
        batch: list[str] = []
        finished = False
        while True:
            try:
                generation, line = self.lines.get_nowait()
            except queue.Empty:
                break
            if generation != self.generation:
                continue
            if line is None:
                finished = True
            else:
                batch.append(line)
        log = self.query_one("#logs", Log)
        if batch:
            log.write_lines(batch)
        if finished:
            self.state = "end of logs" if log.line_count else "no log output"
            self.update_status()
        elif batch and self.state.startswith("loading"):
            self.state = "live" if self.follow else "loading…"
            self.update_status()

    def action_toggle_follow(self) -> None:
        """Toggle live following and reload the logs.

        Returns:
            None.
        """
        self.follow = not self.follow
        self.action_reload()

    def action_toggle_timestamps(self) -> None:
        """Toggle timestamps and reload the logs.

        Returns:
            None.
        """
        self.timestamps = not self.timestamps
        self.action_reload()

    def action_toggle_all(self) -> None:
        """Toggle all lines versus the configured tail and reload.

        Returns:
            None.
        """
        self.show_all = not self.show_all
        self.action_reload()

    def action_scroll_end(self) -> None:
        """Scroll the log widget to the bottom.

        Returns:
            None.
        """
        self.query_one("#logs", Log).scroll_end(animate=False)

    def action_scroll_home(self) -> None:
        """Scroll the log widget to the top.

        Returns:
            None.
        """
        self.query_one("#logs", Log).scroll_home(animate=False)

    def action_close(self) -> None:
        """Close the log viewer.

        Returns:
            None.
        """
        self.dismiss(None)


class WslcTui(App):
    """Textual app for browsing and controlling wslc containers."""

    COMMANDS = App.COMMANDS | {AboutCommandProvider}
    TITLE = APP_NAME
    CSS = """
    DataTable { height: 1fr; }
    #tabs { height: 1fr; }
    #debug {
        height: 12; display: none;
        border: round $warning; border-title-color: $warning;
        scrollbar-size-vertical: 1;
    }
    """
    BINDINGS: ClassVar[list[Binding]] = [
        Binding("r", "refresh", "Refresh"),
        Binding("n", "new_container", "New container"),
        Binding("e", "edit_container", "Edit (copy)"),
        Binding("a", "toggle_all", "All/Running"),
        Binding("s", "start", "Start"),
        Binding("x", "stop", "Stop"),
        Binding("u", "update_image", "Update image"),
        Binding("d", "delete", "Delete"),
        Binding("l", "logs", "Logs"),
        Binding("D", "toggle_debug", "Debug"),
        Binding("H", "toggle_debug_refresh", "Hide/show refresh"),
        Binding("ctrl+shift+a", "about", "About"),
        Binding("q", "quit", "Quit"),
    ]
    CONTAINER_ACTIONS: ClassVar[set[str]] = {
        "toggle_all",
        "start",
        "stop",
        "logs",
        "edit_container",
    }
    IMAGE_ACTIONS: ClassVar[set[str]] = {"update_image"}

    def __init__(self, debug: bool = False) -> None:
        """Initialize application state and debug panel visibility.

        Args:
            debug: Whether to show the debug panel initially.

        Returns:
            None.
        """
        super().__init__()
        self.show_all = True
        self.busy: set[str] = set()
        self.container_states: dict[str, str] = {}
        self.updating: set[str] = set()
        self.deleting: set[str] = set()
        self.images: list[dict] = []
        self.volumes: list[str] = []
        self.volume_created: dict[str, str] = {}
        self.volume_used_by: dict[str, list[str]] = {}
        self.deleting_volumes: set[str] = set()
        self.debug_visible = debug
        self.debug_hide_refresh = False
        self.debug_entries: deque[CommandEntry] = deque(maxlen=DEBUG_MAX_ENTRIES)

    def compose(self) -> ComposeResult:
        """Build the application header, tabs, debug panel, and footer.

        Yields:
            Widgets making up this screen or application.
        """
        yield Header()
        with TabbedContent(id="tabs"):
            with TabPane("Containers", id="containers-tab"):
                yield DataTable(id="containers", cursor_type="row", zebra_stripes=True)
            with TabPane("Images", id="images-tab"):
                yield DataTable(id="images", cursor_type="row", zebra_stripes=True)
            with TabPane("Volumes", id="volumes-tab"):
                yield DataTable(id="volume-table", cursor_type="row", zebra_stripes=True)
        yield RichLog(id="debug", markup=True, wrap=False, max_lines=DEBUG_MAX_ENTRIES)
        yield Footer()

    def on_mount(self) -> None:
        """Initialize table columns, timers, and the first refresh.

        Returns:
            None.
        """
        self.query_one("#containers", DataTable).add_columns(
            "ID", "Name", "Image", "State", "Status", "Ports"
        )
        self.query_one("#images", DataTable).add_columns(
            "Repository", "Tag", "Image ID", "Created", "Size", "Containers"
        )
        self.query_one("#volume-table", DataTable).add_columns(
            "Name", "Driver", "Created", "Used by", "Mountpoint"
        )
        self.query_one("#debug", RichLog).display = self.debug_visible
        self.update_debug_title()
        self.set_interval(0.2, self.drain_debug)
        self.update_subtitle()
        self.action_refresh()
        self.set_interval(REFRESH_SECONDS, self.action_refresh)

    def show_debug_entry(self, entry: CommandEntry) -> bool:
        """Determine whether a traced entry passes the refresh filter.

        Args:
            entry: Traced command entry to inspect or display.

        Returns:
            True if the entry should be shown under the current filter.
        """
        return not (self.debug_hide_refresh and entry.refresh)

    def write_debug_entry(self, log: RichLog, entry: CommandEntry) -> None:
        """Write an escaped, optionally styled entry to the debug panel.

        Args:
            log: Debug log widget receiving the entry.
            entry: Traced command entry to inspect or display.

        Returns:
            None.
        """
        text = escape(entry.text)
        log.write(f"[{entry.style}]{text}[/]" if entry.style else text)

    def drain_debug(self) -> None:
        """Move traced commands into the debug panel (runs on the UI thread).

        Returns:
            None.
        """
        TRACER.announce_slow()
        log = self.query_one("#debug", RichLog)
        added = False
        while True:
            try:
                entry = TRACER.entries.get_nowait()
            except queue.Empty:
                break
            self.debug_entries.append(entry)
            if self.show_debug_entry(entry):
                self.write_debug_entry(log, entry)
            added = True
        if added:
            self.update_debug_title()

    def update_debug_title(self) -> None:
        """Update the debug panel title and keyboard hints.

        Returns:
            None.
        """
        log = self.query_one("#debug", RichLog)
        log.border_title = (
            f"Debug: wslc commands — {TRACER.total} run, {TRACER.failed} failed"
            + (" — refresh hidden" if self.debug_hide_refresh else "")
        )
        log.border_subtitle = "D hide panel · H hide/show refresh commands"

    def action_toggle_debug(self) -> None:
        """Toggle the debug panel and refresh key bindings.

        Returns:
            None.
        """
        self.debug_visible = not self.debug_visible
        log = self.query_one("#debug", RichLog)
        log.display = self.debug_visible
        if self.debug_visible:
            log.scroll_end(animate=False)
        self.refresh_bindings()

    def action_toggle_debug_refresh(self) -> None:
        """Hide or show the auto-refresh commands, re-rendering the panel.

        Returns:
            None.
        """
        self.debug_hide_refresh = not self.debug_hide_refresh
        log = self.query_one("#debug", RichLog)
        log.clear()
        for entry in self.debug_entries:
            if self.show_debug_entry(entry):
                self.write_debug_entry(log, entry)
        self.update_debug_title()

    def active_tab(self) -> str | None:
        """Get the identifier of the currently active tab.

        Returns:
            The active tab identifier, or None if there is no active tab.
        """
        return self.query_one("#tabs", TabbedContent).active

    def check_action(self, action: str, _: tuple[object, ...]) -> bool | None:
        """Only show and enable actions that apply to the active tab.

        Args:
            action: Textual action name to check.
            _: Framework-supplied argument that is not used by this handler.

        Returns:
            True if the action is enabled for the current screen and tab; otherwise False.
        """
        if isinstance(self.screen, ModalScreen):
            # Keep app keys from firing behind an open dialog.
            return False
        if action == "toggle_debug_refresh":
            return self.debug_visible
        if action in self.CONTAINER_ACTIONS:
            return self.active_tab() == "containers-tab"
        if action in self.IMAGE_ACTIONS:
            return self.active_tab() == "images-tab"
        return True

    def on_tabbed_content_tab_activated(
        self, _: TabbedContent.TabActivated
    ) -> None:
        """Refresh action bindings when the active tab changes.

        Args:
            _: Framework-supplied argument that is not used by this handler.

        Returns:
            None.
        """
        self.refresh_bindings()

    def update_subtitle(self) -> None:
        """Show the current container filter in the app subtitle.

        Returns:
            None.
        """
        self.sub_title = "all containers" if self.show_all else "running containers"

    def action_about(self) -> None:
        """Show application information."""
        self.push_screen(AboutScreen())

    def action_refresh(self) -> None:
        """Schedule a data refresh.

        Returns:
            None.
        """
        self.load_data()

    def action_toggle_all(self) -> None:
        """Toggle the container filter and refresh the data.

        Returns:
            None.
        """
        self.show_all = not self.show_all
        self.update_subtitle()
        self.action_refresh()

    @work(thread=True, exclusive=True, group="refresh")
    def load_data(self) -> None:
        """Fetch containers and images off the UI thread.

        Returns:
            None.
        """
        with TRACER.label("refresh"):
            self.fetch_and_populate()

    def fetch_and_populate(self) -> None:
        """Fetch container, image, and volume data and populate the UI.

        Returns:
            None.
        """
        try:
            # Always list every container: stopped ones still hold volumes.
            all_containers = list_containers(True)
            images = list_images()
        except (WslcError, ValueError) as exc:
            self.call_from_thread(self.notify, str(exc), title="Refresh failed", severity="error")
            return
        containers = all_containers if self.show_all else [
            c for c in all_containers if c.get("State") in ("running", "paused", "restarting")
        ]
        volumes = None
        try:
            volumes = list_volume_details()
            self.volumes = [v["Name"] for v in volumes]
            new = [n for n in self.volumes if n not in self.volume_created]
            if new:
                self.volume_created.update(volume_created_times(new))
        except (WslcError, ValueError, KeyError) as exc:
            if volumes is None:
                self.call_from_thread(
                    self.notify, str(exc), title="Listing volumes failed", severity="error"
                )
        self.call_from_thread(self.populate, containers, images, volumes, all_containers)

    def populate(
        self,
        containers: list[dict],
        images: list[dict],
        volumes: list[dict] | None,
        all_containers: list[dict],
    ) -> None:
        """Fill the tables, keeping the cursor on the same row when possible.

        ``volumes`` is None when listing them failed; that table is left as is.

        Args:
            containers: Container records returned by wslc.
            images: Image records returned by wslc.
            volumes: Volume records returned by wslc, or None if listing failed.
            all_containers: All container records, including stopped containers.

        Returns:
            None.
        """
        if volumes is not None:
            self.populate_volumes(volumes, all_containers)
        self.images = images
        table = self.query_one("#containers", DataTable)
        selected = self.selected_container_id()
        table.clear()
        self.container_states = {c.get("ID", ""): c.get("State", "") for c in containers}
        for c in containers:
            cid = c.get("ID", "")
            state = c.get("State", "")
            label = "busy…" if cid in self.busy else state
            table.add_row(
                cid,
                c.get("Names", ""),
                c.get("Image", ""),
                f"[{STATE_STYLES.get(state, '')}]{label}[/]" if label else "",
                c.get("Status", ""),
                container_ports(c),
                key=cid,
            )
        if selected and selected in {c.get("ID") for c in containers}:
            table.move_cursor(row=table.get_row_index(selected))

        image_table = self.query_one("#images", DataTable)
        image_row = image_table.cursor_row
        selected_image = self.selected_image_key()
        image_table.clear()
        keys = []
        for index, i in enumerate(images):
            ref = image_ref(i)
            # Untagged images can share a ref, so fall back to a unique key.
            key = ref if ref and ref not in keys else f"#{index}:{i.get('ID', '')}"
            keys.append(key)
            tag = i.get("Tag", "")
            if ref in self.updating:
                tag = f"{tag} [yellow](updating…)[/]"
            elif (ref or i.get("ID", "")) in self.deleting:
                tag = f"{tag} [red](deleting…)[/]"
            image_table.add_row(
                i.get("Repository", ""),
                tag,
                i.get("ID", ""),
                i.get("CreatedSince", ""),
                i.get("Size", ""),
                i.get("Containers", ""),
                key=key,
            )
        if selected_image in keys:
            image_table.move_cursor(row=keys.index(selected_image))
        elif images:
            image_table.move_cursor(row=min(image_row, len(images) - 1))

    def populate_volumes(self, volumes: list[dict], containers: list[dict]) -> None:
        """Update the volume table and restore its selected row when possible.

        Args:
            volumes: Volume records to display.
            containers: Container records returned by wslc.

        Returns:
            None.
        """
        table = self.query_one("#volume-table", DataTable)
        row = table.cursor_row
        selected = self.selected_volume()
        names = [v["Name"] for v in volumes]
        self.volume_used_by = volume_users(containers, names)
        self.volume_created = {n: t for n, t in self.volume_created.items() if n in names}
        table.clear()
        for v in volumes:
            name = v["Name"]
            label = f"{name} [red](deleting…)[/]" if name in self.deleting_volumes else name
            users = self.volume_used_by.get(name) or []
            table.add_row(
                label,
                v.get("Driver", ""),
                self.volume_created.get(name, ""),
                ", ".join(users) if users else "[dim]unused[/]",
                v.get("Mountpoint", ""),
                key=name,
            )
        if selected in names:
            table.move_cursor(row=names.index(selected))
        elif names:
            table.move_cursor(row=min(row, len(names) - 1))

    def selected_volume(self) -> str | None:
        """Get the selected volume row key.

        Returns:
            The selected volume name, or None if the table is empty.
        """
        table = self.query_one("#volume-table", DataTable)
        if table.row_count == 0:
            return None
        row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        return row_key.value

    def selected_container_id(self) -> str | None:
        """Get the selected container row key.

        Returns:
            The selected container ID, or None if the table is empty.
        """
        table = self.query_one("#containers", DataTable)
        if table.row_count == 0:
            return None
        row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        return row_key.value

    def selected_container(self) -> tuple[str, str] | None:
        """Return (id, name) of the highlighted container, notifying if unavailable.

        Returns:
            The selected container's (ID, name), or None if unavailable.
        """
        if self.query_one("#tabs", TabbedContent).active != "containers-tab":
            self.notify("Switch to the Containers tab to select a container.", severity="warning")
            return None
        cid = self.selected_container_id()
        if cid is None:
            self.notify("No container selected.", severity="warning")
            return None
        if cid in self.busy:
            self.notify("An action is already running for this container.", severity="warning")
            return None
        name = self.query_one("#containers", DataTable).get_row(cid)[1]
        return cid, name

    def action_logs(self) -> None:
        """Open the log viewer for the highlighted container.

        Returns:
            None.
        """
        cid = self.selected_container_id()
        if cid is None:
            self.notify("No container selected.", severity="warning")
            return
        name = self.query_one("#containers", DataTable).get_row(cid)[1]
        follow = self.container_states.get(cid) == "running"
        self.push_screen(LogsScreen(cid, name, follow))

    def action_start(self) -> None:
        """Start the selected container if it is available.

        Returns:
            None.
        """
        if selected := self.selected_container():
            self.container_action("start", *selected)

    def action_stop(self) -> None:
        """Stop the selected container if it is available.

        Returns:
            None.
        """
        if selected := self.selected_container():
            self.container_action("stop", *selected)

    @work(thread=True)
    def container_action(self, verb: str, cid: str, name: str) -> None:
        """Run ``wslc start|stop`` for a container, then refresh.

        Args:
            verb: Container action to run: start or stop.
            cid: Container identifier.
            name: Container name used in notifications.

        Returns:
            None.
        """
        self.busy.add(cid)
        progress = {"start": "Starting", "stop": "Stopping"}[verb]
        self.call_from_thread(self.notify, f"{progress} {name}…")
        self.call_from_thread(self.action_refresh)
        try:
            run_wslc(verb, cid)
        except WslcError as exc:
            self.call_from_thread(
                self.notify, str(exc), title=f"{verb.title()} failed", severity="error"
            )
        else:
            self.call_from_thread(self.notify, f"{name}: {verb} succeeded.")
        finally:
            self.busy.discard(cid)
            self.call_from_thread(self.action_refresh)

    def image_options(self) -> list[tuple[str, str]]:
        """Return (label, value) pairs of local images for the image picker.

        Returns:
            Unique (display label, image reference or ID) pairs.
        """
        options = []
        for image in self.images:
            value = image_ref(image) or image.get("ID", "")
            if value and value not in {v for _, v in options}:
                options.append((f"{value}  [dim]{image.get('Size', '')}[/]", value))
        return options

    def container_names(self) -> set[str]:
        """Get the names in the current container table.

        Returns:
            The container names currently shown in the table.
        """
        table = self.query_one("#containers", DataTable)
        return {table.get_row(row)[1] for row in table.rows}

    def on_create_submitted(self, spec: ContainerSpec | None) -> None:
        """Start container creation when a form specification is submitted.

        Args:
            spec: Container specification to process.

        Returns:
            None.
        """
        if spec:
            self.create_container(spec)

    def action_new_container(self) -> None:
        """Open the New container form, preselecting the highlighted image.

        Returns:
            None.
        """
        options = self.image_options()
        if not options:
            self.notify("No images available. Pull an image first.", severity="warning")
            return
        selected = None
        if self.active_tab() == "images-tab" and (key := self.selected_image_key()):
            selected = self.query_one("#images", DataTable).get_row(key)[2] if key.startswith("#") else key
        self.push_screen(
            CreateContainerScreen(options, self.volumes, self.container_names(), selected),
            self.on_create_submitted,
        )

    def action_edit_container(self) -> None:
        """Open the form pre-filled from the highlighted container, to save a modified copy.

        Returns:
            None.
        """
        if selected := self.selected_container():
            self.load_edit_form(*selected)

    @work(thread=True, exclusive=True, group="edit")
    def load_edit_form(self, cid: str, name: str) -> None:
        """Inspect the container (and its image) off the UI thread, then open the form.

        Args:
            cid: Container identifier.
            name: Source container name shown in the form or error.

        Returns:
            None.
        """
        try:
            info = inspect_object(cid, "container")
            image_id = info.get("Image", "")
            image_name = (info.get("Config") or {}).get("Image") or ""
            image_info = None
            image = image_id
            if image_name:
                try:
                    image_info = inspect_object(image_name, "image")
                except (WslcError, ValueError):
                    image_info = None
            if image_info and image_info.get("Id") == image_id:
                image = image_name
            else:
                # The tag now points elsewhere (or is gone); pin the exact image the container uses.
                image_info = inspect_object(image_id, "image") if image_id else None
                image = image_id.removeprefix("sha256:")[:12] or image_name
        except (WslcError, ValueError, IndexError) as exc:
            self.call_from_thread(
                self.notify, str(exc), title=f"Couldn't read {name}", severity="error"
            )
            return
        spec = replace(spec_from_inspect(info, image_info), image=image)
        running = bool((info.get("State") or {}).get("Running"))
        self.call_from_thread(self.open_edit_form, spec, name, running)

    def open_edit_form(self, spec: ContainerSpec, name: str, running: bool) -> None:
        """Open the copy form with an inspected container specification.

        Args:
            spec: Container specification to process.
            name: Source container name shown in the form.
            running: Whether the source container is running.

        Returns:
            None.
        """
        self.push_screen(
            CreateContainerScreen(
                self.image_options(),
                self.volumes,
                self.container_names(),
                template=spec,
                source_name=name,
                source_running=running,
            ),
            self.on_create_submitted,
        )

    @work(thread=True)
    def create_container(self, spec: ContainerSpec) -> None:
        """Create any new volumes, then create (and optionally start) the container.

        Args:
            spec: Container specification to process.

        Returns:
            None.
        """
        label = spec.name or spec.image
        self.call_from_thread(self.notify, f"Creating container from {spec.image}…")
        created_volumes = []
        try:
            for volume in spec.new_volumes:
                run_wslc("volume", "create", volume)
                created_volumes.append(volume)
            output = run_wslc(*spec.args(), timeout=600)
        except WslcError as exc:
            # Roll back volumes made for this container so a failed attempt leaves nothing behind.
            for volume in created_volumes:
                try:
                    run_wslc("volume", "remove", volume)
                except WslcError:
                    pass
            self.call_from_thread(
                self.notify, str(exc), title=f"Creating {label} failed", severity="error"
            )
        else:
            ids = re.findall(r"\b[0-9a-f]{64}\b", output)
            cid = ids[-1][:12] if ids else ""
            verb = "created and started" if spec.start else "created"
            extra = f", new volume(s): {', '.join(created_volumes)}" if created_volumes else ""
            self.call_from_thread(self.notify, f"{label} {verb} ({cid}){extra}.")
        finally:
            self.call_from_thread(self.action_refresh)

    def action_delete(self) -> None:
        """Delete the highlighted container, image or volume, depending on the active tab.

        Returns:
            None.
        """
        if self.active_tab() == "images-tab":
            self.action_delete_image()
        elif self.active_tab() == "volumes-tab":
            self.action_delete_volume()
        else:
            self.action_delete_container()

    def action_delete_volume(self) -> None:
        """Confirm, then delete the highlighted volume.

        Returns:
            None.
        """
        name = self.selected_volume()
        if name is None:
            self.notify("No volume selected.", severity="warning")
            return
        if name in self.deleting_volumes:
            self.notify(f"{name} is already being deleted.", severity="warning")
            return
        users = self.volume_used_by.get(name) or []
        warning = "Its data will be permanently lost."
        if users:
            warning = (
                f"It's used by {', '.join(users)}. wslc can't delete a volume that's in use; "
                "delete those containers first."
            )

        def on_choice(choice: str | None) -> None:
            """Remove the selected volume if deletion was confirmed.

            Args:
                choice: Selected deletion action, or None when cancelled.

            Returns:
                None.
            """
            if choice:
                self.remove_volume(name)

        self.push_screen(ConfirmDeleteScreen("volume", name, warning, can_force=False), on_choice)

    @work(thread=True)
    def remove_volume(self, name: str) -> None:
        """Run ``wslc volume remove``, then refresh.

        Args:
            name: Name of the volume to remove.

        Returns:
            None.
        """
        self.deleting_volumes.add(name)
        self.call_from_thread(self.notify, f"Deleting volume {name}…")
        self.call_from_thread(self.action_refresh)
        try:
            run_wslc("volume", "remove", name)
        except WslcError as exc:
            self.call_from_thread(
                self.notify, str(exc), title=f"Delete of volume {name} failed", severity="error"
            )
        else:
            self.call_from_thread(self.notify, f"Volume {name} deleted.")
        finally:
            self.deleting_volumes.discard(name)
            self.call_from_thread(self.action_refresh)

    def action_delete_container(self) -> None:
        """Confirm, then delete the highlighted container.

        Returns:
            None.
        """
        selected = self.selected_container()
        if not selected:
            return
        cid, name = selected
        warning = None
        if self.container_states.get(cid) in ("running", "paused", "restarting"):
            warning = (
                f"{name} is {self.container_states[cid]}. Stop it first, "
                "or force delete to stop and remove it."
            )

        def on_choice(choice: str | None) -> None:
            """Remove the selected container if deletion was confirmed.

            Args:
                choice: Selected deletion action, or None when cancelled.

            Returns:
                None.
            """
            if choice:
                self.remove_container(cid, name, force=choice == "force")

        self.push_screen(ConfirmDeleteScreen("container", f"{name} ({cid})", warning), on_choice)

    @work(thread=True)
    def remove_container(self, cid: str, name: str, force: bool) -> None:
        """Run ``wslc remove`` for a container, then refresh.

        Args:
            cid: Container identifier.
            name: Container name used in notifications.
            force: Whether to request forced deletion.

        Returns:
            None.
        """
        self.busy.add(cid)
        self.call_from_thread(self.notify, f"Deleting {name}…")
        self.call_from_thread(self.action_refresh)
        try:
            run_wslc("remove", *(["--force"] if force else []), cid)
        except WslcError as exc:
            self.call_from_thread(
                self.notify, str(exc), title=f"Delete of {name} failed", severity="error"
            )
        else:
            self.call_from_thread(self.notify, f"{name} deleted.")
        finally:
            self.busy.discard(cid)
            self.call_from_thread(self.action_refresh)

    def selected_image_key(self) -> str | None:
        """Get the selected image row key.

        Returns:
            The selected image row key, or None if the table is empty.
        """
        table = self.query_one("#images", DataTable)
        if table.row_count == 0:
            return None
        row_key, _ = table.coordinate_to_cell_key(table.cursor_coordinate)
        return row_key.value

    def action_update_image(self) -> None:
        """Pull the latest version of the highlighted image.

        Returns:
            None.
        """
        key = self.selected_image_key()
        if key is None:
            self.notify("No image selected.", severity="warning")
            return
        if key.startswith("#"):
            self.notify("Untagged images can't be updated.", severity="warning")
            return
        if key in self.updating:
            self.notify(f"{key} is already updating.", severity="warning")
            return
        old_id = self.query_one("#images", DataTable).get_row(key)[2]
        self.pull_image(key, old_id)

    def action_delete_image(self) -> None:
        """Confirm, then delete the highlighted image.

        Returns:
            None.
        """
        key = self.selected_image_key()
        if key is None:
            self.notify("No image selected.", severity="warning")
            return
        row = self.query_one("#images", DataTable).get_row(key)
        # Tagged images are removed by name so other tags of the same ID survive.
        target = row[2] if key.startswith("#") else key
        if target in self.deleting or target in self.updating:
            self.notify(f"{target} is busy.", severity="warning")
            return
        try:
            in_use = int(row[5])
        except (TypeError, ValueError):
            in_use = 0
        warning = None
        if in_use:
            warning = f"{in_use} container(s) use this image. Remove them first, or force delete."

        def on_choice(choice: str | None) -> None:
            """Remove the selected image if deletion was confirmed.

            Args:
                choice: Selected deletion action, or None when cancelled.

            Returns:
                None.
            """
            if choice:
                self.remove_image(target, force=choice == "force")

        self.push_screen(ConfirmDeleteScreen("image", target, warning), on_choice)

    @work(thread=True)
    def remove_image(self, target: str, force: bool) -> None:
        """Run ``wslc rmi`` for an image, then refresh.

        Args:
            target: Image reference or ID to remove.
            force: Whether to request forced deletion.

        Returns:
            None.
        """
        self.deleting.add(target)
        self.call_from_thread(self.notify, f"Deleting {target}…")
        self.call_from_thread(self.action_refresh)
        try:
            run_wslc("rmi", *(["--force"] if force else []), target)
        except WslcError as exc:
            self.call_from_thread(
                self.notify, str(exc), title=f"Delete of {target} failed", severity="error"
            )
        else:
            self.call_from_thread(self.notify, f"{target} deleted.")
        finally:
            self.deleting.discard(target)
            self.call_from_thread(self.action_refresh)

    @work(thread=True)
    def pull_image(self, ref: str, old_id: str) -> None:
        """Run ``wslc pull`` for an image and report whether it changed.

        Args:
            ref: Tagged image reference to pull.
            old_id: Image ID before pulling, used to detect a change.

        Returns:
            None.
        """
        self.updating.add(ref)
        self.call_from_thread(self.notify, f"Updating {ref}…")
        self.call_from_thread(self.action_refresh)
        try:
            run_wslc("pull", ref, timeout=None)
            new_id = next(
                (i.get("ID", "") for i in list_images() if image_ref(i) == ref), ""
            )
        except (WslcError, ValueError) as exc:
            self.call_from_thread(
                self.notify, str(exc), title=f"Update of {ref} failed", severity="error"
            )
        else:
            if new_id and new_id != old_id:
                message = f"{ref} updated ({old_id} → {new_id})."
            else:
                message = f"{ref} is already up to date."
            self.call_from_thread(self.notify, message)
        finally:
            self.updating.discard(ref)
            self.call_from_thread(self.action_refresh)


def main() -> None:
    """Parse command-line options and run the terminal application.

    Returns:
        None.
    """
    parser = argparse.ArgumentParser(description="Terminal UI for the wslc container CLI.")
    parser.add_argument(
        "--debug", action="store_true", help="show the panel listing every wslc command (toggle with D)"
    )
    parser.add_argument(
        "--debug-log", metavar="FILE", help="also append every wslc command and its result to FILE"
    )
    options = parser.parse_args()
    with ExitStack() as stack:
        if options.debug_log:
            TRACER.log_file = stack.enter_context(
                open(options.debug_log, "a", encoding="utf-8")
            )
            TRACER.write_file(
                f"--- {APP_NAME} debug log started {datetime.now():%Y-%m-%d %H:%M:%S} ---"
            )
        WslcTui(debug=options.debug).run()


if __name__ == "__main__":
    main()
