#!/usr/bin/env python3.12
"""
Validate podcast RSS feed structure and enclosure URLs.
"""

from __future__ import annotations

import argparse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path


def fail(msg: str) -> int:
    print(f"FAIL: {msg}")
    return 1


def ok(msg: str) -> None:
    print(f"OK: {msg}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate smear_campaign_feed.xml")
    parser.add_argument("--feed", default="smear_campaign_feed.xml", help="Path to RSS XML file")
    parser.add_argument("--check-urls", action="store_true", help="Run HEAD checks against enclosure URLs")
    parser.add_argument("--max-url-checks", type=int, default=5, help="Max number of enclosure URLs to check")
    args = parser.parse_args()

    feed_path = Path(args.feed)
    if not feed_path.exists():
        return fail(f"feed file not found: {feed_path}")

    try:
        tree = ET.parse(feed_path)
    except Exception as exc:
        return fail(f"xml parse failed: {exc}")

    root = tree.getroot()
    if root.tag != "rss":
        return fail(f"root tag must be rss, got: {root.tag}")

    channel = root.find("channel")
    if channel is None:
        return fail("missing channel element")

    for tag in ("title", "link", "description", "language"):
        node = channel.find(tag)
        if node is None or not (node.text or "").strip():
            return fail(f"missing or empty channel/{tag}")

    items = channel.findall("item")
    ok(f"feed parsed with {len(items)} item(s)")
    if not items:
        return 0

    enclosures: list[str] = []
    for idx, item in enumerate(items, start=1):
        title = item.findtext("title", "").strip()
        guid = item.findtext("guid", "").strip()
        pub_date = item.findtext("pubDate", "").strip()
        enclosure = item.find("enclosure")
        if not title:
            return fail(f"item {idx} missing title")
        if not guid:
            return fail(f"item {idx} missing guid")
        if not pub_date:
            return fail(f"item {idx} missing pubDate")
        if enclosure is None:
            return fail(f"item {idx} missing enclosure")
        url = (enclosure.attrib.get("url") or "").strip()
        media_type = (enclosure.attrib.get("type") or "").strip()
        if not url.startswith("http"):
            return fail(f"item {idx} enclosure URL is not absolute: {url}")
        if media_type != "audio/mpeg":
            return fail(f"item {idx} enclosure type should be audio/mpeg, got: {media_type}")
        enclosures.append(url)

    ok(f"validated {len(enclosures)} enclosure(s)")

    if args.check_urls:
        to_check = enclosures[: max(0, args.max_url_checks)]
        for url in to_check:
            req = urllib.request.Request(url, method="HEAD")
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    status = getattr(resp, "status", 200)
                    if status >= 400:
                        return fail(f"enclosure HEAD failed ({status}): {url}")
                    print(f"URL OK: {status} {url}")
            except Exception as exc:
                return fail(f"enclosure HEAD exception for {url}: {exc}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

