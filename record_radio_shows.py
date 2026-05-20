import os
import subprocess
import math
import json
import shutil
import tempfile
import re
import argparse
import platform
from datetime import datetime
from email.utils import format_datetime
from typing import Callable
from xml.etree.ElementTree import Element, SubElement, ElementTree
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

# =======================
# CONFIG
# =======================

# Folders / files
MP3_FOLDER = "episodes_mp3"
RSS_FILE = "smear_campaign_feed.xml"
TRACK_FILE = "downloaded_episodes.json"

# Podcast + hosting
HOST_NAME = "DJ Tone Deaf"
CHANNEL_IMAGE = "channel_image.jpg"  # put this file in the repo root
CHANNEL_IMAGE_URL = os.getenv("CHANNEL_IMAGE_URL", "").strip()
CATEGORY = "Music"
LANG = "en-US"
EXPLICIT = "no"
BASE_URL = "https://artiefissio.github.io/Podcast-feedS/"  # GitHub Pages base
PUBLISH_BRANCH = os.getenv("PUBLISH_BRANCH", "gh-pages")
PUBLISH_REMOTE = os.getenv("PUBLISH_REMOTE", "origin")

def _env_int(name: str, default: int) -> int:
    val = os.getenv(name)
    if not val:
        return default
    try:
        return int(val)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    val = os.getenv(name)
    if not val:
        return default
    try:
        return float(val)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.lower() in ("1", "true", "yes", "on")


RETENTION_DAYS = _env_int("RETENTION_DAYS", 14)


# Audio / ffmpeg
STREAM_URL = os.getenv("STREAM_URL", "https://ktal.broadcasttool.stream/stream")
AUDIO_BITRATE = os.getenv("AUDIO_BITRATE", "192k")  # 192 kbps as requested
MAX_SIZE_BYTES = 99 * 1024 * 1024  # 99 MB split threshold

FFMPEG_BIN = os.getenv("FFMPEG_BIN", "/opt/homebrew/bin/ffmpeg")
FFPROBE_BIN = os.getenv("FFPROBE_BIN", "/opt/homebrew/bin/ffprobe")

# Git auto-push
GITHUB_PUSH = _env_bool("GITHUB_PUSH", True)

# iMessage notification. Leave blank on non-Mac hosts such as a VPS.
IMESSAGE_NUMBER = os.getenv("IMESSAGE_NUMBER", "")

# Timing / locking
LOCK_STALE_SECONDS = _env_int("LOCK_STALE_SECONDS", 3 * 3600)
WAIT_FOR_HOUR = _env_bool("WAIT_FOR_HOUR", False)
WAIT_MAX_SECONDS = _env_int("WAIT_MAX_SECONDS", 15 * 60)

# Logging
LOG_FILE = os.getenv("LOG_FILE", "logs/record_radio_shows.log")
LOG_MAX_BYTES = _env_int("LOG_MAX_BYTES", 5 * 1024 * 1024)

# Show schedule (0 = Monday, 6 = Sunday in Python, BUT we’ll map to cron usage)
# Python weekday(): Monday=0 ... Sunday=6
SHOWS = [
    {
        "name": "Wolfman Max – Wide World of Funk",
        "day": 5,  # Saturday
        "hours": [17, 18],  # 5–6pm, 6–7pm
        "spinitron_archive": None,
    },
    {
        "name": "The Smear Campaign",
        "day": 5,  # Saturday
        "hours": [19],  # 7–8pm
        "spinitron_archive": "https://spinitron.com/KTAL/show/277361/The-Smear-Campaign?layout=1",
    },
    {
        "name": "Johnny Catalog – Catalog",
        "day": 5,  # Saturday
        "hours": [20],  # 8–9pm
        "spinitron_archive": None,
    },
    {
        "name": "Brain Salad",
        "day": 5,  # Saturday
        "hours": [21],  # 9–10pm
        "spinitron_archive": None,
    },
    {
        "name": "Lost Highway",
        "day": 0,  # Monday
        "hours": [20],  # 8–9pm
        "spinitron_archive": None,
    },
    {
        "name": "Soul Salad",
        "day": 6,  # Sunday
        "hours": [16],  # 4–5pm
        "spinitron_archive": "https://spinitron.com/KTAL/show/290230/Soul-Salad?layout=1",
    },
]

# =======================
# Helpers
# =======================

SESSION = None


def resolve_bin(configured: str, fallback: str) -> str:
    if configured and os.path.exists(configured):
        return configured
    found = shutil.which(fallback)
    return found or configured


