"""Terminal email client for ANetBBS (IMAP inbox + SMTP sending).

Mailbox credentials are encrypted at rest and are never written to logs.
Only TLS-protected IMAP/SMTP connections to public network addresses are
allowed; this keeps the caller-configurable hosts from becoming an SSRF tool.
"""
import asyncio
import base64
import hashlib
import html
import imaplib
import ipaddress
import json
import os
import re
import smtplib
import socket
import ssl
import textwrap
from email import policy
from email.header import decode_header
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import parseaddr
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from .bbs_ui import _app

FG = {'cyan': '\x1b[1;36m', 'yel': '\x1b[1;33m', 'red': '\x1b[1;31m',
      'grn': '\x1b[1;32m', 'gry': '\x1b[0;37m'}
RESET = '\x1b[0m'
MAX_MESSAGES = 20
MAX_BODY = 200_000


def _store_dir():
    app = _app()
    path = Path(app.config.get('MAIL_ACCOUNT_DIR') or
                Path(app.instance_path).parent / 'data' / 'mail_accounts')
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        path.chmod(0o700)
    except OSError:
        pass
    return path


def _key():
    app = _app()
    secret = (app.config.get('MAIL_CREDENTIAL_KEY') or
              app.config.get('SECRET_KEY') or '').encode()
    if not secret:
        raise RuntimeError('MAIL_CREDENTIAL_KEY or SECRET_KEY must be configured')
    return base64.urlsafe_b64encode(hashlib.sha256(b'anetbbs-mail-v1\0' + secret).digest())


def _account_path(user_id):
    return _store_dir() / f'{int(user_id)}.mail'


def _load(user_id):
    path = _account_path(user_id)
    if not path.exists():
        return None
    try:
        return json.loads(Fernet(_key()).decrypt(path.read_bytes()).decode())
    except (InvalidToken, ValueError, OSError, json.JSONDecodeError) as exc:
        raise RuntimeError('Saved mailbox settings could not be decrypted') from exc


def _save(user_id, account):
    path = _account_path(user_id)
    tmp = path.with_suffix('.tmp')
    tmp.write_bytes(Fernet(_key()).encrypt(json.dumps(account).encode()))
    tmp.chmod(0o600)
    os.replace(tmp, path)


def _delete(user_id):
    try:
        _account_path(user_id).unlink()
    except FileNotFoundError:
        pass


def _public_host(host, port):
    if not host or len(host) > 253 or not (1 <= int(port) <= 65535):
        raise ValueError('Invalid mail server or port')
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    if not infos:
        raise ValueError('Mail server did not resolve')
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise ValueError('Mail server must resolve to a public address')


def _decoded(value):
    out = []
    for part, charset in decode_header(value or ''):
        if isinstance(part, bytes):
            out.append(part.decode(charset or 'utf-8', errors='replace'))
        else:
            out.append(part)
    return ''.join(out).replace('\r', ' ').replace('\n', ' ')


def _plain_body(msg):
    part = msg.get_body(preferencelist=('plain', 'html')) if msg.is_multipart() else msg
    if not part:
        return '(No readable message body.)'
    try:
        body = part.get_content()
    except Exception:
        body = part.get_payload(decode=True).decode('utf-8', errors='replace')
    if part.get_content_type() == 'text/html':
        body = re.sub(r'(?is)<(script|style).*?>.*?</\1>', '', body)
        body = re.sub(r'(?i)<br\s*/?>|</p\s*>|</div\s*>', '\n', body)
        body = html.unescape(re.sub(r'(?s)<[^>]+>', '', body))
    return body[:MAX_BODY]


def _imap(account):
    _public_host(account['imap_host'], account['imap_port'])
    client = imaplib.IMAP4_SSL(account['imap_host'], account['imap_port'],
                               ssl_context=ssl.create_default_context(), timeout=20)
    client.login(account['username'], account['password'])
    return client


