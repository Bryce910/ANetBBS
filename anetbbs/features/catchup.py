"""Terminal catch-up for visible forums and public local chat."""
import logging
import re
import textwrap
from datetime import datetime, timedelta

from .bbs_ui import _app
from ..core.tz import fmt_eastern

logger = logging.getLogger(__name__)


def record_chat(user_id, username, message):
    """Persist public local messages only; whispers never use this path."""
    from anetbbs.models import ChatMessage, db
    try:
        with _app().app_context():
            db.session.add(ChatMessage(user_id=user_id, username=username,
                                       message=message or ''))
            db.session.commit()
    except Exception:
        logger.exception('Could not retain public local chat')


def _window(user):
    until = datetime.fromisoformat(user['last_login'])
    previous = user.get('previous_login')
    return (datetime.fromisoformat(previous) if previous else until - timedelta(days=7)), until


def collect(user, page=0, size=10, kind='forums'):
    """Read a stable login-to-login window without marking anything read."""
    from anetbbs.models import Board, Post, User, ChatMessage
    since, until = _window(user)
    with _app().app_context():
        posts = (Post.query.join(Board).filter(
            Board.is_active.is_(True), Post.created_at > since,
            Post.created_at <= until, Post.author_id != user['id']))
        if not user.get('is_admin'):
            posts = posts.filter(Board.min_access_level <= int(user.get('access_level', 10)))
        chats = ChatMessage.query.filter(
            ChatMessage.created_at > since, ChatMessage.created_at <= until,
            ChatMessage.user_id != user['id'])
        counts = (posts.count(), chats.count())
        if kind == 'forums':
            rows = posts.order_by(Post.created_at.desc(), Post.id.desc()).offset(page * size).limit(size).all()
            entries = []
            for post in rows:
                author = User.query.get(post.author_id)
                entries.append({
                    'heading': f"{'Reply' if post.parent_id else 'Topic'} #{post.id}: {post.subject}",
                    'meta': f"{post.board.name} / {author.username if author else '?'} / {fmt_eastern(post.created_at)}",
                    'text': post.content,
                })
        else:
            rows = chats.order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc()).offset(page * size).limit(size).all()
            entries = [{'heading': f'{row.username} / {fmt_eastern(row.created_at)}',
                        'meta': 'Public local chat', 'text': row.message} for row in rows]
    return since, counts, entries


def _safe(text):
    # Strip ANSI sequences and terminal control bytes from user-authored text.
    text = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', str(text or ''))
    return ' '.join(''.join(c for c in text if c.isprintable() or c.isspace()).split())


async def show_catchup(session):
    kind, page = 'forums', 0
    width = max(20, min(100, int(getattr(session, 'term_width', 80) or 80) - 2))
    while True:
        since, counts, entries = collect(session.user, page=page, kind=kind, size=5)
        await session.write('\r\n=== What you missed ===\r\n')
        label = 'Since your previous login' if session.user.get('previous_login') else 'First visit: the past 7 days'
        await session.write(f'{label}: {fmt_eastern(since)}\r\n')
        await session.write(f'{counts[0]} forum posts / {counts[1]} public local chat messages\r\n')
        await session.write('Activity through this login; your own posts are excluded.\r\n')
        await session.write('Local chat history starts when this feature was enabled.\r\n')
        await session.write(f'\r\n{kind.title()} - page {page + 1}\r\n')
        if not entries:
            await session.write('Nothing new here.\r\n')
        for entry in entries:
            # Forum bodies are previews; full discussions remain in Message Boards.
            body = _safe(entry['text'])
            if kind == 'forums' and len(body) > 400:
                body = body[:400] + '... [continued in Message Boards]'
            for value in (entry['heading'], entry['meta'], body):
                for line in textwrap.wrap(_safe(value), width=width):
                    await session.write(line + '\r\n')
            key = await session.read_key('[Any key: next item / Q: return] ')
            if (key or '').upper() == 'Q':
                return
            await session.write('\r\n')
        total = counts[0 if kind == 'forums' else 1]
        key = (await session.read_key('\r\n[F]orums [C]hat [N]ext [P]revious [Q]uit: ') or '').upper()
        if key == 'Q':
            return
        if key in ('F', 'C'):
            kind, page = ('forums' if key == 'F' else 'chat'), 0
        elif key == 'N' and (page + 1) * 5 < total:
            page += 1
        elif key == 'P':
            page = max(0, page - 1)


async def offer_catchup(session):
    try:
        _, counts, _ = collect(session.user, size=0)
    except Exception:
        logger.exception('Could not load login catch-up')
        return
    await session.write(f'\r\nWhat you missed: {counts[0]} forum posts, {counts[1]} local chat messages.\r\n')
    if any(counts):
        answer = await session.read_key('View recap? [Y/N]: ')
        if (answer or '').upper() == 'Y':
            await show_catchup(session)
