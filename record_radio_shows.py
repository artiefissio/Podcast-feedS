import os
import subprocess
import math
import json
from datetime import datetime
from xml.etree.ElementTree import Element, SubElement, ElementTree

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
CATEGORY = "Music"
LANG = "en-US"
EXPLICIT = "no"
BASE_URL = "https://artiefissio.github.io/Podcast-feedS/"  # GitHub Pages base

# Audio / ffmpeg
STREAM_URL = "https://ktal.broadcasttool.stream/stream"
AUDIO_BITRATE = "192k"  # 192 kbps as requested
MAX_SIZE_BYTES = 99 * 1024 * 1024  # 99 MB split threshold

FFMPEG_BIN = "/opt/homebrew/bin/ffmpeg"
FFPROBE_BIN = "/opt/homebrew/bin/ffprobe"

# Git auto-push
GITHUB_PUSH = True

# iMessage (REPLACE with your iMessage-enabled number)
IMESSAGE_NUMBER = "5058143699"  # Arturo's iMessage-enabled number

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
        "spinitron_archive": None,
    },
]

# =======================
# Helpers
# =======================

def send_imessage(phone_number: str, message: str) -> None:
    """
    Sends an iMessage via AppleScript.
    Your Mac must be logged into Messages with this number / Apple ID.
    """
    if not phone_number:
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


def ensure_dirs() -> None:
    os.makedirs(MP3_FOLDER, exist_ok=True)


def load_downloaded() -> dict:
    if os.path.exists(TRACK_FILE):
        with open(TRACK_FILE, "r") as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return {}
    return {}


def save_downloaded(downloaded: dict) -> None:
    with open(TRACK_FILE, "w") as f:
        json.dump(downloaded, f, indent=2)


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
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if os.path.exists(part_filename):
            part_files.append(part_filename)

    # Remove original if splitting succeeded
    if part_files:
        os.remove(file_path)
        return part_files
    else:
        return [file_path]


def rfc2822(dt: datetime) -> str:
    # For podcast pubDate (RFC-2822-ish)
    return dt.strftime("%a, %d %b %Y %H:%M:%S %z") or dt.strftime("%a, %d %b %Y %H:%M:%S +0000")


# --- Spinitron helpers (best-effort) ---

