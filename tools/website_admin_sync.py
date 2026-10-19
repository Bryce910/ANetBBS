#!/usr/bin/env python3
import hashlib, hmac, json, sqlite3, time, urllib.request
from pathlib import Path

DB = Path('/opt/anetbbs/data/anetbbs.db')
SECRET = Path('/opt/anetbbs/data/rewards.secret')
URL = 'https://8bitlair.net/php/public/bbs-sync.php'

db = sqlite3.connect(DB, timeout=10)
db.row_factory = sqlite3.Row
users = [dict(row) for row in db.execute(
    '''SELECT id,username,email,created_at,last_login,COALESCE(login_count,0) login_count,
       COALESCE(is_active,0) is_active,COALESCE(is_admin,0) is_admin,
       COALESCE(is_verified,0) is_verified,COALESCE(is_locked,0) is_locked,
       COALESCE(access_level,0) access_level FROM users ORDER BY created_at DESC LIMIT 1000''')]
notifications = [dict(row) for row in db.execute(
    '''SELECT n.id,n.kind,COALESCE(n.title,'') title,COALESCE(n.body,'') body,
       COALESCE(n.target_url,'') target_url,n.created_at FROM notifications n
       JOIN users u ON u.id=n.user_id WHERE u.is_admin=1 AND COALESCE(n.is_read,0)=0
       ORDER BY n.created_at DESC LIMIT 500''')]
online = db.execute("SELECT COUNT(DISTINCT user_id) FROM user_sessions WHERE last_seen >= datetime('now','-5 minutes')").fetchone()[0]
payload = {'timestamp': int(time.time()), 'online_nodes': online, 'users': users, 'notifications': notifications}
body = json.dumps(payload, separators=(',', ':'), default=str).encode()
signature = hmac.new(SECRET.read_bytes().strip(), body, hashlib.sha256).hexdigest()
request = urllib.request.Request(URL, body, method='POST', headers={
    'Content-Type':'application/json', 'X-8Bit-Signature':signature,
    'User-Agent':'8BIT-BBS-AdminSync/1.0'})
with urllib.request.urlopen(request, timeout=15) as response:
    result = json.loads(response.read())
    if not result.get('ok'): raise RuntimeError(result)
print(json.dumps(result))
