import os
import sqlite3
import subprocess
import threading
import time
import signal
from flask import Flask, render_template, request, jsonify, send_file, session, redirect, url_for
from flask_socketio import SocketIO, emit
from flask_security import login_required, current_user
from datetime import datetime, timedelta
from models import db, Channel, Role, User
from auth import init_security, user_datastore

# ─────────────────────────────────────────────────────────────────────────────
# Flask Setup + Config
# ─────────────────────────────────────────────────────────────────────────────

app = Flask(__name__)
socketio = SocketIO(app, cors_allowed_origins="*")
online_users = {}
ONLINE_TIMEOUT = timedelta(minutes=1)
online_users_lock = threading.Lock()

app.config['SECRET_KEY'] = 'super-secret-key'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///C:/Users/samer/PycharmProjects/vlc website/channels.db'
app.config['SECURITY_PASSWORD_SALT'] = 'super-secret-salt'
app.config['SECURITY_REGISTERABLE'] = True
app.config['SECURITY_SEND_REGISTER_EMAIL'] = False
app.config['SECURITY_LOGIN_USER_TEMPLATE'] = 'security/login_user.html'
app.config['SECURITY_REGISTER_USER_TEMPLATE'] = 'security/register_user.html'
app.config['SECURITY_USER_IDENTITY_ATTRIBUTES'] = [{'email': {'mapper': lambda x: x.lower()}}]
app.config['SECURITY_POST_LOGIN_VIEW'] = '/'
app.config['SECURITY_POST_LOGOUT_VIEW'] = '/'
app.config['SECURITY_POST_REGISTER_VIEW'] = '/'
app.config['SECURITY_DEBUG'] = True

db.init_app(app)
init_security(app)

# ─────────────────────────────────────────────────────────────────────────────
# IPTV STREAMING CLASS
# ─────────────────────────────────────────────────────────────────────────────

