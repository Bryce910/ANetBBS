import unittest
from unittest.mock import AsyncMock, patch

from anetbbs.features.dialout import DialoutMenu


class _Session:
    def __init__(self):
        self.write = AsyncMock()
        self.read_line = AsyncMock(side_effect=['C', 'Q'])


class DialoutDirectoryOnlyTests(unittest.IsolatedAsyncioTestCase):
    async def test_custom_destination_hotkey_cannot_connect(self):
        session = _Session()
        menu = DialoutMenu(session)
        menu._load_directory = lambda: [
            ('Approved BBS', 'approved.example', 23, 'telnet')]
        menu._connect = AsyncMock()

        with patch('anetbbs.features.dialout.write_menu_art',
                   new=AsyncMock(return_value=False)):
            await menu.show_menu()

        menu._connect.assert_not_awaited()
        prompts = [call.args[0] for call in session.read_line.await_args_list]
        self.assertNotIn('Hostname: ', prompts)

    def test_custom_destination_method_was_removed(self):
        self.assertFalse(hasattr(DialoutMenu, '_custom_destination'))