def scrape_smear_campaign_latest():
    """
    Best-effort scrape of the latest Smear Campaign playlist:
    - Gets show page
    - Finds first playlist link (/KTAL/pl/)
    - Pulls tracklist + image
    """
    archive_url = "https://spinitron.com/KTAL/show/277361/The-Smear-Campaign?layout=1"
    try:
        resp = requests.get(archive_url, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        print(f"[Spinitron] Error fetching archive: {e}")
        return {}

    soup = BeautifulSoup(resp.text, "html.parser")

    # Find first playlist link
    pl_link = soup.select_one("a[href*='/KTAL/pl/']")
    if not pl_link:
        return {}

    playlist_url = "https://spinitron.com" + pl_link["href"].split("?")[0]

    try:
        pl_resp = requests.get(playlist_url, timeout=15)
        pl_resp.raise_for_status()
    except Exception as e:
        print(f"[Spinitron] Error fetching playlist: {e}")
        return {}

    pl_soup = BeautifulSoup(pl_resp.text, "html.parser")

    # Tracklist
    tracks = pl_soup.select("div.playlist-tracks li")
    track_html = ""
    if tracks:
        items = "".join(f"<li>{t.get_text(strip=True)}</li>" for t in tracks)
        track_html = f"<ul>{items}</ul>"

    # Image
    img_elem = pl_soup.select_one("div.playlist-art img")
    image = img_elem["src"] if img_elem and img_elem.get("src") else CHANNEL_IMAGE

    # DJ / host – best-effort, may need tweaking
    dj = ""
    possible = pl_soup.select_one(".show-dj, .show-host, .host, .field-name-host")
    if possible:
        dj = possible.get_text(strip=True)

    return {
        "playlist_url": playlist_url,
        "tracklist_html": track_html,
        "image": image,
        "dj": dj or HOST_NAME,
    }


def build_episode_description(show_name: str, start_dt: datetime, meta: dict | None) -> str:
    """
    Build nice HTML description for podcast apps.
    """
    date_str = start_dt.strftime("%A %B %d, %Y • %I:%M %p")
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
# Automated cleanup (keep only 3 weeks)
# =======================

def cleanup_old_episodes(retention_days: int = 21):
    """
    Deletes MP3 files and metadata older than X days (default 21 days = 3 weeks).
    """
    now = datetime.now()
    threshold = now.timestamp() - (retention_days * 86400)

    removed = []

    # Clean MP3 files
    for fname in os.listdir(MP3_FOLDER):
        if not fname.endswith(".mp3"):
            continue
        path = os.path.join(MP3_FOLDER, fname)
        try:
            if os.path.getmtime(path) < threshold:
                os.remove(path)
                removed.append(path)
        except Exception:
            continue
    return removed


def cleanup_downloaded_metadata(downloaded: dict, retention_days: int = 21):
    """
    Removes metadata entries older than X days.
    """
    now = datetime.now()
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


# =======================
# Main recording logic
# =======================

def main():
    ensure_dirs()
    downloaded = load_downloaded()

    # Run cleanup first
    removed_files = cleanup_old_episodes()
    if removed_files:
        print(f"[CLEANUP] Removed {len(removed_files)} old MP3 files.")

    downloaded = cleanup_downloaded_metadata(downloaded)
    save_downloaded(downloaded)

    now = datetime.now()
    today = now.weekday()  # Monday=0 ... Sunday=6
    current_hour = now.hour

    print(f"[INFO] Now: {now.isoformat()} (weekday={today}, hour={current_hour})")

    # Determine which show (if any) is scheduled *right now*
    active_show = None
    for show in SHOWS:
        if show["day"] == today and current_hour in show["hours"]:
            active_show = show
            break

    if not active_show:
        print("Nothing scheduled right now.")
        return

    show = active_show
    show_name = show["name"]
    block_index = show["hours"].index(current_hour) + 1

    # We align the start time to the top of the hour
    start_dt = now.replace(minute=0, second=0, microsecond=0)
    timestamp_str = start_dt.strftime("%Y-%m-%d_%H%M")
    safe_title = show_name.replace(" ", "_").replace("/", "_")
    mp3_file = os.path.join(MP3_FOLDER, f"{timestamp_str}_{safe_title}.mp3")

    print(f"[INFO] Recording show: {show_name} (block {block_index})")
    print(f"[INFO] Output file: {mp3_file}")

    # For The Smear Campaign, try to pull fresh metadata
    smear_meta = None
    if "Smear Campaign" in show_name:
        smear_meta = scrape_smear_campaign_latest()

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
    result = subprocess.run(cmd)

    if result.returncode != 0 or not os.path.exists(mp3_file):
        print(f"[ERROR] Recording failed for {show_name}")
        send_imessage(IMESSAGE_NUMBER, f"[ERROR] Failed recording: {show_name} at {timestamp_str}")
        return

    print("[INFO] Recording complete, checking size / splitting...")
    mp3_files = split_mp3(mp3_file)

    # Build metadata & store in downloaded list
    ep_key = f"{timestamp_str}_{safe_title}"
    description = build_episode_description(show_name, start_dt, smear_meta if smear_meta else None)
    episode_image = smear_meta["image"] if smear_meta and smear_meta.get("image") else CHANNEL_IMAGE
    episode_author = smear_meta["dj"] if smear_meta and smear_meta.get("dj") else HOST_NAME

    downloaded[ep_key] = {
        "mp3_files": mp3_files,
        "title": f"{show_name} – {start_dt.strftime('%Y-%m-%d %H:%M')}",
        "pubDate_iso": start_dt.isoformat(),
        "description_html": description,
        "episode_image": episode_image,
        "author": episode_author,
    }

    save_downloaded(downloaded)
    print("[INFO] Metadata saved to downloaded_episodes.json")

    # Rebuild RSS feed
    build_rss(downloaded)

    # iMessage notification
    send_imessage(IMESSAGE_NUMBER, f"[OK] Recorded {show_name} at {timestamp_str} ({len(mp3_files)} file(s))")

    # Optional: Git auto-push
    if GITHUB_PUSH:
        do_git_push()


# =======================
# RSS builder
# =======================

def build_rss(downloaded: dict) -> None:
    print("[INFO] Rebuilding RSS feed...")

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
    SubElement(channel, "itunes:image", href=CHANNEL_IMAGE)
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

        for idx, file_path in enumerate(ep["mp3_files"]):
            item = SubElement(channel, "item")
            title_text = ep["title"]
            if len(ep["mp3_files"]) > 1:
                title_text += f" – Part {idx+1}"

            SubElement(item, "title").text = title_text
            SubElement(item, "description").text = ep.get("description_html", "")
            SubElement(item, "pubDate").text = rfc2822(pub_dt)
            SubElement(item, "itunes:author").text = ep.get("author", HOST_NAME)
            SubElement(item, "itunes:explicit").text = EXPLICIT
            SubElement(item, "itunes:image", href=ep.get("episode_image", CHANNEL_IMAGE))

            # enclosure URL must be absolute for podcast apps
            audio_url = BASE_URL + file_path
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
    tree.write(RSS_FILE, encoding="utf-8", xml_declaration=True)
    print(f"[INFO] RSS feed written to {RSS_FILE}")


# =======================
# Git auto-push
# =======================

def do_git_push():
    print("[INFO] Running git add/commit/push...")
    try:
        subprocess.run(["git", "add", "."], check=False)
        subprocess.run(
            ["git", "commit", "-m", "Auto update"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        subprocess.run(["git", "push", "origin", "main"], check=False)
        print("[INFO] Git push complete.")
    except Exception as e:
        print(f"[ERROR] Git push failed: {e}")


# =======================
# Entry point
# =======================

if __name__ == "__main__":
    main()
