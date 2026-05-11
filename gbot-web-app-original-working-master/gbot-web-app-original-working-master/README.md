# GBot Web Application

A comprehensive Google Workspace administration and automation platform designed to streamline the management of Google Workspace domains, users, and administrative tasks through a web-based interface.

## 🚀 Features

- **Google Workspace Domain Management**: Add, remove, and manage domain aliases
- **User Account Management**: Create and manage GSuite user accounts
- **Bulk Operations**: Handle large-scale domain and user changes
- **OAuth 2.0 Integration**: Secure Google API authentication
- **Multi-Account Support**: Manage multiple Google Workspace accounts
- **Role-Based Access Control**: Admin and support user roles
- **IP Whitelisting**: Enhanced security with IP-based access control

## 📋 System Requirements

- **Operating System**: Ubuntu 22.04 LTS or Ubuntu 24.04 LTS
- **Python**: Python 3.10 or higher
- **Memory**: Minimum 1GB RAM (2GB recommended)
- **Disk Space**: Minimum 2GB free space
- **Database**: PostgreSQL 14+
- **Internet**: Required for package installation and Google API access

## 🛠️ Installation

### Option 1: Complete Automated Installation (Recommended)

The `install_automation.sh` script handles EVERYTHING automatically:
- System updates and security configuration
- Python 3.10+, PostgreSQL, Nginx, Docker, Node.js
- Chrome/ChromeDriver for Selenium automation
- AWS CLI for cloud operations
- Virtual environment and all dependencies
- **Generates fresh SECRET_KEY, WHITELIST_TOKEN, and DATABASE_URL**
- Systemd service configuration
- Firewall and log rotation setup

#### Quick Start
```bash
# Clone directly into /opt/gbot-web-app (IMPORTANT: specify the directory name!)
cd /opt
sudo git clone https://github.com/Jetalp54/gbot-web-app-original-working.git gbot-web-app

# Go into the cloned directory
cd gbot-web-app

# Make script executable and run installation
sudo chmod +x install_automation.sh
sudo ./install_automation.sh
```

> **⚠️ Important**: You MUST clone with the directory name `gbot-web-app` at the end.
> If you just run `git clone <url>` without specifying the directory, it will create
> `/opt/gbot-web-app-original-working` which is NOT where the app runs.

The script will:
1. Install all system dependencies
2. Create PostgreSQL database with secure password
3. **Generate fresh `.env` file with all secrets** (replaces any existing `.env`)
4. Set up Nginx reverse proxy
5. Configure systemd service
6. **Display all credentials at the end (SAVE THESE!)**

#### After Installation
The script displays important information at the end:
- **SECRET_KEY**: Auto-generated, used for session security
- **WHITELIST_TOKEN**: For emergency access and API authentication
- **DATABASE_URL**: PostgreSQL connection string
- **Emergency Access URL**: `http://YOUR_IP/emergency_access?key=YOUR_TOKEN`

#### Service Commands
- Access: `http://YOUR_SERVER_IP`
- Check status: `sudo systemctl status gbot`
- View logs: `sudo journalctl -u gbot -f`
- Restart: `sudo systemctl restart gbot`

> **📝 Note**: The `.env` file is auto-generated and NOT committed to git.
> See `.env.example` for the template with all available configuration options.

### Option 2: Python Installer Only (Alternative)

```bash
# Check prerequisites
python3 install.py --check

# Install application
python3 install.py

# Validate installation
python3 install.py --validate

# Force reinstall
python3 install.py --reinstall
```

### Option 3: Manual Installation

#### 1. Install System Dependencies
```bash
sudo apt-get update
sudo apt-get install -y python3-pip python3-dev python3-venv build-essential libssl-dev libffi-dev
```

#### 2. Create Virtual Environment
```bash
python3 -m venv venv
source venv/bin/activate
```

#### 3. Install Python Dependencies
```bash
pip install --upgrade pip
pip install -r requirements.txt
```

