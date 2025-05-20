from flask import Flask, render_template, redirect, url_for, send_from_directory, request, jsonify, session
from flask_security import login_required, current_user
from datetime import datetime, timedelta
from models import db, Channel, Role, User
from auth import init_security, user_datastore
from sqlalchemy import text
import subprocess, os, signal, os.path
import logging
import traceback




# Configure logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

app = Flask(__name__)
online_users = {}
ONLINE_TIMEOUT = timedelta(minutes=1)
app.config['SECRET_KEY'] = 'super-secret-key'
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///C:/Users/samer/PycharmProjects/vlc website/channels.db'
app.config['SECURITY_PASSWORD_SALT'] = 'super-secret-salt'

# Enable registration
app.config['SECURITY_REGISTERABLE'] = True
app.config['SECURITY_SEND_REGISTER_EMAIL'] = False

# Custom template paths
app.config['SECURITY_LOGIN_USER_TEMPLATE'] = 'security/login_user.html'
app.config['SECURITY_REGISTER_USER_TEMPLATE'] = 'security/register_user.html'

# ✅ Required format for Flask-Security >= 4.0
app.config['SECURITY_USER_IDENTITY_ATTRIBUTES'] = [
    {'email': {'mapper': lambda x: x.lower()}}
]

# Redirects
app.config['SECURITY_POST_LOGIN_VIEW'] = '/'
app.config['SECURITY_POST_LOGOUT_VIEW'] = '/'
app.config['SECURITY_POST_REGISTER_VIEW'] = '/'

# Debugging (disable in production)
app.config['SECURITY_DEBUG'] = True




# Initialize DB first
db.init_app(app)

# Then initialize Flask-Security
init_security(app)

# === FFMPEG STREAMING ===
FFMPEG_PROCESS = None


def start_ffmpeg_stream(url):
    global FFMPEG_PROCESS
    if FFMPEG_PROCESS is not None:
        try:
            os.kill(FFMPEG_PROCESS.pid, signal.SIGTERM)
        except Exception:
            pass

    FFMPEG_PROCESS = subprocess.Popen([
        r'C:\ffmpeg\bin\ffmpeg.exe',
        '-user_agent', 'IPTV Smarters Pro',
        '-i', url,
        '-c:v', 'copy',
        '-c:a', 'aac',
        '-f', 'hls',
        '-hls_time', '2',
        '-hls_list_size', '10',
        '-hls_flags', 'delete_segments+append_list',
        '-hls_allow_cache', '0',
        'C:/stream/playlist.m3u8'
    ])


# === ROUTES ===
@app.route('/')
@login_required
def index():
    try:
        # Get channels safely
        channels = Channel.query.all()

        logger.debug(f"Found {len(channels)} channels")

        if channels:
            first_channel = channels[0]
            logger.debug(f"First channel: ID={first_channel.id}, Name={first_channel.name}")

        # ✅ Get the currently playing channel (global)
        now_playing = Channel.query.filter_by(is_playing=1).first()

        # ✅ Get list of online users
        online = get_online_users()

        return render_template(
            'index.html',
            channels=channels,
            online_users=online,
            now_playing=now_playing
        )

    except Exception as e:
        logger.error(f"Error in index route: {str(e)}")
        return "Something went wrong", 500


    except Exception as e:
        logger.error(f"Error in index route: {str(e)}")
        logger.error(traceback.format_exc())
        return f"Error loading channels: {str(e)}", 500


@app.route('/api/online_users')
@login_required
def api_online_users():
    return jsonify(get_online_users())


@app.route('/play/<int:channel_id>')
@login_required
def play_channel(channel_id):
    channel = Channel.query.get(channel_id)

    if channel:
        # ✅ Reset all other channels to not playing
        Channel.query.update({Channel.is_playing: False})

        # ✅ Set this one to playing
        channel.is_playing = True
        db.session.commit()

        # ✅ Start the stream
        start_ffmpeg_stream(channel.url)

        # ✅ Optionally still store name in session
        session['current_channel_name'] = channel.name

    return redirect(url_for('index'))


@app.route('/toggle_favorite', methods=['POST'])
@login_required
def toggle_favorite():
    try:
        data = request.get_json()
        channel_id = data.get('channel_id')
        channel = Channel.query.get(channel_id)
        if channel:
            channel.favorites = 0 if channel.favorites == 1 else 1
            db.session.commit()
            return jsonify({'success': True, 'new_status': channel.favorites})
        return jsonify({'success': False})
    except Exception as e:
        logger.error(f"Error toggling favorite: {str(e)}")
        return jsonify({'success': False, 'error': str(e)})


@app.route('/get_favorites')
@login_required
def get_favorites():
    try:
        favorites = Channel.query.filter_by(favorites=1).all()
        result = []
        for channel in favorites:
            result.append([channel.id, channel.name])
        return jsonify(result)
    except Exception as e:
        logger.error(f"Error getting favorites: {str(e)}")
        return jsonify([])


@app.route('/stream/<path:filename>')
def stream_files(filename):
    return send_from_directory('C:/stream', filename)


@app.route('/debug_channel_details')
def debug_channel_details():
    try:
        # Get all channels
        channels = Channel.query.all()

        output = []
        output.append(f"Total channels: {len(channels)}")

        # Get details of the first few channels
        for i, channel in enumerate(channels[:5]):
            output.append(f"Channel {i + 1}:")
            output.append(f"  ID: {channel.id}")
            output.append(f"  Name: {channel.name}")
            output.append(f"  URL: {channel.url}")
            output.append(f"  Favorites: {channel.favorites}")
            output.append(f"  Type: {type(channel)}")
            output.append(f"  Dir: {dir(channel)}")
            output.append("-" * 40)

        return "<br>".join(output)
    except Exception as e:
        logger.error(f"Error in debug_channel_details: {str(e)}")
        logger.error(traceback.format_exc())
        return f"Error: {str(e)}"


@app.before_request
def track_user_activity():
    if current_user.is_authenticated:
        online_users[current_user.username] = datetime.utcnow()

def get_online_users():
    now = datetime.utcnow()
    return [
        username for username, last_seen in online_users.items()
        if now - last_seen < ONLINE_TIMEOUT
    ]



@app.route('/debug_template')
def debug_template():
    try:
        # Return a minimal template with a simple list of channels
        channels = Channel.query.all()
        html = """
        <!DOCTYPE html>
        <html>
        <head>
            <title>Debug Template</title>
        </head>
        <body>
            <h1>Channels Debug</h1>
            <ul>
        """

        for channel in channels:
            html += f"<li>{channel.id} - {channel.name}</li>"

        html += """
            </ul>
        </body>
        </html>
        """

        return html
    except Exception as e:
        return f"Template error: {str(e)}"


with app.app_context():
    db.create_all()

    if not user_datastore.find_role('admin'):
        user_datastore.create_role(name='admin', description='Administrator')
        db.session.commit()


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5050, debug=True)