def _headers(account):
    client = _imap(account)
    try:
        status, _ = client.select('INBOX', readonly=True)
        if status != 'OK':
            raise RuntimeError('Could not open INBOX')
        status, data = client.uid('search', None, 'ALL')
        uids = (data[0].split() if status == 'OK' and data else [])[-MAX_MESSAGES:]
        rows = []
        for uid in reversed(uids):
            status, parts = client.uid('fetch', uid, '(BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])')
            raw = next((p[1] for p in parts or [] if isinstance(p, tuple)), b'')
            msg = BytesParser(policy=policy.default).parsebytes(raw)
            rows.append({'uid': uid.decode(), 'from': _decoded(msg.get('From')),
                         'subject': _decoded(msg.get('Subject')) or '(no subject)',
                         'date': _decoded(msg.get('Date'))})
        return rows
    finally:
        try: client.logout()
        except Exception: pass


def _message(account, uid):
    client = _imap(account)
    try:
        client.select('INBOX', readonly=True)
        status, parts = client.uid('fetch', str(uid), '(BODY.PEEK[])')
        if status != 'OK':
            raise RuntimeError('Message could not be fetched')
        raw = next((p[1] for p in parts or [] if isinstance(p, tuple)), None)
        if not raw:
            raise RuntimeError('Message was empty')
        msg = BytesParser(policy=policy.default).parsebytes(raw)
        return {'from': _decoded(msg.get('From')), 'to': _decoded(msg.get('To')),
                'subject': _decoded(msg.get('Subject')) or '(no subject)',
                'date': _decoded(msg.get('Date')), 'body': _plain_body(msg)}
    finally:
        try: client.logout()
        except Exception: pass


def _send(account, recipient, subject, body):
    _public_host(account['smtp_host'], account['smtp_port'])
    msg = EmailMessage()
    msg['From'] = account['email']
    msg['To'] = recipient
    msg['Subject'] = subject
    msg.set_content(body)
    context = ssl.create_default_context()
    if account['smtp_mode'] == 'ssl':
        smtp = smtplib.SMTP_SSL(account['smtp_host'], account['smtp_port'],
                                context=context, timeout=20)
    else:
        smtp = smtplib.SMTP(account['smtp_host'], account['smtp_port'], timeout=20)
    try:
        smtp.ehlo()
        if account['smtp_mode'] == 'starttls':
            smtp.starttls(context=context)
            smtp.ehlo()
        smtp.login(account['username'], account['password'])
        smtp.send_message(msg)
    finally:
        try: smtp.quit()
        except Exception: smtp.close()


async def _pause(session):
    await session.read_key(f"\r\n{FG['yel']}[Press any key]{RESET}")


async def _setup(session, user_id):
    await session.write(f"\r\n{FG['cyan']}CONNECT A MAILBOX{RESET}\r\n"
                        "Use an app password when your provider supports one.\r\n")
    email_addr = (await session.read_line('Email address: ', max_len=254)).strip()
    if '@' not in email_addr or '\r' in email_addr or '\n' in email_addr:
        await session.write('Invalid email address.\r\n'); return None
    username = (await session.read_line(f'Login [{email_addr}]: ', max_len=254)).strip() or email_addr
    imap_host = (await session.read_line('IMAP host: ', max_len=253)).strip()
    imap_port_raw = (await session.read_line('IMAP TLS port [993]: ', max_len=5)).strip() or '993'
    smtp_host = (await session.read_line('SMTP host: ', max_len=253)).strip()
    smtp_port_raw = (await session.read_line('SMTP port [587]: ', max_len=5)).strip() or '587'
    smtp_mode = (await session.read_line('SMTP security: [T] STARTTLS / [S] implicit TLS: ', max_len=1)).strip().lower()
    smtp_mode = 'ssl' if smtp_mode == 's' else 'starttls'
    password = await session.read_password('Mail app password: ', max_len=512)
    try:
        account = {'email': email_addr, 'username': username, 'password': password,
                   'imap_host': imap_host, 'imap_port': int(imap_port_raw),
                   'smtp_host': smtp_host, 'smtp_port': int(smtp_port_raw),
                   'smtp_mode': smtp_mode}
        await session.write('Testing IMAP login...\r\n')
        await asyncio.wait_for(asyncio.to_thread(_headers, account), timeout=45)
        _save(user_id, account)
        await session.write(f"{FG['grn']}Mailbox connected.{RESET}\r\n")
        return account
    except Exception as exc:
        await session.write(f"{FG['red']}Connection failed: {str(exc)[:160]}{RESET}\r\n")
        return None


