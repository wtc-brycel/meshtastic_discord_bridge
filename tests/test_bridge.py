import io
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from meshtastic_discord_bridge import (
    AuditLogger,
    Config,
    authorize_discord_message,
    classify_mesh_channel,
    format_discord_message,
    format_mesh_message,
    mesh_channel,
    parse_multimon_line,
    MultimonLineSource,
)


def packet(channel, text="hello"):
    return {
        "id": 42,
        "channel": channel,
        "fromId": "!sender",
        "toId": "^all",
        "decoded": {"portnum": "TEXT_MESSAGE_APP", "text": text},
    }


class BridgePolicyTests(unittest.TestCase):
    def test_serial_default_and_channel_configuration(self):
        with patch.dict(os.environ, {"DISCORD_TOKEN": "token", "DISCORD_CHANNEL_ID": "99"}, clear=True):
            config = Config.from_env()
        self.assertEqual(config.discord_channel_id, 99)
        self.assertEqual(config.serial_port, "/dev/ttyUSB0")

    def test_channel_filtering(self):
        self.assertEqual(mesh_channel(packet(0)), 0)
        self.assertEqual(mesh_channel(packet("LongFast")), 1)
        self.assertEqual(classify_mesh_channel(packet(0)), "primary")
        self.assertEqual(classify_mesh_channel(packet("LongFast")), "read_only")
        self.assertEqual(classify_mesh_channel(packet(2)), "rejected")

    def test_read_only_marker_is_explicit(self):
        self.assertNotIn("READ-ONLY", format_mesh_message(packet(0)))
        self.assertIn("[READ-ONLY]", format_mesh_message(packet("LongFast")))

    def test_discord_format_has_name_and_id(self):
        message = SimpleNamespace(
            author=SimpleNamespace(display_name="Alice", name="alice", id=123),
            content="hello",
        )
        self.assertEqual(format_discord_message(message), "Alice (123): hello")

    def test_authorization_requires_configured_channel_and_not_bot(self):
        author = SimpleNamespace(display_name="Alice", id=123)
        good = SimpleNamespace(author=author, channel=SimpleNamespace(id=99))
        self.assertEqual(authorize_discord_message(good, 99, 999)[0], True)
        self.assertEqual(authorize_discord_message(good, 100, 999), (False, "wrong_discord_channel"))
        self.assertEqual(authorize_discord_message(good, 99, 123), (False, "bot_message"))


class MultimonTests(unittest.TestCase):
    def test_decoder_line_parsing_preserves_format(self):
        self.assertEqual(parse_multimon_line("  POCSAG512: Address: 12 Alpha: hi\n"),
                         "POCSAG512: Address: 12 Alpha: hi")
        self.assertEqual(parse_multimon_line("\x1b[32mPOCSAG: hi\x1b[0m"), "POCSAG: hi")
        self.assertIsNone(parse_multimon_line("  \n"))

    def test_line_source_skips_blank_lines(self):
        source = MultimonLineSource("stdin", stdin=io.StringIO("\nfirst\n\nsecond\n"))
        self.assertEqual(list(source), ["first", "second"])
        source.close()


class AuditTests(unittest.TestCase):
    def test_jsonl_has_correlation_and_ids(self):
        output = io.StringIO()
        logger = AuditLogger(stream=output)
        correlation = logger.log("received", packet_id=42, message_id=7, direction="test")
        record = json.loads(output.getvalue())
        self.assertEqual(record["event"], "received")
        self.assertEqual(record["correlation_id"], correlation)
        self.assertEqual(record["packet_id"], "42")
        self.assertEqual(record["message_id"], "7")


if __name__ == "__main__":
    unittest.main()
