import asyncio
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch
from flask import Flask
from sqlalchemy.orm import sessionmaker
from anetbbs.models import db, User, Board, Post, ChatMessage
from anetbbs.features import catchup, multinode
from anetbbs.core import user_manager

class CatchupTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///:memory:'
        db.init_app(self.app)
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()
        self.patch = patch.object(catchup, '_app', return_value=self.app)
        self.patch.start()
        self.now = datetime.utcnow()
        self.before = self.now - timedelta(days=1)
        a = User(username='reader', email='r@test', password_hash='', last_login=self.before, is_verified=True)
        a.set_password('password123')
        b = User(username='writer', email='w@test', password_hash='')
        db.session.add_all([a,b]); db.session.flush()
        self.uid, self.other = a.id, b.id
        self.user = dict(id=a.id, username=a.username, access_level=10,
                         last_login=self.now.isoformat(), previous_login=self.before.isoformat())
        public = Board(name='Public', min_access_level=10)
        secret = Board(name='Secret', min_access_level=100)
        inactive = Board(name='Inactive', is_active=False)
        db.session.add_all([public,secret,inactive]); db.session.flush()
        for board, author, when, subject in [
            (public,b,self.before,'boundary'), (public,b,self.now,'new topic'),
            (secret,b,self.now,'secret'), (inactive,b,self.now,'inactive'),
            (public,a,self.now,'own'), (public,b,self.now+timedelta(seconds=1),'future')]:
            db.session.add(Post(board_id=board.id, author_id=author.id, created_at=when, subject=subject, content='body'))
        db.session.commit()

    def tearDown(self):
        multinode._NODES.clear()
        self.patch.stop()
        db.session.remove(); db.drop_all(); self.context.pop()

    def test_boundaries_permissions_and_own_posts(self):
        _, counts, entries = catchup.collect(self.user)
        self.assertEqual(counts, (1,0))
        self.assertIn('new topic', entries[0]['heading'])
        self.assertEqual(catchup.collect(dict(self.user, is_admin=True))[1], (2,0))
        self.assertEqual(catchup.collect(self.user, page=1, size=1)[2], [])

    def test_replies_and_next_page(self):
        parent = Post.query.filter_by(subject='new topic').one()
        db.session.add(Post(board_id=parent.board_id, author_id=self.other,
                            parent_id=parent.id, subject='Re: new topic', content='reply',
                            created_at=self.now))
        db.session.commit()
        _, counts, entries = catchup.collect(self.user, size=1)
        self.assertEqual(counts[0], 2)
        self.assertTrue(entries[0]['heading'].startswith('Reply'))
        self.assertIn('Topic', catchup.collect(self.user, size=1, page=1)[2][0]['heading'])

    def test_previous_login_survives_authentication(self):
        with patch.object(user_manager, '_Session', sessionmaker(bind=db.engine, expire_on_commit=False)):
            result = user_manager.UserManager().authenticate('reader','password123')
        self.assertEqual(result['previous_login'], self.before.isoformat())
        self.assertGreater(datetime.fromisoformat(result['last_login']), self.before)

    def test_public_chat_saved_and_whisper_not_saved(self):
        entry = multinode.acquire_slot(dict(id=self.other, username='writer'), 'telnet', 'test', 8)
        with patch('anetbbs.features.webhooks.fire'), patch('anetbbs.features.bbs_ui._app', return_value=self.app):
            multinode.broadcast('writer', 'public hello', sender_slot=entry.slot)
            multinode.whisper(entry.slot, 'writer', 'private secret')
            multinode.broadcast('writer', 'joined', kind='join')
        self.assertEqual(ChatMessage.query.count(), 1)
        self.user['last_login'] = (datetime.utcnow()+timedelta(seconds=1)).isoformat()
        _, counts, entries = catchup.collect(self.user, kind='chat')
        self.assertEqual(counts[1], 1)
        self.assertEqual(entries[0]['text'], 'public hello')

    def test_first_login_and_control_sanitization(self):
        since, _, _ = catchup.collect(dict(self.user, previous_login=None))
        self.assertEqual(since, self.now-timedelta(days=7))
        self.assertEqual(catchup._safe('\x1b[2Jhello\x07\r\nworld'), 'hello world')

    def test_recap_can_be_reopened_without_changing_cutoff(self):
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        session = SimpleNamespace(user=self.user.copy(), term_width=40,
                                  write=AsyncMock(), read_key=AsyncMock(return_value='Q'))
        asyncio.run(catchup.show_catchup(session))
        asyncio.run(catchup.show_catchup(session))
        self.assertEqual(session.user, self.user)
        self.assertIn('What you missed', ''.join(call.args[0] for call in session.write.call_args_list))

if __name__ == '__main__':
    unittest.main()
