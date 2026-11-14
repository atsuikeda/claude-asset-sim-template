#!/bin/sh
# 1日1回の自動運用(Mac / Linux)。cron などから呼ぶ。手で実行しても動く。
cd "$(dirname "$0")" || exit 1
mkdir -p logs
log="logs/$(date +%Y-%m-%d).log"
prompt="CLAUDE.md の「毎回の手順」に従って、今日の運用を1回分実行してください。確認の質問はせず、最後まで自分で進めてください。"

echo "=== $(date '+%Y-%m-%d %H:%M') 開始 ===" >> "$log"
claude -p "$prompt" --max-turns 60 >> "$log" 2>&1
echo "=== $(date '+%Y-%m-%d %H:%M') 終了 (exit $?) ===" >> "$log"
