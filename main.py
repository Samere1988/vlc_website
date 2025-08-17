from fastapi import FastAPI, Request, Form, Depends, Body
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
import os, sqlite3, asyncio, socketio
from aiortc import RTCPeerConnection, RTCSessionDescription
from aiortc.contrib.media import MediaPlayer, MediaRelay
import time
from collections import defaultdict

# ─────────────── App / Auth setup ───────────────
SECRET_KEY = "super-secret"
RESET_SECRET = "reset-secret"
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
serializer = URLSafeTimedSerializer(RESET_SECRET)

sio = socketio.AsyncServer(async_mode='asgi', cors_allowed_origins='*')
fastapi_app = FastAPI()
fastapi_app.add_middleware(PatchedSessionMiddleware, secret_key=SECRET_KEY)
asgi_app = socketio.ASGIApp(sio, fastapi_app)

# Keep static mount if you still have old assets in "stream"
if os.path.isdir("stream"):
    fastapi_app.mount("/stream", StaticFiles(directory="stream"), name="stream")

templates = Jinja2Templates(directory="templates")

# ─────────────── Database (users) ───────────────
Base = declarative_base()
engine = create_engine("sqlite:///./users.db", connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine)
online_users = set()

online_last_seen: dict[str, float] = {}
sid_to_username: dict[str, str] = {}
username_conn_counts = defaultdict(int)

# --- put this helper anywhere near the top of main.py ---
def _sdp_force_high_bitrate(sdp: str, max_kbps: int = 10000) -> str:
    """
    Munges SDP to request high video bitrate:
      - inserts b=AS / b=TIAS in the video m-section
      - adds x-google-{start,min,max}-bitrate for VP8/VP9 payload types
    Returns a new SDP string.
    """
    lines = sdp.splitlines()
    out = []
    in_video = False
    injected_b_line = False
    vp8_pt = None
    vp9_pt = None

    # First pass: copy lines, remember VP8/VP9 payload types, and inject b= lines in video section
    for i, line in enumerate(lines):
        if line.startswith("m="):
            in_video = line.startswith("m=video")
            injected_b_line = False
            out.append(line)
            continue

        if in_video and (line.startswith("c=") and not injected_b_line):
            # Insert bitrate lines immediately after the connection line
            out.append(line)
            out.append(f"b=AS:{max_kbps}")
            out.append(f"b=TIAS:{max_kbps * 1000}")
            injected_b_line = True
            continue

        if line.startswith("a=rtpmap:"):
            # a=rtpmap:<pt> <codec>/<clock>
            try:
                pt = line.split(":")[1].split()[0]
            except Exception:
                pt = None
            if "VP8/90000" in line:
                vp8_pt = pt
            elif "VP9/90000" in line:
                vp9_pt = pt

        out.append(line)

    # Second pass: if we saw VP8/VP9, append fmtp hints (ok to append at end)
    def add_fmtp(pt):
        if not pt:
            return
        # If there's an existing fmtp for this PT, append the x-google params; else add new fmtp
        for idx, l in enumerate(out):
            if l.startswith(f"a=fmtp:{pt} "):
                if "x-google-max-bitrate" not in l:
                    out[idx] = (
                        f"{l};x-google-start-bitrate={max_kbps};"
                        f"x-google-min-bitrate={max_kbps};x-google-max-bitrate={max_kbps}"
                    )
                return
        out.append(
            f"a=fmtp:{pt} x-google-start-bitrate={max_kbps};"
            f"x-google-min-bitrate={max_kbps};x-google-max-bitrate={max_kbps}"
        )

    add_fmtp(vp8_pt)
    add_fmtp(vp9_pt)

    return "\r\n".join(out) + "\r\n"


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

# ─────────────── Channels DB (SQLite) ───────────────
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
    for ch in rows:
        ch['Favorites'] = bool(ch.get('Favorites', 0))
        ch['is_playing'] = (ch['id'] == current_channel_id)
    conn.close()
    return rows

# ─────────────── WebRTC broadcaster state ───────────────
relay = MediaRelay()
player = None                 # shared MediaPlayer (current source)
current_channel_id = None
peers: set[RTCPeerConnection] = set()
player_lock = asyncio.Lock()

async def set_channel_from_id(channel_id: int):
    """
    Replace the shared MediaPlayer from the channel's URL.
    Decodes once; all viewers subscribe via relay.
    """
    global player, current_channel_id
    channel = get_channel_by_id(channel_id)
    if not channel:
        raise ValueError("Channel not found")

    url = channel['url']

    # stop old player if any
    old = player
    player = None
    if old and hasattr(old, "stop"):
        try:
            old.stop()
        except Exception:
            pass

    # aiortc MediaPlayer pulls RTSP/RTMP/HLS/HTTP using ffmpeg via PyAV
    new_player = MediaPlayer(url)

    player = new_player
    current_channel_id = channel_id
    mark_playing(channel_id)

    # notify clients
    await sio.emit('channel_changed', {'channel_id': channel_id, 'message': f"Started: {channel['name']}"})

