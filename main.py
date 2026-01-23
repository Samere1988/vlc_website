import os
import sqlite3
import asyncio
import socketio
import subprocess
import threading
import glob
import time
import uuid
from collections import defaultdict
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, Form, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.security import OAuth2PasswordRequestForm
from fastapi.staticfiles import StaticFiles
from plex_service import plex_service
# ─── IMPORT FROM USERS.PY ─────────────────────────────────────────────
from users import (
    User, UserCreate, UserRead, UserManager,
    get_user_manager, auth_backend, fastapi_users,
    current_active_user, engine, Base
)


# ─── APP SETUP ────────────────────────────────────────────────────────
# Lifespan handles database creation on startup
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create the Users database tables (Async)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield


# SocketIO Setup
sio = socketio.AsyncServer(async_mode='asgi', cors_allowed_origins='*')

fastapi_app = FastAPI(lifespan=lifespan)
fastapi_app.mount("/static", StaticFiles(directory="static"), name="static")
asgi_app = socketio.ASGIApp(sio, fastapi_app)
@fastapi_app.exception_handler(status.HTTP_401_UNAUTHORIZED)
async def unauthorized_exception_handler(request: Request, exc: HTTPException):
    # If the user tries to access a page but isn't logged in, redirect them to login
    return RedirectResponse(url="/login", status_code=302)
templates = Jinja2Templates(directory="templates")
@fastapi_app.post("/api/heartbeat")
async def api_heartbeat_http(user: User = Depends(current_active_user)):
    """
    Updates the timestamp for the user so they don't get
    removed from the online list.
    """
    online_users.add(user.username)
    online_last_seen[user.username] = time.time()
    return {"status": "alive"}


# ─── GLOBAL VARS ──────────────────────────────────────────────────────
online_users = set()
online_last_seen: dict[str, float] = {}
sid_to_username: dict[str, str] = {}
username_conn_counts = defaultdict(int)

# ─── CHANNELS DB (Old SQLite, Kept Separate) ──────────────────────────
CHANNELS_DB_PATH = "channels.db"