def get_session() -> requests.Session:
    global SESSION
    if SESSION is not None:
        return SESSION
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "PodcastFeedBot/1.0 (+https://github.com/artiefissio/Podcast-feedS)",
            "Accept-Language": "en-US,en;q=0.9",
        }
    )
    adapter = requests.adapters.HTTPAdapter(max_retries=3)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    SESSION = session
    return SESSION


def write_atomic(path: str, write_fn: Callable[[str], None]) -> None:
    """
    Write a file atomically by writing to a temp file then replacing.
    """
    base_dir = os.path.dirname(path) or "."
    fd, tmp_path = tempfile.mkstemp(dir=base_dir, prefix=".tmp_")
    os.close(fd)
    try:
        write_fn(tmp_path)
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def send_imessage(phone_number: str, message: str) -> None:
    """
    Sends an iMessage via AppleScript.
    Your Mac must be logged into Messages with this number / Apple ID.
    """
    if not phone_number:
        return
    if platform.system() != "Darwin" or not shutil.which("osascript"):
        log_line("[INFO] iMessage notification skipped: osascript unavailable.")
        return

    apple_script = f'''
    tell application "Messages"
        set targetService to 1st service whose service type = iMessage
        set targetBuddy to buddy "{phone_number}" of targetService
        send "{message}" to targetBuddy
    end tell
    '''
    try:
        subprocess.run(["osascript", "-e", apple_script], check=False)
    except Exception as e:
        print(f"iMessage send error: {e}")


def channel_image_href() -> str | None:
    """
    Returns best image href for podcast metadata.
    Prefers absolute CHANNEL_IMAGE_URL, then local CHANNEL_IMAGE on BASE_URL.
    """
    if CHANNEL_IMAGE_URL:
        return CHANNEL_IMAGE_URL
    if os.path.exists(CHANNEL_IMAGE) and os.path.getsize(CHANNEL_IMAGE) > 0:
        return BASE_URL + CHANNEL_IMAGE
    return None


