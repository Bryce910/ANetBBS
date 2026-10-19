import asyncio
import unittest
from unittest.mock import AsyncMock

from anetbbs.core.session import BBSSession, CarrierLost
from anetbbs.core.ssh_server import _SshStreamWriter


class _Reader:
    pass


class _ClosedWriter:
    def is_closing(self):
        return True


class _BrokenWriter:
    def is_closing(self):
        return False

    def write(self, data):
        raise BrokenPipeError()


class _BrokenSshWriter:
    async def drain(self):
        raise BrokenPipeError()


class CarrierLossWriteTests(unittest.IsolatedAsyncioTestCase):
    async def test_closed_writer_raises_carrier_lost(self):
        session = BBSSession(_Reader(), _ClosedWriter(), {})
        with self.assertRaises(CarrierLost):
            await session.write('still writing')

    async def test_broken_write_raises_carrier_lost(self):
        session = BBSSession(_Reader(), _BrokenWriter(), {})
        with self.assertRaises(CarrierLost):
            await session.write('still writing')

    async def test_ssh_drain_marks_adapter_closed_and_raises(self):
        writer = _SshStreamWriter(_BrokenSshWriter(), ('127.0.0.1', 1))
        with self.assertRaises(BrokenPipeError):
            await writer.drain()
        self.assertTrue(writer.is_closing())
        with self.assertRaises(BrokenPipeError):
            writer.write(b'again')

    async def test_session_cleanup_swallows_carrier_loss(self):
        session = BBSSession(_Reader(), _ClosedWriter(), {})
        session.init_session = AsyncMock(
            side_effect=CarrierLost('client disconnected'))
        # start() owns the session boundary and must unwind cleanly even when
        # its best-effort goodbye hits the already-closed transport.
        await session.start()


if __name__ == '__main__':
    unittest.main()
