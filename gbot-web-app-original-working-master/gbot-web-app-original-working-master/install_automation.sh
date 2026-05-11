#!/bin/bash
################################################################################
# GBot Complete Automated Installation Script for Ubuntu 22.04 LTS
# 
# This script performs a COMPLETE installation from a fresh Ubuntu 22 server:
#   - System updates and essential packages
#   - Swap file creation and memory optimization
#   - Kernel parameter tuning for production workloads
#   - Python 3.10+, pip, and virtual environment
#   - PostgreSQL database installation and configuration
#   - Nginx web server installation and configuration
#   - Chrome/Chromium + ChromeDriver for Selenium automation
#   - AWS CLI installation
#   - Application setup with all dependencies
#   - Systemd service configuration with memory limits
#   - Memory monitoring service (auto-restart on low memory)
#   - Firewall (UFW) configuration
#   - Log rotation and backup system
#   - Git repository cloning (if not present)
#
# Usage: 
#   chmod +x install_automation.sh
#   sudo ./install_automation.sh
#
# Author: GBot Automation
# Last Updated: January 2026
# Supports: Ubuntu 22.04 LTS and Ubuntu 24.04 LTS
################################################################################

set -e  # Exit on error

# ============================================================================
# Configuration Variables
# ============================================================================
APP_NAME="gbot"
APP_USER="${SUDO_USER:-$USER}"
APP_DIR="/opt/gbot-web-app"
DB_NAME="gbot_db"
DB_USER="gbot_user"
NGINX_SITE="gbot"
SERVICE_NAME="gbot"
LOG_DIR="/var/log/gbot"
PYTHON_MIN_VERSION="3.10"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# ============================================================================
# Helper Functions
# ============================================================================
print_info() { echo -e "${BLUE}ℹ️  $1${NC}"; }
print_success() { echo -e "${GREEN}✅ $1${NC}"; }
print_warning() { echo -e "${YELLOW}⚠️  $1${NC}"; }
print_error() { echo -e "${RED}❌ $1${NC}"; }

print_section() {
    echo ""
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${CYAN}🔹 $1${NC}"
    echo -e "${CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
}

print_banner() {
    echo ""
    echo -e "${CYAN}╔════════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${CYAN}║           🚀 GBot Complete Installation Script 🚀             ║${NC}"
    echo -e "${CYAN}║                    Ubuntu 22.04 LTS                            ║${NC}"
    echo -e "${CYAN}╚════════════════════════════════════════════════════════════════╝${NC}"
    echo ""
}

# Check if running as root
check_root() {
    if [ "$EUID" -ne 0 ]; then 
        print_error "Please run as root or with sudo"
        echo "Usage: sudo ./install_automation.sh"
        exit 1
    fi
}

# Remove stale apt lock files
remove_stale_locks() {
    print_info "Checking for stale lock files..."
    
    for lock_file in /var/lib/dpkg/lock-frontend /var/lib/dpkg/lock /var/cache/apt/archives/lock; do
        if [ -f "$lock_file" ]; then
            if ! lsof "$lock_file" > /dev/null 2>&1; then
                print_warning "Removing stale lock: $lock_file"
                rm -f "$lock_file"
            fi
        fi
    done
    
    # Reconfigure dpkg if needed
    dpkg --configure -a 2>/dev/null || true
}

# Wait for apt lock
wait_for_apt() {
    local max_wait=60
    local wait_time=0
    
    remove_stale_locks
    
    while pgrep -x "apt-get|apt|dpkg|unattended-upgrades" > /dev/null && [ $wait_time -lt $max_wait ]; do
        print_warning "Package manager is busy, waiting... ($wait_time/$max_wait sec)"
        sleep 5
        wait_time=$((wait_time + 5))
    done
    
    if [ $wait_time -ge $max_wait ]; then
        print_error "Timeout waiting for package manager. Please try again later."
        exit 1
    fi
    
    remove_stale_locks
}

# ============================================================================
# MAIN INSTALLATION
# ============================================================================

print_banner
check_root

print_info "Installation started at: $(date)"
print_info "Target directory: $APP_DIR"
print_info "Application user: $APP_USER"
echo ""

# ============================================================================
# STEP 1: System Updates and Essential Packages
# ============================================================================
print_section "STEP 1/15: System Updates and Essential Packages"

wait_for_apt

print_info "Updating package lists..."
apt-get update -qq

print_info "Upgrading system packages..."
DEBIAN_FRONTEND=noninteractive apt-get upgrade -y -qq

print_info "Installing essential system packages..."
wait_for_apt
apt-get install -y \
    software-properties-common \
    apt-transport-https \
    ca-certificates \
    gnupg \
    lsb-release \
    curl \
    wget \
    git \
    unzip \
    zip \
    jq \
    htop \
    nano \
    vim \
    ufw \
    fail2ban \
    logrotate \
    cron \
    build-essential \
    libssl-dev \
    libffi-dev \
    libpq-dev \
    python3-dev \
    python3-pip \
    python3-venv \
    python3-setuptools \
    python3-wheel \
    lsof

print_success "System packages installed"

# ============================================================================
# STEP 2: Swap File and Memory Optimization
# ============================================================================
print_section "STEP 2/15: Swap File and Memory Optimization"

