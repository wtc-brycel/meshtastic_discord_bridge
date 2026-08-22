"""A small, production-oriented Discord <-> Meshtastic bridge.

The module deliberately keeps the policy and parsing functions independent of
Discord and Meshtastic.  This makes channel safety and message formatting easy
to test, and importing this module never connects to either service.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import queue
import re
import shlex
import subprocess
import sys
import textwrap
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional, TextIO

try:  # Optional at import time so policy/parser tests need no third-party packages.
    import discord
except ImportError:  # pragma: no cover - exercised only in minimal environments
    discord = None

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    def load_dotenv(*_args: Any, **_kwargs: Any) -> bool:
        return False

try:
    from pubsub import pub
except ImportError:  # pragma: no cover
    pub = None


PRIMARY_CHANNEL = 0
READ_ONLY_CHANNEL = 1
MAX_MESH_TEXT = 225
FVP10_COLUMNS = 48  # Star FVP10 80 mm native Font A at 12 cpi.
_ANSI_ESCAPE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")


@dataclass(frozen=True)
class Config:
    discord_token: str
    discord_channel_id: int
    meshtastic_hostname: Optional[str] = None
    serial_port: str = "/dev/ttyUSB0"
    audit_log_path: str = "bridge-audit.jsonl"
    multimon_source: Optional[str] = None
    multimon_command: Optional[str] = None
    multimon_file: Optional[str] = None
    poll_seconds: float = 0.25
    fvp10_enabled: bool = False
    fvp10_transport_command: str = "lp -d star -o raw"
    fvp10_timeout_seconds: float = 15.0

    @classmethod
    def from_env(cls) -> "Config":
        load_dotenv()
        channel = os.getenv("DISCORD_CHANNEL_ID", "").strip()
        if not channel:
            raise ValueError("DISCORD_CHANNEL_ID must be set")
        source = os.getenv("MULTIMON_SOURCE", "").strip() or None
        return cls(
            discord_token=os.getenv("DISCORD_TOKEN", "").strip(),
            discord_channel_id=int(channel),
            meshtastic_hostname=os.getenv("MESHTASTIC_HOSTNAME", "").strip() or None,
            serial_port=os.getenv("MESHTASTIC_SERIAL_PORT", "/dev/ttyUSB0").strip() or "/dev/ttyUSB0",
            audit_log_path=os.getenv("AUDIT_LOG_PATH", "bridge-audit.jsonl").strip() or "bridge-audit.jsonl",
            multimon_source=source,
            multimon_command=os.getenv("MULTIMON_COMMAND", "").strip() or None,
            multimon_file=os.getenv("MULTIMON_FILE", "").strip() or None,
            poll_seconds=float(os.getenv("BRIDGE_POLL_SECONDS", "0.25")),
            fvp10_enabled=_env_bool("FVP10_ENABLED"),
            fvp10_transport_command=os.getenv(
                "FVP10_TRANSPORT_COMMAND", "lp -d star -o raw"
            ).strip() or "lp -d star -o raw",
            fvp10_timeout_seconds=float(os.getenv("FVP10_TIMEOUT_SECONDS", "15")),
        )


class AuditLogger:
    """Write one durable, machine-readable record per bridge event."""

    def __init__(self, path: str = "bridge-audit.jsonl", stream: Optional[TextIO] = None):
        self.path = path
        self._stream = stream
        self._lock = threading.Lock()
        self._owned_stream: Optional[TextIO] = None

    def _get_stream(self) -> TextIO:
        if self._stream is not None:
            return self._stream
        if self.path == "-":
            self._stream = sys.stdout
        else:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            self._owned_stream = open(self.path, "a", encoding="utf-8", buffering=1)
            self._stream = self._owned_stream
        return self._stream

    def log(self, event: str, *, correlation_id: Optional[str] = None,
            packet_id: Any = None, message_id: Any = None, **fields: Any) -> str:
        correlation_id = correlation_id or str(uuid.uuid4())
        record: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": event,
            "correlation_id": correlation_id,
        }
        if packet_id is not None:
            record["packet_id"] = str(packet_id)
        if message_id is not None:
            record["message_id"] = str(message_id)
        record.update(fields)
        with self._lock:
            stream = self._get_stream()
            stream.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
            stream.flush()
        return correlation_id

    def close(self) -> None:
        with self._lock:
            if self._owned_stream is not None:
                self._owned_stream.close()
                self._owned_stream = None
                self._stream = None


def _receipt_ascii(value: str) -> str:
    replacements = {
        "\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"',
        "\u2013": "-", "\u2014": "--", "\u2026": "...", "\u2022": "*",
    }
    for old, new in replacements.items():
        value = value.replace(old, new)
    return value.encode("ascii", errors="replace").decode("ascii")


def _receipt_lines(value: str, width: int = FVP10_COLUMNS) -> list[str]:
    lines: list[str] = []
    for paragraph in _receipt_ascii(value).splitlines():
        lines.extend(textwrap.wrap(
            paragraph.strip(), width=width, break_long_words=False,
            break_on_hyphens=False,
        ) or [""])
    return lines or [""]


def render_fvp10_receipt(payload: str) -> bytes:
    """Render a tested 80 mm Star Line Mode receipt (576 dots / 48 columns)."""
    esc, gs = b"\x1b", b"\x1d"
    data = bytearray(esc + b"@")
    data += esc + b"M"                         # Font A, 12 cpi = 48 columns.
    data += esc + gs + b"a\x01"                # centered header
    data += esc + b"E" + esc + b"-\x01" + esc + b"i\x01\x00"
    data += b"MESHTASTIC MESSAGE\n"
    data += esc + b"i\x00\x00" + esc + b"-\x00" + esc + b"E"
    data += (b"-" * FVP10_COLUMNS) + b"\n"
    data += esc + gs + b"a\x00"                # left aligned body
    for line in _receipt_lines(payload):
        data += line.encode("ascii", errors="replace") + b"\n"
    data += b"-" * FVP10_COLUMNS + b"\n"
    data += esc + gs + b"a\x01" + b"APEX MESHTASTIC BRIDGE\n"
    data += esc + gs + b"a\x00" + b"\n\n\n"
    data += esc + b"d\x03"                    # partial feed and cut
    return bytes(data)


def send_fvp10_transport(command: str, payload: bytes | str, timeout_seconds: float = 15.0) -> None:
    """Send one rendered message to a configured FVP10 transport command.

    The command receives a native Star Line receipt on stdin. It is tokenized without a
    shell, so environment configuration cannot turn message text into shell
    syntax. A typical transport is ``lpr -P fvp10-raw -l``.
    """
    args = shlex.split(command)
    if not args:
        raise ValueError("FVP10 transport command is empty")
    result = subprocess.run(
        args,
        input=payload if isinstance(payload, bytes) else payload.encode("ascii", errors="replace"),
        text=False,
        capture_output=True,
        timeout=timeout_seconds,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "no diagnostic output").strip()
        raise RuntimeError(f"transport exited {result.returncode}: {detail}")


def _value(packet: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in packet and packet[key] is not None:
            return packet[key]
        decoded = packet.get("decoded")
        if isinstance(decoded, dict) and key in decoded and decoded[key] is not None:
            return decoded[key]
    return None


def mesh_channel(packet: dict[str, Any]) -> Optional[int]:
    """Return a packet's numeric channel, including the LongFast alias."""
    value = _value(packet, "channel", "channelIndex", "channel_index", "channelName", "channel_name")
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"longfast", "long fast"}:
            return READ_ONLY_CHANNEL
        if normalized.isdigit():
            return int(normalized)
        return None
    if isinstance(value, dict):
        nested_name = str(value.get("name", "")).strip().lower()
        if nested_name in {"longfast", "long fast"}:
            return READ_ONLY_CHANNEL
        nested_index = value.get("index", value.get("channelIndex"))
        try:
            return int(nested_index) if nested_index is not None else None
        except (TypeError, ValueError):
            return None
    try:
        return int(value) if value is not None else PRIMARY_CHANNEL
    except (TypeError, ValueError):
        return None


