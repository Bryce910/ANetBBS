"""Unit tests for the PETSCII terminal-mode session plumbing added to
anetbbs.core.session.BBSSession (see the "PETSCII Terminal Support
(Phase 1)" plan): forced_term_mode/forced_width overrides, the
term_mode/petscii_width properties, and write()'s PETSCII branch
(translate ANSI color codes to real PETSCII color bytes via
petscii_codec.ansi_to_petscii(), then encode via petscii_codec instead
of cp437 -- see tests/test_petscii_codec.py's AnsiToPetsciiTests for
thorough coverage of the translation itself; this file just confirms
session.write() actually calls it).

login_screen()'s and the post-login dispatch's PETSCII branches are
exercised in tests/test_petscii_ui.py against the lighter-weight
run_petscii_menu() stub instead of a full BBSSession, since driving
login_screen()/handle_login() end-to-end needs a real DB-backed
UserManager -- out of scope for this pure-plumbing test file.
"""
import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from anetbbs.core.session import BBSSession


class _FakeWriter:
    def __init__(self):
        self.written = bytearray()

    def write(self, data):
        self.written += data

    async def drain(self):
        pass

    def close(self):
        pass


class _FakeReader:
    def __init__(self, *chunks):
        self.chunks = list(chunks)

    async def read(self, _n=-1):
        return self.chunks.pop(0) if self.chunks else b''


def _make_session(**kwargs):
    reader = object()  # write() never touches reader
    writer = _FakeWriter()
    session = BBSSession(reader, writer, config={}, **kwargs)
    return session, writer


class ForcedTermModeTests(unittest.TestCase):
    def test_forced_term_mode_overrides_negotiation(self):
        session, _ = _make_session(forced_term_mode='petscii')
        self.assertEqual(session.term_mode, 'petscii')

    def test_no_override_falls_back_to_normal_detection(self):
        session, _ = _make_session()
        self.assertEqual(session.term_mode, 'ansi')  # default, no TTYPE negotiated

    def test_petscii_width_defaults_to_40(self):
        session, _ = _make_session(forced_term_mode='petscii')
        self.assertEqual(session.petscii_width, 40)

    def test_petscii_width_honors_forced_width(self):
        session, _ = _make_session(forced_term_mode='petscii', forced_width=80)
        self.assertEqual(session.petscii_width, 80)

    def test_petscii_width_meaningless_outside_petscii_mode_still_has_a_value(self):
        session, _ = _make_session()
        self.assertEqual(session.petscii_width, 40)  # never raises


class SmartTerminalTests(unittest.TestCase):
    def test_known_terminal_types_are_classified_conservatively(self):
        classify = BBSSession._mode_from_terminal_type
        self.assertEqual(classify('CCGMS'), 'petscii')
        self.assertEqual(classify('xterm-256color'), 'ansi')
        self.assertEqual(classify('dumb'), 'ascii')
        self.assertIsNone(classify('UNKNOWN'))
        self.assertIsNone(classify('mystery-terminal'))

    def test_manual_c_selects_40_column_petscii_and_passes_gate(self):
        writer = _FakeWriter()
        session = BBSSession(_FakeReader(b'C'), writer, config={},
                             smart_terminal=True)
        asyncio.run(session._select_smart_terminal())
        self.assertEqual(session.term_mode, 'petscii')
        self.assertEqual(session.petscii_width, 40)
        self.assertTrue(session._terminal_selector_passed_gate)
        self.assertIn(b'SELECT TERMINAL', bytes(writer.written))

    def test_manual_8_selects_80_column_petscii(self):
        writer = _FakeWriter()
        session = BBSSession(_FakeReader(b'8'), writer, config={},
                             smart_terminal=True)
        asyncio.run(session._select_smart_terminal())
        self.assertEqual(session.term_mode, 'petscii')
        self.assertEqual(session.petscii_width, 80)

    def test_detected_ansi_skips_selector(self):
        writer = _FakeWriter()
        session = BBSSession(_FakeReader(), writer, config={},
                             smart_terminal=True)
        session.terminal_type = 'SyncTERM'
        asyncio.run(session._select_smart_terminal())
        self.assertEqual(session.term_mode, 'ansi')
        self.assertNotIn(b'SELECT TERMINAL', bytes(writer.written))


class WritePetsciiBranchTests(unittest.TestCase):
    def test_plain_text_routes_through_petscii_codec(self):
        # PETSCII's upper/lowercase charset inverts ASCII's letter-case
        # byte assignment (confirmed on real hardware -- see
        # petscii_codec.py's docstring), so 'Hello, World!' comes out
        # with every letter's case swapped, not identity-mapped.
        session, writer = _make_session(forced_term_mode='petscii')
        asyncio.run(session.write('Hello, World!'))
        self.assertEqual(bytes(writer.written), b'hELLO, wORLD!')

    def test_ansi_sgr_color_codes_are_translated_not_stripped(self):
        # SGR 1;33 = bold + ANSI brown(33) = index 11 = PETSCII yellow
        # (0x9E); SGR 0 resets to the default index 7 = light-gray
        # (0x9B). See test_petscii_codec.py's AnsiToPetsciiTests for the
        # full color-mapping table this is exercising end-to-end.
        session, writer = _make_session(forced_term_mode='petscii')
        asyncio.run(session.write('\x1b[1;33mHello\x1b[0m'))
        self.assertEqual(bytes(writer.written), bytes([0x9E]) + b'hELLO' + bytes([0x9B]))

    def test_non_sgr_ansi_escape_sequences_are_still_stripped(self):
        # Cursor moves/erase/etc. have no PETSCII equivalent and are
        # still dropped entirely, same as before this feature existed.
        session, writer = _make_session(forced_term_mode='petscii')
        asyncio.run(session.write('\x1b[2JHello'))
        self.assertEqual(bytes(writer.written), b'hELLO')

    def test_control_code_characters_reach_the_wire_as_real_petscii_bytes(self):
        from anetbbs.features import petscii_codec as pc
        session, writer = _make_session(forced_term_mode='petscii')
        asyncio.run(session.write(f'{pc.CLR_HOME}Hi'))
        self.assertEqual(bytes(writer.written), bytes([0x93]) + b'hI')

    def test_bytes_input_also_translates_ansi_color(self):
        # write() has a separate bytes-input branch (used when raw
        # already-encoded bytes are written directly) that must apply
        # the same ansi_to_petscii() translation, not just the str path.
        session, writer = _make_session(forced_term_mode='petscii')
        asyncio.run(session.write(b'\x1b[31mHi\x1b[0m'))
        self.assertEqual(bytes(writer.written), bytes([0x1C]) + b'hI' + bytes([0x9B]))

    def test_non_petscii_session_still_uses_cp437_encode(self):
        session, writer = _make_session()  # default term_mode == 'ansi'
        asyncio.run(session.write('Hello'))
        self.assertEqual(bytes(writer.written), b'Hello')  # ASCII subset, same either way

    def test_ascii_mode_unaffected_by_petscii_branch(self):
        # Regression guard: adding the petscii branch must not change the
        # existing 'ascii' mode's own ANSI-stripping/cp437 behavior.
        session, writer = _make_session()
        session.terminal_type = 'dumb'
        self.assertEqual(session.term_mode, 'ascii')
        asyncio.run(session.write('\x1b[1;33mHello\x1b[0m'))
        self.assertEqual(bytes(writer.written), b'Hello')


if __name__ == '__main__':
    unittest.main()