# Check if swap already exists
if [ $(swapon --show | wc -l) -eq 0 ]; then
    # Calculate swap size (2x RAM up to 8GB, then 1x)
    RAM_MB=$(free -m | awk '/^Mem:/{print $2}')
    if [ $RAM_MB -lt 4096 ]; then
        SWAP_SIZE="4G"
    elif [ $RAM_MB -lt 8192 ]; then
        SWAP_SIZE="8G"
    else
        SWAP_SIZE="8G"
    fi
    
    print_info "Creating ${SWAP_SIZE} swap file..."
    fallocate -l $SWAP_SIZE /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=${SWAP_SIZE%G}000
    chmod 600 /swapfile
    mkswap /swapfile
    swapon /swapfile
    
    # Make permanent
    if ! grep -q "/swapfile" /etc/fstab; then
        echo '/swapfile none swap sw 0 0' >> /etc/fstab
    fi
    
    print_success "Swap file created: ${SWAP_SIZE}"
else
    print_info "Swap already exists:"
    swapon --show
fi

# ============================================================================
# STEP 3: Kernel Parameter Tuning for Production
# ============================================================================
print_section "STEP 3/15: Kernel Parameter Tuning"

print_info "Applying production kernel optimizations..."

cat > /etc/sysctl.d/99-gbot-production.conf <<'SYSCTL_EOF'
# GBot Production Kernel Parameters
# Prevents freezing and improves stability

# Memory Management
vm.swappiness = 10
vm.dirty_ratio = 10
vm.dirty_background_ratio = 5
vm.overcommit_memory = 0
vm.overcommit_ratio = 80

# Network Performance
net.core.somaxconn = 65535
net.core.netdev_max_backlog = 65535
net.ipv4.tcp_max_syn_backlog = 65535
net.ipv4.tcp_fin_timeout = 15
net.ipv4.tcp_keepalive_time = 300
net.ipv4.tcp_keepalive_probes = 5
net.ipv4.tcp_keepalive_intvl = 15
net.ipv4.tcp_tw_reuse = 1
net.ipv4.ip_local_port_range = 10000 65535

# File Descriptors
fs.file-max = 2097152
fs.nr_open = 2097152

# Increase inotify limits
fs.inotify.max_user_watches = 524288
fs.inotify.max_user_instances = 512

# Prevent OOM killer issues
vm.oom_kill_allocating_task = 1
SYSCTL_EOF

sysctl --system > /dev/null 2>&1

# Increase system limits
cat > /etc/security/limits.d/99-gbot.conf <<'LIMITS_EOF'
# GBot System Limits
*               soft    nofile          524288
*               hard    nofile          524288
root            soft    nofile          524288
root            hard    nofile          524288
*               soft    nproc           32768
*               hard    nproc           32768
LIMITS_EOF

print_success "Kernel parameters optimized for production"

# ============================================================================
# STEP 4: Python 3.10+ Setup
# ============================================================================
print_section "STEP 4/15: Python Setup"

PYTHON_VERSION=$(python3 --version 2>/dev/null | cut -d' ' -f2 | cut -d'.' -f1,2 || echo "0")
print_info "Current Python version: $(python3 --version 2>/dev/null || echo 'Not installed')"

if [ "$(printf '%s\n' "$PYTHON_MIN_VERSION" "$PYTHON_VERSION" | sort -V | head -n1)" != "$PYTHON_MIN_VERSION" ]; then
    print_warning "Python 3.10+ required. Installing from deadsnakes PPA..."
    wait_for_apt
    add-apt-repository -y ppa:deadsnakes/ppa
    wait_for_apt
    apt-get update -qq
    wait_for_apt
    apt-get install -y python3.10 python3.10-venv python3.10-dev python3.10-distutils
    update-alternatives --install /usr/bin/python3 python3 /usr/bin/python3.10 1
    print_success "Python 3.10 installed"
else
    print_success "Python version is sufficient"
fi

# Upgrade pip
print_info "Upgrading pip..."
python3 -m pip install --upgrade pip setuptools wheel

print_success "Python setup complete"

# ============================================================================
# STEP 5: PostgreSQL Database Installation
# ============================================================================
print_section "STEP 5/15: PostgreSQL Database Installation"

print_info "Installing PostgreSQL..."
wait_for_apt
apt-get install -y postgresql postgresql-contrib

print_info "Starting PostgreSQL service..."
systemctl enable postgresql
systemctl start postgresql

# Generate secure database password
DB_PASSWORD=$(python3 -c "import secrets; print(secrets.token_urlsafe(24))")

print_info "Configuring database and user..."

# Create database if not exists
sudo -u postgres psql -tc "SELECT 1 FROM pg_database WHERE datname = '$DB_NAME'" | grep -q 1 || \
    sudo -u postgres psql -c "CREATE DATABASE $DB_NAME;"

# Create/update user
if sudo -u postgres psql -tc "SELECT 1 FROM pg_roles WHERE rolname = '$DB_USER'" | grep -q 1; then
    print_info "Updating existing user password..."
    sudo -u postgres psql -c "ALTER USER $DB_USER WITH PASSWORD '$DB_PASSWORD';"
else
    print_info "Creating database user..."
    sudo -u postgres psql -c "CREATE USER $DB_USER WITH PASSWORD '$DB_PASSWORD';"