async def _compose(session, account, reply=None):
    default_to = parseaddr(reply['from'])[1] if reply else ''
    recipient = (await session.read_line(f'To [{default_to}]: ', max_len=254)).strip() or default_to
    if not parseaddr(recipient)[1] or '\r' in recipient or '\n' in recipient:
        await session.write('Invalid recipient.\r\n'); return
    default_subject = ('Re: ' + reply['subject']) if reply and not reply['subject'].lower().startswith('re:') else (reply['subject'] if reply else '')
    subject = (await session.read_line(f'Subject [{default_subject}]: ', max_len=200)).strip() or default_subject
    await session.write("Enter message. A single '.' on a line sends; /abort cancels.\r\n")
    lines = []
    while len(lines) < 200:
        line = await session.read_line('> ', max_len=998)
        if line == '/abort': return
        if line == '.': break
        lines.append(line)
    if not lines:
        await session.write('Empty message cancelled.\r\n'); return
    try:
        await asyncio.wait_for(asyncio.to_thread(_send, account, recipient, subject, '\n'.join(lines)), timeout=45)
        await session.write(f"{FG['grn']}Message sent.{RESET}\r\n")
    except Exception as exc:
        await session.write(f"{FG['red']}Send failed: {str(exc)[:160]}{RESET}\r\n")


async def run(session):
    """Run the caller's email client until they return to the BBS menu."""
    user_id = int((session.user or {}).get('id') or 0)
    if not user_id:
        await session.write('Login required.\r\n'); return
    try:
        account = _load(user_id)
    except RuntimeError as exc:
        await session.write(f"{FG['red']}{exc}.{RESET}\r\n")
        await _pause(session); return
    if not account:
        account = await _setup(session, user_id)
        if not account:
            await _pause(session); return
    while True:
        await session.write(f"\x1b[2J\x1b[H{FG['cyan']}INTERNET EMAIL{RESET}  {account['email']}\r\n"
                            "[I] Inbox  [C] Compose  [S] Settings  [Q] Return\r\n")
        choice = (await session.read_key('Choice: ')).upper()
        if choice == 'Q': return
        if choice == 'S':
            confirm = (await session.read_key('\r\nDisconnect saved mailbox? (Y/N): ')).upper()
            if confirm == 'Y':
                _delete(user_id); account = await _setup(session, user_id)
                if not account: return
        elif choice == 'C':
            await _compose(session, account); await _pause(session)
        elif choice == 'I':
            await session.write('\r\nLoading inbox...\r\n')
            try:
                rows = await asyncio.wait_for(asyncio.to_thread(_headers, account), timeout=45)
                if not rows:
                    await session.write('(Inbox is empty.)\r\n'); await _pause(session); continue
                for n, row in enumerate(rows, 1):
                    sender = row['from'][:24]
                    subject = row['subject'][:44]
                    await session.write(f'{n:>2}. {sender:<24} {subject}\r\n')
                raw = (await session.read_line('Read # (Enter returns): ', max_len=2)).strip()
                if not raw: continue
                idx = int(raw) - 1
                if not 0 <= idx < len(rows): raise ValueError('No such message')
                item = await asyncio.wait_for(asyncio.to_thread(_message, account, rows[idx]['uid']), timeout=45)
                await session.write(f"\x1b[2J\x1b[HFrom: {item['from']}\r\nTo: {item['to']}\r\nDate: {item['date']}\r\nSubject: {item['subject']}\r\n{'-' * 72}\r\n")
                clean = ''.join(ch if ch in '\r\n\t' or ord(ch) >= 32 else '?' for ch in item['body'])
                for para in clean.replace('\r\n', '\n').split('\n'):
                    await session.write('\r\n'.join(textwrap.wrap(para, 76) or ['']) + '\r\n')
                if (await session.read_key('\r\n[R]eply / any other key returns: ')).upper() == 'R':
                    await _compose(session, account, item)
            except Exception as exc:
                await session.write(f"{FG['red']}Mailbox error: {str(exc)[:160]}{RESET}\r\n")
            await _pause(session)