#### 4. Setup Environment
```bash
# Generate secure keys
SECRET_KEY=$(openssl rand -hex 32)
WHITELIST_TOKEN=$(openssl rand -hex 16)

# Create .env file
cat > .env << EOF
SECRET_KEY=$SECRET_KEY
WHITELIST_TOKEN=$WHITELIST_TOKEN
DATABASE_URL=sqlite:///$(pwd)/gbot.db
DEBUG=True
FLASK_ENV=development
EOF
```

#### 5. Initialize Database
```bash
python3 -c "
from app import app, db
with app.app_context():
    db.create_all()
    print('Database initialized successfully')
"
```

## 🔧 Configuration

### Environment Variables

Create a `.env` file in the project root:

```bash
# Security
SECRET_KEY=your_secret_key_here
WHITELIST_TOKEN=your_whitelist_token_here

# Database
DATABASE_URL=sqlite:///path/to/gbot.db

# Google API (optional)
GOOGLE_CLIENT_ID=your_client_id
GOOGLE_CLIENT_SECRET=your_client_secret

# Application Settings
DEBUG=True
FLASK_ENV=development
```

### Google API Setup

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project or select existing one
3. Enable Google Admin SDK API
4. Create OAuth 2.0 credentials
5. Add your domain to authorized redirect URIs
6. Copy Client ID and Client Secret to your `.env` file

## 🚀 Running the Application

### Development Mode
```bash
# Activate virtual environment
source venv/bin/activate

# Run Flask development server
python3 app.py
```

### Production Mode
```bash
# Start systemd service
sudo systemctl start gbot
sudo systemctl enable gbot

# Check status
sudo systemctl status gbot

# View logs
sudo journalctl -u gbot -f
```

## 🌐 Accessing the Application

- **URL**: http://localhost:5000 (development) or http://your-domain (production)
- **Default Admin Credentials**:
  - Username: `admin`
  - Password: `A9B3nX#Q8k$mZ6vw`

## 📊 Usage

### 1. Account Management
- Add Google Workspace accounts with OAuth credentials
- Manage multiple accounts from single interface
- Authenticate accounts using OAuth 2.0 flow

### 2. User Operations
- Create new GSuite user accounts
- Bulk user management operations
- User lifecycle management

### 3. Domain Management
- Add/remove domain aliases
- Bulk domain operations
- Domain information retrieval

### 4. Security Features
- IP address whitelisting
- Role-based access control
- Secure session management

## 🔍 Troubleshooting

### Common Issues

**1. Virtual Environment Issues**
```bash
# Remove corrupted venv
rm -rf venv

# Recreate virtual environment
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

**2. Database Issues**
```bash
# Remove existing database
rm -f gbot.db

# Reinitialize database
python3 -c "
from app import app, db
with app.app_context():
    db.create_all()
"
```

**3. Permission Issues**
```bash
# Fix file permissions
sudo chown -R $USER:$USER .
chmod +x *.sh *.py
```

**4. Service Issues**
```bash
# Check service status
sudo systemctl status gbot

# Restart service
sudo systemctl restart gbot

# View service logs
sudo journalctl -u gbot -f
```

### Log Files

- **Installation Log**: `install.log`
- **Application Log**: Check Flask logs or systemd journal
- **Setup Log**: `setup.log` (if using enhanced setup script)

### Validation Commands

```bash
# Check installation health
python3 install.py --validate

# Check system prerequisites
python3 install.py --check