fi

# Grant privileges
sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE $DB_NAME TO $DB_USER;"
sudo -u postgres psql -c "ALTER DATABASE $DB_NAME OWNER TO $DB_USER;"

# Grant schema permissions (PostgreSQL 15+ requirement)
sudo -u postgres psql -d "$DB_NAME" -c "GRANT ALL ON SCHEMA public TO $DB_USER;" 2>/dev/null || true

# Grant comprehensive permissions on all existing and future tables/sequences
print_info "Setting up comprehensive database permissions..."
sudo -u postgres psql -d "$DB_NAME" -c "GRANT USAGE, CREATE ON SCHEMA public TO $DB_USER;"
sudo -u postgres psql -d "$DB_NAME" -c "GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO $DB_USER;"
sudo -u postgres psql -d "$DB_NAME" -c "GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO $DB_USER;"
sudo -u postgres psql -d "$DB_NAME" -c "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO $DB_USER;"
sudo -u postgres psql -d "$DB_NAME" -c "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO $DB_USER;"

# Transfer ownership of all existing tables to gbot_user (for restored backups)
sudo -u postgres psql -d "$DB_NAME" -c "
DO \$\$
DECLARE
    r RECORD;
BEGIN
    FOR r IN (SELECT tablename FROM pg_tables WHERE schemaname = 'public') LOOP
        EXECUTE 'ALTER TABLE public.' || quote_ident(r.tablename) || ' OWNER TO $DB_USER';
    END LOOP;
END
\$\$;
" 2>/dev/null || true

# Transfer ownership of all sequences
sudo -u postgres psql -d "$DB_NAME" -c "
DO \$\$
DECLARE
    r RECORD;
BEGIN
    FOR r IN (SELECT sequence_name FROM information_schema.sequences WHERE sequence_schema = 'public') LOOP
        EXECUTE 'ALTER SEQUENCE public.' || quote_ident(r.sequence_name) || ' OWNER TO $DB_USER';
    END LOOP;
END
\$\$;
" 2>/dev/null || true

print_success "PostgreSQL configured successfully with comprehensive permissions"

# ============================================================================
# STEP 6: Chrome/Chromium and ChromeDriver Installation
# ============================================================================
print_section "STEP 6/15: Chrome and ChromeDriver Installation"

print_info "Installing Chromium browser and ChromeDriver..."
wait_for_apt
apt-get install -y chromium-browser chromium-chromedriver

# Verify installation
if command -v chromium-browser &> /dev/null; then
    CHROME_VERSION=$(chromium-browser --version 2>/dev/null || echo "Unknown")
    print_success "Chromium installed: $CHROME_VERSION"
else
    print_warning "Chromium not found, trying Google Chrome..."
    wget -q -O /tmp/google-chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
    apt-get install -y /tmp/google-chrome.deb || true
    rm -f /tmp/google-chrome.deb
fi

# ChromeDriver path for selenium
CHROMEDRIVER_PATH=$(which chromedriver 2>/dev/null || echo "/usr/bin/chromedriver")
print_info "ChromeDriver path: $CHROMEDRIVER_PATH"

print_success "Chrome/ChromeDriver setup complete"

# ============================================================================
# STEP 7: AWS CLI Installation
# ============================================================================
print_section "STEP 7/15: AWS CLI Installation"

if command -v aws &> /dev/null; then
    print_info "AWS CLI already installed: $(aws --version)"
else
    print_info "Installing AWS CLI v2..."
    cd /tmp
    curl -s "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "awscliv2.zip"
    unzip -q -o awscliv2.zip
    ./aws/install --update
    rm -rf awscliv2.zip aws
fi

print_success "AWS CLI installed: $(aws --version 2>/dev/null || echo 'Ready')"

# ============================================================================
# STEP 7b: Docker Installation (for ECR image operations)
# ============================================================================
print_section "STEP 7b: Docker Installation"

if command -v docker &> /dev/null; then
    print_info "Docker already installed: $(docker --version)"
else
    print_info "Installing Docker..."
    wait_for_apt
    
    # Add Docker's official GPG key
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc
    
    # Add the repository to Apt sources
    echo \
        "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu \
        $(. /etc/os-release && echo "$VERSION_CODENAME") stable" | \
        tee /etc/apt/sources.list.d/docker.list > /dev/null
    
    wait_for_apt
    apt-get update -qq
    wait_for_apt
    apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
    
    # Add user to docker group
    usermod -aG docker $APP_USER
    
    # Start and enable Docker
    systemctl enable docker
    systemctl start docker
    
    print_success "Docker installed: $(docker --version)"
fi

# ============================================================================
# STEP 7c: Node.js Installation (for frontend builds if needed)
# ============================================================================
print_section "STEP 7c: Node.js Installation"

if command -v node &> /dev/null; then
    print_info "Node.js already installed: $(node --version)"
else
    print_info "Installing Node.js LTS..."
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
    wait_for_apt
    apt-get install -y nodejs
    print_success "Node.js installed: $(node --version)"
fi

# ============================================================================
# STEP 8: Nginx Web Server Installation
# ============================================================================
print_section "STEP 8/15: Nginx Web Server Installation"