class IPTVStreamer:
    def __init__(self, db_path="channels.db"):
        self.db_path = db_path
        self.current_process = None
        self.current_channel_id = None
        self.stream_dir = "stream"
        self.ensure_stream_directory()

    def ensure_stream_directory(self):
        if not os.path.exists(self.stream_dir):
            os.makedirs(self.stream_dir)

    def cleanup_old_files(self):
        try:
            for file in os.listdir(self.stream_dir):
                if file.endswith(('.m3u8', '.ts')):
                    os.remove(os.path.join(self.stream_dir, file))
        except:
            pass

    def get_channels(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM channels ORDER BY name")
        channels = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return channels

    def get_channel_by_id(self, channel_id):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM channels WHERE id = ?", (channel_id,))
        channel = cursor.fetchone()
        conn.close()
        return dict(channel) if channel else None

    def update_playing_status(self, channel_id):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("UPDATE channels SET is_playing = 0")
        if channel_id:
            cursor.execute("UPDATE channels SET is_playing = 1 WHERE id = ?", (channel_id,))
        conn.commit()
        conn.close()

    def stop_current_stream(self):
        if self.current_process:
            try:
                self.current_process.terminate()
                self.current_process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.current_process.kill()
                self.current_process.wait()
            except:
                pass
            finally:
                self.current_process = None
        self.cleanup_old_files()

    def start_stream(self, channel_id):
        self.stop_current_stream()
        channel = self.get_channel_by_id(channel_id)
        if not channel:
            return False, "Channel not found"

        self.update_playing_status(channel_id)
        self.current_channel_id = channel_id
        self.cleanup_old_files()

        stream_path = os.path.join(self.stream_dir, "stream.m3u8")

        ffmpeg_paths = [
            'ffmpeg', r'C:\ffmpeg\bin\ffmpeg.exe',
            r'C:\Program Files\ffmpeg\bin\ffmpeg.exe', 'ffmpeg.exe'
        ]
        ffmpeg_exe = None
        for path in ffmpeg_paths:
            try:
                subprocess.run([path, '-version'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
                ffmpeg_exe = path
                break
            except (subprocess.CalledProcessError, FileNotFoundError):
                continue

        if not ffmpeg_exe:
            return False, "FFmpeg not found. Please install FFmpeg and add it to PATH."

        ffmpeg_cmd = [
            ffmpeg_exe, '-i', channel['url'],
            '-c:v', 'libx264', '-preset', 'ultrafast', '-tune', 'zerolatency',
            '-profile:v', 'baseline', '-level', '3.0', '-pix_fmt', 'yuv420p',
            '-r', '25', '-g', '50', '-sc_threshold', '0',
            '-c:a', 'aac', '-b:a', '128k', '-ar', '44100',
            '-f', 'hls', '-hls_time', '1', '-hls_list_size', '5',
            '-hls_flags', 'delete_segments+independent_segments',
            '-hls_segment_type', 'mpegts', '-hls_start_number_source', 'epoch',
            '-force_key_frames', 'expr:gte(t,n_forced*1)', '-y', stream_path
        ]

        try:
            self.current_process = subprocess.Popen(
                ffmpeg_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.PIPE
            )
            threading.Thread(target=self._monitor_stream, daemon=True).start()
            time.sleep(2)
            return True, f"Started streaming: {channel['name']}"
        except Exception as e:
            return False, f"Failed to start stream: {str(e)}"

    def _monitor_stream(self):
        if self.current_process:
            _, stderr = self.current_process.communicate()
            if self.current_process.returncode != 0:
                print(f"Stream error: {stderr.decode()}")
                socketio.emit('stream_error', {'message': 'Stream ended unexpectedly'})
            self.current_process = None

streamer = IPTVStreamer()

# ─────────────────────────────────────────────────────────────────────────────
# Routes
# ─────────────────────────────────────────────────────────────────────────────

@app.route('/')
@login_required
def index():
    now_playing = Channel.query.filter_by(is_playing=1).first()
    online = get_online_users()
    return render_template("index.html", now_playing=now_playing, online_users=online)

@app.route('/api/channels')
@login_required
def get_channels():
    return jsonify(streamer.get_channels())

@app.route('/api/play/<int:channel_id>', methods=['POST'])
@login_required
def play_channel(channel_id):
    success, message = streamer.start_stream(channel_id)
    if success:
        socketio.emit('channel_changed', {'channel_id': channel_id, 'message': message})
    return jsonify({'success': success, 'message': message})

@app.route('/api/stop', methods=['POST'])
@login_required
def stop_stream():
    streamer.stop_current_stream()
    streamer.update_playing_status(None)
    streamer.current_channel_id = None
    socketio.emit('stream_stopped')
    return jsonify({'success': True, 'message': 'Stream stopped'})

@app.route('/api/status')
@login_required
def get_status():
    return jsonify({
        'is_streaming': streamer.current_process is not None,
        'current_channel_id': streamer.current_channel_id,
        'process_alive': streamer.current_process.poll() is None if streamer.current_process else False
    })

@app.route('/stream/<path:filename>')
def serve_stream(filename):
    file_path = os.path.join(streamer.stream_dir, filename)
    if not os.path.exists(file_path):
        return "Stream file not found", 404

    mimetype = 'application/vnd.apple.mpegurl' if filename.endswith('.m3u8') else 'video/mp2t'
    response = send_file(file_path, mimetype=mimetype)
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response

@app.route('/toggle_favorite', methods=['POST'])
@login_required
def toggle_favorite():
    data = request.get_json()
    channel_id = data.get('channel_id')
    channel = Channel.query.get(channel_id)
    if channel:
        channel.favorites = 0 if channel.favorites == 1 else 1
        db.session.commit()
        return jsonify({'success': True, 'new_status': channel.favorites})
    return jsonify({'success': False})

@app.route('/get_favorites')
@login_required
def get_favorites():
    favorites = Channel.query.filter_by(favorites=1).all()
    return jsonify([[ch.id, ch.name] for ch in favorites])

@app.route('/api/online_users')
@login_required
def api_online_users():
    return jsonify(get_online_users())

@socketio.on('connect')
def handle_connect():
    emit('status', {
        'is_streaming': streamer.current_process is not None,
        'current_channel_id': streamer.current_channel_id
    })

@socketio.on('disconnect')
def handle_disconnect():
    pass

@app.before_request
def track_user_activity():
    if current_user.is_authenticated:
        with online_users_lock:
            online_users[current_user.username] = datetime.utcnow()

def cleanup_online_users():
    now = datetime.utcnow()
    with online_users_lock:
        stale = [u for u, last in online_users.items() if now - last >= ONLINE_TIMEOUT]
        for u in stale:
            del online_users[u]


def get_online_users():
    cleanup_online_users()
    with online_users_lock:
        return list(online_users.keys())


def _cleanup_loop():
    while True:
        cleanup_online_users()
        time.sleep(30)


threading.Thread(target=_cleanup_loop, daemon=True).start()

def create_app():
    return app

if __name__ == '__main__':
    with app.app_context():
        db.create_all()
        if not user_datastore.find_role('admin'):
            user_datastore.create_role(name='admin', description='Administrator')
            db.session.commit()

    print("✅ IPTV Streaming Server started on http://localhost:5000")
    print("🔒 Login required to access")
    socketio.run(app, host='0.0.0.0', port=5000)





