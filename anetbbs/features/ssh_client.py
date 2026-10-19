"""Per-user outbound SSH terminal for ANSI BBS callers.

Connection profiles contain only destination metadata and a pinned server
fingerprint. Authentication secrets are entered for each connection and are
never written to the database or logs. Non-SysOp callers may connect only to
globally routable addresses, preventing this feature from becoming an SSRF
bridge into the BBS host's private network.
"""
import asyncio
import ipaddress
import re
import socket

import asyncssh

from .ansi_ui import banner, footer, prompt as _prompt, FG, RESET, BOLD


_HOST_RE = re.compile(r'^[A-Za-z0-9.:-]{1,253}$')
_USER_RE = re.compile(r'^[^\x00-\x20\x7f]{1,128}$')
_ESCAPE_BYTE = 0x1d  # Ctrl+]

# NetBird assigns peers from CGNAT space by default. This host's wt0 interface
# is currently 100.111.0.0/16, contained by this allocation. Keep this deny
# ahead of the SysOp private-network exception: nobody may reach NetBird from
# the BBS SSH client, including administrators.
_NEVER_ALLOWED_NETWORKS = (
    ipaddress.ip_network('100.64.0.0/10'),
)


class _PinnedHostClient(asyncssh.SSHClient):
    def __init__(self, expected=None):
        self.expected = expected
        self.seen = None

    def validate_host_public_key(self, host, addr, port, key):
        self.seen = key.get_fingerprint('sha256')
        return self.expected is not None and self.seen == self.expected


async def resolve_ssh_target(hostname, port, allow_private=False):
    """Resolve once and return the exact address AsyncSSH should connect to."""
    loop = asyncio.get_running_loop()
    rows = await loop.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    addresses = []
    for row in rows:
        address = row[4][0]
        if address not in addresses:
            addresses.append(address)
    if not addresses:
        raise ValueError('Hostname did not resolve.')
    for address in addresses:
        parsed = ipaddress.ip_address(address)
        if any(parsed in network for network in _NEVER_ALLOWED_NETWORKS):
            raise ValueError('NetBird network destinations are blocked for all BBS accounts.')
    if allow_private:
        return addresses[0]
    public = [address for address in addresses
              if ipaddress.ip_address(address).is_global]
    if not public:
        raise ValueError('Private, loopback, and link-local SSH targets are blocked.')
    return public[0]


