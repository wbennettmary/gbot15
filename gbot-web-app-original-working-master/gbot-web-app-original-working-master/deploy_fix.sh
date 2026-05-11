#!/bin/bash

echo "=== Deploying Fixed App.py to Ubuntu Server ==="

# Check if we're in the right directory
if [ ! -f "app.py" ]; then
    echo "ERROR: app.py not found in current directory"
    exit 1
fi

echo "✅ Found app.py in current directory"

# Test the app locally first
echo "🧪 Testing app.py syntax..."
python -c "import app; print('✅ App syntax is valid')" 2>&1
if [ $? -eq 0 ]; then
    echo "✅ App.py syntax is valid - ready to deploy"
else
    echo "❌ App.py has syntax errors - cannot deploy"
    exit 1
fi

echo ""
echo "📋 Next steps for Ubuntu server:"
echo "1. Copy this app.py file to your server:"
echo "   scp app.py root@172.233.16.144:/opt/gbot-web-app/"
echo ""
echo "2. Or manually copy the content and replace the file"
echo ""
echo "3. Then restart the service:"
echo "   sudo systemctl restart gbot"
echo ""
echo "4. Check status:"
echo "   sudo systemctl status gbot"