def classify_mesh_channel(packet: dict[str, Any]) -> str:
    channel = mesh_channel(packet)
    if channel == PRIMARY_CHANNEL:
        return "primary"
    if channel is not None and channel > PRIMARY_CHANNEL:
        return "read_only"
    return "rejected"


def packet_id(packet: dict[str, Any]) -> Any:
    return packet.get("id") or packet.get("packetId") or packet.get("packet_id")


def format_mesh_message(packet: dict[str, Any]) -> str:
    """Format a text packet for Discord, marking LongFast as read-only."""
    decoded = packet.get("decoded") if isinstance(packet.get("decoded"), dict) else {}
    text = str(decoded.get("text", packet.get("text", ""))).strip()
    sender = packet.get("fromId", packet.get("from", "unknown"))
    recipient = packet.get("toId", packet.get("to", "broadcast"))
    prefix = "[READ-ONLY] " if classify_mesh_channel(packet) == "read_only" else ""
    return f"{prefix}Node {sender} writes to node {recipient} this message: {text}"


def format_discord_message(message: Any, content: Optional[str] = None) -> str:
    """Prefix a Discord message with its display name and immutable user ID."""
    author = message.author
    name = getattr(author, "display_name", None) or getattr(author, "name", "unknown")
    body = str(message.content if content is None else content).strip()
    return f"{name} ({getattr(author, 'id', 'unknown')}): {body}"


