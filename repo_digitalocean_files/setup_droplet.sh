#!/bin/bash
###############################################################################
# DigitalOcean Droplet Setup Script
# 
# This script prepares an Ubuntu 22.04 droplet with Chrome, ChromeDriver,
# and Python environment for Google Workspace automation.
#
# Installation Steps:
# 1. Updates system packages
# 2. Installs Chrome and ChromeDriver
# 3. Installs Python 3.10+ and required packages
# 4. Sets up the automation script (do_automation.py)
# 5. Configures SFTP access for result collection
#
# Usage:
#   Run as root: sudo bash setup_droplet.sh
###############################################################################

set -e  # Exit on any error

echo "===== DigitalOcean Droplet Setup for Google Workspace Automation ====="
echo "Starting at: $(date)"

# Helper function for silent install
silent_apt() {
    local max_retries=3
    local attempt=1
    local status=1
    
    while [ $attempt -le $max_retries ]; do
        DEBIAN_FRONTEND=noninteractive apt-get --fix-missing "$@" > /dev/null 2>&1
        status=$?
        if [ $status -eq 0 ]; then
            return 0
        fi
        echo "Apt-get failed (attempt $attempt/$max_retries). Retrying..."
        sleep 2
        attempt=$((attempt + 1))
    done

    echo "Error running apt-get $@ after $max_retries attempts. Running with output:"
    DEBIAN_FRONTEND=noninteractive apt-get --fix-missing "$@"
    return $?
}

# Update system
# 1. Update system
echo ""
echo "[1/6] Updating system packages..."

# Fix DigitalOcean mirrors (switching to archive.ubuntu.com for stability)
sed -i 's|mirrors.digitalocean.com|archive.ubuntu.com|g' /etc/apt/sources.list
rm -rf /var/lib/apt/lists/*

silent_apt update
silent_apt upgrade -y

# 2. Install basic dependencies
echo ""
echo "[2/6] Installing basic dependencies..."
silent_apt install -y \
    wget \
    curl \
    unzip \
    git \
    python3-pip \
    python3-venv \
    xvfb \
    libxi6 \
    libgconf-2-4 \
    default-jdk \
    libxss1 \
    libappindicator1 \
    libindicator7 \
    fonts-liberation \
    libnss3 \
    libgbm1 \
    libxshmfence1

# 3. Install Chrome
echo ""
echo "[3/6] Installing Chrome..."
wget -q https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
silent_apt install -y ./google-chrome-stable_current_amd64.deb
rm google-chrome-stable_current_amd64.deb

# Verify Chrome installation
CHROME_VERSION=$(google-chrome --version | awk '{print $3}')
echo "Chrome installed: version $CHROME_VERSION"

# 4. Install ChromeDriver
echo ""
echo "[4/6] Installing ChromeDriver..."
CHROME_MAJOR_VERSION=$(echo "$CHROME_VERSION" | cut -d. -f1)
echo "Detected Chrome version: $CHROME_VERSION"

# Fetch latest compatible driver
if [ "$CHROME_MAJOR_VERSION" -ge "115" ]; then
    echo "Chrome version is 115+, using cf-for-testing..."
    # Fetch the correct ChromeDriver version for 115+
    # Trying cf-chrome-driver automation
    wget -q -O "chromedriver-linux64.zip" "https://edgedl.me.gvt1.com/edgedl/chrome/chrome-for-testing/$CHROME_VERSION/linux64/chromedriver-linux64.zip"

    if [ ! -f "chromedriver-linux64.zip" ]; then
        echo "Direct download failed, trying generic latest..."
        LATEST_CHROMEDRIVER_VERSION=$(curl -s "https://googlechromelabs.github.io/chrome-for-testing/LATEST_RELEASE_STABLE")
        wget -q "https://storage.googleapis.com/chrome-for-testing-public/$LATEST_CHROMEDRIVER_VERSION/linux64/chromedriver-linux64.zip"
    fi
    # Unzip logic handled below
else
    echo "Chrome version is < 115, using legacy storage..."
    # Legacy
    CHROMEDRIVER_VERSION=$(curl -sS chromedriver.storage.googleapis.com/LATEST_RELEASE)
    wget -q "https://chromedriver.storage.googleapis.com/$CHROMEDRIVER_VERSION/chromedriver_linux64.zip"
    # Unzip logic handled below
fi

if [ -f "chromedriver-linux64.zip" ]; then
    unzip -q -o chromedriver-linux64.zip
    mv chromedriver-linux64/chromedriver /usr/local/bin/
    rm -rf chromedriver-linux64.zip chromedriver-linux64
elif [ -f "chromedriver_linux64.zip" ]; then
    unzip -q -o chromedriver_linux64.zip
    mv chromedriver /usr/local/bin/
    rm chromedriver_linux64.zip
fi
chmod +x /usr/local/bin/chromedriver

# Verify ChromeDriver installation
CHROMEDRIVER_VER=$(chromedriver --version | awk '{print $2}')
echo "ChromeDriver installed: version $CHROMEDRIVER_VER"

# 5. Install Python packages
echo ""
echo "[5/6] Installing Python packages..."

# Ensure pip is installed
if ! command -v pip3 &> /dev/null; then
    echo "pip3 not found, installing..."
    apt-get update
    apt-get install -y python3-pip
fi

pip3 install --no-cache-dir \
    selenium==4.15.2 \
    selenium-stealth==1.0.6 \
    selenium-wire==5.1.0 \
    undetected-chromedriver>=3.5.5 \
    paramiko \
    pyotp \
    requests

# Create automation directory
echo ""
echo "[6/6] Setting up automation environment..."
mkdir -p /opt/automation
cd /opt/automation

# Create placeholder for automation script (will be uploaded separately)
cat > /opt/automation/README.txt << 'EOF'
This directory contains the Google Workspace automation script.

The main script (do_automation.py) will be uploaded separately.
SSH keys and SFTP configuration will be set up for result collection.
EOF

# Set permissions
chmod 755 /opt/automation
chmod 644 /opt/automation/README.txt

# Disable unattended upgrades to prevent interference
echo ""
echo "Disabling unattended upgrades..."
systemctl stop unattended-upgrades || true
systemctl disable unattended-upgrades || true

# Clean up
echo ""
echo "Cleaning up..."
apt-get autoremove -y
apt-get autoclean -y

# Create completion marker
touch /opt/automation/setup_complete

echo ""
echo "===== Setup Complete! ====="
echo "Finished at: $(date)"
echo ""
echo "Chrome: $CHROME_VERSION"
echo "ChromeDriver: $CHROMEDRIVER_VER"
echo "Python: $(python3 --version)"
echo ""
echo "Ready for snapshot! Create a DigitalOcean snapshot of this droplet."
echo "The snapshot can be used for bulk droplet creation."