class UserSshMenu:
    def __init__(self, session):
        self.session = session

    @property
    def user_id(self):
        return int(self.session.user['id'])

    @property
    def is_admin(self):
        return bool(self.session.user.get('is_admin'))

    def _profiles(self):
        from ..models import UserSshProfile, db
        from .bbs_ui import _app
        with _app().app_context():
            db.session.remove()
            return [dict(id=p.id, label=p.label, hostname=p.hostname,
                         port=p.port, remote_username=p.remote_username,
                         fingerprint=p.host_key_fingerprint)
                    for p in UserSshProfile.query.filter_by(user_id=self.user_id)
                    .order_by(UserSshProfile.label).all()]

    async def show_menu(self):
        while True:
            profiles = self._profiles()
            await self.session.write('\x1b[2J\x1b[H')
            await self.session.write(banner('Your SSH Terminals'))
            if profiles:
                for index, profile in enumerate(profiles, 1):
                    trust = 'PINNED' if profile['fingerprint'] else 'NEW'
                    await self.session.write(
                        f"{FG['grn']}  [{index:2d}] {profile['label']:<22} "
                        f"{profile['remote_username']}@{profile['hostname']}:"
                        f"{profile['port']}  [{trust}]{RESET}\r\n")
            else:
                await self.session.write(
                    f"{FG['yel']}  No SSH profiles yet.{RESET}\r\n")
            await self.session.write(footer() + '\r\n')
            await self.session.write(
                f"{FG['cyan']}  [A] Add profile  [D] Delete profile  "
                f"[Q] Back{RESET}\r\n\r\n")
            choice = (await self.session.read_line(_prompt('SSH choice: '))
                      or '').strip().upper()
            if not choice or choice == 'Q':
                return
            if choice == 'A':
                await self._add_profile()
                continue
            if choice == 'D':
                await self._delete_profile(profiles)
                continue
            try:
                profile = profiles[int(choice) - 1]
            except (ValueError, IndexError):
                await self._pause('Invalid choice.', 'red')
                continue
            await self._connect(profile)

    async def _add_profile(self):
        label = (await self.session.read_line('Profile name: ') or '').strip()
        hostname = (await self.session.read_line('SSH hostname: ') or '').strip()
        port_text = (await self.session.read_line('Port [22]: ') or '').strip()
        username = (await self.session.read_line('Remote username: ') or '').strip()
        try:
            port = int(port_text or '22')
        except ValueError:
            return await self._pause('Port must be a number.', 'red')
        if not (1 <= port <= 65535) or not _HOST_RE.fullmatch(hostname):
            return await self._pause('Enter a valid hostname and port.', 'red')
        if not (1 <= len(label) <= 80) or not _USER_RE.fullmatch(username):
            return await self._pause('Enter a profile name and remote username.', 'red')
        try:
            await resolve_ssh_target(hostname, port, self.is_admin)
        except (OSError, ValueError) as exc:
            return await self._pause(str(exc), 'red')

        from ..models import UserSshProfile, db
        from .bbs_ui import _app
        try:
            with _app().app_context():
                db.session.add(UserSshProfile(
                    user_id=self.user_id, label=label, hostname=hostname,
                    port=port, remote_username=username))
                try:
                    db.session.commit()
                except Exception:
                    db.session.rollback()
                    raise
        except Exception:
            return await self._pause('That profile name is already in use.', 'red')
        await self._pause('SSH profile saved.', 'grn')

    async def _delete_profile(self, profiles):
        if not profiles:
            return await self._pause('There are no profiles to delete.', 'yel')
        choice = (await self.session.read_line('Profile number to delete: ')
                  or '').strip()
        try:
            profile = profiles[int(choice) - 1]
        except (ValueError, IndexError):
            return await self._pause('Invalid profile number.', 'red')
        confirm = (await self.session.read_line(
            f"Delete {profile['label']}? [y/N]: ") or '').strip().lower()
        if confirm != 'y':
            return
        from ..models import UserSshProfile, db
        from .bbs_ui import _app
        with _app().app_context():
            row = UserSshProfile.query.filter_by(
                id=profile['id'], user_id=self.user_id).first()
            if row:
                db.session.delete(row)
                db.session.commit()

    async def _discover_fingerprint(self, address, profile):
        client = _PinnedHostClient()
        try:
            await asyncssh.connect(
                address, port=profile['port'], username=profile['remote_username'],
                client_factory=lambda: client, known_hosts=b'', client_keys=[],
                preferred_auth=['none'], connect_timeout=15)
        except (asyncssh.Error, OSError):
            pass
        return client.seen

    def _save_fingerprint(self, profile_id, fingerprint):
        from ..models import UserSshProfile, db
        from .bbs_ui import _app
        with _app().app_context():
            row = UserSshProfile.query.filter_by(
                id=profile_id, user_id=self.user_id).first()
            if not row:
                return False
            row.host_key_fingerprint = fingerprint
            db.session.commit()
            return True

    async def _connect(self, profile):
        try:
            address = await resolve_ssh_target(
                profile['hostname'], profile['port'], self.is_admin)
        except (OSError, ValueError) as exc:
            return await self._pause(str(exc), 'red')

        fingerprint = profile['fingerprint']
        if not fingerprint:
            fingerprint = await self._discover_fingerprint(address, profile)
            if not fingerprint:
                return await self._pause('Could not read the SSH server host key.', 'red')
            await self.session.write(
                f"\r\n{FG['yel']}{BOLD}FIRST CONNECTION — VERIFY HOST KEY{RESET}\r\n"
                f"  {profile['hostname']} fingerprint:\r\n  {fingerprint}\r\n")
            confirm = (await self.session.read_line(
                'Trust and save this fingerprint? [y/N]: ') or '').strip().lower()
            if confirm != 'y' or not self._save_fingerprint(profile['id'], fingerprint):
                return await self._pause('Connection cancelled; host key was not saved.', 'yel')

        password = await self.session.read_password('Remote SSH password: ')
        if not password:
            return await self._pause('Connection cancelled.', 'yel')
        verifier = _PinnedHostClient(fingerprint)
        try:
            connection = await asyncssh.connect(
                address, port=profile['port'], username=profile['remote_username'],
                password=password, client_factory=lambda: verifier,
                known_hosts=b'', client_keys=[], connect_timeout=15)
        except (asyncssh.Error, OSError) as exc:
            password = None
            return await self._pause(f'SSH connection failed: {exc}', 'red')
        password = None
        try:
            cols, rows = getattr(self.session, 'window_size', (80, 24))
            process = await connection.create_process(
                term_type='xterm', term_size=(max(40, cols), max(12, rows)),
                encoding=None)
            await self.session.write(
                f"\r\n{FG['cyan']}Connected. Ctrl+] then Q returns to ANetBBS."
                f"{RESET}\r\n")
            await self._proxy(process)
        except (asyncssh.Error, OSError) as exc:
            await self.session.write(f"\r\nSSH session ended: {exc}\r\n")
        finally:
            connection.close()
            await connection.wait_closed()
        await self._pause('[Returned to ANetBBS]', 'cyan')

    async def _proxy(self, process):
        stop = asyncio.Event()

        async def remote_to_user():
            try:
                while not stop.is_set():
                    data = await process.stdout.read(4096)
                    if not data:
                        break
                    self.session.writer.write(data)
                    await self.session.writer.drain()
            finally:
                stop.set()

        async def user_to_remote():
            escaping = False
            try:
                while not stop.is_set():
                    data = await self.session.read_raw(1)
                    if not data:
                        break
                    byte = data[0] if isinstance(data, (bytes, bytearray)) else ord(data)
                    if escaping:
                        escaping = False
                        if byte in (ord('q'), ord('Q')):
                            break
                        continue
                    if byte == _ESCAPE_BYTE:
                        escaping = True
                        continue
                    process.stdin.write(bytes([byte]))
                    await process.stdin.drain()
            finally:
                stop.set()
                try:
                    process.stdin.write_eof()
                except Exception:
                    pass

        tasks = [asyncio.create_task(remote_to_user()),
                 asyncio.create_task(user_to_remote())]
        await stop.wait()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _pause(self, message, color='cyan'):
        await self.session.write(f"\r\n{FG[color]}{message}{RESET}\r\n")
        await self.session.read_key('Press any key...')
