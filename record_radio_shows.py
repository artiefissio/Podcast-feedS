import os
import subprocess
import math
import json
import sys
from datetime import datetime
from xml.etree.ElementTree import Element, SubElement, ElementTree
import requests
from bs4 import BeautifulSoup

# --- Paths ---
BASE = "/Users/test/Podcast-feedS"
MP3_FOLDER = f"{BASE}/episodes_mp3"
LOG_FOLDER = f"{BASE}/logs"
RSS_FILE = f"{BASE}/smear_campaign_feed.xml"
TRACK_FILE = f"{BASE}/downloaded_episodes.json"

# --- Automatic folder creation ---
os.makedirs(MP3_FOLDER, exist_ok=True)
os.makedirs(LOG_FOLDER, exist_ok=True)

# --- Safety Lock ---
LOCK_FILE = f"{BASE}/recording.lock"
if os.path.exists(LOCK_FILE):
    print("Another recording is already running. Exiting.")
    exit(0)

with open(LOCK_FILE, "w") as f:
    f.write("locked\n")

# --- Log rotation (7 days) ---
def rotate_logs():
    now = datetime.now()
    for log in os.listdir(LOG_FOLDER):
        path = os.path.join(LOG_FOLDER, log)
        if os.path.isfile(path):
            mtime = datetime.fromtimestamp(os.path.getmtime(path))
            if (now - mtime).days > 7:
                os.remove(path)

rotate_logs()

# --- Load previous episodes ---
if os.path.exists(TRACK_FILE):
    with open(TRACK_FILE, "r") as f:
        downloaded = json.load(f)
else:
    downloaded = {}

MAX_SIZE_BYTES = 99 * 1024 * 1024

# --- Split large MP3 ---
def split_mp3(file_path, max_size=MAX_SIZE_BYTES):
    size_bytes = os.path.getsize(file_path)
    if size_bytes <= max_size:
        return [file_path]

    duration_sec = float(subprocess.getoutput(
        f"ffprobe -v error -show_entries format=duration "
        f"-of default=noprint_wrappers=1:nokey=1 '{file_path}'"
    ))

    parts = math.ceil(size_bytes / max_size)
    segment = math.ceil(duration_sec / parts)
    results = []

    for i in range(parts):
        out = file_path.replace(".mp3", f"_part{i+1:03d}.mp3")
        subprocess.run([
            "ffmpeg", "-i", file_path,
            "-ss", str(i * segment),
            "-t", str(segment),
            "-c", "copy", "-y", out
        ])
        results.append(out)

    os.remove(file_path)
    return results

# --- Spinitron metadata fetch ---
SPINITRON_BASE = "https://spinitron.com"
CAL_URL = "https://spinitron.com/KTAL/calendar?layout=1"

def get_metadata(title):
    try:
        resp = requests.get(CAL_URL)
        soup = BeautifulSoup(resp.text, "html.parser")

        link = soup.find("a", string=lambda t: t and title.lower() in t.lower())
        if not link:
            return {"tracklist": "", "image": "", "url": ""}

        show_url = SPINITRON_BASE + link["href"]
        show_resp = requests.get(show_url)
        show_soup = BeautifulSoup(show_resp.text, "html.parser")

        tracks = [
            li.get_text(strip=True)
            for li in show_soup.select("div.playlist-tracks li")
        ]
        tracklist_html = "<ul>" + "".join(f"<li>{t}</li>" for t in tracks) + "</ul>"

        img_elem = show_soup.select_one("div.playlist-art img")
        image = img_elem["src"] if img_elem else ""

        return {
            "tracklist": tracklist_html,
            "image": image,
            "url": show_url
        }
    except:
        return {"tracklist": "", "image": "", "url": ""}

# --- Schedule (fixed) ---

SATURDAY_SHOWS = [
    ("Wolfman Max Wide World of Funk", 17),
    ("Wolfman Max Wide World of Funk", 18),
    ("Smear Campaign", 19),
    ("Johnny Catalog Catalog", 20),
    ("Brain Salad", 21),
]

STREAM_URL = "https://ktal.broadcasttool.stream/stream"

# --- Determine show ---
now = datetime.now()
weekday = now.weekday()   # Monday=0 ... Sunday=6
hour = now.hour

show_to_record = None

# Sunday: Soul Salad @ 16:00
if weekday == 6 and hour == 16:
    show_to_record = ("Soul Salad", 16)

# Monday: Lost Highway @ 20:00
if weekday == 1 and hour == 20:
    show_to_record = ("Lost Highway", 20)

# Saturday: multiple shows
if weekday == 5:
    for title, show_hour in SATURDAY_SHOWS:
        if hour == show_hour:
            show_to_record = (title, show_hour)

# Manual force mode
if len(sys.argv) > 1 and sys.argv[1] == "force":
    show_to_record = ("TEST RECORDING", hour)

if not show_to_record:
    print("Nothing scheduled right now.")
    os.remove(LOCK_FILE)
    exit(0)

title, show_hour = show_to_record
timestamp = now.strftime("%Y-%m-%d_%H%M")
outfile = f"{MP3_FOLDER}/{timestamp}_{title.replace(' ', '_')}.mp3"
logfile = f"{LOG_FOLDER}/{title.replace(' ', '_')}.log"

print(f"Recording: {title} → {outfile}")

# --- Metadata ---
meta = get_metadata(title)

# --- Recording ---
cmd = [
    "ffmpeg",
    "-i", STREAM_URL,
    "-c:a", "libmp3lame",
    "-b:a", "128k",
    "-t", "3600",
    "-y", outfile
]

result = subprocess.run(cmd, capture_output=True, text=True)

with open(logfile, "a") as f:
    f.write(f"\n[{timestamp}] Recording started\n")
    f.write(result.stdout + "\n" + result.stderr + "\n")

if result.returncode != 0 or not os.path.exists(outfile):
    print("Recording failed.")
    os.remove(LOCK_FILE)
    exit(1)

# --- Split if needed ---
mp3_files = split_mp3(outfile)

downloaded[timestamp] = {
    "title": title,
    "mp3_files": mp3_files,
    "timestamp": timestamp,
    "tracklist": meta["tracklist"],
    "image": meta["image"],
    "spinitron_url": meta["url"]
}

with open(TRACK_FILE, "w") as f:
    json.dump(downloaded, f, indent=2)

# --- Remove Lock ---
os.remove(LOCK_FILE)

print("Recording complete.")