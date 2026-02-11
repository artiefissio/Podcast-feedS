#!/bin/sh
set -e
PORT=17891
IP=$(ipconfig getifaddr en0 || true)
if [ -z "$IP" ]; then
  IP=$(ipconfig getifaddr en1 || true)
fi
if [ -z "$IP" ]; then
  echo "Could not detect Wi-Fi IP. Try: ipconfig getifaddr en0"
  exit 1
fi
echo "Feed URL: http://$IP:$PORT/smear_campaign_feed.xml"
