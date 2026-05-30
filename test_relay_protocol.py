import unittest

from relay_protocol import (
    ProtocolError,
    build_read_status_frame,
    build_write_single_frame,
    make_status_reply_for_test,
    parse_status_reply,
    parse_write_single_reply,
)


class RelayProtocolTest(unittest.TestCase):
    def test_build_read_status_frame(self) -> None:
        frame = build_read_status_frame(1)

        self.assertEqual(frame, bytes.fromhex("48 3A 01 53 00 00 00 00 00 00 00 00 D6 45 44"))

    def test_build_write_single_on_frame(self) -> None:
        frame = build_write_single_frame(1, relay_number=2, turn_on=True)

        self.assertEqual(frame, bytes.fromhex("48 3A 01 70 02 01 00 00 45 44"))

    def test_parse_write_single_reply_accepts_on_reply_command(self) -> None:
        frame = bytearray(build_write_single_frame(1, relay_number=2, turn_on=True))
        frame[3] = 0x71

        parse_write_single_reply(
            bytes(frame),
            expected_address=1,
            expected_relay_number=2,
            expected_state=True,
        )

    def test_parse_write_single_reply_accepts_off_reply_command(self) -> None:
        frame = bytearray(build_write_single_frame(1, relay_number=2, turn_on=False))
        frame[3] = 0x71

        parse_write_single_reply(
            bytes(frame),
            expected_address=1,
            expected_relay_number=2,
            expected_state=False,
        )

    def test_parse_write_single_reply_rejects_echo_command(self) -> None:
        frame = build_write_single_frame(1, relay_number=2, turn_on=True)

        with self.assertRaisesRegex(ProtocolError, "Unexpected write reply command"):
            parse_write_single_reply(
                frame,
                expected_address=1,
                expected_relay_number=2,
                expected_state=True,
            )

    def test_parse_status_reply(self) -> None:
        frame = make_status_reply_for_test(1, relay1=True, relay2=False)

        status = parse_status_reply(frame, expected_address=1)

        self.assertIs(status.relay1, True)
        self.assertIs(status.relay2, False)

    def test_parse_rejects_bad_checksum(self) -> None:
        frame = bytearray(make_status_reply_for_test(1, relay1=True, relay2=False))
        frame[12] ^= 0xFF

        with self.assertRaisesRegex(ProtocolError, "Checksum mismatch"):
            parse_status_reply(bytes(frame), expected_address=1)

    def test_parse_rejects_address_mismatch(self) -> None:
        frame = make_status_reply_for_test(2, relay1=True, relay2=False)

        with self.assertRaisesRegex(ProtocolError, "does not match"):
            parse_status_reply(frame, expected_address=1)


if __name__ == "__main__":
    unittest.main()
