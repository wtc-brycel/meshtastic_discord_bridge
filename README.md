# meshtastic_discord_bridge

A small Discord bot that bridges text between one Discord channel and a locally connected Meshtastic radio.

## Safety model

- Mesh channel 0 is the only bidirectional channel.
- Incoming nonzero channels (channel 1, `LongFast`, and any additional configured channels) are forwarded to Discord with an explicit `[READ-ONLY]` marker.
- Packets on unknown channels are rejected. No nonzero-channel content is ever used as a Discord-to-mesh transmit request.
- Discord-to-mesh payloads always include the sender's display name and Discord user ID, for example `Alice (123456789): hello`.
- Every received, forwarded, rejected, and transmit attempted/succeeded/failed event is written as one JSONL audit record with a correlation ID.

## Install and configure

Python 3.10+ and a supported USB Meshtastic radio are required. Copy `sampledotenvfile` to `.env`, then set `DISCORD_TOKEN` and `DISCORD_CHANNEL_ID` (the numeric ID of the one permitted Discord channel).

By default the bridge opens `/dev/ttyUSB0`. Override it with `MESHTASTIC_SERIAL_PORT`, or set `MESHTASTIC_HOSTNAME` to use a TCP-connected radio instead.

```sh
python3 -m pip install -r requirements.txt
python3 meshtastic_discord_bridge.py
```

The original commands remain available:

```text
$sendprimary <message>
$send nodenum=########### <message>
$activenodes
```

Messages longer than 225 characters are truncated to fit the Meshtastic text limit. Messages from another Discord channel, the bot itself, and non-send commands are rejected and audited.

## Optional multimon-ng input

Set one of the following. The adapter is intentionally line-oriented and preserves each non-empty decoder line as its payload; it does not assume a particular multimon-ng decoder or output format.

```dotenv
MULTIMON_SOURCE="stdin"
MULTIMON_SOURCE="file:/var/lib/meshtastic/multimon.log"
MULTIMON_SOURCE="command:multimon-ng -a POCSAG512 -t raw -"
```

`MULTIMON_COMMAND` and `MULTIMON_FILE` are equivalent convenience variables. Each decoded line is sent on channel 0 only and is audited like any other transmit.

## Audit log

`AUDIT_LOG_PATH` defaults to `bridge-audit.jsonl`; set it to `-` for stdout. Records contain an ISO-8601 UTC timestamp, event name, correlation ID, and packet/message IDs where available. Use a log rotation policy for long-running deployments.

## Tests

```sh
python3 -m unittest discover -s tests -v
```

## systemd example

See [`systemd/meshtastic-discord-bridge.service.example`](systemd/meshtastic-discord-bridge.service.example). It runs as a dedicated user, uses an explicit working directory, and expects secrets/configuration in `/etc/meshtastic-discord-bridge.env`.

## Screenshot

![Interacting with Meshtastic through Discord](/DiscordScreenshot.png)
