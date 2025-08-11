from fastapi import FastAPI, Request, Form, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from session_middleware_patch import PatchedSessionMiddleware
from sqlalchemy.orm import Session
from passlib.context import CryptContext
from itsdangerous import URLSafeTimedSerializer
from sqlalchemy import create_engine, Column, Integer, String
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.gzip import GZipMiddleware
import os, shutil, subprocess, threading, sqlite3, asyncio, time, socketio

# ──────────────── SETUP ────────────────
SECRET_KEY = "super-secret"
RESET_SECRET = "reset-secret"
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
serializer = URLSafeTimedSerializer(RESET_SECRET)

sio = socketio.AsyncServer(async_mode='asgi', cors_allowed_origins='*')
fastapi_app = FastAPI()
fastapi_app.add_middleware(PatchedSessionMiddleware, secret_key=SECRET_KEY)
asgi_app = socketio.ASGIApp(sio, fastapi_app)

fastapi_app.mount("/stream", StaticFiles(directory="stream"), name="stream")
templates = Jinja2Templates(directory="templates")

# ──────────────── DATABASE ────────────────
Base = declarative_base()
engine = create_engine("sqlite:///./users.db", connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine)
online_users = {}

@fastapi_app.middleware("http")
async def no_cache_static_headers(request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/stream/"):
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
    return response
@sio.event
async def connect(sid, environ):
    scope = environ.get("asgi.scope")
    session = scope.get("session") if scope else None
    if not session:
        return

    email = session.get("user")
    if not email:
        return

    db = SessionLocal()
    user = db.query(User).filter(User.email == email).first()
    db.close()

    if user:
        online_users[sid] = user.username  # ✅ Map sid → username

    await sio.emit("online_users", list(online_users.values()))




@sio.event
async def disconnect(sid):
    if sid in online_users:
        del online_users[sid]
        await sio.emit("online_users", list(online_users.values()))

@fastapi_app.get("/api/online_users")
async def get_online_users():
    return list(online_users.values())

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    username = Column(String, unique=True)
    email = Column(String, unique=True)
    hashed_password = Column(String)

Base.metadata.create_all(bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# ──────────────── STREAMING ────────────────
class IPTVStreamer:
    def __init__(self, db_path="channels.db"):
        self.db_path = db_path
        self.current_channel_id = None
        self.current_process = None

    def cleanup_old_files(self):
        for file in os.listdir("stream"):
            if file.endswith((".m3u8", ".ts")):
                try:
                    os.remove(os.path.join("stream", file))
                except:
                    pass

    def get_channels(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM channels ORDER BY name")
        channels = [dict(row) for row in cursor.fetchall()]
        for ch in channels:
            ch['is_playing'] = (ch['id'] == self.current_channel_id)
            ch['Favorites'] = bool(ch.get('Favorites', 0))
        conn.close()
        return channels

    def get_channel_by_id(self, channel_id):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM channels WHERE id = ?", (channel_id,))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None

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

        stream_path = os.path.join("stream", "stream.m3u8")

        ffmpeg_paths = ['ffmpeg', r'C:\\ffmpeg\\bin\\ffmpeg.exe', r'C:\\Program Files\\ffmpeg\\bin\\ffmpeg.exe', 'ffmpeg.exe']
        ffmpeg_exe = next((p for p in ffmpeg_paths if shutil.which(p)), None)
        if not ffmpeg_exe:
            return False, "FFmpeg not found."

        ffmpeg_cmd = [
            ffmpeg_exe,
            '-re',
            '-rw_timeout', '5000000',
            '-i', channel['url'],
            '-vf', 'scale=1920:-1',
            '-c:v', 'libx264', '-preset', 'veryfast', '-tune', 'zerolatency',
            '-b:v', '3000k', '-maxrate', '3500k', '-bufsize', '5000k',
            '-profile:v', 'baseline', '-level', '3.0', '-pix_fmt', 'yuv420p',
            '-r', '25', '-g', '100', '-sc_threshold', '0',
            '-c:a', 'aac', '-b:a', '128k', '-ar', '44100',
            '-f', 'hls',
            '-hls_time', '4',
            '-hls_list_size', '6',
            '-hls_flags', 'delete_segments+independent_segments',
            '-hls_segment_filename', os.path.join("stream", "segment_%03d.ts"),
            '-hls_start_number_source', 'epoch',
            '-hls_segment_type', 'mpegts',
            '-force_key_frames', 'expr:gte(t,n_forced*4)',
            '-y', stream_path
        ]

        try:
            self.current_process = subprocess.Popen(ffmpeg_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            threading.Thread(target=self._monitor_stream, daemon=True).start()
            time.sleep(2)
            return True, f"Started streaming: {channel['name']}"
        except Exception as e:
            return False, f"Failed to start stream: {str(e)}"

    def _monitor_stream(self):
        proc = self.current_process
        if proc:
            _, stderr = proc.communicate()
            if proc.returncode != 0:
                print(f"Stream error: {stderr.decode()}")
                asyncio.run(sio.emit('stream_error', {'message': 'Stream ended unexpectedly'}))

                # Attempt auto-restart
                if self.current_channel_id:
                    print("Attempting to auto-restart stream...")
                    time.sleep(3)  # brief delay to avoid thrashing
                    self.start_stream(self.current_channel_id)

            self.current_process = None

streamer = IPTVStreamer()

# ──────────────── ROUTES ────────────────
@fastapi_app.get("/", response_class=HTMLResponse)
async def index(request: Request, db: Session = Depends(get_db)):
    user_email = request.session.get("user")
    user = db.query(User).filter(User.email == user_email).first() if user_email else None

    if user:
        online_users[user_email] = user.username  # 👈 Track by email to username

    if not user:
        return RedirectResponse("/login")

    now_playing = streamer.get_channel_by_id(streamer.current_channel_id)
    channels = streamer.get_channels()
    favorites = [ch for ch in channels if ch.get("Favorites")]

    return templates.TemplateResponse("index.html", {
        "request": request,
        "user": user,
        "now_playing": now_playing,
        "channels": channels,
        "favorites": favorites
    })

@fastapi_app.get("/forgot-password", name="forgot_password")
async def forgot_password(request: Request):
    return HTMLResponse("<h2>Forgot password is not implemented yet</h2>")

@fastapi_app.post("/forgot-password", response_class=HTMLResponse)
async def forgot_password_submit(request: Request, email: str = Form(...)):
    # You can implement email reset logic here
    return templates.TemplateResponse("forgot_password.html", {
        "request": request,
        "message": f"If {email} exists, a reset link will be sent."
    })
@fastapi_app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("security/login_user.html", {"request": request})

@fastapi_app.post("/login")
async def login(request: Request, email: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == email).first()
    if not user or not pwd_context.verify(password, user.hashed_password):
        return templates.TemplateResponse("security/login_user.html", {"request": request, "error": "Invalid credentials"})
    request.session["user"] = user.email
    online_users[user.username] = True
    return RedirectResponse("/", status_code=302)

@fastapi_app.get("/logout")
async def logout(request: Request):
    user_email = request.session.get("user")
    if user_email:
        request.session.clear()
        online_users.discard(user_email)
    return RedirectResponse("/login")
@fastapi_app.get("/register", name="register")
async def register_page(request: Request):
    return templates.TemplateResponse("security/register_user.html", {"request": request})

@fastapi_app.post("/register")
async def register_user(request: Request,
                        username: str = Form(...),
                        email: str = Form(...),
                        password: str = Form(...),
                        password_confirm: str = Form(...),
                        db: Session = Depends(get_db)):
    if password != password_confirm:
        return templates.TemplateResponse("security/register_user.html", {
            "request": request,
            "error": "Passwords do not match"
        })

    existing = db.query(User).filter((User.email == email) | (User.username == username)).first()
    if existing:
        return templates.TemplateResponse("security/register_user.html", {
            "request": request,
            "error": "Email or username already exists"
        })

    user = User(username=username, email=email, hashed_password=pwd_context.hash(password))
    db.add(user)
    db.commit()
    return RedirectResponse("/login", status_code=302)

@fastapi_app.get("/api/channels")
async def api_channels():
    return streamer.get_channels()

@fastapi_app.get("/api/status")
async def api_status():
    return {
        "is_streaming": streamer.current_channel_id is not None,
        "current_channel_id": streamer.current_channel_id
    }

@fastapi_app.get("/api/online_users")
async def api_online_users():
    return list(online_users.keys())

@fastapi_app.post("/api/play/{channel_id}")
async def api_play(channel_id: int):
    success, message = streamer.start_stream(channel_id)
    await sio.emit('channel_changed', {'channel_id': channel_id, 'message': message})
    return {"success": success, "message": message}

@fastapi_app.post("/api/stop")
async def api_stop():
    streamer.stop_current_stream()
    await sio.emit('stream_stopped')
    return {"success": True, "message": "Stream stopped"}

