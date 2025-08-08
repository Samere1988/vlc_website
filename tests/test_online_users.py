from datetime import datetime, timedelta
import os
import sys
import types

# Stub external dependencies used in app.py so the module can be imported
flask = types.ModuleType("flask")

class DummyFlask:
    def __init__(self, *args, **kwargs):
        self.config = {}

    def route(self, *args, **kwargs):
        def decorator(f):
            return f

        return decorator

    def before_request(self, f):
        return f


def _no_op(*args, **kwargs):
    pass


flask.Flask = DummyFlask
flask.render_template = _no_op
flask.request = _no_op
flask.jsonify = _no_op
flask.send_file = _no_op
flask.session = {}
flask.redirect = _no_op
flask.url_for = _no_op
sys.modules["flask"] = flask

flask_socketio = types.ModuleType("flask_socketio")


class DummySocketIO:
    def __init__(self, *args, **kwargs):
        pass

    def on(self, *args, **kwargs):
        def decorator(f):
            return f

        return decorator

    def run(self, *args, **kwargs):
        pass


flask_socketio.SocketIO = DummySocketIO
flask_socketio.emit = _no_op
sys.modules["flask_socketio"] = flask_socketio

flask_security = types.ModuleType("flask_security")


def login_required(f):
    return f


class DummyCurrentUser:
    is_authenticated = False


flask_security.login_required = login_required
flask_security.current_user = DummyCurrentUser()
sys.modules["flask_security"] = flask_security

models = types.ModuleType("models")
models.db = types.SimpleNamespace(init_app=lambda app: None, session=types.SimpleNamespace(commit=lambda: None))
models.Channel = models.Role = models.User = object
sys.modules["models"] = models

auth = types.ModuleType("auth")
auth.init_security = _no_op
auth.user_datastore = types.SimpleNamespace(find_role=lambda x: None, create_role=lambda **kwargs: None)
sys.modules["auth"] = auth

sys.path.append(os.path.dirname(os.path.dirname(__file__)))

import app


def setup_module(module):
    # ensure clean state before each module run
    with app.online_users_lock:
        app.online_users.clear()


def test_cleanup_removes_stale_users():
    with app.online_users_lock:
        app.online_users['old'] = datetime.utcnow() - app.ONLINE_TIMEOUT - timedelta(seconds=1)
    result = app.get_online_users()
    assert 'old' not in result
    with app.online_users_lock:
        assert 'old' not in app.online_users


def test_get_online_users_keeps_recent_entries():
    with app.online_users_lock:
        app.online_users['new'] = datetime.utcnow()
    result = app.get_online_users()
    assert 'new' in result