def authorize_discord_message(message: Any, channel_id: int,
                              bot_user_id: Any = None) -> tuple[bool, str]:
    """Authorize only messages from the configured Discord channel."""
    author = getattr(message, "author", None)
    author_id = getattr(author, "id", None)
    if (bot_user_id is not None and author_id == bot_user_id) or getattr(author, "bot", False):
        return False, "bot_message"
    actual_channel = getattr(getattr(message, "channel", None), "id", None)
    if actual_channel != channel_id:
        return False, "wrong_discord_channel"
    return True, "authorized"


def parse_multimon_line(line: str) -> Optional[str]:
    """Parse one line from multimon-ng without assuming a decoder format.

    multimon-ng emits several decoder-specific formats.  The bridge treats the
    complete non-empty line as payload, removing terminal colour escapes only.
    This preserves decoder text and makes the adapter work with future formats.
    """
    cleaned = _ANSI_ESCAPE.sub("", line).strip()
    return cleaned or None


class MultimonLineSource(Iterator[str]):
    """Line-oriented stdin, file, or command adapter for multimon-ng output."""

    def __init__(self, spec: str, *, stdin: Optional[TextIO] = None):
        self.spec = spec.strip()
        self._process: Optional[subprocess.Popen[str]] = None
        self._owns_stream = False
        if self.spec in {"", "none", "disabled"}:
            self.stream = None
        elif self.spec == "stdin":
            self.stream = stdin or sys.stdin
        elif self.spec.startswith("file:"):
            self.stream = open(self.spec[5:], "r", encoding="utf-8", errors="replace")
            self._owns_stream = True
        elif self.spec.startswith("command:"):
            args = shlex.split(self.spec[8:])
            if not args:
                raise ValueError("MULTIMON_SOURCE command is empty")
            self._process = subprocess.Popen(args, stdout=subprocess.PIPE,
                                              stderr=subprocess.STDOUT, text=True,
                                              bufsize=1)
            self.stream = self._process.stdout
        else:
            raise ValueError("MULTIMON_SOURCE must be stdin, file:PATH, or command:COMMAND")

    def __next__(self) -> str:
        if self.stream is None:
            raise StopIteration
        line = self.stream.readline()
        if line == "":
            raise StopIteration
        parsed = parse_multimon_line(line)
        return parsed if parsed is not None else self.__next__()

    def close(self) -> None:
        if self._process is not None and self._process.poll() is None:
            self._process.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                self._process.wait(timeout=2)
        if (self._owns_stream or self._process is not None) and self.stream is not None:
            self.stream.close()


def multimon_spec(config: Config) -> Optional[str]:
    if config.multimon_source:
        return config.multimon_source
    if config.multimon_command:
        return f"command:{config.multimon_command}"
    if config.multimon_file:
        return f"file:{config.multimon_file}"
    return None


def _text_packet(packet: dict[str, Any]) -> bool:
    decoded = packet.get("decoded")
    return isinstance(decoded, dict) and decoded.get("portnum") == "TEXT_MESSAGE_APP" and "text" in decoded


