#!/bin/bash
###############################################################################
# DigitalOcean Cloud-Init Script
# 
# This script automatically runs when a droplet is first created.
# It downloads the setup script from GitHub and executes it.
#
# Usage: This is passed as user_data when creating a droplet
###############################################################################

# Update system
# PRE-EMPTIVELY KILL AUTOMATIC UPGRADES TO AVOID LOCKS
echo "Stopping unattended-upgrades..."
systemctl stop unattended-upgrades || true
systemctl disable unattended-upgrades || true
echo "Killing any existing apt processes..."
killall apt apt-get || true
rm /var/lib/dpkg/lock-frontend || true
rm /var/lib/dpkg/lock || true
dpkg --configure -a || true

echo "Updating apt..."
apt-get update -y

# Install git and curl
apt-get install -y git curl

# Clone your repository (replace with your actual repo)
GITHUB_REPO="https://github.com/Jetalp54/gbot-web-app-original-working.git"
CLONE_DIR="/tmp/gbot-setup"

echo "Cloning repository from GitHub..."
for i in {1..5}; do
    git clone "$GITHUB_REPO" "$CLONE_DIR" && break
    echo "Git clone failed (attempt $i/5). Retrying in 10s..."
    sleep 10
done

if [ ! -d "$CLONE_DIR" ]; then
    echo "ERROR: Failed to clone repository after 5 attempts."
    exit 1
fi

# Run the setup script
if [ -f "$CLONE_DIR/repo_digitalocean_files/setup_droplet.sh" ]; then
    echo "Running setup script..."
    bash "$CLONE_DIR/repo_digitalocean_files/setup_droplet.sh"
    
    # Copy automation script to /opt/automation
    if [ -f "$CLONE_DIR/repo_digitalocean_files/do_automation.py" ]; then
        cp "$CLONE_DIR/repo_digitalocean_files/do_automation.py" /opt/automation/
        chmod +x /opt/automation/do_automation.py
    fi
    
    # Create success marker
    touch /root/.droplet_setup_complete
    echo "Setup complete at $(date)" > /root/.droplet_setup_complete
else
    echo "ERROR: Setup script not found in repository!"
    exit 1
fi

# Cleanup
rm -rf "$CLONE_DIR"

echo "Cloud-init setup completed successfully!"