print_info "Installing Nginx..."
wait_for_apt
apt-get install -y nginx

# Remove default site
rm -f /etc/nginx/sites-enabled/default

# Create Nginx configuration
print_info "Creating Nginx configuration..."
cat > /etc/nginx/sites-available/$NGINX_SITE <<'NGINX_EOF'
# Rate limiting zones
limit_conn_zone $binary_remote_addr zone=conn_limit_per_ip:10m;
limit_req_zone $binary_remote_addr zone=api:10m rate=10r/s;
limit_req_zone $binary_remote_addr zone=upload:10m rate=1r/s;

upstream gbot_app {
    server 127.0.0.1:5000 fail_timeout=0;
    keepalive 32;
}

server {
    listen 80;
    server_name _;

    # Security headers
    add_header X-Frame-Options "SAMEORIGIN" always;
    add_header X-XSS-Protection "1; mode=block" always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header Referrer-Policy "no-referrer-when-downgrade" always;

    # Client settings
    client_max_body_size 500M;
    client_body_buffer_size 128k;
    client_body_timeout 300s;
    client_header_timeout 60s;
    keepalive_timeout 65s;

    # Gzip compression
    gzip on;
    gzip_vary on;
    gzip_min_length 1024;
    gzip_comp_level 6;
    gzip_types text/plain text/css text/xml text/javascript application/json application/javascript application/xml;

    # Connection limiting
    limit_conn conn_limit_per_ip 100;

    # Main location
    location / {
        limit_req zone=api burst=100 nodelay;
        
        proxy_pass http://gbot_app;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        
        proxy_connect_timeout 60s;
        proxy_send_timeout 300s;
        proxy_read_timeout 300s;
        
        proxy_http_version 1.1;
        proxy_set_header Connection "";
    }

    # Upload endpoint
    location /api/upload-app-passwords {
        limit_req zone=upload burst=5 nodelay;
        
        proxy_pass http://gbot_app;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        
        proxy_connect_timeout 60s;
        proxy_send_timeout 600s;
        proxy_read_timeout 600s;
        
        proxy_request_buffering off;
        proxy_buffering off;
    }

    # Health check
    location /health {
        access_log off;
        return 200 "healthy\n";
        add_header Content-Type text/plain;
    }

    # Block sensitive files
    location ~ /\. { deny all; access_log off; log_not_found off; }
    location ~ \.(env|log|conf)$ { deny all; access_log off; log_not_found off; }
}
NGINX_EOF

# Enable site and test
ln -sf /etc/nginx/sites-available/$NGINX_SITE /etc/nginx/sites-enabled/
nginx -t
systemctl enable nginx
systemctl restart nginx

print_success "Nginx configured and started"

# ============================================================================
# STEP 9: Application Directory Setup
# ============================================================================
print_section "STEP 9/15: Application Directory Setup"

# GitHub repository URL
GITHUB_REPO="https://github.com/Jetalp54/gbot-web-app-original-working.git"