# Validate with enhanced script
./setup_enhanced.sh --validate
```

## 🏗️ Architecture

### Components
- **Flask Web Application**: Main web framework
- **SQLAlchemy ORM**: Database abstraction layer
- **Google Admin SDK**: Google Workspace management
- **OAuth 2.0**: Secure authentication
- **SQLite/PostgreSQL**: Database storage

### File Structure
```
gbot-web-app/
├── app.py                 # Main Flask application
├── core_logic.py          # Google API integration
├── database.py            # Database models
├── config.py              # Configuration settings
├── install.py             # Python installer
├── setup_enhanced.sh      # Enhanced setup script
├── requirements.txt       # Python dependencies
├── .env                   # Environment variables
├── static/                # Static assets
├── templates/             # HTML templates
└── venv/                  # Python virtual environment
```

## 🔒 Security Considerations

- **IP Whitelisting**: Restrict access to specific IP addresses
- **Role-Based Access**: Different permission levels for users
- **Secure Keys**: Automatically generated secure keys
- **OAuth 2.0**: Industry-standard authentication
- **Session Management**: Secure user sessions

## 📚 API Endpoints

### Authentication
- `POST /api/authenticate` - Authenticate Google account
- `POST /api/complete-oauth` - Complete OAuth flow

### User Management
- `POST /api/add-user` - Add system user
- `GET /api/list-users` - List system users
- `POST /api/edit-user` - Edit user
- `POST /api/delete-user` - Delete user

### Google Workspace
- `POST /api/create-gsuite-user` - Create GSuite user
- `GET /api/get-domain-info` - Get domain information
- `POST /api/add-domain-alias` - Add domain alias
- `POST /api/delete-domain` - Delete domain

### IP Whitelist
- `POST /api/add-whitelist-ip` - Add IP to whitelist
- `GET /api/list-whitelist-ips` - List whitelisted IPs
- `POST /api/delete-whitelist-ip` - Remove IP from whitelist

## 🚀 Deployment

### Development
```bash
# Activate virtual environment
source venv/bin/activate

# Run development server
python3 app.py
```

### Production Deployment

#### Quick Production Setup (Recommended)
```bash
# Complete production deployment with PostgreSQL, Nginx, and all dependencies
sudo ./install_automation.sh
```

#### Production Configuration (Automatic)
- **Database**: PostgreSQL with optimized settings
- **Web Server**: Nginx with reverse proxy and rate limiting
- **Application Server**: Gunicorn with multi-threaded workers
- **Process Management**: Systemd service with auto-restart
- **Docker**: Installed for ECR image operations
- **Security**: Firewall (UFW), security headers
- **Monitoring**: Log rotation, memory monitoring service
- **Backup**: Daily automated backups at 2 AM

#### SSL Certificate Setup
```bash
# Install Certbot and get SSL certificate
sudo apt install certbot python3-certbot-nginx
sudo certbot --nginx -d yourdomain.com
```

#### Production Commands
```bash
# Check service status
sudo systemctl status gbot nginx postgresql

# View application logs
sudo journalctl -u gbot -f

# View Nginx logs
sudo tail -f /var/log/nginx/error.log
sudo tail -f /var/log/gbot/error.log

# Restart services
sudo systemctl restart gbot nginx

# Run backup manually
sudo /opt/gbot-web-app/backup.sh
```

### Docker (Future Enhancement)
```bash
# Build and run with Docker
docker build -t gbot-web .
docker run -p 5000:5000 gbot-web
```

## 🤝 Contributing

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Test thoroughly
5. Submit a pull request

## 📄 License

This project is licensed under the MIT License - see the LICENSE file for details.

## 🆘 Support

### Getting Help
- Check the troubleshooting section above
- Review log files for error details
- Validate installation with provided tools
- Check system requirements

### Reporting Issues
- Include system information (Ubuntu version, Python version)
- Provide error messages and log files
- Describe steps to reproduce the issue

## 🔄 Updates

### Updating the Application
```bash
# Pull latest changes
git pull origin main

# Update dependencies
source venv/bin/activate
pip install -r requirements.txt

# Restart services
sudo systemctl restart gbot
```

### Updating System Dependencies
```bash
sudo apt-get update
sudo apt-get upgrade
```

---

**Note**: This application is designed specifically for Ubuntu/Linux systems. Windows support is not provided.
