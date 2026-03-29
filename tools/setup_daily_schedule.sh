#!/bin/bash
# =============================================================================
# Set up daily automated claims processing via macOS launchd
# Runs Mon-Fri at 7:00 AM automatically
# =============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PLIST_NAME="com.lci.claims-automation"
PLIST_PATH="$HOME/Library/LaunchAgents/${PLIST_NAME}.plist"

echo ""
echo "Setting up daily claims automation schedule..."
echo "  Run time: Monday-Friday at 7:00 AM"
echo "  Script: $SCRIPT_DIR/run.sh"
echo ""

cat > "$PLIST_PATH" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${PLIST_NAME}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${SCRIPT_DIR}/.venv/bin/python3</string>
        <string>${SCRIPT_DIR}/orchestrator.py</string>
        <string>--action</string>
        <string>all</string>
    </array>
    <key>StartCalendarInterval</key>
    <array>
        <dict><key>Weekday</key><integer>1</integer><key>Hour</key><integer>7</integer><key>Minute</key><integer>0</integer></dict>
        <dict><key>Weekday</key><integer>2</integer><key>Hour</key><integer>7</integer><key>Minute</key><integer>0</integer></dict>
        <dict><key>Weekday</key><integer>3</integer><key>Hour</key><integer>7</integer><key>Minute</key><integer>0</integer></dict>
        <dict><key>Weekday</key><integer>4</integer><key>Hour</key><integer>7</integer><key>Minute</key><integer>0</integer></dict>
        <dict><key>Weekday</key><integer>5</integer><key>Hour</key><integer>7</integer><key>Minute</key><integer>0</integer></dict>
    </array>
    <key>WorkingDirectory</key>
    <string>${SCRIPT_DIR}</string>
    <key>StandardOutPath</key>
    <string>${SCRIPT_DIR}/logs/schedule_stdout.log</string>
    <key>StandardErrorPath</key>
    <string>${SCRIPT_DIR}/logs/schedule_stderr.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/opt/homebrew/bin</string>
        <key>HOME</key>
        <string>${HOME}</string>
    </dict>
</dict>
</plist>
EOF

# Load the schedule
launchctl unload "$PLIST_PATH" 2>/dev/null
launchctl load "$PLIST_PATH"

echo "Daily schedule installed!"
echo "  Plist: $PLIST_PATH"
echo ""
echo "Commands:"
echo "  Check status:  launchctl list | grep lci"
echo "  Stop schedule: launchctl unload $PLIST_PATH"
echo "  Start again:   launchctl load $PLIST_PATH"
echo ""
echo "NOTE: The Claim.MD session needs to be refreshed daily."
echo "      Run 'bash ~/claims.sh claimmd' each morning before 7 AM,"
echo "      or the automation will use the API (which doesn't need a session)."
echo ""
