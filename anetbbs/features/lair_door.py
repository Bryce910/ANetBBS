"""8BIT BBS progression/adventure door for ANSI and PETSCII sessions."""
import asyncio
import hashlib
import hmac
import json
import os
import random
import secrets
import sqlite3
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

GAME_DB = Path('/opt/anetbbs/data/8bit_lair.db')
BBS_DB = Path('/opt/anetbbs/data/anetbbs.db')
SECRET_FILE = Path('/opt/anetbbs/data/rewards.secret')
REWARD_URL = os.getenv('LAIR_REWARD_URL', 'https://8bitlair.net/php/public/bbs-rewards.php')
REFERENCE_LIBRARY = Path('/opt/repos/8bit/data/app/php/public/assets/reference-library.json')

ROOMS = {
    'lobby': ('THE LOBBY', 'A cyan cursor blinks over a wall of caller handles.', {'n': 'lab', 'e': 'arcade', 'w': 'tunnel'}),
    'lab': ('COMMODORE LAB', 'Warm CRTs surround a C64. A note reads: POKE 53280,0.', {'s': 'lobby', 'e': 'signal'}),
    'signal': ('SIGNAL ROOM', 'A receiver sweeps forgotten frequencies. Something pulses at 8.064 MHz.', {'w': 'lab', 's': 'archive'}),
    'archive': ('FILE ARCHIVES', 'Disk boxes vanish into darkness. One spine is marked 1541.', {'n': 'signal', 'w': 'deep'}),
    'arcade': ('ARCADE VAULT', 'High scores glow in amber. The cabinet waits for one more credit.', {'w': 'lobby', 'n': 'radio'}),
    'radio': ('LAIR RADIO', 'SID voices leak from a patched-together transmitter.', {'s': 'arcade'}),
    'tunnel': ('GRAFFITI TUNNEL', 'PETSCII tags cover the brickwork. One says SEARCH THE STATIC.', {'e': 'lobby', 'n': 'deep'}),
    'deep': ('THE DEEP LAIR', 'A hidden node hums beneath the floor. Elite callers leave messages here.', {'e': 'archive', 's': 'tunnel'}),
}

SECRETS = {
    'lab': ('border_zero', 125, 'BORDER ZERO', 'The C64 border snaps to black. A compartment opens.'),
    'signal': ('eight_megahertz', 175, 'GHOST FREQUENCY', 'You tune 8.064 MHz and capture a ghost packet.'),
    'archive': ('disk_1541', 150, 'DIRECTORY SLEUTH', 'The 1541 box contains an unreleased directory listing.'),
    'tunnel': ('static_search', 100, 'STATIC WALKER', 'Behind the static is a tiny phosphor key.'),
    'deep': ('deep_core', 300, 'LAIR GUARDIAN', 'The hidden node recognizes your carrier signature.'),
}

RADIO = [
    ('Rob Hubbard', 'Monty on the Run', '6581 anthem transmission'),
    ('Martin Galway', 'Comic Bakery', 'three-voice earworm'),
    ('Jeroen Tel', 'Cybernoid II', 'Maniacs of Noise signal'),
    ('Ben Daglish', 'The Last Ninja', 'courtyard after midnight'),
    ('Chris Huelsbeck', 'The Great Giana Sisters', 'dream-sequence carrier'),
]

RANKS = [
    (0, 'LURKER', 10), (250, 'CALLER', 20), (750, 'OPERATOR', 30),
    (1500, 'CRACKER', 40), (3000, 'ELITE', 50), (6000, 'LAIR GUARDIAN', 60),
]


