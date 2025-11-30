import os
import subprocess
import math
import json
from datetime import datetime, timedelta
from xml.etree.ElementTree import Element, SubElement, ElementTree
import requests
from bs4 import BeautifulSoup

# --- Config ---
MP3_FOLDER = "episodes_mp3"
RSS_FILE = "smear_campaign_feed.xml"
TRACK_FILE = "downloaded_episodes.json"
HOST_NAME = "DJ Tone Deaf"
CHANNEL_IMAGE = "channel_image.jpg"
CATEGORY = "Music"
LANG = "en-US"
EXPLICIT = "no"
MAX_SIZE_BYTES = 99 * 1024 * 1024  # 99MB max
GITHUB_PUSH = True  # Push automatically to GitHub
BASE_URL = "https://artiefissio.github.io/Podcast-feedS/"

STREAM_URL = "https://ktal.broadcasttool.stream/stream"

os.makedirs(MP3_FOLDER, exist_ok=True)

# Load previous episodes
if os.path.exists(TRACK_FILE):
    with open(TRACK_FILE, "r") as f:
        downloaded = json.load(f)
else:
    downloaded = {}

# Helper: split large MP3 into parts
def split_mp3(file_path, max_size=MAX_SIZE_BYTES):
    if not os.path.exists(file_path):
        return []
    size_bytes = os.path.getsize(file_path)
    if size_bytes <= max_size:
        return [file_path]
    duration_sec = float(subprocess.getoutput(
        f"ffprobe -v error -show_entries format=duration -of default=noprint_wrappers=1:nokey=1 '{file_path}'"
    ))
    parts = math.ceil(size_bytes / max_size)
    segment_time = math.ceil(duration_sec / parts)
    part_files = []
    for i in range(parts):
        part_filename = file_path.replace(".mp3", f"_part{i+1:03d}.mp3")
        subprocess.run([
            "ffmpeg", "-i", file_path,
            "-ss", str(i*segment_time),
            "-t", str(segment_time),
            "-c", "copy", "-y", part_filename
        ])
        part_files.append(part_filename)
    os.remove(file_path)
    return part_files

# --- Scrape Spinitron metadata ---
def scrape_spinitron():
    url = "https://spinitron.com/KTAL/calendar?layout=1"
    try:
        resp = requests.get(url)
    except:
        return {}

    soup = BeautifulSoup(resp.text, "html.parser")
    out = {}

    for a in soup.select("a[href*='/KTAL/pl/']"):
        ep_url = "https://spinitron.com" + a['href'].split("?")[0]
        ep_title = a.text.strip()

        try:
            ep_resp = requests.get(ep_url)
            ep_soup = BeautifulSoup(ep_resp.text, "html.parser")

            # Tracklist
            tracks = ep_soup.select("div.playlist-tracks li")
            tracklist = "<ul>" + "".join(f"<li>{t.get_text(strip=True)}</li>" for t in tracks) + "</ul>" if tracks else ""

            img = ep_soup.select_one("div.playlist-art img")
            image = img["src"] if img else CHANNEL_IMAGE

            out[ep_title] = {
                "url": ep_url,
                "tracklist_html": tracklist,
                "image": image
            }
        except:
            continue

    return out

ktal_episodes = scrape_spinitron()

# --- Schedule fixed ---
shows = [
    {"name": "Wolfman Max – Wide World of Funk", "day": 5, "hours": [17, 18]},
    {"name": "The Smear Campaign", "day": 5, "hours": [19]},
    {"name": "Johnny Catalog – Catalog", "day": 5, "hours": [20]},
    {"name": "Brain Salad", "day": 5, "hours": [21]},
    {"name": "Lost Highway", "day": 1, "hours": [20]},
    {"name": "Soul Salad", "day": 0, "hours": [16]}
]

today = datetime.today().weekday()
now = datetime.now()

# --- Record shows ---
for show in shows:
    if today != show["day"]:
        continue

    for hr in show["hours"]:
        timestamp_str = now.strftime("%Y-%m-%d_") + f"{hr:02}00"
        safe_title = show["name"].replace(" ", "_").replace("/", "_")
        mp3_file = os.path.join(MP3_FOLDER, f"{timestamp_str}_{safe_title}.mp3")

        print(f"Recording {show['name']} to {mp3_file}...")

        subprocess.run([
            "ffmpeg", "-i", STREAM_URL,
            "-c:a", "libmp3lame",
            "-b:a", "128k",
            "-t", "01:00:00",
            "-y",
            mp3_file
        ])

        if not os.path.exists(mp3_file):
            print(f"Recording failed: {mp3_file}")
            continue

        mp3_files = split_mp3(mp3_file)
        ep_meta = ktal_episodes.get(show["name"], {})

        downloaded[timestamp_str] = {
            "mp3_files": mp3_files,
            "title": show["name"],
            "pubDate": now.isoformat(),
            "description": f"<p><strong>{show['name']}</strong></p>"
                           f"<p><a href='{ep_meta.get('url','')}'>Show page</a></p>"
                           f"{ep_meta.get('tracklist_html','')}",
            "episode_image": ep_meta.get("image", CHANNEL_IMAGE)
        }

# --- Save JSON ---
with open(TRACK_FILE, "w") as f:
    json.dump(downloaded, f, indent=2)

# --- Build RSS feed ---
rss = Element("rss", version="2.0", attrib={"xmlns:itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd"})
channel = SubElement(rss, "channel")
SubElement(channel, "title").text = "Automated Radio Shows – DJ Tone Deaf"
SubElement(channel, "link").text = BASE_URL
SubElement(channel, "description").text = "Automated recordings with metadata"
SubElement(channel, "language").text = LANG
SubElement(channel, "itunes:author").text = HOST_NAME
SubElement(channel, "itunes:explicit").text = EXPLICIT
SubElement(channel, "itunes:image", href=CHANNEL_IMAGE)
itunes_category = SubElement(channel, "itunes:category")
itunes_category.set("text", CATEGORY)

for key, ep in downloaded.items():
    for idx, file_path in enumerate(ep["mp3_files"]):
        item = SubElement(channel, "item")
        SubElement(item, "title").text = ep["title"] + (f" – Part {idx+1}" if len(ep["mp3_files"])>1 else "")
        SubElement(item, "description").text = ep["description"]
        SubElement(item, "pubDate").text = ep["pubDate"]
        SubElement(item, "itunes:author").text = HOST_NAME
        SubElement(item, "itunes:explicit").text = EXPLICIT
        SubElement(item, "itunes:image", href=ep["episode_image"])
        SubElement(item, "itunes:duration").text = "3600"
        SubElement(item, "enclosure", url=f"{BASE_URL}{file_path}", length=str(os.path.getsize(file_path)), type="audio/mpeg")
        SubElement(item, "guid").text = f"{BASE_URL}{file_path}"

tree = ElementTree(rss)
tree.write(RSS_FILE, encoding="utf-8", xml_declaration=True)

# --- Optional GitHub push ---
if GITHUB_PUSH:
    subprocess.run(["git", "add", "."])
    subprocess.run(["git", "commit", "-m", "Auto update"], stderr=subprocess.DEVNULL)
    subprocess.run(["git", "push", "origin", "main"])
