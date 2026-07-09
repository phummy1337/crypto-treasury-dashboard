#!/bin/bash
# Daily refresh for the BTC Treasury Tracker. Invoked by cron (see `crontab -l`).
# Logs each run to refresh.log in this directory.
cd /Users/petehumiston/crypto-treasury-dashboard || exit 1
PY=/Library/Frameworks/Python.framework/Versions/3.14/bin/python3
echo "===== $(date '+%Y-%m-%d %H:%M:%S') =====" >> refresh.log
"$PY" refresh_data.py >> refresh.log 2>&1
echo "" >> refresh.log
