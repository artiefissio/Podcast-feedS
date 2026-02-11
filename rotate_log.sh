#!/bin/bash
# Rotate cron.log if > 200MB
LOG="$HOME/AIprojects/Podcast-feedS/cron.log"
MAX_SIZE=$((200 * 1024 * 1024))  # 200MB in bytes

if [ -f "$LOG" ]; then
    SIZE=$(stat -f%z "$LOG" 2>/dev/null || stat -c%s "$LOG" 2>/dev/null)
    if [ "$SIZE" -gt "$MAX_SIZE" ]; then
        # Keep last 50MB, discard rest
        tail -c 52428800 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
        echo "[$(date)] Log rotated - was ${SIZE} bytes" >> "$LOG"
    fi
fi
