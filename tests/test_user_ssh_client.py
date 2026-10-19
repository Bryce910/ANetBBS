import asyncio
import ipaddress
import unittest
from unittest.mock import patch

from anetbbs.features.ssh_client import resolve_ssh_target
from anetbbs.features import menu_engine
from anetbbs.cfg.sections.menu import ACTION_TYPE_CHOICES


class UserSshClientTests(unittest.TestCase):
    def test_action_is_registered(self):
        self.assertIn('ssh_client', menu_engine._ACTIONS)
        self.assertIn('ssh_client', ACTION_TYPE_CHOICES)

    def test_private_target_is_blocked_for_regular_users(self):
        row = (None, None, None, None, ('127.0.0.1', 22))
        loop = asyncio.new_event_loop()
        self.addCleanup(loop.close)
        with patch.object(loop, 'getaddrinfo', return_value=[row]):
            with self.assertRaisesRegex(ValueError, 'blocked'):
                loop.run_until_complete(resolve_ssh_target('localhost', 22))

    def test_private_target_is_allowed_for_sysop(self):
        row = (None, None, None, None, ('10.0.0.5', 22))
        loop = asyncio.new_event_loop()
        self.addCleanup(loop.close)
        with patch.object(loop, 'getaddrinfo', return_value=[row]):
            result = loop.run_until_complete(
                resolve_ssh_target('server.lan', 22, allow_private=True))
        self.assertEqual(result, '10.0.0.5')

    def test_netbird_target_is_blocked_even_for_sysop(self):
        row = (None, None, None, None, ('100.111.127.14', 22))
        loop = asyncio.new_event_loop()
        self.addCleanup(loop.close)
        with patch.object(loop, 'getaddrinfo', return_value=[row]):
            with self.assertRaisesRegex(ValueError, 'NetBird.*blocked'):
                loop.run_until_complete(resolve_ssh_target(
                    'netbird-peer.internal', 22, allow_private=True))

    def test_any_dns_answer_on_netbird_rejects_the_whole_target(self):
        rows = [
            (None, None, None, None, ('1.1.1.1', 22)),
            (None, None, None, None, ('100.111.9.8', 22)),
        ]
        loop = asyncio.new_event_loop()
        self.addCleanup(loop.close)
        with patch.object(loop, 'getaddrinfo', return_value=rows):
            with self.assertRaisesRegex(ValueError, 'NetBird.*blocked'):
                loop.run_until_complete(resolve_ssh_target(
                    'mixed-answer.example', 22, allow_private=True))

    def test_public_target_is_allowed(self):
        address = str(ipaddress.ip_address('203.0.113.8'))
        row = (None, None, None, None, (address, 22))
        loop = asyncio.new_event_loop()
        self.addCleanup(loop.close)
        with patch.object(loop, 'getaddrinfo', return_value=[row]):
            # Documentation ranges aren't globally routable, so use a real
            # public resolver address only in the mocked result.
            row = (None, None, None, None, ('1.1.1.1', 22))
            with patch.object(loop, 'getaddrinfo', return_value=[row]):
                result = loop.run_until_complete(resolve_ssh_target('example', 22))
        self.assertEqual(result, '1.1.1.1')
