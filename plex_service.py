import os
import shutil
import subprocess
import time
import re
from plexapi.server import PlexServer

# ─── CONFIGURATION ──────────────────────────
PLEX_URL = 'http://127.0.0.1:32400'
PLEX_TOKEN = 'uHmJsmLp1jo-BxJKWQGU'

# NEW: Save to 'stream/plex' subfolder
STREAM_DIR = os.path.join(os.getcwd(), "stream", "plex")


class PlexManager:
    def __init__(self):
        self.current_process = None
        self.current_movie_title = "Waiting for link..."
        self.current_movie_id = None
        self.current_movie_duration = 0

        # SYNC VARIABLES
        self.is_playing = False
        self.current_time = 0
        self.last_system_time = 0

        # *** THIS IS THE FIX ***
        # We attach the global variable to 'self' so main.py can read it
        self.STREAM_DIR = STREAM_DIR

        try:
            self.server = PlexServer(PLEX_URL, PLEX_TOKEN)
            print("Plex Connected Successfully")
        except Exception as e:
            print(f"Plex Connection Failed: {e}")
            self.server = None

    def get_real_time(self):
        if self.is_playing:
            delta = time.time() - self.last_system_time
            t = self.current_time + delta
            # clamp
            if self.current_movie_duration:
                t = max(0, min(t, self.current_movie_duration))
            return t
        return self.current_time

    def update_state(self, is_playing, current_time):
        if self.current_movie_duration:
            current_time = max(0, min(current_time, self.current_movie_duration))

        self.is_playing = bool(is_playing)
        self.current_time = float(current_time)

        # This is the important line you were missing:
        self.last_system_time = time.time()

        print(f"State Updated: Playing={self.is_playing}, Time={self.current_time}")
    def extract_id_from_url(self, url_or_id):
        if str(url_or_id).isdigit(): return int(url_or_id)
        match = re.search(r'metadata%2F(\d+)', str(url_or_id))
        if match: return int(match.group(1))
        match = re.search(r'metadata\/(\d+)', str(url_or_id))
        if match: return int(match.group(1))
        return None

    def start_transcode_from_link(self, link):
        self.stop_transcode()

        rating_key = self.extract_id_from_url(link)
        if not rating_key:
            print("❌ Could not find ID in link")
            return False

        if not self.server:
            print("❌ Plex server not connected")
            return False

        try:
            item = self.server.fetchItem(rating_key)
            file_path = item.media[0].parts[0].file

            if not os.path.exists(file_path):
                print(f"❌ FATAL ERROR: Python cannot find the file at: {file_path}")
                return False

            # --- Title / Metadata ---
            self.current_movie_title = item.title
            if item.type == 'episode':
                self.current_movie_title = f"{item.grandparentTitle} - S{item.parentIndex}E{item.index}"

            self.current_movie_id = rating_key
            self.current_movie_duration = (item.duration / 1000) if item.duration else 0

            # --- Ensure Plex Stream Dir ---
            if not os.path.exists(STREAM_DIR):
                os.makedirs(STREAM_DIR)

            # --- Clean ONLY Plex Stream Dir ---
            for f in os.listdir(STREAM_DIR):
                try:
                    os.remove(os.path.join(STREAM_DIR, f))
                except:
                    pass

            # ✅ Put logs somewhere stable (not in the plex folder full of segments)
            LOG_DIR = os.path.join(os.getcwd(), "logs")
            os.makedirs(LOG_DIR, exist_ok=True)
            log_path = os.path.join(LOG_DIR, "ffmpeg_plex.log")

            # ✅ Use RELATIVE names so the playlist references "seg_000.ts"
            # and the browser can request /stream/plex/seg_000.ts correctly.
            output_file = "stream.m3u8"
            segment_pattern = "seg_%03d.ts"

            # ✅ AUTO-DETECT FFMPEG (Windows friendly)
            ffmpeg_exe = "ffmpeg"
            known_paths = [
                r"C:\ffmpeg\bin\ffmpeg.exe",
                r"C:\Program Files\ffmpeg\bin\ffmpeg.exe"
            ]
            for p in known_paths:
                if os.path.exists(p):
                    ffmpeg_exe = p
                    break

            cmd = [
                ffmpeg_exe, "-y",
                "-hide_banner",
                "-loglevel", "warning",
                "-probesize", "50M",
                "-analyzeduration", "100M",
                "-i", file_path,

                # --- VIDEO ---
                "-c:v", "libx264",
                "-preset", "ultrafast",
                "-tune", "zerolatency",
                "-crf", "23",
                "-maxrate", "10M",
                "-bufsize", "20M",
                "-pix_fmt", "yuv420p",
                # Keyframe every 4s (must match hls_time)
                "-force_key_frames", "expr:gte(t,n_forced*4)",

                # --- AUDIO ---
                "-c:a", "aac",
                "-b:a", "192k",
                "-ac", "2",

                # --- HLS ---
                "-f", "hls",
                "-hls_time", "4",
                "-hls_list_size", "0",
                "-hls_playlist_type", "event",
                "-start_number", "0",
                "-hls_segment_filename", segment_pattern,

                output_file
            ]

            print(f"🎬 Starting Transcode (Plex): {self.current_movie_title}")
            print("▶️ CMD:", " ".join(cmd))
            print("📄 FFmpeg log:", log_path)

            # Write logs to a stable file
            log_file = open(log_path, "w", encoding="utf-8", errors="ignore")

            # ⭐ CRITICAL: cwd=STREAM_DIR makes m3u8 refer to relative seg_###.ts
            self.current_process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=log_file,
                cwd=STREAM_DIR
            )

            # Wait for playlist to appear
            start_time = time.time()
            output_path = os.path.join(STREAM_DIR, output_file)

            while True:
                if os.path.exists(output_path) and os.path.getsize(output_path) > 0:
                    print("✅ Plex Stream started!")
                    return True

                if self.current_process.poll() is not None:
                    print("❌ FFmpeg died unexpectedly. Check log:", log_path)
                    return False

                if time.time() - start_time > 40:
                    print("❌ Timeout waiting for stream file. Check log:", log_path)
                    return False

                time.sleep(0.5)

        except Exception as e:
            print(f"Transcode Error: {e}")
            return False

    def stop_transcode(self):
        if self.current_process:
            try:
                self.current_process.terminate()
            except:
                pass
            self.current_process = None


plex_service = PlexManager()