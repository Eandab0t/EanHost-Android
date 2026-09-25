#!/usr/bin/env bash
set -u
DEV=SERIAL1

echo "=== TERMUX STATUS (uids/gids) ==="
adb -s $DEV shell "cat /proc/3731/status" | grep -E 'Uid:|Gid:|Groups:'

echo "=== GRANTED EXT STORAGE ==="
adb -s $DEV shell "dumpsys package com.termux" | grep -i -A1 'EXTERNAL_STORAGE'

echo "=== STORAGE SYMLINKS ==="
adb -s $DEV shell "ls -ld /data/data/com.termux/files/home/storage"
adb -s $DEV shell "ls -ld /data/data/com.termux/files/home/storage/shared"
adb -s $DEV shell "readlink /data/data/com.termux/files/home/storage/shared"

echo "=== EMULATED EanHost ==="
adb -s $DEV shell "ls -ld /storage/emulated/0/EanHost"
adb -s $DEV shell "ls -la /storage/emulated/0/EanHost"