# Get the script's directory (where the source files are)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Check if app.py exists in APP_DIR
if [ ! -f "$APP_DIR/app.py" ]; then
    if [ -f "$SCRIPT_DIR/app.py" ] && [ "$SCRIPT_DIR" != "$APP_DIR" ]; then
        print_info "Copying application files from $SCRIPT_DIR to $APP_DIR..."
        mkdir -p $APP_DIR
        cp -r "$SCRIPT_DIR"/* $APP_DIR/ 2>/dev/null || true
        cp "$SCRIPT_DIR"/.env $APP_DIR/ 2>/dev/null || true
        cp "$SCRIPT_DIR"/.gitignore $APP_DIR/ 2>/dev/null || true
    else
        print_info "Cloning application from GitHub to $APP_DIR..."
        # Remove any existing directory to ensure clean clone
        rm -rf $APP_DIR 2>/dev/null || true
        # Clone directly into the target directory name (not the repo name)
        git clone $GITHUB_REPO $APP_DIR
        print_success "Repository cloned successfully to $APP_DIR"
    fi
else
    print_info "Application files already exist in $APP_DIR"
    # Update from git if .git directory exists
    if [ -d "$APP_DIR/.git" ]; then
        print_info "Pulling latest changes from GitHub..."
        cd $APP_DIR
        git pull origin master 2>/dev/null || git pull origin main 2>/dev/null || print_warning "Could not pull latest changes"
    fi
fi

# Create required subdirectories AFTER clone/copy
mkdir -p $APP_DIR/logs
mkdir -p $APP_DIR/backups
mkdir -p $APP_DIR/instance
mkdir -p $LOG_DIR

# Set ownership
chown -R $APP_USER:$APP_USER $APP_DIR
chown -R $APP_USER:$APP_USER $LOG_DIR

print_success "Application directory setup complete"

# ============================================================================
# STEP 10: Python Virtual Environment and Dependencies
# ============================================================================
print_section "STEP 10/15: Python Virtual Environment and Dependencies"

cd $APP_DIR

# Create virtual environment
print_info "Creating Python virtual environment..."
if [ ! -d "venv" ]; then
    python3 -m venv venv
fi

# Activate and upgrade pip
print_info "Installing Python dependencies..."
source venv/bin/activate
pip install --upgrade pip setuptools wheel

# Install requirements
if [ -f requirements.txt ]; then
    pip install -r requirements.txt
else
    print_warning "requirements.txt not found, installing essential packages..."
    pip install \
        Flask==3.0.3 \
        paramiko==3.5.0 \
        google-auth==2.38.0 \
        google-auth-oauthlib==1.2.2 \
        google-api-python-client==2.165.0 \
        faker==33.3.0 \
        psycopg2-binary==2.9.10 \
        Flask-SQLAlchemy==3.1.1 \
        python-dotenv==1.0.1 \
        psutil==6.1.1 \
        gunicorn==23.0.0 \
        requests==2.32.3 \
        pyotp==2.9.0 \
        publicsuffix2==2.20191221 \
        boto3==1.35.100 \
        selenium==4.27.1 \
        webdriver-manager==4.0.2 \
        selenium-stealth==1.0.6 \
        fake-useragent==2.0.3 \
        undetected-chromedriver==3.5.5
fi

# Install additional production packages (explicitly to ensure they're installed)
pip install gunicorn psutil psycopg2-binary

# Verify gunicorn was installed
if [ ! -f "$APP_DIR/venv/bin/gunicorn" ]; then
    print_error "CRITICAL: gunicorn not found in venv! Trying to install again..."
    pip install --force-reinstall gunicorn
    if [ ! -f "$APP_DIR/venv/bin/gunicorn" ]; then
        print_error "Failed to install gunicorn. Please check pip and try manually."
        exit 1
    fi
fi
print_success "✓ Verified gunicorn is installed at $APP_DIR/venv/bin/gunicorn"

# Fix ownership of venv (created as root, needs to be owned by APP_USER for systemd)
chown -R $APP_USER:$APP_USER $APP_DIR/venv
chmod -R 755 $APP_DIR/venv/bin

print_success "Python dependencies installed"

# ============================================================================
# STEP 11: Environment Configuration
# ============================================================================
print_section "STEP 11/15: Environment Configuration"

cd $APP_DIR

# Generate secrets
SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")
WHITELIST_TOKEN=$(python3 -c "import secrets; print(secrets.token_hex(16))")
SERVER_IP=$(curl -s ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}' || echo "127.0.0.1")

ENV_FILE="$APP_DIR/.env"

# ALWAYS create a fresh production .env file
# The git repo contains a dev template that must be replaced with production values
print_info "Creating production .env file with generated secrets..."

# Backup any existing .env (whether dev template or old production config)
if [ -f "$ENV_FILE" ]; then
    print_warning "Backing up existing .env file..."
    mv "$ENV_FILE" "$APP_DIR/.env.backup.$(date +%Y%m%d_%H%M%S)"
fi

# Create fresh production .env with all required values
cat > "$ENV_FILE" <<EOF
# GBot Web Application - Production Configuration
# Generated: $(date)
# DO NOT COMMIT THIS FILE TO GIT!

# Security (auto-generated - keep these secret!)
SECRET_KEY=$SECRET_KEY
WHITELIST_TOKEN=$WHITELIST_TOKEN

# Database - PostgreSQL connection (REQUIRED - DO NOT COMMENT OUT!)
DATABASE_URL=postgresql://$DB_USER:$DB_PASSWORD@127.0.0.1/$DB_NAME

# IP Whitelist Configuration
ENABLE_IP_WHITELIST=True
ALLOW_ALL_IPS_IN_DEV=False

# Google API Configuration (fill in your credentials)
GOOGLE_CLIENT_ID=
GOOGLE_CLIENT_SECRET=

# Application Settings (PRODUCTION MODE)
DEBUG=False
FLASK_ENV=production
LOG_LEVEL=INFO

# Session Settings
SESSION_COOKIE_SECURE=False
SESSION_COOKIE_HTTPONLY=True
SESSION_COOKIE_SAMESITE=Lax
PERMANENT_SESSION_LIFETIME=3600

# Server Information
SERVER_IP=$SERVER_IP

# Chrome Settings for Selenium
CHROME_BINARY=/usr/bin/chromium-browser
CHROMEDRIVER_PATH=/usr/bin/chromedriver
EOF

# Ensure .env is in .gitignore so git pull never overwrites it again
if ! grep -q "^\.env$" "$APP_DIR/.gitignore" 2>/dev/null; then
    echo "" >> "$APP_DIR/.gitignore"
    echo "# Never commit production .env file" >> "$APP_DIR/.gitignore"
    echo ".env" >> "$APP_DIR/.gitignore"
    print_info "Added .env to .gitignore"
fi

chmod 600 "$ENV_FILE"
chown $APP_USER:$APP_USER "$ENV_FILE"

# Verify DATABASE_URL is set and NOT commented
print_info "Verifying DATABASE_URL configuration..."
if grep -q "^DATABASE_URL=" "$ENV_FILE"; then
    print_success "✓ DATABASE_URL is correctly configured (not commented)"
else
    print_error "DATABASE_URL is missing or commented! Adding it now..."
    echo "DATABASE_URL=postgresql://$DB_USER:$DB_PASSWORD@127.0.0.1/$DB_NAME" >> "$ENV_FILE"
fi

# Test database connection before proceeding
print_info "Testing database connection..."
source $APP_DIR/venv/bin/activate
if python3 -c "import psycopg2; conn = psycopg2.connect('postgresql://$DB_USER:$DB_PASSWORD@127.0.0.1/$DB_NAME'); conn.close(); print('OK')" 2>/dev/null; then
    print_success "✓ Database connection successful!"
else
    print_error "Database connection FAILED! Trying to fix..."
    # Reset the password in PostgreSQL to match what we have
    sudo -u postgres psql -c "ALTER USER $DB_USER WITH PASSWORD '$DB_PASSWORD';" 2>/dev/null || true
    
    # Test again
    if python3 -c "import psycopg2; conn = psycopg2.connect('postgresql://$DB_USER:$DB_PASSWORD@127.0.0.1/$DB_NAME'); conn.close(); print('OK')" 2>/dev/null; then
        print_success "✓ Database connection fixed and working!"
    else
        print_error "Could not establish database connection. Please check PostgreSQL manually."
        print_error "DATABASE_URL: postgresql://$DB_USER:****@127.0.0.1/$DB_NAME"
    fi
fi

print_success "Environment configuration complete"

# ============================================================================
# STEP 12: Database Initialization
# ============================================================================
print_section "STEP 12/15: Database Initialization"

cd $APP_DIR
source venv/bin/activate

if [ -f migrate_db.py ]; then
    print_info "Running database migration..."
    python3 migrate_db.py || print_warning "Migration may have already been applied"
else
    print_info "Initializing database tables..."
    python3 -c "
from app import app, db
with app.app_context():
    db.create_all()
    print('Database tables created successfully')
" 2>/dev/null || print_warning "Database initialization may need manual attention"
fi

print_success "Database initialization complete"

# ============================================================================
# STEP 13: Systemd Service Configuration
# ============================================================================
print_section "STEP 13/15: Systemd Service Configuration"

# Ensure log directories exist with proper permissions
print_info "Ensuring log directories exist with correct permissions..."
mkdir -p $LOG_DIR
mkdir -p $APP_DIR/logs
chown -R $APP_USER:$APP_USER $LOG_DIR
chown -R $APP_USER:$APP_USER $APP_DIR/logs
chmod 755 $LOG_DIR
chmod 755 $APP_DIR/logs
touch $LOG_DIR/access.log $LOG_DIR/error.log
chown $APP_USER:$APP_USER $LOG_DIR/*.log

# Always create/update gunicorn config with absolute log paths
print_info "Creating gunicorn configuration with absolute log paths..."
cat > "$APP_DIR/gunicorn.conf.py" <<'GUNICORN_EOF'
import multiprocessing
import os

# Server socket
bind = "127.0.0.1:5000"
backlog = 2048

# Worker processes
workers = multiprocessing.cpu_count() * 2 + 1
worker_class = 'sync'
worker_connections = 1000
timeout = 300
keepalive = 5
max_requests = 1000
max_requests_jitter = 50

# Logging - absolute paths for systemd compatibility
accesslog = '/var/log/gbot/access.log'
errorlog = '/var/log/gbot/error.log'
loglevel = 'info'
capture_output = True

# Process naming
proc_name = 'gbot'

# Server mechanics
daemon = False
pidfile = '/tmp/gbot.pid'
umask = 0
user = None
group = None
tmp_upload_dir = None

# SSL (disabled by default)
keyfile = None
certfile = None
GUNICORN_EOF
chown $APP_USER:$APP_USER "$APP_DIR/gunicorn.conf.py"

# Create systemd service
print_info "Creating systemd service..."
cat > /etc/systemd/system/$SERVICE_NAME.service <<SERVICE_EOF
[Unit]
Description=GBot Web Application
After=network.target postgresql.service
Wants=postgresql.service

[Service]
Type=notify
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$APP_DIR
Environment=PATH=$APP_DIR/venv/bin
Environment=FLASK_ENV=production
Environment=PYTHONPATH=$APP_DIR
EnvironmentFile=$APP_DIR/.env
ExecStart=$APP_DIR/venv/bin/gunicorn --config gunicorn.conf.py app:app
ExecReload=/bin/kill -s HUP \$MAINPID
KillMode=mixed
TimeoutStopSec=300
PrivateTmp=true
Restart=always
RestartSec=10

LimitNOFILE=524288
LimitNPROC=32768

# Security settings
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=$APP_DIR
ReadWritePaths=$LOG_DIR
ReadWritePaths=/tmp

# Logging
StandardOutput=journal
StandardError=journal
SyslogIdentifier=$SERVICE_NAME

[Install]
WantedBy=multi-user.target
SERVICE_EOF

# Reload and enable
systemctl daemon-reload
systemctl enable $SERVICE_NAME

print_success "Systemd service configured"

# ============================================================================
# STEP 13b: Firewall, Log Rotation, and Final Setup
# ============================================================================
print_section "STEP 13/15: Firewall, Log Rotation, and Backup Setup"

# Firewall
print_info "Configuring firewall..."
ufw --force enable
ufw allow 22/tcp comment 'SSH'
ufw allow 'Nginx Full' comment 'HTTP/HTTPS'
ufw allow from 127.0.0.1

# Log rotation
print_info "Configuring log rotation..."
cat > /etc/logrotate.d/$SERVICE_NAME <<LOGROTATE_EOF
$LOG_DIR/*.log {
    daily
    missingok
    rotate 14
    compress
    delaycompress
    notifempty
    create 0640 $APP_USER $APP_USER
    sharedscripts
    postrotate
        systemctl reload $SERVICE_NAME > /dev/null 2>&1 || true
    endscript
}

$APP_DIR/logs/*.log {
    daily
    missingok
    rotate 7
    compress
    delaycompress
    notifempty
    create 0640 $APP_USER $APP_USER
}
LOGROTATE_EOF

# Backup script
print_info "Creating backup script..."
cat > $APP_DIR/backup.sh <<'BACKUP_EOF'
#!/bin/bash
BACKUP_DIR="/opt/gbot-web-app/backups"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)

mkdir -p $BACKUP_DIR

# Backup database
sudo -u postgres pg_dump gbot_db | gzip > $BACKUP_DIR/db_$TIMESTAMP.sql.gz

# Backup application
tar -czf $BACKUP_DIR/app_$TIMESTAMP.tar.gz \
    --exclude='venv' \
    --exclude='*.pyc' \
    --exclude='__pycache__' \
    --exclude='logs/*' \
    --exclude='backups/*' \
    /opt/gbot-web-app

# Keep only last 7 days
find $BACKUP_DIR -name "*.gz" -mtime +7 -delete

echo "Backup completed: $BACKUP_DIR/db_$TIMESTAMP.sql.gz"
BACKUP_EOF

chmod +x $APP_DIR/backup.sh
chown $APP_USER:$APP_USER $APP_DIR/backup.sh

# Add backup to crontab (daily at 2 AM)
(crontab -u $APP_USER -l 2>/dev/null | grep -v "backup.sh"; echo "0 2 * * * $APP_DIR/backup.sh >> $LOG_DIR/backup.log 2>&1") | crontab -u $APP_USER -

# Fix permissions script (for use after restoring backups)
print_info "Creating database permission fix script..."
cat > $APP_DIR/fix_permissions.sh <<'FIXPERM_EOF'
#!/bin/bash
#
# Fix database permissions after restoring a backup
# Usage: sudo ./fix_permissions.sh
#
echo "🔧 Fixing database permissions for gbot_user..."

DB_NAME="gbot_db"
DB_USER="gbot_user"

# Grant schema permissions
sudo -u postgres psql -d "$DB_NAME" -c "GRANT ALL ON SCHEMA public TO $DB_USER;"
sudo -u postgres psql -d "$DB_NAME" -c "GRANT USAGE, CREATE ON SCHEMA public TO $DB_USER;"
sudo -u postgres psql -d "$DB_NAME" -c "GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO $DB_USER;"
sudo -u postgres psql -d "$DB_NAME" -c "GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO $DB_USER;"
sudo -u postgres psql -d "$DB_NAME" -c "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO $DB_USER;"
sudo -u postgres psql -d "$DB_NAME" -c "ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO $DB_USER;"

# Transfer ownership of all tables
echo "📋 Transferring table ownership..."
sudo -u postgres psql -d "$DB_NAME" -c "
DO \$\$
DECLARE
    r RECORD;
BEGIN
    FOR r IN (SELECT tablename FROM pg_tables WHERE schemaname = 'public') LOOP
        EXECUTE 'ALTER TABLE public.' || quote_ident(r.tablename) || ' OWNER TO $DB_USER';
        RAISE NOTICE 'Changed owner of table % to $DB_USER', r.tablename;
    END LOOP;
END
\$\$;
"

# Transfer ownership of all sequences
echo "📋 Transferring sequence ownership..."
sudo -u postgres psql -d "$DB_NAME" -c "
DO \$\$
DECLARE
    r RECORD;
BEGIN
    FOR r IN (SELECT sequence_name FROM information_schema.sequences WHERE sequence_schema = 'public') LOOP
        EXECUTE 'ALTER SEQUENCE public.' || quote_ident(r.sequence_name) || ' OWNER TO $DB_USER';
        RAISE NOTICE 'Changed owner of sequence % to $DB_USER', r.sequence_name;
    END LOOP;
END
\$\$;
"

# Verify
echo ""
echo "📊 Current table ownership:"
sudo -u postgres psql -d "$DB_NAME" -c "SELECT tablename, tableowner FROM pg_tables WHERE schemaname = 'public';"

echo ""
echo "✅ Database permissions fixed!"
echo "🔄 Now restart the service: sudo systemctl restart gbot"
FIXPERM_EOF

chmod +x $APP_DIR/fix_permissions.sh
chown $APP_USER:$APP_USER $APP_DIR/fix_permissions.sh

print_success "Firewall, log rotation, backup, and permission fix scripts configured"

# ============================================================================
# STEP 14: Memory Monitoring Service
# ============================================================================
print_section "STEP 14/15: Memory Monitoring Service"

print_info "Creating memory monitoring service..."

# Create memory monitor script
cat > $APP_DIR/memory_monitor.sh <<'MEMMON_EOF'
#!/bin/bash
#
# Memory Monitor for GBot
# Auto-restarts the service if memory usage exceeds threshold
#

THRESHOLD=90
SERVICE="gbot"
LOG_FILE="/var/log/gbot/memory_monitor.log"

while true; do
    MEM_USED=$(free | awk '/Mem:/ {printf "%.0f", $3/$2 * 100}')
    
    if [ "$MEM_USED" -gt "$THRESHOLD" ]; then
        echo "$(date): Memory at ${MEM_USED}% - restarting $SERVICE" >> $LOG_FILE
        systemctl restart $SERVICE
        sleep 60  # Wait before checking again
    fi
    
    sleep 30
done
MEMMON_EOF

chmod +x $APP_DIR/memory_monitor.sh
chown $APP_USER:$APP_USER $APP_DIR/memory_monitor.sh

# Create memory monitor systemd service
cat > /etc/systemd/system/gbot-memory-monitor.service <<'MEMMONSVC_EOF'
[Unit]
Description=GBot Memory Monitor
After=gbot.service
BindsTo=gbot.service

[Service]
Type=simple
ExecStart=/opt/gbot-web-app/memory_monitor.sh
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
MEMMONSVC_EOF

systemctl daemon-reload
systemctl enable gbot-memory-monitor

print_success "Memory monitoring service configured"

# ============================================================================
# STEP 15: Start Services
# ============================================================================
print_section "STEP 15/15: Starting Services"

print_info "Starting GBot service..."
systemctl start $SERVICE_NAME

# Wait and verify
sleep 3

if systemctl is-active --quiet $SERVICE_NAME; then
    print_success "GBot service started successfully"
else
    print_warning "Service may not have started properly. Checking logs..."
    journalctl -u $SERVICE_NAME -n 20 --no-pager
fi

# Start memory monitor
print_info "Starting memory monitoring service..."
systemctl start gbot-memory-monitor

if systemctl is-active --quiet gbot-memory-monitor; then
    print_success "Memory monitoring service started"
else
    print_warning "Memory monitoring service may need manual start"
fi

# ============================================================================
# Installation Complete
# ============================================================================
echo ""
echo -e "${GREEN}╔════════════════════════════════════════════════════════════════╗${NC}"
echo -e "${GREEN}║             ✅ INSTALLATION COMPLETE! ✅                       ║${NC}"
echo -e "${GREEN}╚════════════════════════════════════════════════════════════════╝${NC}"
echo ""

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "📋 Installation Summary:"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "  Application Directory: $APP_DIR"
echo "  Environment File:      $APP_DIR/.env"
echo "  Log Directory:         $LOG_DIR"
echo "  Database:              $DB_NAME"
echo "  Database User:         $DB_USER"
echo ""

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "🌐 Access Information:"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "  Server IP:     $SERVER_IP"
echo "  HTTP URL:      http://$SERVER_IP"
echo "  Health Check:  http://$SERVER_IP/health"
echo ""

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "🔑 Generated Secrets (SAVE THESE!):"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "  SECRET_KEY:      $SECRET_KEY"
echo "  WHITELIST_TOKEN: $WHITELIST_TOKEN"
echo "  DB_PASSWORD:     $DB_PASSWORD"
echo ""
echo "  Emergency Access: http://$SERVER_IP/emergency_access?key=$WHITELIST_TOKEN"
echo ""

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "📝 Useful Commands:"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "  Service Status:    sudo systemctl status $SERVICE_NAME"
echo "  View Logs:         sudo journalctl -u $SERVICE_NAME -f"
echo "  Restart Service:   sudo systemctl restart $SERVICE_NAME"
echo "  Nginx Logs:        sudo tail -f /var/log/nginx/error.log"
echo "  Edit Config:       sudo nano $APP_DIR/.env"
echo "  Run Backup:        sudo $APP_DIR/backup.sh"
echo ""

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "🔧 Next Steps:"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "  1. Configure Google OAuth in $APP_DIR/.env"
echo "  2. Configure AWS credentials: aws configure"
echo "  3. Access the application at http://$SERVER_IP"
echo ""
echo "  4. ⚠️  To set up SSL/HTTPS, run these commands (replace YOUR_DOMAIN):"
echo ""
echo "     # Step 1: Create domain-specific Nginx config"
echo "     sudo nano /etc/nginx/sites-available/YOUR_DOMAIN"
echo ""
echo "     # Step 2: Add this content (replace YOUR_DOMAIN):"
cat << 'SSL_EXAMPLE'
     server {
         listen 80;
         server_name YOUR_DOMAIN www.YOUR_DOMAIN;

         location / {
             proxy_pass http://127.0.0.1:5000;
             proxy_set_header Host $host;
             proxy_set_header X-Real-IP $remote_addr;
             proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
             proxy_set_header X-Forwarded-Proto $scheme;
         }
     }
SSL_EXAMPLE
echo ""
echo "     # Step 3: Enable the site"
echo "     sudo ln -s /etc/nginx/sites-available/YOUR_DOMAIN /etc/nginx/sites-enabled/"
echo ""
echo "     # Step 4: Test and reload Nginx"
echo "     sudo nginx -t && sudo systemctl reload nginx"
echo ""
echo "     # Step 5: Install SSL certificate"
echo "     sudo apt install -y certbot python3-certbot-nginx"
echo "     sudo certbot --nginx -d YOUR_DOMAIN -d www.YOUR_DOMAIN"
echo ""

print_success "Installation completed at: $(date)"
echo ""