def get_channel_by_id(channel_id: int):
    conn = sqlite3.connect(CHANNELS_DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT * FROM channels WHERE id = ?", (channel_id,))
    row = cur.fetchone()
    conn.close()
    return dict(row) if row else None


def mark_playing(channel_id: int | None):
    conn = sqlite3.connect(CHANNELS_DB_PATH)
    cur = conn.cursor()
    cur.execute("UPDATE channels SET is_playing = 0")
    if channel_id is not None:
        cur.execute("UPDATE channels SET is_playing = 1 WHERE id = ?", (channel_id,))
    conn.commit()
    conn.close()


def list_channels():
    conn = sqlite3.connect(CHANNELS_DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    cur.execute("SELECT * FROM channels ORDER BY name")
    rows = [dict(r) for r in cur.fetchall()]
    current_id = streamer.current_channel_id
    for ch in rows:
        ch['Favorites'] = bool(ch.get('Favorites', 0))
        ch['is_playing'] = (ch['id'] == current_id)
    conn.close()
    return rows


# ─── STREAMING LOGIC ──────────────────────────────────────────────────
class IPTVStreamer:
    def __init__(self):
        self.current_process = None
        self.current_channel_id = None
        # UPDATE THIS PATH TO MATCH YOUR PC
        self.stream_dir = os.path.join(os.getcwd(), "stream")
        self.ensure_stream_directory()

    def ensure_stream_directory(self):
        if not os.path.exists(self.stream_dir):
            os.makedirs(self.stream_dir)

    def cleanup_old_files(self):
        try:
            files = glob.glob(os.path.join(self.stream_dir, "*"))
            for f in files:
                try:
                    os.remove(f)
                except:
                    pass
        except:
            pass

    def stop_current_stream(self):
        if self.current_process:
            try:
                self.current_process.terminate()
                self.current_process.wait(timeout=2)
            except:
                try:
                    self.current_process.kill()
                except:
                    pass
            self.current_process = None
        self.cleanup_old_files()
        self.current_channel_id = None
        mark_playing(None)

    def start_stream(self, channel_id: int):
        self.stop_current_stream()
        channel = get_channel_by_id(channel_id)
        if not channel: return False, "Channel not found"

        session_id = str(uuid.uuid4())[:8]
        stream_path = os.path.join(self.stream_dir, "stream.m3u8")

        # AUTO-DETECT FFMPEG
        ffmpeg_exe = "ffmpeg"
        known_paths = [
            r"C:\ffmpeg\bin\ffmpeg.exe",
            r"C:\Program Files\ffmpeg\bin\ffmpeg.exe"
        ]
        for p in known_paths:
            if os.path.exists(p):
                ffmpeg_exe = p
                break

        ffmpeg_cmd = [
            ffmpeg_exe,
            '-hide_banner', '-loglevel', 'error',  # Cleaner logs
            '-i', channel['url'],

            # ─── VIDEO QUALITY SETTINGS ───
            '-c:v', 'libx264',
            '-preset', 'veryfast',
            '-tune', 'zerolatency',
            '-crf', '15',
            '-maxrate', '15M',
            '-bufsize', '30M',
            '-force_key_frames', 'expr:gte(t,n_forced*2)',
            '-sc_threshold', '0',
            '-pix_fmt', 'yuv420p',

            # ─── AUDIO QUALITY ───
            '-c:a', 'aac',
            '-ar', '48000',  # Increased to 48khz (DVD quality)
            '-b:a', '320k',  # Max standard AAC quality

            # ─── HLS SETTINGS ───
            '-f', 'hls',
            '-hls_time', '2',
            '-hls_list_size', '6',
            '-hls_flags', 'delete_segments',
            '-hls_segment_filename', os.path.join(self.stream_dir, f'seg_{session_id}_%03d.ts'),
            '-y', stream_path
        ]

        try:
            self.current_process = subprocess.Popen(
                ffmpeg_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            threading.Thread(target=self._monitor_stream, daemon=True).start()

            # Wait for file
            for i in range(15):
                time.sleep(1)
                if os.path.exists(stream_path) and os.path.getsize(stream_path) > 0:
                    self.current_channel_id = channel_id
                    mark_playing(channel_id)
                    return True, f"Playing: {channel['name']}"

            self.stop_current_stream()
            return False, "Stream timed out"
        except Exception as e:
            return False, str(e)

    def _monitor_stream(self):
        if not self.current_process: return
        try:
            while self.current_process.poll() is None:
                line = self.current_process.stderr.readline()
                if not line: break
        except:
            pass


streamer = IPTVStreamer()


# ─── ROUTES: AUTH & PAGES ─────────────────────────────────────────────

# 1. Login Page (GET)
@fastapi_app.get("/login", response_class=HTMLResponse, name="login")
async def login_page(request: Request):
    return templates.TemplateResponse("security/login_user.html", {"request": request})


# 2. Login Logic (POST)
@fastapi_app.post("/login", name="login")
async def login(
        request: Request,
        email: str = Form(...),
        password: str = Form(...),
        user_manager: UserManager = Depends(get_user_manager)
):
    # 1. Validate credentials
    user = await user_manager.authenticate(OAuth2PasswordRequestForm(username=email, password=password))
    if not user:
        return templates.TemplateResponse("security/login_user.html",
                                          {"request": request, "error": "Invalid email or password"})

    # 2. Get the auth strategy
    strategy = auth_backend.get_strategy()

    # 3. Generate the standard login response (This contains the "Set-Cookie" header)
    auth_response = await auth_backend.login(strategy, user)

    # 4. Create our Redirect Response (to send user to Home)
    response = RedirectResponse("/", status_code=status.HTTP_302_FOUND)

    # 5. COPY THE COOKIE: Take the "set-cookie" header from the auth response
    # and add it to our redirect response. This logs the user in.
    response.headers["set-cookie"] = auth_response.headers["set-cookie"]

    # 6. Mark online and return
    online_users.add(user.username)
    return response


# 3. Register Page (GET)
@fastapi_app.get("/register", response_class=HTMLResponse, name="register")
async def register_page(request: Request):
    return templates.TemplateResponse("security/register_user.html", {"request": request})


# 4. Register Logic (POST)
@fastapi_app.post("/register", name="register")
async def register(
        request: Request,
        username: str = Form(...),
        email: str = Form(...),
        password: str = Form(...),
        password_confirm: str = Form(...),
        user_manager: UserManager = Depends(get_user_manager)
):
    if password != password_confirm:
        return templates.TemplateResponse("security/register_user.html",
                                          {"request": request, "error": "Passwords do not match"})

    try:
        user_create = UserCreate(email=email, password=password, username=username)
        await user_manager.create(user_create)
        return RedirectResponse("/login", status_code=302)
    except Exception as e:
        # User likely exists
        return templates.TemplateResponse("security/register_user.html",
                                          {"request": request, "error": "Email or Username already registered"})


# 5. Logout
@fastapi_app.get("/logout", name="logout")
async def logout(request: Request):
    response = RedirectResponse("/login")
    # This deletes the "auth" cookie
    response.delete_cookie("auth")
    return response


# 6. Forgot Password Stubs (Placeholder logic)
@fastapi_app.get("/forgot-password", name="forgot_password")
async def forgot_password_page(request: Request):
    return templates.TemplateResponse("security/forgot_password.html", {"request": request})


# Real Forgot Password Logic
@fastapi_app.post("/forgot-password")
async def forgot_password_submit(
        request: Request,
        email: str = Form(...),
        user_manager: UserManager = Depends(get_user_manager)
):
    try:
        # 1. Find the user in the database
        user = await user_manager.get_by_email(email)

        # 2. Trigger the email sending logic (calls on_after_forgot_password in users.py)
        await user_manager.forgot_password(user, request)

    except Exception as e:
        # Debugging: Print error if it fails
        print(f" EMAIL ERROR: {e}")
        # In production, you usually pass here to prevent email enumeration,
        # but for debugging, we want to see the error.

    return templates.TemplateResponse("security/forgot_password.html",
                                      {"request": request,
                                       "message": "If that email exists, a reset link has been sent."})


@fastapi_app.get("/reset-password/{token}", name="reset_password")
async def reset_password_page(request: Request, token: str):
    return templates.TemplateResponse("security/reset_password.html", {"request": request, "token": token})


@fastapi_app.post("/reset-password/{token}")
async def reset_password_submit(request: Request, token: str, password: str = Form(...),
                                password_confirm: str = Form(...)):
    if password != password_confirm:
        return templates.TemplateResponse("security/reset_password.html",
                                          {"request": request, "token": token, "error": "Passwords don't match"})
    # Real logic: await user_manager.reset_password(token, password)
    return RedirectResponse("/login", status_code=302)


@fastapi_app.get("/change-password", name="change_password")
async def change_pw_page(request: Request):
    return templates.TemplateResponse("security/change_password.html", {"request": request})


@fastapi_app.post("/change-password", name="change_password")
async def change_pw_submit(
        request: Request,
        password: str = Form(...),  # Current PW (not used in placeholder)
        new_password: str = Form(...),
        user=Depends(current_active_user)
):
    # In real fastapi-users, you need to verify current PW first
    return templates.TemplateResponse("security/change_password.html",
                                      {"request": request, "message": "Feature needs mail config"})


# ─── MAIN APP ROUTES ──────────────────────────────────────────────────

@fastapi_app.get("/", response_class=HTMLResponse, name="index")
async def index(
        request: Request,
        # This dependency ensures only logged-in users can see the page
        user: User = Depends(current_active_user)
):
    # Mark user online
    online_users.add(user.username)
    online_last_seen[user.username] = time.time()

    now_playing = get_channel_by_id(streamer.current_channel_id) if streamer.current_channel_id else None
    return templates.TemplateResponse("index.html", {
        "request": request,
        "user": user,
        "now_playing": now_playing
    })

# ─── PLEX WATCH TOGETHER ROUTES ──────────────────────────────────────

@fastapi_app.get("/plex-watch-together", response_class=HTMLResponse)
async def plex_page(request: Request, user: User = Depends(current_active_user)):
    # This serves the HTML file
    return templates.TemplateResponse("plex_watch.html", {"request": request, "user": user})


@fastapi_app.get("/api/plex/content")
async def api_plex_content(parent_id: str | None = None, user: User = Depends(current_active_user)):
    # Handle the string 'null' that JS sometimes sends
    if parent_id == 'null': parent_id = None

    # Call the engine
    content = plex_service.get_content(parent_id)
    return content

@fastapi_app.get("/api/plex/image")
async def api_plex_image(id: str, thumb: str | None = None, user: User = Depends(current_active_user)):
    # This loads the thumbnails
    return plex_service.get_image(id, thumb)

# Heartbeat (Keep-Alive)
@sio.event
async def heartbeat(sid, data):
    plex_service.update_state(data['isPlaying'], data['currentTime'])


@fastapi_app.get("/api/online_users")
async def get_online_users():
    now = time.time()
    cutoff = now - 15
    for u, ts in list(online_last_seen.items()):
        if ts < cutoff and username_conn_counts.get(u, 0) == 0:
            online_last_seen.pop(u, None)
            online_users.discard(u)
    return sorted(list(online_users))


@fastapi_app.get("/api/channels")
async def api_channels(user: User = Depends(current_active_user)):
    return list_channels()


@fastapi_app.get("/api/status")
async def api_status(user: User = Depends(current_active_user)):
    return {
        "is_streaming": streamer.current_channel_id is not None,
        "current_channel_id": streamer.current_channel_id
    }


@fastapi_app.post("/api/play/{channel_id}")
async def api_play(channel_id: int, user: User = Depends(current_active_user)):
    loop = asyncio.get_event_loop()
    success, msg = await loop.run_in_executor(None, streamer.start_stream, channel_id)
    if success:
        await sio.emit('channel_changed', {'channel_id': channel_id, 'message': msg})
    return {"success": success, "message": msg}


@fastapi_app.post("/api/stop")
async def api_stop(user: User = Depends(current_active_user)):
    streamer.stop_current_stream()
    await sio.emit('stream_stopped')
    return {"success": True, "message": "Stopped"}


@fastapi_app.get("/stream/{filename}")
async def serve_stream_file(filename: str):
    # No auth on stream files to allow HLS players to fetch segments easily
    # (Browsers often handle media requests separately from main page requests)
    file_path = os.path.join(streamer.stream_dir, filename)
    if not os.path.exists(file_path):
        return JSONResponse({"error": "Not found"}, status_code=404)

    media_type = "video/mp2t"
    if filename.endswith(".m3u8"): media_type = "application/vnd.apple.mpegurl"

    response = FileResponse(file_path, media_type=media_type)
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response


@fastapi_app.get("/stream/plex/{filename}")
async def serve_plex_stream_file(filename: str):
    # This serves files specifically from stream/plex/
    file_path = os.path.join(plex_service.STREAM_DIR, filename)

    if not os.path.exists(file_path):
        return JSONResponse({"error": "Not found"}, status_code=404)

    media_type = "video/mp2t"
    if filename.endswith(".m3u8"): media_type = "application/vnd.apple.mpegurl"

    response = FileResponse(file_path, media_type=media_type)
    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    return response

# ─── SOCKET.IO EVENTS ─────────────────────────────────────────────────
@sio.event
async def connect(sid, environ):
    # SocketIO doesn't easily see the HttpOnly cookie, but we can rely on the Heartbeat API
    # for online status. We just allow the connection here.
    await sio.emit("status", {
        "is_streaming": streamer.current_channel_id is not None,
        "current_channel_id": streamer.current_channel_id
    }, room=sid)


@sio.event
async def request_room_state(sid):
    if not plex_service.current_movie_id:
        return

    real_time = plex_service.get_real_time()

    # 1. Send movie info + current authoritative state
    await sio.emit(
        'change_movie',
        {
            'url': '/stream/plex/stream.m3u8',
            'title': plex_service.current_movie_title,
            'movieId': plex_service.current_movie_id,
            'duration': getattr(plex_service, 'current_movie_duration', 0),
            'startTime': real_time,
            'isPlaying': plex_service.is_playing,
        },
        room=sid
    )

    # 2. Send explicit sync state so client applies play/pause correctly
    await sio.emit(
        'sync_state',
        {
            'isPlaying': plex_service.is_playing,
            'currentTime': real_time,
            'from_sid': 'System'
        },
        room=sid
    )


@sio.event
async def request_movie_link(sid, link_data):
    await sio.emit('chat_message', {'user': 'System', 'text': "Processing link..."})

    loop = asyncio.get_event_loop()
    success = await loop.run_in_executor(None, plex_service.start_transcode_from_link, link_data)

    if not success:
        await sio.emit(
            'chat_message',
            {'user': 'System', 'text': "Invalid link or failed to start stream."},
            room=sid
        )
        return

    # ✅ Start playback immediately
    plex_service.update_state(True, 0)

    # ✅ Tell everyone to load the movie
    await sio.emit('change_movie', {
        'url': '/stream/plex/stream.m3u8',
        'title': plex_service.current_movie_title,
        'movieId': plex_service.current_movie_id,
        'duration': getattr(plex_service, 'current_movie_duration', 0),
        'startTime': 0,
        'isPlaying': True,
        'starter_sid': sid
    })

    # ✅ Reinforce the authoritative play state (helps clients that lag during load)
    await sio.emit('sync_state', {
        'isPlaying': True,
        'currentTime': 0,
        'from_sid': 'System'
    })

    await sio.emit('chat_message', {
        'user': 'System',
        'text': f"Now Playing: {plex_service.current_movie_title}"
    })


    await sio.emit(
        'chat_message',
        {
            'user': 'System',
            'text': f"Now Playing: {plex_service.current_movie_title}"
        }
    )


@sio.event
async def player_update(sid, data):
    """
    Triggered by Play, Pause, or Seek.
    Broadcasts IMMEDIATELY to others.
    """
    plex_service.update_state(data['isPlaying'], data['currentTime'])

    # Broadcast to everyone, BUT include the sender's ID (sid)
    await sio.emit('sync_state', {
        'isPlaying': plex_service.is_playing,
        'currentTime': plex_service.current_time,
        'from_sid': sid  # <--- CRITICAL: Tells clients who caused this
    })

@sio.event
async def chat_message(sid, msg):
    # LOOK UP THE REAL NAME
    # If not found (rare), default to "Guest"
    username = sid_to_username.get(sid, "Guest")

    # Send to everyone with the real name
    await sio.emit('chat_message', {'user': username, 'text': msg})


@sio.event
async def register_user(sid, username):
    sid_to_username[sid] = username
    await sio.emit('chat_message', {'user': 'System', 'text': f"{username} joined."})

    # If a movie is active, sync ONLY the new user
    if plex_service.current_movie_id:
        real_time = plex_service.get_real_time()

        await sio.emit('change_movie', {
            'url': '/stream/plex/stream.m3u8',
            'title': plex_service.current_movie_title,
            'movieId': plex_service.current_movie_id,
            'duration': getattr(plex_service, 'current_movie_duration', 0),
            'startTime': real_time,
            'isPlaying': plex_service.is_playing,
        }, room=sid)

        await sio.emit('sync_state', {
            'isPlaying': plex_service.is_playing,
            'currentTime': real_time,
            'from_sid': 'System'
        }, room=sid)


@sio.event
async def disconnect(sid):
    # Remove them from the list when they close the tab
    if sid in sid_to_username:
        username = sid_to_username[sid]
        del sid_to_username[sid]
        # Optional: Announce they left
        await sio.emit('chat_message', {'user': 'System', 'text': f"{username} left."})


if __name__ == "__main__":
    import uvicorn

    # Clean previous stream files
    streamer.cleanup_old_files()
    uvicorn.run(asgi_app, host="0.0.0.0", port=8505)