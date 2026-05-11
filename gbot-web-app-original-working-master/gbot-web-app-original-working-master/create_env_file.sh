#!/bin/bash
# Quick script to create .env file manually
# Usage: sudo bash create_env_file.sh

APP_DIR="/opt/gbot-web-app"
DB_NAME="gbot_db"
DB_USER="gbot_user"

# Check if running as root
if [ "$EUID" -ne 0 ]; then 
    echo "ERROR: Please run as root or with sudo"
    exit 1
fi

# Check if .env already exists
if [ -f "$APP_DIR/.env" ]; then
    echo "WARNING: .env file already exists at $APP_DIR/.env"
    read -p "Do you want to overwrite it? (y/N): " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo "Aborted."
        exit 1
    fi
    cp "$APP_DIR/.env" "$APP_DIR/.env.backup.$(date +%Y%m%d_%H%M%S)"
    echo "Backed up existing .env file"
fi

# Get database password (try to extract from existing .env or generate new)
if [ -f "$APP_DIR/.env.backup" ]; then
    DB_PASSWORD=$(grep "DATABASE_URL" "$APP_DIR/.env.backup" | sed 's/.*:\([^@]*\)@.*/\1/' || echo "")
fi

if [ -z "$DB_PASSWORD" ]; then
    echo "Generating new database password..."
    DB_PASSWORD=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
    
    # Update PostgreSQL user password
    sudo -u postgres psql -c "ALTER USER $DB_USER WITH PASSWORD '$DB_PASSWORD';" 2>/dev/null || \
    sudo -u postgres psql -c "CREATE USER $DB_USER WITH PASSWORD '$DB_PASSWORD';"
    
    # Grant privileges
    sudo -u postgres psql -c "GRANT ALL PRIVILEGES ON DATABASE $DB_NAME TO $DB_USER;" 2>/dev/null
fi

# Generate secrets
SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")
WHITELIST_TOKEN=$(python3 -c "import secrets; print(secrets.token_hex(16))")

# Get server IP
SERVER_IP=$(curl -s ifconfig.me 2>/dev/null || hostname -I | awk '{print $1}' || echo "127.0.0.1")

# Get app user
APP_USER="${SUDO_USER:-$USER}"

# Create .env file
echo "Creating .env file at $APP_DIR/.env..."

mkdir -p "$APP_DIR"

cat > "$APP_DIR/.env" <<EOF
# GBot Web Application Environment Configuration
# Generated automatically on $(date)

# Security
SECRET_KEY=$SECRET_KEY
WHITELIST_TOKEN=$WHITELIST_TOKEN

# Database
DATABASE_URL=postgresql://$DB_USER:$DB_PASSWORD@127.0.0.1/$DB_NAME

# IP Whitelist Configuration
ENABLE_IP_WHITELIST=True
ALLOW_ALL_IPS_IN_DEV=False

# Google API Configuration
GOOGLE_CLIENT_ID=
GOOGLE_CLIENT_SECRET=

# Application Settings
DEBUG=False
FLASK_ENV=production
LOG_LEVEL=INFO

# Production Settings
SESSION_COOKIE_SECURE=False
SESSION_COOKIE_HTTPONLY=True
SESSION_COOKIE_SAMESITE=Lax
PERMANENT_SESSION_LIFETIME=3600

# Server Information
SERVER_IP=$SERVER_IP
EOF

# Set permissions
chmod 600 "$APP_DIR/.env"
chown $APP_USER:$APP_USER "$APP_DIR/.env"

# Verify
if [ -f "$APP_DIR/.env" ]; then
    echo ""
    echo "✅ .env file created successfully at $APP_DIR/.env"
    echo ""
    echo "File location: $APP_DIR/.env"
    echo "File size: $(stat -c%s "$APP_DIR/.env" 2>/dev/null || echo 'unknown') bytes"
    echo "File permissions: $(stat -c '%a %U:%G' "$APP_DIR/.env" 2>/dev/null || echo 'unknown')"
    echo ""
    echo "🔑 Generated Secrets:"
    echo "  SECRET_KEY: $SECRET_KEY"
    echo "  WHITELIST_TOKEN: $WHITELIST_TOKEN"
    echo "  DB_PASSWORD: $DB_PASSWORD"
    echo ""
    echo "To view the file: cat $APP_DIR/.env"
    echo "To edit the file: nano $APP_DIR/.env"
else
    echo "❌ ERROR: Failed to create .env file!"
    exit 1
fi