# ─────────────── Socket.IO events ───────────────
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
        username = user.username
        online_users.add(username)
        username_conn_counts[username] += 1
        sid_to_username[sid] = username
        online_last_seen[username] = time.time()

    await sio.emit("status", {
        "is_streaming": current_channel_id is not None,
        "current_channel_id": current_channel_id
    })

@sio.event
async def disconnect(sid):
    username = sid_to_username.pop(sid, None)
    if not username:
        return
    cnt = username_conn_counts.get(username, 0) - 1
    if cnt <= 0:
        username_conn_counts.pop(username, None)
        # do not immediately drop; let pruning remove if no more heartbeats
        # (but you can also discard here if you prefer hard-drop)
        online_users.discard(username)
        online_last_seen.pop(username, None)
    else:
        username_conn_counts[username] = cnt
        online_last_seen[username] = time.time()

# ─────────────── Routes / Pages ───────────────
@fastapi_app.get("/", response_class=HTMLResponse)
async def index(request: Request, db: Session = Depends(get_db)):
    user_email = request.session.get("user")
    user = db.query(User).filter(User.email == user_email).first() if user_email else None
    if not user:
        return RedirectResponse("/login")
    now_playing = get_channel_by_id(current_channel_id) if current_channel_id else None
    return templates.TemplateResponse("index.html", {
        "request": request,
        "user": user,
        "now_playing": now_playing
    })
@fastapi_app.post("/api/heartbeat")
async def api_heartbeat(request: Request, db: Session = Depends(get_db)):
    user_email = request.session.get("user")
    if not user_email:
        return {"ok": False}
    user = db.query(User).filter(User.email == user_email).first()
    if not user:
        return {"ok": False}
    username = user.username
    online_users.add(username)
    online_last_seen[username] = time.time()
    return {"ok": True}
@fastapi_app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("security/login_user.html", {"request": request})

@fastapi_app.post("/login")
async def login(request: Request, email: str = Form(...), password: str = Form(...), db: Session = Depends(get_db)):
    user = db.query(User).filter(User.email == email).first()
    if not user or not pwd_context.verify(password, user.hashed_password):
        return templates.TemplateResponse("security/login_user.html", {"request": request, "error": "Invalid credentials"})
    request.session["user"] = user.email
    online_users.add(user.username)
    return RedirectResponse("/", status_code=302)

@fastapi_app.get("/logout")
async def logout(request: Request, db: Session = Depends(get_db)):
    user_email = request.session.get("user")
    if user_email:
        user = db.query(User).filter(User.email == user_email).first()
        if user:
            username = user.username
            username_conn_counts.pop(username, None)
            online_users.discard(username)
            online_last_seen.pop(username, None)
        request.session.clear()
    return RedirectResponse("/login")
# ─────────────── REST APIs ───────────────
@fastapi_app.get("/api/online_users")
async def get_online_users():
    now = time.time()
    cutoff = now - 15  # allow 3×5s grace
    # prune stale
    for u, ts in list(online_last_seen.items()):
        if ts < cutoff and username_conn_counts.get(u, 0) == 0:
            online_last_seen.pop(u, None)
            online_users.discard(u)
    return sorted(list(online_users))

@fastapi_app.get("/api/channels")
async def api_channels():
    return list_channels()

@fastapi_app.get("/api/status")
async def api_status():
    return {"is_streaming": current_channel_id is not None, "current_channel_id": current_channel_id}

@fastapi_app.post("/api/play/{channel_id}")
async def api_play(channel_id: int):
    async with player_lock:
        try:
            await set_channel_from_id(channel_id)
            return {"success": True, "message": "OK"}
        except Exception as e:
            return {"success": False, "message": str(e)}

@fastapi_app.post("/api/stop")
async def api_stop():
    global player, current_channel_id
    async with player_lock:
        old = player
        player = None
        current_channel_id = None
        mark_playing(None)
        if old and hasattr(old, "stop"):
            try:
                old.stop()
            except Exception:
                pass
    await sio.emit('stream_stopped')
    return {"success": True, "message": "Stopped"}

# ─────────────── WebRTC signaling ───────────────
@fastapi_app.post("/webrtc/offer")
async def webrtc_offer(sdp: dict = Body(...)):
    if player is None:
        return JSONResponse({"error": "No active source"}, status_code=409)

    pc = RTCPeerConnection()
    peers.add(pc)

    @pc.on("connectionstatechange")
    async def on_state_change():
        if pc.connectionState in ("failed", "closed", "disconnected"):
            await pc.close()
            peers.discard(pc)

    # 1) Set remote offer
    offer = RTCSessionDescription(sdp["sdp"], sdp["type"])
    await pc.setRemoteDescription(offer)

    # 2) Attach shared tracks via relay (single decode → multi fan-out)
    v = player.video and relay.subscribe(player.video)
    a = player.audio and relay.subscribe(player.audio)
    if a:
        pc.addTrack(a)
    if v:
        pc.addTrack(v)

    # 3) Create answer, munge SDP to request max bitrate, then set it
    answer = await pc.createAnswer()
    munged = _sdp_force_high_bitrate(answer.sdp, max_kbps=10000)  # 10 Mbps; raise if you like
    await pc.setLocalDescription(RTCSessionDescription(munged, answer.type))

    return {"sdp": pc.localDescription.sdp, "type": pc.localDescription.type}