def connect_game():
    db = sqlite3.connect(GAME_DB, timeout=15)
    db.row_factory = sqlite3.Row
    db.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS players (
          user_id INTEGER PRIMARY KEY, username TEXT NOT NULL, room TEXT NOT NULL DEFAULT 'lobby',
          bonus_bytes INTEGER NOT NULL DEFAULT 0, website_handle TEXT,
          first_seen TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS discoveries (
          user_id INTEGER NOT NULL, code TEXT NOT NULL, earned_bytes INTEGER NOT NULL,
          discovered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(user_id, code)
        );
        CREATE TABLE IF NOT EXISTS weekly_scores (
          week TEXT NOT NULL, user_id INTEGER NOT NULL, username TEXT NOT NULL, score INTEGER NOT NULL,
          submitted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, PRIMARY KEY(week, user_id)
        );
        CREATE TABLE IF NOT EXISTS reward_claims (
          claim_id TEXT PRIMARY KEY, user_id INTEGER NOT NULL, username TEXT NOT NULL,
          website_handle TEXT NOT NULL, tier TEXT NOT NULL, bytes INTEGER NOT NULL,
          status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
    ''')
    return db


def bbs_stats(user_id):
    db = sqlite3.connect(BBS_DB, timeout=15)
    row = db.execute('SELECT login_count, access_level FROM users WHERE id=?', (user_id,)).fetchone() or (0, 10)
    posts = db.execute('SELECT COUNT(*) FROM posts WHERE author_id=?', (user_id,)).fetchone()[0]
    badges = db.execute('SELECT COUNT(*) FROM user_achievements WHERE user_id=?', (user_id,)).fetchone()[0]
    return int(row[0] or 0), int(posts), int(badges), int(row[1] or 10)


def award(user_id, code):
    db = sqlite3.connect(BBS_DB, timeout=15)
    row = db.execute('SELECT id FROM achievements WHERE code=? AND is_active=1', (code,)).fetchone()
    if row:
        db.execute('INSERT OR IGNORE INTO user_achievements(user_id,achievement_id,earned_at) VALUES(?,?,CURRENT_TIMESTAMP)', (user_id, row[0]))
        db.commit()


def snapshot(user_id, username):
    game = connect_game()
    game.execute('INSERT OR IGNORE INTO players(user_id, username) VALUES(?,?)', (user_id, username))
    game.execute('UPDATE players SET username=?, updated_at=CURRENT_TIMESTAMP WHERE user_id=?', (username, user_id))
    game.commit()
    player = game.execute('SELECT * FROM players WHERE user_id=?', (user_id,)).fetchone()
    found = game.execute('SELECT COUNT(*) FROM discoveries WHERE user_id=?', (user_id,)).fetchone()[0]
    login_count, posts, badges, current_access = bbs_stats(user_id)
    total = login_count * 5 + posts * 15 + badges * 50 + int(player['bonus_bytes'])
    rank, access = RANKS[0][1], RANKS[0][2]
    for floor, label, level in RANKS:
        if total >= floor:
            rank, access = label, level
    if access > current_access:
        bbs = sqlite3.connect(BBS_DB, timeout=15)
        bbs.execute('UPDATE users SET access_level=? WHERE id=? AND is_admin=0', (access, user_id))
        bbs.commit()
    return game, player, total, rank, found


def week_key():
    now = datetime.now(timezone.utc)
    year, week, _ = now.isocalendar()
    return f'{year}-W{week:02d}'


def send_claim(payload):
    secret = SECRET_FILE.read_bytes().strip()
    body = json.dumps(payload, separators=(',', ':'), sort_keys=True).encode()
    signature = hmac.new(secret, body, hashlib.sha256).hexdigest()
    req = urllib.request.Request(REWARD_URL, body, method='POST', headers={
        'Content-Type': 'application/json', 'X-8Bit-Signature': signature,
        'Host': '8bitlair.net', 'User-Agent': '8BIT-BBS-Rewards/1.0',
    })
    with urllib.request.urlopen(req, timeout=5) as response:
        return json.loads(response.read().decode())


async def run(session, user_id, username):
    async def out(text=''):
        await session.write(text.replace('\n', '\r\n') + ('\r\n' if text else ''))

    async def ask(prompt):
        value = await session.read_line(prompt)
        return (value or '').strip()

    game, player, total, rank, found = snapshot(user_id, username)
    award(user_id, 'lair_explorer')
    await out('\x1b[2J\x1b[H\x1b[1;36m+--------------------------------------+')
    await out('|       8BIT UNDERGROUND NETWORK       |')
    await out('+--------------------------------------+\x1b[0m')
    await out(f'CALLER: {username}  RANK: {rank}  BYTES: {total}')
    await out('Type HELP. Explore. Search everything. Trust no cursor.')
    room = player['room'] if player['room'] in ROOMS else 'lobby'

    while True:
        title, description, exits = ROOMS[room]
        await out(f'\n\x1b[1;35m[{title}]\x1b[0m')
        await out(description)
        await out('EXITS: ' + ' '.join(k.upper() for k in exits))
        command = (await ask('\r\n8BIT> ')).lower()
        if not command:
            continue
        bits = command.split()
        verb = bits[0]
        if verb in ('q', 'quit', 'exit'):
            await out('NO CARRIER -- progress saved.')
            break
        if verb in ('n', 's', 'e', 'w', 'north', 'south', 'east', 'west'):
            direction = verb[0]
            target = exits.get(direction)
            if not target:
                await out('A SOLID WALL OF STATIC BLOCKS THAT PATH.')
                continue
            if target == 'deep' and total < 3000:
                await out('ACCESS DENIED. ELITE RANK (3000 BYTES) REQUIRED.')
                continue
            room = target
            game.execute('UPDATE players SET room=?, updated_at=CURRENT_TIMESTAMP WHERE user_id=?', (room, user_id))
            game.commit()
            continue
        if verb in ('look', 'l'):
            continue
        if verb == 'search':
            secret = SECRETS.get(room)
            if not secret:
                await out('ONLY DUST, CABLES, AND OLD PACKETS.')
                continue
            code, value, badge, message = secret
            before = game.total_changes
            game.execute('INSERT OR IGNORE INTO discoveries(user_id,code,earned_bytes) VALUES(?,?,?)', (user_id, code, value))
            if game.total_changes > before:
                game.execute('UPDATE players SET bonus_bytes=bonus_bytes+? WHERE user_id=?', (value, user_id))
                game.commit()
                total += value
                await out(f'\x1b[1;33mSECRET FOUND: {badge}  +{value} BYTES\x1b[0m')
                await out(message)
                award(user_id, 'secret_hunter')
                if code == 'deep_core':
                    award(user_id, 'elite_clearance')
            else:
                await out('YOU ALREADY EXTRACTED THIS SECRET.')
            continue
        if verb in ('status', 'rank'):
            game, player, total, rank, found = snapshot(user_id, username)
            await out(f'RANK: {rank} | BYTES: {total} | SECRETS: {found}/{len(SECRETS)}')
            await out('NEXT: ' + next((f'{label} AT {floor}' for floor, label, _ in RANKS if floor > total), 'MAXIMUM CLEARANCE'))
            continue
        if verb == 'map':
            await out('LAB--SIGNAL\n |      |\nLOBBY  ARCHIVE--DEEP\n |  \\          /\nTUNNEL  ARCADE--RADIO')
            continue
        if verb in ('challenge', 'scores'):
            week = week_key()
            await out(f'WEEKLY CHALLENGE {week}: ONE-CREDIT RIVER RAID')
            await out('Submit an honest score with: SCORE 12345')
            rows = game.execute('SELECT username,score FROM weekly_scores WHERE week=? ORDER BY score DESC LIMIT 10', (week,)).fetchall()
            for i, row in enumerate(rows, 1):
                await out(f'{i:2}. {row["username"][:16]:16} {row["score"]:8}')
            continue
        if verb == 'score' and len(bits) == 2 and bits[1].isdigit():
            score = min(int(bits[1]), 99999999)
            week = week_key()
            old = game.execute('SELECT score FROM weekly_scores WHERE week=? AND user_id=?', (week, user_id)).fetchone()
            if old and score <= old['score']:
                await out(f'YOUR EXISTING SCORE OF {old["score"]} IS HIGHER.')
            else:
                game.execute('INSERT INTO weekly_scores(week,user_id,username,score) VALUES(?,?,?,?) ON CONFLICT(week,user_id) DO UPDATE SET username=excluded.username,score=excluded.score,submitted_at=CURRENT_TIMESTAMP', (week, user_id, username, score))
                game.commit()
                await out('SCORE TRANSMITTED. SYSOP MAY REQUEST PROOF.')
                award(user_id, 'weekly_challenger')
            continue
        if verb == 'radio':
            artist, title, note = random.choice(RADIO)
            await out(f'NOW TRANSMITTING: {artist} -- {title}')
            await out(f'SIGNAL NOTES: {note}')
            await out('Tune the browser archive to HVSC for the full SID stream.')
            continue
        if verb in ('library', 'resources', 'reference'):
            try:
                resources = json.loads(REFERENCE_LIBRARY.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                await out('PUBLIC LIBRARY INDEX OFFLINE. TRY THE WEBSITE ARCHIVE.')
                continue
            wanted = bits[1].lower() if len(bits) > 1 else ''
            matches = [item for item in resources if not wanted or item.get('category', '').lower().startswith(wanted)]
            await out(f'PUBLIC REFERENCE LIBRARY -- {len(matches)} RECORDS')
            await out('FILTER: LIBRARY GAMES | LIBRARY ART | LIBRARY SOUNDS')
            for item in matches:
                await out(f'[{item.get("category", "?").upper()}] {item.get("title", "UNTITLED")} -- {item.get("license", "CHECK LICENSE")}')
                await out(f'  {item.get("url", "") }')
            await out('FULL NOTES + LICENSE LINKS: https://8bitlair.net/?page=resources')
            continue
        if verb == 'redeem':
            if len(bits) < 2:
                await out('USE: REDEEM <YOUR-WEBSITE-HANDLE>')
                continue
            website_handle = bits[1][:18]
            if total < 1000:
                await out('1000 BYTES REQUIRED TO ENTER THE GIFT-CARD REWARD QUEUE.')
                continue
            tier = 'GOLD' if total >= 5000 else ('SILVER' if total >= 2500 else 'BRONZE')
            claim_id = '8BL-' + secrets.token_hex(6).upper()
            payload = {'claim_id': claim_id, 'bbs_user_id': user_id, 'bbs_username': username,
                       'website_handle': website_handle, 'tier': tier, 'bytes': total,
                       'timestamp': int(time.time()), 'nonce': secrets.token_hex(12)}
            try:
                result = await asyncio.to_thread(send_claim, payload)
                game.execute('INSERT INTO reward_claims(claim_id,user_id,username,website_handle,tier,bytes) VALUES(?,?,?,?,?,?)',
                             (claim_id, user_id, username, website_handle, tier, total))
                game.execute('UPDATE players SET website_handle=? WHERE user_id=?', (website_handle, user_id))
                game.commit()
                await out(f'REWARD CLAIM QUEUED: {result.get("claim_id", claim_id)}')
                await out(f'ATTRIBUTED TO WEBSITE HANDLE @{website_handle}. SYSOP APPROVAL REQUIRED.')
                award(user_id, 'reward_signal')
            except Exception as exc:
                await out(f'REWARD LINK OFFLINE. CLAIM NOT SPENT: {type(exc).__name__}')
            continue
        if verb == 'help':
            await out('N/S/E/W  LOOK  SEARCH  MAP  STATUS  CHALLENGE  SCORE <N>')
            await out('RADIO  LIBRARY [GAMES|ART|SOUNDS]  REDEEM <WEBSITE-HANDLE>')
            await out('HELP  QUIT')
            continue
        await out('UNKNOWN COMMAND. TYPE HELP.')
