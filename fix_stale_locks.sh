#!/bin/bash
# Quick fix for stale dpkg locks
# Run: sudo bash fix_stale_locks.sh

echo "Checking for stale lock files..."

# Check if any apt/dpkg processes are running
if pgrep -x "apt-get|apt|dpkg|unattended-upgrades" > /dev/null; then
    echo "ERROR: Package manager processes are running. Please wait for them to finish."
    ps aux | grep -E 'apt|dpkg'
    exit 1
fi

# Remove stale lock files
LOCK_FILES=(
    "/var/lib/dpkg/lock-frontend"
    "/var/lib/dpkg/lock"
    "/var/cache/apt/archives/lock"
)

REMOVED=0
for lock_file in "${LOCK_FILES[@]}"; do
    if [ -f "$lock_file" ]; then
        # Check if file is actually in use
        if command -v lsof > /dev/null 2>&1; then
            if lsof "$lock_file" > /dev/null 2>&1; then
                echo "WARNING: $lock_file is in use, skipping..."
                continue
            fi
        fi
        
        echo "Removing stale lock: $lock_file"
        rm -f "$lock_file"
        REMOVED=$((REMOVED + 1))
    fi
done

if [ $REMOVED -gt 0 ]; then
    echo "✅ Removed $REMOVED stale lock file(s)"
    echo "Reconfiguring dpkg..."
    dpkg --configure -a 2>/dev/null || true
    echo "✅ Done! You can now run the installation script."
else
    echo "No stale locks found."
fi