def _build_client(config: Config, audit: AuditLogger) -> Any:
    if discord is None:
        raise RuntimeError("discord.py is required to run the bridge")
    if pub is None:
        raise RuntimeError("pypubsub is required to run the bridge")

    intents = discord.Intents.default()
    intents.message_content = True

    class Client(discord.Client):
        def __init__(self, *args: Any, **kwargs: Any):
            super().__init__(*args, **kwargs)
            self.interface: Any = None
            self.multimon: Optional[MultimonLineSource] = None
            self.tasks: list[asyncio.Task[Any]] = []
            self.stopping = False

        async def setup_hook(self) -> None:
            self.tasks.append(asyncio.create_task(self.bridge_loop()))
            spec = multimon_spec(config)
            if spec:
                self.multimon = MultimonLineSource(spec)
                self.tasks.append(asyncio.create_task(self.multimon_loop()))

        async def on_ready(self) -> None:
            print(f"Logged in as {self.user} (ID: {self.user.id})")

        async def on_message(self, message: Any) -> None:
            received_correlation = audit.log("received", message_id=getattr(message, "id", None),
                                             direction="discord_to_mesh")
            allowed, reason = authorize_discord_message(message, config.discord_channel_id,
                                                         getattr(self.user, "id", None))
            if not allowed:
                audit.log("rejected", correlation_id=received_correlation,
                          message_id=getattr(message, "id", None), reason=reason)
                return
            content = str(message.content).strip()
            if content.startswith("$help"):
                await message.channel.send(
                    "Meshtastic Discord Bridge is up. Commands:\n"
                    "$sendprimary <message>\n$send nodenum=########### <message>\n"
                    "$activenodes"
                )
                return
            if content.startswith("$activenodes"):
                await self.send_node_list(message.channel)
                return
            text, destination = self.parse_send_command(content)
            if text is None:
                audit.log("rejected", correlation_id=received_correlation,
                          message_id=getattr(message, "id", None), reason="not_a_send_command")
                return
            if not text:
                audit.log("rejected", correlation_id=received_correlation,
                          message_id=getattr(message, "id", None), reason="empty_message")
                await message.channel.send("Could not send an empty message")
                return
            text = format_discord_message(message, text)
            # The Discord identity prefix is intentionally part of every mesh payload.
            text = text[:MAX_MESH_TEXT]
            succeeded = await self.transmit(text, destination=destination,
                                            correlation_id=received_correlation,
                                            message_id=getattr(message, "id", None))
            if succeeded:
                target = "primary channel" if destination is None else f"node {destination}"
                await message.channel.send(f"Sent to {target}.")
            else:
                await message.channel.send("Could not send message to the mesh.")

        @staticmethod
        def parse_send_command(content: str) -> tuple[Optional[str], Optional[int]]:
            if content == "$sendprimary" or content.startswith("$sendprimary "):
                return content[len("$sendprimary"):].strip()[:MAX_MESH_TEXT], None
            match = re.match(r"^\$send\s+nodenum=(\d+)\s+(.+)$", content, re.DOTALL)
            if match:
                return match.group(2).strip()[:MAX_MESH_TEXT], int(match.group(1))
            return None, None

        async def connect_mesh(self) -> Any:
            import meshtastic.serial_interface
            import meshtastic.tcp_interface
            if config.meshtastic_hostname:
                return meshtastic.tcp_interface.TCPInterface(config.meshtastic_hostname)
            # Some 2.7.x firmware emits diagnostic frames which are not valid
            # FromRadio messages. The stock StreamInterface logs a traceback
            # for each one. Probe before delegating so the reader can discard
            # only malformed frames and immediately resynchronize.
            from google.protobuf.message import DecodeError
            from meshtastic.protobuf import mesh_pb2

            class BridgeSerialInterface(meshtastic.serial_interface.SerialInterface):
                def _handleFromRadio(self, payload: bytes) -> None:
                    try:
                        mesh_pb2.FromRadio().ParseFromString(payload)
                    except DecodeError:
                        return
                    super()._handleFromRadio(payload)

            return BridgeSerialInterface(devPath=config.serial_port)

        def receive_mesh(self, packet: dict[str, Any], _interface: Any = None) -> None:
            pid = packet_id(packet)
            correlation = audit.log("received", packet_id=pid, direction="mesh_to_discord",
                                    channel=mesh_channel(packet))
            if not _text_packet(packet):
                audit.log("rejected", correlation_id=correlation, packet_id=pid,
                          reason="not_text_message")
                return
            classification = classify_mesh_channel(packet)
            if classification == "rejected":
                audit.log("rejected", correlation_id=correlation, packet_id=pid,
                          reason="unsupported_mesh_channel", channel=mesh_channel(packet))
                return
            payload = format_mesh_message(packet)
            audit.log("forwarded", correlation_id=correlation, packet_id=pid,
                      destination="discord", read_only=classification == "read_only")
            self._mesh_to_discord.put((payload, correlation, pid))

        async def transmit(self, text: str, *, destination: Optional[int],
                           correlation_id: str, message_id: Any = None) -> bool:
            # There is exactly one transmit path and it is hard-coded to channel 0.
            audit.log("transmit_attempted", correlation_id=correlation_id,
                      message_id=message_id, channel=PRIMARY_CHANNEL, destination=destination)
            try:
                kwargs: dict[str, Any] = {"channelIndex": PRIMARY_CHANNEL}
                if destination is not None:
                    kwargs["destinationId"] = destination
                try:
                    self.interface.sendText(text, **kwargs)
                except TypeError:  # Older Meshtastic releases infer the primary channel.
                    kwargs.pop("channelIndex", None)
                    self.interface.sendText(text, **kwargs)
                audit.log("transmit_succeeded", correlation_id=correlation_id,
                          message_id=message_id, channel=PRIMARY_CHANNEL)
                return True
            except Exception as exc:
                audit.log("transmit_failed", correlation_id=correlation_id,
                          message_id=message_id, channel=PRIMARY_CHANNEL, error=str(exc))
                return False

        async def bridge_loop(self) -> None:
            await self.wait_until_ready()
            self._mesh_to_discord: queue.Queue[tuple[str, str, Any]] = queue.Queue()
            self.interface = await self.connect_mesh()
            pub.subscribe(self.receive_mesh, "meshtastic.receive")
            channel = self.get_channel(config.discord_channel_id)
            while not self.stopping and not self.is_closed():
                try:
                    payload, correlation, pid = self._mesh_to_discord.get_nowait()
                except queue.Empty:
                    await asyncio.sleep(config.poll_seconds)
                    continue
                try:
                    await channel.send(payload)
                    audit.log("forward_succeeded", correlation_id=correlation,
                              packet_id=pid, destination="discord")
                except Exception as exc:
                    audit.log("forward_failed", correlation_id=correlation,
                              packet_id=pid, destination="discord", error=str(exc))
                if config.fvp10_enabled:
                    audit.log("transport_attempted", correlation_id=correlation,
                              packet_id=pid, destination="fvp10")
                    try:
                        await asyncio.to_thread(
                            send_fvp10_transport,
                            config.fvp10_transport_command,
                            render_fvp10_receipt(payload),
                            config.fvp10_timeout_seconds,
                        )
                        audit.log("transport_succeeded", correlation_id=correlation,
                                  packet_id=pid, destination="fvp10")
                    except Exception as exc:
                        audit.log("transport_failed", correlation_id=correlation,
                                  packet_id=pid, destination="fvp10", error=str(exc))

        async def multimon_loop(self) -> None:
            await self.wait_until_ready()
            while not self.stopping and self.multimon is not None:
                try:
                    line = await asyncio.to_thread(self._read_multimon_line)
                except EOFError:
                    return
                except Exception as exc:
                    audit.log("rejected", reason="multimon_read_failed", error=str(exc))
                    return
                correlation = audit.log("received", direction="multimon_to_mesh",
                                        channel=PRIMARY_CHANNEL, payload=line)
                await self.transmit(line[:MAX_MESH_TEXT], destination=None,
                                    correlation_id=correlation)

        def _read_multimon_line(self) -> str:
            """Translate iterator EOF into an exception safe for asyncio.to_thread."""
            try:
                return next(self.multimon)  # type: ignore[arg-type]
            except StopIteration as exc:
                raise EOFError from exc

        async def send_node_list(self, channel: Any) -> None:
            nodes = getattr(self.interface, "nodes", {}) or {}
            lines = ["Node list:"]
            for key, node in nodes.items():
                user = node.get("user", {})
                lines.append(f"id:{user.get('id', key)}, num:{node.get('num', '?')}, "
                             f"longname:{user.get('longName', '?')}, hops:{node.get('hopsAway', 0)}, "
                             f"snr:{node.get('snr', '?')}")
            await channel.send("\n".join(lines)[:1900])

        async def close(self) -> None:
            if self.stopping:
                return
            self.stopping = True
            if pub is not None:
                with contextlib.suppress(Exception):
                    pub.unsubscribe(self.receive_mesh, "meshtastic.receive")
            if self.multimon:
                self.multimon.close()
            for task in self.tasks:
                task.cancel()
            if self.tasks:
                await asyncio.gather(*self.tasks, return_exceptions=True)
            if self.interface is not None:
                with contextlib.suppress(Exception):
                    self.interface.close()
            audit.close()
            await super().close()

    return Client(intents=intents)


def main() -> None:
    config = Config.from_env()
    if not config.discord_token:
        raise SystemExit("DISCORD_TOKEN must be set")
    audit = AuditLogger(config.audit_log_path)
    client = _build_client(config, audit)
    try:
        client.run(config.discord_token)
    except KeyboardInterrupt:
        pass
    finally:
        audit.close()


if __name__ == "__main__":
    main()