def normalize_title_for_filename(title: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", title)
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    return cleaned or "episode"


def create_test_recording(mp3_file: str, duration_seconds: int = 10) -> bool:
    """
    Generate a short silent MP3 for pipeline verification.
    """
    cmd = [
        FFMPEG_BIN,
        "-f", "lavfi",
        "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
        "-t", str(max(1, duration_seconds)),
        "-c:a", "libmp3lame",
        "-b:a", AUDIO_BITRATE,
        "-y",
        mp3_file,
    ]
    result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return result.returncode == 0 and os.path.exists(mp3_file)


def ensure_dirs() -> None:
    os.makedirs(MP3_FOLDER, exist_ok=True)
    if LOG_FILE:
        os.makedirs(os.path.dirname(LOG_FILE) or ".", exist_ok=True)


def load_downloaded() -> dict:
    if os.path.exists(TRACK_FILE):
        with open(TRACK_FILE, "r") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return {}
    return {}


def save_downloaded(downloaded: dict) -> None:
    def _write(path: str) -> None:
        with open(path, "w") as f:
            json.dump(downloaded, f, indent=2)

    write_atomic(TRACK_FILE, _write)


def split_mp3(file_path: str, max_size: int = MAX_SIZE_BYTES) -> list:
    """
    If MP3 > max_size, split into multiple parts using ffmpeg + ffprobe.
    Returns list of part file paths (or [file_path] if no split).
    """
    if not os.path.exists(file_path):
        print(f"[split_mp3] File does not exist: {file_path}")
        return []

    size_bytes = os.path.getsize(file_path)
    if size_bytes <= max_size:
        return [file_path]

    print(f"[split_mp3] Splitting {file_path} (size {size_bytes} bytes)")

    # Get duration in seconds
    try:
        duration_sec_str = subprocess.getoutput(
            f"{FFPROBE_BIN} -v error -show_entries format=duration "
            f"-of default=noprint_wrappers=1:nokey=1 '{file_path}'"
        )
        duration_sec = float(duration_sec_str)
    except Exception as e:
        print(f"[split_mp3] ffprobe error: {e}")
        return [file_path]

    parts = math.ceil(size_bytes / max_size)
    segment_time = math.ceil(duration_sec / parts)
    part_files = []

    for i in range(parts):
        start = i * segment_time
        part_filename = file_path.replace(".mp3", f"_part{i+1:03d}.mp3")
        cmd = [
            FFMPEG_BIN,
            "-i", file_path,
            "-ss", str(start),
            "-t", str(segment_time),
            "-c", "copy",
            "-y", part_filename
        ]
        result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if result.returncode != 0:
            print(f"[split_mp3] ffmpeg failed for part {i+1}")
        if os.path.exists(part_filename):
            part_files.append(part_filename)

    # Remove original if splitting succeeded
    if len(part_files) == parts:
        os.remove(file_path)
        return part_files

    for part in part_files:
        try:
            os.remove(part)
        except Exception:
            pass
    return [file_path]


def rfc2822(dt: datetime) -> str:
    # For podcast pubDate (RFC 2822 with timezone)
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return format_datetime(dt)


def log_line(message: str) -> None:
    ts = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
    line = f"{ts} {message}\n"
    try:
        os.makedirs(os.path.dirname(LOG_FILE) or ".", exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(line)
        if os.path.getsize(LOG_FILE) > LOG_MAX_BYTES:
            rotated = LOG_FILE + ".1"
            if os.path.exists(rotated):
                os.remove(rotated)
            os.replace(LOG_FILE, rotated)
    except Exception:
        pass


def acquire_lock(lock_path: str, stale_seconds: int = 3 * 3600) -> None:
    """
    Prevent overlapping runs with a simple lockfile.
    """
    if os.path.exists(lock_path):
        try:
            age = datetime.now().timestamp() - os.path.getmtime(lock_path)
            if age < stale_seconds:
                raise RuntimeError(f"Lock exists (age {int(age)}s): {lock_path}")
            log_line(f"[LOCK] Removing stale lock (age {int(age)}s): {lock_path}")
            os.remove(lock_path)
        except Exception as e:
            raise RuntimeError(f"Lock exists and cannot be cleared: {e}")
    with open(lock_path, "w") as f:
        f.write(f"{os.getpid()}\n{datetime.now().isoformat()}\n")
    log_line(f"[LOCK] Acquired lock: {lock_path}")


def wait_until_top_of_hour(now: datetime) -> datetime:
    if not WAIT_FOR_HOUR:
        return now
    start_dt = now.replace(minute=0, second=0, microsecond=0)
    if now == start_dt:
        return now
    seconds_to_wait = int((start_dt.timestamp() + 3600) - now.timestamp())
    if seconds_to_wait > WAIT_MAX_SECONDS:
        log_line(f"[WAIT] Skipping wait ({seconds_to_wait}s > {WAIT_MAX_SECONDS}s)")
        return now
    log_line(f"[WAIT] Sleeping {seconds_to_wait}s until top of hour")
    try:
        import time
        time.sleep(max(0, seconds_to_wait))
    except Exception:
        return datetime.now().astimezone()
    return datetime.now().astimezone()


# --- Spinitron helpers (best-effort) ---

def _absolute_spinitron_url(value: str | None) -> str:
    if not value:
        return ""
    return urljoin("https://spinitron.com", value)


def _extract_tracklist_html(pl_soup: BeautifulSoup, limit: int = 20) -> str:
    items: list[str] = []
    row_selectors = [
        "table.spins tbody tr",
        "table.playlist tbody tr",
        ".playlist-tracks tr",
        ".spins tr",
    ]
    for selector in row_selectors:
        for row in pl_soup.select(selector):
            artist = row.select_one(".artist, .spin-artist, td.artist")
            song = row.select_one(".song, .title, .spin-song, td.song, td.title")
            artist_text = artist.get_text(" ", strip=True) if artist else ""
            song_text = song.get_text(" ", strip=True) if song else ""
            line = " - ".join([part for part in [artist_text, song_text] if part]).strip()
            if line and line not in items:
                items.append(line)
                if len(items) >= limit:
                    break
        if len(items) >= limit:
            break

    if not items:
        for node in pl_soup.select("div.playlist-tracks li, ul.playlist-tracks li, .spins li"):
            text = node.get_text(" ", strip=True)
            if text and text not in items:
                items.append(text)
                if len(items) >= limit:
                    break

    if not items:
        return ""
    return "<ul>" + "".join(f"<li>{t}</li>" for t in items) + "</ul>"


def _extract_playlist_meta(pl_soup: BeautifulSoup) -> dict:
    og_image = pl_soup.select_one("meta[property='og:image']")
    image = _absolute_spinitron_url(og_image["content"]) if og_image and og_image.get("content") else ""
    if not image:
        img_elem = pl_soup.select_one(
            "div.playlist-art img, .playlist-art img, .show-art img, img[alt*='art'], img[src*='show']"
        )
        image = _absolute_spinitron_url(img_elem["src"]) if img_elem and img_elem.get("src") else ""

    dj_elem = pl_soup.select_one(".show-dj, .show-host, .host, .field-name-host, .persona a")
    dj = dj_elem.get_text(" ", strip=True) if dj_elem else ""
    if not dj:
        dj = HOST_NAME

    track_html = _extract_tracklist_html(pl_soup)
    return {
        "tracklist_html": track_html,
        "image": image or (channel_image_href() or ""),
        "dj": dj,
    }


def _fetch_playlist_metadata(playlist_url: str) -> dict:
    try:
        pl_resp = get_session().get(playlist_url, timeout=20)
        pl_resp.raise_for_status()
    except Exception as e:
        print(f"[Spinitron] Playlist fetch error: {e}")
        return {}

    pl_soup = BeautifulSoup(pl_resp.text, "html.parser")
    meta = _extract_playlist_meta(pl_soup)
    meta["playlist_url"] = playlist_url
    return meta


def scrape_spinitron_by_name(show_name: str, broadcast_time: datetime):
    """
    Search for a playlist by show name and broadcast time on KTAL homepage.
    Used for shows without dedicated archive pages.
    """
    try:
        resp = get_session().get("https://spinitron.com/KTAL", timeout=15)
        resp.raise_for_status()
    except Exception as e:
        print(f"[Spinitron] Homepage fetch error: {e}")
        return {}

    soup = BeautifulSoup(resp.text, "html.parser")

    candidates = []
    for link in soup.select("a[href*='/KTAL/pl/']"):
        href = link.get("href", "")
        if not href:
            continue
        playlist_url = _absolute_spinitron_url(href.split("?")[0])
        link_text = link.get_text(" ", strip=True).lower()
        context_text = link.parent.get_text(" ", strip=True).lower() if link.parent else ""
        score = 0
        show_lower = show_name.lower()
        if show_lower in link_text:
            score += 3
        if show_lower in context_text:
            score += 2
        if str(broadcast_time.hour) in context_text:
            score += 1
        candidates.append((score, playlist_url))

    if not candidates:
        print(f"[Spinitron] No playlist links found for {show_name}")
        return {}

    candidates.sort(key=lambda item: item[0], reverse=True)
    for _, playlist_url in candidates[:5]:
        print(f"[Spinitron] Trying playlist for {show_name}: {playlist_url}")
        meta = _fetch_playlist_metadata(playlist_url)
        if meta:
            return meta

    print(f"[Spinitron] No usable playlist metadata found for {show_name}")
    return {}


def scrape_spinitron_show(archive_url: str):
    """
    Generic Spinitron scraper for any KTAL show.
    - Finds newest playlist
    - Pulls tracklist
    - Gets show/playlist art
    - Gets DJ/Host name (best effort)
    """
    if not archive_url:
        return {}

    try:
        resp = get_session().get(archive_url, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        print(f"[Spinitron] Archive fetch error: {e}")
        return {}

    soup = BeautifulSoup(resp.text, "html.parser")

    pl_links = soup.select("a[href*='/KTAL/pl/']")
    if not pl_links:
        return {}
    for pl_link in pl_links[:5]:
        href = pl_link.get("href", "")
        if not href:
            continue
        playlist_url = _absolute_spinitron_url(href.split("?")[0])
        meta = _fetch_playlist_metadata(playlist_url)
        if meta:
            return meta
    return {}


def build_episode_description(show_name: str, start_dt: datetime, meta: dict | None) -> str:
    """
    Build nice HTML description for podcast apps.
    """
    date_str = start_dt.strftime("%A %B %d, %Y • %I:%M %p %Z")
    base = [
        f"<p><strong>{show_name}</strong></p>",
        f"<p>Aired: {date_str}</p>",
    ]

    if meta:
        if meta.get("playlist_url"):
            base.append(f"<p><a href='{meta['playlist_url']}'>View playlist on Spinitron</a></p>")
        if meta.get("tracklist_html"):
            base.append("<p>Tracklist:</p>")
            base.append(meta["tracklist_html"])

    return "\n".join(base)


# =======================
# Automated cleanup (keep only the retention window)
# =======================


def episode_paths(downloaded: dict) -> set[str]:
    """
    Return normalized MP3 paths referenced by retained metadata.
    """
    paths: set[str] = set()
    for ep in downloaded.values():
        for file_path in ep.get("mp3_files", []):
            if file_path:
                paths.add(os.path.normpath(file_path))
    return paths


def cleanup_old_episodes(downloaded: dict | None = None, retention_days: int = RETENTION_DAYS):
    """
    Delete MP3 files outside retained metadata.

    If metadata is unavailable, fall back to file mtime and retention_days.
    """
    if not os.path.isdir(MP3_FOLDER):
        return []

    now = datetime.now()
    threshold = now.timestamp() - (retention_days * 86400)
    retained_paths = episode_paths(downloaded) if downloaded is not None else None

    removed = []

    for fname in os.listdir(MP3_FOLDER):
        if not fname.lower().endswith(".mp3"):
            continue
        path = os.path.join(MP3_FOLDER, fname)
        try:
            normalized = os.path.normpath(path)
            if retained_paths is not None:
                should_remove = normalized not in retained_paths
            else:
                should_remove = os.path.getmtime(path) < threshold
            if should_remove:
                os.remove(path)
                removed.append(path)
        except Exception:
            continue
    return removed


def cleanup_downloaded_metadata(downloaded: dict, retention_days: int = RETENTION_DAYS):
    """
    Remove metadata entries outside the retention window.
    """
    now = datetime.now().astimezone()
    threshold = now.timestamp() - (retention_days * 86400)
    new_dl = {}

    for key, ep in downloaded.items():
        iso = ep.get("pubDate_iso")
        if not iso:
            continue
        try:
            dt = datetime.fromisoformat(iso)
        except:
            continue

        if dt.timestamp() >= threshold:
            new_dl[key] = ep

    return new_dl


def normalize_downloaded(downloaded: dict) -> dict:
    """
    Drop metadata for missing audio files and normalize file lists.
    """
    cleaned = {}
    for key, ep in downloaded.items():
        files = ep.get("mp3_files", [])
        existing = [p for p in files if os.path.exists(p)]
        if not existing:
            continue
        ep_copy = dict(ep)
        ep_copy["mp3_files"] = existing
        cleaned[key] = ep_copy
    return cleaned


def cleanup_episode_state(downloaded: dict, retention_days: int = RETENTION_DAYS) -> tuple[dict, list[str], bool]:
    """
    Apply metadata and file cleanup as one retention pass.
    """
    before = json.dumps(downloaded, sort_keys=True)
    retained = cleanup_downloaded_metadata(downloaded, retention_days)
    retained = normalize_downloaded(retained)
    removed_files = cleanup_old_episodes(retained, retention_days)
    after = json.dumps(retained, sort_keys=True)
    changed = before != after or bool(removed_files)
    return retained, removed_files, changed


def publish_snapshot(downloaded: dict, reason: str) -> None:
    """
    Rebuild and optionally publish the current rolling snapshot.
    """
    print(f"[INFO] Publishing snapshot: {reason}")
    log_line(f"[INFO] Publishing snapshot: {reason}")
    build_rss(downloaded)
    if GITHUB_PUSH:
        do_git_push()


def get_active_show(at_dt: datetime):
    today = at_dt.weekday()
    hour = at_dt.hour
    for show in SHOWS:
        if show["day"] == today and hour in show["hours"]:
            return show
    return None


# =======================
# Main recording logic
# =======================

def main(test_run: bool = False):
    ensure_dirs()
    log_line("[INFO] Starting run")
    lock_path = os.path.join(MP3_FOLDER, ".record_lock")
    try:
        acquire_lock(lock_path, LOCK_STALE_SECONDS)
    except RuntimeError as e:
        print(f"[INFO] {e}")
        log_line(f"[INFO] {e}")
        return

    try:
        global FFMPEG_BIN, FFPROBE_BIN
        FFMPEG_BIN = resolve_bin(FFMPEG_BIN, "ffmpeg")
        FFPROBE_BIN = resolve_bin(FFPROBE_BIN, "ffprobe")
        if not FFMPEG_BIN or not os.path.exists(FFMPEG_BIN):
            raise RuntimeError(f"ffmpeg not found: {FFMPEG_BIN}")
        if not FFPROBE_BIN or not os.path.exists(FFPROBE_BIN):
            raise RuntimeError(f"ffprobe not found: {FFPROBE_BIN}")

        downloaded = load_downloaded()

        # Run retention first so every publish is a rolling snapshot, not an archive.
        downloaded, removed_files, cleanup_changed = cleanup_episode_state(downloaded)
        if removed_files:
            print(f"[CLEANUP] Removed {len(removed_files)} old MP3 files.")
            log_line(f"[CLEANUP] Removed {len(removed_files)} old MP3 files.")
        if cleanup_changed:
            save_downloaded(downloaded)
        cleanup_needs_publish = cleanup_changed or not os.path.exists(RSS_FILE)

        now = datetime.now().astimezone()
        print(f"[INFO] Now: {now.isoformat()} (weekday={now.weekday()}, hour={now.hour})")
        log_line(f"[INFO] Now: {now.isoformat()} (weekday={now.weekday()}, hour={now.hour})")

        if test_run:
            show = {"name": "Pipeline Test Episode", "hours": [now.hour], "spinitron_archive": None}
            show_name = show["name"]
            block_index = 1
            start_dt = now
        else:
            # Determine which show (if any) is scheduled *right now*
            active_show = get_active_show(now)
            if not active_show:
                print("Nothing scheduled right now.")
                log_line("[INFO] Nothing scheduled right now.")
                if cleanup_needs_publish:
                    publish_snapshot(downloaded, "retention cleanup with no scheduled show")
                return

            show = active_show
            show_name = show["name"]
            block_index = show["hours"].index(now.hour) + 1

            # Optionally wait until the top of the hour
            start_dt = wait_until_top_of_hour(now)
            if start_dt != now:
                refreshed_show = get_active_show(start_dt)
                if not refreshed_show:
                    log_line("[INFO] No scheduled show after wait; exiting.")
                    if cleanup_needs_publish:
                        publish_snapshot(downloaded, "retention cleanup after schedule wait")
                    return
                active_show = refreshed_show
                show = active_show
                show_name = show["name"]
                block_index = show["hours"].index(start_dt.hour) + 1
        timestamp_str = start_dt.strftime("%Y-%m-%d_%H%M")
        safe_title = normalize_title_for_filename(show_name)
        mp3_file = os.path.join(MP3_FOLDER, f"{timestamp_str}_{safe_title}.mp3")

        print(f"[INFO] Recording show: {show_name} (block {block_index})")
        print(f"[INFO] Output file: {mp3_file}")
        log_line(f"[INFO] Recording show: {show_name} (block {block_index})")
        log_line(f"[INFO] Output file: {mp3_file}")

        # Generic: Try to scrape metadata from Spinitron
        meta = {}
        if not test_run:
            if show.get("spinitron_archive"):
                meta = scrape_spinitron_show(show["spinitron_archive"])
            else:
                print(f"[INFO] Searching Spinitron for {show_name}...")
                meta = scrape_spinitron_by_name(show_name, start_dt)

        if test_run:
            print("[INFO] Running ffmpeg test recording...")
            log_line("[INFO] Running ffmpeg test recording...")
            ok = create_test_recording(mp3_file, duration_seconds=10)
            if not ok:
                print(f"[ERROR] Test recording failed for {show_name}")
                log_line(f"[ERROR] Test recording failed for {show_name}")
                if cleanup_needs_publish:
                    publish_snapshot(downloaded, "retention cleanup after failed test recording")
                return
        else:
            # Record 1 hour from KTAL stream at 192 kbps
            cmd = [
                FFMPEG_BIN,
                "-i", STREAM_URL,
                "-c:a", "libmp3lame",
                "-b:a", AUDIO_BITRATE,
                "-t", "01:00:00",
                "-y",
                mp3_file,
            ]
            print("[INFO] Running ffmpeg...")
            log_line("[INFO] Running ffmpeg...")
            result = subprocess.run(cmd)

            if result.returncode != 0 or not os.path.exists(mp3_file):
                print(f"[ERROR] Recording failed for {show_name}")
                log_line(f"[ERROR] Recording failed for {show_name}")
                if cleanup_needs_publish:
                    publish_snapshot(downloaded, "retention cleanup after failed recording")
                send_imessage(IMESSAGE_NUMBER, f"[ERROR] Failed recording: {show_name} at {timestamp_str}")
                return

        print("[INFO] Recording complete, checking size / splitting...")
        log_line("[INFO] Recording complete, checking size / splitting...")
        mp3_files = split_mp3(mp3_file)

        # Build metadata & store in downloaded list
        ep_key = f"{timestamp_str}_{safe_title}"
        description = build_episode_description(show_name, start_dt, meta)
        episode_image = meta.get("image") or (channel_image_href() or "")
        episode_author = meta.get("dj", HOST_NAME)

        downloaded[ep_key] = {
            "mp3_files": mp3_files,
            "title": f"{show_name} – {start_dt.strftime('%a %b %d, %Y %I%p')}",
            "pubDate_iso": start_dt.isoformat(),
            "description_html": description,
            "episode_image": episode_image,
            "author": episode_author,
        }

        save_downloaded(downloaded)
        print("[INFO] Metadata saved to downloaded_episodes.json")
        log_line("[INFO] Metadata saved to downloaded_episodes.json")

        # iMessage notification
        send_imessage(IMESSAGE_NUMBER, f"[OK] Recorded {show_name} at {timestamp_str} ({len(mp3_files)} file(s))")

        publish_snapshot(downloaded, "recording complete")
    finally:
        try:
            os.remove(lock_path)
        except Exception:
            pass
        log_line("[INFO] Run complete")


# =======================
# RSS builder
# =======================

def build_rss(downloaded: dict) -> None:
    print("[INFO] Rebuilding RSS feed...")
    log_line("[INFO] Rebuilding RSS feed...")

    rss = Element(
        "rss",
        version="2.0",
        attrib={"xmlns:itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd"},
    )
    channel = SubElement(rss, "channel")
    SubElement(channel, "title").text = "Automated KTAL Shows – DJ Tone Deaf"
    SubElement(channel, "link").text = BASE_URL
    SubElement(channel, "description").text = (
        "Automated recordings of KTAL shows (Wolfman Max, The Smear Campaign, Johnny Catalog, "
        "Brain Salad, Lost Highway, Soul Salad) with best-effort metadata."
    )
    SubElement(channel, "language").text = LANG
    SubElement(channel, "itunes:author").text = HOST_NAME
    SubElement(channel, "itunes:explicit").text = EXPLICIT
    ch_image = channel_image_href()
    if ch_image:
        SubElement(channel, "itunes:image", href=ch_image)
    itunes_category = SubElement(channel, "itunes:category")
    itunes_category.set("text", CATEGORY)

    # Sort episodes by date
    items = sorted(downloaded.items(), key=lambda kv: kv[1].get("pubDate_iso", ""), reverse=True)

    for key, ep in items:
        pub_iso = ep.get("pubDate_iso")
        try:
            pub_dt = datetime.fromisoformat(pub_iso) if pub_iso else datetime.now()
        except Exception:
            pub_dt = datetime.now()
        if pub_dt.tzinfo is None:
            pub_dt = pub_dt.astimezone()

        mp3_files = ep.get("mp3_files", [])
        for idx, file_path in enumerate(mp3_files):
            item = SubElement(channel, "item")
            title_text = ep["title"]
            if len(mp3_files) > 1:
                title_text += f" – Part {idx+1}"

            SubElement(item, "title").text = title_text
            SubElement(item, "description").text = ep.get("description_html", "")
            SubElement(item, "pubDate").text = rfc2822(pub_dt)
            SubElement(item, "itunes:author").text = ep.get("author", HOST_NAME)
            SubElement(item, "itunes:explicit").text = EXPLICIT
            episode_image = ep.get("episode_image", "")
            if episode_image and not episode_image.startswith("http"):
                if os.path.exists(episode_image) and os.path.getsize(episode_image) > 0:
                    episode_image = BASE_URL + episode_image.replace(os.sep, "/")
                else:
                    episode_image = ""
            if not episode_image:
                episode_image = ch_image or ""
            if episode_image:
                SubElement(item, "itunes:image", href=episode_image)

            # enclosure URL must be absolute for podcast apps
            audio_url = BASE_URL + file_path.replace(os.sep, "/")
            try:
                size_bytes = os.path.getsize(file_path)
            except FileNotFoundError:
                size_bytes = 0

            SubElement(
                item,
                "enclosure",
                url=audio_url,
                length=str(size_bytes),
                type="audio/mpeg",
            )
            SubElement(item, "guid").text = audio_url

    tree = ElementTree(rss)
    def _write(path: str) -> None:
        tree.write(path, encoding="utf-8", xml_declaration=True)
    write_atomic(RSS_FILE, _write)
    print(f"[INFO] RSS feed written to {RSS_FILE}")
    log_line(f"[INFO] RSS feed written to {RSS_FILE}")


# =======================
# Git auto-push
# =======================

def do_git_push():
    print("[INFO] Publishing rolling snapshot to gh-pages...")
    log_line("[INFO] Publishing rolling snapshot to gh-pages...")
    try:
        remote_url = subprocess.check_output(
            ["git", "remote", "get-url", PUBLISH_REMOTE],
            text=True,
        ).strip()

        user_name = subprocess.check_output(
            ["git", "config", "--get", "user.name"],
            text=True,
        ).strip()
        user_email = subprocess.check_output(
            ["git", "config", "--get", "user.email"],
            text=True,
        ).strip()

        with tempfile.TemporaryDirectory(prefix="podcast_publish_") as tmp:
            publish_root = os.path.abspath(tmp)
            publish_mp3 = os.path.join(publish_root, MP3_FOLDER)
            os.makedirs(publish_mp3, exist_ok=True)

            if os.path.exists(RSS_FILE):
                shutil.copy2(RSS_FILE, os.path.join(publish_root, RSS_FILE))
            if os.path.exists(CHANNEL_IMAGE) and os.path.getsize(CHANNEL_IMAGE) > 0:
                shutil.copy2(CHANNEL_IMAGE, os.path.join(publish_root, CHANNEL_IMAGE))

            copied_mp3 = 0
            if os.path.isdir(MP3_FOLDER):
                for name in sorted(os.listdir(MP3_FOLDER)):
                    if not name.lower().endswith(".mp3"):
                        continue
                    src = os.path.join(MP3_FOLDER, name)
                    if not os.path.isfile(src):
                        continue
                    shutil.copy2(src, os.path.join(publish_mp3, name))
                    copied_mp3 += 1

            with open(os.path.join(publish_root, ".nojekyll"), "w") as f:
                f.write("")
            with open(os.path.join(publish_root, "index.html"), "w") as f:
                f.write(
                    "<!doctype html><meta charset='utf-8'>"
                    "<title>Podcast Feed</title>"
                    "<p>Podcast feed: <a href='smear_campaign_feed.xml'>smear_campaign_feed.xml</a></p>"
                )

            subprocess.run(["git", "init", "-b", PUBLISH_BRANCH], cwd=publish_root, check=True)
            subprocess.run(["git", "config", "user.name", user_name], cwd=publish_root, check=True)
            subprocess.run(["git", "config", "user.email", user_email], cwd=publish_root, check=True)
            subprocess.run(["git", "add", "."], cwd=publish_root, check=True)

            status = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=publish_root)
            if status.returncode == 0:
                log_line("[INFO] Nothing to publish in rolling snapshot.")
                return

            subprocess.run(
                ["git", "commit", "-m", f"Auto update ({datetime.now().strftime('%Y-%m-%d %H:%M')})"],
                cwd=publish_root,
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            subprocess.run(["git", "remote", "add", PUBLISH_REMOTE, remote_url], cwd=publish_root, check=True)

            remote_ref = f"refs/remotes/{PUBLISH_REMOTE}/{PUBLISH_BRANCH}"
            fetch = subprocess.run(
                [
                    "git",
                    "fetch",
                    "--depth=1",
                    PUBLISH_REMOTE,
                    f"refs/heads/{PUBLISH_BRANCH}:{remote_ref}",
                ],
                cwd=publish_root,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            remote_sha = ""
            if fetch.returncode == 0:
                remote_sha = subprocess.check_output(
                    ["git", "rev-parse", remote_ref],
                    cwd=publish_root,
                    text=True,
                ).strip()
                same_tree = subprocess.run(["git", "diff", "--quiet", "HEAD", remote_ref, "--"], cwd=publish_root)
                if same_tree.returncode == 0:
                    print("[INFO] No content changes to publish.")
                    log_line("[INFO] No content changes to publish.")
                    return

            push_cmd = ["git", "push"]
            if remote_sha:
                push_cmd.append(f"--force-with-lease=refs/heads/{PUBLISH_BRANCH}:{remote_sha}")
            push_cmd.extend([PUBLISH_REMOTE, f"HEAD:{PUBLISH_BRANCH}"])
            subprocess.run(push_cmd, cwd=publish_root, check=True)
            log_line(f"[INFO] Published {copied_mp3} mp3 file(s) to {PUBLISH_BRANCH}.")
        print("[INFO] Publish complete.")
        log_line("[INFO] Publish complete.")
    except Exception as e:
        print(f"[ERROR] Publish failed: {e}")
        log_line(f"[ERROR] Publish failed: {e}")


# =======================
# Entry point
# =======================

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Record scheduled radio shows and publish podcast feed.")
    parser.add_argument(
        "--test-run",
        action="store_true",
        help="Create a short synthetic episode and publish it (no live stream dependency).",
    )
    args = parser.parse_args()
    main(test_run=args.test_run)
