#!/bin/bash

# Fix Server Deployment Script
# Copies updated files to the correct location and restarts the service

echo "Starting deployment fix..."

# Define source and destination
SOURCE_DIR="/opt/gbot-web-app-original-working"
DEST_DIR="/opt/gbot-web-app"

# Ensure destination directory exists
if [ ! -d "$DEST_DIR" ]; then
    echo "Error: Destination directory $DEST_DIR does not exist."
    exit 1
fi

# Copy database.py (Critical for CloudflareConfig)
echo "Copying database.py..."
cp "$SOURCE_DIR/database.py" "$DEST_DIR/"

# Copy app.py (Critical for imports and routes)
echo "Copying app.py..."
cp "$SOURCE_DIR/app.py" "$DEST_DIR/"

# Copy services
echo "Copying services..."
cp "$SOURCE_DIR/services/cloudflare_dns_service.py" "$DEST_DIR/services/"
cp "$SOURCE_DIR/services/namecheap_dns_service.py" "$DEST_DIR/services/"
cp "$SOURCE_DIR/services/zone_utils.py" "$DEST_DIR/services/"
cp "$SOURCE_DIR/services/google_domains_service.py" "$DEST_DIR/services/"

# Copy routes
echo "Copying routes..."
cp "$SOURCE_DIR/routes/dns_manager.py" "$DEST_DIR/routes/"

# Copy templates
echo "Copying templates..."
cp "$SOURCE_DIR/templates/dashboard.html" "$DEST_DIR/templates/"
cp "$SOURCE_DIR/templates/settings.html" "$DEST_DIR/templates/"
cp "$SOURCE_DIR/templates/aws_management.html" "$DEST_DIR/templates/"

# Set permissions
echo "Setting permissions..."
chown -R www-data:www-data "$DEST_DIR"
chmod -R 755 "$DEST_DIR"

# Copy table creation script
echo "Copying table creation script..."
cp "$SOURCE_DIR/create_cloudflare_table.py" "$DEST_DIR/"

# Copy DB sequence fix script
echo "Copying DB sequence fix script..."
cp "$SOURCE_DIR/fix_db_sequence.py" "$DEST_DIR/"

# Run table creation
echo "Creating CloudflareConfig table..."
cd "$DEST_DIR"
./venv/bin/python create_cloudflare_table.py

# Run DB sequence fix
echo "Fixing DB sequence..."
./venv/bin/python fix_db_sequence.py

# Restart service
echo "Restarting gbot service..."
systemctl restart gbot

# Check status
echo "Checking service status..."
sleep 5
systemctl status gbot --no-pager

echo "Deployment fix completed."
