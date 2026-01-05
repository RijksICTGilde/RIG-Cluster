#!/bin/bash
# Traffic generator script - makes requests at random intervals for 5 minutes

set -e

if [ -z "$1" ]; then
    echo "Usage: $0 <url>"
    echo "Example: $0 https://example.com/api/health"
    exit 1
fi

URL="$1"
DURATION=300  # 5 minutes in seconds
START_TIME=$(date +%s)
END_TIME=$((START_TIME + DURATION))
REQUEST_COUNT=0

echo "Starting traffic generator"
echo "URL: $URL"
echo "Duration: $DURATION seconds"
echo "Interval: 1-10 seconds (random)"
echo "---"

while [ $(date +%s) -lt $END_TIME ]; do
    REQUEST_COUNT=$((REQUEST_COUNT + 1))
    TIMESTAMP=$(date '+%Y-%m-%d %H:%M:%S')

    # Make request and capture status code
    HTTP_CODE=$(curl -s -o /dev/null -w '%{http_code}' "$URL" 2>/dev/null || echo "ERR")

    echo "[$TIMESTAMP] Request #$REQUEST_COUNT - Status: $HTTP_CODE"

    # Random sleep between 1 and 10 seconds
    SLEEP_TIME=$((RANDOM % 10 + 1))
    sleep $SLEEP_TIME
done

echo "---"
echo "Completed $REQUEST_COUNT requests in $DURATION seconds"
