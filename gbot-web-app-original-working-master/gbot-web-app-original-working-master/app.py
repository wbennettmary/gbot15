import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import json
import logging
import random
import string
import csv
import io
import smtplib
import tempfile
import time
import sqlite3
import traceback
from sqlalchemy import text
from werkzeug.security import generate_password_hash, check_password_hash
import logging.handlers
import threading
import uuid
import paramiko
import pyotp
import re

from flask import Flask, render_template, request, jsonify, session, redirect, url_for, flash
from google_auth_oauthlib.flow import InstalledAppFlow
from faker import Faker
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import re

from core_logic import google_api
from database import db, User, WhitelistedIP, UsedDomain, GoogleAccount, GoogleToken, Scope, ServerConfig, UserAppPassword, AutomationAccount, RetrievedUser, NamecheapConfig, DomainOperation, AwsConfig, ServiceAccount, CloudflareConfig, Notification, WorkspaceList
from routes.dns_manager import dns_manager
from routes.aws_manager import aws_manager

# Progress tracking system for domain changes
# Progress tracking system for domain changes
# Using file-based storage for multi-worker support to avoid "Task not found"
PROGRESS_DIR = os.path.join(tempfile.gettempdir(), 'gbot_progress')
if not os.path.exists(PROGRESS_DIR):
    try:
        os.makedirs(PROGRESS_DIR)
    except:
        pass

def get_task_file(task_id):
    # Sanitize task_id just in case
    safe_id = "".join(x for x in str(task_id) if x.isalnum() or x in "-_")
    return os.path.join(PROGRESS_DIR, f'task_{safe_id}.json')

def update_progress(task_id, current, total, status="processing", message="", result_data=None):
    """Update progress for a task (File-based)"""
    try:
        progress_data = {
            'task_id': task_id,
            'current': current,
            'total': total,
            'status': status,  # processing, completed, error
            'message': message,
            'percentage': int((current / total) * 100) if total > 0 else 0,
            'timestamp': datetime.now().isoformat(),
            'result_data': result_data
        }
        
        file_path = get_task_file(task_id)
        # Atomic write (write temp then rename)
        temp_path = file_path + f".tmp_{uuid.uuid4()}"
        with open(temp_path, 'w') as f:
            json.dump(progress_data, f)
        os.replace(temp_path, file_path)
            
        # Log significant updates
        if status in ['completed', 'error'] or (total > 0 and current % max(1, total // 5) == 0):
             logging.info(f"=== PROGRESS {task_id}: {status} - {message} ({current}/{total}) ===")
             
    except Exception as e:
        logging.error(f"Failed to update progress file for {task_id}: {e}")

def get_progress(task_id):
    """Get current progress for a task"""
    try:
        file_path = get_task_file(task_id)
        if os.path.exists(file_path):
            with open(file_path, 'r') as f:
                return json.load(f)
    except Exception as e:
        logging.error(f"Failed to read progress file for {task_id}: {e}")
        
    return {
        'current': 0, 'total': 0, 
        'status': 'not_found', 
        'message': 'Task not found', 
        'percentage': 0, 
        'result_data': None
    }

def clear_progress(task_id):
    """Clear progress file"""
    try:
        file_path = get_task_file(task_id)
        if os.path.exists(file_path):
            os.remove(file_path)
    except:
        pass

def cleanup_old_progress():
    """Clean up old progress files"""
    try:
        if not os.path.exists(PROGRESS_DIR):
            return
            
        current_time = datetime.now()
        for filename in os.listdir(PROGRESS_DIR):
            if not filename.endswith('.json'):
                continue
                
            file_path = os.path.join(PROGRESS_DIR, filename)
            try:
                mtime = datetime.fromtimestamp(os.path.getmtime(file_path))
                age_minutes = (current_time - mtime).total_seconds() / 60
                
                # Cleanup > 24 hours
                if age_minutes > 1440:
                    os.remove(file_path)
                    logging.info(f"Cleaned up old task file: {filename}")
            except:
                pass
    except Exception as e:
        logging.error(f"Cleanup failed: {e}")

app = Flask(__name__)
app.config.from_object('config')

# Set secret key for sessions
if app.config.get('SECRET_KEY'):
    app.secret_key = app.config['SECRET_KEY']
else:
    app.secret_key = 'fallback-secret-key-for-development'

# Rate limiting configuration (temporarily disabled to fix startup issue)
# TODO: Re-enable after fixing flask_limiter installation

def rate_limit(limit_str):
    """No-op decorator - rate limiting temporarily disabled"""
    def decorator(f):
        return f
    return decorator

RATE_LIMITING_ENABLED = False

# Configure session settings
app.config['SESSION_COOKIE_SECURE'] = False  # Allow HTTP
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['PERMANENT_SESSION_LIFETIME'] = 86400  # 24 hours

# Configure file upload settings
# app.config['MAX_CONTENT_LENGTH'] = 1024 * 1024 * 1024  # File size limit removed
app.config['UPLOAD_FOLDER'] = 'backups'
# Allow large .txt uploads for massive app-password lists (e.g., up to ~100MB)
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024  # 100 MB

db.init_app(app)

# Register blueprints
app.register_blueprint(dns_manager)
app.register_blueprint(aws_manager)

# Global concurrency limiter - REMOVED for unlimited concurrent machines
# No artificial limits - let the server handle as many requests as possible
MAX_CONCURRENT_JOBS = 999999  # Effectively unlimited
job_semaphore = None  # Disabled - no semaphore blocking

# Lightweight OTP secret cache to avoid repeated SSH calls
OTP_SECRET_CACHE = {}
OTP_SECRET_TTL_SECONDS = int(os.environ.get('OTP_SECRET_TTL_SECONDS', '600'))  # 10 minutes default

def get_cached_otp_secret(account_name: str):
    entry = OTP_SECRET_CACHE.get(account_name)
    if not entry:
        return None
    secret, ts = entry
    if (time.time() - ts) <= OTP_SECRET_TTL_SECONDS:
        return secret
    # expired
    OTP_SECRET_CACHE.pop(account_name, None)
    return None

def set_cached_otp_secret(account_name: str, secret: str):
    OTP_SECRET_CACHE[account_name] = (secret, time.time())

# Production logging configuration
if not app.debug:
    # Create logs directory if it doesn't exist
    if not os.path.exists('logs'):
        os.makedirs('logs')
    
    # Configure file handler for production
    file_handler = logging.handlers.RotatingFileHandler(
        'logs/gbot.log', maxBytes=10240000, backupCount=10
    )
    file_handler.setFormatter(logging.Formatter(
        '%(asctime)s %(levelname)s: %(message)s [in %(pathname)s:%(lineno)d]'
    ))
    file_handler.setLevel(logging.INFO)
    app.logger.addHandler(file_handler)
    
    app.logger.setLevel(logging.INFO)
    app.logger.info('GBot startup')

with app.app_context():
    db.create_all()
    
    # Auto-migration: Add ever_used column if it doesn't exist
    # Auto-migration: Add ever_used column if it doesn't exist
    try:
        from sqlalchemy import inspect
        inspector = inspect(db.engine)
        columns = [col['name'] for col in inspector.get_columns('used_domain')]
        
        if 'ever_used' not in columns:
            logging.info("Adding missing 'ever_used' column to used_domain table...")
            with db.engine.connect() as conn:
                if 'postgresql' in str(db.engine.url):
                    # Postgres requires properly cast default or boolean literal
                    conn.execute(text('ALTER TABLE "used_domain" ADD COLUMN ever_used BOOLEAN DEFAULT FALSE'))
                    conn.execute(text('UPDATE "used_domain" SET ever_used = TRUE WHERE user_count > 0'))
                else:
                    # SQLite: Using integer 0/1 is safer for older versions, though FALSE/TRUE often works
                    conn.execute(text("ALTER TABLE used_domain ADD COLUMN ever_used BOOLEAN DEFAULT 0"))
                    conn.execute(text("UPDATE used_domain SET ever_used = 1 WHERE user_count > 0"))
                conn.commit()
            logging.info("✅ Successfully added 'ever_used' column!")
        else:
            logging.debug("Column 'ever_used' already exists")
            
        # Enable WAL mode for SQLite to improve concurrency
        if 'sqlite' in str(db.engine.url):
            try:
                # db.session.execute(text("PRAGMA journal_mode=WAL"))
                # Execution of PRAGMA via session might not work for all drivers, 
                # but standard SQLAlchemy execution for SQLite usually works.
                # using engine.connect() is safer for PRAGMAs
                with db.engine.connect() as con:
                    con.execute(text("PRAGMA journal_mode=WAL"))
                logging.info("✅ Enabled SQLite Write-Ahead Logging (WAL) mode")
            except Exception as e:
                logging.warning(f"⚠️ Could not enable SQLite WAL mode: {e}")
                
    except Exception as e:
        logging.warning(f"Could not auto-migrate ever_used column: {e}")
        try:
            db.session.rollback()
        except:
            pass

    # Auto-migration: Add last_login column if it doesn't exist
    try:
        from sqlalchemy import inspect
        inspector = inspect(db.engine)
        columns = [col['name'] for col in inspector.get_columns('user')]
        
        if 'last_login' not in columns:
            logging.info("Adding missing 'last_login' column to user table...")
            # Add the column
            with db.engine.connect() as conn:
                if 'postgresql' in str(db.engine.url):
                    # Postgres requires quoted table name "user" because it is a keyword
                    conn.execute(text('ALTER TABLE "user" ADD COLUMN last_login TIMESTAMP'))
                else:
                    # SQLite is more lenient but quoting is still good practice
                    conn.execute(text("ALTER TABLE user ADD COLUMN last_login TIMESTAMP"))
                conn.commit()
            logging.info("✅ Successfully added 'last_login' column!")
        else:
            logging.debug("Column 'last_login' already exists")
    except Exception as e:
        logging.warning(f"Could not auto-migrate last_login column: {e}")
        try:
            db.session.rollback()
        except:
            pass

    # Auto-migration: Add created_at column to whitelisted_ip if it doesn't exist
    try:
        from sqlalchemy import inspect
        inspector = inspect(db.engine)
        columns = [col['name'] for col in inspector.get_columns('whitelisted_ip')]
        
        if 'created_at' not in columns:
            logging.info("Adding missing 'created_at' column to whitelisted_ip table...")
            with db.engine.connect() as conn:
                if 'postgresql' in str(db.engine.url):
                    conn.execute(text('ALTER TABLE "whitelisted_ip" ADD COLUMN created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP'))
                else:
                    conn.execute(text("ALTER TABLE whitelisted_ip ADD COLUMN created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP"))
                conn.commit()
            logging.info("✅ Successfully added 'created_at' column to whitelisted_ip!")
        else:
            logging.debug("Column 'created_at' already exists in whitelisted_ip")
    except Exception as e:
        logging.warning(f"Could not auto-migrate created_at column for whitelisted_ip: {e}")
        try:
            db.session.rollback()
        except:
            pass
    if not User.query.filter_by(username='admin').first():
        admin_user = User(username='admin', password=generate_password_hash('A9B3nX#Q8k$mZ6vw', method='pbkdf2:sha256'), role='admin')
        db.session.add(admin_user)
        db.session.commit()

def login_required(f):
    def wrapper(*args, **kwargs):
        if 'user' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    wrapper.__name__ = f.__name__
    return wrapper

# Role-based permissions configuration
ROLE_PERMISSIONS = {
    'admin': ['dashboard', 'aws_management', 'settings', 'users', 'whitelist', 'list_management'],
    'mailer': ['dashboard', 'aws_management', 'settings', 'list_management'],
    'support': ['dashboard', 'list_management']
}

def permission_required(permission):
    """Decorator to check if user has permission for a page/feature"""
    def decorator(f):
        def wrapper(*args, **kwargs):
            user_id = session.get('user_id')
            if not user_id:
                return redirect(url_for('login'))
            user = User.query.get(user_id)
            if not user:
                return redirect(url_for('login'))
            user_permissions = ROLE_PERMISSIONS.get(user.role, [])
            if permission not in user_permissions:
                flash('Access denied: insufficient permissions', 'danger')
                return redirect(url_for('dashboard'))
            return f(*args, **kwargs)
        wrapper.__name__ = f.__name__
        return wrapper
    return decorator

# Generic Task Progress Endpoint
@app.route('/api/task-progress/<task_id>', methods=['GET'])
@login_required
def api_get_task_progress(task_id):
    """Generic endpoint to poll progress of background tasks."""
    progress = get_progress(task_id)
    return jsonify(progress)

@app.context_processor
def inject_user_info():
    """Inject user role and permissions into all templates"""
    # Emergency access gets all permissions
    if session.get('emergency_access'):
        return {
            'user_role': 'admin',
            'user_permissions': ROLE_PERMISSIONS.get('admin', [])
        }
    
    user_id = session.get('user_id')
    if user_id:
        user = User.query.get(user_id)
        if user:
            return {
                'user_role': user.role,
                'user_permissions': ROLE_PERMISSIONS.get(user.role, [])
            }
    return {'user_role': None, 'user_permissions': []}

@app.before_request
def before_request():
    # Debug logging
    app.logger.debug(f"Before request: endpoint={request.endpoint}, user={session.get('user')}, emergency_access={session.get('emergency_access')}, client_ip={get_client_ip()}")
    
    # Always allow these critical routes without any checks (truly exempt)
    # These are required for emergency access and system health
    if request.endpoint in ['static', 'emergency_access', 'api_emergency_add_ip', 'api_debug_config', 'api_debug_whitelist', 'health_check', 'test-admin']:
        app.logger.debug(f"Allowing {request.endpoint} route without restrictions")
        return

    # Allow emergency access users to access all endpoints
    if session.get('emergency_access'):
        app.logger.debug(f"Allowing emergency access user to access {request.endpoint}")
        return

    # ================================================================
    # IP WHITELIST CHECK - MUST HAPPEN BEFORE LOGIN IS ALLOWED
    # This is the FIRST security gate - if IP is not whitelisted, DENY ALL
    # ================================================================
    if app.config.get('ENABLE_IP_WHITELIST', False):  # Default to False to prevent lockout
        client_ip = get_client_ip()
        
        # Normalize IP (strip whitespace, lowercase for IPv6)
        client_ip_normalized = client_ip.strip().lower() if client_ip else ''
        
        app.logger.info(f"Checking IP whitelist for '{client_ip_normalized}' accessing {request.endpoint}")
        
        # Get all whitelisted IPs for comparison
        all_whitelisted = WhitelistedIP.query.all()
        whitelisted_ips = [ip.ip_address.strip().lower() for ip in all_whitelisted]
        app.logger.info(f"Whitelisted IPs in database: {whitelisted_ips}")
        
        # Check if IP is whitelisted (case-insensitive, stripped)
        whitelisted_ip = None
        for wip in all_whitelisted:
            if wip.ip_address.strip().lower() == client_ip_normalized:
                whitelisted_ip = wip
                break
        
        if not whitelisted_ip:
            app.logger.warning(f"IP '{client_ip}' (normalized: '{client_ip_normalized}') not whitelisted, access denied to {request.endpoint}")
            # Block EVERYTHING including login if IP is not whitelisted
            return f"Access denied. IP {client_ip} is not whitelisted. Please contact administrator or use <a href='/emergency_access?key=YOUR_WHITELIST_TOKEN'>emergency access</a>.", 403
        else:
            app.logger.info(f"IP '{client_ip_normalized}' is whitelisted, allowing access")
    else:
        app.logger.debug("IP whitelist disabled, allowing access")

    # ================================================================
    # AFTER IP CHECK PASSES - Allow login route
    # ================================================================
    if request.endpoint == 'login':
        app.logger.debug("Allowing login route after IP check passed")
        return

    # ================================================================
    # ROLE-BASED ACCESS CONTROL for logged-in users
    # ================================================================
    if session.get('user'):
        # Restricted Routes Check
        if request.path == '/settings':
            user_role = session.get('role')
            allowed_perms = ROLE_PERMISSIONS.get(user_role, [])
            
            if 'settings' not in allowed_perms:
                app.logger.warning(f"Access denied to /settings for user {session.get('user')} (role: {user_role})")
                flash("Access denied: You do not have permission to view Settings.", "danger")
                return redirect(url_for('dashboard'))

        app.logger.debug(f"Allowing logged-in user {session.get('user')} to access {request.endpoint}")
        return

@app.after_request
def add_security_headers(response):
    """Add security headers to all responses"""
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'SAMEORIGIN'
    response.headers['X-XSS-Protection'] = '1; mode=block'
    response.headers['Strict-Transport-Security'] = 'max-age=31536000; includeSubDomains'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    return response

def get_client_ip():
    if request.headers.get('X-Forwarded-For'):
        return request.headers.get('X-Forwarded-For').split(',')[0].strip()
    return request.remote_addr

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        
        # Debug logging
        app.logger.info(f"Login attempt for username: {username}")
        
        user = User.query.filter_by(username=username).first()
        
        if user:
            app.logger.info(f"User found: {user.username}, role: {user.role}")
            if check_password_hash(user.password, password):
                app.logger.info(f"Password verified for user: {username}")
                session['user'] = user.username
                session['user_id'] = user.id
                session['role'] = user.role.lower()
                session.permanent = True  # Make session persistent
                
                # Update last login time
                try:
                    user.last_login = datetime.now()
                    db.session.commit()
                except Exception as e:
                    app.logger.error(f"Failed to update last_login for {username}: {e}")
                
                # Create login notification
                try:
                    client_ip = get_client_ip()
                    notification = Notification(
                        type='login',
                        title=f'{user.username} logged in',
                        message=f'{user.username} ({user.role}) logged in from {client_ip}',
                        icon='fa-sign-in-alt',
                        user_id=user.id
                    )
                    db.session.add(notification)
                    db.session.commit()
                    app.logger.info(f"Login notification created for {username}")
                except Exception as e:
                    app.logger.error(f"Failed to create login notification: {e}")
                    
                app.logger.info(f"Session set - user: {session.get('user')}, role: {session.get('role')}")
                flash(f'Welcome {user.username}!', 'success')
                return redirect(url_for('dashboard'))
            else:
                app.logger.warning(f"Invalid password for user: {username}")
                flash('Invalid credentials', 'error')
        else:
            app.logger.warning(f"User not found: {username}")
            flash('Invalid credentials', 'error')
    
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    flash('Logged out successfully', 'success')
    return redirect(url_for('login'))

@app.route('/test-admin')
def test_admin():
    """Test route to check admin user and authentication"""
    try:
        admin_user = User.query.filter_by(username='admin').first()
        if admin_user:
            from werkzeug.security import check_password_hash
            password_works = check_password_hash(admin_user.password, 'A9B3nX#Q8k$mZ6vw')
            return jsonify({
                'admin_exists': True,
                'username': admin_user.username,
                'role': admin_user.role,
                'password_works': password_works,
                'session_user': session.get('user'),
                'session_role': session.get('role')
            })
        else:
            return jsonify({'admin_exists': False})
    except Exception as e:
        return jsonify({'error': str(e)})

@app.route('/whitelist-bypass')
def whitelist_bypass():
    """Temporary bypass route for whitelist management"""
    # Set emergency access session
    session['emergency_access'] = True
    session['role'] = 'admin'
    session['user'] = 'emergency_admin'
    session.permanent = True
    
    flash('Emergency access granted for whitelist management', 'success')
    return redirect(url_for('whitelist'))

@app.route('/health')
def health_check():
    """Health check endpoint for monitoring"""
    try:
        # Test database connection
        db.session.execute('SELECT 1')
        db_status = 'healthy'
    except Exception as e:
        db_status = f'unhealthy: {str(e)}'
    
    return jsonify({
        'status': 'healthy',
        'timestamp': datetime.now().isoformat(),
        'database': db_status,
        'version': '1.0.0'
    })

@app.route('/')
@app.route('/dashboard')
@login_required
def dashboard():
    # Pass minimal data to dashboard, accounts will be loaded via API
    return render_template('dashboard.html', accounts=[], user=session.get('user'), role=session.get('role'))

@app.route('/api/search-accounts')
@login_required
def search_accounts():
    query = request.args.get('q', '').lower().strip()
    limit = int(request.args.get('limit', 20))
    
    # Base queries
    oauth_query = GoogleAccount.query
    service_query = ServiceAccount.query.filter_by(is_active=True)
    
    if query:
        oauth_query = oauth_query.filter(GoogleAccount.account_name.ilike(f'%{query}%'))
        service_query = service_query.filter(ServiceAccount.name.ilike(f'%{query}%'))
    
    # Fetch limited results
    oauth_accounts = oauth_query.limit(limit).all()
    # Adjust limit for second query to avoid over-fetching if first filled it up
    remaining_limit = max(0, limit - len(oauth_accounts))
    service_accounts = service_query.limit(remaining_limit).all()
    
    results = []
    
    for acc in oauth_accounts:
        results.append({
            'id': acc.id,
            'account_name': acc.account_name,
            'type': 'oauth'
        })
        
    for acc in service_accounts:
        results.append({
            'id': acc.id,
            'account_name': acc.name,
            'type': 'service_account'
        })
        
    return jsonify({
        'success': True,
        'accounts': results,
        'count': len(results)
    })

@app.route('/api/list-users')
def list_users():
    """API to list all users - admin only"""
    if not session.get('user') or session.get('role') != 'admin':
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403
        
    try:
        users = User.query.all()
        user_list = []
        
        now = datetime.now()
        
        for u in users:
            # Format last login
            last_seen = "Never"
            status = "inactive"
            
            if u.last_login:
                diff = now - u.last_login
                seconds = diff.total_seconds()
                
                if seconds < 60:
                    last_seen = "Just now"
                    status = "active"
                elif seconds < 3600:
                    mins = int(seconds / 60)
                    last_seen = f"{mins}m ago"
                    status = "active"
                elif seconds < 86400:
                    hours = int(seconds / 3600)
                    last_seen = f"{hours}h ago"
                    status = "active" if hours < 24 else "inactive"
                elif seconds < 604800: # 7 days
                    days = int(seconds / 86400)
                    last_seen = f"{days}d ago"
                    status = "inactive"
                else:
                    last_seen = u.last_login.strftime('%b %d, %Y')
                    status = "inactive"
            
            user_list.append({
                'username': u.username,
                'role': u.role,
                'last_login': last_seen,
                'status': status,
                'raw_last_login': u.last_login.isoformat() if u.last_login else None
            })
            
        return jsonify({'success': True, 'users': user_list})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/users')
def users():
    """Users management page - requires admin or emergency access"""
    # Emergency access gets full access
    if session.get('emergency_access'):
        return render_template('users.html', user='emergency', role='admin')
    
    # Check if logged in
    if not session.get('user'):
        flash("Access denied. Please log in.", "danger")
        return redirect(url_for('login'))
    
    # Only admin can access users page
    if session.get('role') != 'admin':
        flash("Access denied: Admin privileges required", "danger")
        return redirect(url_for('dashboard'))
    
    return render_template('users.html', user=session.get('user'), role=session.get('role'))

@app.route('/emergency_access')
def emergency_access():
    """Emergency access route that bypasses IP whitelist for initial setup"""
    # Check for static key in URL parameters
    static_key = request.args.get('key', '')
    whitelist_token = app.config.get('WHITELIST_TOKEN', '')
    secret_key = app.config.get('SECRET_KEY', '')
    
    # Debug logging
    app.logger.info(f"Emergency access route - Key provided: {static_key[:8] if static_key else 'None'}..., WHITELIST_TOKEN: {whitelist_token[:8] if whitelist_token else 'None'}..., SECRET_KEY: {secret_key[:8] if secret_key else 'None'}...")
    
    # If WHITELIST_TOKEN is provided directly, auto-whitelist the current IP
    if static_key == whitelist_token:
        client_ip = get_client_ip()
        app.logger.info(f"WHITELIST_TOKEN provided - auto-whitelisting IP: {client_ip}")
        
        # Check if IP already exists
        existing_ip = WhitelistedIP.query.filter_by(ip_address=client_ip).first()
        if not existing_ip:
            try:
                new_ip = WhitelistedIP(ip_address=client_ip)
                db.session.add(new_ip)
                db.session.commit()
                app.logger.info(f"IP {client_ip} auto-whitelisted successfully")
                flash(f'IP {client_ip} has been automatically whitelisted!', 'success')
            except Exception as db_error:
                db.session.rollback()
                app.logger.error(f"Database error auto-whitelisting IP {client_ip}: {db_error}")
                
                # If it's a unique constraint violation, the IP might already exist
                if "duplicate key value violates unique constraint" in str(db_error):
                    # Check again if it was added by another process
                    existing_ip = WhitelistedIP.query.filter_by(ip_address=client_ip).first()
                    if existing_ip:
                        app.logger.info(f"IP {client_ip} was already whitelisted")
                        flash(f'IP {client_ip} is already whitelisted!', 'info')
                    else:
                        flash(f'Database error: {str(db_error)}', 'error')
                else:
                    flash(f'Database error: {str(db_error)}', 'error')
        else:
            app.logger.info(f"IP {client_ip} already whitelisted")
            flash(f'IP {client_ip} is already whitelisted!', 'info')
        
        # Set session and redirect to whitelist management
        session['emergency_access'] = True
        session['role'] = 'admin'
        session['user'] = 'emergency_admin'
        return redirect(url_for('whitelist'))
    
    # If SECRET_KEY is provided, show the emergency access form
    elif static_key == secret_key:
        app.logger.info("SECRET_KEY provided - showing emergency access form")
        return render_template('emergency_access.html')
    
    # If no valid key, show the emergency access form
    else:
        app.logger.info("No valid key provided - showing emergency access form")
        return render_template('emergency_access.html')

@app.route('/api/emergency-add-ip', methods=['POST'])
def api_emergency_add_ip():
    """Emergency API to add IP to whitelist without authentication"""
    try:
        data = request.get_json()
        ip_address = data.get('ip_address', '').strip()
        emergency_key = data.get('emergency_key', '').strip()
        
        # Debug logging
        app.logger.info(f"Emergency access attempt - IP: {ip_address}, Key provided: {emergency_key[:8]}...")
        
        if not ip_address:
            return jsonify({'success': False, 'error': 'IP address required'})
        
        if not emergency_key:
            return jsonify({'success': False, 'error': 'Emergency key required'})
        
        # Check against both WHITELIST_TOKEN and SECRET_KEY
        whitelist_token = app.config.get('WHITELIST_TOKEN', '')
        secret_key = app.config.get('SECRET_KEY', '')
        
        if not whitelist_token and not secret_key:
            return jsonify({'success': False, 'error': 'No emergency keys configured'})
        
        # Accept either key
        if emergency_key != whitelist_token and emergency_key != secret_key:
            return jsonify({'success': False, 'error': 'Invalid emergency key. Please use your WHITELIST_TOKEN or SECRET_KEY.'})
        
        # Check if IP already exists
        existing_ip = WhitelistedIP.query.filter_by(ip_address=ip_address).first()
        if existing_ip:
            app.logger.info(f"IP {ip_address} already exists in whitelist")
            return jsonify({'success': True, 'message': f'IP address {ip_address} is already whitelisted'})
        
        # Add new IP to whitelist with error handling
        try:
            new_ip = WhitelistedIP(ip_address=ip_address)
            db.session.add(new_ip)
            db.session.commit()
            app.logger.info(f"IP {ip_address} successfully added to whitelist")
        except Exception as db_error:
            db.session.rollback()
            app.logger.error(f"Database error adding IP {ip_address}: {db_error}")
            
            # If it's a unique constraint violation, the IP might already exist
            if "duplicate key value violates unique constraint" in str(db_error):
                # Check again if it was added by another process
                existing_ip = WhitelistedIP.query.filter_by(ip_address=ip_address).first()
                if existing_ip:
                    app.logger.info(f"IP {ip_address} was added by another process")
                    return jsonify({'success': True, 'message': f'IP address {ip_address} is already whitelisted'})
                else:
                    return jsonify({'success': False, 'error': f'Database constraint violation: {str(db_error)}'})
            else:
                return jsonify({'success': False, 'error': f'Database error: {str(db_error)}'})
        
        app.logger.info(f"IP {ip_address} successfully whitelisted via emergency access")
        
        # Set session for this user so they can access other pages
        session['emergency_access'] = True
        session['role'] = 'admin'
        session['user'] = 'emergency_admin'
        
        return jsonify({'success': True, 'message': f'IP address {ip_address} whitelisted successfully'})
        
    except Exception as e:
        app.logger.error(f"Emergency access error: {str(e)}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/debug-config')
def api_debug_config():
    """Debug endpoint to check configuration values"""
    return jsonify({
        'WHITELIST_TOKEN': app.config.get('WHITELIST_TOKEN', '')[:8] + '...' if app.config.get('WHITELIST_TOKEN') else 'None',
        'SECRET_KEY': app.config.get('SECRET_KEY', '')[:8] + '...' if app.config.get('SECRET_KEY') else 'None',
        'ENABLE_IP_WHITELIST': app.config.get('ENABLE_IP_WHITELIST', False),
        'DEBUG': app.config.get('DEBUG', False),
        'note': 'Both WHITELIST_TOKEN and SECRET_KEY can be used for emergency access'
    })

@app.route('/api/debug-session')
def api_debug_session():
    """Debug endpoint to check current session state"""
    return jsonify({
        'session_data': dict(session),
        'client_ip': get_client_ip(),
        'endpoint': request.endpoint if request.endpoint else 'None'
    })

@app.route('/api/debug-whitelist')
def api_debug_whitelist():
    """Debug endpoint to check whitelist status"""
    try:
        client_ip = get_client_ip()
        whitelisted_ips = WhitelistedIP.query.all()
        ip_list = [ip.ip_address for ip in whitelisted_ips]
        
        return jsonify({
            'client_ip': client_ip,
            'whitelisted_ips': ip_list,
            'is_whitelisted': client_ip in ip_list,
            'total_whitelisted': len(ip_list),
            'enable_ip_whitelist_config': app.config.get('ENABLE_IP_WHITELIST', False),
            'app_debug_mode': app.debug,
            'emergency_access_session': session.get('emergency_access', False),
            'session_data': {
                'user': session.get('user'),
                'role': session.get('role'),
                'emergency_access': session.get('emergency_access')
            }
        })
    except Exception as e:
        app.logger.error(f"Error in debug-whitelist endpoint: {str(e)}")
        return jsonify({'error': str(e)})

@app.route('/whitelist')
def whitelist():
    """Whitelist management page - accessible via emergency access or admin login"""
    # Emergency access bypasses normal permissions
    if session.get('emergency_access'):
        pass  # Allow access
    elif not session.get('user'):
        flash("Access denied. Please use emergency access or log in.", "danger")
        return redirect(url_for('login'))
    else:
        # Check role-based permissions
        user_id = session.get('user_id')
        if user_id:
            user = User.query.get(user_id)
            if user:
                user_permissions = ROLE_PERMISSIONS.get(user.role, [])
                if 'whitelist' not in user_permissions:
                    flash("Access denied: insufficient permissions", "danger")
                    return redirect(url_for('dashboard'))
            else:
                return redirect(url_for('login'))
        else:
            return redirect(url_for('login'))
    
    # Get all whitelisted IPs for display
    try:
        # Pass full objects to template to access created_at
        whitelisted_ips = WhitelistedIP.query.order_by(WhitelistedIP.created_at.desc()).all()
    except Exception as e:
        app.logger.error(f"Error fetching whitelisted IPs: {e}")
        whitelisted_ips = []
    
    app.logger.info(f"Whitelist access granted: user={session.get('user')}, role={session.get('role')}, emergency_access={session.get('emergency_access')}")
    return render_template('whitelist.html', user=session.get('user'), role=session.get('role'), whitelisted_ips=whitelisted_ips)

# ============================================================================
# LIST MANAGEMENT - Workspace Account Lists with Lifecycle Tracking
# ============================================================================

@app.route('/list-management')
@login_required
def list_management():
    """List Management page for managing workspace account lists"""
    # Check permissions
    user_id = session.get('user_id')
    if user_id:
        user = User.query.get(user_id)
        if user:
            user_permissions = ROLE_PERMISSIONS.get(user.role, [])
            if 'list_management' not in user_permissions:
                flash("Access denied: insufficient permissions", "danger")
                return redirect(url_for('dashboard'))
    
    return render_template('list_management.html', user=session.get('user'), role=session.get('role'))

@app.route('/api/lists', methods=['GET'])
@login_required
def api_get_lists():
    """Get all workspace lists"""
    try:
        lists = WorkspaceList.query.order_by(WorkspaceList.created_at.desc()).all()
        return jsonify({
            'success': True,
            'lists': [lst.to_dict() for lst in lists]
        })
    except Exception as e:
        app.logger.error(f"Error fetching lists: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/lists', methods=['POST'])
@login_required
def api_create_list():
    """Create a new workspace list"""
    try:
        data = request.get_json()
        name = data.get('name', '').strip()
        raw_accounts = data.get('raw_accounts', '').strip()
        remaining_days = int(data.get('remaining_days', 14))
        
        if not name:
            return jsonify({'success': False, 'error': 'List name is required'})
        
        if not raw_accounts:
            return jsonify({'success': False, 'error': 'Account list is required'})
        
        # Calculate expiration date
        from datetime import timedelta
        lifetime_expires_at = datetime.utcnow() + timedelta(days=remaining_days)
        
        new_list = WorkspaceList(
            name=name,
            raw_accounts=raw_accounts,
            lifetime_expires_at=lifetime_expires_at
        )
        
        db.session.add(new_list)
        db.session.commit()
        
        return jsonify({
            'success': True,
            'message': f'List "{name}" created successfully',
            'list': new_list.to_dict()
        })
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Error creating list: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/lists/<int:list_id>', methods=['PUT'])
@login_required
def api_update_list(list_id):
    """Update a workspace list"""
    try:
        lst = WorkspaceList.query.get(list_id)
        if not lst:
            return jsonify({'success': False, 'error': 'List not found'}), 404
        
        data = request.get_json()
        
        if 'name' in data:
            lst.name = data['name'].strip()
        
        if 'raw_accounts' in data:
            lst.raw_accounts = data['raw_accounts'].strip()
        
        if 'remaining_days' in data:
            from datetime import timedelta
            remaining_days = int(data['remaining_days'])
            lst.lifetime_expires_at = datetime.utcnow() + timedelta(days=remaining_days)
        
        db.session.commit()
        
        return jsonify({
            'success': True,
            'message': f'List "{lst.name}" updated successfully',
            'list': lst.to_dict()
        })
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Error updating list: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/lists/<int:list_id>', methods=['DELETE'])
@login_required
def api_delete_list(list_id):
    """Delete a workspace list"""
    try:
        lst = WorkspaceList.query.get(list_id)
        if not lst:
            return jsonify({'success': False, 'error': 'List not found'}), 404
        
        name = lst.name
        db.session.delete(lst)
        db.session.commit()
        
        return jsonify({
            'success': True,
            'message': f'List "{name}" deleted successfully'
        })
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Error deleting list: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/lists/<int:list_id>/start-timer', methods=['POST'])
@login_required
def api_start_list_timer(list_id):
    """Start the 24-hour usage timer for a list"""
    try:
        lst = WorkspaceList.query.get(list_id)
        if not lst:
            return jsonify({'success': False, 'error': 'List not found'}), 404
        
        # Check if list is expired
        if lst.compute_status() == 'expired':
            return jsonify({'success': False, 'error': 'Cannot start timer on expired list'})
        
        # Set 24h expiration
        from datetime import timedelta
        lst.active_24h_expires_at = datetime.utcnow() + timedelta(hours=24)
        lst.status = 'in_use'
        db.session.commit()
        
        return jsonify({
            'success': True,
            'message': f'24-hour timer started for "{lst.name}"',
            'list': lst.to_dict()
        })
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Error starting timer: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/lists/<int:list_id>/set-timer', methods=['POST'])
@login_required
def api_set_list_timer(list_id):
    """Set a custom 24-hour usage timer for a list"""
    try:
        lst = WorkspaceList.query.get(list_id)
        if not lst:
            return jsonify({'success': False, 'error': 'List not found'}), 404
        
        data = request.get_json()
        seconds = int(data.get('seconds', 0))
        
        if seconds <= 0:
            return jsonify({'success': False, 'error': 'Invalid time value'})
        
        if seconds > 86400:
            return jsonify({'success': False, 'error': 'Timer cannot exceed 24 hours'})
        
        # Set custom expiration time
        from datetime import timedelta
        lst.active_24h_expires_at = datetime.utcnow() + timedelta(seconds=seconds)
        db.session.commit()
        
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        secs = seconds % 60
        
        return jsonify({
            'success': True,
            'message': f'Timer set to {hours:02d}:{minutes:02d}:{secs:02d} for "{lst.name}"',
            'list': lst.to_dict()
        })
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Error setting timer: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/lists/<int:list_id>/reset-timer', methods=['POST'])

@login_required
def api_reset_list_timer(list_id):
    """Reset the 24-hour usage timer for a list"""
    try:
        lst = WorkspaceList.query.get(list_id)
        if not lst:
            return jsonify({'success': False, 'error': 'List not found'}), 404
        
        # Clear 24h timer
        lst.active_24h_expires_at = None
        lst.status = 'ready'
        db.session.commit()
        
        return jsonify({
            'success': True,
            'message': f'24-hour timer reset for "{lst.name}"',
            'list': lst.to_dict()
        })
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Error resetting timer: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/lists/<int:list_id>/expire', methods=['POST'])
@login_required
def api_expire_list(list_id):
    """Manually mark a list as expired"""
    try:
        lst = WorkspaceList.query.get(list_id)
        if not lst:
            return jsonify({'success': False, 'error': 'List not found'}), 404
        
        # Set lifetime expiration to now (makes it expired)
        lst.lifetime_expires_at = datetime.utcnow()
        lst.status = 'expired'
        lst.active_24h_expires_at = None  # Clear any active timer
        db.session.commit()
        
        return jsonify({
            'success': True,
            'message': f'"{lst.name}" marked as expired',
            'list': lst.to_dict()
        })
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Error expiring list: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/add-user', methods=['POST'])
@login_required
def api_add_user():
    if session.get('role') != 'admin':
        return jsonify({'success': False, 'error': 'Admin access required'})
    
    try:
        data = request.get_json()
        username = data.get('username', '').strip()
        password = data.get('password', '').strip()
        role = data.get('role', 'support')
        
        if not username or not password:
            return jsonify({'success': False, 'error': 'Username and password required'})
        
        if User.query.filter_by(username=username).first():
            return jsonify({'success': False, 'error': 'Username already exists'})
        
        hashed_password = generate_password_hash(password, method='pbkdf2:sha256')
        new_user = User(username=username, password=hashed_password, role=role)
        db.session.add(new_user)
        db.session.commit()
        
        return jsonify({'success': True, 'message': f'User {username} created successfully'})
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/list-users', methods=['GET'])
@login_required
def api_list_users():
    if session.get('role') != 'admin':
        return jsonify({'success': False, 'error': 'Admin access required'})
    
    try:
        users = User.query.all()
        user_list = [{'username': user.username, 'role': user.role} for user in users]
        return jsonify({'success': True, 'users': user_list})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/edit-user', methods=['POST'])
@login_required
def api_edit_user():
    if session.get('role') != 'admin':
        return jsonify({'success': False, 'error': 'Admin access required'})
    
    try:
        data = request.get_json()
        username = data.get('username', '').strip()
        new_password = data.get('password', '').strip()
        new_role = data.get('role', '').strip()
        
        if not username:
            return jsonify({'success': False, 'error': 'Username required'})
        
        user = User.query.filter_by(username=username).first()
        if not user:
            return jsonify({'success': False, 'error': 'User not found'})
        
        if not new_password:
            return jsonify({'success': False, 'error': 'Password required'})
        
        if new_role not in ['admin', 'support', 'mailer']:
            return jsonify({'success': False, 'error': 'Role must be admin, support, or mailer'})
        
        user.password = generate_password_hash(new_password, method='pbkdf2:sha256')
        user.role = new_role
        db.session.commit()
        
        return jsonify({'success': True, 'message': f'User {username} updated successfully'})
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/delete-user', methods=['POST'])
@login_required
def api_delete_user():
    if session.get('role') != 'admin':
        return jsonify({'success': False, 'error': 'Admin access required'})
    
    try:
        data = request.get_json()
        username = data.get('username', '').strip()
        
        if not username:
            return jsonify({'success': False, 'error': 'Username required'})
        
        user = User.query.filter_by(username=username).first()
        if not user:
            return jsonify({'success': False, 'error': 'User not found'})
        
        if username == session.get('user'):
            return jsonify({'success': False, 'error': 'Cannot delete your own account'})
        
        # CASCADE DELETE: First delete all notifications referencing this user
        try:
            from database import Notification
            deleted_notifications = Notification.query.filter_by(user_id=user.id).delete()
            app.logger.info(f"Deleted {deleted_notifications} notifications for user {username} (id={user.id})")
        except Exception as notif_err:
            app.logger.warning(f"Could not delete notifications for user {username}: {notif_err}")
        
        db.session.delete(user)
        db.session.commit()
        
        # SAVE DOMAIN TO DATABASE AS 'USED'
        try:
            if '@' in username:
                domain = username.split('@')[1].lower()
                from database import UsedDomain
                used_domain = UsedDomain.query.filter_by(domain_name=domain).first()
                if used_domain:
                    used_domain.ever_used = True
                    used_domain.updated_at = db.func.current_timestamp()
                else:
                    new_used_domain = UsedDomain(domain_name=domain, ever_used=True, is_verified=True, user_count=0)
                    db.session.add(new_used_domain)
                db.session.commit()
                app.logger.info(f"Saved domain {domain} as USED in DB via api_delete_user")
        except Exception as db_err:
            app.logger.error(f"Failed to save domain to DB in api_delete_user: {db_err}")
            # Don't rollback the user deletion if domain save fails, just log it.
        
        return jsonify({'success': True, 'message': f'User {username} deleted successfully'})
        
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Error deleting user: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/delete-users', methods=['POST'])
@login_required
def api_delete_users():
    """Delete multiple Google Workspace users by email addresses"""
    try:
        # Check if user is authenticated with Google account
        if 'current_account_name' not in session:
            return jsonify({'success': False, 'error': 'No Google account authenticated. Please authenticate first.'})
        
        # Get the current authenticated account
        account_name = session.get('current_account_name')
        logging.info(f"Deleting users for account: {account_name}")
        
        # Validate and recreate service if necessary
        if not google_api.validate_and_recreate_service(account_name):
            logging.error(f"Failed to validate or recreate service for account {account_name}")
            return jsonify({'success': False, 'error': 'Failed to establish Google API connection. Please re-authenticate.'})
        
        data = request.get_json()
        user_emails = data.get('user_emails', [])
        
        if not user_emails:
            return jsonify({'success': False, 'error': 'No email addresses provided'})
        
        if not isinstance(user_emails, list):
            return jsonify({'success': False, 'error': 'Email addresses must be provided as a list'})
        
        logging.info(f"Attempting to delete {len(user_emails)} users: {user_emails}")
        
        results = []
        successful_deletions = 0
        
        # Track domains to save status
        domains_to_save = set()

        for email in user_emails:
            email = email.strip()
            if not email:
                continue
            
            # Extract domain for saving
            if '@' in email:
                domains_to_save.add(email.split('@')[1].lower())

            try:
                logging.info(f"Deleting user: {email}")
                
                # Delete user from Google Workspace
                google_api.service.users().delete(userKey=email).execute()
                
                results.append({
                    'email': email,
                    'result': {'success': True, 'message': f'User {email} deleted successfully'}
                })
                successful_deletions += 1
                logging.info(f"Successfully deleted user: {email}")
                
            except Exception as user_error:
                error_msg = str(user_error)
                logging.error(f"Failed to delete user {email}: {error_msg}")
                results.append({
                    'email': email,
                    'result': {'success': False, 'error': error_msg}
                })

        logging.info(f"DEBUG: Domains extracted for saving: {domains_to_save}")
        
        # SAVE DOMAINS TO DATABASE AS 'USED'
        if domains_to_save:
            try:
                from database import UsedDomain, db
                for domain in domains_to_save:
                    logging.info(f"DEBUG: Attempting to save domain: {domain}")
                    used_domain = UsedDomain.query.filter_by(domain_name=domain).first()
                    if used_domain:
                        used_domain.ever_used = True
                        used_domain.updated_at = db.func.current_timestamp()
                        logging.info(f"DEBUG: Updated existing domain {domain} to ever_used=True")
                    else:
                        new_used_domain = UsedDomain(domain_name=domain, ever_used=True, is_verified=True, user_count=0)
                        db.session.add(new_used_domain)
                        logging.info(f"DEBUG: Added NEW domain {domain} with ever_used=True")
                db.session.commit()
                logging.info(f"Saved {len(domains_to_save)} domains as USED in DB via api_delete_users")
            except Exception as db_err:
                logging.error(f"Failed to save domains to DB in api_delete_users: {db_err}")
                db.session.rollback()
        else:
             logging.info("DEBUG: No domains found to save.")

        logging.info(f"User deletion completed. Successfully deleted {successful_deletions} out of {len(user_emails)} users")
        
        return jsonify({
            'success': True,
            'message': f'User deletion completed. Successfully deleted {successful_deletions} out of {len(user_emails)} users.',
            'results': results,
            'total_requested': len(user_emails),
            'successful_deletions': successful_deletions
        })
        
    except Exception as e:
        logging.error(f"Delete users error: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/add-whitelist-ip', methods=['POST'])
@login_required
def api_add_whitelist_ip():
    if session.get('role') != 'admin':
        return jsonify({'success': False, 'error': 'Admin access required'})
    
    try:
        data = request.get_json()
        ip_address = data.get('ip_address', '').strip()
        
        if not ip_address:
            return jsonify({'success': False, 'error': 'IP address required'})
        
        if WhitelistedIP.query.filter_by(ip_address=ip_address).first():
            return jsonify({'success': False, 'error': 'IP address already exists'})
        
        try:
            new_ip = WhitelistedIP(ip_address=ip_address)
            db.session.add(new_ip)
            db.session.commit()
        except Exception as db_error:
            db.session.rollback()
            app.logger.error(f"Database error adding IP {ip_address}: {db_error}")
            return jsonify({'success': False, 'error': str(db_error)})
            
        return jsonify({'success': True, 'message': f'IP {ip_address} added to whitelist'})
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/add-account-json', methods=['POST'])
@login_required
def api_add_account_json():
    """Add a new Google Workspace account using Service Account JSON"""
    # Allow admin, mailer, and support roles to add accounts
    allowed_roles = ['admin', 'mailer', 'support']
    if session.get('role') not in allowed_roles:
        return jsonify({'success': False, 'error': 'Access denied'})
    
    try:
        email = request.form.get('email', '').strip()
        name = request.form.get('name', '').strip()
        json_file = request.files.get('json_file')
        
        if not email:
            return jsonify({'success': False, 'error': 'Account email is required'})
        
        if not json_file:
            return jsonify({'success': False, 'error': 'Service account JSON file is required'})
            
        # Read and parse JSON file
        try:
            json_content_str = json_file.read().decode('utf-8')
            json_content = json.loads(json_content_str)
        except Exception as e:
            return jsonify({'success': False, 'error': f'Invalid JSON file: {str(e)}'})
            
        # Validate JSON content
        required_keys = ['type', 'project_id', 'private_key_id', 'private_key', 'client_email']
        missing_keys = [key for key in required_keys if key not in json_content]
        
        if missing_keys:
            return jsonify({'success': False, 'error': f'Invalid service account JSON. Missing keys: {", ".join(missing_keys)}'})
            
        if json_content.get('type') != 'service_account':
            return jsonify({'success': False, 'error': 'JSON file is not a service account credential'})
            
        # Check if account already exists (by admin email or client email)
        existing_account = ServiceAccount.query.filter(
            (ServiceAccount.admin_email == email) | (ServiceAccount.client_email == json_content['client_email'])
        ).first()
        
        if existing_account:
            return jsonify({'success': False, 'error': f'Account already exists (Admin: {existing_account.admin_email}, Client: {existing_account.client_email})'})
        
        # Auto-fix sequence to avoid duplicate key errors
        try:
            db.session.execute(db.text("SELECT setval('service_account_id_seq', COALESCE((SELECT MAX(id) FROM service_account), 1))"))
        except:
            pass  # Ignore if sequence doesn't exist
            
        # Create new Service Account
        new_account = ServiceAccount(
            name=name if name else email,
            admin_email=email,
            project_id=json_content['project_id'],
            client_email=json_content['client_email'],
            private_key_id=json_content['private_key_id'],
            json_content=json_content_str,
            is_active=True
        )
        
        db.session.add(new_account)
        db.session.commit()
        
        app.logger.info(f"Added new service account: {name} ({email})")
        return jsonify({'success': True, 'message': f'Account {email} added successfully'})
        
    except Exception as e:
        app.logger.error(f"Error adding account: {str(e)}")
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/import-account-from-s3', methods=['POST'])
@login_required
def api_import_account_from_s3():
    """Import a service account from S3 bucket.
    
    The JSON file should be stored at: s3://dev-app-passwords/workspace-keys/{email}.json
    """
    try:
        data = request.get_json()
        email = data.get('email', '').strip()
        
        if not email or '@' not in email:
            return jsonify({'success': False, 'error': 'Invalid email address'})
        
        # Check if account already exists
        existing_account = ServiceAccount.query.filter_by(admin_email=email).first()
        if existing_account:
            return jsonify({'success': False, 'error': f'Account {email} already exists'})
        
        # S3 configuration
        s3_bucket = 'dev-app-passwords'
        s3_key = f'workspace-keys/{email}.json'
        
        # Get AWS credentials from session or database
        try:
            aws_config = AwsConfig.query.first()
            if aws_config and aws_config.access_key_id and aws_config.secret_access_key:
                import boto3
                session = boto3.Session(
                    aws_access_key_id=aws_config.access_key_id,
                    aws_secret_access_key=aws_config.secret_access_key,
                    region_name=aws_config.region or 'eu-west-1'
                )
                s3_client = session.client('s3')
            else:
                # Try default credentials
                import boto3
                s3_client = boto3.client('s3', region_name='eu-west-1')
        except Exception as aws_err:
            app.logger.error(f"AWS session error: {aws_err}")
            return jsonify({'success': False, 'error': f'AWS configuration error: {str(aws_err)}'})
        
        # Fetch JSON file from S3
        try:
            app.logger.info(f"Fetching s3://{s3_bucket}/{s3_key}")
            response = s3_client.get_object(Bucket=s3_bucket, Key=s3_key)
            json_content_str = response['Body'].read().decode('utf-8')
            json_content = json.loads(json_content_str)
        except s3_client.exceptions.NoSuchKey:
            return jsonify({'success': False, 'error': f'JSON file not found in S3: {s3_key}'})
        except Exception as s3_err:
            app.logger.error(f"S3 fetch error: {s3_err}")
            return jsonify({'success': False, 'error': f'S3 error: {str(s3_err)}'})
        
        # Validate JSON content
        required_keys = ['type', 'project_id', 'private_key_id', 'private_key', 'client_email']
        missing_keys = [key for key in required_keys if key not in json_content]
        
        if missing_keys:
            return jsonify({'success': False, 'error': f'Invalid service account JSON. Missing keys: {", ".join(missing_keys)}'})
        
        if json_content.get('type') != 'service_account':
            return jsonify({'success': False, 'error': 'JSON file is not a service account credential'})
        
        # Auto-fix sequence to avoid duplicate key errors
        try:
            db.session.execute(db.text("SELECT setval('service_account_id_seq', COALESCE((SELECT MAX(id) FROM service_account), 1))"))
        except:
            pass
        
        # Create new Service Account
        new_account = ServiceAccount(
            name=email,
            admin_email=email,
            project_id=json_content['project_id'],
            client_email=json_content['client_email'],
            private_key_id=json_content['private_key_id'],
            json_content=json_content_str,
            is_active=True
        )
        
        db.session.add(new_account)
        db.session.commit()
        
        app.logger.info(f"Imported service account from S3: {email}")
        return jsonify({'success': True, 'message': f'Account imported successfully'})
        
    except Exception as e:
        app.logger.error(f"Error importing account from S3: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/fix-database-sequences', methods=['POST'])
@login_required
def api_fix_database_sequences():
    """Fix database sequences that might be out of sync"""
    # Allow all logged-in users to fix sequences (needed for account creation)
    
    try:
        fixed = []
        
        # Fix whitelisted_ip sequence
        try:
            db.session.execute(db.text("SELECT setval('whitelisted_ip_id_seq', COALESCE((SELECT MAX(id) FROM whitelisted_ip), 1))"))
            fixed.append('whitelisted_ip')
        except Exception as e:
            app.logger.warning(f"Could not fix whitelisted_ip sequence: {e}")
        
        # Fix service_account sequence
        try:
            db.session.execute(db.text("SELECT setval('service_account_id_seq', COALESCE((SELECT MAX(id) FROM service_account), 1))"))
            fixed.append('service_account')
        except Exception as e:
            app.logger.warning(f"Could not fix service_account sequence: {e}")
        
        # Fix google_account sequence
        try:
            db.session.execute(db.text("SELECT setval('google_account_id_seq', COALESCE((SELECT MAX(id) FROM google_account), 1))"))
            fixed.append('google_account')
        except Exception as e:
            app.logger.warning(f"Could not fix google_account sequence: {e}")
        
        db.session.commit()
        
        app.logger.info(f"Database sequences fixed: {fixed}")
        return jsonify({'success': True, 'message': f'Database sequences fixed: {", ".join(fixed)}', 'fixed': fixed})
        
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Error fixing database sequences: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/cleanup-duplicate-ips', methods=['POST'])
@login_required
def api_cleanup_duplicate_ips():
    """Clean up duplicate IP addresses in the whitelist"""
    if session.get('role') != 'admin':
        return jsonify({'success': False, 'error': 'Admin access required'})
    
    try:
        # Find and remove duplicate IPs, keeping only the first one
        duplicates = db.session.execute(db.text("""
            DELETE FROM whitelisted_ip 
            WHERE id NOT IN (
                SELECT MIN(id) 
                FROM whitelisted_ip 
                GROUP BY ip_address
            )
        """))
        
        db.session.commit()
        
        app.logger.info(f"Cleaned up {duplicates.rowcount} duplicate IP addresses")
        return jsonify({'success': True, 'message': f'Cleaned up {duplicates.rowcount} duplicate IP addresses'})
        
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Error cleaning up duplicate IPs: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/list-whitelist-ips', methods=['GET'])
@login_required
def api_list_whitelist_ips():
    if session.get('role') != 'admin':
        return jsonify({'success': False, 'error': 'Admin access required'})
    
    try:
        ips = WhitelistedIP.query.all()
        ip_list = [ip.ip_address for ip in ips]
        return jsonify({'success': True, 'ips': ip_list})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/delete-whitelist-ip', methods=['POST'])
def api_delete_whitelist_ip():
    """Delete IP from whitelist - accessible via emergency access or admin login"""
    # Check if user has emergency access or is logged in as admin
    if not session.get('emergency_access') and not session.get('user'):
        return jsonify({'success': False, 'error': 'Access denied. Please use emergency access or log in.'})
    
    if session.get('role') != 'admin' and not session.get('emergency_access'):
        return jsonify({'success': False, 'error': 'Admin access required'})
    
    try:
        data = request.get_json()
        ip_address = data.get('ip_address', '').strip()
        
        app.logger.info(f"Delete IP request: {ip_address}")
        app.logger.info(f"Session data: user={session.get('user')}, role={session.get('role')}, emergency_access={session.get('emergency_access')}")
        
        if not ip_address:
            return jsonify({'success': False, 'error': 'IP address required'})
        
        ip_to_delete = WhitelistedIP.query.filter_by(ip_address=ip_address).first()
        if not ip_to_delete:
            app.logger.warning(f"IP {ip_address} not found in database")
            return jsonify({'success': False, 'error': 'IP address not found'})
        
        app.logger.info(f"Found IP to delete: {ip_to_delete.id} - {ip_to_delete.ip_address}")
        
        try:
            db.session.delete(ip_to_delete)
            db.session.commit()
            app.logger.info(f"IP {ip_address} successfully deleted from whitelist by user: {session.get('user', 'emergency_access')}")
            return jsonify({'success': True, 'message': f'IP address {ip_address} removed from whitelist'})
        except Exception as db_error:
            db.session.rollback()
            app.logger.error(f"Database error deleting IP {ip_address}: {db_error}")
            return jsonify({'success': False, 'error': f'Database error: {str(db_error)}'})
        
    except Exception as e:
        app.logger.error(f"Error deleting IP from whitelist: {str(e)}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/delete-whitelist-ip-simple', methods=['POST'])
def api_delete_whitelist_ip_simple():
    """Simple delete IP endpoint for testing - no authentication required"""
    try:
        data = request.get_json()
        ip_address = data.get('ip_address', '').strip()
        
        app.logger.info(f"Simple delete IP request: {ip_address}")
        
        if not ip_address:
            return jsonify({'success': False, 'error': 'IP address required'})
        
        ip_to_delete = WhitelistedIP.query.filter_by(ip_address=ip_address).first()
        if not ip_to_delete:
            app.logger.warning(f"IP {ip_address} not found in database")
            return jsonify({'success': False, 'error': 'IP address not found'})
        
        app.logger.info(f"Found IP to delete: {ip_to_delete.id} - {ip_to_delete.ip_address}")
        
        try:
            db.session.delete(ip_to_delete)
            db.session.commit()
            app.logger.info(f"IP {ip_address} successfully deleted from whitelist")
            return jsonify({'success': True, 'message': f'IP address {ip_address} removed from whitelist'})
        except Exception as db_error:
            db.session.rollback()
            app.logger.error(f"Database error deleting IP {ip_address}: {db_error}")
            return jsonify({'success': False, 'error': f'Database error: {str(db_error)}'})
        
    except Exception as e:
        app.logger.error(f"Error deleting IP from whitelist: {str(e)}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/delete-all-whitelist-ips', methods=['POST'])
def api_delete_all_whitelist_ips():
    """Delete all IPs from whitelist - accessible via emergency access or admin login"""
    # Check if user has emergency access or is logged in as admin
    if not session.get('emergency_access') and not session.get('user'):
        return jsonify({'success': False, 'error': 'Access denied. Please use emergency access or log in.'})
    
    if session.get('role') != 'admin' and not session.get('emergency_access'):
        return jsonify({'success': False, 'error': 'Admin access required'})
    
    try:
        # Get count before deletion
        total_count = WhitelistedIP.query.count()
        app.logger.info(f"Deleting all {total_count} whitelisted IPs by user: {session.get('user', 'emergency_access')}")
        
        if total_count == 0:
            return jsonify({'success': True, 'message': 'No IPs to delete', 'deleted_count': 0})
        
        # Delete all whitelisted IPs
        WhitelistedIP.query.delete()
        db.session.commit()
        
        app.logger.info(f"Successfully deleted all {total_count} whitelisted IPs")
        return jsonify({
            'success': True, 
            'message': f'Successfully deleted all {total_count} whitelisted IPs',
            'deleted_count': total_count
        })
        
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Error deleting all whitelisted IPs: {str(e)}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/delete-all-whitelist-ips-simple', methods=['POST'])
def api_delete_all_whitelist_ips_simple():
    """Simple delete all IPs endpoint for testing - no authentication required"""
    try:
        # Get count before deletion
        total_count = WhitelistedIP.query.count()
        app.logger.info(f"Simple delete all {total_count} whitelisted IPs")
        
        if total_count == 0:
            return jsonify({'success': True, 'message': 'No IPs to delete', 'deleted_count': 0})
        
        # Delete all whitelisted IPs
        WhitelistedIP.query.delete()
        db.session.commit()
        
        app.logger.info(f"Successfully deleted all {total_count} whitelisted IPs (simple)")
        return jsonify({
            'success': True, 
            'message': f'Successfully deleted all {total_count} whitelisted IPs',
            'deleted_count': total_count
        })
        
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Error deleting all whitelisted IPs (simple): {str(e)}")
        return jsonify({'success': False, 'error': str(e)})

# ============================================================
# NOTIFICATION SYSTEM API ENDPOINTS
# ============================================================

@app.route('/api/notifications', methods=['GET'])
def api_get_notifications():
    """Get recent notifications for the header bell"""
    try:
        # Get the last 20 notifications, ordered by date
        notifications = Notification.query.order_by(Notification.created_at.desc()).limit(20).all()
        
        # Count unread
        unread_count = Notification.query.filter_by(is_read=False).count()
        
        notification_list = []
        for n in notifications:
            notification_list.append({
                'id': n.id,
                'type': n.type,
                'title': n.title,
                'message': n.message,
                'icon': n.icon,
                'is_read': n.is_read,
                'created_at': n.created_at.isoformat() if n.created_at else None,
                'time_ago': get_time_ago(n.created_at) if n.created_at else 'Unknown'
            })
        
        return jsonify({
            'success': True,
            'notifications': notification_list,
            'unread_count': unread_count
        })
    except Exception as e:
        app.logger.error(f"Error getting notifications: {e}")
        return jsonify({'success': False, 'error': str(e), 'notifications': [], 'unread_count': 0})

@app.route('/api/notifications/mark-read', methods=['POST'])
def api_mark_notifications_read():
    """Mark notifications as read"""
    try:
        data = request.get_json() or {}
        notification_ids = data.get('ids', [])
        mark_all = data.get('mark_all', False)
        
        if mark_all:
            # Mark all as read
            Notification.query.filter_by(is_read=False).update({'is_read': True})
            db.session.commit()
            return jsonify({'success': True, 'message': 'All notifications marked as read'})
        elif notification_ids:
            # Mark specific notifications as read
            Notification.query.filter(Notification.id.in_(notification_ids)).update({'is_read': True}, synchronize_session=False)
            db.session.commit()
            return jsonify({'success': True, 'message': f'{len(notification_ids)} notifications marked as read'})
        else:
            return jsonify({'success': False, 'error': 'No notification IDs provided'})
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Error marking notifications as read: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/notifications/clear', methods=['POST'])
def api_clear_notifications():
    """Clear all notifications (admin only)"""
    if session.get('role') != 'admin':
        return jsonify({'success': False, 'error': 'Admin access required'})
    
    try:
        count = Notification.query.count()
        Notification.query.delete()
        db.session.commit()
        return jsonify({'success': True, 'message': f'Cleared {count} notifications'})
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Error clearing notifications: {e}")
        return jsonify({'success': False, 'error': str(e)})

def get_time_ago(dt):
    """Convert datetime to human-readable time ago string"""
    if not dt:
        return 'Unknown'
    
    now = datetime.now()
    diff = now - dt
    seconds = diff.total_seconds()
    
    if seconds < 60:
        return 'Just now'
    elif seconds < 3600:
        mins = int(seconds / 60)
        return f'{mins}m ago'
    elif seconds < 86400:
        hours = int(seconds / 3600)
        return f'{hours}h ago'
    elif seconds < 604800:
        days = int(seconds / 86400)
        return f'{days}d ago'
    else:
        return dt.strftime('%b %d')

@app.route('/api/debug-whitelist-ips', methods=['GET'])
def api_debug_whitelist_ips():
    """Debug endpoint to check whitelist IPs and session"""
    try:
        # Get all whitelisted IPs
        ips = WhitelistedIP.query.all()
        ip_list = [{'id': ip.id, 'ip_address': ip.ip_address} for ip in ips]
        
        # Get session info
        session_info = {
            'user': session.get('user'),
            'role': session.get('role'),
            'emergency_access': session.get('emergency_access'),
            'session_keys': list(session.keys())
        }
        
        return jsonify({
            'success': True,
            'ip_count': len(ip_list),
            'ips': ip_list,
            'session': session_info
        })
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/authenticate', methods=['POST'])
@login_required
def api_authenticate():
    try:
        data = request.get_json()
        account_name = data.get('account_name')
        account_id = data.get('account_id')
        
        if not account_name and not account_id:
            return jsonify({'success': False, 'error': 'No account specified'})
        
        # 1. Check for Service Account (New Method - Priority)
        service_account = None
        if account_id:
            service_account = ServiceAccount.query.get(account_id)
        
        if not service_account and account_name:
            # Search by name first
            service_account = ServiceAccount.query.filter_by(name=account_name).first()
            
            # If not found by name, try admin_email
            if not service_account:
                service_account = ServiceAccount.query.filter_by(admin_email=account_name).first()
            
            # If still not found, try client_email
            if not service_account:
                service_account = ServiceAccount.query.filter_by(client_email=account_name).first()

        if service_account:
            # Service Accounts don't need OAuth flow, just set in session
            session['current_account_name'] = service_account.name
            session['current_account_id'] = service_account.id
            session['account_type'] = 'service_account'
            
            logging.info(f"Authenticated Service Account: {service_account.name}")
            return jsonify({
                'success': True, 
                'message': f'Successfully authenticated Service Account: {service_account.name}',
                'is_service_account': True
            })

        # 2. Check for Google Account (Old Method - Deprecated)
        # We keep this check only to provide a helpful error message
        if account_id:
            account = GoogleAccount.query.get(account_id)
        else:
            account = GoogleAccount.query.filter_by(account_name=account_name).first()
            
        if account:
            return jsonify({
                'success': False, 
                'error': 'OAuth authentication is deprecated. Please use Service Accounts.',
                'deprecated': True
            })
            
        return jsonify({'success': False, 'error': 'Account not found in database'})
            
    except Exception as e:
        logging.error(f"Authentication error: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/add-account', methods=['POST'])
@login_required
def api_add_account():
    return jsonify({'success': False, 'error': 'This authentication method is deprecated. Please use Service Accounts.'}), 410

@app.route('/api/list-accounts', methods=['GET'])
@login_required
def api_list_accounts():
    """List all Google accounts from database"""
    try:
        accounts = GoogleAccount.query.all()
        account_list = []
        for account in accounts:
            # Check if account has valid tokens
            token = GoogleToken.query.filter_by(account_id=account.id).first()
            is_authenticated = token is not None and token.token is not None
            
            account_data = {
                'id': account.id,
                'account_name': account.account_name,
                'client_id': account.client_id,
                'client_secret': account.client_secret,
                'is_authenticated': is_authenticated,
                'has_tokens': token is not None
            }
            account_list.append(account_data)
        
        return jsonify({'success': True, 'accounts': account_list})
    except Exception as e:
        logging.error(f"List accounts error: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/delete-account', methods=['POST'])
@login_required
def api_delete_account():
    """Delete a Google account or Service Account from database"""
    try:
        # Allow all user types (admin, mailer, support) to delete accounts
        user_role = session.get('role')
        if user_role not in ['admin', 'mailer', 'support']:
            return jsonify({'success': False, 'error': 'Access denied. Valid user role required.'})
        
        data = request.get_json()
        account_id = data.get('account_id')
        
        if not account_id:
            return jsonify({'success': False, 'error': 'Account ID required'})
        
        # Try to find in GoogleAccount (OAuth) first
        account = GoogleAccount.query.get(account_id)
        
        if account:
            account_name = account.account_name
            # Properly delete all related records to avoid foreign key constraints
            try:
                # First, get all tokens for this account
                tokens = GoogleToken.query.filter_by(account_id=account_id).all()
                
                for token in tokens:
                    # Clear the many-to-many relationship with scopes first
                    token.scopes.clear()
                    db.session.flush()  # Flush to ensure the relationship is cleared
                
                # Now delete all tokens for this account
                GoogleToken.query.filter_by(account_id=account_id).delete()
                
                # Finally delete the account (cascade will handle any remaining relationships)
                db.session.delete(account)
                
                # Commit all changes
                db.session.commit()
                
                logging.info(f"Successfully deleted OAuth account: {account_name} (ID: {account_id})")
                return jsonify({'success': True, 'message': f'Account {account_name} deleted successfully'})
                
            except Exception as db_error:
                db.session.rollback()
                logging.error(f"Database error during OAuth account deletion: {db_error}")
                return jsonify({'success': False, 'error': f'Database error: {str(db_error)}'})

        # If not found in GoogleAccount, try ServiceAccount
        service_account = ServiceAccount.query.get(account_id)
        
        if service_account:
            account_name = service_account.name
            try:
                db.session.delete(service_account)
                db.session.commit()
                
                logging.info(f"Successfully deleted Service Account: {account_name} (ID: {account_id})")
                return jsonify({'success': True, 'message': f'Service Account {account_name} deleted successfully'})
            except Exception as db_error:
                db.session.rollback()
                logging.error(f"Database error during Service Account deletion: {db_error}")
                return jsonify({'success': False, 'error': f'Database error: {str(db_error)}'})
                
        return jsonify({'success': False, 'error': 'Account not found'})
        
    except Exception as e:
        db.session.rollback()
        logging.error(f"Delete account error: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/get-account-status', methods=['GET'])
@login_required
def api_get_account_status():
    """Get authentication status for all accounts"""
    try:
        accounts = GoogleAccount.query.all()
        total_accounts = len(accounts)
        authenticated_count = 0
        need_auth_count = 0
        
        for account in accounts:
            token = GoogleToken.query.filter_by(account_id=account.id).first()
            if token and token.token:
                authenticated_count += 1
            else:
                need_auth_count += 1
        
        status = {
            'total': total_accounts,
            'authenticated': authenticated_count,
            'need_auth': need_auth_count,
            'status': 'Complete'
        }
        
        return jsonify({'success': True, 'status': status})
    except Exception as e:
        logging.error(f"Get account status error: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/add-accounts-from-json', methods=['POST'])
@login_required
def api_add_accounts_from_json():
    return jsonify({'success': False, 'error': 'This authentication method is deprecated. Please use Service Accounts.'}), 410

@app.route('/api/check-token-status', methods=['GET'])
@login_required
def api_check_token_status():
    """Check token status for all accounts"""
    try:
        accounts = GoogleAccount.query.all()
        token_status = []
        
        for account in accounts:
            token = GoogleToken.query.filter_by(account_id=account.id).first()
            
            status_info = {
                'account_id': account.id,
                'account_name': account.account_name,
                'has_tokens': token is not None,
                'token_valid': False,
                'needs_auth': True
            }
            
            if token and token.token:
                # Basic token validation (you can enhance this)
                status_info['token_valid'] = True
                status_info['needs_auth'] = False
            
            token_status.append(status_info)
        
        return jsonify({'success': True, 'token_status': token_status})
        
    except Exception as e:
        logging.error(f"Check token status error: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/oauth-callback')
def oauth_callback():
    return "This authentication method is deprecated. Please use Service Accounts.", 410

@app.route('/api/complete-oauth', methods=['POST'])
@login_required
def api_complete_oauth():
    return jsonify({'success': False, 'error': 'This authentication method is deprecated. Please use Service Accounts.'}), 410

@app.route('/api/create-gsuite-user', methods=['POST'])
@login_required
def api_create_gsuite_user():
    try:
        data = request.get_json()
        first_name = data.get('first_name')
        last_name = data.get('last_name')
        email = data.get('email')
        password = data.get('password')

        if not all([first_name, last_name, email, password]):
            return jsonify({'success': False, 'error': 'All fields are required'})

        result = google_api.create_gsuite_user(first_name, last_name, email, password)
        
        if result.get('success'):
            try:
                # Mark domain as used (ensure lowercase)
                domain = email.split('@')[1].lower()
                used_domain = UsedDomain.query.filter_by(domain_name=domain).first()
                if used_domain:
                    used_domain.ever_used = True
                    used_domain.updated_at = db.func.current_timestamp()
                else:
                    new_used_domain = UsedDomain(
                        domain_name=domain,
                        ever_used=True,
                        is_verified=True,
                        user_count=1
                    )
                    db.session.add(new_used_domain)
                db.session.commit()
            except Exception as db_e:
                app.logger.error(f"Failed to mark domain {domain} as used: {db_e}")
                
        return jsonify(result)

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/create-random-admin-users', methods=['POST'])
@login_required
def api_create_random_admin_users():
    """Create random admin users with specified admin roles"""
    try:
        if 'current_account_name' not in session:
            return jsonify({'success': False, 'error': 'No account authenticated. Please authenticate first.'})
        
        account_name = session.get('current_account_name')
        
        # Check if we have valid tokens for this account
        if not google_api.service:
            if google_api.is_token_valid(account_name):
                success = google_api.authenticate_with_tokens(account_name)
                if not success:
                    return jsonify({'success': False, 'error': 'Failed to authenticate with saved tokens. Please re-authenticate.'})
            else:
                return jsonify({'success': False, 'error': 'No valid tokens found. Please re-authenticate.'})
        
        req = request.get_json(silent=True) or {}
        num_users = req.get('num_users', 1)
        domain = req.get('domain', '')
        password = req.get('password', '')
        admin_role = req.get('admin_role', 'SUPER_ADMIN')
        
        if not domain or '.' not in domain:
            return jsonify({'success': False, 'error': 'Please provide a valid domain'})
        
        if not password or len(password) < 8:
            return jsonify({'success': False, 'error': 'Password must be at least 8 characters long'})
        
        if num_users <= 0 or num_users > 50:
            return jsonify({'success': False, 'error': 'Number of admin users must be between 1 and 50'})
        
        # Validate admin role
        valid_roles = [
            'SUPER_ADMIN', 'USER_MANAGEMENT_ADMIN', 'HELP_DESK_ADMIN',
            'SERVICE_ADMIN', 'BILLING_ADMIN', 'SECURITY_ADMIN'
        ]
        if admin_role not in valid_roles:
            return jsonify({'success': False, 'error': f'Invalid admin role. Must be one of: {", ".join(valid_roles)}'})
        
        logging.info(f"Creating {num_users} random admin users for domain {domain} with role {admin_role}")
        
        # Create random admin users
        result = google_api.create_random_admin_users(
            num_users=num_users,
            domain=domain,
            password=password,
            admin_role=admin_role
        )
        
        if result['success']:
            try:
                # Mark domain as used (ensure lowercase)
                domain_lower = domain.lower()
                used_domain = UsedDomain.query.filter_by(domain_name=domain_lower).first()
                if used_domain:
                    used_domain.ever_used = True
                    used_domain.updated_at = db.func.current_timestamp()
                else:
                    new_used_domain = UsedDomain(
                        domain_name=domain_lower,
                        ever_used=True,
                        is_verified=True,
                        user_count=num_users
                    )
                    db.session.add(new_used_domain)
                db.session.commit()
                logging.info(f"Marked domain {domain} as used (admin users)")
            except Exception as db_e:
                logging.error(f"Failed to mark domain {domain} as used: {db_e}")

            return jsonify({
                'success': True,
                'message': f'Successfully created {num_users} random admin users',
                'password': password,
                'admin_role': admin_role,
                'results': result.get('results', [])
            })
        else:
            return jsonify({
                'success': False,
                'error': result.get('error', 'Unknown error'),
                'error_type': result.get('error_type', 'unknown')
            })

    except Exception as e:
        logging.error(f"Create random admin users error: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/create-random-users', methods=['POST'])
@login_required
def api_create_random_users():
    # Set timeout for this endpoint (15 minutes)
    import signal
    
    def timeout_handler(signum, frame):
        raise TimeoutError("Request timed out after 30 minutes")
    
    signal.signal(signal.SIGALRM, timeout_handler)
    signal.alarm(1800)  # 30 minutes timeout
    
    try:
        data = request.get_json()
        num_users = data.get('num_users')
        domain = data.get('domain')
        password = data.get('password', 'SecurePass123')

        if not num_users or num_users <= 0:
            return jsonify({'success': False, 'error': 'Number of users must be greater than 0'})

        if not domain or not domain.strip():
            return jsonify({'success': False, 'error': 'Domain is required'})
        
        if not password or len(password) < 8:
            return jsonify({'success': False, 'error': 'Password must be at least 8 characters long'})

        # Sanitize password - remove any potentially problematic characters
        import re
        password = re.sub(r'[^\w\-_!@#$%^&*()+=]', '', password)
        
        if not password.strip():
            return jsonify({'success': False, 'error': 'Password cannot be empty after sanitization'})

        # Limit the number of users for performance
        if num_users > 1000:
            return jsonify({'success': False, 'error': 'Maximum 1000 users allowed per batch for performance'})

        # Clean domain name
        domain = domain.strip().lower()
        
        # Basic domain validation - check if it has at least one dot and valid characters
        if '.' not in domain or len(domain.split('.')) < 2:
            return jsonify({'success': False, 'error': 'Domain must be a valid domain (e.g., example.com)'})
        
        # Check for valid domain characters (letters, numbers, dots, hyphens)
        if not re.match(r'^[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$', domain):
            return jsonify({'success': False, 'error': 'Domain contains invalid characters'})

        result = google_api.create_random_users(num_users, domain, password)
        signal.alarm(0)  # Cancel timeout
        
        if result.get('success') and result.get('successful_count', 0) > 0:
            try:
                # Mark domain as used
                used_domain = UsedDomain.query.filter_by(domain_name=domain).first()
                if used_domain:
                    used_domain.ever_used = True
                    used_domain.updated_at = db.func.current_timestamp()
                else:
                    new_used_domain = UsedDomain(
                        domain_name=domain,
                        ever_used=True,
                        is_verified=True,
                        user_count=result.get('successful_count', 0)
                    )
                    db.session.add(new_used_domain)
                db.session.commit()
            except Exception as db_e:
                app.logger.error(f"Failed to mark domain {domain} as used: {db_e}")
                
        return jsonify(result)

    except Exception as e:
        signal.alarm(0)  # Cancel timeout
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/bulk-create-account-users', methods=['POST'])
@login_required
def api_bulk_create_account_users():
    """
    Create users for multiple accounts in bulk (Async).
    """
    cleanup_old_progress()
    data = request.get_json()
    accounts_data = data.get('accounts_data', [])
    
    # Validation
    if not accounts_data or len(accounts_data) == 0:
        return jsonify({'success': False, 'error': 'No accounts provided'})
    
    import re
    for idx, account_info in enumerate(accounts_data):
        account_name = account_info.get('account', '').strip()
        users_per_account = account_info.get('users_per_account', 0)
        domain = account_info.get('domain', '').strip()
        password = account_info.get('password', '').strip()
        
        if not account_name:
            return jsonify({'success': False, 'error': f'Row {idx + 1}: Account email is required'})
        
        if not users_per_account or int(users_per_account) < 1 or int(users_per_account) > 1000:
            return jsonify({'success': False, 'error': f'Row {idx + 1} ({account_name}): Users per account must be between 1 and 1000'})
        
        if not password or len(password) < 8:
            return jsonify({'success': False, 'error': f'Row {idx + 1} ({account_name}): Password must be at least 8 characters long'})
        
        # Clean domain if provided
        if domain:
            domain = domain.strip().lower()
            if not re.match(r'^[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$', domain):
                return jsonify({'success': False, 'error': f'Row {idx + 1} ({account_name}): Invalid domain format'})
        
        # Sanitize password
        password = re.sub(r'[^\w\-_!@#$%^&*()+=]', '', password)
        if not password.strip():
            return jsonify({'success': False, 'error': f'Row {idx + 1} ({account_name}): Password cannot be empty after sanitization'})
        
        # Update
        account_info['domain'] = domain if domain else ''
        account_info['password'] = password
        account_info['users_per_account'] = int(users_per_account)

    import uuid
    import threading
    task_id = str(uuid.uuid4())
    update_progress(task_id, 0, len(accounts_data), "starting", "Initializing bulk creation...")
    
    def background_task(task_id, accounts_data):
        with app.app_context():
            from concurrent.futures import ThreadPoolExecutor, as_completed
            import random
            import string
            from faker import Faker
            
            try:
                # Step 0: Immediate Domain Save (Async now)
                domains_to_save_immediately = set()
                for account_info in accounts_data:
                    domain = account_info.get('domain', '').strip().lower()
                    if domain:
                        domains_to_save_immediately.add(domain)
                
                if domains_to_save_immediately:
                    try:
                        from database import UsedDomain, db
                        update_progress(task_id, 0, len(accounts_data), "saving_domains", f"Pre-saving {len(domains_to_save_immediately)} domains...")
                        for target_domain in domains_to_save_immediately:
                            used_domain = UsedDomain.query.filter_by(domain_name=target_domain).first()
                            if used_domain:
                                used_domain.ever_used = True
                                used_domain.updated_at = db.func.current_timestamp()
                            else:
                                new_used_domain = UsedDomain(domain_name=target_domain, ever_used=True, is_verified=True, user_count=0)
                                db.session.add(new_used_domain)
                        db.session.commit()
                    except Exception as immediate_save_err:
                        app.logger.error(f"Immediate save error: {immediate_save_err}")
                        db.session.rollback()

                app.logger.info(f"[BULK ACCOUNTS] Starting bulk creation for {len(accounts_data)} account(s)")
                
                # Step 1: Authenticate
                update_progress(task_id, 0, len(accounts_data), "authenticating", f"Authenticating {len(accounts_data)} accounts...")
                
                authenticated_accounts = {} # map account_name -> auth_result

                def authenticate_account(account_info):
                    with app.app_context(): # Ensure app context for DB access
                        try:
                            account_name = account_info['account']
                            from database import ServiceAccount, GoogleAccount
                            from services.google_domains_service import GoogleDomainsService
                            
                            service_account = ServiceAccount.query.filter_by(admin_email=account_name).first()
                            if not service_account:
                                service_account = ServiceAccount.query.filter_by(name=account_name).first()
                            
                            if service_account:
                                try:
                                    domains_service = GoogleDomainsService(service_account.name)
                                    admin_service = domains_service._get_admin_service()
                                    return {'account': account_name, 'authenticated': True, 'service': admin_service, 'is_service_account': True}
                                except Exception as sa_err:
                                    return {'account': account_name, 'authenticated': False, 'error': str(sa_err)}
                            
                            # Fallback to GoogleAccount (legacy OAuth)
                            # Need to import these functions or ensure they are globally available
                            from . import authenticate_without_session, get_service_without_session
                            if not authenticate_without_session(account_name):
                                return {'account': account_name, 'authenticated': False, 'error': 'Auth failed'}
                            
                            service = get_service_without_session(account_name)
                            if not service:
                                return {'account': account_name, 'authenticated': False, 'error': 'No service'}
                            
                            return {'account': account_name, 'authenticated': True, 'service': service}
                        except Exception as e:
                            return {'account': account_info['account'], 'authenticated': False, 'error': str(e)}

                with ThreadPoolExecutor(max_workers=max(1, min(10, len(accounts_data)))) as auth_executor:
                    auth_futures = {auth_executor.submit(authenticate_account, info): info for info in accounts_data}
                    for i, future in enumerate(as_completed(auth_futures)):
                        res = future.result()
                        if res['authenticated']:
                            authenticated_accounts[res['account']] = res
                        update_progress(task_id, i+1, len(accounts_data), "authenticating", f"Authenticated {i+1}/{len(accounts_data)} accounts...")

                if not authenticated_accounts:
                     # All failed
                     pass # Continue to show errors

                # Step 2: Create Users
                update_progress(task_id, 0, len(authenticated_accounts), "creating", f"Creating users for {len(authenticated_accounts)} accounts...")

                def process_account(account_info):
                    # Re-implementation of logic
                    with app.app_context():
                        from database import UsedDomain, db, ServiceAccount, GoogleAccount
                        account_name = account_info['account']
                        users_per_account = account_info['users_per_account']
                        domain_input = account_info['domain'] # Pre-validated/default handling to come
                        password = account_info['password']
                        
                        account_result = {
                            'account': account_name,
                            'authenticated': False,
                            'users': [],
                            'error': None
                        }
                        
                        try:
                            if account_name not in authenticated_accounts:
                                account_result['error'] = 'Authentication failed previously'
                                return account_result
                            
                            auth_data = authenticated_accounts[account_name]
                            service = auth_data['service']
                            account_result['authenticated'] = True
                            
                            # Determine domain
                            domain = domain_input
                            if not domain:
                                if '@' in account_name:
                                    domain = account_name.split('@')[1].lower()
                                else:
                                    # Try to fetch from DB config
                                    sa = ServiceAccount.query.filter_by(name=account_name).first()
                                    if sa and sa.admin_email and '@' in sa.admin_email:
                                        domain = sa.admin_email.split('@')[1].lower()
                                    else:
                                        ga = GoogleAccount.query.filter_by(account_name=account_name).first()
                                        if ga and ga.account_name and '@' in ga.account_name:
                                            domain = ga.account_name.split('@')[1].lower()
                            
                            if not domain:
                                account_result['error'] = 'Could not determine domain'
                                return account_result
                            
                            app.logger.info(f"[BULK ACCOUNTS] [{account_name}] Creating {users_per_account} users on domain {domain}")
                            
                            # Create users
                            fake = Faker()
                            
                            for i in range(users_per_account):
                                try:
                                    first_name = fake.first_name()
                                    last_name = fake.last_name()
                                    random_num = ''.join(random.choices(string.digits, k=4))
                                    email = f"{first_name.lower()}{last_name.lower()}{random_num}@{domain}"
                                    
                                    user_body = {
                                        "primaryEmail": email,
                                        "name": { "givenName": first_name, "familyName": last_name },
                                        "password": password,
                                        "changePasswordAtNextLogin": False
                                    }
                                    
                                    service.users().insert(body=user_body).execute()
                                    
                                    account_result['users'].append({
                                        'email': email,
                                        'password': password,
                                        'first_name': first_name,
                                        'last_name': last_name,
                                        'success': True
                                    })
                                except Exception as user_err:
                                     account_result['users'].append({
                                        'email': f'failed_{i}@{domain}',
                                        'password': password,
                                        'success': False,
                                        'error': str(user_err)
                                    })
                            
                            # Domain update (already done mostly, but update user count/verified)
                            success_count = sum(1 for u in account_result['users'] if u.get('success'))
                            if success_count > 0:
                                try:
                                    domain_lower = domain.lower()
                                    used_domain = UsedDomain.query.filter_by(domain_name=domain_lower).first()
                                    if used_domain:
                                        used_domain.ever_used = True
                                        used_domain.updated_at = db.func.current_timestamp()
                                        # used_domain.user_count = (used_domain.user_count or 0) + success_count # Optional
                                    else:
                                        new_used_domain = UsedDomain(domain_name=domain_lower, ever_used=True, is_verified=True, user_count=success_count)
                                        db.session.add(new_used_domain)
                                    db.session.commit()
                                except Exception as db_e:
                                    app.logger.error(f"DB update failed: {db_e}")
                            
                        except Exception as e:
                            account_result['error'] = str(e)
                        
                        return account_result

                all_results = []
                with ThreadPoolExecutor(max_workers=max(1, min(10, len(accounts_data)))) as executor:
                     # Only submit authenticated ones + handle failed separately
                    future_to_info = {}
                    for info in accounts_data:
                        if info['account'] in authenticated_accounts:
                             future_to_info[executor.submit(process_account, info)] = info
                        else:
                             # Add failed auth result
                             all_results.append({'account': info['account'], 'authenticated': False, 'users': [], 'error': 'Authentication failed'})
                    
                    completed_count = 0
                    for future in as_completed(future_to_info):
                        res = future.result()
                        all_results.append(res)
                        completed_count += 1
                        update_progress(task_id, completed_count, len(future_to_info), "creating", f"Processed {completed_count}/{len(future_to_info)} accounts...")

                # Summary
                total_accounts = len(all_results)
                total_users_created = sum(sum(1 for u in r.get('users', []) if u.get('success')) for r in all_results)
                total_users_failed = sum(sum(1 for u in r.get('users', []) if not u.get('success')) for r in all_results)
                successful_accounts = sum(1 for r in all_results if r.get('authenticated') and len(r.get('users', [])) > 0)
                failed_accounts = total_accounts - successful_accounts

                final_result = {
                    'success': True,
                    'total_accounts_processed': total_accounts,
                    'successful_accounts': successful_accounts,
                    'failed_accounts': failed_accounts,
                    'total_successful_users': total_users_created,
                    'total_failed_users': total_users_failed,
                    'results': all_results
                }
                
                update_progress(task_id, 100, 100, "completed", "Bulk creation finished!", final_result)
                app.logger.info(f"[BULK ACCOUNT TASK {task_id}] COMPLETED")
                
            except Exception as e:
                app.logger.error(f"[BULK CREATE TASK {task_id}] CRASHED: {e}")
                import traceback
                app.logger.error(traceback.format_exc())
                update_progress(task_id, 0, 0, "error", f"Task failed: {str(e)}")

    threading.Thread(target=background_task, args=(task_id, accounts_data)).start()
    return jsonify({'success': True, 'task_id': task_id})

@app.route('/api/bulk-retrieve-account-users', methods=['POST'])
@login_required
def api_bulk_retrieve_account_users():
    """
    Authenticate and retrieve all users from multiple accounts in bulk.
    Authenticates all accounts first, then retrieves users in parallel.
    Supports both ServiceAccount and GoogleAccount.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    
    try:
        data = request.get_json()
        accounts = data.get('accounts', [])
        account_passwords = data.get('account_passwords', {}) # Optional mapping of account -> password
        
        if not accounts or len(accounts) == 0:
            return jsonify({'success': False, 'error': 'No accounts provided'})
        
        app.logger.info(f"[BULK RETRIEVE] Starting bulk retrieve for {len(accounts)} account(s)")
        
        # Step 1: Authenticate all accounts in parallel
        # (This logic is duplicated from api_bulk_delete_account_users for self-containment)
        def authenticate_account(account_name):
            """Authenticate a single account - supports both ServiceAccount and GoogleAccount"""
            with app.app_context():
                try:
                    # Try ServiceAccount first (admin_email or name)
                    from database import ServiceAccount
                    service_account = ServiceAccount.query.filter_by(admin_email=account_name).first()
                    if not service_account:
                        service_account = ServiceAccount.query.filter_by(name=account_name).first()
                    
                    if service_account:
                        # Use GoogleDomainsService for Service Account
                        from services.google_domains_service import GoogleDomainsService
                        try:
                            domains_service = GoogleDomainsService(service_account.name)
                            admin_service = domains_service._get_admin_service()
                            return {'account': account_name, 'authenticated': True, 'service': admin_service, 'is_service_account': True}
                        except Exception as sa_err:
                            app.logger.error(f"[BULK RETRIEVE] [{account_name}] ServiceAccount auth error: {sa_err}")
                            return {'account': account_name, 'authenticated': False, 'error': f'ServiceAccount auth failed: {str(sa_err)}'}
                    
                    # Fallback to GoogleAccount (legacy OAuth)
                    from database import GoogleAccount
                    account_db = GoogleAccount.query.filter_by(account_name=account_name).first()
                    if not account_db:
                        return {'account': account_name, 'authenticated': False, 'error': 'Account not found in database'}
                    
                    auth_success = authenticate_without_session(account_name)
                    if not auth_success:
                        return {'account': account_name, 'authenticated': False, 'error': 'Failed to authenticate. Please ensure authenticator is saved.'}
                    
                    service = get_service_without_session(account_name)
                    if not service:
                        return {'account': account_name, 'authenticated': False, 'error': 'Failed to get service'}
                    
                    return {'account': account_name, 'authenticated': True, 'service': service}
                except Exception as e:
                    app.logger.error(f"[BULK RETRIEVE] [{account_name}] Authentication error: {e}")
                    return {'account': account_name, 'authenticated': False, 'error': str(e)}
        
        authenticated_accounts = {}
        with ThreadPoolExecutor(max_workers=max(1, min(10, len(accounts)))) as auth_executor:
            auth_futures = {auth_executor.submit(authenticate_account, account): account for account in accounts}
            
            for future in as_completed(auth_futures):
                account = auth_futures[future]
                try:
                    auth_result = future.result()
                    if auth_result['authenticated']:
                        authenticated_accounts[account] = auth_result
                    else:
                        app.logger.error(f"[BULK RETRIEVE] [{account}] Authentication failed: {auth_result.get('error', 'Unknown error')}")
                except Exception as e:
                    app.logger.error(f"[BULK RETRIEVE] [{account}] Authentication exception: {e}")
        
        app.logger.info(f"[BULK RETRIEVE] Authenticated {len(authenticated_accounts)}/{len(accounts)} account(s)")
        
        # Step 2: Retrieve users from all authenticated accounts in parallel
        def retrieve_account_users(account_name):
            """Retrieve all users from a single authenticated account"""
            with app.app_context():
                account_result = {
                    'account': account_name,
                    'authenticated': False,
                    'users': [],
                    'error': None
                }
                
                try:
                    if account_name not in authenticated_accounts:
                        account_result['error'] = 'Account was not authenticated'
                        return account_result
                    
                    auth_data = authenticated_accounts[account_name]
                    service = auth_data['service']
                    account_result['authenticated'] = True
                    
                    app.logger.info(f"[BULK RETRIEVE] [{account_name}] Retrieving users...")
                    
                    # Get all users
                    all_users = []
                    page_token = None
                    while True:
                        try:
                            # Use maxResults=500 for efficiency
                            users_result = service.users().list(customer='my_customer', maxResults=500, pageToken=page_token).execute()
                            users = users_result.get('users', [])
                            all_users.extend(users)
                            
                            page_token = users_result.get('nextPageToken')
                            if not page_token:
                                break
                        except Exception as e:
                            app.logger.error(f"[BULK RETRIEVE] [{account_name}] Error retrieving page: {e}")
                            if not all_users: # If we failed on first page, treat as error
                                raise e
                            break # Otherwise accept partial results
                    
                    # Format user data
                    formatted_users = []
                    default_password = account_passwords.get(account_name, '')
                    
                    for user in all_users:
                        email = user.get('primaryEmail', '')
                        user_id = user.get('id', '') # Fixed indentation
                        
                        # Detect if admin
                        is_admin = user.get('isAdmin', False)
                        
                        # Add to list
                        formatted_users.append({
                            'email': email,
                            'primaryEmail': email,
                            'id': user_id,
                            'name': user.get('name', {}).get('fullName', ''),
                            'isAdmin': is_admin,
                            'creationTime': user.get('creationTime', ''),
                            'lastLoginTime': user.get('lastLoginTime', ''),
                            'suspended': user.get('suspended', False),
                            'password': default_password, # Includes account password if available
                            'app_password': '' # Placeholder, we don't know app passwords from Google API
                        })
                    
                    account_result['users'] = formatted_users
                    app.logger.info(f"[BULK RETRIEVE] [{account_name}] Successfully retrieved {len(formatted_users)} user(s)")
                    
                except Exception as e:
                    account_result['error'] = str(e)
                    app.logger.error(f"[BULK RETRIEVE] [{account_name}] Error: {e}")
                
                return account_result
        
        # Parallel execution for retrieval
        all_results = []
        if not authenticated_accounts:
            # Return partial results if some auths failed but process completed
            pass
        else:
            with ThreadPoolExecutor(max_workers=max(1, min(10, len(authenticated_accounts)))) as executor:
                futures = {executor.submit(retrieve_account_users, account): account for account in authenticated_accounts.keys()}
                
                for future in as_completed(futures):
                    try:
                        all_results.append(future.result())
                    except Exception as e:
                        account = futures[future]
                        all_results.append({
                            'account': account,
                            'authenticated': True, # It was auth'd to retrieve
                            'error': f"Exception during retrieval: {str(e)}"
                        })

        # Compile final stats
        total_retrieved_users = sum(len(r.get('users', []) or []) for r in all_results)
        failed_accounts = len(accounts) - len([r for r in all_results if not r.get('error') and r.get('authenticated')])
        
        return jsonify({
            'success': True,
            'message': 'Bulk user retrieval completed',
            'results': all_results,
            'total_accounts': len(accounts),
            'total_users': total_retrieved_users, # This was likely the missing field frontend expected
            'total_failed': failed_accounts
        })

    except Exception as e:
        app.logger.error(f"[BULK RETRIEVE] ✗✗✗ CRITICAL ERROR: {e}")
        app.logger.error(traceback.format_exc())
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/bulk-delete-account-users', methods=['POST'])
@login_required
def api_bulk_delete_account_users():
    """
    Authenticate and delete all users from multiple accounts in bulk (Async).
    """
    cleanup_old_progress()
    data = request.get_json()
    accounts = data.get('accounts', [])
    
    if not accounts or len(accounts) == 0:
        return jsonify({'success': False, 'error': 'No accounts provided'})
    
    task_id = str(uuid.uuid4())
    update_progress(task_id, 0, len(accounts), "starting", "Initializing bulk deletion...")
    
    def background_task(task_id, accounts):
        with app.app_context():
            from concurrent.futures import ThreadPoolExecutor, as_completed
            # Define result containers
            saved_domains_list = []
            failed_domains_list = []
            attempted_domains = []
            domain_map = {}
            all_results = []
            
            try:
                app.logger.info(f"[BULK DELETE TASK {task_id}] Starting for {len(accounts)} accounts")
                
                # Pre-process accounts
                account_list = []
                for item in accounts:
                    if isinstance(item, dict):
                        acc_name = item.get('account')
                        acc_domain = item.get('domain')
                        if acc_name:
                            account_list.append(acc_name)
                            if acc_domain:
                                domain_map[acc_name] = acc_domain.lower()
                    elif isinstance(item, str):
                        account_list.append(item)
                
                # Step 0: Immediate Domain Save
                if domain_map:
                    try:
                        from database import UsedDomain, db
                        from sqlalchemy import text
                        update_progress(task_id, 0, len(account_list), "saving_domains", f"Pre-saving {len(domain_map)} domains...")
                        
                        for acc_name, target_domain in domain_map.items():
                            attempted_domains.append(f"ATTEMPTING:{target_domain}")
                            try:
                                now = datetime.utcnow()
                                result = db.session.execute(
                                    text("UPDATE used_domain SET ever_used = TRUE, updated_at = :now WHERE domain_name = :domain"),
                                    {"domain": target_domain, "now": now}
                                )
                                if result.rowcount == 0:
                                    db.session.execute(text("SELECT setval('used_domain_id_seq', (SELECT COALESCE(MAX(id), 0) + 1 FROM used_domain), false)"))
                                    db.session.execute(
                                        text("INSERT INTO used_domain (domain_name, ever_used, is_verified, user_count, created_at, updated_at) VALUES (:domain, TRUE, TRUE, 0, :now, :now)"),
                                        {"domain": target_domain, "now": now}
                                    )
                                db.session.commit()
                                
                                # Verify
                                verify = db.session.execute(text("SELECT domain_name FROM used_domain WHERE domain_name = :domain"), {"domain": target_domain}).fetchone()
                                if verify:
                                    saved_domains_list.append(target_domain)
                                else:
                                    failed_domains_list.append(target_domain)
                            except Exception as save_err:
                                app.logger.error(f"Save failed for {target_domain}: {save_err}")
                                failed_domains_list.append(f"{target_domain}: {str(save_err)}")
                                db.session.rollback()
                    except Exception as domain_err:
                        app.logger.error(f"Domain save error: {domain_err}")

                # Step 1: Authenticate
                update_progress(task_id, 0, len(account_list), "authenticating", f"Authenticating {len(account_list)} accounts...")
                
                authenticated_accounts = {}
                
                def authenticate_account(account_name):
                    with app.app_context():
                        try:
                            from database import ServiceAccount, GoogleAccount
                            from services.google_domains_service import GoogleDomainsService
                            
                            # Service Account Check
                            service_account = ServiceAccount.query.filter_by(admin_email=account_name).first()
                            if not service_account:
                                service_account = ServiceAccount.query.filter_by(name=account_name).first()
                            
                            if service_account:
                                try:
                                    domains_service = GoogleDomainsService(service_account.name)
                                    admin_service = domains_service._get_admin_service()
                                    return {'account': account_name, 'authenticated': True, 'service': admin_service, 'is_service_account': True}
                                except Exception as sa_err:
                                    return {'account': account_name, 'authenticated': False, 'error': str(sa_err)}
                            
                            # OAuth Check
                            if not authenticate_without_session(account_name):
                                reason = "Auth failed"
                                try:
                                    from database import ServiceAccount, GoogleAccount
                                    # Diagnostic check
                                    sa = ServiceAccount.query.filter(ServiceAccount.admin_email.ilike(account_name)).first()
                                    ga = GoogleAccount.query.filter(GoogleAccount.account_name.ilike(account_name)).first()
                                    if not sa and not ga:
                                        reason = f"Account '{account_name}' not found in DB"
                                    elif sa:
                                        reason = f"ServiceAccount '{sa.admin_email}' found but auth failed (JSON/Scopes?)"
                                    elif ga:
                                        reason = f"GoogleAccount '{ga.account_name}' found but auth failed (Token?)"
                                except:
                                    reason = "Auth failed (Diagnostic error)"
                                return {'account': account_name, 'authenticated': False, 'error': reason}
                            
                            service = get_service_without_session(account_name)
                            if not service:
                                return {'account': account_name, 'authenticated': False, 'error': 'No service returned'}
                            
                            return {'account': account_name, 'authenticated': True, 'service': service}
                        except Exception as e:
                            return {'account': account_name, 'authenticated': False, 'error': str(e)}

                auth_failures = []
                with ThreadPoolExecutor(max_workers=max(1, min(10, len(account_list)))) as auth_executor:
                    auth_futures = {auth_executor.submit(authenticate_account, acc): acc for acc in account_list}
                    for i, future in enumerate(as_completed(auth_futures)):
                        acc = auth_futures[future]
                        res = future.result()
                        if res['authenticated']:
                            authenticated_accounts[acc] = res
                        else:
                            auth_failures.append(res)
                        update_progress(task_id, i+1, len(account_list), "authenticating", f"Authenticated {i+1}/{len(account_list)} accounts...")

                if not authenticated_accounts:
                    first_err = auth_failures[0]['error'] if auth_failures else "Unknown"
                    result_data = {'success': False, 'error': f"All {len(account_list)} accounts failed auth. First error: {first_err}", 'results': auth_failures}
                    update_progress(task_id, len(account_list), len(account_list), "completed", f"Auth Failed: {first_err}", result_data)
                    return

                # Step 2: Delete Users
                update_progress(task_id, 0, len(authenticated_accounts), "deleting", f"Deleting users from {len(authenticated_accounts)} accounts...")
                
                def delete_account_users(account_name):
                    with app.app_context():
                        # We can use app.app_context() again or assume outer context
                        # Simplified for brevity but keeping core logic
                        account_result = {'account': account_name, 'authenticated': True, 'deleted_count': 0, 'failed_count': 0, 'error': None}
                        try:
                            auth_data = authenticated_accounts[account_name]
                            service = auth_data['service']
                            
                            # List users
                            all_users = []
                            page_token = None
                            while True:
                                try:
                                    if page_token:
                                        users_result = service.users().list(customer='my_customer', maxResults=500, pageToken=page_token).execute()
                                    else:
                                        users_result = service.users().list(customer='my_customer', maxResults=500).execute()
                                    all_users.extend(users_result.get('users', []))
                                    page_token = users_result.get('nextPageToken')
                                    if not page_token: break
                                except: break
                            
                            # Delete users
                            for user in all_users:
                                email = user.get('primaryEmail', '')
                                # Skip admin
                                if email.lower().startswith('admin') or 'administrator' in email.lower(): continue
                                
                                # Domain saving logic (simplified - rely on explicit map mostly)
                                if '@' in email:
                                    d = email.split('@')[1].lower()
                                
                                try:
                                    service.users().delete(userKey=email).execute()
                                    account_result['deleted_count'] += 1
                                except:
                                    account_result['failed_count'] += 1
                                    
                        except Exception as e:
                            account_result['error'] = str(e)
                        return account_result

                # Run deletion in parallel
                with ThreadPoolExecutor(max_workers=max(1, min(10, len(authenticated_accounts)))) as del_executor:
                    del_futures = {del_executor.submit(delete_account_users, acc): acc for acc in authenticated_accounts.keys()}
                    completed_count = 0
                    for future in as_completed(del_futures):
                        res = future.result()
                        all_results.append(res)
                        completed_count += 1
                        update_progress(task_id, completed_count, len(authenticated_accounts), "deleting", f"Processed {completed_count}/{len(authenticated_accounts)} accounts...")

                # Add failed auth to results
                for acc in account_list:
                    if acc not in authenticated_accounts:
                        all_results.append({'account': acc, 'authenticated': False, 'error': 'Authentication failed', 'deleted_count': 0, 'failed_count': 0})

                # Compile final results
                total_deleted = sum(r.get('deleted_count', 0) for r in all_results)
                total_failed = sum(r.get('failed_count', 0) for r in all_results)
                
                final_result = {
                    'success': True,
                    'total_accounts': len(all_results),
                    'total_deleted': total_deleted,
                    'total_failed': total_failed,
                    'results': all_results,
                    'saved_domains': saved_domains_list,
                    'failed_domains': failed_domains_list
                }
                
                update_progress(task_id, 100, 100, "completed", "Bulk deletion finished!", final_result)
                app.logger.info(f"[BULK DELETE TASK {task_id}] COMPLETED")
                
            except Exception as e:
                app.logger.error(f"[BULK DELETE TASK {task_id}] CRASHED: {e}")
                app.logger.error(traceback.format_exc())
                update_progress(task_id, 0, 0, "error", f"Task failed: {str(e)}")

    # Start thread
    threading.Thread(target=background_task, args=(task_id, accounts)).start()
    
    return jsonify({'success': True, 'task_id': task_id})


# [DUPLICATE REMOVED] - This function was accidentally duplicated.
# Removing the route decorator to fix startup error.
def _unused_duplicate_api_bulk_retrieve_account_users():
    """
    Authenticate and retrieve all users from multiple accounts in bulk.
    Returns users in email:password format (password from app passwords if available).
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    
    try:
        data = request.get_json()
        accounts = data.get('accounts', [])
        account_passwords = data.get('account_passwords', {})  # Account -> password mapping
        
        if not accounts or len(accounts) == 0:
            return jsonify({'success': False, 'error': 'No accounts provided'})
        
        app.logger.info(f"[BULK RETRIEVE] Starting bulk user retrieval for {len(accounts)} account(s)")
        if account_passwords:
            app.logger.info(f"[BULK RETRIEVE] Received passwords for {len(account_passwords)} account(s)")
        
        # Step 1: Authenticate all accounts in parallel
        def authenticate_account(account_name):
            """Authenticate a single account - supports both ServiceAccount and GoogleAccount"""
            with app.app_context():
                try:
                    # Try ServiceAccount first (admin_email or name)
                    from database import ServiceAccount
                    service_account = ServiceAccount.query.filter_by(admin_email=account_name).first()
                    if not service_account:
                        service_account = ServiceAccount.query.filter_by(name=account_name).first()
                    
                    if service_account:
                        # Use GoogleDomainsService for Service Account
                        from services.google_domains_service import GoogleDomainsService
                        try:
                            domains_service = GoogleDomainsService(service_account.name)
                            admin_service = domains_service._get_admin_service()
                            return {'account': account_name, 'authenticated': True, 'service': admin_service, 'is_service_account': True}
                        except Exception as sa_err:
                            app.logger.error(f"[BULK RETRIEVE] [{account_name}] ServiceAccount auth error: {sa_err}")
                            return {'account': account_name, 'authenticated': False, 'error': f'ServiceAccount auth failed: {str(sa_err)}'}
                    
                    # Fallback to GoogleAccount (legacy OAuth)
                    account_db = GoogleAccount.query.filter_by(account_name=account_name).first()
                    if not account_db:
                        return {'account': account_name, 'authenticated': False, 'error': 'Account not found in database (checked ServiceAccount and GoogleAccount)'}
                    
                    auth_success = authenticate_without_session(account_name)
                    if not auth_success:
                        return {'account': account_name, 'authenticated': False, 'error': 'Failed to authenticate. Please ensure authenticator is saved.'}
                    
                    service = get_service_without_session(account_name)
                    if not service:
                        return {'account': account_name, 'authenticated': False, 'error': 'Failed to get service'}
                    
                    return {'account': account_name, 'authenticated': True, 'service': service}
                except Exception as e:
                    app.logger.error(f"[BULK RETRIEVE] [{account_name}] Authentication error: {e}")
                    return {'account': account_name, 'authenticated': False, 'error': str(e)}
        
        authenticated_accounts = {}
        if not accounts:
            return jsonify({'success': False, 'error': 'No accounts provided'}), 400
        with ThreadPoolExecutor(max_workers=max(1, min(10, len(accounts)))) as auth_executor:
            auth_futures = {auth_executor.submit(authenticate_account, account): account for account in accounts}
            
            for future in as_completed(auth_futures):
                account = auth_futures[future]
                try:
                    auth_result = future.result()
                    if auth_result['authenticated']:
                        authenticated_accounts[account] = auth_result
                    else:
                        app.logger.error(f"[BULK RETRIEVE] [{account}] Authentication failed: {auth_result.get('error', 'Unknown error')}")
                except Exception as e:
                    app.logger.error(f"[BULK RETRIEVE] [{account}] Authentication exception: {e}")
        
        app.logger.info(f"[BULK RETRIEVE] Authenticated {len(authenticated_accounts)}/{len(accounts)} account(s)")
        
        # Step 2: Retrieve users in parallel across all authenticated accounts
        # Make account_passwords accessible in the closure
        account_passwords_dict = account_passwords  # Store in a variable accessible to the closure
        
        def retrieve_account_users(account_name):
            """Retrieve all users from a single account"""
            with app.app_context():
                account_result = {
                    'account': account_name,
                    'authenticated': False,
                    'users': [],
                    'error': None
                }
                
                try:
                    if account_name not in authenticated_accounts:
                        account_result['error'] = 'Account was not authenticated'
                        return account_result
                    
                    auth_data = authenticated_accounts[account_name]
                    service = auth_data['service']
                    account_result['authenticated'] = True
                    
                    # Get the password for this account (from account_passwords mapping)
                    account_password = account_passwords_dict.get(account_name, None)
                    if account_password:
                        app.logger.info(f"[BULK RETRIEVE] [{account_name}] ✓ Using stored password for all users (length: {len(account_password)})")
                    else:
                        app.logger.warning(f"[BULK RETRIEVE] [{account_name}] ⚠️ No password found in account_passwords dict. Available accounts: {list(account_passwords_dict.keys())}")
                        app.logger.warning(f"[BULK RETRIEVE] [{account_name}] Will try to find app passwords from database")
                    
                    app.logger.info(f"[BULK RETRIEVE] [{account_name}] Retrieving users...")
                    
                    # Get all users with pagination
                    all_users = []
                    page_token = None
                    
                    while True:
                        try:
                            if page_token:
                                users_result = service.users().list(
                                    customer='my_customer',
                                    maxResults=500,
                                    pageToken=page_token
                                ).execute()
                            else:
                                users_result = service.users().list(
                                    customer='my_customer',
                                    maxResults=500
                                ).execute()
                            
                            users = users_result.get('users', [])
                            all_users.extend(users)
                            
                            page_token = users_result.get('nextPageToken')
                            if not page_token:
                                break
                        except Exception as e:
                            app.logger.error(f"[BULK RETRIEVE] [{account_name}] Error retrieving users: {e}")
                            break
                    
                    app.logger.info(f"[BULK RETRIEVE] [{account_name}] Found {len(all_users)} user(s)")
                    
                    # Use account password for all users, or try to find app passwords from database
                    for user in all_users:
                        email = user.get('primaryEmail', '')
                        if not email:
                            continue
                        
                        # Skip admin accounts
                        if email.lower().startswith('admin') or 'administrator' in email.lower():
                            continue
                        
                        # Use account password if available, otherwise try to find app password from database
                        user_password = account_password
                        if not user_password:
                            try:
                                user_app_password = UserAppPassword.query.filter_by(user_email=email).first()
                                if user_app_password:
                                    user_password = user_app_password.app_password
                                    app.logger.debug(f"[BULK RETRIEVE] [{account_name}] Found app password from DB for {email}")
                            except Exception as db_err:
                                app.logger.debug(f"[BULK RETRIEVE] [{account_name}] Could not get app password for {email}: {db_err}")
                        
                        # Log if password is missing
                        if not user_password:
                            app.logger.warning(f"[BULK RETRIEVE] [{account_name}] ⚠️ No password available for {email}")
                        
                        account_result['users'].append({
                            'email': email,
                            'password': user_password or '',  # Use account password or empty if not found
                            'app_password': user_password or '',
                            'first_name': user.get('name', {}).get('givenName', ''),
                            'last_name': user.get('name', {}).get('familyName', '')
                        })
                    
                    app.logger.info(f"[BULK RETRIEVE] [{account_name}] ✓ Retrieved {len(account_result['users'])} user(s) with password: {'Yes' if account_password else 'No (from DB)'}")
                    
                except Exception as e:
                    account_result['error'] = str(e)
                    app.logger.error(f"[BULK RETRIEVE] [{account_name}] ✗✗✗ Error: {e}")
                    app.logger.error(traceback.format_exc())
                
                return account_result
        
        # Retrieve users from all authenticated accounts in parallel
        all_results = []
        if not authenticated_accounts:
            return jsonify({'success': False, 'error': 'No authenticated accounts to retrieve users from', 'results': []})
        with ThreadPoolExecutor(max_workers=max(1, min(10, len(authenticated_accounts)))) as executor:
            retrieve_futures = {executor.submit(retrieve_account_users, account): account for account in authenticated_accounts.keys()}
            
            for future in as_completed(retrieve_futures):
                account = retrieve_futures[future]
                try:
                    result = future.result()
                    all_results.append(result)
                except Exception as e:
                    app.logger.error(f"[BULK RETRIEVE] [{account}] Exception: {e}")
                    all_results.append({
                        'account': account,
                        'authenticated': False,
                        'users': [],
                        'error': str(e)
                    })
        
        # Add results for accounts that failed authentication
        for account in accounts:
            if account not in authenticated_accounts:
                all_results.append({
                    'account': account,
                    'authenticated': False,
                    'users': [],
                    'error': 'Authentication failed'
                })
        
        # Calculate totals
        total_accounts = len(all_results)
        total_users = sum(len(r.get('users', [])) for r in all_results)
        total_failed = sum(1 for r in all_results if not r.get('authenticated'))
        
        app.logger.info(f"[BULK RETRIEVE] ✓✓✓ Bulk retrieval completed: {total_users} users retrieved from {total_accounts} accounts")
        
        return jsonify({
            'success': True,
            'message': 'Bulk user retrieval process completed.',
            'total_accounts': total_accounts,
            'total_users': total_users,
            'total_failed': total_failed,
            'results': all_results
        })
        
    except Exception as e:
        app.logger.error(f"Error in bulk user retrieval: {e}")
        app.logger.error(traceback.format_exc())
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/api/update-user-passwords', methods=['POST'])
@login_required
def api_update_user_passwords():
    # Set timeout for this endpoint (10 minutes)
    import signal
    
    def timeout_handler(signum, frame):
        raise TimeoutError("Request timed out after 30 minutes")
    
    signal.signal(signal.SIGALRM, timeout_handler)
    signal.alarm(1800)  # 30 minutes timeout
    
    try:
        data = request.get_json()
        users = data.get('users', [])
        new_password = data.get('new_password')

        if not users or len(users) == 0:
            return jsonify({'success': False, 'error': 'No users provided'})

        if not new_password or len(new_password) < 8:
            return jsonify({'success': False, 'error': 'Password must be at least 8 characters long'})

        # Sanitize password - remove any potentially problematic characters
        import re
        new_password = re.sub(r'[^\w\-_!@#$%^&*()+=]', '', new_password)
        
        if not new_password.strip():
            return jsonify({'success': False, 'error': 'Password cannot be empty after sanitization'})

        # Limit the number of users for performance
        if len(users) > 10000:
            return jsonify({'success': False, 'error': 'Maximum 10000 users allowed per batch for performance'})

        result = google_api.update_user_passwords(users, new_password)
        signal.alarm(0)  # Cancel timeout
        return jsonify(result)

    except Exception as e:
        signal.alarm(0)  # Cancel timeout
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/get-domain-info', methods=['GET'])
@login_required
def api_get_domain_info():
    try:
        # Get live domains from Google
        result = google_api.get_domain_info()
        live_domains = result.get('domains', []) if result.get('success') else []
        
        # Get used domains from database
        db_used_domains = UsedDomain.query.filter_by(ever_used=True).all()
        
        # Create a set of live domain names for easy lookup
        live_domain_names = {d.get('domainName') for d in live_domains}
        
        # Merge DB domains if they aren't in the live list
        for db_domain in db_used_domains:
            if db_domain.domain_name not in live_domain_names:
                # Add domain from DB to the list
                live_domains.append({
                    'domainName': db_domain.domain_name,
                    'verified': db_domain.is_verified,
                    'isPrimary': False, # DB-only domains are likely not primary
                    'source': 'database', # Flag to indicate source
                    'user_count': db_domain.user_count
                })
        
        # Sort domains by name
        live_domains.sort(key=lambda x: x.get('domainName', ''))
        
        return jsonify({'success': True, 'domains': live_domains})
    except Exception as e:
        app.logger.error(f"Error fetching domain info: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/add-domain-alias', methods=['POST'])
@login_required
def api_add_domain_alias():
    try:
        data = request.get_json()
        domain_alias = data.get('domain_alias')

        if not domain_alias:
            return jsonify({'success': False, 'error': 'Domain alias is required'})

        result = google_api.add_domain_alias(domain_alias)
        return jsonify(result)

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/delete-domain', methods=['POST'])
@login_required
def api_delete_domain():
    try:
        data = request.get_json()
        domain_name = data.get('domain_name')

        if not domain_name:
            return jsonify({'success': False, 'error': 'Domain name is required'})

        result = google_api.delete_domain(domain_name)
        return jsonify(result)

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/retrieve-users', methods=['POST'])
@login_required
def api_retrieve_users():
    """Retrieve all users from the authenticated Google account"""
    try:
        # Check if user is authenticated
        if 'current_account_name' not in session:
            return jsonify({'success': False, 'error': 'No account authenticated. Please authenticate first.'})
        
        # Get the current authenticated account
        account_name = session.get('current_account_name')
        
        # First, try to authenticate using saved tokens if service is not available
        if not google_api.service:
            # Check if we have valid tokens for this account
            if google_api.is_token_valid(account_name):
                success = google_api.authenticate_with_tokens(account_name)
                if not success:
                    return jsonify({'success': False, 'error': 'Failed to authenticate with saved tokens. Please re-authenticate.'})
            else:
                return jsonify({'success': False, 'error': 'No valid tokens found. Please re-authenticate.'})
        
        # Support batched mode to avoid timeouts
        # Client can pass { mode: 'batched', page_token, max_pages }
        req = request.get_json(silent=True) or {}
        mode = req.get('mode')
        page_token = req.get('page_token')
        max_pages = int(req.get('max_pages') or 5)

        try:
            if mode == 'batched':
                result = google_api.get_users_batch(page_token=page_token, max_pages=max_pages)
                if not result['success']:
                    return jsonify({'success': False, 'error': result.get('error', 'Unknown error')})

                users = result['users']
                return jsonify({
                    'success': True,
                    'users': [
                        {
                            'email': u.get('primaryEmail', ''),
                            'first_name': u.get('name', {}).get('givenName', ''),
                            'last_name': u.get('name', {}).get('familyName', ''),
                            'admin': u.get('isAdmin', False),
                            'suspended': u.get('suspended', False)
                        } for u in users
                    ],
                    'total_count': len(users),
                    'next_page_token': result.get('next_page_token'),
                    'fetched_pages': result.get('fetched_pages')
                })

            # Fallback: full retrieval (may be long)
            result = google_api.get_users()
            
            if not result['success']:
                return jsonify({'success': False, 'error': result['error']})
            
            users = result['users']
            
            # Format user data
            formatted_users = []
            for user in users:
                user_data = {
                    'email': user.get('primaryEmail', ''),
                    'first_name': user.get('name', {}).get('givenName', ''),
                    'last_name': user.get('name', {}).get('familyName', ''),
                    'admin': user.get('isAdmin', False),
                    'suspended': user.get('suspended', False)
                }
                formatted_users.append(user_data)
            
            return jsonify({
                'success': True,
                'users': formatted_users,
                'total_count': len(formatted_users)
            })
            
        except Exception as api_error:
            logging.error(f"Google API error: {api_error}")
            return jsonify({'success': False, 'error': f'Failed to retrieve users: {str(api_error)}'})
            
    except Exception as e:
        logging.error(f"Retrieve users error: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/update-all-passwords', methods=['POST'])
@login_required
def api_update_all_passwords():
    """Update passwords for multiple users"""
    try:
        data = request.get_json()
        password = data.get('password')
        user_emails = data.get('user_emails', [])
        
        if not password or len(password) < 8:
            return jsonify({'success': False, 'error': 'Password must be at least 8 characters'})
        
        if not user_emails:
            return jsonify({'success': False, 'error': 'No user emails provided'})
        
        # Check if user is authenticated
        if 'current_account_name' not in session:
            return jsonify({'success': False, 'error': 'No account authenticated. Please authenticate first.'})
        
        # Get the current authenticated account
        account_name = session.get('current_account_name')
        
        # First, try to authenticate using saved tokens if service is not available
        if not google_api.service:
            # Check if we have valid tokens for this account
            if google_api.is_token_valid(account_name):
                success = google_api.authenticate_with_tokens(account_name)
                if not success:
                    return jsonify({'success': False, 'error': 'Failed to authenticate with saved tokens. Please re-authenticate.'})
            else:
                return jsonify({'success': False, 'error': 'No valid tokens found. Please re-authenticate.'})
        
        successful_emails = []
        failed_details = []
        
        for email in user_emails:
            try:
                # Update user password in Google Admin Directory API
                user_body = {
                    'password': password,
                    'changePasswordAtNextLogin': False
                }
                
                google_api.service.users().update(userKey=email, body=user_body).execute()
                successful_emails.append(email)
                
            except Exception as user_error:
                failed_details.append({
                    'email': email,
                    'error': str(user_error)
                })
        
        return jsonify({
            'success': True,
            'message': f'Password update completed. {len(successful_emails)} successful, {len(failed_details)} failed.',
            'successful_emails': successful_emails,
            'failed_details': failed_details
        })
        
    except Exception as e:
        logging.error(f"Update all passwords error: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/calculate-domain-users', methods=['POST'])
@login_required
def api_calculate_domain_users():
    """Calculate user counts for specific domains to avoid timeout"""
    try:
        if 'current_account_name' not in session:
            return jsonify({'success': False, 'error': 'No account authenticated. Please authenticate first.'})
        
        account_name = session.get('current_account_name')
        
        # Check if we have valid tokens for this account
        if not google_api.service:
            if google_api.is_token_valid(account_name):
                success = google_api.authenticate_with_tokens(account_name)
                if not success:
                    return jsonify({'success': False, 'error': 'Failed to authenticate with saved tokens. Please re-authenticate.'})
            else:
                return jsonify({'success': False, 'error': 'No valid tokens found. Please re-authenticate.'})
        
        req = request.get_json(silent=True) or {}
        domain_names = req.get('domains', [])
        
        if not domain_names:
            return jsonify({'success': False, 'error': 'No domains provided'})
        
        # Get user counts for specific domains only
        domain_user_counts = {}
        
        # Fetch users in batches and filter by the requested domains
        user_page_token = None
        total_users = 0
        
        while True:
            try:
                if user_page_token:
                    users_result = google_api.service.users().list(
                        customer='my_customer',
                        maxResults=500,
                        pageToken=user_page_token
                    ).execute()
                else:
                    users_result = google_api.service.users().list(
                        customer='my_customer',
                        maxResults=500
                    ).execute()
                
                users = users_result.get('users', [])
                total_users += len(users)
                
                # Count users for the requested domains only
                for user in users:
                    email = user.get('primaryEmail', '')
                    if email and '@' in email:
                        domain = email.split('@')[1]
                        if domain in domain_names:
                            domain_user_counts[domain] = domain_user_counts.get(domain, 0) + 1
                
                user_page_token = users_result.get('nextPageToken')
                if not user_page_token:
                    break
                    
            except Exception as e:
                logging.warning(f"Failed to retrieve users page: {e}")
                break
        
        logging.info(f"Calculated user counts for {len(domain_names)} domains from {total_users} total users")
        
        return jsonify({
            'success': True,
            'domain_user_counts': domain_user_counts
        })
        
    except Exception as e:
        logging.error(f"Calculate domain users error: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/retrieve-domains-for-account', methods=['POST'])
@login_required
def api_retrieve_domains_for_account():
    """Retrieve all domains for a specific account (for bulk account management)"""
    try:
        data = request.get_json() or {}
        account_email = data.get('account_email', '').strip()
        
        if not account_email:
            return jsonify({'success': False, 'error': 'Account email is required'})
        
        # Try to find account - check ServiceAccount first, then GoogleAccount
        from database import GoogleAccount, ServiceAccount
        
        service_account = None
        google_account = None
        account_name = None
        
        # Try to find by admin_email in ServiceAccount (most common case now)
        service_account = ServiceAccount.query.filter_by(admin_email=account_email).first()
        
        if not service_account:
            # Try to find by name in ServiceAccount
            service_account = ServiceAccount.query.filter_by(name=account_email).first()
        
        if not service_account:
            # Try partial match on admin_email
            service_account = ServiceAccount.query.filter(ServiceAccount.admin_email.contains(account_email.split('@')[0])).first()
        
        if service_account:
            account_name = service_account.name
            logging.info(f"Found ServiceAccount: {account_name} for email {account_email}")
            
            # Use GoogleDomainsService for Service Account
            from services.google_domains_service import GoogleDomainsService
            try:
                domains_service = GoogleDomainsService(account_name)
                admin_service = domains_service._get_admin_service()
                
                # Get domains using Admin SDK
                domains_result = admin_service.domains().list(customer='my_customer').execute()
                all_domains = domains_result.get('domains', [])
                
                # Get users to calculate domain usage
                all_users = []
                page_token = None
                while True:
                    try:
                        if page_token:
                            users_result = admin_service.users().list(
                                customer='my_customer',
                                maxResults=500,
                                pageToken=page_token
                            ).execute()
                        else:
                            users_result = admin_service.users().list(
                                customer='my_customer',
                                maxResults=500
                            ).execute()
                        
                        users = users_result.get('users', [])
                        all_users.extend(users)
                        page_token = users_result.get('nextPageToken')
                        if not page_token:
                            break
                    except Exception as users_err:
                        logging.warning(f"Could not fetch users: {users_err}")
                        break
                
                # Calculate user count per domain
                domain_user_counts = {}
                for user in all_users:
                    email = user.get('primaryEmail', '')
                    if email and '@' in email:
                        domain = email.split('@')[1].lower()
                        domain_user_counts[domain] = domain_user_counts.get(domain, 0) + 1
                
                # Format domains with status information - CONDITIONAL DB SAVING
                from database import UsedDomain, db
                formatted_domains = []
                
                # Strict Filtering: Only include verified domains from API
                for domain in all_domains:
                    domain_name = domain.get('domainName', '')
                    if not domain_name:
                        continue
                    
                    is_verified = domain.get('verified', False)
                    # Strict Verified Check
                    if not is_verified or (str(is_verified).lower() != 'true' and is_verified is not True):
                        continue

                    # Use lowercase for lookup
                    domain_name_lower = domain_name.lower()
                    
                    user_count = domain_user_counts.get(domain_name_lower, 0)
                    
                    # Logic:
                    # 1. If users > 0: Status 'in_use', SAVE/UPDATE to DB (ever_used=True)
                    # 2. If users == 0 AND in DB (ever_used=True): Status 'used'
                    # 3. If users == 0 AND NOT in DB: Status 'available', DO NOT SAVE
                    
                    # DEBUG: Log what we're looking up
                    logging.info(f"[LOAD DOMAINS] Looking up domain: '{domain_name_lower}' (user_count={user_count})")
                    
                    domain_record = UsedDomain.query.filter_by(domain_name=domain_name_lower).first()
                    ever_used_db = getattr(domain_record, 'ever_used', False) if domain_record else False
                    
                    # DEBUG: Log what we found
                    if domain_record:
                        logging.info(f"[LOAD DOMAINS] FOUND in DB: domain='{domain_record.domain_name}', ever_used={ever_used_db}")
                    else:
                        logging.info(f"[LOAD DOMAINS] NOT FOUND in DB: '{domain_name_lower}'")
                        # Also check total count of UsedDomain records
                        total_count = UsedDomain.query.count()
                        logging.info(f"[LOAD DOMAINS] Total UsedDomain records in DB: {total_count}")
                        # List first 5 domains in DB
                        first_five = UsedDomain.query.limit(5).all()
                        for d in first_five:
                            logging.info(f"[LOAD DOMAINS] DB has domain: '{d.domain_name}' (ever_used={d.ever_used})")
                    
                    if user_count > 0:
                        status = 'in_use'
                        status_text = 'IN USE'
                        status_color = 'purple'
                        
                        # SAVE/UPDATE to DB
                        try:
                            if domain_record:
                                domain_record.user_count = user_count
                                domain_record.is_verified = True
                                try:
                                    domain_record.ever_used = True
                                except:
                                    pass
                                domain_record.updated_at = db.func.current_timestamp()
                            else:
                                try:
                                    new_domain = UsedDomain(domain_name=domain_name_lower, user_count=user_count, is_verified=True, ever_used=True)
                                except:
                                    new_domain = UsedDomain(domain_name=domain_name_lower, user_count=user_count, is_verified=True)
                                db.session.add(new_domain)
                            db.session.commit()
                        except Exception as e:
                            logging.warning(f"Failed to save domain {domain_name_lower}: {e}")
                            db.session.rollback()

                    elif domain_record and ever_used_db:
                        status = 'used'
                        status_text = 'USED'
                        status_color = 'red'
                    else:
                        status = 'available'
                        status_text = 'AVAILABLE'
                        status_color = 'green'
                    
                    formatted_domains.append({
                        'domain_name': domain_name,
                        'status': status,
                        'status_text': status_text,
                        'status_color': status_color,
                        'user_count': user_count,
                        'ever_used': ever_used_db or (user_count > 0)
                    })
                
                logging.info(f"Retrieved {len(formatted_domains)} domains for ServiceAccount {account_name}")
                
                return jsonify({
                    'success': True,
                    'domains': formatted_domains,
                    'count': len(formatted_domains)
                })
                
            except Exception as sa_err:
                logging.error(f"Error using ServiceAccount {account_name}: {sa_err}")
                return jsonify({'success': False, 'error': f'Failed to retrieve domains: {str(sa_err)}'})
        
        # Fallback: try GoogleAccount (legacy OAuth)
        google_account = GoogleAccount.query.filter_by(account_name=account_email).first()
        
        if not google_account:
            google_account = GoogleAccount.query.filter(GoogleAccount.account_name.contains(account_email.split('@')[0])).first()
        
        if not google_account:
            return jsonify({'success': False, 'error': f'Account {account_email} not found. Please check the email or authenticate the account first.'})
        
        account_name = google_account.account_name
        logging.info(f"Found GoogleAccount: {account_name} for email {account_email}")
        
        # Authenticate with this account's tokens
        if google_api.is_token_valid(account_name):
            success = google_api.authenticate_with_tokens(account_name)
            if not success:
                return jsonify({'success': False, 'error': f'Failed to authenticate with saved tokens for {account_name}. Please re-authenticate this account.'})
        else:
            return jsonify({'success': False, 'error': f'No valid tokens found for {account_name}. Please re-authenticate this account.'})
        
        # Get domains using batched mode
        all_domains = []
        next_token = None
        
        while True:
            result = google_api.get_domains_batch(page_token=next_token)
            if not result['success']:
                return jsonify({'success': False, 'error': result.get('error', 'Unknown error')})
            
            domains = result.get('domains', [])
            all_domains.extend(domains)
            
            next_token = result.get('next_page_token')
            if not next_token:
                break
        
        # Get user counts for domains to determine status
        from database import UsedDomain
        all_users = []
        try:
            # Get all users to calculate domain usage
            page_token = None
            while True:
                users_result = google_api.service.users().list(
                    customer='my_customer',
                    maxResults=500,
                    pageToken=page_token
                ).execute()
                users = users_result.get('users', [])
                all_users.extend(users)
                page_token = users_result.get('nextPageToken')
                if not page_token:
                    break
        except Exception as users_err:
            logger.warning(f"Could not fetch users for domain status: {users_err}")
        
        # Calculate user count per domain
        domain_user_counts = {}
        for user in all_users:
            email = user.get('primaryEmail', '')
            if email and '@' in email:
                domain = email.split('@')[1].lower()
                domain_user_counts[domain] = domain_user_counts.get(domain, 0) + 1
        
        # Format domains with status information - CONDITIONAL DB SAVING
        from database import UsedDomain, db
        formatted_domains = []
        
        for domain in all_domains:
            domain_name = domain.get('domainName', '')
            if not domain_name:
                continue
            
            is_verified = domain.get('verified', False)
            # Strict Verified Check
            if not is_verified or (str(is_verified).lower() != 'true' and is_verified is not True):
                continue

            # Use lowercase for lookup
            domain_name_lower = domain_name.lower()
            
            user_count = domain_user_counts.get(domain_name_lower, 0)
            
            # Logic:
            # 1. If users > 0: Status 'in_use', SAVE/UPDATE to DB (ever_used=True)
            # 2. If users == 0 AND in DB (ever_used=True): Status 'used'
            # 3. If users == 0 AND NOT in DB: Status 'available', DO NOT SAVE
            
            domain_record = UsedDomain.query.filter_by(domain_name=domain_name_lower).first()
            ever_used_db = getattr(domain_record, 'ever_used', False) if domain_record else False
            
            if user_count > 0:
                status = 'in_use'
                status_text = 'IN USE'
                status_color = 'purple'
                
                # SAVE/UPDATE to DB
                try:
                    if domain_record:
                        domain_record.user_count = user_count
                        domain_record.is_verified = True
                        try:
                            domain_record.ever_used = True
                        except:
                            pass
                        domain_record.updated_at = db.func.current_timestamp()
                    else:
                        try:
                            new_domain = UsedDomain(domain_name=domain_name_lower, user_count=user_count, is_verified=True, ever_used=True)
                        except:
                            new_domain = UsedDomain(domain_name=domain_name_lower, user_count=user_count, is_verified=True)
                        db.session.add(new_domain)
                    db.session.commit()
                except Exception as e:
                    logging.warning(f"Failed to save domain {domain_name_lower}: {e}")
                    db.session.rollback()

            elif domain_record and ever_used_db:
                status = 'used'
                status_text = 'USED'
                status_color = 'red'
            else:
                status = 'available'
                status_text = 'AVAILABLE'
                status_color = 'green'
            
            formatted_domains.append({
                'domain_name': domain_name,
                'status': status,
                'status_text': status_text,
                'status_color': status_color,
                'user_count': user_count,
                'ever_used': ever_used_db or (user_count > 0)
            })
        
        return jsonify({
            'success': True,
            'domains': formatted_domains,
            'count': len(formatted_domains)
        })
        
    except Exception as e:
        logger.error(f"Error retrieving domains for account: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)})

# @app.route('/api/mark-used-domains', methods=['POST'])
# @login_required
def api_mark_used_domains_deprecated():

    """
    Scans all configured accounts (Service Accounts and Google Accounts) for users,
    identifies their domains, and marks them as 'used' in the database.
    This acts as a repair/sync tool for domain persistence.
    """
    try:
        from database import ServiceAccount, GoogleAccount, UsedDomain
        
        updated_count = 0
        created_count = 0
        processed_domains = set()
        
        # 1. Process Service Accounts
        service_accounts = ServiceAccount.query.all()
        for sa in service_accounts:
            try:
                logging.info(f"Scanning Service Account: {sa.name}")
                from services.google_domains_service import GoogleDomainsService
                domains_service = GoogleDomainsService(sa.name)
                admin_service = domains_service._get_admin_service()
                
                # List all users
                page_token = None
                while True:
                    users_result = admin_service.users().list(
                        customer='my_customer',
                        maxResults=500,
                        pageToken=page_token
                    ).execute()
                    
                    users = users_result.get('users', [])
                    for user in users:
                        email = user.get('primaryEmail', '')
                        if email and '@' in email:
                            domain = email.split('@')[1].lower()
                            processed_domains.add(domain)
                    
                    page_token = users_result.get('nextPageToken')
                    if not page_token:
                        break
            except Exception as e:
                logging.error(f"Error scanning Service Account {sa.name}: {e}")
        
        # 2. Process Google Accounts (Legacy/OAuth)
        google_accounts = GoogleAccount.query.all()
        for ga in google_accounts:
            try:
                logging.info(f"Scanning Google Account: {ga.account_name}")
                if google_api.authenticate_with_tokens(ga.account_name):
                    page_token = None
                    while True:
                        users_result = google_api.service.users().list(
                            customer='my_customer',
                            maxResults=500,
                            pageToken=page_token
                        ).execute()
                        
                        users = users_result.get('users', [])
                        for user in users:
                            email = user.get('primaryEmail', '')
                            if email and '@' in email:
                                domain = email.split('@')[1].lower()
                                processed_domains.add(domain)
                        
                        page_token = users_result.get('nextPageToken')
                        if not page_token:
                            break
            except Exception as e:
                logging.error(f"Error scanning Google Account {ga.account_name}: {e}")

        # 3. Update Database
        logging.info(f"Found {len(processed_domains)} active domains: {processed_domains}")
        
        for domain in processed_domains:
            existing = UsedDomain.query.filter_by(domain_name=domain).first()
            if existing:
                if not existing.ever_used:
                    existing.ever_used = True
                    existing.updated_at = db.func.current_timestamp()
                    updated_count += 1
            else:
                new_domain = UsedDomain(
                    domain_name=domain,
                    ever_used=True,
                    is_verified=True, # Assume verified if it has users
                    user_count=0 # Will be updated by retrieve logic
                )
                db.session.add(new_domain)
                created_count += 1
        
        db.session.commit()
        
        # Get Stats
        total_used = UsedDomain.query.filter_by(ever_used=True).count()
        total_available = UsedDomain.query.filter_by(ever_used=False).count()
        
        return jsonify({
            'success': True, 
            'message': f"Successfully processed {len(processed_domains)} active domains.",
            'stats': {
                'updated': updated_count,
                'created': created_count,
                'total_used': total_used,
                'total_available': total_available
            }
        })

    except Exception as e:
        logger.error(f"Mark used domains error: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/retrieve-domains', methods=['POST'])
@login_required
def api_retrieve_domains():
    """Retrieve all domains from the authenticated Google account"""
    try:
        # Check if user is authenticated
        if 'current_account_name' not in session:
            return jsonify({'success': False, 'error': 'No account authenticated. Please authenticate first.'})
        
        # Get the current authenticated account
        account_name = session.get('current_account_name')
        
        # First, try to authenticate using saved tokens if service is not available
        if not google_api.service:
            # Check if we have valid tokens for this account
            if google_api.is_token_valid(account_name):
                success = google_api.authenticate_with_tokens(account_name)
                if not success:
                    return jsonify({'success': False, 'error': 'Failed to authenticate with saved tokens. Please re-authenticate.'})
            else:
                return jsonify({'success': False, 'error': 'No valid tokens found. Please re-authenticate.'})
        
        # Support batched mode to avoid timeouts with large domain lists
        req = request.get_json(silent=True) or {}
        mode = req.get('mode')
        page_token = req.get('page_token')

        try:
            if mode == 'batched':
                result = google_api.get_domains_batch(page_token=page_token)
                if not result['success']:
                    return jsonify({'success': False, 'error': result.get('error', 'Unknown error')})

                raw_domains = result['domains']
                
                # STRICT FILTERING: Only show VERIFIED domains
                # AND prioritize domains matching the account's domain suffix to avoid clutter
                # Logic: If account is 'user@example.com', we prefer domains ending in 'example.com'
                # But we must be careful not to hide valid domains if the user manages multiple.
                # However, the user explicitly asked for "real existing domains" (11 vs 200).
                # We will filter by VERIFIED status strictly.
                
                user_domain_suffix = account_name.split('@')[-1] if '@' in account_name else ''
                
                domains = []
                for d in raw_domains:
                    is_verified = d.get('verified', False)
                    d_name = d.get('domainName', '')
                    
                    # Strict Verified Check
                    if not is_verified:
                        continue
                        
                    # Optional: Heuristic to prioritize relevant domains
                    # If the list is huge (>50), we might want to filter by suffix
                    # But for now, let's just trust 'verified' is enough, BUT ensure d['verified'] is actually boolean True.
                    # Some APIs return strings 'true'/'false'.
                    if str(is_verified).lower() == 'true' or is_verified is True:
                         domains.append(d)
                
                if len(domains) < len(raw_domains):
                    app.logger.info(f"Filtered {len(raw_domains) - len(domains)} unverified domains. Keeping {len(domains)} verified domains.")
                
                # For batched mode, we'll get user counts separately to avoid timeout
                # OPTIMIZATION: Only query for domains we actually retrieved
                # This prevents loading the entire database (Crash Fix) and ensures we only show relevant data
                # Format domains with basic info first (no user counts to avoid timeout)
                # NO DB USAGE
                formatted_domains = []
                for domain in domains:
                    domain_name = domain.get('domainName', '')
                    is_verified = domain.get('verified', False)
                    
                    # Purely live status (user count unknown yet, assume available or check later)
                    # Since this is batched, we default to available/green until updated
                    status = 'available'
                    status_text = 'AVAILABLE'
                    status_color = '#4CAF50'  # Green
                    
                    formatted_domain = {
                        'domainName': domain_name,
                        'domain_name': domain_name,
                        'verified': is_verified,
                        'user_count': 0, # Will be calculated separately
                        'status': status,
                        'status_text': status_text,
                        'status_color': status_color,
                        'is_used': False,
                        'ever_used': False,
                        'needs_user_count': True
                    }
                    formatted_domains.append(formatted_domain)
                
                return jsonify({
                    'success': True,
                    'domains': formatted_domains,
                    'next_page_token': result.get('next_page_token'),
                    'total_fetched': result.get('total_fetched')
                })

            # Fallback: full domain retrieval (may be long for 500+ domains)
            result = google_api.get_domain_info()
            
            if not result['success']:
                return jsonify({'success': False, 'error': result['error']})
            
            raw_domains = result['domains']
            
            # STRICT FILTERING: Only show VERIFIED domains
            domains = []
            for d in raw_domains:
                is_verified = d.get('verified', False)
                if not is_verified:
                    continue
                if str(is_verified).lower() == 'true' or is_verified is True:
                     domains.append(d)
            
            app.logger.info(f"API Retrieve (Full): Found {len(raw_domains)} raw domains, filtered to {len(domains)} verified domains.")
            
            # Get all users to calculate domain usage (handle pagination for large user bases)
            all_users = []
            page_token = None
            
            while True:
                try:
                    if page_token:
                        users_result = google_api.service.users().list(
                            customer='my_customer',
                            maxResults=500,
                            pageToken=page_token
                        ).execute()
                    else:
                        users_result = google_api.service.users().list(
                            customer='my_customer',
                            maxResults=500
                        ).execute()
                    
                    users = users_result.get('users', [])
                    all_users.extend(users)
                    
                    page_token = users_result.get('nextPageToken')
                    if not page_token:
                        break
                        
                except Exception as e:
                    logging.warning(f"Failed to retrieve users page: {e}")
                    break
            
            logging.info(f"Retrieved {len(all_users)} total users for domain calculation")
            
            # Calculate user count per domain
            domain_user_counts = {}
            for user in all_users:
                email = user.get('primaryEmail', '')
                if email and '@' in email:
                    domain = email.split('@')[1]
                    domain_user_counts[domain] = domain_user_counts.get(domain, 0) + 1
            
            # Format domain data with new three-state system - CONDITIONAL DB SAVING
            from database import UsedDomain, db
            formatted_domains = []
            for domain in domains:
                domain_name = domain.get('domainName', '')
                user_count = domain_user_counts.get(domain_name, 0)
                
                # Logic:
                # 1. If users > 0: Status 'in_use', SAVE/UPDATE to DB (ever_used=True)
                # 2. If users == 0 AND in DB (ever_used=True): Status 'used'
                # 3. If users == 0 AND NOT in DB: Status 'available', DO NOT SAVE
                
                domain_record = UsedDomain.query.filter_by(domain_name=domain_name).first()
                ever_used_db = getattr(domain_record, 'ever_used', False) if domain_record else False
                
                # Determine domain status
                if user_count > 0:
                    status = 'in_use'  # Purple - currently has users
                    status_text = 'IN USE'
                    status_color = 'purple'
                    
                    # SAVE/UPDATE to DB
                    try:
                        if domain_record:
                            domain_record.user_count = user_count
                            domain_record.is_verified = domain.get('verified', False)
                            try:
                                domain_record.ever_used = True
                            except:
                                pass
                            domain_record.updated_at = db.func.current_timestamp()
                        else:
                            try:
                                new_domain = UsedDomain(domain_name=domain_name, user_count=user_count, is_verified=domain.get('verified', False), ever_used=True)
                            except:
                                new_domain = UsedDomain(domain_name=domain_name, user_count=user_count, is_verified=domain.get('verified', False))
                            db.session.add(new_domain)
                        db.session.commit()
                        logging.debug(f"Synced domain {domain_name}: {user_count} users, status={status}")
                    except Exception as db_error:
                        logging.warning(f"Failed to sync domain {domain_name} to database: {db_error}")
                        try:
                            db.session.rollback()
                        except:
                            pass

                elif domain_record and ever_used_db:
                    status = 'used'  # Orange - previously used but no current users
                    status_text = 'USED'
                    status_color = 'orange'
                else:
                    status = 'available'  # Green - never been used
                    status_text = 'AVAILABLE'
                    status_color = 'green'
                
                domain_data = {
                    'domain_name': domain_name,
                    'verified': domain.get('verified', False),
                    'user_count': user_count,
                    'status': status,
                    'status_text': status_text,
                    'status_color': status_color,
                    'is_used': user_count > 0,
                    'ever_used': ever_used_db or (user_count > 0)
                }
                formatted_domains.append(domain_data)
            
            return jsonify({
                'success': True,
                'domains': formatted_domains
            })
            
        except Exception as api_error:
            logging.error(f"Google API error: {api_error}")
            return jsonify({'success': False, 'error': f'Failed to retrieve domains: {str(api_error)}'})
            
    except Exception as e:
        logging.error(f"Retrieve domains error: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/get-domain-usage-stats', methods=['GET'])
@login_required
def api_get_domain_usage_stats():
    """Get domain usage statistics from database"""
    try:
        from database import UsedDomain
        domains = UsedDomain.query.all()
        
        # Sort domains by user count (descending) and then by name
        sorted_domains = sorted(domains, key=lambda x: (x.user_count, x.domain_name), reverse=True)
        
        # Calculate stats with new three-state system (handle missing ever_used column)
        in_use_domains = [d for d in domains if d.user_count > 0]
        used_domains = []
        available_domains = []
        
        for d in domains:
            if d.user_count == 0:
                try:
                    ever_used = getattr(d, 'ever_used', False)
                    if ever_used:
                        used_domains.append(d)
                    else:
                        available_domains.append(d)
                except:
                    # Column doesn't exist, treat as available
                    available_domains.append(d)
        
        stats = {
            'total_domains': len(domains),
            'in_use_domains': len(in_use_domains),
            'used_domains': len(used_domains),
            'available_domains': len(available_domains),
            'total_users': sum(d.user_count for d in domains),
            'domains': [
                {
                    'domain_name': d.domain_name,
                    'user_count': d.user_count,
                    'is_verified': d.is_verified,
                    'is_used': d.user_count > 0,  # For backward compatibility
                    'ever_used': getattr(d, 'ever_used', False),
                    'status': 'in_use' if d.user_count > 0 else ('used' if getattr(d, 'ever_used', False) else 'available'),
                    'status_text': 'IN USE' if d.user_count > 0 else ('USED' if getattr(d, 'ever_used', False) else 'AVAILABLE'),
                    'status_color': 'purple' if d.user_count > 0 else ('orange' if getattr(d, 'ever_used', False) else 'green'),
                    'last_updated': d.updated_at.isoformat() if d.updated_at else None
                }
                for d in sorted_domains
            ]
        }
        
        logging.info(f"Domain usage stats: {stats['total_domains']} domains, {stats['total_users']} users")
        
        return jsonify({'success': True, 'stats': stats})
        
    except Exception as e:
        logging.error(f"Get domain usage stats error: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/clear-old-domain-data', methods=['POST'])
@login_required
def api_clear_old_domain_data():
    """Reset all domain statuses from 'used' to 'available'"""
    try:
        from database import UsedDomain
        
        # Find all domains that are marked as "used" (ever_used=True and user_count=0)
        used_domains = UsedDomain.query.filter(
            UsedDomain.ever_used == True,
            UsedDomain.user_count == 0
        ).all()
        
        count = len(used_domains)
        
        # Reset ever_used to False for all used domains
        for domain in used_domains:
            domain.ever_used = False
            domain.updated_at = db.func.current_timestamp()
        
        db.session.commit()
        
        return jsonify({
            'success': True, 
            'message': f'Reset {count} domains from "used" to "available" status',
            'reset_count': count
        })
        
    except Exception as e:
        logging.error(f"Clear old domain data error: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/debug-auth-status', methods=['GET'])
@login_required
def api_debug_auth_status():
    """Debug endpoint to check authentication and service status"""
    try:
        current_account = session.get('current_account_name')
        service_available = google_api.service is not None
        token_valid = google_api.is_token_valid(current_account) if current_account else False
        
        debug_info = {
            'current_account': current_account,
            'service_available': service_available,
            'token_valid': token_valid,
            'session_id': session.get('session_id'),
            'session_keys': list(session.keys())
        }
        
        return jsonify({
            'success': True,
            'debug_info': debug_info
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/test-suspended-query', methods=['POST'])
@login_required
def api_test_suspended_query():
    """Test endpoint to debug suspended user queries"""
    try:
        current_account = session.get('current_account_name')
        if not current_account:
            return jsonify({'success': False, 'error': 'No account authenticated'})
        
        # Validate service
        if not google_api.validate_and_recreate_service(current_account):
            return jsonify({'success': False, 'error': 'Failed to establish Google API connection'})
        
        results = {}
        
        # Test 1: Direct suspended query
        try:
            suspended_result = google_api.service.users().list(
                customer='my_customer', 
                query='suspended:true',
                maxResults=10
            ).execute()
            results['direct_suspended_query'] = {
                'success': True,
                'count': len(suspended_result.get('users', [])),
                'users': [{'email': u.get('primaryEmail'), 'suspended': u.get('suspended')} for u in suspended_result.get('users', [])]
            }
        except Exception as e:
            results['direct_suspended_query'] = {'success': False, 'error': str(e)}
        
        # Test 2: Get all users and check suspension status
        try:
            all_users_result = google_api.service.users().list(
                customer='my_customer',
                maxResults=10
            ).execute()
            all_users = all_users_result.get('users', [])
            suspended_users = [u for u in all_users if u.get('suspended', False)]
            results['all_users_filter'] = {
                'success': True,
                'total_users': len(all_users),
                'suspended_count': len(suspended_users),
                'users': [{'email': u.get('primaryEmail'), 'suspended': u.get('suspended')} for u in all_users]
            }
        except Exception as e:
            results['all_users_filter'] = {'success': False, 'error': str(e)}
        
        return jsonify({
            'success': True,
            'account': current_account,
            'results': results
        })
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/load-suspended-users', methods=['POST'])
@login_required
def api_load_suspended_users():
    """Load suspended users from the authenticated Google account"""
    try:
        # Check if user is authenticated
        if 'current_account_name' not in session:
            logging.error("No current_account_name in session")
            return jsonify({'success': False, 'error': 'No account authenticated. Please authenticate first.'})
        
        # Get the current authenticated account
        account_name = session.get('current_account_name')
        logging.info(f"Loading suspended users for account: {account_name}")
        
        # Validate and recreate service if necessary
        if not google_api.validate_and_recreate_service(account_name):
            logging.error(f"Failed to validate or recreate service for account {account_name}")
            return jsonify({'success': False, 'error': 'Failed to establish Google API connection. Please re-authenticate.'})
        
        try:
            logging.info(f"Retrieving suspended users from Google Admin Directory API for {account_name}")
            
            # First try the direct query approach
            try:
                users_result = google_api.service.users().list(
                    customer='my_customer', 
                    query='suspended:true',
                    maxResults=500
                ).execute()
                suspended_users = users_result.get('users', [])
                logging.info(f"Direct query found {len(suspended_users)} suspended users for {account_name}")
            except Exception as query_error:
                logging.warning(f"Direct suspended query failed: {query_error}, trying alternative approach...")
                
                # Alternative approach: get all users and filter for suspended ones
                all_users_result = google_api.service.users().list(
                    customer='my_customer',
                    maxResults=500
                ).execute()
                
                all_users = all_users_result.get('users', [])
                suspended_users = [user for user in all_users if user.get('suspended', False)]
                logging.info(f"Alternative approach found {len(suspended_users)} suspended users out of {len(all_users)} total users for {account_name}")
            
            logging.info(f"Final count: {len(suspended_users)} suspended users for {account_name}")
            
            # Format suspended user data with full information
            formatted_suspended_users = []
            for user in suspended_users:
                if user.get('primaryEmail'):
                    user_data = {
                        'email': user.get('primaryEmail', ''),
                        'first_name': user.get('name', {}).get('givenName', ''),
                        'last_name': user.get('name', {}).get('familyName', ''),
                        'admin': user.get('isAdmin', False),
                        'suspended': True,  # These are all suspended users
                        'full_name': f"{user.get('name', {}).get('givenName', '')} {user.get('name', {}).get('familyName', '')}".strip()
                    }
                    formatted_suspended_users.append(user_data)
                    logging.debug(f"Formatted suspended user: {user_data['email']}")
            
            logging.info(f"Successfully formatted {len(formatted_suspended_users)} suspended users for {account_name}")
            
            # Add debug information to response
            response_data = {
                'success': True,
                'users': formatted_suspended_users,
                'total_count': len(formatted_suspended_users),
                'debug_info': {
                    'account_name': account_name,
                    'raw_suspended_count': len(suspended_users),
                    'formatted_count': len(formatted_suspended_users)
                }
            }
            
            return jsonify(response_data)
            
        except Exception as api_error:
            logging.error(f"Google API error for account {account_name}: {api_error}")
            return jsonify({'success': False, 'error': f'Failed to retrieve suspended users: {str(api_error)}'})
            
    except Exception as e:
        logging.error(f"Load suspended users error: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/change-domain-all-users', methods=['POST'])
@login_required
def api_change_domain_all_users():
    """Change domain for all users matching the current domain suffix"""
    try:
        # Check if user is authenticated
        if 'current_account_name' not in session:
            return jsonify({'success': False, 'error': 'No account authenticated. Please authenticate first.'})
        
        # Get the current authenticated account
        account_name = session.get('current_account_name')
        
        # First, try to authenticate using saved tokens if service is not available
        if not google_api.service:
            # Check if we have valid tokens for this account
            if google_api.is_token_valid(account_name):
                success = google_api.authenticate_with_tokens(account_name)
                if not success:
                    return jsonify({'success': False, 'error': 'Failed to authenticate with saved tokens. Please re-authenticate.'})
            else:
                return jsonify({'success': False, 'error': 'No valid tokens found. Please re-authenticate.'})
        
        # Get request data
        data = request.get_json()
        current_domain = data.get('current_domain', '').strip()
        new_domain = data.get('new_domain', '').strip()
        exclude_admin = data.get('exclude_admin', True)
        
        if not current_domain or not new_domain:
            return jsonify({'success': False, 'error': 'Both current and new domain are required'})
        
        if current_domain == new_domain:
            return jsonify({'success': False, 'error': 'Current and new domain cannot be the same'})
        
        try:
            # First try to get all users and filter by domain (more reliable)
            logging.info(f"Searching for users with domain: {current_domain}")
            
            # Get all users first (Google Admin API limit is 500)
            # Add timeout to prevent hanging
            all_users_result = google_api.service.users().list(
                customer='my_customer',
                maxResults=500
            ).execute()
            
            all_users = all_users_result.get('users', [])
            logging.info(f"Found {len(all_users)} total users")
            
            # Filter users by domain
            users = []
            for user in all_users:
                email = user.get('primaryEmail', '')
                if email and email.endswith(f"@{current_domain}"):
                    users.append(user)
            
            logging.info(f"Found {len(users)} users with domain {current_domain}")
            
            if not users:
                return jsonify({
                    'success': True,
                    'successful': 0,
                    'failed': 0,
                    'skipped': 0,
                    'message': f'No users found with domain {current_domain}'
                })
            
            # Update domain status in database IMMEDIATELY (before processing users)
            # This ensures domain status is saved even if the operation times out later
            try:
                from database import UsedDomain
                
                logging.info(f"Pre-updating domain status: {current_domain} → {new_domain}")
                
                # Mark old domain as used but with 0 current users
                old_domain_record = UsedDomain.query.filter_by(domain_name=current_domain).first()
                if old_domain_record:
                    old_domain_record.user_count = 0
                    try:
                        old_domain_record.ever_used = True
                    except:
                        pass  # Column doesn't exist yet
                    old_domain_record.updated_at = db.func.current_timestamp()
                else:
                    try:
                        old_domain_record = UsedDomain(
                            domain_name=current_domain,
                            user_count=0,
                            ever_used=True,
                            is_verified=True
                        )
                    except:
                        old_domain_record = UsedDomain(
                            domain_name=current_domain,
                            user_count=0,
                            is_verified=True
                        )
                    db.session.add(old_domain_record)
                
                # Mark new domain as currently in use (with estimated user count)
                new_domain_record = UsedDomain.query.filter_by(domain_name=new_domain).first()
                estimated_users = len(users)
                
                if new_domain_record:
                    new_domain_record.user_count = estimated_users
                    try:
                        new_domain_record.ever_used = True
                    except:
                        pass  # Column doesn't exist yet
                    new_domain_record.updated_at = db.func.current_timestamp()
                else:
                    try:
                        new_domain_record = UsedDomain(
                            domain_name=new_domain,
                            user_count=estimated_users,
                            ever_used=True,
                            is_verified=True
                        )
                    except:
                        new_domain_record = UsedDomain(
                            domain_name=new_domain,
                            user_count=estimated_users,
                            is_verified=True
                        )
                    db.session.add(new_domain_record)
                
                db.session.commit()
                logging.info(f"✅ Domain status pre-updated: {current_domain} (USED) → {new_domain} (IN USE)")
                
            except Exception as db_error:
                logging.error(f"ERROR: Failed to pre-update domain status: {db_error}")
                try:
                    db.session.rollback()
                except:
                    pass
            
            successful = 0
            failed = 0
            skipped = 0
            results = []
            
            # Process users in smaller batches to avoid timeouts
            batch_size = 5  # Reduced batch size for better performance
            total_users = len(users)
            
            # Add early response for large batches to prevent timeout
            if total_users > 50:
                logging.warning(f"Large batch detected ({total_users} users). Consider processing in smaller chunks.")
            
            for i, user in enumerate(users):
                try:
                    email = user.get('primaryEmail', '')
                    if not email:
                        continue
                    
                    # Check if user is admin (skip if exclude_admin is True)
                    if exclude_admin and user.get('isAdmin', False):
                        skipped += 1
                        results.append({
                            'email': email,
                            'skipped': True,
                            'reason': 'Admin user'
                        })
                        continue
                    
                    # Create new email with new domain
                    username = email.split('@')[0]
                    new_email = f"{username}@{new_domain}"
                    
                    # Update user's primary email
                    user_update = {
                        'primaryEmail': new_email
                    }
                    
                    logging.info(f"Updating user {i+1}/{total_users}: {email} → {new_email}")
                    
                    # Add timeout to the API call
                    google_api.service.users().update(
                        userKey=email,
                        body=user_update
                    ).execute()
                    
                    successful += 1
                    results.append({
                        'success': True,
                        'old_email': email,
                        'new_email': new_email
                    })
                    
                    logging.info(f"✅ Successfully updated user {i+1}/{total_users}: {email} → {new_email}")
                    
                    # Add small delay between API calls to avoid rate limiting
                    import time
                    time.sleep(0.1)
                    
                except Exception as user_error:
                    failed += 1
                    results.append({
                        'success': False,
                        'email': email,
                        'error': str(user_error)
                    })
                    logging.error(f"ERROR: Failed to update user {i+1}/{total_users} {email}: {user_error}")
                    
                    # Continue processing other users even if one fails
                    continue
            
            # Update final user count in database (optional - domain status already saved)
            try:
                from database import UsedDomain
                new_domain_record = UsedDomain.query.filter_by(domain_name=new_domain).first()
                if new_domain_record:
                    new_domain_record.user_count = successful
                    db.session.commit()
                    logging.info(f"✅ Updated final user count: {new_domain} = {successful} users")
            except Exception as db_error:
                logging.warning(f"Failed to update final user count: {db_error}")
                try:
                    db.session.rollback()
                except:
                    pass
            
            return jsonify({
                'success': True,
                'successful': successful,
                'failed': failed,
                'skipped': skipped,
                'results': results,
                'message': f'Domain change completed: {successful} users updated, {failed} failed, {skipped} skipped'
            })
            
        except Exception as api_error:
            logging.error(f"Google API error: {api_error}")
            return jsonify({'success': False, 'error': f'Failed to change domains: {str(api_error)}'})
            
    except Exception as e:
        logging.error(f"Change domain all users error: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/change-domain', methods=['POST'])
@login_required
def api_change_domain():
    """Change domain for specific users"""
    try:
        # Check if user is authenticated
        if 'current_account_name' not in session:
            return jsonify({'success': False, 'error': 'No account authenticated. Please authenticate first.'})
        
        # Get the current authenticated account
        account_name = session.get('current_account_name')
        
        # First, try to authenticate using saved tokens if service is not available
        if not google_api.service:
            # Check if we have valid tokens for this account
            if google_api.is_token_valid(account_name):
                success = google_api.authenticate_with_tokens(account_name)
                if not success:
                    return jsonify({'success': False, 'error': 'Failed to authenticate with saved tokens. Please re-authenticate.'})
            else:
                return jsonify({'success': False, 'error': 'No valid tokens found. Please re-authenticate.'})
        
        # Get request data
        data = request.get_json()
        old_domain = data.get('old_domain', '').strip()
        new_domain = data.get('new_domain', '').strip()
        user_emails = data.get('user_emails', [])
        
        if not old_domain or not new_domain:
            return jsonify({'success': False, 'error': 'Both old and new domain are required'})
        
        if old_domain == new_domain:
            return jsonify({'success': False, 'error': 'Old and new domain cannot be the same'})
        
        if not user_emails:
            return jsonify({'success': False, 'error': 'No user emails provided'})
        
        results = []
        successful = 0
        failed = 0
        
        try:
            for email in user_emails:
                try:
                    # Extract username from old email
                    if '@' not in email:
                        failed += 1
                        results.append({
                            'success': False,
                            'email': email,
                            'error': 'Invalid email format'
                        })
                        continue
                    
                    username = email.split('@')[0]
                    new_email = f"{username}@{new_domain}"
                    
                    # Update user's primary email
                    user_body = {
                        'primaryEmail': new_email
                    }
                    
                    google_api.service.users().update(userKey=email, body=user_body).execute()
                    
                    successful += 1
                    results.append({
                        'success': True,
                        'old_email': email,
                        'new_email': new_email
                    })
                    
                    logging.info(f"✅ Changed domain for user: {email} → {new_email}")
                    
                    # Add small delay to avoid API rate limits
                    import time
                    time.sleep(0.05)  # Reduced delay for better performance
                    
                    # Commit database changes periodically to prevent long transactions
                    if (i + 1) % 10 == 0:
                        try:
                            db.session.commit()
                            logging.info(f"Processed {i + 1}/{total_users} users...")
                        except:
                            pass
                    
                except Exception as user_error:
                    failed += 1
                    results.append({
                        'success': False,
                        'email': email,
                        'error': str(user_error)
                    })
                    logging.error(f"ERROR: Failed to update user {email}: {user_error}")
                    continue
            
            # Update domain usage in database
            try:
                from database import UsedDomain
                
                # Update old domain user count (decrease by successful changes)
                old_domain_record = UsedDomain.query.filter_by(domain_name=old_domain).first()
                if old_domain_record:
                    old_domain_record.user_count = max(0, old_domain_record.user_count - successful)
                    old_domain_record.updated_at = db.func.current_timestamp()
                
                # Update new domain user count (increase by successful changes)
                new_domain_record = UsedDomain.query.filter_by(domain_name=new_domain).first()
                if new_domain_record:
                    new_domain_record.user_count += successful
                    new_domain_record.updated_at = db.func.current_timestamp()
                else:
                    # Create new domain record
                    new_domain_record = UsedDomain(
                        domain_name=new_domain,
                        user_count=successful,
                        is_verified=True
                    )
                    db.session.add(new_domain_record)
                
                db.session.commit()
                logging.info(f"Updated domain usage: {old_domain} (-{successful}) → {new_domain} (+{successful})")
                
            except Exception as db_error:
                logging.warning(f"Failed to update domain usage in database: {db_error}")
                # Don't fail the entire operation for database update issues
            
            return jsonify({
                'success': True,
                'message': f'Domain change completed. {successful} successful, {failed} failed.',
                'successful': successful,
                'failed': failed,
                'results': results
            })
            
        except Exception as api_error:
            logging.error(f"Google API error during domain change: {api_error}")
            return jsonify({'success': False, 'error': f'Failed to change domains: {str(api_error)}'})
            
    except Exception as e:
        logging.error(f"Change domain error: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/debug-domain-users', methods=['POST'])
@login_required
def api_debug_domain_users():
    """Debug endpoint to check users for a specific domain"""
    try:
        # Check if user is authenticated
        if 'current_account_name' not in session:
            return jsonify({'success': False, 'error': 'No account authenticated. Please authenticate first.'})
        
        # Get the current authenticated account
        account_name = session.get('current_account_name')
        
        # First, try to authenticate using saved tokens if service is not available
        if not google_api.service:
            # Check if we have valid tokens for this account
            if google_api.is_token_valid(account_name):
                success = google_api.authenticate_with_tokens(account_name)
                if not success:
                    return jsonify({'success': False, 'error': 'Failed to authenticate with saved tokens. Please re-authenticate.'})
            else:
                return jsonify({'success': False, 'error': 'No valid tokens found. Please re-authenticate.'})
        
        data = request.get_json()
        domain = data.get('domain', '').strip()
        
        if not domain:
            return jsonify({'success': False, 'error': 'Domain is required'})
        
        try:
            # Get all users first (Google Admin API limit is 500)
            all_users_result = google_api.service.users().list(
                customer='my_customer',
                maxResults=500
            ).execute()
            
            all_users = all_users_result.get('users', [])
            
            # Filter users by domain
            domain_users = []
            for user in all_users:
                email = user.get('primaryEmail', '')
                if email and email.endswith(f"@{domain}"):
                    domain_users.append({
                        'email': email,
                        'name': user.get('name', {}),
                        'isAdmin': user.get('isAdmin', False),
                        'suspended': user.get('suspended', False)
                    })
            
            return jsonify({
                'success': True,
                'domain': domain,
                'total_users_found': len(all_users),
                'domain_users_found': len(domain_users),
                'domain_users': domain_users
            })
            
        except Exception as api_error:
            logging.error(f"Google API error: {api_error}")
            return jsonify({'success': False, 'error': f'Failed to retrieve users: {str(api_error)}'})
            
    except Exception as e:
        logging.error(f"Debug domain users error: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/mark-domain-used', methods=['POST'])
@login_required
def api_mark_domain_used():
    """Mark a domain as used in the database"""
    try:
        data = request.get_json()
        domain = data.get('domain', '').strip()
        
        if not domain:
            return jsonify({'success': False, 'error': 'Domain is required'})
        
        from database import UsedDomain
        
        # Find or create domain record
        domain_record = UsedDomain.query.filter_by(domain_name=domain).first()
        if domain_record:
            domain_record.user_count = max(domain_record.user_count, 1)  # At least 1 user
            domain_record.updated_at = db.func.current_timestamp()
        else:
            domain_record = UsedDomain(
                domain_name=domain,
                user_count=1,
                is_verified=True
            )
            db.session.add(domain_record)
        
        db.session.commit()
        
        return jsonify({
            'success': True,
            'message': f'Domain {domain} marked as used'
        })
        
    except Exception as e:
        logging.error(f"Mark domain used error: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/force-refresh-domains', methods=['POST'])
@login_required
def api_force_refresh_domains():
    """Force refresh domain data from Google Admin API and update database"""
    try:
        # Check if user is authenticated
        if 'current_account_name' not in session:
            return jsonify({'success': False, 'error': 'No account authenticated. Please authenticate first.'})
        
        # Get the current authenticated account
        account_name = session.get('current_account_name')
        
        # First, try to authenticate using saved tokens if service is not available
        if not google_api.service:
            # Check if we have valid tokens for this account
            if google_api.is_token_valid(account_name):
                success = google_api.authenticate_with_tokens(account_name)
                if not success:
                    return jsonify({'success': False, 'error': 'Failed to authenticate with saved tokens. Please re-authenticate.'})
            else:
                return jsonify({'success': False, 'error': 'No valid tokens found. Please re-authenticate.'})
        
        try:
            # Get all users with pagination
            all_users = []
            page_token = None
            
            while True:
                try:
                    if page_token:
                        users_result = google_api.service.users().list(
                            customer='my_customer',
                            maxResults=500,
                            pageToken=page_token
                        ).execute()
                    else:
                        users_result = google_api.service.users().list(
                            customer='my_customer',
                            maxResults=500
                        ).execute()
                    
                    users = users_result.get('users', [])
                    all_users.extend(users)
                    
                    page_token = users_result.get('nextPageToken')
                    if not page_token:
                        break
                        
                except Exception as e:
                    logging.warning(f"Failed to retrieve users page: {e}")
                    break
            
            logging.info(f"Force refresh: Retrieved {len(all_users)} total users")
            
            # Calculate user count per domain
            domain_user_counts = {}
            for user in all_users:
                email = user.get('primaryEmail', '')
                if email and '@' in email:
                    domain = email.split('@')[1]
                    domain_user_counts[domain] = domain_user_counts.get(domain, 0) + 1
            
            # Update database with real user counts
            from database import UsedDomain
            
            for domain_name, user_count in domain_user_counts.items():
                try:
                    existing_domain = UsedDomain.query.filter_by(domain_name=domain_name).first()
                    if existing_domain:
                        existing_domain.user_count = user_count
                        existing_domain.updated_at = db.func.current_timestamp()
                        logging.info(f"Updated domain {domain_name}: {user_count} users")
                    else:
                        new_domain = UsedDomain(
                            domain_name=domain_name,
                            user_count=user_count,
                            is_verified=True
                        )
                        db.session.add(new_domain)
                        logging.info(f"Added new domain {domain_name}: {user_count} users")
                except Exception as e:
                    logging.warning(f"Failed to update domain {domain_name}: {e}")
                    continue
            
            db.session.commit()
            
            return jsonify({
                'success': True,
                'message': f'Domain data refreshed successfully. Found {len(domain_user_counts)} domains with {len(all_users)} total users.',
                'domains_updated': len(domain_user_counts),
                'total_users': len(all_users)
            })
            
        except Exception as api_error:
            logging.error(f"Google API error during force refresh: {api_error}")
            return jsonify({'success': False, 'error': f'Failed to refresh domains: {str(api_error)}'})
            
    except Exception as e:
        logging.error(f"Force refresh domains error: {e}")
        return jsonify({'success': False, 'error': str(e)})

# Settings page route
@app.route('/settings')
@login_required
def settings():
    # Allow admin, mailer, and support roles to access settings
    allowed_roles = ['admin', 'mailer', 'support']
    if session.get('role') not in allowed_roles:
        flash('Access denied. Insufficient privileges.', 'error')
        return redirect(url_for('dashboard'))
    
    # Ensure AWS config table exists
    try:
        inspector = db.inspect(db.engine)
        if 'aws_config' not in inspector.get_table_names():
            db.create_all()
            app.logger.info("Created aws_config table")
    except Exception as e:
        app.logger.error(f"Error ensuring aws_config table exists: {e}")
    
    return render_template('settings.html', user=session.get('user'), role=session.get('role'))

# Server configuration API routes
@app.route('/api/get-server-config', methods=['GET'])
@login_required
def get_server_config():
    # Allow admin, mailer, and support to access server config
    allowed_roles = ['admin', 'mailer', 'support']
    if session.get('role') not in allowed_roles:
        return jsonify({'success': False, 'error': 'Access denied'})
    
    try:
        from database import ServerConfig
        config = ServerConfig.query.first()
        if config:
            return jsonify({
                'success': True,
                'config': {
                    'host': config.host,
                    'port': config.port,
                    'username': config.username,
                    'auth_method': config.auth_method,
                    'password': config.password if config.password else '',
                    'private_key': config.private_key if config.private_key else '',
                    'json_path': config.json_path,
                    'file_pattern': config.file_pattern,
                    'is_configured': config.is_configured,
                    'last_tested': config.last_tested.isoformat() if config.last_tested else None
                }
            })
        else:
            return jsonify({'success': True, 'config': None})
    except Exception as e:
        app.logger.error(f"Error getting server config: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/save-server-config', methods=['POST'])
@login_required
def save_server_config():
    # Allow admin, mailer, and support to save server config
    allowed_roles = ['admin', 'mailer', 'support']
    if session.get('role') not in allowed_roles:
        return jsonify({'success': False, 'error': 'Access denied'})
    
    try:
        data = request.get_json()
        
        # Validate required fields - updated for new structure
        required_fields = ['host', 'username']
        for field in required_fields:
            if not data.get(field):
                return jsonify({'success': False, 'error': f'Missing required field: {field}'})
        
        from database import ServerConfig
        
        # Get or create config
        config = ServerConfig.query.first()
        if not config:
            config = ServerConfig()
            db.session.add(config)
        
        # Update config
        config.host = data['host']
        config.port = data.get('port', 22)
        config.username = data['username']
        config.auth_method = data.get('auth_method', 'password')
        
        # Set fixed values for new directory structure
        config.json_path = "/home/brightmindscampus"  # Fixed base path
        config.file_pattern = "*.json"  # Fixed pattern
        
        # Handle authentication credentials
        if data['auth_method'] == 'password':
            config.password = data.get('password', '')
            config.private_key = None
        else:
            config.private_key = data.get('private_key', '')
            config.password = None
        
        config.is_configured = True
        config.updated_at = db.func.current_timestamp()
        
        db.session.commit()
        
        return jsonify({'success': True, 'message': 'Server configuration saved successfully'})
        
    except Exception as e:
        app.logger.error(f"Error saving server config: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/namecheap-config', methods=['GET'])
@login_required
def get_namecheap_config():
    """Get Namecheap configuration"""
    # Allow admin, mailer, and support to access Namecheap config
    allowed_roles = ['admin', 'mailer', 'support']
    if session.get('role') not in allowed_roles:
        return jsonify({'success': False, 'error': 'Access denied'})
    
    try:
        from database import NamecheapConfig
        config = NamecheapConfig.query.filter_by(is_configured=True).first()
        
        if config:
            return jsonify({
                'success': True,
                'config': {
                    'api_user': config.api_user,
                    'username': config.username,
                    'client_ip': config.client_ip,
                    'api_key_configured': bool(config.api_key)
                    # Do not return api_key for security
                }
            })
        else:
            return jsonify({'success': True, 'config': None})
            
    except Exception as e:
        app.logger.error(f"Error getting Namecheap config: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/namecheap-config', methods=['POST'])
@login_required
def save_namecheap_config():
    """Save Namecheap configuration"""
    # Allow admin, mailer, and support to save Namecheap config
    allowed_roles = ['admin', 'mailer', 'support']
    if session.get('role') not in allowed_roles:
        return jsonify({'success': False, 'error': 'Access denied'})
    
    try:
        data = request.get_json()
        
        from database import NamecheapConfig
        
        # Get existing configured record first
        config = NamecheapConfig.query.filter_by(is_configured=True).first()
        
        # If no configured record, get any record
        if not config:
            config = NamecheapConfig.query.first()
            
        # If still no record, create new one
        if not config:
            config = NamecheapConfig()
            db.session.add(config)
            
        app.logger.info(f"Updating Namecheap config. ID: {config.id if config.id else 'New'}")
            
        # Validate required fields
        # API Key is optional if we already have one configured
        if not data.get('api_key') and not config.api_key:
             return jsonify({'success': False, 'error': 'Missing required field: api_key'})
             
        required_fields = ['api_user', 'username', 'client_ip']
        for field in required_fields:
            if not data.get(field):
                return jsonify({'success': False, 'error': f'Missing required field: {field}'})
        
        # Update config
        config.api_user = data['api_user']
        # Only update API key if provided (and not empty string)
        if data.get('api_key'):
            config.api_key = data['api_key']
            app.logger.info("Namecheap API key updated")
        else:
            app.logger.info("Namecheap API key preserved (not provided in request)")
            
        config.username = data['username']
        config.client_ip = data['client_ip']
        config.is_configured = True
        config.updated_at = db.func.current_timestamp()
        
        db.session.commit()
        
        # Clean up any other configured records to avoid confusion
        try:
            other_configs = NamecheapConfig.query.filter(NamecheapConfig.id != config.id).all()
            for other in other_configs:
                db.session.delete(other)
            if other_configs:
                db.session.commit()
                app.logger.info(f"Cleaned up {len(other_configs)} duplicate Namecheap config records")
        except Exception as cleanup_error:
            app.logger.error(f"Error cleaning up duplicate configs: {cleanup_error}")
        
        app.logger.info(f"Namecheap configuration saved by {session.get('user')}")
        return jsonify({'success': True, 'message': 'Namecheap configuration saved successfully'})
        
    except Exception as e:
        app.logger.error(f"Error saving Namecheap config: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/test-server-connection', methods=['POST'])
@login_required
def test_server_connection():
    if session.get('role') != 'admin':
        return jsonify({'success': False, 'error': 'Admin privileges required'})
    
    try:
        data = request.get_json()
        
        # Validate required fields - updated for new structure
        required_fields = ['host', 'username']
        for field in required_fields:
            if not data.get(field):
                return jsonify({'success': False, 'error': f'Missing required field: {field}'})
        
        # Test SSH connection and file access
        import paramiko
        import tempfile
        import os
        
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        
        try:
            # Connect to server
            if data['auth_method'] == 'password':
                ssh.connect(
                    data['host'],
                    port=data.get('port', 22),
                    username=data['username'],
                    password=data.get('password', ''),
                    timeout=10
                )
            else:
                # Create temporary key file
                with tempfile.NamedTemporaryFile(mode='w', delete=False) as key_file:
                    key_file.write(data.get('private_key', ''))
                    key_file_path = key_file.name
                
                try:
                    private_key = paramiko.RSAKey.from_private_key_file(key_file_path)
                    ssh.connect(
                        data['host'],
                        port=data.get('port', 22),
                        username=data['username'],
                        pkey=private_key,
                        timeout=10
                    )
                finally:
                    os.unlink(key_file_path)
            
            # Test file access with new directory structure
            sftp = ssh.open_sftp()
            try:
                # Test the base directory structure: /home/brightmindscampus/
                base_dir = "/home/brightmindscampus"
                
                try:
                    # List directories in the base directory
                    account_dirs = sftp.listdir(base_dir)
                except FileNotFoundError:
                    ssh.close()
                    return jsonify({'success': False, 'error': f'Base directory not found: {base_dir}'})
                
                # Test a few account directories to find JSON files
                valid_accounts = []
                tested_accounts = 0
                max_test_accounts = 5  # Limit testing to avoid long delays
                
                for account_dir in account_dirs[:max_test_accounts]:
                    if '@' not in account_dir:  # Skip non-email directories
                        continue
                    
                    tested_accounts += 1
                    account_path = f"{base_dir}/{account_dir}"
                    
                    try:
                        # List files in the account directory
                        account_files = sftp.listdir(account_path)
                        
                        # Look for JSON files
                        import fnmatch
                        json_files = [f for f in account_files if fnmatch.fnmatch(f, '*.json')]
                        
                        if json_files:
                            # Test reading the first JSON file
                            json_filename = json_files[0]
                            file_path = f"{account_path}/{json_filename}"
                            
                        try:
                            with sftp.open(file_path, 'r') as f:
                                content = f.read()
                                json_data = json.loads(content)
                            
                            # Validate JSON structure
                            if 'installed' in json_data or 'web' in json_data:
                                valid_accounts.append({
                                    'account': account_dir,
                                    'json_file': json_filename,
                                    'has_credentials': True
                                })
                            else:
                                valid_accounts.append({
                                    'account': account_dir,
                                    'json_file': json_filename,
                                    'has_credentials': False
                                })
                        except Exception as e:
                            app.logger.warning(f"Invalid JSON file {file_path}: {e}")
                            continue
                    
                    except Exception as e:
                        app.logger.warning(f"Could not access account directory {account_path}: {e}")
                        continue
                
                ssh.close()
                
                if valid_accounts:
                    return jsonify({
                        'success': True,
                        'message': f'Connection successful. Found {len(valid_accounts)} account(s) with JSON files in {len(account_dirs)} total directories.',
                        'accounts_count': len(valid_accounts),
                        'total_dirs': len(account_dirs),
                        'tested_accounts': tested_accounts,
                        'sample_accounts': valid_accounts[:5]  # Return first 5 valid accounts
                    })
                else:
                    return jsonify({
                        'success': False,
                        'error': f'No valid JSON files found in any account directories. Checked {tested_accounts} directories.'
                })
                
            except Exception as e:
                ssh.close()
                return jsonify({'success': False, 'error': f'Failed to access directories: {str(e)}'})
                
        except Exception as e:
            return jsonify({'success': False, 'error': f'SSH connection failed: {str(e)}'})
            
    except Exception as e:
        app.logger.error(f"Error testing server connection: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/clear-server-config', methods=['POST'])
@login_required
def clear_server_config():
    if session.get('role') != 'admin':
        return jsonify({'success': False, 'error': 'Admin privileges required'})
    
    try:
        from database import ServerConfig
        config = ServerConfig.query.first()
        if config:
            db.session.delete(config)
            db.session.commit()
        
        return jsonify({'success': True, 'message': 'Server configuration cleared'})
        
    except Exception as e:
        app.logger.error(f"Error clearing server config: {e}")
        return jsonify({'success': False, 'error': str(e)})

# Helper function for SQLAlchemy-based backup
def create_sqlalchemy_backup(filepath, include_data):
    """Create a backup using SQLAlchemy when pg_dump fails"""
    try:
        app.logger.info("Creating SQLAlchemy-based backup...")
        app.logger.info(f"Database URI: {app.config['SQLALCHEMY_DATABASE_URI']}")
        
        # Test database connection first
        try:
            from sqlalchemy import text
            result = db.session.execute(text('SELECT 1 as test'))
            app.logger.info("Database connection test successful")
        except Exception as e:
            app.logger.error(f"Database connection test failed: {e}")
            return jsonify({'success': False, 'error': f'Database connection failed: {str(e)}'})
        
        with open(filepath, 'w') as f:
            # Write header
            f.write("-- GBot Database Backup (SQLAlchemy)\n")
            f.write(f"-- Created: {datetime.now().isoformat()}\n")
            f.write("-- Database: PostgreSQL\n\n")
            
            # Import all models - MUST include ALL tables in the database
            from database import User, WhitelistedIP, UsedDomain, GoogleAccount, GoogleToken, Scope, ServerConfig, UserAppPassword, ServiceAccount
            
            # List of all tables to back up - ServiceAccount is critical!
            tables = [User, WhitelistedIP, UsedDomain, GoogleAccount, GoogleToken, Scope, ServerConfig, UserAppPassword, ServiceAccount]
            
            total_records = 0
            
            for table in tables:
                table_name = table.__tablename__
                f.write(f"\n-- Table: {table_name}\n")
                
                if include_data in ['full', 'schema']:
                    # Create table schema
                    f.write(f"CREATE TABLE IF NOT EXISTS {table_name} (\n")
                    columns = []
                    for column in table.__table__.columns:
                        col_def = f"    {column.name} {column.type}"
                        if column.primary_key:
                            col_def += " PRIMARY KEY"
                        if not column.nullable:
                            col_def += " NOT NULL"
                        columns.append(col_def)
                    f.write(",\n".join(columns))
                    f.write("\n);\n\n")
                
                if include_data in ['full', 'data']:
                    # Insert data
                    try:
                        records = table.query.all()
                        app.logger.info(f"Found {len(records)} records in table {table_name}")
                        
                        if records:
                            f.write(f"-- Data for {table_name} ({len(records)} records)\n")
                            for record in records:
                                values = []
                                for column in table.__table__.columns:
                                    value = getattr(record, column.name)
                                    if value is None:
                                        values.append('NULL')
                                    elif isinstance(value, str):
                                        # Escape single quotes
                                        escaped_value = value.replace("'", "''")
                                        values.append(f"'{escaped_value}'")
                                    elif isinstance(value, datetime):
                                        values.append(f"'{value.isoformat()}'")
                                    else:
                                        values.append(str(value))
                                
                                column_names = [col.name for col in table.__table__.columns]
                                f.write(f"INSERT INTO {table_name} ({', '.join(column_names)}) VALUES ({', '.join(values)});\n")
                            f.write("\n")
                            total_records += len(records)
                        else:
                            f.write(f"-- No data in {table_name}\n")
                    except Exception as table_error:
                        app.logger.error(f"Error reading table {table_name}: {table_error}")
                        f.write(f"-- Error reading table {table_name}: {table_error}\n")
            
            # Write summary
            f.write(f"\n-- Backup Summary\n")
            f.write(f"-- Total records backed up: {total_records}\n")
            f.write(f"-- Backup completed at: {datetime.now().isoformat()}\n")
        
        # Check if file was created and has content
        if not os.path.exists(filepath):
            app.logger.error("Backup file was not created")
            return jsonify({'success': False, 'error': 'SQLAlchemy backup file was not created'})
        
        file_size = os.path.getsize(filepath)
        app.logger.info(f"SQLAlchemy backup created: {filepath} ({file_size} bytes)")
        
        if file_size < 100:  # Less than 100 bytes is suspicious
            app.logger.warning(f"Backup file is very small ({file_size} bytes), might be empty")
            # Don't fail, just warn - empty database is valid
        
        app.logger.info("SQLAlchemy backup created successfully")
        return True  # Return success status, not JSON
        
    except Exception as e:
        app.logger.error(f"SQLAlchemy backup failed: {e}")
        return False  # Return failure status, not JSON

def convert_json_to_sql(json_filepath, sql_filepath, include_data):
    """Convert JSON backup to SQL format for restore compatibility"""
    try:
        import json
        with open(json_filepath, 'r') as f:
            backup_data = json.load(f)
        
        with open(sql_filepath, 'w') as f:
            # Write header
            f.write("-- GBot Database Backup (Converted from JSON)\n")
            f.write(f"-- Created: {datetime.now().isoformat()}\n")
            f.write("-- Database: PostgreSQL\n\n")
            
            total_records = 0
            
            for table_name, table_data in backup_data.items():
                f.write(f"\n-- Table: {table_name}\n")
                
                if include_data in ['full', 'schema']:
                    # Create table schema
                    f.write(f"CREATE TABLE IF NOT EXISTS {table_name} (\n")
                    columns = []
                    for col in table_data['schema']['columns']:
                        col_def = f"    {col['name']} {col['type']}"
                        columns.append(col_def)
                    f.write(",\n".join(columns))
                    f.write("\n);\n\n")
                
                if include_data in ['full', 'data'] and table_data['data']:
                    # Insert data
                    f.write(f"-- Data for {table_name} ({len(table_data['data'])} records)\n")
                    for record in table_data['data']:
                        values = []
                        for col in table_data['schema']['columns']:
                            value = record.get(col['name'])
                            if value is None:
                                values.append('NULL')
                            elif isinstance(value, str):
                                # Escape single quotes
                                escaped_value = value.replace("'", "''")
                                values.append(f"'{escaped_value}'")
                            else:
                                values.append(str(value))
                        
                        column_names = [col['name'] for col in table_data['schema']['columns']]
                        f.write(f"INSERT INTO {table_name} ({', '.join(column_names)}) VALUES ({', '.join(values)});\n")
                    f.write("\n")
                    total_records += len(table_data['data'])
                else:
                    f.write(f"-- No data in {table_name}\n")
            
            # Write summary
            f.write(f"\n-- Backup Summary\n")
            f.write(f"-- Total records backed up: {total_records}\n")
            f.write(f"-- Backup completed at: {datetime.now().isoformat()}\n")
        
        return True
    except Exception as e:
        app.logger.error(f"Failed to convert JSON to SQL: {e}")
        return False

# Database Backup API routes
@app.route('/api/diagnose-backup', methods=['GET'])
@login_required
def diagnose_backup():
    """Diagnose backup issues"""
    if session.get('role') != 'admin':
        return jsonify({'success': False, 'error': 'Admin privileges required'})
    
    try:
        import os
        import subprocess
        
        # Check database connection
        db_status = {}
        try:
            from sqlalchemy import text
            result = db.session.execute(text('SELECT 1 as test'))
            db_status['connection'] = 'OK'
            db_status['uri'] = app.config['SQLALCHEMY_DATABASE_URI']
        except Exception as e:
            db_status['connection'] = f'FAILED: {str(e)}'
            db_status['uri'] = app.config['SQLALCHEMY_DATABASE_URI']
        
        # Check pg_dump availability
        pg_dump_status = {}
        try:
            result = subprocess.run(['pg_dump', '--version'], capture_output=True, text=True, timeout=5)
            if result.returncode == 0:
                pg_dump_status['available'] = True
                pg_dump_status['version'] = result.stdout.strip()
            else:
                pg_dump_status['available'] = False
                pg_dump_status['error'] = result.stderr
        except Exception as e:
            pg_dump_status['available'] = False
            pg_dump_status['error'] = str(e)
        
        # Check backup directory
        backup_dir = os.path.join(os.path.dirname(__file__), 'backups')
        backup_status = {
            'directory': backup_dir,
            'exists': os.path.exists(backup_dir),
            'writable': False
        }
        
        if backup_status['exists']:
            try:
                test_file = os.path.join(backup_dir, 'test_write.tmp')
                with open(test_file, 'w') as f:
                    f.write('test')
                os.remove(test_file)
                backup_status['writable'] = True
            except Exception as e:
                backup_status['error'] = str(e)
        
        # Check database tables and record counts
        table_status = {}
        try:
            from database import User, WhitelistedIP, UsedDomain, GoogleAccount, GoogleToken, Scope, ServerConfig, UserAppPassword
            tables = [User, WhitelistedIP, UsedDomain, GoogleAccount, GoogleToken, Scope, ServerConfig, UserAppPassword]
            
            for table in tables:
                try:
                    count = table.query.count()
                    table_status[table.__tablename__] = count
                except Exception as e:
                    table_status[table.__tablename__] = f'ERROR: {str(e)}'
        except Exception as e:
            table_status['error'] = str(e)
        
        return jsonify({
            'success': True,
            'diagnosis': {
                'database': db_status,
                'pg_dump': pg_dump_status,
                'backup_directory': backup_status,
                'tables': table_status
            }
        })
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/test-backup', methods=['POST'])
@login_required
def test_backup():
    """Test backup functionality with minimal data"""
    if session.get('role') != 'admin':
        return jsonify({'success': False, 'error': 'Admin privileges required'})
    
    try:
        import os
        
        # Create test backup directory
        backup_dir = os.path.join(os.path.dirname(__file__), 'backups')
        if not os.path.exists(backup_dir):
            os.makedirs(backup_dir)
        
        # Generate test filename
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f"test_backup_{timestamp}.sql"
        filepath = os.path.join(backup_dir, filename)
        
        app.logger.info(f"Testing backup creation: {filepath}")
        
        # Test database connection
        try:
            from sqlalchemy import text
            result = db.session.execute(text('SELECT 1 as test'))
            app.logger.info("Database connection test successful")
        except Exception as e:
            app.logger.error(f"Database connection test failed: {e}")
            return jsonify({'success': False, 'error': f'Database connection failed: {str(e)}'})
        
        # Test table access
        try:
            from database import User, WhitelistedIP, UsedDomain, GoogleAccount, GoogleToken, Scope, ServerConfig, UserAppPassword
            tables = [User, WhitelistedIP, UsedDomain, GoogleAccount, GoogleToken, Scope, ServerConfig, UserAppPassword]
            
            table_counts = {}
            total_records = 0
            
            for table in tables:
                try:
                    count = table.query.count()
                    table_counts[table.__tablename__] = count
                    total_records += count
                    app.logger.info(f"Table {table.__tablename__}: {count} records")
                except Exception as e:
                    app.logger.error(f"Error accessing table {table.__tablename__}: {e}")
                    table_counts[table.__tablename__] = f'ERROR: {str(e)}'
            
            # Create a simple test backup
            with open(filepath, 'w') as f:
                f.write("-- Test Backup\n")
                f.write(f"-- Created: {datetime.now().isoformat()}\n")
                f.write(f"-- Total records: {total_records}\n\n")
                
                for table_name, count in table_counts.items():
                    f.write(f"-- Table: {table_name} ({count} records)\n")
                
                f.write("\n-- Test backup completed successfully\n")
            
            file_size = os.path.getsize(filepath)
            app.logger.info(f"Test backup created: {filepath}, size: {file_size} bytes")
            
            # Clean up test file
            os.remove(filepath)
            
            return jsonify({
                'success': True,
                'message': 'Backup system test successful',
                'table_counts': table_counts,
                'total_records': total_records,
                'test_file_size': file_size
            })
            
        except Exception as e:
            app.logger.error(f"Error in backup test: {e}")
            return jsonify({'success': False, 'error': f'Backup test failed: {str(e)}'})
        
    except Exception as e:
        app.logger.error(f"Error in test backup: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/create-database-backup', methods=['POST'])
@login_required
def create_database_backup():
    if session.get('role') != 'admin':
        return jsonify({'success': False, 'error': 'Admin privileges required'})
    
    try:
        data = request.get_json()
        backup_format = data.get('format', 'sql')
        include_data = data.get('include_data', 'full')
        
        # Create backup directory if it doesn't exist
        import os
        backup_dir = os.path.join(os.path.dirname(__file__), 'backups')
        if not os.path.exists(backup_dir):
            os.makedirs(backup_dir)
        
        # Generate filename with timestamp
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        filename = f"gbot_db_backup_{timestamp}.{backup_format}"
        filepath = os.path.join(backup_dir, filename)
        
        if backup_format == 'sql':
            # Create SQL dump
            if app.config['SQLALCHEMY_DATABASE_URI'].startswith('postgresql'):
                # PostgreSQL backup
                import subprocess
                import urllib.parse
                
                # Check if pg_dump is available, if not try to install it
                try:
                    subprocess.run(['pg_dump', '--version'], capture_output=True, check=True)
                    app.logger.info("pg_dump is available")
                except (subprocess.CalledProcessError, FileNotFoundError):
                    app.logger.warning("pg_dump not found, attempting to install PostgreSQL client tools...")
                    try:
                        # Try to install PostgreSQL client tools
                        install_cmd = ['apt-get', 'update', '&&', 'apt-get', 'install', '-y', 'postgresql-client']
                        subprocess.run(' '.join(install_cmd), shell=True, check=True)
                        app.logger.info("PostgreSQL client tools installed successfully")
                    except subprocess.CalledProcessError as install_error:
                        app.logger.error(f"Failed to install PostgreSQL client tools: {install_error}")
                        app.logger.info("Falling back to SQLAlchemy-based backup")
                        result = create_sqlalchemy_backup(filepath, include_data)
                        if not result:
                            return jsonify({'success': False, 'error': 'SQLAlchemy backup fallback failed'})
                
                # Parse database URL
                db_url = app.config['SQLALCHEMY_DATABASE_URI']
                parsed = urllib.parse.urlparse(db_url)
                
                app.logger.info(f"Creating PostgreSQL backup for database: {parsed.path[1:]}")
                
                # Set environment variables for pg_dump
                env = os.environ.copy()
                env['PGPASSWORD'] = parsed.password
                
                # Build pg_dump command
                cmd = [
                    'pg_dump',
                    '-h', parsed.hostname or 'localhost',
                    '-p', str(parsed.port or 5432),
                    '-U', parsed.username,
                    '-d', parsed.path[1:] if parsed.path else 'gbot_db',  # Remove leading slash
                    '--no-password',
                    '--verbose'
                ]
                
                if include_data == 'schema':
                    cmd.append('--schema-only')
                elif include_data == 'data':
                    cmd.append('--data-only')
                
                app.logger.info(f"Executing pg_dump command: {' '.join(cmd)}")
                app.logger.info(f"Database URL: {db_url}")
                app.logger.info(f"Environment PGPASSWORD set: {'Yes' if env.get('PGPASSWORD') else 'No'}")
                
                # Execute pg_dump
                try:
                    
                    with open(filepath, 'w') as f:
                        result = subprocess.run(cmd, stdout=f, stderr=subprocess.PIPE, env=env, text=True, timeout=300)
                    
                    app.logger.info(f"pg_dump return code: {result.returncode}")
                    if result.stderr:
                        app.logger.warning(f"pg_dump stderr: {result.stderr}")
                    
                    if result.returncode != 0:
                        app.logger.error(f"pg_dump failed with return code {result.returncode}: {result.stderr}")
                        return jsonify({'success': False, 'error': f'pg_dump failed (code {result.returncode}): {result.stderr}'})
                    
                    # Check if file was created and has content
                    file_size = os.path.getsize(filepath) if os.path.exists(filepath) else 0
                    app.logger.info(f"Backup file created: {filepath}, size: {file_size} bytes")
                    
                    if not os.path.exists(filepath) or file_size == 0:
                        app.logger.warning("pg_dump created empty file, trying SQLAlchemy fallback...")
                        # Fallback to SQLAlchemy-based backup
                        result = create_sqlalchemy_backup(filepath, include_data)
                        if not result:
                            return jsonify({'success': False, 'error': 'SQLAlchemy backup fallback failed'})
                    
                    # Check if file is too small (less than 1KB indicates potential issue)
                    if file_size < 1024:
                        app.logger.warning(f"Backup file is very small ({file_size} bytes), checking content...")
                        with open(filepath, 'r') as f:
                            content = f.read()
                            if not content.strip() or len(content.strip()) < 100:
                                app.logger.warning("Backup file appears empty or minimal, trying SQLAlchemy fallback...")
                                result = create_sqlalchemy_backup(filepath, include_data)
                                if not result:
                                    return jsonify({'success': False, 'error': 'SQLAlchemy backup fallback failed'})
                        
                except subprocess.TimeoutExpired:
                    app.logger.warning("pg_dump timed out, trying SQLAlchemy fallback...")
                    result = create_sqlalchemy_backup(filepath, include_data)
                    if not result:
                        return jsonify({'success': False, 'error': 'SQLAlchemy backup fallback failed'})
                except Exception as e:
                    app.logger.warning(f"pg_dump failed: {e}, trying SQLAlchemy fallback...")
                    result = create_sqlalchemy_backup(filepath, include_data)
                    if not result:
                        return jsonify({'success': False, 'error': 'SQLAlchemy backup fallback failed'})
                    
            else:
                # SQLite backup
                import shutil
                db_path = app.config['SQLALCHEMY_DATABASE_URI'].replace('sqlite:///', '')
                if not os.path.exists(db_path):
                    return jsonify({'success': False, 'error': f'SQLite database file not found: {db_path}'})
                shutil.copy2(db_path, filepath)
        
        elif backup_format == 'json':
            # Create JSON export
            backup_data = {}
            
            # Export all tables
            from database import User, WhitelistedIP, UsedDomain, GoogleAccount, GoogleToken, Scope, ServerConfig, UserAppPassword
            
            tables = [User, WhitelistedIP, UsedDomain, GoogleAccount, GoogleToken, Scope, ServerConfig, UserAppPassword]
            
            for table in tables:
                table_name = table.__tablename__
                records = []
                
                if include_data in ['full', 'data']:
                    for record in table.query.all():
                        record_dict = {}
                        for column in table.__table__.columns:
                            value = getattr(record, column.name)
                            if isinstance(value, datetime):
                                value = value.isoformat()
                            record_dict[column.name] = value
                        records.append(record_dict)
                
                backup_data[table_name] = {
                    'schema': {
                        'columns': [{'name': col.name, 'type': str(col.type)} for col in table.__table__.columns]
                    },
                    'data': records if include_data in ['full', 'data'] else []
                }
            
            # Write JSON file
            import json
            with open(filepath, 'w') as f:
                json.dump(backup_data, f, indent=2, default=str)
            
            # Also create SQL version for restore compatibility
            sql_filepath = filepath.replace('.json', '.sql')
            result = create_sqlalchemy_backup(sql_filepath, include_data)
            if not result:
                app.logger.warning("Failed to create SQL version of JSON backup")
        
        elif backup_format == 'csv':
            # Create CSV export
            import csv
            import zipfile
            
            # Create ZIP file with multiple CSV files
            zip_path = filepath.replace('.csv', '.zip')
            
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zip_file:
                from database import User, WhitelistedIP, UsedDomain, GoogleAccount, GoogleToken, Scope, ServerConfig
                
                tables = [User, WhitelistedIP, UsedDomain, GoogleAccount, GoogleToken, Scope, ServerConfig]
                
                for table in tables:
                    if include_data in ['full', 'data']:
                        # Create CSV content
                        csv_content = io.StringIO()
                        writer = csv.writer(csv_content)
                        
                        # Write headers
                        headers = [col.name for col in table.__table__.columns]
                        writer.writerow(headers)
                        
                        # Write data
                        for record in table.query.all():
                            row = []
                            for column in table.__table__.columns:
                                value = getattr(record, column.name)
                                if isinstance(value, datetime):
                                    value = value.isoformat()
                                row.append(str(value) if value is not None else '')
                            writer.writerow(row)
                        
                        # Add to ZIP
                        zip_file.writestr(f"{table.__tablename__}.csv", csv_content.getvalue())
            
            # Update filepath to ZIP
            filepath = zip_path
            filename = os.path.basename(zip_path)
        
        # Get file size
        file_size = os.path.getsize(filepath)
        
        app.logger.info(f"Database backup created: {filename} ({file_size} bytes)")
        
        return jsonify({
            'success': True,
            'message': f'Database backup created successfully',
            'filename': filename,
            'size': file_size
        })
        
    except Exception as e:
        app.logger.error(f"Error creating database backup: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/list-database-backups', methods=['GET'])
@login_required
def list_database_backups():
    if session.get('role') != 'admin':
        return jsonify({'success': False, 'error': 'Admin privileges required'})
    
    try:
        import os
        backup_dir = os.path.join(os.path.dirname(__file__), 'backups')
        
        if not os.path.exists(backup_dir):
            return jsonify({'success': True, 'files': []})
        
        backup_files = []
        for filename in os.listdir(backup_dir):
            if filename.startswith('gbot_db_backup_') and filename.endswith(('.sql', '.json', '.zip')):
                filepath = os.path.join(backup_dir, filename)
                stat = os.stat(filepath)
                backup_files.append({
                    'name': filename,
                    'size': stat.st_size,
                    'modified': datetime.fromtimestamp(stat.st_mtime).isoformat()
                })
        
        # Sort by modification time (newest first)
        backup_files.sort(key=lambda x: x['modified'], reverse=True)
        
        return jsonify({
            'success': True,
            'files': backup_files
        })
        
    except Exception as e:
        app.logger.error(f"Error listing database backups: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/download-database-backup', methods=['POST'])
@login_required
def download_database_backup():
    if session.get('role') != 'admin':
        return jsonify({'success': False, 'error': 'Admin privileges required'})
    
    try:
        data = request.get_json()
        filename = data.get('filename')
        
        if not filename:
            return jsonify({'success': False, 'error': 'Filename is required'})
        
        import os
        backup_dir = os.path.join(os.path.dirname(__file__), 'backups')
        filepath = os.path.join(backup_dir, filename)
        
        if not os.path.exists(filepath):
            return jsonify({'success': False, 'error': f'Backup file not found: {filename}'})
        
        # Read file content
        with open(filepath, 'rb') as f:
            file_content = f.read()
        
        # Return file as response
        from flask import Response
        return Response(
            file_content,
            mimetype='application/octet-stream',
            headers={
                'Content-Disposition': f'attachment; filename="{filename}"',
                'Content-Length': str(len(file_content))
            }
        )
        
    except Exception as e:
        app.logger.error(f"Error downloading database backup: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/delete-database-backup', methods=['POST'])
@login_required
def delete_database_backup():
    if session.get('role') != 'admin':
        return jsonify({'success': False, 'error': 'Admin privileges required'})
    
    try:
        data = request.get_json()
        filename = data.get('filename')
        
        if not filename:
            return jsonify({'success': False, 'error': 'Filename is required'})
        
        import os
        backup_dir = os.path.join(os.path.dirname(__file__), 'backups')
        filepath = os.path.join(backup_dir, filename)
        
        if not os.path.exists(filepath):
            return jsonify({'success': False, 'error': f'Backup file not found: {filename}'})
        
        # Delete file
        os.remove(filepath)
        
        app.logger.info(f"Database backup deleted: {filename}")
        
        return jsonify({
            'success': True,
            'message': f'Backup file {filename} deleted successfully'
        })
        
    except Exception as e:
        app.logger.error(f"Error deleting database backup: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/cleanup-old-backups', methods=['POST'])
@login_required
def cleanup_old_backups():
    if session.get('role') != 'admin':
        return jsonify({'success': False, 'error': 'Admin privileges required'})
    
    try:
        import os
        backup_dir = os.path.join(os.path.dirname(__file__), 'backups')
        
        if not os.path.exists(backup_dir):
            return jsonify({'success': True, 'deleted_count': 0})
        
        # Get all backup files
        backup_files = []
        for filename in os.listdir(backup_dir):
            if filename.startswith('gbot_db_backup_') and filename.endswith(('.sql', '.json', '.zip')):
                filepath = os.path.join(backup_dir, filename)
                stat = os.stat(filepath)
                backup_files.append({
                    'name': filename,
                    'path': filepath,
                    'modified': stat.st_mtime
                })
        
        # Sort by modification time (newest first)
        backup_files.sort(key=lambda x: x['modified'], reverse=True)
        
        # Keep only the 5 most recent backups
        files_to_delete = backup_files[5:]
        deleted_count = 0
        
        for file_info in files_to_delete:
            try:
                os.remove(file_info['path'])
                deleted_count += 1
                app.logger.info(f"Deleted old backup: {file_info['name']}")
            except Exception as e:
                app.logger.error(f"Error deleting backup {file_info['name']}: {e}")
        
        return jsonify({
            'success': True,
            'message': f'Cleanup completed. Deleted {deleted_count} old backup files.',
            'deleted_count': deleted_count
        })
        
    except Exception as e:
        app.logger.error(f"Error cleaning up old backups: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/simple-db-test', methods=['GET'])
@login_required
def simple_db_test():
    """Simple database connection test"""
    try:
        from database import db
        from sqlalchemy import text
        with app.app_context():
            result = db.session.execute(text('SELECT 1 as test')).fetchone()
            return jsonify({
                'success': True, 
                'message': 'Database connection successful',
                'test_result': result[0] if result else None
            })
    except Exception as e:
        return jsonify({
            'success': False, 
            'error': str(e),
            'database_uri': app.config['SQLALCHEMY_DATABASE_URI']
        })

@app.route('/api/test-database-connection', methods=['GET'])
@login_required
def test_database_connection():
    """Test database connection and show table contents"""
    if session.get('role') != 'admin':
        return jsonify({'success': False, 'error': 'Admin privileges required'})
    
    try:
        from database import User, WhitelistedIP, UsedDomain, GoogleAccount, GoogleToken, Scope, ServerConfig, BackupServerConfig
        
        tables_info = {}
        tables = [User, WhitelistedIP, UsedDomain, GoogleAccount, GoogleToken, Scope, ServerConfig, BackupServerConfig]
        
        for table in tables:
            table_name = table.__tablename__
            try:
                count = table.query.count()
                tables_info[table_name] = {
                    'count': count,
                    'status': 'OK'
                }
            except Exception as e:
                tables_info[table_name] = {
                    'count': 0,
                    'status': f'Error: {str(e)}'
                }
        
        return jsonify({
            'success': True,
            'database_uri': app.config['SQLALCHEMY_DATABASE_URI'],
            'tables': tables_info
        })
        
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/test-database-backup', methods=['GET'])
@login_required
def test_database_backup():
    """Test database backup functionality"""
    if session.get('role') != 'admin':
        return jsonify({'success': False, 'error': 'Admin privileges required'})
    
    try:
        import subprocess
        import urllib.parse
        
        # Check database configuration
        db_url = app.config['SQLALCHEMY_DATABASE_URI']
        parsed = urllib.parse.urlparse(db_url)
        
        test_results = {
            'database_type': 'PostgreSQL' if db_url.startswith('postgresql') else 'SQLite',
            'database_url': f"{parsed.scheme}://{parsed.username}@{parsed.hostname}:{parsed.port}{parsed.path}",
            'pg_dump_available': False,
            'database_connection': False,
            'backup_directory': False
        }
        
        # Test pg_dump availability
        try:
            result = subprocess.run(['pg_dump', '--version'], capture_output=True, text=True, timeout=10)
            if result.returncode == 0:
                test_results['pg_dump_available'] = True
                test_results['pg_dump_version'] = result.stdout.strip()
        except Exception as e:
            test_results['pg_dump_error'] = str(e)
        
        # Test database connection
        try:
            from database import db
            from sqlalchemy import text
            with app.app_context():
                db.session.execute(text('SELECT 1'))
                test_results['database_connection'] = True
        except Exception as e:
            test_results['database_connection_error'] = str(e)
        
        # Test backup directory
        import os
        backup_dir = os.path.join(os.path.dirname(__file__), 'backups')
        if os.path.exists(backup_dir) or os.access(os.path.dirname(backup_dir), os.W_OK):
            test_results['backup_directory'] = True
        else:
            test_results['backup_directory_error'] = 'Cannot create backup directory'
        
        return jsonify({
            'success': True,
            'test_results': test_results
        })
        
    except Exception as e:
        app.logger.error(f"Error testing database backup: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/bulk-delete-accounts', methods=['POST'])
@login_required
def bulk_delete_accounts():
    """Bulk delete multiple Google Workspace accounts"""
    if session.get('role') != 'admin':
        return jsonify({'success': False, 'error': 'Admin privileges required'})
    
    try:
        data = request.get_json()
        account_names = data.get('account_names', [])
        
        if not account_names:
            return jsonify({'success': False, 'error': 'No account names provided'})
        
        if not isinstance(account_names, list):
            return jsonify({'success': False, 'error': 'Account names must be provided as a list'})
        
        logging.info(f"Bulk deleting {len(account_names)} accounts: {account_names}")
        
        results = []
        successful_deletions = 0
        
        for account_name in account_names:
            account_name = account_name.strip()
            if not account_name:
                continue
                
            try:
                logging.info(f"Deleting account: {account_name}")
                
                # Find the account in the database
                account = GoogleAccount.query.filter_by(account_name=account_name).first()
                
                if not account:
                    results.append({
                        'success': False,
                        'account': account_name,
                        'error': 'Account not found in database'
                    })
                    continue
                
                # Delete the account from database (this will cascade delete tokens)
                db.session.delete(account)
                db.session.commit()
                
                results.append({
                    'success': True,
                    'account': account_name,
                    'message': 'Account deleted successfully'
                })
                successful_deletions += 1
                logging.info(f"Successfully deleted account: {account_name}")
                
            except Exception as account_error:
                error_msg = str(account_error)
                logging.error(f"Failed to delete account {account_name}: {error_msg}")
                results.append({
                    'success': False,
                    'account': account_name,
                    'error': error_msg
                })
        
        logging.info(f"Bulk account deletion completed. Successfully deleted {successful_deletions} out of {len(account_names)} accounts")
        
        return jsonify({
            'success': True,
            'message': f'Bulk deletion completed. Successfully deleted {successful_deletions} out of {len(account_names)} accounts.',
            'results': results,
            'total_requested': len(account_names),
            'successful_deletions': successful_deletions
        })
        
    except Exception as e:
        logging.error(f"Bulk delete accounts error: {e}")
        return jsonify({'success': False, 'error': str(e)})

# Automated Subdomain Change API
@app.route('/api/auto-change-subdomain', methods=['POST'])
@login_required
def api_auto_change_subdomain():
    """Automatically change subdomain from current in-use to next available domain"""
    try:
        # Check if user is authenticated
        if 'current_account_name' not in session:
            return jsonify({'success': False, 'error': 'No account authenticated. Please authenticate first.'})
        
        # Get the current authenticated account
        account_name = session.get('current_account_name')
        
        # First, try to authenticate using saved tokens if service is not available
        if not google_api.service:
            if google_api.is_token_valid(account_name):
                success = google_api.authenticate_with_tokens(account_name)
                if not success:
                    return jsonify({'success': False, 'error': 'Failed to authenticate with saved tokens. Please re-authenticate.'})
            else:
                return jsonify({'success': False, 'error': 'No valid tokens found. Please re-authenticate.'})
        
        # Get all domains and their status
        result = google_api.get_domain_info()
        if not result['success']:
            return jsonify({'success': False, 'error': result['error']})
        
        domains = result['domains']
        
        # Get all users to calculate domain usage
        all_users = []
        page_token = None
        
        while True:
            try:
                if page_token:
                    users_result = google_api.service.users().list(
                        customer='my_customer',
                        maxResults=500,
                        pageToken=page_token
                    ).execute()
                else:
                    users_result = google_api.service.users().list(
                        customer='my_customer',
                        maxResults=500
                    ).execute()
                
                users = users_result.get('users', [])
                all_users.extend(users)
                
                page_token = users_result.get('nextPageToken')
                if not page_token:
                    break
                    
            except Exception as e:
                logging.warning(f"Failed to retrieve users page: {e}")
                break
        
        # Calculate user count per domain
        domain_user_counts = {}
        for user in all_users:
            email = user.get('primaryEmail', '')
            if email and '@' in email:
                domain = email.split('@')[1]
                domain_user_counts[domain] = domain_user_counts.get(domain, 0) + 1
        
        # Find current in-use domain (domain with most users)
        current_domain = None
        max_users = 0
        
        for domain_name, user_count in domain_user_counts.items():
            if user_count > max_users:
                max_users = user_count
                current_domain = domain_name
        
        if not current_domain or max_users == 0:
            return jsonify({'success': False, 'error': 'No domain currently in use found.'})
        
        # Get domain records from database to check ever_used status
        from database import UsedDomain
        domain_records = {}
        for domain_record in UsedDomain.query.all():
            domain_records[domain_record.domain_name] = domain_record
        
        # Find next available domain from the retrieved domains list (ascending order)
        available_domains = []
        for domain in domains:
            domain_name = domain.get('domainName', '')
            user_count = domain_user_counts.get(domain_name, 0)
            domain_record = domain_records.get(domain_name)
            
            # Check if domain is available (never used)
            ever_used = False
            if domain_record:
                try:
                    ever_used = getattr(domain_record, 'ever_used', False)
                except:
                    ever_used = False
            
            if user_count == 0 and not ever_used:
                available_domains.append(domain_name)
        
        if not available_domains:
            return jsonify({'success': False, 'error': 'No available domains found for automatic change.'})
        
        # Sort available domains alphabetically for ascending order
        available_domains.sort()
        
        # Find the next domain after the current domain
        next_domain = None
        for domain in available_domains:
            if domain > current_domain:
                next_domain = domain
                break
        
        # If no domain found after current, use the first available domain
        if not next_domain:
            next_domain = available_domains[0]
        
        # Get all users from current domain (excluding admin accounts)
        users_to_change = []
        for user in all_users:
            email = user.get('primaryEmail', '')
            if email and email.endswith(f'@{current_domain}'):
                # Skip admin accounts
                if not user.get('isAdmin', False):
                    users_to_change.append({
                        'email': email,
                        'user': user
                    })
        
        if not users_to_change:
            return jsonify({'success': False, 'error': f'No non-admin users found in domain {current_domain}.'})
        
        # Perform domain change for all users with progress tracking
        successful_changes = 0
        failed_changes = []
        total_users = len(users_to_change)
        
        for i, user_data in enumerate(users_to_change):
            email = user_data['email']
            user = user_data['user']
            
            try:
                # Extract username from email
                username = email.split('@')[0]
                new_email = f"{username}@{next_domain}"
                
                # Update user's primary email
                user['primaryEmail'] = new_email
                
                # Update user in Google Workspace
                google_api.service.users().update(
                    userKey=email,
                    body=user
                ).execute()
                
                successful_changes += 1
                logging.info(f"✅ Successfully changed {email} → {new_email} ({i+1}/{total_users})")
                
            except Exception as e:
                failed_changes.append({'email': email, 'error': str(e)})
                logging.error(f"ERROR: Failed to change {email}: {e}")
        
        # Update domain usage in database
        try:
            # Update old domain record
            old_domain_record = UsedDomain.query.filter_by(domain_name=current_domain).first()
            if old_domain_record:
                old_domain_record.user_count = max(0, old_domain_record.user_count - successful_changes)
                old_domain_record.updated_at = db.func.current_timestamp()
            
            # Update new domain record
            new_domain_record = UsedDomain.query.filter_by(domain_name=next_domain).first()
            if new_domain_record:
                new_domain_record.user_count += successful_changes
                new_domain_record.updated_at = db.func.current_timestamp()
            else:
                # Create new domain record
                new_domain_record = UsedDomain(
                    domain_name=next_domain,
                    user_count=successful_changes,
                    is_verified=True,
                    ever_used=True
                )
                db.session.add(new_domain_record)
            
            db.session.commit()
            logging.info(f"Updated domain usage: {current_domain} (-{successful_changes}) → {next_domain} (+{successful_changes})")
            
        except Exception as db_error:
            logging.warning(f"Failed to update domain usage in database: {db_error}")
        
        # Prepare response
        message = f"Automated subdomain change completed: {current_domain} → {next_domain}"
        if failed_changes:
            message += f". {len(failed_changes)} users failed to change."
        
        return jsonify({
            'success': True,
            'message': message,
            'current_domain': current_domain,
            'next_domain': next_domain,
            'successful_changes': successful_changes,
            'failed_changes': len(failed_changes),
            'total_users': total_users,
            'failed_details': failed_changes,
            'available_domains': available_domains
        })
        
    except Exception as e:
        logging.error(f"Auto change subdomain error: {e}")
        return jsonify({'success': False, 'error': str(e)})
# CSV User Management API
@app.route('/api/create-users-from-csv', methods=['POST'])
@login_required
def create_users_from_csv():
    """Create users from uploaded CSV file"""
    try:
        # Check if user has permission
        if session.get('role') not in ['admin', 'support']:
            return jsonify({'success': False, 'error': 'Admin or support privileges required'})
        
        # Check if file was uploaded
        if 'csv_file' not in request.files:
            return jsonify({'success': False, 'error': 'No CSV file uploaded'})
        
        file = request.files['csv_file']
        if file.filename == '':
            return jsonify({'success': False, 'error': 'No file selected'})
        
        if not file.filename.endswith('.csv'):
            return jsonify({'success': False, 'error': 'Only .csv files are allowed'})
        
        # Read and parse CSV content
        content = file.read().decode('utf-8')
        lines = content.strip().split('\n')
        
        if len(lines) < 2:
            return jsonify({'success': False, 'error': 'CSV file must have at least a header and one data row'})
        
        # Parse header - look for Google Workspace CSV format
        header = lines[0].split(',')
        
        # Look for email column (could be "Email Address [Required]" or just "email")
        email_index = None
        first_name_index = None
        last_name_index = None
        password_index = None
        
        for i, h in enumerate(header):
            h_clean = h.strip().lower()
            if 'email address' in h_clean or h_clean == 'email':
                email_index = i
            elif 'first name' in h_clean:
                first_name_index = i
            elif 'last name' in h_clean:
                last_name_index = i
            elif 'password' in h_clean and 'hash' not in h_clean:
                password_index = i
        
        if email_index is None:
            return jsonify({'success': False, 'error': 'CSV must have an "Email Address" or "email" column'})
        
        if first_name_index is None:
            return jsonify({'success': False, 'error': 'CSV must have a "First Name" column'})
        
        if last_name_index is None:
            return jsonify({'success': False, 'error': 'CSV must have a "Last Name" column'})
        
        if password_index is None:
            return jsonify({'success': False, 'error': 'CSV must have a "Password" column'})
        
        # Import required modules
        from database import GoogleAccount
        
        # Check if Google API is properly authenticated
        if not google_api.service:
            return jsonify({'success': False, 'error': 'Google API not authenticated. Please authenticate an account first.'})
        
        app.logger.info(f"Google API service status: {google_api.service is not None}")
        
        created_count = 0
        results = []
        
        # Process each line
        for line_num, line in enumerate(lines[1:], 2):  # Skip header
            line = line.strip()
            if not line:
                continue
            
            try:
                # Parse CSV line
                values = line.split(',')
                if len(values) <= max(email_index, first_name_index, last_name_index, password_index):
                    results.append({
                        'email': f'Line {line_num}',
                        'success': False,
                        'error': 'Not enough columns'
                    })
                    continue
                
                # Extract values from CSV
                email = values[email_index].strip().strip('"')
                first_name = values[first_name_index].strip().strip('"') if first_name_index < len(values) else 'User'
                last_name = values[last_name_index].strip().strip('"') if last_name_index < len(values) else 'Test'
                password = values[password_index].strip().strip('"') if password_index < len(values) else 'DefaultPass123'
                
                if not email or '@' not in email:
                    results.append({
                        'email': f'Line {line_num}',
                        'success': False,
                        'error': 'Invalid email format'
                    })
                    continue
                
                if not first_name:
                    first_name = 'User'
                if not last_name:
                    last_name = 'Test'
                if not password:
                    password = 'DefaultPass123'
                
                # Check if user already exists
                existing_account = GoogleAccount.query.filter_by(account_name=email).first()
                if existing_account:
                    results.append({
                        'email': email,
                        'success': False,
                        'error': 'User already exists'
                    })
                    continue
                
                # Create user using Google API with CSV values
                app.logger.info(f"Creating user: {email}")
                app.logger.info(f"First name: {first_name}, Last name: {last_name}")
                app.logger.info(f"Password: '{password}' (length: {len(password)}, type: {type(password)})")
                app.logger.info(f"Password bytes: {password.encode('utf-8')}")
                
                # Ensure password is a clean string
                clean_password = str(password).strip()
                app.logger.info(f"Clean password: '{clean_password}'")
                
                # Try to create the user
                result = google_api.create_gsuite_user(first_name, last_name, email, clean_password)
                
                if result.get('success'):
                    created_count += 1
                    
                    # SAVE DOMAIN TO DATABASE AS 'USED'
                    try:
                        from database import UsedDomain, db
                        domain = email.split('@')[1].lower()
                        used_domain = UsedDomain.query.filter_by(domain_name=domain).first()
                        if used_domain:
                            used_domain.ever_used = True
                            used_domain.updated_at = db.func.current_timestamp()
                        else:
                            new_used_domain = UsedDomain(domain_name=domain, ever_used=True, is_verified=True, user_count=1)
                            db.session.add(new_used_domain)
                        db.session.commit()
                        app.logger.info(f"Marked domain {domain} as used in DB")
                    except Exception as db_e:
                        app.logger.error(f"Failed to save domain {domain} to DB: {db_e}")
                        db.session.rollback()
                    results.append({
                        'email': email,
                        'success': True,
                        'error': None
                    })
                else:
                    results.append({
                        'email': email,
                        'success': False,
                        'error': result.get('error', 'Unknown error')
                    })
                
            except Exception as e:
                results.append({
                    'email': f'Line {line_num}',
                    'success': False,
                    'error': str(e)
                })
        
        return jsonify({
            'success': True,
            'created_count': created_count,
            'total_processed': len(results),
            'results': results
        })
        
    except Exception as e:
        app.logger.error(f"CSV user creation error: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/download-users-csv', methods=['GET'])
@login_required
def download_users_csv():
    """Download users as CSV file"""
    try:
        # Check if user has permission
        if session.get('role') not in ['admin', 'support']:
            return jsonify({'success': False, 'error': 'Admin or support privileges required'})
        
        from database import GoogleAccount
        import io
        
        # Get all users
        users = GoogleAccount.query.all()
        
        # Create CSV content
        output = io.StringIO()
        writer = csv.writer(output)
        
        # Write header
        writer.writerow(['email', 'client_id'])
        
        # Write user data
        for user in users:
            writer.writerow([
                user.account_name,
                user.client_id
            ])
        
        # Create response
        output.seek(0)
        csv_content = output.getvalue()
        output.close()
        
        # Create response with CSV content
        response = make_response(csv_content)
        response.headers['Content-Type'] = 'text/csv'
        response.headers['Content-Disposition'] = 'attachment; filename=users.csv'
        
        return response
        
    except Exception as e:
        app.logger.error(f"CSV download error: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/apply-domain-to-csv', methods=['POST'])
@login_required
def apply_domain_to_csv():
    """Apply domain changes to users from CSV"""
    try:
        # Check if user has permission
        if session.get('role') not in ['admin', 'support']:
            return jsonify({'success': False, 'error': 'Admin or support privileges required'})
        
        data = request.get_json()
        csv_path = data.get('csv_path')
        new_domain = data.get('new_domain')
        
        if not csv_path or not new_domain:
            return jsonify({'success': False, 'error': 'CSV path and new domain are required'})
        
        # Read CSV file
        with open(csv_path, 'r', encoding='utf-8') as file:
            content = file.read()
        
        lines = content.strip().split('\n')
        if len(lines) < 2:
            return jsonify({'success': False, 'error': 'CSV file must have at least a header and one data row'})
        
        # Parse header
        header = lines[0].split(',')
        if 'email' not in [h.strip().lower() for h in header]:
            return jsonify({'success': False, 'error': 'CSV must have an "email" column'})
        
        email_index = None
        for i, h in enumerate(header):
            if h.strip().lower() == 'email':
                email_index = i
                break
        
        if email_index is None:
            return jsonify({'success': False, 'error': 'Could not find email column'})
        
        from database import GoogleAccount
        
        updated_count = 0
        results = []
        
        # Process each line
        for line_num, line in enumerate(lines[1:], 2):
            line = line.strip()
            if not line:
                continue
            
            try:
                values = line.split(',')
                if len(values) <= email_index:
                    continue
                
                old_email = values[email_index].strip().strip('"')
                if not old_email or '@' not in old_email:
                    continue
                
                # Create new email with new domain
                username = old_email.split('@')[0]
                new_email = f"{username}@{new_domain}"
                
                # Find existing account
                account = GoogleAccount.query.filter_by(account_name=old_email).first()
                if account:
                    account.account_name = new_email
                    updated_count += 1
                    results.append({
                        'old_email': old_email,
                        'new_email': new_email,
                        'success': True,
                        'error': None
                    })
                else:
                    results.append({
                        'old_email': old_email,
                        'new_email': new_email,
                        'success': False,
                        'error': 'Account not found'
                    })
                
            except Exception as e:
                results.append({
                    'old_email': f'Line {line_num}',
                    'new_email': '',
                    'success': False,
                    'error': str(e)
                })
        
        db.session.commit()
        
        return jsonify({
            'success': True,
            'updated_count': updated_count,
            'total_processed': len(results),
            'results': results
        })
        
    except Exception as e:
        app.logger.error(f"Apply domain to CSV error: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/process-csv-domain-changes', methods=['POST'])
@login_required
def process_csv_domain_changes():
    """Process domain changes from CSV file"""
    try:
        # Check if user has permission
        if session.get('role') not in ['admin', 'support']:
            return jsonify({'success': False, 'error': 'Admin or support privileges required'})
        
        data = request.get_json()
        csv_path = data.get('csv_path')
        
        if not csv_path:
            return jsonify({'success': False, 'error': 'CSV path is required'})
        
        # Read CSV file
        with open(csv_path, 'r', encoding='utf-8') as file:
            content = file.read()
        
        lines = content.strip().split('\n')
        if len(lines) < 2:
            return jsonify({'success': False, 'error': 'CSV file must have at least a header and one data row'})
        
        # Parse header
        header = lines[0].split(',')
        if 'old_email' not in [h.strip().lower() for h in header] or 'new_email' not in [h.strip().lower() for h in header]:
            return jsonify({'success': False, 'error': 'CSV must have "old_email" and "new_email" columns'})
        
        old_email_index = None
        new_email_index = None
        for i, h in enumerate(header):
            if h.strip().lower() == 'old_email':
                old_email_index = i
            elif h.strip().lower() == 'new_email':
                new_email_index = i
        
        if old_email_index is None or new_email_index is None:
            return jsonify({'success': False, 'error': 'Could not find required email columns'})
        
        from database import GoogleAccount
        
        successful = 0
        failed = 0
        skipped = 0
        results = []
        
        # Process each line
        for line_num, line in enumerate(lines[1:], 2):
            line = line.strip()
            if not line:
                continue
            
            try:
                values = line.split(',')
                if len(values) <= max(old_email_index, new_email_index):
                    continue
                
                old_email = values[old_email_index].strip().strip('"')
                new_email = values[new_email_index].strip().strip('"')
                
                if not old_email or not new_email or '@' not in old_email or '@' not in new_email:
                    skipped += 1
                    continue
                
                # Find existing account
                account = GoogleAccount.query.filter_by(account_name=old_email).first()
                if account:
                    account.account_name = new_email
                    successful += 1
                    results.append({
                        'old_email': old_email,
                        'new_email': new_email,
                        'success': True,
                        'error': None
                    })
                else:
                    failed += 1
                    results.append({
                        'old_email': old_email,
                        'new_email': new_email,
                        'success': False,
                        'error': 'Account not found'
                    })
                
            except Exception as e:
                failed += 1
                results.append({
                    'old_email': f'Line {line_num}',
                    'new_email': '',
                    'success': False,
                    'error': str(e)
                })
        
        db.session.commit()
        
        return jsonify({
            'success': True,
            'successful': successful,
            'failed': failed,
            'skipped': skipped,
            'results': results
        })
        
    except Exception as e:
        app.logger.error(f"Process CSV domain changes error: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/generate-csv', methods=['POST'])
@login_required
def generate_csv():
    """Generate CSV file with user data"""
    try:
        # Check if user has permission
        if session.get('role') not in ['admin', 'support']:
            return jsonify({'success': False, 'error': 'Admin or support privileges required'})
        
        data = request.get_json()
        csv_type = data.get('type', 'users')  # users, passwords, etc.
        num_users = data.get('num_users', 10)
        domain = data.get('domain', 'example.com')
        password = data.get('password', 'SecurePass123')
        
        # Sanitize password - remove any potentially problematic characters
        import re
        # Keep only alphanumeric characters and basic symbols
        password = re.sub(r'[^\w\-_!@#$%^&*()+=]', '', password)
        
        # Validate password meets basic requirements
        if len(password) < 8:
            return jsonify({'success': False, 'error': 'Password must be at least 8 characters long'})
        
        # Basic validation - just check length and that it's not empty
        if not password.strip():
            return jsonify({'success': False, 'error': 'Password cannot be empty'})
        
        # Validate domain - remove any @ symbols that shouldn't be there
        if '@' in domain:
            # If domain contains @, extract the part after @
            domain_parts = domain.split('@')
            if len(domain_parts) > 1:
                domain = domain_parts[-1]  # Take the last part after @
            else:
                domain = domain_parts[0]  # Take the part before @
        
        # Ensure domain doesn't start with @
        domain = domain.lstrip('@')
        
        if not domain or '.' not in domain:
            return jsonify({'success': False, 'error': 'Invalid domain format'})
        
        from database import GoogleAccount, UserAppPassword
        import io
        
        # Create CSV content
        output = io.StringIO()
        writer = csv.writer(output)
        
        if csv_type == 'users':
            # Write header matching Google Workspace CSV format
            writer.writerow([
                'First Name [Required]',
                'Last Name [Required]', 
                'Email Address [Required]',
                'Password [Required]',
                'Password Hash Function [UPLOAD ONLY]',
                'Org Unit Path [Required]',
                'New Primary Email [UPLOAD ONLY]',
                'Recovery Email',
                'Home Secondary Email',
                'Work Secondary Email',
                'Recovery Phone [MUST BE IN THE E.164 FORMAT]',
                'Work Phone',
                'Home Phone',
                'Mobile Phone',
                'Work Address',
                'Home Address',
                'Employee ID',
                'Employee Type',
                'Employee Title',
                'Manager Email',
                'Department',
                'Cost Center',
                'Building ID',
                'Floor Name',
                'Floor Section',
                'Change Password at Next Sign-In',
                'New Status [UPLOAD ONLY]',
                'Advanced Protection Program enrollment'
            ])
            
            # Generate sample users with realistic data
            import random
            from faker import Faker
            fake = Faker()
            
            # Common first names and last names for realistic data
            first_names = [
                'James', 'John', 'Robert', 'Michael', 'William', 'David', 'Richard', 'Charles', 'Joseph', 'Thomas',
                'Christopher', 'Daniel', 'Paul', 'Mark', 'Donald', 'George', 'Kenneth', 'Steven', 'Edward', 'Brian',
                'Ronald', 'Anthony', 'Kevin', 'Jason', 'Matthew', 'Gary', 'Timothy', 'Jose', 'Larry', 'Jeffrey',
                'Mary', 'Patricia', 'Jennifer', 'Linda', 'Elizabeth', 'Barbara', 'Susan', 'Jessica', 'Sarah', 'Karen',
                'Nancy', 'Lisa', 'Betty', 'Helen', 'Sandra', 'Donna', 'Carol', 'Ruth', 'Sharon', 'Michelle',
                'Laura', 'Kimberly', 'Deborah', 'Dorothy', 'Amanda', 'Ashley', 'Brenda', 'Catherine', 'Christine', 'Diane',
                'Emily', 'Emma', 'Grace', 'Heather', 'Janet', 'Joyce', 'Judith', 'Julie', 'Katherine', 'Kelly',
                'Margaret', 'Maria', 'Marie', 'Martha', 'Melissa', 'Pamela', 'Rachel', 'Rebecca', 'Shirley', 'Tammy',
                'Teresa', 'Alexander', 'Andrew', 'Benjamin', 'Brandon', 'Carl', 'Christian', 'Eric', 'Frank', 'Gabriel',
                'Gregory', 'Harold', 'Henry', 'Jack', 'Jacob', 'Jeremy', 'Jonathan', 'Jordan', 'Justin', 'Keith',
                'Lawrence', 'Louis', 'Martin', 'Mason', 'Nicholas', 'Patrick', 'Peter', 'Raymond', 'Roger', 'Ryan',
                'Samuel', 'Scott', 'Sean', 'Stephen', 'Terry', 'Tyler', 'Victor', 'Wayne', 'Zachary', 'Aaron', 'Adam',
                'Alan', 'Albert', 'Arthur', 'Austin', 'Bruce', 'Bryan', 'Carlos', 'Craig', 'Dennis', 'Derek',
                'Douglas', 'Eugene', 'Gregory', 'Harold', 'Howard', 'Jack', 'Jerry', 'Joe', 'Jordan', 'Joshua',
                'Juan', 'Keith', 'Kenneth', 'Kyle', 'Lawrence', 'Louis', 'Manuel', 'Mason', 'Nicholas', 'Patrick',
                'Peter', 'Raymond', 'Roger', 'Roy', 'Ryan', 'Samuel', 'Scott', 'Sean', 'Stephen', 'Terry',
                'Tyler', 'Victor', 'Wayne', 'Zachary', 'Zachary', 'Aaron', 'Adam', 'Alan', 'Albert', 'Arthur'
            ]
            
            last_names = [
                'Smith', 'Johnson', 'Williams', 'Brown', 'Jones', 'Garcia', 'Miller', 'Davis', 'Rodriguez', 'Martinez',
                'Hernandez', 'Lopez', 'Gonzalez', 'Wilson', 'Anderson', 'Thomas', 'Taylor', 'Moore', 'Jackson', 'Martin',
                'Lee', 'Perez', 'Thompson', 'White', 'Harris', 'Sanchez', 'Clark', 'Ramirez', 'Lewis', 'Robinson',
                'Walker', 'Young', 'Allen', 'King', 'Wright', 'Scott', 'Torres', 'Nguyen', 'Hill', 'Flores',
                'Green', 'Adams', 'Nelson', 'Baker', 'Hall', 'Rivera', 'Campbell', 'Mitchell', 'Carter', 'Roberts',
                'Gomez', 'Phillips', 'Evans', 'Turner', 'Diaz', 'Parker', 'Cruz', 'Edwards', 'Collins', 'Reyes',
                'Stewart', 'Morris', 'Morales', 'Murphy', 'Cook', 'Rogers', 'Gutierrez', 'Ortiz', 'Morgan', 'Cooper',
                'Peterson', 'Bailey', 'Reed', 'Kelly', 'Howard', 'Ramos', 'Kim', 'Cox', 'Ward', 'Richardson',
                'Watson', 'Brooks', 'Chavez', 'Wood', 'James', 'Bennett', 'Gray', 'Mendoza', 'Ruiz', 'Hughes',
                'Price', 'Alvarez', 'Castillo', 'Sanders', 'Patel', 'Myers', 'Long', 'Ross', 'Foster', 'Jimenez',
                'Powell', 'Jenkins', 'Perry', 'Russell', 'Sullivan', 'Bell', 'Coleman', 'Butler', 'Henderson', 'Barnes',
                'Gonzales', 'Fisher', 'Vasquez', 'Simmons', 'Romero', 'Jordan', 'Patterson', 'Alexander', 'Hamilton', 'Graham',
                'Reynolds', 'Griffin', 'Wallace', 'Moreno', 'West', 'Cole', 'Hayes', 'Bryant', 'Herrera', 'Gibson',
                'Ellis', 'Tran', 'Medina', 'Aguilar', 'Stevens', 'Murray', 'Ford', 'Castro', 'Marshall', 'Owens',
                'Harrison', 'Fernandez', 'McDonald', 'Woods', 'Washington', 'Kennedy', 'Wells', 'Vargas', 'Henry', 'Chen',
                'Freeman', 'Webb', 'Tucker', 'Guzman', 'Burns', 'Crawford', 'Olson', 'Simpson', 'Porter', 'Hunter',
                'Gordon', 'Mendez', 'Aguirre', 'Gutierrez', 'Schmidt', 'Carr', 'Vasquez', 'Castillo', 'Wheeler', 'Chapman',
                'Oliver', 'Montgomery', 'Richards', 'Williamson', 'Johnston', 'Banks', 'Meyer', 'Bishop', 'McCoy', 'Howell',
                'Alvarez', 'Morales', 'Murphy', 'Cook', 'Rogers', 'Gutierrez', 'Ortiz', 'Morgan', 'Cooper', 'Peterson',
                'Bailey', 'Reed', 'Kelly', 'Howard', 'Ramos', 'Kim', 'Cox', 'Ward', 'Richardson', 'Watson',
                'Brooks', 'Chavez', 'Wood', 'James', 'Bennett', 'Gray', 'Mendoza', 'Ruiz', 'Hughes', 'Price',
                'Alvarez', 'Castillo', 'Sanders', 'Patel', 'Myers', 'Long', 'Ross', 'Foster', 'Jimenez', 'Powell'
            ]
            
            # Advanced unique alias generation system
            used_aliases = set()
            used_names = set()
            
            def generate_complex_alias(first_name, last_name, index, attempt=0):
                """Generate complex, unique aliases using only letters"""
                import string
                import hashlib
                import time
                
                # Base components
                fname = first_name.lower()
                lname = last_name.lower()
                f_initial = fname[0]
                l_initial = lname[0]
                
                # Generate random letter sequences
                def random_letters(length):
                    return ''.join(random.choice(string.ascii_lowercase) for _ in range(length))
                
                # Complex patterns with letters only
                patterns = [
                    # Pattern 1: Name + Random Letters + Index letters
                    f"{fname}{lname}{random_letters(4)}{chr(97 + (index % 26))}{chr(97 + ((index // 26) % 26))}",
                    f"{fname}{random_letters(3)}{lname}{random_letters(3)}",
                    f"{f_initial}{lname}{random_letters(5)}{chr(97 + (index % 26))}",
                    
                    # Pattern 2: Mixed combinations with letters
                    f"{fname}{random_letters(3)}{lname}{random_letters(2)}",
                    f"{fname[0:3]}{lname[0:3]}{random_letters(4)}",
                    f"{fname}{lname[0:2]}{random_letters(4)}{chr(97 + (index % 26))}",
                    
                    # Pattern 3: Hash-based unique identifiers (letters only)
                    f"{fname}{lname}{hashlib.md5(f'{fname}{lname}{index}{attempt}'.encode()).hexdigest()[:6].replace('0', 'a').replace('1', 'b').replace('2', 'c').replace('3', 'd').replace('4', 'e').replace('5', 'f').replace('6', 'g').replace('7', 'h').replace('8', 'i').replace('9', 'j')}",
                    f"{f_initial}{lname}{hashlib.md5(f'{index}{attempt}{time.time()}'.encode()).hexdigest()[:8].replace('0', 'k').replace('1', 'l').replace('2', 'm').replace('3', 'n').replace('4', 'o').replace('5', 'p').replace('6', 'q').replace('7', 'r').replace('8', 's').replace('9', 't')}",
                    
                    # Pattern 4: Complex letter combinations
                    f"{fname}{random.choice(string.ascii_lowercase)}{lname}{random_letters(3)}",
                    f"{fname[0:2]}{lname[0:2]}{random_letters(5)}",
                    f"{fname}{lname[0:3]}{random_letters(4)}{chr(97 + (index % 26))}",
                    
                    # Pattern 5: Time-based unique identifiers (letters only)
                    f"{fname}{lname}{random_letters(6)}{chr(97 + (index % 26))}",
                    f"{f_initial}{lname}{random_letters(5)}{chr(97 + (index % 26))}",
                    
                    # Pattern 6: Advanced letter combinations
                    f"{fname}{lname[0:4]}{random_letters(3)}",
                    f"{fname[0:3]}{lname}{random_letters(4)}",
                    f"{fname}{lname[0:2]}{random_letters(5)}{chr(97 + (index % 26))}",
                    
                    # Pattern 7: Complex letter patterns
                    f"{fname}{lname}{random_letters(6)}",
                    f"{f_initial}{lname[0:3]}{random_letters(5)}",
                    f"{fname[0:2]}{lname[0:3]}{random_letters(4)}",
                    
                    # Pattern 8: Advanced letter combinations
                    f"{fname}{lname}{random_letters(4)}{chr(97 + (index % 26))}",
                    f"{f_initial}{lname}{random_letters(6)}{chr(97 + (index % 26))}",
                    
                    # Pattern 9: Multi-part unique identifiers (letters only)
                    f"{fname}{lname[0:2]}{random_letters(3)}{chr(97 + (index % 26))}",
                    f"{fname[0:4]}{lname}{random_letters(4)}{chr(97 + (index % 26))}",
                    f"{fname}{lname[0:3]}{random_letters(5)}{chr(97 + (index % 26))}",
                    
                    # Pattern 10: Advanced hash combinations (letters only)
                    f"{fname}{lname}{hashlib.sha256(f'{fname}{lname}{index}{attempt}{random_letters(4)}'.encode()).hexdigest()[:7].replace('0', 'a').replace('1', 'b').replace('2', 'c').replace('3', 'd').replace('4', 'e').replace('5', 'f').replace('6', 'g').replace('7', 'h').replace('8', 'i').replace('9', 'j')}",
                    f"{f_initial}{lname}{hashlib.sha256(f'{index}{attempt}{time.time()}{random_letters(3)}'.encode()).hexdigest()[:9].replace('0', 'k').replace('1', 'l').replace('2', 'm').replace('3', 'n').replace('4', 'o').replace('5', 'p').replace('6', 'q').replace('7', 'r').replace('8', 's').replace('9', 't')}"
                ]
                
                # Try each pattern until we find a unique one
                for pattern in patterns:
                    if pattern not in used_aliases:
                        used_aliases.add(pattern)
                        return pattern
                
                # If all patterns are taken, create a completely unique one (letters only)
                unique_id = f"{fname}{lname}{random_letters(8)}{chr(97 + (index % 26))}{chr(97 + (attempt % 26))}"
                used_aliases.add(unique_id)
                return unique_id
            
            for i in range(1, int(num_users) + 1):
                # Ensure unique name combinations
                while True:
                    first_name = random.choice(first_names)
                    last_name = random.choice(last_names)
                    name_key = f"{first_name}_{last_name}"
                    if name_key not in used_names:
                        used_names.add(name_key)
                        break
                
                alias = generate_complex_alias(first_name, last_name, i)
                email = f"{alias}@{domain}"
                
                writer.writerow([
                    first_name,           # First Name [Required]
                    last_name,            # Last Name [Required]
                    email,                # Email Address [Required]
                    password,             # Password [Required]
                    '',                   # Password Hash Function [UPLOAD ONLY]
                    '/',                  # Org Unit Path [Required]
                    '',                   # New Primary Email [UPLOAD ONLY]
                    '',                   # Recovery Email
                    '',                   # Home Secondary Email
                    '',                   # Work Secondary Email
                    '',                   # Recovery Phone [MUST BE IN THE E.164 FORMAT]
                    '',                   # Work Phone
                    '',                   # Home Phone
                    '',                   # Mobile Phone
                    '',                   # Work Address
                    '',                   # Home Address
                    f"EMP{i:04d}",        # Employee ID
                    'Employee',           # Employee Type
                    'Staff',              # Employee Title
                    '',                   # Manager Email
                    'IT',                 # Department
                    '',                   # Cost Center
                    '',                   # Building ID
                    '',                   # Floor Name
                    '',                   # Floor Section
                    'False',              # Change Password at Next Sign-In
                    '',                   # New Status [UPLOAD ONLY]
                    'False'               # Advanced Protection Program enrollment
                ])
        
        elif csv_type == 'existing_users':
            # Write header
            writer.writerow(['email', 'client_id'])
            
            # Get all users
            users = GoogleAccount.query.all()
            
            # Write user data
            for user in users:
                writer.writerow([
                    user.account_name,
                    user.client_id
                ])
        
        elif csv_type == 'passwords':
            # Write header
            writer.writerow(['email', 'app_password', 'smtp_server', 'smtp_port'])
            
            # Get all app passwords
            passwords = UserAppPassword.query.all()
            
            # Write password data
            for password in passwords:
                email = f"{password.username}@{password.domain}"
                writer.writerow([
                    email,
                    password.app_password,
                    'smtp.gmail.com',
                    '587'
                ])
        
        else:
            return jsonify({'success': False, 'error': 'Invalid CSV type'})
        
        # Create response
        output.seek(0)
        csv_content = output.getvalue()
        output.close()
        
        # Save to file
        import os
        filename = f"{csv_type}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        filepath = os.path.join('exports', filename)
        
        # Create exports directory if it doesn't exist
        os.makedirs('exports', exist_ok=True)
        
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(csv_content)
        
        return jsonify({
            'success': True,
            'csv_data': csv_content,
            'filename': f"users_{domain}_{num_users}.csv",
            'message': f'CSV file generated with {num_users} users'
        })
        
    except Exception as e:
        app.logger.error(f"Generate CSV error: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/preview-csv', methods=['POST'])
@login_required
def preview_csv():
    """Preview CSV file content"""
    try:
        # Check if user has permission
        if session.get('role') not in ['admin', 'support']:
            return jsonify({'success': False, 'error': 'Admin or support privileges required'})
        
        data = request.get_json()
        num_users = data.get('num_users', 5)
        domain = data.get('domain', 'example.com')
        password = data.get('password', 'SecurePass123')
        
        # Sanitize password - remove any potentially problematic characters
        import re
        # Keep only alphanumeric characters and basic symbols
        password = re.sub(r'[^\w\-_!@#$%^&*()+=]', '', password)
        
        # Validate password meets basic requirements
        if len(password) < 8:
            return jsonify({'success': False, 'error': 'Password must be at least 8 characters long'})
        
        # Basic validation - just check length and that it's not empty
        if not password.strip():
            return jsonify({'success': False, 'error': 'Password cannot be empty'})
        
        # Validate domain - remove any @ symbols that shouldn't be there
        if '@' in domain:
            # If domain contains @, extract the part after @
            domain_parts = domain.split('@')
            if len(domain_parts) > 1:
                domain = domain_parts[-1]  # Take the last part after @
            else:
                domain = domain_parts[0]  # Take the part before @
        
        # Ensure domain doesn't start with @
        domain = domain.lstrip('@')
        
        if not domain or '.' not in domain:
            return jsonify({'success': False, 'error': 'Invalid domain format'})
        
        # Generate preview CSV content
        import io
        output = io.StringIO()
        writer = csv.writer(output)
        
        # Write header matching Google Workspace CSV format
        writer.writerow([
            'First Name [Required]',
            'Last Name [Required]', 
            'Email Address [Required]',
            'Password [Required]',
            'Password Hash Function [UPLOAD ONLY]',
            'Org Unit Path [Required]',
            'New Primary Email [UPLOAD ONLY]',
            'Recovery Email',
            'Home Secondary Email',
            'Work Secondary Email',
            'Recovery Phone [MUST BE IN THE E.164 FORMAT]',
            'Work Phone',
            'Home Phone',
            'Mobile Phone',
            'Work Address',
            'Home Address',
            'Employee ID',
            'Employee Type',
            'Employee Title',
            'Manager Email',
            'Department',
            'Cost Center',
            'Building ID',
            'Floor Name',
            'Floor Section',
            'Change Password at Next Sign-In',
            'New Status [UPLOAD ONLY]',
            'Advanced Protection Program enrollment'
        ])
        
        # Generate preview users with realistic data
        import random
        
        # Common first names and last names for realistic data
        first_names = [
            'James', 'John', 'Robert', 'Michael', 'William', 'David', 'Richard', 'Charles', 'Joseph', 'Thomas',
            'Christopher', 'Daniel', 'Paul', 'Mark', 'Donald', 'George', 'Kenneth', 'Steven', 'Edward', 'Brian',
            'Ronald', 'Anthony', 'Kevin', 'Jason', 'Matthew', 'Gary', 'Timothy', 'Jose', 'Larry', 'Jeffrey',
            'Mary', 'Patricia', 'Jennifer', 'Linda', 'Elizabeth', 'Barbara', 'Susan', 'Jessica', 'Sarah', 'Karen',
            'Nancy', 'Lisa', 'Betty', 'Helen', 'Sandra', 'Donna', 'Carol', 'Ruth', 'Sharon', 'Michelle',
            'Laura', 'Kimberly', 'Deborah', 'Dorothy', 'Amanda', 'Ashley', 'Brenda', 'Catherine', 'Christine', 'Diane',
            'Emily', 'Emma', 'Grace', 'Heather', 'Janet', 'Joyce', 'Judith', 'Julie', 'Katherine', 'Kelly',
            'Margaret', 'Maria', 'Marie', 'Martha', 'Melissa', 'Pamela', 'Rachel', 'Rebecca', 'Shirley', 'Tammy',
            'Teresa', 'Alexander', 'Andrew', 'Benjamin', 'Brandon', 'Carl', 'Christian', 'Eric', 'Frank', 'Gabriel',
            'Gregory', 'Harold', 'Henry', 'Jack', 'Jacob', 'Jeremy', 'Jonathan', 'Jordan', 'Justin', 'Keith',
            'Lawrence', 'Louis', 'Martin', 'Mason', 'Nicholas', 'Patrick', 'Peter', 'Raymond', 'Roger', 'Ryan',
            'Samuel', 'Scott', 'Sean', 'Stephen', 'Terry', 'Tyler', 'Victor', 'Wayne', 'Zachary', 'Aaron', 'Adam',
            'Alan', 'Albert', 'Arthur', 'Austin', 'Bruce', 'Bryan', 'Carlos', 'Craig', 'Dennis', 'Derek',
            'Douglas', 'Eugene', 'Gregory', 'Harold', 'Howard', 'Jack', 'Jerry', 'Joe', 'Jordan', 'Joshua',
            'Juan', 'Keith', 'Kenneth', 'Kyle', 'Lawrence', 'Louis', 'Manuel', 'Mason', 'Nicholas', 'Patrick',
            'Peter', 'Raymond', 'Roger', 'Roy', 'Ryan', 'Samuel', 'Scott', 'Sean', 'Stephen', 'Terry',
            'Tyler', 'Victor', 'Wayne', 'Zachary', 'Zachary', 'Aaron', 'Adam', 'Alan', 'Albert', 'Arthur'
        ]
        
        last_names = [
            'Smith', 'Johnson', 'Williams', 'Brown', 'Jones', 'Garcia', 'Miller', 'Davis', 'Rodriguez', 'Martinez',
            'Hernandez', 'Lopez', 'Gonzalez', 'Wilson', 'Anderson', 'Thomas', 'Taylor', 'Moore', 'Jackson', 'Martin',
            'Lee', 'Perez', 'Thompson', 'White', 'Harris', 'Sanchez', 'Clark', 'Ramirez', 'Lewis', 'Robinson',
            'Walker', 'Young', 'Allen', 'King', 'Wright', 'Scott', 'Torres', 'Nguyen', 'Hill', 'Flores',
            'Green', 'Adams', 'Nelson', 'Baker', 'Hall', 'Rivera', 'Campbell', 'Mitchell', 'Carter', 'Roberts',
            'Gomez', 'Phillips', 'Evans', 'Turner', 'Diaz', 'Parker', 'Cruz', 'Edwards', 'Collins', 'Reyes',
            'Stewart', 'Morris', 'Morales', 'Murphy', 'Cook', 'Rogers', 'Gutierrez', 'Ortiz', 'Morgan', 'Cooper',
            'Peterson', 'Bailey', 'Reed', 'Kelly', 'Howard', 'Ramos', 'Kim', 'Cox', 'Ward', 'Richardson',
            'Watson', 'Brooks', 'Chavez', 'Wood', 'James', 'Bennett', 'Gray', 'Mendoza', 'Ruiz', 'Hughes',
            'Price', 'Alvarez', 'Castillo', 'Sanders', 'Patel', 'Myers', 'Long', 'Ross', 'Foster', 'Jimenez',
            'Powell', 'Jenkins', 'Perry', 'Russell', 'Sullivan', 'Bell', 'Coleman', 'Butler', 'Henderson', 'Barnes',
            'Gonzales', 'Fisher', 'Vasquez', 'Simmons', 'Romero', 'Jordan', 'Patterson', 'Alexander', 'Hamilton', 'Graham',
            'Reynolds', 'Griffin', 'Wallace', 'Moreno', 'West', 'Cole', 'Hayes', 'Bryant', 'Herrera', 'Gibson',
            'Ellis', 'Tran', 'Medina', 'Aguilar', 'Stevens', 'Murray', 'Ford', 'Castro', 'Marshall', 'Owens',
            'Harrison', 'Fernandez', 'McDonald', 'Woods', 'Washington', 'Kennedy', 'Wells', 'Vargas', 'Henry', 'Chen',
            'Freeman', 'Webb', 'Tucker', 'Guzman', 'Burns', 'Crawford', 'Olson', 'Simpson', 'Porter', 'Hunter',
            'Gordon', 'Mendez', 'Aguirre', 'Gutierrez', 'Schmidt', 'Carr', 'Vasquez', 'Castillo', 'Wheeler', 'Chapman',
            'Oliver', 'Montgomery', 'Richards', 'Williamson', 'Johnston', 'Banks', 'Meyer', 'Bishop', 'McCoy', 'Howell',
            'Alvarez', 'Morales', 'Murphy', 'Cook', 'Rogers', 'Gutierrez', 'Ortiz', 'Morgan', 'Cooper', 'Peterson',
            'Bailey', 'Reed', 'Kelly', 'Howard', 'Ramos', 'Kim', 'Cox', 'Ward', 'Richardson', 'Watson',
            'Brooks', 'Chavez', 'Wood', 'James', 'Bennett', 'Gray', 'Mendoza', 'Ruiz', 'Hughes', 'Price',
            'Alvarez', 'Castillo', 'Sanders', 'Patel', 'Myers', 'Long', 'Ross', 'Foster', 'Jimenez', 'Powell'
        ]
        
        # Advanced unique alias generation system for preview
        used_aliases_preview = set()
        used_names_preview = set()
        
        def generate_complex_alias_preview(first_name, last_name, index, attempt=0):
            """Generate complex, unique aliases using only letters for preview"""
            import string
            import hashlib
            import time
            
            # Base components
            fname = first_name.lower()
            lname = last_name.lower()
            f_initial = fname[0]
            l_initial = lname[0]
            
            # Generate random letter sequences
            def random_letters(length):
                return ''.join(random.choice(string.ascii_lowercase) for _ in range(length))
            
            # Complex patterns with letters only
            patterns = [
                # Pattern 1: Name + Random Letters + Index letters
                f"{fname}{lname}{random_letters(4)}{chr(97 + (index % 26))}{chr(97 + ((index // 26) % 26))}",
                f"{fname}{random_letters(3)}{lname}{random_letters(3)}",
                f"{f_initial}{lname}{random_letters(5)}{chr(97 + (index % 26))}",
                
                # Pattern 2: Mixed combinations with letters
                f"{fname}{random_letters(3)}{lname}{random_letters(2)}",
                f"{fname[0:3]}{lname[0:3]}{random_letters(4)}",
                f"{fname}{lname[0:2]}{random_letters(4)}{chr(97 + (index % 26))}",
                
                # Pattern 3: Hash-based unique identifiers (letters only)
                f"{fname}{lname}{hashlib.md5(f'{fname}{lname}{index}{attempt}'.encode()).hexdigest()[:6].replace('0', 'a').replace('1', 'b').replace('2', 'c').replace('3', 'd').replace('4', 'e').replace('5', 'f').replace('6', 'g').replace('7', 'h').replace('8', 'i').replace('9', 'j')}",
                f"{f_initial}{lname}{hashlib.md5(f'{index}{attempt}{time.time()}'.encode()).hexdigest()[:8].replace('0', 'k').replace('1', 'l').replace('2', 'm').replace('3', 'n').replace('4', 'o').replace('5', 'p').replace('6', 'q').replace('7', 'r').replace('8', 's').replace('9', 't')}",
                
                # Pattern 4: Complex letter combinations
                f"{fname}{random.choice(string.ascii_lowercase)}{lname}{random_letters(3)}",
                f"{fname[0:2]}{lname[0:2]}{random_letters(5)}",
                f"{fname}{lname[0:3]}{random_letters(4)}{chr(97 + (index % 26))}",
                
                # Pattern 5: Time-based unique identifiers (letters only)
                f"{fname}{lname}{random_letters(6)}{chr(97 + (index % 26))}",
                f"{f_initial}{lname}{random_letters(5)}{chr(97 + (index % 26))}",
                
                # Pattern 6: Advanced letter combinations
                f"{fname}{lname[0:4]}{random_letters(3)}",
                f"{fname[0:3]}{lname}{random_letters(4)}",
                f"{fname}{lname[0:2]}{random_letters(5)}{chr(97 + (index % 26))}",
                
                # Pattern 7: Complex letter patterns
                f"{fname}{lname}{random_letters(6)}",
                f"{f_initial}{lname[0:3]}{random_letters(5)}",
                f"{fname[0:2]}{lname[0:3]}{random_letters(4)}{chr(97 + (index % 26))}",
                
                # Pattern 8: Advanced letter combinations
                f"{fname}{lname}{random_letters(4)}{chr(97 + (index % 26))}",
                f"{f_initial}{lname}{random_letters(6)}{chr(97 + (index % 26))}",
                
                # Pattern 9: Multi-part unique identifiers (letters only)
                f"{fname}{lname[0:2]}{random_letters(3)}{chr(97 + (index % 26))}",
                f"{fname[0:4]}{lname}{random_letters(4)}{chr(97 + (index % 26))}",
                f"{fname}{lname[0:3]}{random_letters(5)}{chr(97 + (index % 26))}",
                
                # Pattern 10: Advanced hash combinations (letters only)
                f"{fname}{lname}{hashlib.sha256(f'{fname}{lname}{index}{attempt}{random_letters(4)}'.encode()).hexdigest()[:7].replace('0', 'a').replace('1', 'b').replace('2', 'c').replace('3', 'd').replace('4', 'e').replace('5', 'f').replace('6', 'g').replace('7', 'h').replace('8', 'i').replace('9', 'j')}",
                f"{f_initial}{lname}{hashlib.sha256(f'{index}{attempt}{time.time()}{random_letters(3)}'.encode()).hexdigest()[:9].replace('0', 'k').replace('1', 'l').replace('2', 'm').replace('3', 'n').replace('4', 'o').replace('5', 'p').replace('6', 'q').replace('7', 'r').replace('8', 's').replace('9', 't')}"
            ]
            
            # Try each pattern until we find a unique one
            for pattern in patterns:
                if pattern not in used_aliases_preview:
                    used_aliases_preview.add(pattern)
                    return pattern
            
            # If all patterns are taken, create a completely unique one (letters only)
            unique_id = f"{fname}{lname}{random_letters(8)}{chr(97 + (index % 26))}{chr(97 + (attempt % 26))}"
            used_aliases_preview.add(unique_id)
            return unique_id
        
        # Generate preview users
        for i in range(1, min(int(num_users), 10) + 1):  # Max 10 for preview
            # Ensure unique name combinations for preview
            while True:
                first_name = random.choice(first_names)
                last_name = random.choice(last_names)
                name_key = f"{first_name}_{last_name}"
                if name_key not in used_names_preview:
                    used_names_preview.add(name_key)
                    break
            
            alias = generate_complex_alias_preview(first_name, last_name, i)
            email = f"{alias}@{domain}"
            
            writer.writerow([
                first_name,           # First Name [Required]
                last_name,            # Last Name [Required]
                email,                # Email Address [Required]
                password,             # Password [Required]
                '',                   # Password Hash Function [UPLOAD ONLY]
                '/',                  # Org Unit Path [Required]
                '',                   # New Primary Email [UPLOAD ONLY]
                '',                   # Recovery Email
                '',                   # Home Secondary Email
                '',                   # Work Secondary Email
                '',                   # Recovery Phone [MUST BE IN THE E.164 FORMAT]
                '',                   # Work Phone
                '',                   # Home Phone
                '',                   # Mobile Phone
                '',                   # Work Address
                '',                   # Home Address
                f"EMP{i:04d}",        # Employee ID
                'Employee',           # Employee Type
                'Staff',              # Employee Title
                '',                   # Manager Email
                'IT',                 # Department
                '',                   # Cost Center
                '',                   # Building ID
                '',                   # Floor Name
                '',                   # Floor Section
                'False',              # Change Password at Next Sign-In
                '',                   # New Status [UPLOAD ONLY]
                'False'               # Advanced Protection Program enrollment
            ])
        
        # Get preview content
        output.seek(0)
        preview_content = output.getvalue()
        output.close()
        
        preview_lines = preview_content.strip().split('\n')
        
        return jsonify({
            'success': True,
            'preview': preview_lines,
            'total_lines': int(num_users),
            'showing_lines': len(preview_lines)
        })
        
    except Exception as e:
        app.logger.error(f"Preview CSV error: {e}")
        return jsonify({'success': False, 'error': str(e)})

# App Password Management API - OLD FUNCTION REMOVED

@app.route('/api/retrieve-app-passwords', methods=['POST'])
@login_required
def retrieve_app_passwords():
    """Retrieve stored app passwords without mutating storage.
    Rules:
    - Alias (username) static
    - App password static
    - Host/port static (smtp.gmail.com,587)
    - Domain provided per-alias via alias_domain_map, or a single domain for all
    Only the output is transformed; storage is not modified here.
    """
    try:
        # Check if user has permission
        if session.get('role') not in ['admin', 'support']:
            return jsonify({'success': False, 'error': 'Admin or support privileges required'})
        
        data = request.get_json(silent=True) or {}
        new_domain = (data.get('domain') or '').strip()
        alias_domain_map = data.get('alias_domain_map') or {}
        
        if not new_domain and not alias_domain_map:
            return jsonify({'success': False, 'error': 'Domain or alias_domain_map is required'})
        
        # Get app passwords from the SQLite app_passwords table
        result = google_api.get_all_app_passwords()
        
        if not result['success'] or not result['app_passwords']:
            return jsonify({
                'success': True,
                'domain': new_domain,
                'count': 0,
                'app_passwords': [],
                'message': f"No app passwords found to update for domain {new_domain}"
            })
        
        # Build output only (no writes)
        results = []
        for record in result['app_passwords']:
            user_alias = record['user_alias']
            username = user_alias.split('@')[0] if '@' in user_alias else user_alias
            # Determine target domain: per-alias overrides global
            target_domain = alias_domain_map.get(user_alias) or alias_domain_map.get(username) or new_domain
            results.append(f"{username}@{target_domain},{record['app_password']},smtp.gmail.com,587")

        return jsonify({
            'success': True,
            'domain': new_domain,
            'count': len(results),
            'app_passwords': results,
            'message': f"Generated {len(results)} SMTP lines using stored app passwords"
        })
        
    except Exception as e:
        logging.error(f"Retrieve app passwords error: {e}")
        return jsonify({'success': False, 'error': str(e)})

# OLD SQLite-based clear function removed - now using PostgreSQL-based function below

# OLD SQLite-based delete function removed - now using PostgreSQL-based function below

@app.route('/api/add-from-server-json', methods=['POST'])
@login_required
def add_from_server_json():
    # Check if user is mailer role (not allowed to add accounts)
    if session.get('role') == 'mailer':
        return jsonify({'success': False, 'error': 'Mailer users cannot add accounts'})
    
    # Only admin and support users can add accounts
    if session.get('role') not in ['admin', 'support']:
        return jsonify({'success': False, 'error': 'Admin or support privileges required'})
    
    try:
        data = request.get_json()
        emails = data.get('emails', [])
        
        if not emails:
            return jsonify({'success': False, 'error': 'No email addresses provided'})
        
        # Get server configuration
        from database import ServerConfig
        config = ServerConfig.query.first()
        if not config or not config.is_configured:
            return jsonify({'success': False, 'error': 'Server not configured. Please configure server settings first.'})
        
        # Connect to server and retrieve JSON files
        import paramiko
        import tempfile
        import os
        
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        
        try:
            # Connect to server
            if config.auth_method == 'password':
                ssh.connect(
                    config.host,
                    port=config.port,
                    username=config.username,
                    password=config.password,
                    timeout=10
                )
            else:
                # Create temporary key file
                with tempfile.NamedTemporaryFile(mode='w', delete=False) as key_file:
                    key_file.write(config.private_key)
                    key_file_path = key_file.name
                
                try:
                    private_key = paramiko.RSAKey.from_private_key_file(key_file_path)
                    ssh.connect(
                        config.host,
                        port=config.port,
                        username=config.username,
                        pkey=private_key,
                        timeout=10
                    )
                finally:
                    os.unlink(key_file_path)
            
            # Get JSON files using the new directory structure
            sftp = ssh.open_sftp()
            try:
                # Process each email
                added_accounts = []
                failed_accounts = []
                
                for email in emails:
                    email = email.strip()
                    if not email or '@' not in email:
                        failed_accounts.append({'email': email, 'error': 'Invalid email format'})
                        continue
                    
                    try:
                        # Construct the account-specific directory path
                        # Pattern: /home/brightmindscampus/{account}/*.json
                        account_dir = f"/home/brightmindscampus/{email}"
                        
                        # Check if account directory exists
                        try:
                            account_files = sftp.listdir(account_dir)
                        except FileNotFoundError:
                            failed_accounts.append({'email': email, 'error': f'Account directory not found: {account_dir}'})
                            continue
                        
                        # Look for JSON files in the account directory
                        import fnmatch
                        json_files = [f for f in account_files if fnmatch.fnmatch(f, '*.json')]
                        
                        if not json_files:
                            failed_accounts.append({'email': email, 'error': f'No JSON files found in directory: {account_dir}'})
                            continue
                        
                        # Use the first JSON file found (or could be modified to use specific pattern)
                        json_filename = json_files[0]
                        file_path = f"{account_dir}/{json_filename}"
                        
                            # Read and parse JSON file
                        try:
                            with sftp.open(file_path, 'r') as f:
                                content = f.read()
                                json_data = json.loads(content)
                            
                            # Extract client credentials
                            if 'installed' in json_data:
                                client_data = json_data['installed']
                            elif 'web' in json_data:
                                client_data = json_data['web']
                            else:
                                failed_accounts.append({'email': email, 'error': 'Invalid JSON format - missing installed/web section'})
                                continue
                            
                            client_id = client_data.get('client_id')
                            client_secret = client_data.get('client_secret')
                            
                            if not client_id or not client_secret:
                                failed_accounts.append({'email': email, 'error': 'Missing client_id or client_secret in JSON file'})
                                continue
                            
                            # Check if account already exists
                            from database import GoogleAccount
                            existing_account = GoogleAccount.query.filter_by(account_name=email).first()
                            if existing_account:
                                failed_accounts.append({'email': email, 'error': 'Account already exists'})
                                continue
                            
                            # Add new account
                            new_account = GoogleAccount(
                                account_name=email,
                                client_id=client_id,
                                client_secret=client_secret
                            )
                            db.session.add(new_account)
                            added_accounts.append(email)
                            
                        except Exception as e:
                            failed_accounts.append({'email': email, 'error': f'Failed to process account: {str(e)}'})
                            continue
                
                    except Exception as e:
                        failed_accounts.append({'email': email, 'error': f'Failed to process account: {str(e)}'})
                        continue
                
                # Commit all changes
                db.session.commit()
                
                ssh.close()
                
                # Prepare response message
                message = f"Successfully added {len(added_accounts)} account(s)."
                if failed_accounts:
                    message += f" Failed to add {len(failed_accounts)} account(s)."
                
                return jsonify({
                    'success': True,
                    'message': message,
                    'added_accounts': added_accounts,
                    'failed_accounts': failed_accounts
                })
                
            except Exception as e:
                ssh.close()
                return jsonify({'success': False, 'error': f'Failed to access files: {str(e)}'})
                
        except Exception as e:
            return jsonify({'success': False, 'error': f'SSH connection failed: {str(e)}'})
            
    except Exception as e:
        app.logger.error(f"Error adding from server JSON: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/test-smtp-sync', methods=['POST'])
@login_required
def test_smtp_credentials_sync():
    """Test SMTP credentials synchronously and return all results at once"""
    user_role = session.get('role')
    if user_role not in ['admin', 'mailer', 'support']:
        return jsonify({'success': False, 'error': 'Access denied. Valid user role required.'})
    
    try:
        data = request.get_json()
        credentials_text = data.get('credentials', '').strip()
        recipient_email = data.get('recipient_email', '').strip()
        smtp_server = data.get('smtp_server', 'smtp.gmail.com').strip()
        smtp_port = int(data.get('smtp_port', 587))
        
        if not credentials_text:
            return jsonify({'success': False, 'error': 'No credentials provided'})
        
        if not recipient_email or '@' not in recipient_email:
            return jsonify({'success': False, 'error': 'Invalid recipient email'})
        
        # Parse credentials (email:password format, one per line)
        credentials_lines = [line.strip() for line in credentials_text.split('\n') if line.strip()]
        
        results = []
        success_count = 0
        
        for line in credentials_lines:
            if ':' not in line:
                results.append({'email': line, 'status': 'error', 'error': 'Invalid format - use email:password'})
                continue
            
            try:
                email, password = line.split(':', 1)
                email = email.strip()
                password = password.strip()
                
                if not email or not password:
                    results.append({'email': email or 'unknown', 'status': 'error', 'error': 'Empty email or password'})
                    continue
                
                # Create message
                msg = MIMEMultipart()
                msg['From'] = email
                msg['To'] = recipient_email
                msg['Subject'] = f"SMTP Test from {email}"
                
                body = f"""
This is a test email sent from {email} using the GBot Web Application SMTP tester.

Test Details:
- Sender: {email}
- SMTP Server: {smtp_server}:{smtp_port}
- Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

If you received this email, the SMTP credentials are working correctly.
"""
                msg.attach(MIMEText(body, 'plain'))
                
                # Connect and send
                server = smtplib.SMTP(smtp_server, smtp_port, timeout=30)
                server.starttls()
                server.login(email, password)
                server.send_message(msg)
                server.quit()
                
                results.append({'email': email, 'status': 'success', 'message': f'Email sent to {recipient_email}'})
                success_count += 1
                
            except smtplib.SMTPAuthenticationError as e:
                results.append({'email': email, 'status': 'error', 'error': 'Authentication failed - check password'})
            except smtplib.SMTPException as e:
                results.append({'email': email, 'status': 'error', 'error': str(e)})
            except Exception as e:
                results.append({'email': email if 'email' in dir() else 'unknown', 'status': 'error', 'error': str(e)})
        
        return jsonify({
            'success': True,
            'results': results,
            'total': len(credentials_lines),
            'success_count': success_count,
            'fail_count': len(credentials_lines) - success_count
        })
        
    except Exception as e:
        app.logger.error(f"Error in SMTP sync test: {e}")
        return jsonify({'success': False, 'error': f'Server error: {str(e)}'})

@app.route('/api/test-smtp-progress', methods=['POST'])
@login_required
def test_smtp_credentials_progress():

    """Test SMTP credentials with progress tracking"""
    # Allow all user types (admin, mailer, support) to test SMTP
    user_role = session.get('role')
    if user_role not in ['admin', 'mailer', 'support']:
        return jsonify({'success': False, 'error': 'Access denied. Valid user role required.'})
    
    try:
        data = request.get_json()
        credentials_text = data.get('credentials', '').strip()
        recipient_email = data.get('recipient_email', '').strip()
        smtp_server = data.get('smtp_server', 'smtp.gmail.com').strip()
        smtp_port = int(data.get('smtp_port', 587))
        
        if not credentials_text:
            return jsonify({'success': False, 'error': 'No credentials provided'})
        
        if not recipient_email or '@' not in recipient_email:
            return jsonify({'success': False, 'error': 'Invalid recipient email'})
        
        # Parse credentials (email:password format, one per line)
        credentials_lines = [line.strip() for line in credentials_text.split('\n') if line.strip()]
        
        # Generate unique task ID
        import uuid
        task_id = str(uuid.uuid4())
        
        # Initialize progress tracking
        with progress_lock:
            progress_tracker[task_id] = {
                'status': 'running',
                'progress': 0,
                'total': len(credentials_lines),
                'current_email': '',
                'message': 'Starting SMTP testing...',
                'results': [],
                'success_count': 0,
                'fail_count': 0,
                'timestamp': datetime.now().isoformat()  # Required for cleanup logic
            }
            app.logger.info(f"[SMTP] Created progress tracker for task {task_id}")
            app.logger.info(f"[SMTP] Progress tracker now has {len(progress_tracker)} tasks: {list(progress_tracker.keys())}")

        
        # Start background task
        import threading
        def smtp_test_worker():
            import smtplib
            from email.mime.text import MIMEText
            from email.mime.multipart import MIMEMultipart
            import socket
            
            try:
                for i, line in enumerate(credentials_lines, 1):
                    with progress_lock:
                        if task_id not in progress_tracker:
                            break
                        progress_tracker[task_id]['progress'] = i
                        progress_tracker[task_id]['message'] = f'Testing credential {i}/{len(credentials_lines)}...'
                    
                    if ':' not in line:
                        with progress_lock:
                            if task_id in progress_tracker:
                                progress_tracker[task_id]['fail_count'] += 1
                                progress_tracker[task_id]['results'].append({
                                    'email': line,
                                    'status': 'error',
                                    'error': 'Invalid format - use email:password'
                                })
                        continue
                    
                    try:
                        email, password = line.split(':', 1)
                        email = email.strip()
                        password = password.strip()
                        
                        if not email or not password:
                            with progress_lock:
                                if task_id in progress_tracker:
                                    progress_tracker[task_id]['fail_count'] += 1
                                    progress_tracker[task_id]['results'].append({
                                        'email': email or 'unknown',
                                        'status': 'error',
                                        'error': 'Empty email or password'
                                    })
                            continue
                        
                        with progress_lock:
                            if task_id in progress_tracker:
                                progress_tracker[task_id]['current_email'] = email
                                progress_tracker[task_id]['message'] = f'Testing {email}...'
                        
                        # Create message
                        msg = MIMEMultipart()
                        msg['From'] = email
                        msg['To'] = recipient_email
                        msg['Subject'] = f"SMTP Test from {email}"
                        
                        body = f"""
This is a test email sent from {email} using the GBot Web Application SMTP tester.

Test Details:
- Sender: {email}
- SMTP Server: {smtp_server}:{smtp_port}
- Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

If you received this email, the SMTP credentials are working correctly.
"""
                        msg.attach(MIMEText(body, 'plain'))
                        
                        # Connect and send
                        server = smtplib.SMTP(smtp_server, smtp_port)
                        server.starttls()  # Enable encryption
                        server.login(email, password)
                        server.send_message(msg)
                        server.quit()
                        
                        with progress_lock:
                            if task_id in progress_tracker:
                                progress_tracker[task_id]['success_count'] += 1
                                progress_tracker[task_id]['results'].append({
                                    'email': email,
                                    'status': 'success',
                                    'message': f'Test email sent successfully to {recipient_email}'
                                })
                        
                    except smtplib.SMTPAuthenticationError as e:
                        with progress_lock:
                            if task_id in progress_tracker:
                                progress_tracker[task_id]['fail_count'] += 1
                                progress_tracker[task_id]['results'].append({
                                    'email': email,
                                    'status': 'error',
                                    'error': f'Authentication failed: {str(e)}'
                                })
                    except smtplib.SMTPException as e:
                        with progress_lock:
                            if task_id in progress_tracker:
                                progress_tracker[task_id]['fail_count'] += 1
                                progress_tracker[task_id]['results'].append({
                                    'email': email,
                                    'status': 'error',
                                    'error': f'SMTP error: {str(e)}'
                                })
                    except socket.gaierror as e:
                        with progress_lock:
                            if task_id in progress_tracker:
                                progress_tracker[task_id]['fail_count'] += 1
                                progress_tracker[task_id]['results'].append({
                                    'email': email,
                                    'status': 'error',
                                    'error': f'DNS/Network error: {str(e)}'
                                })
                    except Exception as e:
                        with progress_lock:
                            if task_id in progress_tracker:
                                progress_tracker[task_id]['fail_count'] += 1
                                progress_tracker[task_id]['results'].append({
                                    'email': email,
                                    'status': 'error',
                                    'error': f'Unexpected error: {str(e)}'
                                })
        
                # Mark as completed
                with progress_lock:
                    if task_id in progress_tracker:
                        progress_tracker[task_id]['status'] = 'completed'
                        progress_tracker[task_id]['message'] = 'SMTP testing completed'
                        
            except Exception as e:
                with progress_lock:
                    if task_id in progress_tracker:
                        progress_tracker[task_id]['status'] = 'error'
                        progress_tracker[task_id]['message'] = f'Error: {str(e)}'
        
        # Start the background thread
        thread = threading.Thread(target=smtp_test_worker)
        thread.daemon = True
        thread.start()
        
        return jsonify({
            'success': True,
            'task_id': task_id,
            'message': 'SMTP testing started'
        })
        
    except Exception as e:
        app.logger.error(f"Error starting SMTP testing: {e}")
        return jsonify({'success': False, 'error': f'Server error: {str(e)}'})

@app.route('/api/smtp-progress/<task_id>', methods=['GET'])
@login_required
def get_smtp_progress(task_id):
    """Get SMTP testing progress for a specific task"""
    try:
        app.logger.info(f"[SMTP] Progress request for task: {task_id}")
        
        with progress_lock:
            app.logger.info(f"[SMTP] Available tasks in tracker: {list(progress_tracker.keys())}")
            
            if task_id not in progress_tracker:
                app.logger.warning(f"[SMTP] Task {task_id} NOT FOUND in progress tracker")
                return jsonify({'success': False, 'error': 'Task not found or expired'})
            
            progress = progress_tracker[task_id].copy()
            app.logger.info(f"[SMTP] Task {task_id} found with status: {progress.get('status')}")
        
        return jsonify({
            'success': True,
            'progress': progress
        })
    except Exception as e:
        app.logger.error(f"Error getting SMTP progress: {e}")
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/test-simple-mega', methods=['POST'])
@login_required
def test_simple_mega():
    """Simple test endpoint to verify basic functionality"""
    try:
        data = request.get_json()
        accounts = data.get('accounts', [])
        features = data.get('features', {})
        
        app.logger.info(f"TEST: Received {len(accounts)} accounts")
        app.logger.info(f"TEST: Features: {features}")
        
        # Just return what we received for testing
        return jsonify({
            'success': True,
            'message': 'Test endpoint working',
            'received_accounts': accounts,
            'received_features': features,
            'total_accounts': len(accounts)
        })
        
    except Exception as e:
        app.logger.error(f"TEST ERROR: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/debug-mega-upgrade', methods=['POST'])
@login_required
def debug_mega_upgrade():
    """Debug endpoint to test mega upgrade without complex processing"""
    try:
        app.logger.info("DEBUG: Debug mega upgrade endpoint called")
        
        data = request.get_json()
        app.logger.info(f"DEBUG: Received data: {data}")
        
        accounts = data.get('accounts', [])
        features = data.get('features', {})
        
        app.logger.info(f"DEBUG: Accounts: {accounts}")
        app.logger.info(f"DEBUG: Features: {features}")
        
        # Test database connection
        try:
            from models import GoogleAccount
            account_count = GoogleAccount.query.count()
            app.logger.info(f"DEBUG: Database connection OK, {account_count} accounts found")
        except Exception as db_error:
            app.logger.error(f"DEBUG: Database error: {db_error}")
            return jsonify({'success': False, 'error': f'Database error: {str(db_error)}'})
        
        return jsonify({
            'success': True,
            'message': 'Debug endpoint working',
            'accounts_received': len(accounts),
            'features_received': features,
            'database_accounts': account_count,
            'debug_info': 'All systems operational'
        })
        
    except Exception as e:
        app.logger.error(f"DEBUG ERROR: {e}")
        import traceback
        app.logger.error(f"DEBUG TRACEBACK: {traceback.format_exc()}")
        return jsonify({'success': False, 'error': str(e), 'traceback': traceback.format_exc()})

@app.route('/api/mega-upgrade', methods=['POST'])
@login_required
# Rate limit removed - allow unlimited concurrent requests
def mega_upgrade():
    """Mega upgrade using EXISTING authentication and subdomain change functions"""
    # Import required models at the top
    from database import GoogleAccount, UsedDomain, UserAppPassword
    from sqlalchemy import text, func
    
    # Allow all user types (admin, mailer, support) to use mega upgrade
    user_role = session.get('role')
    if user_role not in ['admin', 'mailer', 'support']:
        return jsonify({'success': False, 'error': 'Access denied. Valid user role required.'})
    
    # Concurrency guard REMOVED - allow unlimited concurrent machines
    try:
        # Set longer timeout for this endpoint (5 minutes)
        import signal
        def timeout_handler(signum, frame):
            raise TimeoutError("Mega upgrade timeout")
        
        # Set timeout to 30 minutes (1800 seconds) for large user bases with multiple accounts
        signal.signal(signal.SIGALRM, timeout_handler)
        signal.alarm(1800)
        
        data = request.get_json()
        accounts = data.get('accounts', [])
        features = data.get('features', {})
        
        app.logger.info(f"🚀 Starting Mega Upgrade for {len(accounts)} accounts with 30-minute timeout")
        app.logger.info(f"📋 Accounts to process: {accounts}")
        app.logger.info(f"🔧 Features enabled: {features}")
        
        # Initialize progress tracking with detailed account counting
        import time
        progress_data = {
            'status': 'running',
            'total_accounts': len(accounts),
            'completed_accounts': 0,
            'successful_accounts': 0,
            'failed_accounts': 0,
            'current_account': None,
            'current_account_index': 0,
            'started_at': time.time(),
            'account_details': [],
            'progress_message': f'Processing account 0/{len(accounts)}: Initializing...'
        }
        
        if not accounts:
            return jsonify({'success': False, 'error': 'No accounts provided'})
        
        # No limit on accounts - support unlimited concurrent machines
        # if len(accounts) > 500:
        #     return jsonify({'success': False, 'error': 'Maximum 500 accounts allowed per batch for performance'})
        
        app.logger.info(f"Starting MEGA UPGRADE using EXISTING functions for {len(accounts)} accounts with features: {features}")
        
        successful_accounts = 0
        failed_accounts = 0
        final_results = []
        failed_details = []
        smtp_results = []
        # Map base domain (everything after first dot) -> next subdomain chosen by server
        next_domain_map = {}

        # Concurrency primitives
        results_lock = threading.Lock()
        session_lock = threading.Lock()

        # Worker function to process a single account
        def process_account(account_email: str, index: int):
            nonlocal successful_accounts, failed_accounts
            
            # Update progress with account counting
            progress_data['current_account'] = account_email
            progress_data['current_account_index'] = index + 1
            progress_data['progress_message'] = f'Processing account {index + 1}/{len(accounts)}: {account_email}'
            
            # TEMPORARILY DISABLED: Database-based locking for multi-machine synchronization
            # This was causing accounts to be skipped in single-machine usage
            lock_acquired = True  # Always allow processing for now
            lock_conn = None
            app.logger.info(f"🔓 Lock mechanism disabled - processing account {account_email}")
            
            try:
                app.logger.info(f"🚀 Worker {index+1} starting for account: {account_email}")
                app.logger.info(f"📊 Progress: Processing account {index + 1}/{len(accounts)}: {account_email}")
                
                # Create a new app context for this thread
                with app.app_context():
                    # Push a new request context to avoid "Working outside of request context" error
                    with app.test_request_context():
                        acct = (account_email or '').strip()
                        if not acct:
                            app.logger.warning(f"Worker {index+1}: Empty account email")
                            return

                        app.logger.info(f"🔧 Worker {index+1} processing account: {acct}")

                        # Step 1: Find account in database (use exact match like manual authentication)
                        google_account = GoogleAccount.query.filter_by(account_name=acct).first()
                        
                        # If exact match fails, try case-insensitive match as fallback
                        if not google_account:
                            app.logger.info(f"Exact match failed for {acct}, trying case-insensitive match")
                        google_account = GoogleAccount.query.filter(
                            func.lower(GoogleAccount.account_name) == acct.lower()
                        ).first()
                        if not google_account:
                            app.logger.warning(f"Account {acct} not found in database")
                            all_accounts = GoogleAccount.query.all()
                            with results_lock:
                                failed_accounts += 1
                                failed_details.append({
                                    'account': acct,
                                    'step': 'database_lookup',
                                    'error': f"Account not found in database. Available accounts: {[acc.account_name for acc in all_accounts]}"
                                })
                            return
                        
                        app.logger.info(f"Found account in database: {google_account.account_name} (input was: {acct})")
                        app.logger.info(f"Case comparison: '{acct.lower()}' == '{google_account.account_name.lower()}' = {acct.lower() == google_account.account_name.lower()}")
                        
                        # Debug: Check if this account has tokens
                        app.logger.info(f"Checking tokens for account: {google_account.account_name}")
                        has_tokens = google_api.is_token_valid(google_account.account_name)
                        app.logger.info(f"Token validation result: {has_tokens}")
                        
                        # Debug: Also check with the original input case
                        app.logger.info(f"Checking tokens for original input: {acct}")
                        has_tokens_original = google_api.is_token_valid(acct)
                        app.logger.info(f"Token validation result for original: {has_tokens_original}")

                        original_account_name = google_account.account_name

                        # Protect only truly critical system accounts (disabled for now)
                        # critical_accounts = ['system@', 'noreply@', 'postmaster@']
                        # if any(original_account_name.lower().startswith(prefix) for prefix in critical_accounts):
                        #     app.logger.warning(f"Skipping critical account {acct}")
                        #     with results_lock:
                        #         failed_accounts += 1
                        #         failed_details.append({
                        #             'account': acct,
                        #             'step': 'protection',
                        #             'error': 'Critical system accounts cannot be modified for security'
                        #         })
                        #     return
                        
                        app.logger.info(f"Processing account {acct} - protection check disabled")

                        # Step 2: Authenticate
                        authenticated_account_name = None
                        if features.get('authenticate'):
                            # Use the database account name (correct case) for authentication
                            db_account_name = google_account.account_name
                            app.logger.info(f"Authenticating with database account name: {db_account_name}")
                            
                            # Try authentication with database account name first
                            auth_success = False
                            if google_api.is_token_valid(db_account_name):
                                auth_success = google_api.authenticate_with_tokens(db_account_name)
                                app.logger.info(f"Authentication with db account name result: {auth_success}")
                                if auth_success:
                                    authenticated_account_name = db_account_name
                            
                            # If that fails, try with original input (in case tokens are stored under original case)
                            if not auth_success and acct != db_account_name:
                                app.logger.info(f"Trying authentication with original input: {acct}")
                            if google_api.is_token_valid(acct):
                                    auth_success = google_api.authenticate_with_tokens(acct)
                                    app.logger.info(f"Authentication with original input result: {auth_success}")
                                    if auth_success:
                                        authenticated_account_name = acct
                            
                            if not auth_success:
                                with results_lock:
                                    failed_accounts += 1
                                    failed_details.append({
                                        'account': acct,
                                        'step': 'authentication',
                                        'error': 'No valid tokens found - OAuth required'
                                    })
                                return

                        # Step 3: Change subdomain
                        domain_users = []
                        next_domain = None
                        if features.get('changeSubdomain'):
                            with session_lock:
                                original_session_account = session.get('current_account_name')
                                # Use the account name that successfully authenticated
                                session['current_account_name'] = authenticated_account_name or db_account_name
                            try:
                                result = google_api.get_domain_info()
                                if not result['success']:
                                    with results_lock:
                                        failed_accounts += 1
                                        failed_details.append({
                                            'account': acct,
                                            'step': 'changeSubdomain',
                                            'error': f"Failed to get domain info: {result['error']}"
                                        })
                                    return

                                domains = result['domains']

                                # Get all users
                                all_users = []
                                page_token = None
                                while True:
                                    try:
                                        users_result = google_api.service.users().list(
                                            customer='my_customer',
                                            maxResults=500,
                                            pageToken=page_token
                                        ).execute()
                                        users = users_result.get('users', [])
                                        all_users.extend(users)
                                        page_token = users_result.get('nextPageToken')
                                        if not page_token:
                                            break
                                    except Exception as e:
                                        app.logger.error(f"Error getting users: {e}")
                                        break

                                # Partition users
                                all_regular_users = []
                                for user in all_users:
                                    email = user.get('primaryEmail', '')
                                    if '@' in email and not (user.get('isAdmin', False) or user.get('isDelegatedAdmin', False)):
                                        all_regular_users.append(email)

                                if not all_regular_users:
                                    with results_lock:
                                        failed_accounts += 1
                                        failed_details.append({
                                            'account': acct,
                                            'step': 'changeSubdomain',
                                            'error': 'No regular users found in Google Workspace'
                                        })
                                    return

                                # Domain usage counts
                                domain_user_counts = {}
                                for user in all_users:
                                    email = user.get('primaryEmail', '')
                                    if '@' in email:
                                        d = email.split('@')[1]
                                        domain_user_counts[d] = domain_user_counts.get(d, 0) + 1

                                available_domains = []
                                domain_records = {r.domain_name: r for r in UsedDomain.query.all()}
                                for d in domains:
                                    dname = d.get('domainName') or d.get('domain_name') or ''
                                    if not dname:
                                        continue
                                    user_count = domain_user_counts.get(dname, 0)
                                    rec = domain_records.get(dname)
                                    ever_used = bool(getattr(rec, 'ever_used', False)) if rec else False
                                    if user_count == 0 and not ever_used:
                                        available_domains.append(dname)

                                if not available_domains:
                                    with results_lock:
                                        failed_accounts += 1
                                        failed_details.append({
                                            'account': acct,
                                            'step': 'changeSubdomain',
                                            'error': 'No available domains found'
                                        })
                                    return

                                available_domains.sort()
                                next_domain = available_domains[0]
                                # Record mapping for frontend (base -> next_domain)
                                try:
                                    if '.' in next_domain:
                                        base = next_domain.split('.', 1)[1]
                                        with results_lock:
                                            next_domain_map[base] = next_domain
                                except Exception:
                                    pass
                                domain_users = all_regular_users

                                # Apply user updates with enhanced retry logic and synchronization
                                successful_user_changes = 0
                                failed_user_changes = []
                                max_retries = 5  # Increased retries
                                retry_delay = 2  # Increased delay between retries
                                
                                app.logger.info(f"🔄 Starting domain change for {len(domain_users)} users in account {acct}")
                                
                                # Get the current domain being used (before change)
                                current_domain = None
                                if domain_users:
                                    current_domain = domain_users[0].split('@')[1]
                                    app.logger.info(f"📋 Current domain: {current_domain}, Target domain: {next_domain}")
                                
                                # Process users with enhanced synchronization
                                for u_email in domain_users:
                                    username = u_email.split('@')[0]
                                    new_email = f"{username}@{next_domain}"
                                    user_success = False
                                    
                                    # Enhanced retry logic for each user
                                    for retry_attempt in range(max_retries):
                                        try:
                                            app.logger.info(f"🔄 Attempt {retry_attempt + 1}/{max_retries} for user {u_email} -> {new_email}")
                                            
                                            # Add synchronization delay to prevent API rate limiting
                                            if retry_attempt > 0:
                                                import time
                                                time.sleep(retry_delay * retry_attempt)  # Progressive delay
                                            
                                            # Execute the update with timeout
                                            google_api.service.users().update(userKey=u_email, body={'primaryEmail': new_email}).execute()
                                            
                                            # Verify the change was applied
                                            try:
                                                updated_user = google_api.service.users().get(userKey=new_email).execute()
                                                if updated_user.get('primaryEmail') == new_email:
                                                    successful_user_changes += 1
                                                    user_success = True
                                                    app.logger.info(f"✅ Successfully updated and verified user {u_email} -> {new_email}")
                                                    break
                                                else:
                                                    app.logger.warning(f"⚠️ User update not verified for {u_email}")
                                            except Exception as verify_e:
                                                app.logger.warning(f"⚠️ Verification failed for {u_email}: {verify_e}")
                                                # Still count as success if the update call succeeded
                                                successful_user_changes += 1
                                                user_success = True
                                                app.logger.info(f"✅ Successfully updated user {u_email} -> {new_email} (verification failed)")
                                                break
                                                
                                        except Exception as e:
                                            app.logger.warning(f"⚠️ Attempt {retry_attempt + 1} failed for user {u_email}: {e}")
                                            if retry_attempt < max_retries - 1:
                                                import time
                                                time.sleep(retry_delay)  # Wait before retry
                                            else:
                                                app.logger.error(f"❌ All attempts failed for user {u_email}: {e}")
                                                failed_user_changes.append({
                                                    'user': u_email,
                                                    'error': str(e),
                                                    'attempts': max_retries
                                                })
                                    
                                    # CRITICAL: If user failed, we must retry the entire account
                                    if not user_success:
                                        app.logger.error(f"❌ CRITICAL: User {u_email} failed to update after {max_retries} attempts")
                                        # Mark this account as needing complete retry
                                        with results_lock:
                                            failed_accounts += 1
                                            failed_details.append({
                                                'account': acct,
                                                'step': 'changeSubdomain',
                                                'error': f"User {u_email} failed to update after {max_retries} attempts - account needs retry",
                                                'failed_users': failed_user_changes,
                                                'retry_needed': True,
                                                'incomplete': True
                                            })
                                        return  # Exit immediately - don't proceed with partial success
                                
                                # Log completion status
                                total_users = len(domain_users)
                                success_rate = (successful_user_changes / total_users) * 100 if total_users > 0 else 0
                                app.logger.info(f"📊 Account {acct} domain change results:")
                                app.logger.info(f"   Total users: {total_users}")
                                app.logger.info(f"   Successful: {successful_user_changes}")
                                app.logger.info(f"   Failed: {len(failed_user_changes)}")
                                app.logger.info(f"   Success rate: {success_rate:.1f}%")
                                
                                # Validation: Check if all users were actually changed
                                if successful_user_changes < total_users:
                                    app.logger.warning(f"⚠️ INCOMPLETE: Only {successful_user_changes}/{total_users} users changed for account {acct}")
                                    # List the failed users for debugging
                                    for failed_user in failed_user_changes:
                                        app.logger.warning(f"   Failed user: {failed_user['user']} - {failed_user['error']}")
                                else:
                                    app.logger.info(f"✅ COMPLETE: All {total_users} users successfully changed for account {acct}")
                                
                                # Only proceed if we have 100% success or handle partial failures
                                if successful_user_changes > 0:
                                    if next_domain in domain_records:
                                        domain_records[next_domain].user_count += successful_user_changes
                                    else:
                                        try:
                                            # Use PostgreSQL UPSERT for atomic operation
                                            from sqlalchemy import text
                                            
                                            upsert_sql = text("""
                                                INSERT INTO used_domain (domain_name, user_count, is_verified, ever_used, created_at, updated_at)
                                                VALUES (:domain_name, :user_count, :is_verified, :ever_used, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                                                ON CONFLICT (domain_name) 
                                                DO UPDATE SET 
                                                    user_count = used_domain.user_count + :user_count,
                                                    ever_used = TRUE,
                                                    updated_at = CURRENT_TIMESTAMP
                                            """)
                                            
                                            db.session.execute(upsert_sql, {
                                                'domain_name': next_domain,
                                                'user_count': successful_user_changes,
                                                'is_verified': True,
                                                'ever_used': True
                                            })
                                            
                                        except Exception as e:
                                            # Rollback any failed transaction before trying fallback
                                            db.session.rollback()
                                            app.logger.warning(f"UPSERT failed for {next_domain}, using fallback method: {e}")
                                            
                                            try:
                                                # Try to fetch existing record first
                                                existing_domain = UsedDomain.query.filter_by(domain_name=next_domain).first()
                                                
                                                if existing_domain:
                                                    # Update existing record
                                                    existing_domain.user_count += successful_user_changes
                                                    existing_domain.ever_used = True
                                                else:
                                                    # Create new record
                                                    db.session.add(UsedDomain(domain_name=next_domain, user_count=successful_user_changes, is_verified=True, ever_used=True))
                                                    
                                            except Exception as e2:
                                                # Handle duplicate key violation - another process might have created it
                                                if 'duplicate key' in str(e2).lower() or 'unique constraint' in str(e2).lower():
                                                    db.session.rollback()
                                                    # Try to fetch the existing record
                                                    existing_domain = UsedDomain.query.filter_by(domain_name=next_domain).first()
                                                    if existing_domain:
                                                        existing_domain.user_count += successful_user_changes
                                                        existing_domain.ever_used = True
                                                    else:
                                                        app.logger.error(f"Failed to find or create domain record for {next_domain}")
                                                        # Skip this domain and continue processing
                                                else:
                                                    db.session.rollback()
                                                    raise e2
                                    db.session.commit()
                                    
                                    # If we have failures, we need to retry the entire account
                                    if len(failed_user_changes) > 0:
                                        app.logger.warning(f"⚠️ Account {acct} has {len(failed_user_changes)} failed users - marking for retry")
                                        # Mark this account as needing retry
                                        with results_lock:
                                            failed_accounts += 1
                                            failed_details.append({
                                                'account': acct,
                                                'step': 'changeSubdomain',
                                                'error': f"Partial failure: {len(failed_user_changes)}/{total_users} users failed to update",
                                                'failed_users': failed_user_changes,
                                                'success_rate': f"{success_rate:.1f}%",
                                                'retry_needed': True
                                            })
                                        return
                                    
                                    # Final validation: Verify the changes were actually applied
                                    app.logger.info(f"🔍 Validating domain changes for account {acct}...")
                                    try:
                                        # Get updated user list to verify changes
                                        verification_users = []
                                        page_token = None
                                        while True:
                                            try:
                                                users_result = google_api.service.users().list(
                                                    customer='my_customer',
                                                    maxResults=500,
                                                    pageToken=page_token
                                                ).execute()
                                                users = users_result.get('users', [])
                                                verification_users.extend(users)
                                                page_token = users_result.get('nextPageToken')
                                                if not page_token:
                                                    break
                                            except Exception as e:
                                                app.logger.error(f"Error during verification: {e}")
                                                break
                                        
                                        # Count users on the new domain
                                        users_on_new_domain = 0
                                        for user in verification_users:
                                            email = user.get('primaryEmail', '')
                                            if email.endswith(f'@{next_domain}'):
                                                users_on_new_domain += 1
                                        
                                        app.logger.info(f"🔍 Verification results for {acct}:")
                                        app.logger.info(f"   Expected users on {next_domain}: {successful_user_changes}")
                                        app.logger.info(f"   Actual users on {next_domain}: {users_on_new_domain}")
                                        
                                        if users_on_new_domain != successful_user_changes:
                                            app.logger.warning(f"⚠️ Verification mismatch for {acct}: expected {successful_user_changes}, found {users_on_new_domain}")
                                        else:
                                            app.logger.info(f"✅ Verification successful for {acct}: all {users_on_new_domain} users confirmed on {next_domain}")
                                            
                                            # Mark the OLD domain as used since users moved away from it
                                            if current_domain and current_domain != next_domain:
                                                try:
                                                    app.logger.info(f"🔄 Marking old domain {current_domain} as used...")
                                                    # Use UPSERT to handle the old domain
                                                    db.session.execute(text("""
                                                        INSERT INTO used_domain (domain_name, user_count, is_verified, ever_used, created_at, updated_at)
                                                        VALUES (:domain_name, :user_count, :is_verified, :ever_used, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                                                        ON CONFLICT (domain_name) 
                                                        DO UPDATE SET 
                                                            user_count = used_domain.user_count + :user_count,
                                                            ever_used = TRUE,
                                                            updated_at = CURRENT_TIMESTAMP
                                                    """), {
                                                        'domain_name': current_domain,
                                                        'user_count': successful_user_changes,
                                                        'is_verified': True,
                                                        'ever_used': True
                                                    })
                                                    db.session.commit()
                                                    app.logger.info(f"✅ Successfully marked old domain {current_domain} as used")
                                                except Exception as e:
                                                    db.session.rollback()
                                                    app.logger.warning(f"⚠️ Failed to mark old domain {current_domain} as used: {e}")
                                            
                                    except Exception as e:
                                        app.logger.error(f"❌ Verification failed for {acct}: {e}")
                                else:
                                    app.logger.error(f"❌ No users were successfully updated for account {acct}")
                                    with results_lock:
                                        failed_accounts += 1
                                        failed_details.append({
                                            'account': acct,
                                            'step': 'changeSubdomain',
                                            'error': 'No users were successfully updated',
                                            'failed_users': failed_user_changes
                                        })
                                    return
                            finally:
                                with session_lock:
                                    if original_session_account:
                                        session['current_account_name'] = original_session_account
                                    else:
                                        session.pop('current_account_name', None)

                        # Step 4: Generate app passwords (COMPLETELY DISABLED)
                        # App password generation is disabled - no processing

                        with results_lock:
                            successful_accounts += 1
                            account_result = {
                                'account': acct,
                                'new_account_name': original_account_name,
                                'users_processed': successful_user_changes,
                                'total_users': len(domain_users) if domain_users else 0,
                                'success_rate': f"{success_rate:.1f}%",
                                'status': 'success',
                                'domain_changed': next_domain if next_domain else None,
                                'completed_at': time.time()
                            }
                            final_results.append(account_result)
                            
                            # Update progress tracking
                            progress_data['successful_accounts'] = successful_accounts
                            progress_data['completed_accounts'] = successful_accounts + failed_accounts
                            progress_data['progress_message'] = f'Completed account {index + 1}/{len(accounts)}: {acct} (Success)'
                            progress_data['account_details'].append(account_result)
                            
                            app.logger.info(f"✅ Worker {index+1} completed successfully for {acct} - {successful_user_changes}/{len(domain_users) if domain_users else 0} users changed ({success_rate:.1f}%)")
            except Exception as e:
                app.logger.error(f"❌ Worker {index+1} failed for account {account_email}: {e}")
                import traceback
                app.logger.error(f"Worker {index+1} traceback: {traceback.format_exc()}")
                with results_lock:
                    failed_accounts += 1
                    failed_detail = {
                        'account': account_email,
                        'step': 'processing',
                        'error': str(e),
                        'completed_at': time.time()
                    }
                    failed_details.append(failed_detail)
                    
                    # Update progress tracking
                    progress_data['failed_accounts'] = failed_accounts
                    progress_data['completed_accounts'] = successful_accounts + failed_accounts
                    progress_data['progress_message'] = f'Failed account {index + 1}/{len(accounts)}: {account_email} (Error)'
                    progress_data['account_details'].append({
                        'account': account_email,
                        'status': 'failed',
                        'error': str(e),
                        'completed_at': time.time()
                    })
            finally:
                # Always release the lock
                if lock_acquired and lock_conn:
                    try:
                        lock_cursor = lock_conn.cursor()
                        lock_cursor.execute('DELETE FROM mega_upgrade_locks WHERE account_email = ?', (account_email,))
                        lock_conn.commit()
                        app.logger.info(f"🔓 Lock released for account {account_email}")
                    except Exception as lock_e:
                        app.logger.error(f"❌ Failed to release lock for {account_email}: {lock_e}")
                    finally:
                        lock_conn.close()

        # Run with reduced workers for better synchronization and multi-machine compatibility
        max_workers = min(4, len(accounts))  # Reduced workers for better synchronization
        max_account_retries = 3  # Increased retries for failed accounts
        app.logger.info(f"Starting synchronized processing with {max_workers} workers for {len(accounts)} accounts")
        app.logger.info(f"Multi-machine compatibility: Using reduced workers and enhanced synchronization")
        
        # First pass - process all accounts
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = [executor.submit(process_account, acc, idx) for idx, acc in enumerate(accounts)]
            app.logger.info(f"Submitted {len(futures)} tasks to thread pool")
            
            completed_tasks = 0
            for i, f in enumerate(as_completed(futures)):
                try:
                    result = f.result()
                    completed_tasks += 1
                    progress_percent = int((completed_tasks / len(futures)) * 100)
                    app.logger.info(f"Task {completed_tasks}/{len(futures)} completed successfully ({progress_percent}%)")
                except Exception as e:
                    completed_tasks += 1
                    progress_percent = int((completed_tasks / len(futures)) * 100)
                    app.logger.error(f"Task {completed_tasks}/{len(futures)} failed ({progress_percent}%): {e}")
                    import traceback
                    app.logger.error(f"Task {completed_tasks} traceback: {traceback.format_exc()}")
        
        # Second pass - retry failed accounts
        if failed_accounts > 0:
            app.logger.info(f"🔄 Retrying {failed_accounts} failed accounts...")
            retry_accounts = [detail['account'] for detail in failed_details if 'changeSubdomain' in detail.get('step', '')]
            
            if retry_accounts:
                app.logger.info(f"🔄 Retrying domain change for: {retry_accounts}")
                with ThreadPoolExecutor(max_workers=min(4, len(retry_accounts))) as retry_executor:
                    retry_futures = [retry_executor.submit(process_account, acc, idx + 1000) for idx, acc in enumerate(retry_accounts)]
                    
                    for f in as_completed(retry_futures):
                        try:
                            result = f.result()
                            app.logger.info(f"✅ Retry successful for account")
                        except Exception as e:
                            app.logger.error(f"❌ Retry failed: {e}")
        
        # Final progress update
        progress_data['status'] = 'completed'
        progress_data['completed_at'] = time.time()
        progress_data['total_time'] = progress_data['completed_at'] - progress_data['started_at']
        
        app.logger.info(f"🎯 MEGA UPGRADE FINAL RESULTS:")
        app.logger.info(f"   Total accounts: {len(accounts)}")
        app.logger.info(f"   Successful: {successful_accounts}")
        app.logger.info(f"   Failed: {failed_accounts}")
        app.logger.info(f"   Success rate: {(successful_accounts / len(accounts)) * 100:.1f}%")
        app.logger.info(f"   Total time: {progress_data['total_time']:.1f} seconds")
        
        # Log detailed results for each account
        for result in final_results:
            app.logger.info(f"   ✅ {result['account']}: {result['users_processed']}/{result['total_users']} users ({result['success_rate']})")
        
        for detail in failed_details:
            app.logger.info(f"   ❌ {detail['account']}: {detail.get('error', 'Unknown error')}")
        
        # Cancel timeout
        signal.alarm(0)
        
        return jsonify({
            'success': True,
            'message': f'Mega upgrade completed using existing functions: {successful_accounts} successful, {failed_accounts} failed',
            'total_accounts': len(accounts),
            'successful_accounts': successful_accounts,
            'failed_accounts': failed_accounts,
            'success_rate': f"{(successful_accounts / len(accounts)) * 100:.1f}%",
            'total_time': f"{progress_data['total_time']:.1f} seconds",
            'final_results': final_results,
            'failed_details': failed_details,
            'smtp_results': smtp_results,
            'next_domain_map': next_domain_map,
            'progress_data': progress_data
        })
        
    except TimeoutError as e:
        # Cancel timeout
        signal.alarm(0)
        app.logger.error(f"Mega upgrade timeout after 30 minutes: {e}")
        return jsonify({
            'success': False, 
            'error': f'Process timed out after 30 minutes. Partial results: {successful_accounts} successful, {failed_accounts} failed',
            'partial_results': {
                'successful_accounts': successful_accounts,
                'failed_accounts': failed_accounts,
                'failed_details': failed_details,
                'next_domain_map': next_domain_map
            }
        })
    except Exception as e:
        # Cancel timeout
        signal.alarm(0)
        app.logger.error(f"Error in mega upgrade: {e}")
        return jsonify({
            'success': False, 
            'error': str(e),
            'partial_results': {
                'successful_accounts': successful_accounts if 'successful_accounts' in locals() else 0,
                'failed_accounts': failed_accounts if 'failed_accounts' in locals() else 0,
                'failed_details': failed_details if 'failed_details' in locals() else []
            }
        })

@app.route('/api/debug-progress', methods=['GET'])
@login_required
def debug_progress():
    """Debug endpoint to check progress tracking system"""
    try:
        with progress_lock:
            return jsonify({
                'success': True,
                'active_tasks': list(progress_tracker.keys()),
                'task_count': len(progress_tracker),
                'tasks': progress_tracker,
                'timestamp': datetime.now().isoformat()
            })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/debug-progress-raw', methods=['GET'])
@login_required
def debug_progress_raw():
    """Debug endpoint to check progress tracking system without any processing"""
    try:
        with progress_lock:
            return jsonify({
                'success': True,
                'raw_tracker': progress_tracker,
                'timestamp': datetime.now().isoformat()
            })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/test-progress', methods=['POST'])
@login_required
def test_progress():
    """Test endpoint to create a test task and verify progress tracking"""
    try:
        # Create a test task
        test_task_id = str(uuid.uuid4())
        logging.info(f"Creating test task: {test_task_id}")
        
        # Update progress
        update_progress(test_task_id, 0, 100, "testing", "Test task created")
        
        # Verify it was created
        with progress_lock:
            if test_task_id in progress_tracker:
                logging.info(f"Test task {test_task_id} successfully created")
                return jsonify({
                    'success': True,
                    'test_task_id': test_task_id,
                    'message': 'Test task created successfully',
                    'progress_tracker_size': len(progress_tracker)
                })
            else:
                logging.error(f"Test task {test_task_id} not found in progress tracker")
                return jsonify({
                    'success': False,
                    'error': 'Test task not found in progress tracker'
                })
    except Exception as e:
        logging.error(f"Error in test progress: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/test-mega-upgrade', methods=['POST'])
@login_required
def test_mega_upgrade():
    """Test endpoint to debug mega upgrade issues"""
    try:
        data = request.get_json()
        accounts = data.get('accounts', [])
        features = data.get('features', {})
        
        if not accounts:
            return jsonify({'success': False, 'error': 'No accounts provided'})
        
        # Simple test without threading
        results = []
        for account_email in accounts[:2]:  # Test with first 2 accounts only
            account_email = account_email.strip()
            if account_email:
                # Simulate processing
                domain = account_email.split('@')[1] if '@' in account_email else 'domain.com'
                result = f"user@{domain},app_password123,smtp.gmail.com,587"
                results.append(result)
        
        return jsonify({
            'success': True,
            'message': f'Test completed successfully for {len(results)} accounts',
            'results': results
        })
        
    except Exception as e:
        app.logger.error(f"Error in test mega upgrade: {e}")
        return jsonify({'success': False, 'error': f'Server error: {str(e)}'})

@app.route('/api/mega-upgrade-progress/<task_id>')
@login_required
def get_mega_upgrade_progress(task_id):
    """Get mega upgrade progress with enhanced error handling"""
    try:
        with progress_lock:
            if task_id not in progress_tracker:
                # Log the missing task for debugging
                app.logger.warning(f"Progress request for missing task: {task_id}")
                return jsonify({'success': False, 'error': 'Task not found or expired'})
            
            progress_data = progress_tracker[task_id].copy()
            
            # Clean up completed tasks after 15 minutes (increased from 10)
            if progress_data['status'] in ['completed', 'error']:
                import time
                if 'completed_at' not in progress_data:
                    progress_data['completed_at'] = time.time()
                elif time.time() - progress_data['completed_at'] > 900:  # 15 minutes
                    app.logger.info(f"Cleaning up expired task: {task_id}")
                    del progress_tracker[task_id]
            
            return jsonify({
                'success': True,
                'progress': progress_data
            })
            
    except Exception as e:
        app.logger.error(f"Error getting mega upgrade progress: {e}")
        return jsonify({'success': False, 'error': f'Server error: {str(e)}'})

@app.route('/api/test-smtp', methods=['POST'])
@login_required
def test_smtp():
    """Test SMTP credentials by sending test emails (legacy endpoint for compatibility)"""
    # Allow all user types (admin, mailer, support) to test SMTP
    user_role = session.get('role')
    if user_role not in ['admin', 'mailer', 'support']:
        return jsonify({'success': False, 'error': 'Access denied. Valid user role required.'})
    
    try:
        data = request.get_json()
        credentials_text = data.get('credentials', '').strip()
        recipient_email = data.get('recipient_email', '').strip()
        smtp_server = data.get('smtp_server', 'smtp.gmail.com').strip()
        smtp_port = int(data.get('smtp_port', 587))
        
        if not credentials_text:
            return jsonify({'success': False, 'error': 'No credentials provided'})
        
        if not recipient_email or '@' not in recipient_email:
            return jsonify({'success': False, 'error': 'Invalid recipient email'})
        
        # Parse credentials (email:password format, one per line)
        credentials_lines = [line.strip() for line in credentials_text.split('\n') if line.strip()]
        
        if not credentials_lines:
            return jsonify({'success': False, 'error': 'No valid credentials found'})
        
        # Test first credential only for legacy compatibility
        first_credential = credentials_lines[0]
        if ':' not in first_credential:
            return jsonify({'success': False, 'error': 'Invalid credential format. Use email:password'})
        
        email, password = first_credential.split(':', 1)
        email = email.strip()
        password = password.strip()
        
        if not email or not password:
            return jsonify({'success': False, 'error': 'Email and password are required'})
        
        # Test SMTP connection
        import smtplib
        from email.mime.text import MIMEText
        from email.mime.multipart import MIMEMultipart
        
        try:
            # Create message
            msg = MIMEMultipart()
            msg['From'] = email
            msg['To'] = recipient_email
            msg['Subject'] = "Test Email from GBot Web App"
            
            body = f"""
            This is a test email sent from GBot Web App.
            
            SMTP Configuration:
            - Server: {smtp_server}
            - Port: {smtp_port}
            - From: {email}
            - To: {recipient_email}
            - Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
            
            If you receive this email, the SMTP configuration is working correctly.
            """
            
            msg.attach(MIMEText(body, 'plain'))
            
            # Send email
            server = smtplib.SMTP(smtp_server, smtp_port)
            server.starttls()
            server.login(email, password)
            text = msg.as_string()
            server.sendmail(email, recipient_email, text)
            server.quit()
            
            return jsonify({
                'success': True,
                'message': f'Test email sent successfully to {recipient_email}',
                'details': {
                    'from': email,
                    'to': recipient_email,
                    'smtp_server': smtp_server,
                    'smtp_port': smtp_port
                }
            })
            
        except smtplib.SMTPAuthenticationError:
            return jsonify({'success': False, 'error': 'SMTP authentication failed. Check email and password.'})
        except smtplib.SMTPConnectError:
            return jsonify({'success': False, 'error': f'Cannot connect to SMTP server {smtp_server}:{smtp_port}'})
        except smtplib.SMTPException as e:
            return jsonify({'success': False, 'error': f'SMTP error: {str(e)}'})
    except Exception as e:
            return jsonify({'success': False, 'error': f'Email sending failed: {str(e)}'})
        
    except Exception as e:
        app.logger.error(f"Error in test SMTP: {e}")
        return jsonify({'success': False, 'error': f'Server error: {str(e)}'})

@app.route('/api/refresh-domain-status', methods=['POST'])
@login_required
def api_refresh_domain_status():
    """Refresh domain status by syncing with current Google Workspace users"""
    try:
        # Check if user is authenticated
        if 'current_account_name' not in session:
            return jsonify({'success': False, 'error': 'No account authenticated. Please authenticate first.'})
        
        # Get the current authenticated account
        account_name = session.get('current_account_name')
        
        # First, try to authenticate using saved tokens if service is not available
        if not google_api.service:
            # Check if we have valid tokens for this account
            if google_api.is_token_valid(account_name):
                success = google_api.authenticate_with_tokens(account_name)
                if not success:
                    return jsonify({'success': False, 'error': 'Failed to authenticate with saved tokens. Please re-authenticate.'})
            else:
                return jsonify({'success': False, 'error': 'No valid tokens found. Please re-authenticate.'})
        
        # Get all users from Google Workspace
        users_result = google_api.get_all_users()
        if not users_result['success']:
            return jsonify({'success': False, 'error': f"Failed to get users: {users_result['error']}"})
        
        users = users_result['users']
        
        # Get all domains from database
        domains = UsedDomain.query.all()
        domain_dict = {domain.domain_name: domain for domain in domains}
        
        # Count users per domain
        domain_user_counts = {}
        for user in users:
            email = user.get('primaryEmail', '')
            if '@' in email:
                domain = email.split('@')[1]
                domain_user_counts[domain] = domain_user_counts.get(domain, 0) + 1
            
        # Update domain statuses
        updated_domains = []
        for domain_name, user_count in domain_user_counts.items():
            if domain_name in domain_dict:
                domain = domain_dict[domain_name]
                old_count = domain.user_count
                domain.user_count = user_count
                domain.ever_used = True
                updated_domains.append({
                    'domain': domain_name,
                    'old_count': old_count,
                    'new_count': user_count
                })
            else:
                # Create new domain entry
                new_domain = UsedDomain(
                    domain_name=domain_name,
                    user_count=user_count,
                    ever_used=True
                )
                db.session.add(new_domain)
                updated_domains.append({
                    'domain': domain_name,
                    'old_count': 0,
                    'new_count': user_count
                })
        
        # Mark domains with 0 users as available
        for domain in domains:
            if domain.domain_name not in domain_user_counts:
                if domain.user_count > 0:
                    domain.user_count = 0
                    updated_domains.append({
                        'domain': domain.domain_name,
                        'old_count': domain.user_count,
                        'new_count': 0
                    })
            
            db.session.commit()
            
            return jsonify({
                'success': True,
                'message': f'Domain status refreshed successfully. Updated {len(updated_domains)} domains.',
                'updated_domains': updated_domains,
            'total_users': len(users),
            'total_domains': len(domains)
            })
            
    except Exception as e:
        app.logger.error(f"Error refreshing domain status: {e}")
        return jsonify({'success': False, 'error': f'Server error: {str(e)}'})

@app.route('/api/list-backups', methods=['GET'])
@login_required
def list_backups():
    """List all available backup files"""
    if session.get('role') != 'admin':
        return jsonify({'success': False, 'error': 'Admin privileges required'})
    
    try:
        import os
        import glob
        from datetime import datetime
        
        backup_dir = os.path.join(os.path.dirname(__file__), 'backups')
        
        if not os.path.exists(backup_dir):
            return jsonify({'success': True, 'backups': []})
        
        # Get all backup files
        backup_files = []
        for pattern in ['*.sql', '*.db', '*.json', '*.tar.gz']:
            files = glob.glob(os.path.join(backup_dir, pattern))
            for file_path in files:
                filename = os.path.basename(file_path)
                file_size = os.path.getsize(file_path)
                file_mtime = os.path.getmtime(file_path)
                file_date = datetime.fromtimestamp(file_mtime).strftime('%Y-%m-%d %H:%M:%S')
                
                # Determine backup type
                if filename.endswith('.sql'):
                    backup_type = 'SQL'
                elif filename.endswith('.db'):
                    backup_type = 'SQLite'
                elif filename.endswith('.json'):
                    backup_type = 'JSON'
                elif filename.endswith('.tar.gz'):
                    backup_type = 'Full System'
                else:
                    backup_type = 'Unknown'
                
                backup_files.append({
                    'filename': filename,
                    'filepath': file_path,
                    'size': file_size,
                    'size_mb': round(file_size / (1024 * 1024), 2),
                    'date': file_date,
                    'type': backup_type,
                    'readable': True
                })
        
        # Sort by date (newest first)
        backup_files.sort(key=lambda x: x['date'], reverse=True)
        
        return jsonify({
            'success': True,
            'backups': backup_files,
            'backup_dir': backup_dir
        })
        
    except Exception as e:
        app.logger.error(f"Error listing backups: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/restore-backup', methods=['POST'])
@login_required
def restore_backup():
    """Restore database from existing backup file"""
    if session.get('role') != 'admin':
        return jsonify({'success': False, 'error': 'Admin privileges required'})
    
    try:
        data = request.get_json()
        backup_filename = data.get('filename')
        
        if not backup_filename:
            return jsonify({'success': False, 'error': 'No backup filename provided'})
        
        import os
        import shutil
        import subprocess
        import urllib.parse
        
        backup_dir = os.path.join(os.path.dirname(__file__), 'backups')
        backup_path = os.path.join(backup_dir, backup_filename)
        
        if not os.path.exists(backup_path):
            return jsonify({'success': False, 'error': f'Backup file not found: {backup_filename}'})
        
        # Get current database configuration
        db_url = app.config['SQLALCHEMY_DATABASE_URI']
        
        # Create a backup of current database before restore
        current_backup_name = f"pre_restore_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"
        current_backup_path = os.path.join(backup_dir, current_backup_name)
        
        if db_url.startswith('sqlite'):
            # SQLite restore
            db_path = db_url.replace('sqlite:///', '')
            shutil.copy2(db_path, current_backup_path)
            shutil.copy2(backup_path, db_path)
            
        elif db_url.startswith('postgresql'):
            # PostgreSQL restore
            parsed = urllib.parse.urlparse(db_url)
            
            # Create backup of current database
            pg_dump_cmd = [
                'pg_dump',
                f'--host={parsed.hostname or "localhost"}',
                f'--port={parsed.port or 5432}',
                f'--username={parsed.username or "postgres"}',
                f'--dbname={parsed.path[1:] if parsed.path else "gbot_db"}',
                '--file', current_backup_path
            ]
            
            env = os.environ.copy()
            if parsed.password:
                env['PGPASSWORD'] = parsed.password
            
            # Create current backup
            result = subprocess.run(pg_dump_cmd, env=env, capture_output=True, text=True, timeout=300)
            if result.returncode != 0:
                return jsonify({'success': False, 'error': f'Failed to create current backup: {result.stderr}'})
            
            # Restore from backup
            if backup_filename.endswith('.sql'):
                # SQL file restore
                psql_cmd = [
                    psql_path,
                    f'--host={parsed.hostname or "localhost"}',
                    f'--port={parsed.port or 5432}',
                    f'--username={parsed.username or "postgres"}',
                    f'--dbname={parsed.path[1:] if parsed.path else "gbot_db"}',
                    '--file', backup_path
                ]
                
                result = subprocess.run(psql_cmd, env=env, capture_output=True, text=True, timeout=300)
                if result.returncode != 0:
                    return jsonify({'success': False, 'error': f'Failed to restore database: {result.stderr}'})
            elif backup_filename.endswith('.json'):
                # Convert JSON to SQL first
                sql_backup_path = backup_path.replace('.json', '.sql')
                if convert_json_to_sql(backup_path, sql_backup_path, 'full'):
                    # Use the converted SQL file
                    psql_cmd = [
                        psql_path,
                        f'--host={parsed.hostname or "localhost"}',
                        f'--port={parsed.port or 5432}',
                        f'--username={parsed.username or "postgres"}',
                        f'--dbname={parsed.path[1:] if parsed.path else "gbot_db"}',
                        '--file', sql_backup_path
                ]
                
                result = subprocess.run(psql_cmd, env=env, capture_output=True, text=True, timeout=300)
                if result.returncode != 0:
                    return jsonify({'success': False, 'error': f'Failed to restore database: {result.stderr}'})
                else:
                    return jsonify({'success': False, 'error': 'Failed to convert JSON backup to SQL format'})
            else:
                return jsonify({'success': False, 'error': 'Unsupported backup format for PostgreSQL restore'})
        
        else:
            return jsonify({'success': False, 'error': 'Unsupported database type'})
        
        # Clear SQLAlchemy session to force reload
        db.session.remove()
        
        return jsonify({
            'success': True,
            'message': f'Database restored successfully from {backup_filename}',
            'current_backup': current_backup_name
        })
        
    except Exception as e:
        app.logger.error(f"Error restoring backup: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/upload-restore-backup', methods=['POST'])
@login_required
def upload_restore_backup():
    """Upload and restore a backup file"""
    if session.get('role') != 'admin':
        return jsonify({'success': False, 'error': 'Admin privileges required'})
    
    try:
        app.logger.info(f"Upload restore backup request received from {request.remote_addr}")
        app.logger.info(f"Request files: {list(request.files.keys())}")
        app.logger.info(f"Request content type: {request.content_type}")
        
        if 'backup_file' not in request.files:
            app.logger.error("No backup_file in request.files")
            return jsonify({'success': False, 'error': 'No backup file provided'})
        
        backup_file = request.files['backup_file']
        app.logger.info(f"Backup file received: {backup_file.filename}")
        
        # Debug file size
        backup_file.seek(0, 2)  # Seek to end
        file_size = backup_file.tell()
        backup_file.seek(0)  # Reset to beginning
        app.logger.info(f"File size from request: {file_size} bytes ({file_size / (1024*1024):.2f} MB)")
        
        if backup_file.filename == '':
            app.logger.error("Empty filename")
            return jsonify({'success': False, 'error': 'No file selected'})
        
        # Import required modules first
        import os
        import shutil
        import subprocess
        import urllib.parse
        
        # Validate file extension
        allowed_extensions = {'.sql', '.db', '.json', '.tar.gz'}
        filename = backup_file.filename.lower()
        
        # Check for .tar.gz first (special case)
        if filename.endswith('.tar.gz'):
            file_ext = '.tar.gz'
        else:
            file_ext = os.path.splitext(filename)[1]
        
        app.logger.info(f"Original filename: {backup_file.filename}")
        app.logger.info(f"Detected extension: {file_ext}, Allowed: {allowed_extensions}")
        
        if file_ext not in allowed_extensions:
            app.logger.error(f"Invalid file extension: {file_ext}")
            return jsonify({'success': False, 'error': f'Invalid file type. Allowed: {", ".join(allowed_extensions)}'})
        
        # Save uploaded file
        backup_dir = os.path.join(os.path.dirname(__file__), 'backups')
        os.makedirs(backup_dir, exist_ok=True)
        
        # Generate unique filename
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        uploaded_filename = f"uploaded_backup_{timestamp}{file_ext}"
        uploaded_path = os.path.join(backup_dir, uploaded_filename)
        
        backup_file.save(uploaded_path)
        app.logger.info(f"File saved to: {uploaded_path}")
        
        # Verify file was saved
        if not os.path.exists(uploaded_path):
            app.logger.error(f"File was not saved successfully: {uploaded_path}")
            return jsonify({'success': False, 'error': 'Failed to save uploaded file'})
        
        file_size = os.path.getsize(uploaded_path)
        app.logger.info(f"Uploaded file size: {file_size} bytes")
        
        # Get current database configuration
        db_url = app.config['SQLALCHEMY_DATABASE_URI']
        
        # Create a backup of current database before restore
        current_backup_name = f"pre_restore_backup_{timestamp}.db"
        current_backup_path = os.path.join(backup_dir, current_backup_name)
        
        if db_url.startswith('sqlite'):
            # SQLite restore
            db_path = db_url.replace('sqlite:///', '')
            shutil.copy2(db_path, current_backup_path)
            shutil.copy2(uploaded_path, db_path)
            
        elif db_url.startswith('postgresql'):
            # PostgreSQL restore
            parsed = urllib.parse.urlparse(db_url)
            
            # Create backup of current database
            pg_dump_cmd = [
                'pg_dump',
                f'--host={parsed.hostname or "localhost"}',
                f'--port={parsed.port or 5432}',
                f'--username={parsed.username or "postgres"}',
                f'--dbname={parsed.path[1:] if parsed.path else "gbot_db"}',
                '--file', current_backup_path
            ]
            
            env = os.environ.copy()
            if parsed.password:
                env['PGPASSWORD'] = parsed.password
            
            # Create current backup
            result = subprocess.run(pg_dump_cmd, env=env, capture_output=True, text=True, timeout=300)
            if result.returncode != 0:
                return jsonify({'success': False, 'error': f'Failed to create current backup: {result.stderr}'})
            
            # Restore from uploaded file
            if file_ext == '.sql':
                # SQL file restore
                psql_cmd = [
                    psql_path,
                    f'--host={parsed.hostname or "localhost"}',
                    f'--port={parsed.port or 5432}',
                    f'--username={parsed.username or "postgres"}',
                    f'--dbname={parsed.path[1:] if parsed.path else "gbot_db"}',
                    '--file', uploaded_path
                ]
                
                result = subprocess.run(psql_cmd, env=env, capture_output=True, text=True, timeout=300)
                if result.returncode != 0:
                    return jsonify({'success': False, 'error': f'Failed to restore database: {result.stderr}'})
            elif file_ext == '.json':
                # Convert JSON to SQL first
                sql_uploaded_path = uploaded_path.replace('.json', '.sql')
                if convert_json_to_sql(uploaded_path, sql_uploaded_path, 'full'):
                    # Use the converted SQL file
                    psql_cmd = [
                        psql_path,
                        f'--host={parsed.hostname or "localhost"}',
                        f'--port={parsed.port or 5432}',
                        f'--username={parsed.username or "postgres"}',
                        f'--dbname={parsed.path[1:] if parsed.path else "gbot_db"}',
                        '--file', sql_uploaded_path
                ]
                
                result = subprocess.run(psql_cmd, env=env, capture_output=True, text=True, timeout=300)
                if result.returncode != 0:
                    return jsonify({'success': False, 'error': f'Failed to restore database: {result.stderr}'})
                else:
                    return jsonify({'success': False, 'error': 'Failed to convert JSON backup to SQL format'})
            else:
                return jsonify({'success': False, 'error': 'Unsupported backup format for PostgreSQL restore'})
        
        else:
            return jsonify({'success': False, 'error': 'Unsupported database type'})
        
        # Clear SQLAlchemy session to force reload
        db.session.remove()
        
        return jsonify({
            'success': True,
            'message': f'Database restored successfully from uploaded file: {backup_file.filename}',
            'uploaded_file': uploaded_filename,
            'current_backup': current_backup_name
        })
        
    except Exception as e:
        app.logger.error(f"Error uploading and restoring backup: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/test-chunked-upload', methods=['POST'])
@login_required
def test_chunked_upload():
    """Test endpoint to verify chunked upload system is working"""
    try:
        app.logger.info("Test chunked upload endpoint called")
        return jsonify({
            'success': True,
            'message': 'Chunked upload system is working',
            'timestamp': datetime.now().isoformat()
        })
    except Exception as e:
        app.logger.error(f"Test chunked upload error: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/upload-chunk', methods=['POST'])
@login_required
def upload_chunk():
    """Upload a file chunk for chunked upload"""
    if session.get('role') != 'admin':
        return jsonify({'success': False, 'error': 'Admin privileges required'})
    
    try:
        upload_id = request.form.get('upload_id')
        chunk_index = int(request.form.get('chunk_index'))
        total_chunks = int(request.form.get('total_chunks'))
        filename = request.form.get('filename')
        
        if 'chunk' not in request.files:
            return jsonify({'success': False, 'error': 'No chunk provided'})
        
        chunk = request.files['chunk']
        
        # Import required modules
        import os
        
        # Create chunks directory
        chunks_dir = os.path.join(os.path.dirname(__file__), 'chunks', upload_id)
        os.makedirs(chunks_dir, exist_ok=True)
        
        # Save chunk
        chunk_path = os.path.join(chunks_dir, f'chunk_{chunk_index}')
        chunk.save(chunk_path)
        
        app.logger.info(f"Chunk {chunk_index + 1}/{total_chunks} uploaded for {filename}")
        
        return jsonify({
            'success': True,
            'message': f'Chunk {chunk_index + 1}/{total_chunks} uploaded',
            'chunk_index': chunk_index,
            'total_chunks': total_chunks
        })
        
    except Exception as e:
        app.logger.error(f"Error uploading chunk: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/restore-from-chunks', methods=['POST'])
@login_required
def restore_from_chunks():
    """Restore database from uploaded chunks"""
    if session.get('role') != 'admin':
        return jsonify({'success': False, 'error': 'Admin privileges required'})
    
    try:
        data = request.get_json()
        upload_id = data.get('upload_id')
        filename = data.get('filename')
        total_chunks = data.get('total_chunks')
        
        # Import required modules
        import os
        import shutil
        import subprocess
        import urllib.parse
        
        # Create chunks directory path
        chunks_dir = os.path.join(os.path.dirname(__file__), 'chunks', upload_id)
        
        # Verify all chunks exist
        for i in range(total_chunks):
            chunk_path = os.path.join(chunks_dir, f'chunk_{i}')
            if not os.path.exists(chunk_path):
                return jsonify({'success': False, 'error': f'Chunk {i} not found'})
        
        # Reassemble file
        backup_dir = os.path.join(os.path.dirname(__file__), 'backups')
        os.makedirs(backup_dir, exist_ok=True)
        
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        reassembled_filename = f"chunked_backup_{timestamp}_{filename}"
        reassembled_path = os.path.join(backup_dir, reassembled_filename)
        
        with open(reassembled_path, 'wb') as output_file:
            for i in range(total_chunks):
                chunk_path = os.path.join(chunks_dir, f'chunk_{i}')
                with open(chunk_path, 'rb') as chunk_file:
                    output_file.write(chunk_file.read())
        
        app.logger.info(f"File reassembled: {reassembled_filename}")
        
        # Get file extension
        file_ext = os.path.splitext(filename)[1].lower()
        
        # Get current database configuration
        db_url = app.config['SQLALCHEMY_DATABASE_URI']
        
        # Create a backup of current database before restore
        current_backup_name = f"pre_restore_backup_{timestamp}.db"
        current_backup_path = os.path.join(backup_dir, current_backup_name)
        
        if db_url.startswith('sqlite'):
            # SQLite restore
            db_path = db_url.replace('sqlite:///', '')
            shutil.copy2(db_path, current_backup_path)
            shutil.copy2(reassembled_path, db_path)
            
        elif db_url.startswith('postgresql'):
            # PostgreSQL restore
            parsed = urllib.parse.urlparse(db_url)
            
            # Create backup of current database
            pg_dump_cmd = [
                'pg_dump',
                f'--host={parsed.hostname or "localhost"}',
                f'--port={parsed.port or 5432}',
                f'--username={parsed.username or "postgres"}',
                f'--dbname={parsed.path[1:] if parsed.path else "gbot_db"}',
                '--file', current_backup_path
            ]
            
            env = os.environ.copy()
            if parsed.password:
                env['PGPASSWORD'] = parsed.password
            
            # Create current backup
            result = subprocess.run(pg_dump_cmd, env=env, capture_output=True, text=True, timeout=300)
            if result.returncode != 0:
                return jsonify({'success': False, 'error': f'Failed to create current backup: {result.stderr}'})
            
            # Restore from reassembled file
            if file_ext == '.sql':
                psql_cmd = [
                    psql_path,
                    f'--host={parsed.hostname or "localhost"}',
                    f'--port={parsed.port or 5432}',
                    f'--username={parsed.username or "postgres"}',
                    f'--dbname={parsed.path[1:] if parsed.path else "gbot_db"}',
                    '--file', reassembled_path
                ]
                
                result = subprocess.run(psql_cmd, env=env, capture_output=True, text=True, timeout=300)
                if result.returncode != 0:
                    return jsonify({'success': False, 'error': f'Failed to restore database: {result.stderr}'})
            else:
                return jsonify({'success': False, 'error': 'Unsupported backup format for PostgreSQL restore'})
        
        else:
            return jsonify({'success': False, 'error': 'Unsupported database type'})
        
        # Clear SQLAlchemy session to force reload
        db.session.remove()
        
        # Clean up chunks
        shutil.rmtree(chunks_dir, ignore_errors=True)
        
        return jsonify({
            'success': True,
            'message': f'Database restored successfully from chunked upload: {filename}',
            'reassembled_file': reassembled_filename,
            'current_backup': current_backup_name
        })
        
    except Exception as e:
        app.logger.error(f"Error restoring from chunks: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/restore-from-base64', methods=['POST'])
@login_required
def restore_from_base64():
    """Restore database from base64 encoded file content"""
    if session.get('role') != 'admin':
        return jsonify({'success': False, 'error': 'Admin privileges required'})
    
    try:
        data = request.get_json()
        filename = data.get('filename')
        base64_content = data.get('content')
        file_size = data.get('size')
        
        if not filename or not base64_content:
            return jsonify({'success': False, 'error': 'Missing filename or content'})
        
        # Import required modules
        import os
        import shutil
        import subprocess
        import urllib.parse
        import base64
        
        # Decode base64 content
        try:
            file_content = base64.b64decode(base64_content)
        except Exception as e:
            return jsonify({'success': False, 'error': f'Failed to decode base64 content: {e}'})
        
        # Save decoded file
        backup_dir = os.path.join(os.path.dirname(__file__), 'backups')
        os.makedirs(backup_dir, exist_ok=True)
        
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        decoded_filename = f"base64_backup_{timestamp}_{filename}"
        decoded_path = os.path.join(backup_dir, decoded_filename)
        
        with open(decoded_path, 'wb') as f:
            f.write(file_content)
        
        app.logger.info(f"Base64 file decoded and saved: {decoded_filename}")
        
        # Get file extension
        file_ext = os.path.splitext(filename)[1].lower()
        
        # Get current database configuration
        db_url = app.config['SQLALCHEMY_DATABASE_URI']
        
        # Create a backup of current database before restore
        current_backup_name = f"pre_restore_backup_{timestamp}.db"
        current_backup_path = os.path.join(backup_dir, current_backup_name)
        
        if db_url.startswith('sqlite'):
            # SQLite restore
            db_path = db_url.replace('sqlite:///', '')
            
            # Handle Windows paths
            if db_path.startswith('/'):
                # Convert Unix-style path to Windows path
                db_path = db_path[1:]  # Remove leading slash
                db_path = db_path.replace('/', '\\')  # Convert to Windows separators
            
            app.logger.info(f"SQLite database path: {db_path}")
            app.logger.info(f"Decoded file path: {decoded_path}")
            
            # Check if database file exists
            if not os.path.exists(db_path):
                return jsonify({'success': False, 'error': f'Database file not found: {db_path}'})
            
            # Create backup of current database
            shutil.copy2(db_path, current_backup_path)
            app.logger.info(f"Current database backed up to: {current_backup_name}")
            
            # Restore from decoded file
            if file_ext == '.db':
                # Direct SQLite database file
                shutil.copy2(decoded_path, db_path)
                app.logger.info(f"SQLite database restored from: {decoded_filename}")
            elif file_ext == '.sql':
                # SQL dump file - need to recreate database
                # First, remove the current database
                os.remove(db_path)
                
                # Create new database and import SQL
                import sqlite3
                conn = sqlite3.connect(db_path)
                with open(decoded_path, 'r', encoding='utf-8') as f:
                    sql_content = f.read()
                    conn.executescript(sql_content)
                conn.close()
                app.logger.info(f"SQLite database recreated from SQL dump: {decoded_filename}")
            else:
                return jsonify({'success': False, 'error': f'Unsupported backup format for SQLite: {file_ext}'})
            
        elif db_url.startswith('postgresql'):
            # PostgreSQL restore
            parsed = urllib.parse.urlparse(db_url)
            
            # Check if PostgreSQL tools are available
            pg_tools_available = False
            pg_dump_path = None
            psql_path = None
            
            try:
                # Enhanced detection - check multiple common paths
                common_paths = [
                    '/usr/bin/pg_dump',
                    '/usr/local/bin/pg_dump',
                    '/opt/postgresql/bin/pg_dump',
                    '/usr/lib/postgresql/*/bin/pg_dump',  # Ubuntu/Debian standard
                    '/usr/pgsql-*/bin/pg_dump',  # RedHat/CentOS
                    '/opt/local/bin/pg_dump',  # MacPorts
                    '/usr/local/pgsql/bin/pg_dump',  # Source install
                    'pg_dump'  # Try PATH
                ]
                
                # Also check for versioned paths
                import glob
                versioned_paths = glob.glob('/usr/lib/postgresql/*/bin/pg_dump')
                common_paths.extend(versioned_paths)
                
                app.logger.info(f"Checking PostgreSQL tools in {len(common_paths)} locations...")
                
                for path in common_paths:
                    try:
                        result = subprocess.run([path, '--version'], capture_output=True, text=True, timeout=5)
                        if result.returncode == 0:
                            pg_dump_path = path
                            app.logger.info(f"✅ pg_dump found at {path}: {result.stdout.strip()}")
                            break
                        else:
                            app.logger.debug(f"pg_dump at {path} returned code {result.returncode}")
                    except Exception as e:
                        app.logger.debug(f"pg_dump not found at {path}: {e}")
                
                if not pg_dump_path:
                    app.logger.warning("❌ pg_dump not found in any common location")
                    # Try to find it using which command
                    try:
                        which_result = subprocess.run(['which', 'pg_dump'], capture_output=True, text=True, timeout=5)
                        if which_result.returncode == 0:
                            pg_dump_path = which_result.stdout.strip()
                            app.logger.info(f"✅ pg_dump found via which: {pg_dump_path}")
                    except Exception as e:
                        app.logger.debug(f"which pg_dump failed: {e}")
                
                # Check for psql
                common_paths = [
                    '/usr/bin/psql',
                    '/usr/local/bin/psql',
                    '/opt/postgresql/bin/psql',
                    '/usr/lib/postgresql/*/bin/psql',  # Ubuntu/Debian standard
                    '/usr/pgsql-*/bin/psql',  # RedHat/CentOS
                    '/opt/local/bin/psql',  # MacPorts
                    '/usr/local/pgsql/bin/psql',  # Source install
                    'psql'  # Try PATH
                ]
                
                # Also check for versioned paths
                versioned_paths = glob.glob('/usr/lib/postgresql/*/bin/psql')
                common_paths.extend(versioned_paths)
                
                for path in common_paths:
                    try:
                        result = subprocess.run([path, '--version'], capture_output=True, text=True, timeout=5)
                        if result.returncode == 0:
                            psql_path = path
                            app.logger.info(f"✅ psql found at {path}: {result.stdout.strip()}")
                            break
                        else:
                            app.logger.debug(f"psql at {path} returned code {result.returncode}")
                    except Exception as e:
                        app.logger.debug(f"psql not found at {path}: {e}")
                
                if not psql_path:
                    app.logger.warning("❌ psql not found in any common location")
                    # Try to find it using which command
                    try:
                        which_result = subprocess.run(['which', 'psql'], capture_output=True, text=True, timeout=5)
                        if which_result.returncode == 0:
                            psql_path = which_result.stdout.strip()
                            app.logger.info(f"✅ psql found via which: {psql_path}")
                    except Exception as e:
                        app.logger.debug(f"which psql failed: {e}")
                
                if pg_dump_path and psql_path:
                    pg_tools_available = True
                    app.logger.info("PostgreSQL tools are available")
                else:
                    app.logger.warning(f"PostgreSQL tools not fully available. pg_dump: {pg_dump_path}, psql: {psql_path}")
                    
            except Exception as e:
                app.logger.warning(f"Error checking PostgreSQL tools: {e}")
                pg_tools_available = False
            
            if pg_tools_available:
                # Use PostgreSQL command-line tools
                # Create backup of current database
                pg_dump_cmd = [
                    pg_dump_path,
                    f'--host={parsed.hostname or "localhost"}',
                    f'--port={parsed.port or 5432}',
                    f'--username={parsed.username or "postgres"}',
                    f'--dbname={parsed.path[1:] if parsed.path else "gbot_db"}',
                    '--file', current_backup_path
                ]
                
                env = os.environ.copy()
                if parsed.password:
                    env['PGPASSWORD'] = parsed.password
                
                # Create current backup
                result = subprocess.run(pg_dump_cmd, env=env, capture_output=True, text=True, timeout=300)
                if result.returncode != 0:
                    return jsonify({'success': False, 'error': f'Failed to create current backup: {result.stderr}'})
                
                # Restore from decoded file
                if file_ext == '.sql':
                    psql_cmd = [
                        psql_path,
                        f'--host={parsed.hostname or "localhost"}',
                        f'--port={parsed.port or 5432}',
                        f'--username={parsed.username or "postgres"}',
                        f'--dbname={parsed.path[1:] if parsed.path else "gbot_db"}',
                        '--file', decoded_path
                    ]
                    
                    result = subprocess.run(psql_cmd, env=env, capture_output=True, text=True, timeout=300)
                    if result.returncode != 0:
                        return jsonify({'success': False, 'error': f'Failed to restore database: {result.stderr}'})
                else:
                    return jsonify({'success': False, 'error': 'Unsupported backup format for PostgreSQL restore'})
            else:
                # PostgreSQL tools not available - try to install them
                app.logger.info("PostgreSQL client tools not found, attempting to install...")
                try:
                    # Check if we're running as root (no sudo needed)
                    import getpass
                    is_root = getpass.getuser() == 'root'
                    
                    if is_root:
                        # Running as root, no sudo needed
                        install_cmd = 'apt-get update && apt-get install -y postgresql-client'
                        app.logger.info("Running as root, installing PostgreSQL client tools without sudo")
                    else:
                        # Not root, use sudo
                        install_cmd = 'sudo apt-get update && sudo apt-get install -y postgresql-client'
                        app.logger.info("Not running as root, installing PostgreSQL client tools with sudo")
                    
                    result = subprocess.run(install_cmd, shell=True, capture_output=True, text=True, timeout=300)
                    
                    if result.returncode == 0:
                        app.logger.info("PostgreSQL client tools installed successfully")
                        # Retry with command-line tools
                        pg_dump_cmd = [
                            'pg_dump',
                            f'--host={parsed.hostname or "localhost"}',
                            f'--port={parsed.port or 5432}',
                            f'--username={parsed.username or "postgres"}',
                            f'--dbname={parsed.path[1:] if parsed.path else "gbot_db"}',
                            '--file', current_backup_path
                        ]
                        
                        env = os.environ.copy()
                        if parsed.password:
                            env['PGPASSWORD'] = parsed.password
                        
                        # Create current backup
                        result = subprocess.run(pg_dump_cmd, env=env, capture_output=True, text=True, timeout=300)
                        if result.returncode != 0:
                            return jsonify({'success': False, 'error': f'Failed to create current backup: {result.stderr}'})
                        
                        # Restore from decoded file
                        if file_ext == '.sql':
                            psql_cmd = [
                                'psql',
                                f'--host={parsed.hostname or "localhost"}',
                                f'--port={parsed.port or 5432}',
                                f'--username={parsed.username or "postgres"}',
                                f'--dbname={parsed.path[1:] if parsed.path else "gbot_db"}',
                                '--file', decoded_path
                            ]
                            
                            result = subprocess.run(psql_cmd, env=env, capture_output=True, text=True, timeout=300)
                            if result.returncode != 0:
                                return jsonify({'success': False, 'error': f'Failed to restore database: {result.stderr}'})
                        else:
                            return jsonify({'success': False, 'error': 'Unsupported backup format for PostgreSQL restore'})
                    else:
                        sudo_cmd = 'sudo apt-get install postgresql-client' if not is_root else 'apt-get install postgresql-client'
                        return jsonify({'success': False, 'error': f'Failed to install PostgreSQL client tools: {result.stderr}. PostgreSQL client tools are installed but not detected. Please check PATH or install manually: sudo apt-get install postgresql-client'})
                        
                except Exception as e:
                    sudo_cmd = 'sudo apt-get install postgresql-client' if not is_root else 'apt-get install postgresql-client'
                    return jsonify({'success': False, 'error': f'Failed to install PostgreSQL client tools: {str(e)}. PostgreSQL client tools are installed but not detected. Please check PATH or install manually: sudo apt-get install postgresql-client'})
        
        else:
            return jsonify({'success': False, 'error': 'Unsupported database type'})
        
        # Clear SQLAlchemy session to force reload
        db.session.remove()
        
        return jsonify({
            'success': True,
            'message': f'Database restored successfully from base64 upload: {filename}',
            'decoded_file': decoded_filename,
            'current_backup': current_backup_name
        })
        
    except Exception as e:
        app.logger.error(f"Error restoring from base64: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/restore-from-base64-chunks', methods=['POST'])
@login_required
def restore_from_base64_chunks():
    """Restore database from uploaded base64 chunks"""
    if session.get('role') != 'admin':
        return jsonify({'success': False, 'error': 'Admin privileges required'})
    
    try:
        data = request.get_json()
        upload_id = data.get('upload_id')
        filename = data.get('filename')
        total_chunks = data.get('total_chunks')
        file_size = data.get('file_size')
        
        # Import required modules
        import os
        import shutil
        import subprocess
        import urllib.parse
        import base64
        
        # Create chunks directory path
        chunks_dir = os.path.join(os.path.dirname(__file__), 'chunks', upload_id)
        
        # Verify all chunks exist
        for i in range(total_chunks):
            chunk_path = os.path.join(chunks_dir, f'base64_chunk_{i}')
            if not os.path.exists(chunk_path):
                return jsonify({'success': False, 'error': f'Base64 chunk {i} not found'})
        
        # Reassemble base64 content
        base64_content = ''
        for i in range(total_chunks):
            chunk_path = os.path.join(chunks_dir, f'base64_chunk_{i}')
            with open(chunk_path, 'r') as f:
                base64_content += f.read()
        
        app.logger.info(f"Base64 content reassembled, length: {len(base64_content)}")
        
        # Decode base64 content
        try:
            file_content = base64.b64decode(base64_content)
        except Exception as e:
            return jsonify({'success': False, 'error': f'Failed to decode base64 content: {e}'})
        
        # Save decoded file
        backup_dir = os.path.join(os.path.dirname(__file__), 'backups')
        os.makedirs(backup_dir, exist_ok=True)
        
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        decoded_filename = f"chunked_base64_backup_{timestamp}_{filename}"
        decoded_path = os.path.join(backup_dir, decoded_filename)
        
        with open(decoded_path, 'wb') as f:
            f.write(file_content)
        
        app.logger.info(f"Chunked base64 file decoded and saved: {decoded_filename}")
        
        # Get file extension
        file_ext = os.path.splitext(filename)[1].lower()
        
        # Get current database configuration
        db_url = app.config['SQLALCHEMY_DATABASE_URI']
        
        # Create a backup of current database before restore
        current_backup_name = f"pre_restore_backup_{timestamp}.db"
        current_backup_path = os.path.join(backup_dir, current_backup_name)
        
        if db_url.startswith('sqlite'):
            # SQLite restore
            db_path = db_url.replace('sqlite:///', '')
            
            # Handle Windows paths
            if db_path.startswith('/'):
                # Convert Unix-style path to Windows path
                db_path = db_path[1:]  # Remove leading slash
                db_path = db_path.replace('/', '\\')  # Convert to Windows separators
            
            app.logger.info(f"SQLite database path: {db_path}")
            app.logger.info(f"Decoded file path: {decoded_path}")
            
            # Check if database file exists
            if not os.path.exists(db_path):
                return jsonify({'success': False, 'error': f'Database file not found: {db_path}'})
            
            # Create backup of current database
            shutil.copy2(db_path, current_backup_path)
            app.logger.info(f"Current database backed up to: {current_backup_name}")
            
            # Restore from decoded file
            if file_ext == '.db':
                # Direct SQLite database file
                shutil.copy2(decoded_path, db_path)
                app.logger.info(f"SQLite database restored from: {decoded_filename}")
            elif file_ext == '.sql':
                # SQL dump file - need to recreate database
                # First, remove the current database
                os.remove(db_path)
                
                # Create new database and import SQL
                import sqlite3
                conn = sqlite3.connect(db_path)
                with open(decoded_path, 'r', encoding='utf-8') as f:
                    sql_content = f.read()
                    conn.executescript(sql_content)
                conn.close()
                app.logger.info(f"SQLite database recreated from SQL dump: {decoded_filename}")
            else:
                return jsonify({'success': False, 'error': f'Unsupported backup format for SQLite: {file_ext}'})
            
        elif db_url.startswith('postgresql'):
            # PostgreSQL restore
            parsed = urllib.parse.urlparse(db_url)
            
            # Check if PostgreSQL tools are available
            pg_tools_available = False
            pg_dump_path = None
            psql_path = None
            
            try:
                # Enhanced detection - check multiple common paths
                common_paths = [
                    '/usr/bin/pg_dump',
                    '/usr/local/bin/pg_dump',
                    '/opt/postgresql/bin/pg_dump',
                    '/usr/lib/postgresql/*/bin/pg_dump',  # Ubuntu/Debian standard
                    '/usr/pgsql-*/bin/pg_dump',  # RedHat/CentOS
                    '/opt/local/bin/pg_dump',  # MacPorts
                    '/usr/local/pgsql/bin/pg_dump',  # Source install
                    'pg_dump'  # Try PATH
                ]
                
                # Also check for versioned paths
                import glob
                versioned_paths = glob.glob('/usr/lib/postgresql/*/bin/pg_dump')
                common_paths.extend(versioned_paths)
                
                app.logger.info(f"Checking PostgreSQL tools in {len(common_paths)} locations...")
                
                for path in common_paths:
                    try:
                        result = subprocess.run([path, '--version'], capture_output=True, text=True, timeout=5)
                        if result.returncode == 0:
                            pg_dump_path = path
                            app.logger.info(f"✅ pg_dump found at {path}: {result.stdout.strip()}")
                            break
                        else:
                            app.logger.debug(f"pg_dump at {path} returned code {result.returncode}")
                    except Exception as e:
                        app.logger.debug(f"pg_dump not found at {path}: {e}")
                
                if not pg_dump_path:
                    app.logger.warning("❌ pg_dump not found in any common location")
                    # Try to find it using which command
                    try:
                        which_result = subprocess.run(['which', 'pg_dump'], capture_output=True, text=True, timeout=5)
                        if which_result.returncode == 0:
                            pg_dump_path = which_result.stdout.strip()
                            app.logger.info(f"✅ pg_dump found via which: {pg_dump_path}")
                    except Exception as e:
                        app.logger.debug(f"which pg_dump failed: {e}")
                
                # Check for psql
                common_paths = [
                    '/usr/bin/psql',
                    '/usr/local/bin/psql',
                    '/opt/postgresql/bin/psql',
                    '/usr/lib/postgresql/*/bin/psql',  # Ubuntu/Debian standard
                    '/usr/pgsql-*/bin/psql',  # RedHat/CentOS
                    '/opt/local/bin/psql',  # MacPorts
                    '/usr/local/pgsql/bin/psql',  # Source install
                    'psql'  # Try PATH
                ]
                
                # Also check for versioned paths
                versioned_paths = glob.glob('/usr/lib/postgresql/*/bin/psql')
                common_paths.extend(versioned_paths)
                
                for path in common_paths:
                    try:
                        result = subprocess.run([path, '--version'], capture_output=True, text=True, timeout=5)
                        if result.returncode == 0:
                            psql_path = path
                            app.logger.info(f"✅ psql found at {path}: {result.stdout.strip()}")
                            break
                        else:
                            app.logger.debug(f"psql at {path} returned code {result.returncode}")
                    except Exception as e:
                        app.logger.debug(f"psql not found at {path}: {e}")
                
                if not psql_path:
                    app.logger.warning("❌ psql not found in any common location")
                    # Try to find it using which command
                    try:
                        which_result = subprocess.run(['which', 'psql'], capture_output=True, text=True, timeout=5)
                        if which_result.returncode == 0:
                            psql_path = which_result.stdout.strip()
                            app.logger.info(f"✅ psql found via which: {psql_path}")
                    except Exception as e:
                        app.logger.debug(f"which psql failed: {e}")
                
                if pg_dump_path and psql_path:
                    pg_tools_available = True
                    app.logger.info("PostgreSQL tools are available")
                else:
                    app.logger.warning(f"PostgreSQL tools not fully available. pg_dump: {pg_dump_path}, psql: {psql_path}")
                    
            except Exception as e:
                app.logger.warning(f"Error checking PostgreSQL tools: {e}")
                pg_tools_available = False
            
            if pg_tools_available:
                # Use PostgreSQL command-line tools
                # Create backup of current database
                pg_dump_cmd = [
                    pg_dump_path,
                    f'--host={parsed.hostname or "localhost"}',
                    f'--port={parsed.port or 5432}',
                    f'--username={parsed.username or "postgres"}',
                    f'--dbname={parsed.path[1:] if parsed.path else "gbot_db"}',
                    '--file', current_backup_path
                ]
                
                env = os.environ.copy()
                if parsed.password:
                    env['PGPASSWORD'] = parsed.password
                
                # Create current backup
                result = subprocess.run(pg_dump_cmd, env=env, capture_output=True, text=True, timeout=300)
                if result.returncode != 0:
                    return jsonify({'success': False, 'error': f'Failed to create current backup: {result.stderr}'})
                
                # Restore from decoded file
                if file_ext == '.sql':
                    psql_cmd = [
                        psql_path,
                        f'--host={parsed.hostname or "localhost"}',
                        f'--port={parsed.port or 5432}',
                        f'--username={parsed.username or "postgres"}',
                        f'--dbname={parsed.path[1:] if parsed.path else "gbot_db"}',
                        '--file', decoded_path
                    ]
                    
                    result = subprocess.run(psql_cmd, env=env, capture_output=True, text=True, timeout=300)
                    if result.returncode != 0:
                        return jsonify({'success': False, 'error': f'Failed to restore database: {result.stderr}'})
                else:
                    return jsonify({'success': False, 'error': 'Unsupported backup format for PostgreSQL restore'})
            else:
                # PostgreSQL tools not available - try to install them
                app.logger.info("PostgreSQL client tools not found, attempting to install...")
                try:
                    # Check if we're running as root (no sudo needed)
                    import getpass
                    is_root = getpass.getuser() == 'root'
                    
                    if is_root:
                        # Running as root, no sudo needed
                        install_cmd = 'apt-get update && apt-get install -y postgresql-client'
                        app.logger.info("Running as root, installing PostgreSQL client tools without sudo")
                    else:
                        # Not root, use sudo
                        install_cmd = 'sudo apt-get update && sudo apt-get install -y postgresql-client'
                        app.logger.info("Not running as root, installing PostgreSQL client tools with sudo")
                    
                    result = subprocess.run(install_cmd, shell=True, capture_output=True, text=True, timeout=300)
                    
                    if result.returncode == 0:
                        app.logger.info("PostgreSQL client tools installed successfully")
                        # Retry with command-line tools
                        pg_dump_cmd = [
                            'pg_dump',
                            f'--host={parsed.hostname or "localhost"}',
                            f'--port={parsed.port or 5432}',
                            f'--username={parsed.username or "postgres"}',
                            f'--dbname={parsed.path[1:] if parsed.path else "gbot_db"}',
                            '--file', current_backup_path
                        ]
                        
                        env = os.environ.copy()
                        if parsed.password:
                            env['PGPASSWORD'] = parsed.password
                        
                        # Create current backup
                        result = subprocess.run(pg_dump_cmd, env=env, capture_output=True, text=True, timeout=300)
                        if result.returncode != 0:
                            return jsonify({'success': False, 'error': f'Failed to create current backup: {result.stderr}'})
                        
                        # Restore from decoded file
                        if file_ext == '.sql':
                            psql_cmd = [
                                'psql',
                                f'--host={parsed.hostname or "localhost"}',
                                f'--port={parsed.port or 5432}',
                                f'--username={parsed.username or "postgres"}',
                                f'--dbname={parsed.path[1:] if parsed.path else "gbot_db"}',
                                '--file', decoded_path
                            ]
                            
                            result = subprocess.run(psql_cmd, env=env, capture_output=True, text=True, timeout=300)
                            if result.returncode != 0:
                                return jsonify({'success': False, 'error': f'Failed to restore database: {result.stderr}'})
                        else:
                            return jsonify({'success': False, 'error': 'Unsupported backup format for PostgreSQL restore'})
                    else:
                        sudo_cmd = 'sudo apt-get install postgresql-client' if not is_root else 'apt-get install postgresql-client'
                        return jsonify({'success': False, 'error': f'Failed to install PostgreSQL client tools: {result.stderr}. PostgreSQL client tools are installed but not detected. Please check PATH or install manually: sudo apt-get install postgresql-client'})
                        
                except Exception as e:
                    sudo_cmd = 'sudo apt-get install postgresql-client' if not is_root else 'apt-get install postgresql-client'
                    return jsonify({'success': False, 'error': f'Failed to install PostgreSQL client tools: {str(e)}. PostgreSQL client tools are installed but not detected. Please check PATH or install manually: sudo apt-get install postgresql-client'})
        
        else:
            return jsonify({'success': False, 'error': 'Unsupported database type'})
        
        # Clear SQLAlchemy session to force reload
        db.session.remove()
        
        # Clean up chunks
        shutil.rmtree(chunks_dir, ignore_errors=True)
        
        return jsonify({
            'success': True,
            'message': f'Database restored successfully from chunked base64 upload: {filename}',
            'decoded_file': decoded_filename,
            'current_backup': current_backup_name
        })
        
    except Exception as e:
        app.logger.error(f"Error restoring from base64 chunks: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/upload-base64-chunk', methods=['POST'])
@login_required
def upload_base64_chunk():
    """Upload a base64 chunk for chunked base64 upload"""
    if session.get('role') != 'admin':
        return jsonify({'success': False, 'error': 'Admin privileges required'})
    
    try:
        data = request.get_json()
        upload_id = data.get('upload_id')
        chunk_index = data.get('chunk_index')
        total_chunks = data.get('total_chunks')
        filename = data.get('filename')
        chunk_content = data.get('chunk_content')
        
        if not all([upload_id, chunk_index is not None, total_chunks, filename, chunk_content]):
            return jsonify({'success': False, 'error': 'Missing required fields'})
        
        # Import required modules
        import os
        
        # Create chunks directory
        chunks_dir = os.path.join(os.path.dirname(__file__), 'chunks', upload_id)
        os.makedirs(chunks_dir, exist_ok=True)
        
        # Save chunk
        chunk_path = os.path.join(chunks_dir, f'base64_chunk_{chunk_index}')
        with open(chunk_path, 'w') as f:
            f.write(chunk_content)
        
        app.logger.info(f"Base64 chunk {chunk_index + 1}/{total_chunks} uploaded for {filename}")
        
        return jsonify({
            'success': True,
            'message': f'Base64 chunk {chunk_index + 1}/{total_chunks} uploaded',
            'chunk_index': chunk_index,
            'total_chunks': total_chunks
        })
        
    except Exception as e:
        app.logger.error(f"Error uploading base64 chunk: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/suspend-user', methods=['POST'])
@login_required
def api_suspend_user():
    """Suspend a user account"""
    try:
        if 'current_account_name' not in session:
            return jsonify({'success': False, 'error': 'No account authenticated. Please authenticate first.'})
        
        account_name = session.get('current_account_name')
        
        # Check if we have valid tokens for this account
        if not google_api.service:
            if google_api.is_token_valid(account_name):
                success = google_api.authenticate_with_tokens(account_name)
                if not success:
                    return jsonify({'success': False, 'error': 'Failed to authenticate with saved tokens. Please re-authenticate.'})
            else:
                return jsonify({'success': False, 'error': 'No valid tokens found. Please re-authenticate.'})
        
        req = request.get_json(silent=True) or {}
        email = req.get('email', '').strip()
        
        if not email:
            return jsonify({'success': False, 'error': 'Email address is required'})
        
        if '@' not in email:
            return jsonify({'success': False, 'error': 'Please provide a valid email address'})
        
        logging.info(f"Suspending user: {email}")
        
        # Suspend the user
        result = google_api.suspend_user(email)
        
        if result['success']:
            return jsonify({
                'success': True,
                'message': result['message'],
                'email': result['email']
            })
        else:
            return jsonify({
                'success': False,
                'error': result.get('error', 'Unknown error'),
                'email': result.get('email', email)
            })
            
    except Exception as e:
        logging.error(f"Suspend user error: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/unsuspend-user', methods=['POST'])
@login_required
def api_unsuspend_user():
    """Unsuspend a user account"""
    try:
        if 'current_account_name' not in session:
            return jsonify({'success': False, 'error': 'No account authenticated. Please authenticate first.'})
        
        account_name = session.get('current_account_name')
        
        # Check if we have valid tokens for this account
        if not google_api.service:
            if google_api.is_token_valid(account_name):
                success = google_api.authenticate_with_tokens(account_name)
                if not success:
                    return jsonify({'success': False, 'error': 'Failed to authenticate with saved tokens. Please re-authenticate.'})
            else:
                return jsonify({'success': False, 'error': 'No valid tokens found. Please re-authenticate.'})
        
        req = request.get_json(silent=True) or {}
        email = req.get('email', '').strip()
        
        if not email:
            return jsonify({'success': False, 'error': 'Email address is required'})
        
        if '@' not in email:
            return jsonify({'success': False, 'error': 'Please provide a valid email address'})
        
        logging.info(f"Unsuspending user: {email}")
        
        # Unsuspend the user
        result = google_api.unsuspend_user(email)
        
        if result['success']:
            return jsonify({
                'success': True,
                'message': result['message'],
                'email': result['email']
            })
        else:
            return jsonify({
                'success': False,
                'error': result.get('error', 'Unknown error'),
                'email': result.get('email', email)
            })
            
    except Exception as e:
        logging.error(f"Unsuspend user error: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/get-suspended-users', methods=['GET'])
@login_required
def api_get_suspended_users():
    """Get all suspended users"""
    try:
        if 'current_account_name' not in session:
            return jsonify({'success': False, 'error': 'No account authenticated. Please authenticate first.'})
        
        account_name = session.get('current_account_name')
        
        # Check if we have valid tokens for this account
        if not google_api.service:
            if google_api.is_token_valid(account_name):
                success = google_api.authenticate_with_tokens(account_name)
                if not success:
                    return jsonify({'success': False, 'error': 'Failed to authenticate with saved tokens. Please re-authenticate.'})
            else:
                return jsonify({'success': False, 'error': 'No valid tokens found. Please re-authenticate.'})
        
        logging.info("Retrieving suspended users")
        
        # Get suspended users
        result = google_api.get_suspended_users()
        
        if result['success']:
            return jsonify({
                'success': True,
                'suspended_users': result['suspended_users'],
                'total_count': result['total_count']
            })
        else:
            return jsonify({
                'success': False,
                'error': result.get('error', 'Unknown error')
            })
            
    except Exception as e:
        logging.error(f"Get suspended users error: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/store-app-password', methods=['POST'])
@login_required
def api_store_app_password():
    """Store app password for a user alias"""
    try:
        req = request.get_json(silent=True) or {}
        user_alias = req.get('user_alias', '').strip()
        app_password = req.get('app_password', '').strip()
        domain = req.get('domain', '').strip()
        
        if not user_alias:
            return jsonify({'success': False, 'error': 'User alias is required'})
        
        if not app_password:
            return jsonify({'success': False, 'error': 'App password is required'})
        
        logging.info(f"Storing app password for user alias: {user_alias}")
        
        # Store app password
        result = google_api.store_app_password(user_alias, app_password, domain)
        
        if result['success']:
            return jsonify({
                'success': True,
                'message': result['message']
            })
        else:
            return jsonify({
                'success': False,
                'error': result.get('error', 'Unknown error')
            })
            
    except Exception as e:
        logging.error(f"Store app password error: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/get-app-password', methods=['POST'])
@login_required
def api_get_app_password():
    """Get app password for a user alias"""
    try:
        req = request.get_json(silent=True) or {}
        user_alias = req.get('user_alias', '').strip()
        
        if not user_alias:
            return jsonify({'success': False, 'error': 'User alias is required'})
        
        logging.info(f"Getting app password for user alias: {user_alias}")
        
        # Get app password
        result = google_api.get_app_password(user_alias)
        
        if result['success']:
            return jsonify({
                'success': True,
                'app_password': result['app_password'],
                'domain': result.get('domain', '')
            })
        else:
            return jsonify({
                'success': False,
                'error': result.get('error', 'Unknown error')
            })
            
    except Exception as e:
        logging.error(f"Get app password error: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/get-all-app-passwords', methods=['GET'])
@login_required
def api_get_all_app_passwords():
    """Get all stored app passwords"""
    try:
        logging.info("Getting all app passwords")
        
        # Get all app passwords
        result = google_api.get_all_app_passwords()
        
        if result['success']:
            return jsonify({
                'success': True,
                'app_passwords': result['app_passwords'],
                'total_count': result['total_count']
            })
        else:
            return jsonify({
                'success': False,
                'error': result.get('error', 'Unknown error')
            })
            
    except Exception as e:
        logging.error(f"Get all app passwords error: {e}")
        return jsonify({'success': False, 'error': str(e)})

# ===== APP PASSWORD MANAGEMENT API =====

@app.route('/api/test-app-passwords', methods=['GET'])
@login_required
def api_test_app_passwords():
    """Test endpoint to verify app passwords API is working"""
    try:
        # Test database access
        count = UserAppPassword.query.count()
        
        # Test insert
        test_user = UserAppPassword(
            username='test',
            domain='test.com',
            app_password='test123'
        )
        db.session.add(test_user)
        db.session.commit()
        
        # Delete test
        UserAppPassword.query.filter_by(username='test', domain='test.com').delete()
        db.session.commit()
        
        return jsonify({
            'success': True,
            'message': 'App passwords API is working',
            'database_count': count,
            'test_passed': True
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e),
            'message': 'Database error - table may not exist'
        })

@app.route('/api/debug-app-password-matching', methods=['POST'])
@login_required
def api_debug_app_password_matching():
    """Debug app password matching for a specific email"""
    try:
        data = request.get_json()
        email = data.get('email', '')
        
        if not email:
            return jsonify({'success': False, 'error': 'Email required'})
        
        username, domain = email.split('@', 1)
        username = username.strip().lower()
        domain = domain.strip().lower()
        email_lower = email.lower()
        
        debug_info = {
            'email': email,
            'username': username,
            'domain': domain,
            'email_lower': email_lower,
            'matches': []
        }
        
        # Test all matching strategies
        strategies = [
            ('Exact match', UserAppPassword.query.filter_by(username=username, domain=domain).first()),
            ('Case-insensitive exact', UserAppPassword.query.filter(
                db.func.lower(UserAppPassword.username) == username,
                db.func.lower(UserAppPassword.domain) == domain
            ).first()),
            ('Full email match', UserAppPassword.query.filter(
                db.func.lower(db.func.concat(UserAppPassword.username, '@', UserAppPassword.domain)) == email_lower
            ).first()),
            ('Wildcard domain', UserAppPassword.query.filter(
                db.func.lower(UserAppPassword.username) == username,
                UserAppPassword.domain == '*'
            ).first()),
            ('Username only', UserAppPassword.query.filter(
                db.func.lower(UserAppPassword.username) == username
            ).first()),
            ('Partial username', UserAppPassword.query.filter(
                db.func.lower(UserAppPassword.username).like(f'%{username}%')
            ).first()),
            ('Prefix match', UserAppPassword.query.filter(
                db.func.lower(UserAppPassword.username).like(f'{username}%')
            ).first())
        ]
        
        for strategy_name, result in strategies:
            if result:
                debug_info['matches'].append({
                    'strategy': strategy_name,
                    'found': True,
                    'username': result.username,
                    'domain': result.domain,
                    'has_password': bool(result.app_password)
                })
            else:
                debug_info['matches'].append({
                    'strategy': strategy_name,
                    'found': False
                })
        
        # Also show similar records
        similar_records = UserAppPassword.query.filter(
            db.func.lower(UserAppPassword.username).like(f'%{username}%')
        ).limit(10).all()
        
        debug_info['similar_records'] = [
            {
                'username': record.username,
                'domain': record.domain,
                'has_password': bool(record.app_password)
            } for record in similar_records
        ]
        
        return jsonify({
            'success': True,
            'debug_info': debug_info
        })
        
    except Exception as e:
        app.logger.error(f"Debug app password matching error: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/upload-app-passwords', methods=['POST'])
@login_required
@rate_limit("10 per hour")  # Limit to 10 uploads per hour per IP
def api_upload_app_passwords():
    """Upload and store app passwords from file - SIMPLE VERSION"""
    print("=== UPLOAD STARTED ===")
    
    try:
        # Test database connection first
        test_count = UserAppPassword.query.count()
        print(f"Database connection test: {test_count} existing records")
        # Check if file exists
        if 'file' not in request.files:
            print("ERROR: No file in request")
            return jsonify({'success': False, 'error': 'No file provided'})
        
        file = request.files['file']
        print(f"File received: {file.filename}, size: {len(file.read())}")
        file.seek(0)  # Reset file pointer
        
        # Read file content
        content = file.read().decode('utf-8')
        lines = content.split('\n')
        print(f"File has {len(lines)} lines")
        print(f"First few lines: {lines[:3]}")
        
        stored_count = 0
        updated_count = 0
        added_count = 0
        error_count = 0
        
        # Use a set to avoid duplicates within the same upload file
        processed_pairs = set()

        # Use session.no_autoflush to prevent premature flushes
        with db.session.no_autoflush:
            from sqlalchemy.dialects.postgresql import insert as pg_insert
            from sqlalchemy.exc import IntegrityError

            for line_num, line in enumerate(lines, 1):
                line = line.strip()
                if not line:
                    continue
                    
                print(f"Line {line_num}: {line}")
                
                # Accept both colon and comma separators
                if ':' in line or ',' in line:
                    sep = ':' if ':' in line else ','
                    email, password = line.split(sep, 1)
                    email = email.strip()
                    password = password.strip()
                    
                    # Remove SMTP part if it's already included in the password
                    if password.endswith(',smtp.gmail.com,587'):
                        password = password[:-len(',smtp.gmail.com,587')]
                        print(f"Removed SMTP part from password: {password}")
                    elif password.endswith(',smtp.gmail.com,587,smtp.gmail.com,587'):
                        password = password[:-len(',smtp.gmail.com,587,smtp.gmail.com,587')]
                        print(f"Removed duplicated SMTP part from password: {password}")
                    
                    if email and password:
                        # Extract username and domain
                        if '@' in email:
                            username, domain = email.split('@', 1)
                        else:
                            username, domain = email, '*'
                        
                        # Normalize for case-insensitive matching
                        username = username.strip().lower()
                        domain = domain.strip().lower()
                        pair_key = (username, domain)

                        # Skip duplicates in the same upload file
                        if pair_key in processed_pairs:
                            print(f"Skipping duplicate within file: {username}@{domain}")
                            continue
                        
                        try:
                            # PostgreSQL upsert to avoid unique constraint violations
                            stmt = pg_insert(UserAppPassword).values(
                                username=username,
                                domain=domain,
                                app_password=password
                            )
                            stmt = stmt.on_conflict_do_update(
                                index_elements=[UserAppPassword.username, UserAppPassword.domain],
                                set_={
                                    'app_password': password,
                                    'updated_at': db.func.current_timestamp()
                                }
                            )
                            db.session.execute(stmt)

                            # Track counters based on whether it already existed
                            existing = UserAppPassword.query.filter(
                                db.func.lower(UserAppPassword.username) == username,
                                db.func.lower(UserAppPassword.domain) == domain
                            ).first()
                            if existing and existing.app_password == password:
                                # Treat as update when the record was there
                                updated_count += 1
                            else:
                                added_count += 1

                            processed_pairs.add(pair_key)
                            stored_count += 1

                        except IntegrityError as ie:
                            # In the rare case of race conditions, fallback to update path
                            db.session.rollback()
                            try:
                                existing = UserAppPassword.query.filter(
                                    db.func.lower(UserAppPassword.username) == username,
                                    db.func.lower(UserAppPassword.domain) == domain
                                ).first()
                                if existing:
                                    existing.app_password = password
                                    existing.updated_at = db.func.current_timestamp()
                                    updated_count += 1
                                    stored_count += 1
                                    processed_pairs.add(pair_key)
                                else:
                                    # Retry insert
                                    db.session.add(UserAppPassword(
                                        username=username,
                                        domain=domain,
                                        app_password=password
                                    ))
                                    added_count += 1
                                    stored_count += 1
                                    processed_pairs.add(pair_key)
                            except Exception as e2:
                                error_count += 1
                                print(f"Integrity fallback failed for {username}@{domain}: {e2}")
                                db.session.rollback()
                                continue
                        except Exception as e:
                            error_count += 1
                            print(f"Error processing {username}@{domain}: {e}")
                            
                            # Check if this is a sequence issue (primary key conflict)
                            if 'duplicate key value violates unique constraint "user_app_password_pkey"' in str(e):
                                print(f"❌ Sequence issue detected for {username}@{domain}. The user_app_password_id_seq is out of sync.")
                                print(f"Please run: python fix_used_domain_sequence.py --all")
                                # Return early with sequence error
                                db.session.rollback()
                                return jsonify({
                                    'success': False, 
                                    'error': f'Database sequence error for {username}@{domain}. Please run the sequence fix script.',
                                    'sequence_error': True,
                                    'fix_command': 'python fix_used_domain_sequence.py --all'
                                })
                            
                            db.session.rollback()
                            # Try to continue with other records
                            continue
        
        db.session.commit()
        print(f"=== UPLOAD COMPLETE: {stored_count} passwords processed ===")
        print(f"Added: {added_count}, Updated: {updated_count}, Errors: {error_count}")
        
        return jsonify({
            'success': True,
            'count': stored_count,
            'added': added_count,
            'updated': updated_count,
            'errors': error_count,
            'message': f'Processed {stored_count} app passwords (Added: {added_count}, Updated: {updated_count}, Errors: {error_count})'
        })
        
    except Exception as e:
        print(f"=== UPLOAD ERROR: {str(e)} ===")
        print(f"Error type: {type(e)}")
        import traceback
        print(f"Traceback: {traceback.format_exc()}")
        db.session.rollback()
        
        # Check if this is a sequence issue
        if 'duplicate key value violates unique constraint "user_app_password_pkey"' in str(e):
            app.logger.error(f"❌ Sequence issue detected during app password upload. The user_app_password_id_seq is out of sync.")
            return jsonify({
                'success': False, 
                'error': 'Database sequence error. Please run the sequence fix script.',
                'sequence_error': True,
                'fix_command': 'python fix_used_domain_sequence.py --all'
            })
        
        return jsonify({'success': False, 'error': str(e), 'error_type': str(type(e))})

@app.route('/api/test-upload-endpoint', methods=['POST'])
@login_required
def api_test_upload_endpoint():
    """Test endpoint to verify upload functionality"""
    try:
        print("=== TEST UPLOAD ENDPOINT ===")
        
        # Test database connection
        count = UserAppPassword.query.count()
        print(f"Database test: {count} records")
        
        # Test file handling
        if 'file' not in request.files:
            return jsonify({'success': False, 'error': 'No file in request'})
        
        file = request.files['file']
        print(f"File test: {file.filename}, size: {len(file.read())}")
        file.seek(0)
        
        content = file.read().decode('utf-8')
        lines = content.split('\n')
        print(f"Content test: {len(lines)} lines")
        
        return jsonify({
            'success': True,
            'message': 'Upload endpoint test successful',
            'file_info': {
                'filename': file.filename,
                'size': len(content),
                'lines': len(lines)
            },
            'database_count': count
        })
        
    except Exception as e:
        print(f"Test endpoint error: {e}")
        import traceback
        print(f"Traceback: {traceback.format_exc()}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/list-app-passwords', methods=['GET'])
@login_required
def api_list_app_passwords():
    try:
        # Get all app passwords, not just first 100
        q = UserAppPassword.query.order_by(UserAppPassword.username.asc()).all()
        
        app.logger.info(f"Retrieved {len(q)} app passwords from database")
        
        users = []
        for r in q:
            if r.username and r.domain:  # Only include valid records
                users.append({
                    'username': r.username,
                    'domain': r.domain,
                    'app_password': r.app_password,
                    'has_password': bool(r.app_password and r.app_password.strip())
                })
        
        app.logger.info(f"Returning {len(users)} valid app password records")
        
        return jsonify({
            'success': True,
            'count': len(users),
            'total_in_db': len(q),
            'users': users
        })
    except Exception as e:
        app.logger.error(f"Error listing app passwords: {e}")
        import traceback
        app.logger.error(f"Traceback: {traceback.format_exc()}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/debug-app-passwords', methods=['GET'])
@login_required
def api_debug_app_passwords():
    """Debug endpoint to check app password data"""
    try:
        # Get total count
        total_count = UserAppPassword.query.count()
        
        # Get sample records
        sample_records = UserAppPassword.query.limit(10).all()
        
        # Get records with specific patterns
        recent_records = UserAppPassword.query.order_by(UserAppPassword.created_at.desc()).limit(5).all()
        
        debug_info = {
            'total_count': total_count,
            'sample_records': [
                {
                    'username': r.username,
                    'domain': r.domain,
                    'has_password': bool(r.app_password),
                    'created_at': str(r.created_at) if r.created_at else None
                } for r in sample_records
            ],
            'recent_records': [
                {
                    'username': r.username,
                    'domain': r.domain,
                    'has_password': bool(r.app_password),
                    'created_at': str(r.created_at) if r.created_at else None
                } for r in recent_records
            ]
        }
        
        return jsonify({
            'success': True,
            'debug_info': debug_info
        })
        
    except Exception as e:
        app.logger.error(f"Debug app passwords error: {e}")
        return jsonify({'success': False, 'error': str(e)})
        
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Error uploading app passwords: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/clear-app-passwords', methods=['DELETE'])
@login_required
def api_clear_app_passwords():
    """Clear all stored app passwords - Admin only"""
    # Check if user is admin
    if session.get('role') != 'admin':
        return jsonify({'success': False, 'error': 'Admin access required'})
    
    try:
        # Delete all app passwords
        deleted_count = UserAppPassword.query.count()
        UserAppPassword.query.delete()
        db.session.commit()
        
        app.logger.info(f"All app passwords cleared by admin: {session.get('user', 'unknown')}")
        
        return jsonify({
            'success': True,
            'message': f'All {deleted_count} app passwords cleared successfully'
        })
        
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Error clearing app passwords: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/app-passwords-status', methods=['GET'])
@login_required
def api_app_passwords_status():
    """Get app passwords status"""
    try:
        count = UserAppPassword.query.count()
        
        # Also return some sample data for debugging
        sample_passwords = UserAppPassword.query.limit(5).all()
        sample_data = []
        for pwd in sample_passwords:
            sample_data.append({
                'username': pwd.username,
                'domain': pwd.domain,
                'full_email': f"{pwd.username}@{pwd.domain}",
                'has_password': bool(pwd.app_password)
            })
        
        return jsonify({
            'success': True,
            'count': count,
            'sample_data': sample_data
        })
        
    except Exception as e:
        app.logger.error(f"Error getting app passwords status: {e}")
        return jsonify({'success': False, 'error': str(e)})

# ===== ADVANCED APP PASSWORD MANAGEMENT API =====

@app.route('/api/update-app-password', methods=['POST'])
@login_required
def api_update_app_password():
    """Update a specific app password"""
    try:
        data = request.get_json()
        username = data.get('username', '').strip()
        domain = data.get('domain', '').strip()
        app_password = data.get('app_password', '').strip()
        
        if not username or not app_password:
            return jsonify({'success': False, 'error': 'Username and app password are required'})
        
        # Find the existing record
        existing = UserAppPassword.query.filter_by(username=username, domain=domain).first()
        if not existing:
            return jsonify({'success': False, 'error': 'App password record not found'})
        
        # Update the password
        existing.app_password = app_password
        db.session.commit()
        
        return jsonify({'success': True, 'message': 'App password updated successfully'})
        
    except Exception as e:
        app.logger.error(f"Error updating app password: {e}")
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/delete-app-password', methods=['POST'])
@login_required
def api_delete_app_password():
    """Delete a specific app password"""
    try:
        data = request.get_json()
        username = data.get('username', '').strip()
        domain = data.get('domain', '').strip()
        
        if not username:
            return jsonify({'success': False, 'error': 'Username is required'})
        
        # Find and delete the record
        existing = UserAppPassword.query.filter_by(username=username, domain=domain).first()
        if not existing:
            return jsonify({'success': False, 'error': 'App password record not found'})
        
        db.session.delete(existing)
        db.session.commit()
        
        return jsonify({'success': True, 'message': 'App password deleted successfully'})
        
    except Exception as e:
        app.logger.error(f"Error deleting app password: {e}")
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/delete-specific-app-passwords', methods=['POST'])
@login_required
def api_delete_specific_app_passwords():
    """Delete specific app passwords by username/email list"""
    try:
        data = request.get_json()
        users = data.get('users', [])
        
        if not users:
            return jsonify({'success': False, 'error': 'No users provided'})
        
        deleted_count = 0
        errors = []
        
        for user_input in users:
            user_input = user_input.strip()
            if not user_input:
                continue
                
            try:
                # Handle both email format (user@domain) and username format
                if '@' in user_input:
                    username, domain = user_input.split('@', 1)
                else:
                    username = user_input
                    domain = '*'  # Try wildcard domain first
                
                # Try to find and delete the record
                existing = UserAppPassword.query.filter_by(username=username, domain=domain).first()
                if existing:
                    db.session.delete(existing)
                    deleted_count += 1
                else:
                    # Try with wildcard domain if not found
                    if domain != '*':
                        existing = UserAppPassword.query.filter_by(username=username, domain='*').first()
                        if existing:
                            db.session.delete(existing)
                            deleted_count += 1
                        else:
                            errors.append(f"User not found: {user_input}")
                    else:
                        errors.append(f"User not found: {user_input}")
                        
            except Exception as e:
                errors.append(f"Error processing {user_input}: {str(e)}")
        
        db.session.commit()
        
        return jsonify({
            'success': True,
            'deleted_count': deleted_count,
            'errors': errors,
            'message': f'Successfully deleted {deleted_count} app password(s)'
        })
        
    except Exception as e:
        app.logger.error(f"Error deleting specific app passwords: {e}")
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)})

# ===== USER TYPE DETECTION API =====

@app.route('/api/detect-user-types', methods=['POST'])
@login_required
def api_detect_user_types():
    """Detect if users are regular users or admins using Google Admin API"""
    try:
        data = request.get_json()
        users = data.get('users', [])
        
        if not users:
            return jsonify({'success': False, 'error': 'No users provided'})
        
        user_types = []
        
        for user in users:
            email = user.get('email', '') or user.get('primaryEmail', '')
            source_account = user.get('source_account', '')
            
            # Debug logging
            app.logger.info(f"Processing user: {email}")
            app.logger.info(f"User data keys: {list(user.keys())}")
            app.logger.info(f"isAdmin field: {user.get('isAdmin', 'NOT_FOUND')}")
            app.logger.info(f"isAdmin type: {type(user.get('isAdmin', 'NOT_FOUND'))}")
            
            # Determine user type using Google Admin API data
            user_type = 'user'  # Default to user
            
            # Method 1: Check if user has isAdmin field from Google Admin API
            is_admin_api = user.get('isAdmin', False)
            if is_admin_api:
                user_type = 'admin'
                app.logger.info(f"Admin detected via API for {email}")
            # Method 2: Check if email matches source account (authentication account)
            elif email and source_account and email.lower() == source_account.lower():
                user_type = 'admin'
                app.logger.info(f"Admin detected via source account match for {email}")
            # Method 3: Check for common admin patterns in email
            elif email:
                email_lower = email.lower()
                admin_patterns = ['admin', 'support', 'noreply', 'postmaster', 'abuse', 'webmaster', 'administrator', 'contact']
                if any(pattern in email_lower for pattern in admin_patterns):
                    user_type = 'admin'
                    app.logger.info(f"Admin detected via pattern match for {email}")
            
            user_types.append({
                'email': email,
                'user_type': user_type,
                'source_account': source_account,
                'is_admin_api': is_admin_api
            })
        
        return jsonify({
            'success': True,
            'user_types': user_types
        })
        
    except Exception as e:
        app.logger.error(f"Error detecting user types: {e}")
        return jsonify({'success': False, 'error': str(e)})

# ===== SIMPLIFIED AUTOMATION AUTHENTICATION API =====

# Global dictionary to store automation tasks (in production, use Redis or database)
automation_tasks = {}

def authenticate_without_session(account_name):
    """Authenticate without using Flask session - for background threads"""
    try:
        # First try ServiceAccount (newer approach)
        service_account = ServiceAccount.query.filter(
            (ServiceAccount.name == account_name) |
            (ServiceAccount.admin_email.ilike(account_name))
        ).first()
        
        if service_account and service_account.json_content:
            import json
            from google.oauth2 import service_account as sa
            import google.auth.transport.requests
            
            # Parse the JSON content
            json_content = json.loads(service_account.json_content)
            
            # Create credentials with domain-wide delegation
            scopes = [
                'https://www.googleapis.com/auth/admin.directory.user',
                'https://www.googleapis.com/auth/admin.directory.domain',
                'https://www.googleapis.com/auth/admin.directory.group',
                'https://www.googleapis.com/auth/admin.directory.orgunit'
            ]
            
            credentials = sa.Credentials.from_service_account_info(
                json_content,
                scopes=scopes
            )
            
            # Impersonate the admin user for domain-wide delegation
            admin_email = service_account.admin_email
            if admin_email:
                credentials = credentials.with_subject(admin_email)
            
            from googleapiclient.discovery import build
            service = build('admin', 'directory_v1', credentials=credentials)
            
            # Store service in global dictionary instead of session
            service_key = f"service_{account_name}"
            automation_tasks[service_key] = service
            
            app.logger.info(f"ServiceAccount authentication successful for {account_name}")
            return True
        
        # Fallback to GoogleAccount (OAuth - legacy)
        account = GoogleAccount.query.filter(GoogleAccount.account_name.ilike(account_name)).first()
        if not account or not account.tokens:
            app.logger.warning(f"No account found for {account_name} (checked ServiceAccount and GoogleAccount)")
            return False
        
        token = account.tokens[0]
        scopes = [scope.name for scope in token.scopes]
        
        from google.oauth2.credentials import Credentials
        import google.auth.transport.requests
        
        creds = Credentials(
            token=token.token,
            refresh_token=token.refresh_token,
            token_uri=token.token_uri,
            client_id=account.client_id,
            client_secret=account.client_secret,
            scopes=scopes
        )
        
        if creds.expired and creds.refresh_token:
            creds.refresh(google.auth.transport.requests.Request())
        
        if creds.valid:
            from googleapiclient.discovery import build
            service = build('admin', 'directory_v1', credentials=creds)
            
            # Store service in global dictionary instead of session
            service_key = f"service_{account_name}"
            automation_tasks[service_key] = service
            
            return True
        return False
        
    except Exception as e:
        app.logger.error(f"Error in authenticate_without_session for {account_name}: {e}")
        import traceback
        app.logger.error(traceback.format_exc())
        return False

def get_service_without_session(account_name):
    """Get service without using Flask session - for background threads"""
    service_key = f"service_{account_name}"
    return automation_tasks.get(service_key)

def get_users_with_service(service):
    """Get users using a Google service object - for background threads"""
    try:
        # Get users from Google Workspace
        results = service.users().list(customer='my_customer', maxResults=500).execute()
        users = results.get('users', [])
        
        return {
            'success': True,
            'users': users,
            'total': len(users)
        }
    except Exception as e:
        app.logger.error(f"Error getting users with service: {e}")
        return {
            'success': False,
            'error': str(e),
            'users': []
        }

@app.route('/api/start-automation-process', methods=['POST'])
@login_required
@rate_limit("5 per minute")  # Limit to 5 automation processes per minute per IP
def api_start_automation_process():
    """Start automation process - return account list for sequential processing"""
    try:
        data = request.get_json()
        if not data:
            return jsonify({'success': False, 'error': 'No JSON data received'})
            
        accounts_text = data.get('accounts', '')
        
        if not accounts_text.strip():
            return jsonify({'success': False, 'error': 'No accounts provided'})
        
        # Parse accounts from text
        accounts = [line.strip() for line in accounts_text.split('\n') if line.strip()]
        
        # No limit - support unlimited concurrent machines
        # if len(accounts) > 50:
        #     return jsonify({'success': False, 'error': f'Too many accounts ({len(accounts)}). Maximum 50 accounts allowed per batch for performance.'})
        
        app.logger.info(f"🚀 Starting SEQUENTIAL automation process for {len(accounts)} accounts")
        
        # Return account list for frontend to process sequentially
        return jsonify({
            'success': True,
            'accounts': accounts,
            'total_count': len(accounts),
            'message': f'Ready to process {len(accounts)} accounts sequentially'
        })
        
    except Exception as e:
        app.logger.error(f"Error in SEQUENTIAL automation process: {e}")
        import traceback
        app.logger.error(f"Full traceback: {traceback.format_exc()}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/process-single-account', methods=['POST'])
@login_required
def api_process_single_account():
    """Process a single account - no timeout issues"""
    try:
        data = request.get_json()
        if not data:
            return jsonify({'success': False, 'error': 'No JSON data received'})
            
        account_email = data.get('account', '').strip()
        
        if not account_email:
            return jsonify({'success': False, 'error': 'No account provided'})
        
        app.logger.info(f"📧 Processing single account: {account_email}")
        
        result = {
            'account': account_email,
            'success': False,
            'message': '',
            'users_count': 0,
            'users': []
        }
        
        # Find account in database
        google_account = GoogleAccount.query.filter(
            db.func.lower(GoogleAccount.account_name) == account_email.lower()
        ).first()
        
        if not google_account:
            result['message'] = 'Account not found in database'
            return jsonify({'success': True, 'result': result})
        
        app.logger.info(f"Found account in database: {google_account.account_name} (input was: {account_email})")
        
        # Try to authenticate the account using existing tokens
        auth_success = False
        try:
            db_account_name = google_account.account_name
            app.logger.info(f"Authenticating with database account name: {db_account_name}")
            
            if google_api.is_token_valid(db_account_name):
                auth_success = authenticate_without_session(db_account_name)
                if auth_success:
                    app.logger.info(f"Successfully authenticated with {db_account_name}")
            else:
                app.logger.warning(f"No valid tokens found for {db_account_name}")
        except Exception as e:
            app.logger.warning(f"Token authentication failed for {account_email}: {e}")
        
        if auth_success:
            result['success'] = True
            result['message'] = 'Successfully authenticated'
            
            # Try to retrieve users for this account
            try:
                app.logger.info(f"🔍 Retrieving users for account: {db_account_name}")
                service = get_service_without_session(db_account_name)
                if service:
                    users_data = get_users_with_service(service)
                    app.logger.info(f"📊 Users data received: {type(users_data)}")
                    if users_data and 'users' in users_data:
                        account_users = users_data['users']
                        result['users_count'] = len(account_users)
                        result['users'] = account_users
                        
                        # Add account info and app password to each user
                        for user in account_users:
                            user['source_account'] = account_email
                            
                            # Try to find matching app password
                            user_email = user.get('email', '') or user.get('primaryEmail', '')
                            
                            # Skip admin/authentication accounts - only process actual users
                            if user_email and '@' in user_email and user_email.lower() != account_email.lower():
                                try:
                                    username, domain = user_email.split('@', 1)
                                    
                                    # Normalize username & domain for matching
                                    username = (username or '').strip().lower()
                                    domain = (domain or '').strip().lower()

                                    app.logger.info(f"Searching app password for: {user_email} (username: {username}, domain: {domain})")

                                    # Strategy 1: Exact match (username + domain)
                                    app_password_record = UserAppPassword.query.filter_by(
                                        username=username, 
                                        domain=domain
                                    ).first()
                                    
                                    if app_password_record:
                                        app.logger.info(f"Found exact match for {user_email}")
                                    else:
                                        # Strategy 2: Case-insensitive exact match
                                        app_password_record = UserAppPassword.query.filter(
                                            db.func.lower(UserAppPassword.username) == username,
                                            db.func.lower(UserAppPassword.domain) == domain
                                        ).first()
                                        
                                        if app_password_record:
                                            app.logger.info(f"Found case-insensitive match for {user_email}")
                                        else:
                                            # Strategy 3: Username-only match (any domain)
                                            app_password_record = UserAppPassword.query.filter(
                                                db.func.lower(UserAppPassword.username) == username
                                            ).first()
                                            
                                            if app_password_record:
                                                app.logger.info(f"Found username-only match for {user_email}")
                                    
                                    if app_password_record:
                                        user['app_password'] = app_password_record.app_password
                                        user['app_password_domain'] = app_password_record.domain
                                        app.logger.info(f"✅ Found app password for {user_email} -> {app_password_record.username}@{app_password_record.domain}")
                                    else:
                                        app.logger.info(f"❌ No app password found for {user_email} after trying all strategies")
                                        user['app_password'] = None
                                except Exception as e:
                                    app.logger.warning(f"Error processing app password for {user_email}: {e}")
                                    user['app_password'] = None
                            else:
                                # Skip admin/authentication accounts - no app password needed
                                user['app_password'] = None
                                if user_email and user_email.lower() == account_email.lower():
                                    app.logger.info(f"Skipped admin account {user_email} - no app password needed")
                        
                        result['message'] += f' and retrieved {result["users_count"]} users'
                    else:
                        app.logger.warning(f"No users data received for {db_account_name}")
                        result['message'] += ' but no users found'
                else:
                    app.logger.warning(f"No service available for {db_account_name}")
                    result['message'] += ' but failed to get service'
            except Exception as e:
                result['message'] += f' but failed to retrieve users: {str(e)}'
        else:
            result['message'] = 'Failed to authenticate - may need OAuth authorization'
        
        app.logger.info(f"✅ Single account processing completed: {account_email} - {result['message']}")
        
        return jsonify({
            'success': True,
            'result': result
        })
        
    except Exception as e:
        app.logger.error(f"Error in single account processing: {e}")
        import traceback
        app.logger.error(f"Full traceback: {traceback.format_exc()}")
        return jsonify({'success': False, 'error': str(e)})

def execute_automation_background(task_id, accounts):
    """Execute automation process in background thread"""
    # Create Flask application context for background thread
    with app.app_context():
        try:
            app.logger.info(f"🔄 Starting background automation task {task_id}")
            
            # Update task status in global dictionary
            automation_tasks[task_id]['status'] = 'running'
            
            results = []
            authenticated_count = 0
            all_users = []
            users_retrieved = 0
            
            # Process each account
            for index, account_email in enumerate(accounts, 1):
                try:
                    app.logger.info(f"📧 Processing account {index}/{len(accounts)}: {account_email}")
                    
                    # Update progress
                    progress = int((index - 1) / len(accounts) * 100)
                    automation_tasks[task_id]['progress'] = progress
                    
                    result = {
                        'account': account_email,
                        'success': False,
                        'message': '',
                        'users_count': 0,
                        'users': []
                    }
                    
                    # Find the GoogleAccount by email/account name (case-insensitive)
                    google_account = GoogleAccount.query.filter(
                        db.func.lower(GoogleAccount.account_name) == account_email.lower()
                    ).first()
                    
                    if not google_account:
                        # Try exact match as fallback
                        google_account = GoogleAccount.query.filter_by(account_name=account_email).first()
                    
                    # If still not found, try partial domain matching
                    if not google_account:
                        app.logger.info(f"Trying partial domain matching for {account_email}")
                        # Extract domain from account email
                        if '@' in account_email:
                            domain_part = account_email.split('@')[1].lower()
                            # Try to find accounts with similar domains
                            similar_accounts = GoogleAccount.query.filter(
                                db.func.lower(GoogleAccount.account_name).like(f'%@{domain_part}')
                            ).all()
                            app.logger.info(f"Found {len(similar_accounts)} accounts with similar domain: {[acc.account_name for acc in similar_accounts]}")
                            
                            # Try to find the closest match
                            for acc in similar_accounts:
                                if acc.account_name.lower() == account_email.lower():
                                    google_account = acc
                                    break
                            
                            if not google_account and similar_accounts:
                                google_account = similar_accounts[0]  # Use first match
                    
                    if not google_account:
                        result['message'] = 'Account not found in database'
                        results.append(result)
                        continue
                    
                    app.logger.info(f"Found account in database: {google_account.account_name} (input was: {account_email})")
                    
                    # Try to authenticate the account using existing tokens
                    auth_success = False
                    try:
                        # Use the database account name for authentication
                        db_account_name = google_account.account_name
                        app.logger.info(f"Authenticating with database account name: {db_account_name}")
                        
                        if google_api.is_token_valid(db_account_name):
                            # Use session-independent authentication for background thread
                            auth_success = authenticate_without_session(db_account_name)
                            if auth_success:
                                app.logger.info(f"Successfully authenticated with {db_account_name}")
                        else:
                            app.logger.warning(f"No valid tokens found for {db_account_name}")
                    except Exception as e:
                        app.logger.warning(f"Token authentication failed for {account_email}: {e}")
                    
                    if auth_success:
                        authenticated_count += 1
                        result['success'] = True
                        result['message'] = 'Successfully authenticated'
                        
                        # Try to retrieve users for this account
                        try:
                            app.logger.info(f"🔍 Retrieving users for account: {db_account_name}")
                            # Use session-independent service for background thread
                            service = get_service_without_session(db_account_name)
                            if service:
                                users_data = get_users_with_service(service)
                                app.logger.info(f"📊 Users data received: {type(users_data)}")
                                if users_data and 'users' in users_data:
                                    account_users = users_data['users']
                                    result['users_count'] = len(account_users)
                                    result['users'] = account_users
                                    
                                    # Add account info and app password to each user
                                    for user in account_users:
                                        user['source_account'] = account_email
                                        
                                        # Try to find matching app password
                                        user_email = user.get('email', '') or user.get('primaryEmail', '')
                                        
                                        # Skip admin/authentication accounts - only process actual users
                                        if user_email and '@' in user_email and user_email.lower() != account_email.lower():
                                            try:
                                                username, domain = user_email.split('@', 1)
                                                
                                                # Normalize username & domain for matching
                                                username = (username or '').strip().lower()
                                                domain = (domain or '').strip().lower()
                                                user_email_lower = user_email.lower()

                                                app.logger.info(f"Searching app password for: {user_email} (username: {username}, domain: {domain})")

                                                # Strategy 1: Exact match (username + domain)
                                                app_password_record = UserAppPassword.query.filter_by(
                                                    username=username, 
                                                    domain=domain
                                                ).first()
                                                
                                                if app_password_record:
                                                    app.logger.info(f"Found exact match for {user_email}")
                                                else:
                                                    # Strategy 2: Case-insensitive exact match
                                                    app_password_record = UserAppPassword.query.filter(
                                                        db.func.lower(UserAppPassword.username) == username,
                                                        db.func.lower(UserAppPassword.domain) == domain
                                                    ).first()
                                                    
                                                    if app_password_record:
                                                        app.logger.info(f"Found case-insensitive match for {user_email}")
                                                    else:
                                                        # Strategy 3: Username-only match (any domain)
                                                        app_password_record = UserAppPassword.query.filter(
                                                            db.func.lower(UserAppPassword.username) == username
                                                        ).first()
                                                        
                                                        if app_password_record:
                                                            app.logger.info(f"Found username-only match for {user_email}")
                                                
                                                if app_password_record:
                                                    user['app_password'] = app_password_record.app_password
                                                    user['app_password_domain'] = app_password_record.domain
                                                    app.logger.info(f"✅ Found app password for {user_email} -> {app_password_record.username}@{app_password_record.domain}")
                                                else:
                                                    app.logger.info(f"❌ No app password found for {user_email} after trying all strategies")
                                                    user['app_password'] = None
                                            except Exception as e:
                                                app.logger.warning(f"Error processing app password for {user_email}: {e}")
                                                user['app_password'] = None
                                        else:
                                            # Skip admin/authentication accounts - no app password needed
                                            user['app_password'] = None
                                            if user_email and user_email.lower() == account_email.lower():
                                                app.logger.info(f"Skipped admin account {user_email} - no app password needed")
                                    
                                    all_users.extend(account_users)
                                    users_retrieved += result['users_count']
                                    result['message'] += f' and retrieved {result["users_count"]} users'
                                else:
                                    app.logger.warning(f"No users data received for {db_account_name}")
                                    result['message'] += ' but no users found'
                            else:
                                app.logger.warning(f"No service available for {db_account_name}")
                                result['message'] += ' but failed to get service'
                        except Exception as e:
                            result['message'] += f' but failed to retrieve users: {str(e)}'
                    else:
                        result['message'] = 'Failed to authenticate - may need OAuth authorization'
                    
                    results.append(result)
                    
                    # Add small delay between accounts to avoid API rate limiting
                    if index < len(accounts):  # Don't delay after the last account
                        import time
                        time.sleep(1)  # 1 second delay between accounts
                    
                except Exception as e:
                    app.logger.error(f"❌ Error processing account {account_email}: {e}")
                    import traceback
                    error_details = traceback.format_exc()
                    app.logger.error(f"Account processing traceback: {error_details}")
                    results.append({
                        'account': account_email,
                        'success': False,
                        'message': f'Error processing account: {str(e)}',
                        'users_count': 0,
                        'users': []
                    })
            
            # Mark task as completed
            automation_tasks[task_id]['status'] = 'completed'
            automation_tasks[task_id]['progress'] = 100
            automation_tasks[task_id]['results'] = results
            automation_tasks[task_id]['authenticated_count'] = authenticated_count
            automation_tasks[task_id]['users_retrieved'] = users_retrieved
            automation_tasks[task_id]['all_users'] = all_users
            automation_tasks[task_id]['completed_at'] = time.time()
            
            app.logger.info(f"✅ Background automation task {task_id} completed: {authenticated_count}/{len(accounts)} authenticated, {users_retrieved} users retrieved")
            
        except Exception as e:
            app.logger.error(f"❌ Background automation task {task_id} failed: {e}")
            import traceback
            error_details = traceback.format_exc()
            app.logger.error(f"Background task traceback: {error_details}")
            
            # Mark task as failed
            automation_tasks[task_id]['status'] = 'failed'
            automation_tasks[task_id]['error'] = str(e)

@app.route('/api/check-automation-status', methods=['POST'])
@login_required
def api_check_automation_status():
    """Check the status of a background automation task"""
    try:
        data = request.get_json()
        task_id = data.get('task_id')
        
        if not task_id:
            return jsonify({'success': False, 'error': 'No task ID provided'})
        
        if task_id not in automation_tasks:
            app.logger.warning(f"Task {task_id} not found in automation_tasks. Available tasks: {list(automation_tasks.keys())}")
            return jsonify({'success': False, 'error': 'Task not found or expired'})
        
        task_data = automation_tasks[task_id]
        
        # Add task cleanup after successful completion (keep for 5 minutes)
        if task_data['status'] == 'completed':
            import time
            completed_time = task_data.get('completed_at', time.time())
            if time.time() - completed_time > 300:  # 5 minutes
                app.logger.info(f"Cleaning up old completed task {task_id}")
                del automation_tasks[task_id]
                return jsonify({'success': False, 'error': 'Task expired after completion'})
        
        if task_data['status'] == 'completed':
            return jsonify({
                'success': True,
                'status': 'completed',
                'processed_count': len(task_data['accounts']),
                'authenticated_count': task_data['authenticated_count'],
                'users_retrieved': task_data['users_retrieved'],
                'all_users': task_data['all_users'],
                'results': task_data['results']
            })
        elif task_data['status'] == 'failed':
            return jsonify({
                'success': False,
                'status': 'failed',
                'error': task_data.get('error', 'Unknown error')
            })
        else:
            return jsonify({
                'success': True,
                'status': task_data['status'],
                'progress': task_data['progress']
            })
        
    except Exception as e:
        app.logger.error(f"Error checking automation status: {e}")
        return jsonify({'success': False, 'error': str(e)})

# Legacy endpoint for backward compatibility
@app.route('/api/execute-automation-process', methods=['POST'])
@login_required
def api_execute_automation_process():
    """Execute the complete automation process: authenticate + retrieve users from multiple accounts"""
    # Add timeout protection for large batches
    import signal
    
    def timeout_handler(signum, frame):
        raise TimeoutError("Automation process timeout")
    
    # Set timeout to 20 minutes for large batches
    signal.signal(signal.SIGALRM, timeout_handler)
    signal.alarm(1200)  # 20 minutes timeout
    
    try:
        data = request.get_json()
        accounts = data.get('accounts', [])
        
        if not accounts:
            signal.alarm(0)  # Cancel timeout
            return jsonify({'success': False, 'error': 'No accounts provided'})
        
        # Add account limit check with warning
        if len(accounts) > 50:
            signal.alarm(0)  # Cancel timeout
            return jsonify({'success': False, 'error': f'Too many accounts ({len(accounts)}). Maximum 50 accounts allowed per batch for performance.'})
        
        app.logger.info(f"🚀 Starting automation process for {len(accounts)} accounts with 20-minute timeout")
        app.logger.info(f"📋 Accounts to process: {accounts}")
        
        # Check if Google API is properly initialized
        if not google_api:
            signal.alarm(0)  # Cancel timeout
            app.logger.error("Google API not initialized")
            return jsonify({'success': False, 'error': 'Google API not initialized. Please restart the application.'})
        
        app.logger.info(f"✅ Google API initialized: {type(google_api)}")
        
        results = []
        authenticated_count = 0
        all_users = []  # Store all users from all accounts
        users_retrieved = 0
        
        # Process each account with progress tracking
        for index, account_email in enumerate(accounts, 1):
            try:
                app.logger.info(f"📧 Processing account {index}/{len(accounts)}: {account_email}")
                
                result = {
                    'account': account_email,
                    'success': False,
                    'message': '',
                    'users_count': 0,
                    'users': []
                }
                
                # Find the GoogleAccount by email/account name (case-insensitive)
                google_account = GoogleAccount.query.filter(
                    db.func.lower(GoogleAccount.account_name) == account_email.lower()
                ).first()
                
                if not google_account:
                    # Try exact match as fallback
                    google_account = GoogleAccount.query.filter_by(account_name=account_email).first()
                
                # If still not found, try partial domain matching
                if not google_account:
                    app.logger.info(f"Trying partial domain matching for {account_email}")
                    # Extract domain from account email
                    if '@' in account_email:
                        domain_part = account_email.split('@')[1].lower()
                        # Try to find accounts with similar domains
                        similar_accounts = GoogleAccount.query.filter(
                            db.func.lower(GoogleAccount.account_name).like(f'%@{domain_part}')
                        ).all()
                        app.logger.info(f"Found {len(similar_accounts)} accounts with similar domain: {[acc.account_name for acc in similar_accounts]}")
                        
                        # Try to find the closest match
                        for acc in similar_accounts:
                            if domain_part in acc.account_name.lower():
                                google_account = acc
                                app.logger.info(f"Found partial domain match: {acc.account_name}")
                                break
                
                if not google_account:
                    # Debug: List all accounts in database for troubleshooting
                    all_accounts = GoogleAccount.query.all()
                    account_names = [acc.account_name for acc in all_accounts]
                    app.logger.warning(f"Account {account_email} not found in database. Available accounts: {account_names}")
                    result['message'] = f'Account not found in database. Available accounts: {account_names[:5]}...'  # Show first 5 for brevity
                    results.append(result)
                    continue
                
                app.logger.info(f"Found account in database: {google_account.account_name} (input was: {account_email})")
                
                # Try to authenticate the account using existing tokens
                auth_success = False
                try:
                    # Use the database account name for authentication
                    db_account_name = google_account.account_name
                    app.logger.info(f"Authenticating with database account name: {db_account_name}")
                    
                    if google_api.is_token_valid(db_account_name):
                        auth_success = google_api.authenticate_with_tokens(db_account_name)
                        if auth_success:
                            # Note: Cannot set session in background thread, but authentication works
                            app.logger.info(f"Successfully authenticated with {db_account_name}")
                    else:
                        app.logger.warning(f"No valid tokens found for {db_account_name}")
                except Exception as e:
                    app.logger.warning(f"Token authentication failed for {account_email}: {e}")
                
                if auth_success:
                    authenticated_count += 1
                    result['success'] = True
                    result['message'] = 'Successfully authenticated'
                    
                    # Try to retrieve users for this account
                    try:
                        app.logger.info(f"🔍 Retrieving users for account: {db_account_name}")
                        # Use the existing retrieve users functionality
                        users_data = google_api.get_users()
                        app.logger.info(f"📊 Users data received: {type(users_data)}")
                        if users_data and 'users' in users_data:
                            account_users = users_data['users']
                            result['users_count'] = len(account_users)
                            result['users'] = account_users
                            
                            # Add account info and app password to each user
                            for user in account_users:
                                user['source_account'] = account_email
                                
                                # Try to find matching app password
                                user_email = user.get('email', '') or user.get('primaryEmail', '')
                                
                                # Skip admin/authentication accounts - only process actual users
                                if user_email and '@' in user_email and user_email.lower() != account_email.lower():
                                    try:
                                        username, domain = user_email.split('@', 1)
                                        
                                        # Normalize username & domain for matching
                                        username = (username or '').strip().lower()
                                        domain = (domain or '').strip().lower()
                                        user_email_lower = user_email.lower()

                                        app.logger.info(f"Searching app password for: {user_email} (username: {username}, domain: {domain})")

                                        # Strategy 1: Exact match (username + domain)
                                        app_password_record = UserAppPassword.query.filter_by(
                                            username=username, 
                                            domain=domain
                                        ).first()
                                        
                                        if app_password_record:
                                            app.logger.info(f"Found exact match for {user_email}")
                                        else:
                                            # Strategy 2: Case-insensitive exact match
                                            app_password_record = UserAppPassword.query.filter(
                                                db.func.lower(UserAppPassword.username) == username,
                                                db.func.lower(UserAppPassword.domain) == domain
                                            ).first()
                                            
                                            if app_password_record:
                                                app.logger.info(f"Found case-insensitive exact match for {user_email}")
                                            else:
                                                # Strategy 3: Full email match (case-insensitive)
                                                app_password_record = UserAppPassword.query.filter(
                                                    db.func.lower(db.func.concat(UserAppPassword.username, '@', UserAppPassword.domain)) == user_email_lower
                                                ).first()
                                                
                                                if app_password_record:
                                                    app.logger.info(f"Found full email match for {user_email}")
                                                else:
                                                    # Strategy 4: Username with wildcard domain
                                                    app_password_record = UserAppPassword.query.filter(
                                                        db.func.lower(UserAppPassword.username) == username,
                                                        UserAppPassword.domain == '*'
                                                    ).first()
                                                    
                                                    if app_password_record:
                                                        app.logger.info(f"Found wildcard domain match for {user_email}")
                                                    else:
                                                        # Strategy 5: Username only (ignore domain differences)
                                                        app_password_record = UserAppPassword.query.filter(
                                                            db.func.lower(UserAppPassword.username) == username
                                                        ).first()
                                                        
                                                        if app_password_record:
                                                            app.logger.info(f"Found username-only match for {user_email}")
                                                        else:
                                                            # Strategy 6: Partial username match (for complex aliases)
                                                            app_password_record = UserAppPassword.query.filter(
                                                                db.func.lower(UserAppPassword.username).like(f'%{username}%')
                                                            ).first()
                                                            
                                                            if app_password_record:
                                                                app.logger.info(f"Found partial username match for {user_email}")
                                                            else:
                                                                # Strategy 7: Check if username is contained in stored username
                                                                app_password_record = UserAppPassword.query.filter(
                                                                    db.func.lower(UserAppPassword.username).like(f'{username}%')
                                                                ).first()
                                                                
                                                                if app_password_record:
                                                                    app.logger.info(f"Found prefix match for {user_email}")
                                                                
                                        if app_password_record:
                                            user['app_password'] = app_password_record.app_password
                                            app.logger.info(f"✅ Found app password for {user_email} -> {app_password_record.username}@{app_password_record.domain}")
                                        else:
                                            user['app_password'] = None
                                            app.logger.info(f"❌ No app password found for {user_email} after trying all strategies")
                                            
                                            # Debug: Show what's in the database for this username
                                            debug_records = UserAppPassword.query.filter(
                                                db.func.lower(UserAppPassword.username).like(f'%{username}%')
                                            ).limit(5).all()
                                            
                                            if debug_records:
                                                app.logger.info(f"Debug: Found similar records for {username}:")
                                                for record in debug_records:
                                                    app.logger.info(f"  - {record.username}@{record.domain}")
                                            
                                    except Exception as e:
                                        app.logger.warning(f"Error matching app password for {user_email}: {e}")
                                        user['app_password'] = None
                                else:
                                    # Skip admin/authentication accounts - no app password needed
                                    user['app_password'] = None
                                    if user_email and user_email.lower() == account_email.lower():
                                        app.logger.info(f"Skipped admin account {user_email} - no app password needed")
                            
                            all_users.extend(account_users)
                            users_retrieved += result['users_count']
                            result['message'] += f' and retrieved {result["users_count"]} users'
                        else:
                            result['message'] += ' but no users found'
                    except Exception as e:
                        result['message'] += f' but failed to retrieve users: {str(e)}'
                else:
                    result['message'] = 'Failed to authenticate - may need OAuth authorization'
                
                results.append(result)
                
                # Add small delay between accounts to avoid API rate limiting
                if index < len(accounts):  # Don't delay after the last account
                    import time
                    time.sleep(1)  # 1 second delay between accounts
                
            except Exception as e:
                app.logger.error(f"❌ Error processing account {account_email}: {e}")
                import traceback
                error_details = traceback.format_exc()
                app.logger.error(f"Account processing traceback: {error_details}")
                results.append({
                    'account': account_email,
                    'success': False,
                    'message': f'Error processing account: {str(e)}',
                    'users_count': 0,
                    'users': []
                })
        
        # Cancel timeout on successful completion
        signal.alarm(0)
        
        app.logger.info(f"✅ Automation process completed: {authenticated_count}/{len(accounts)} authenticated, {users_retrieved} users retrieved")
        
        # Check session validity before returning response
        if 'user' not in session:
            app.logger.error("Session expired during automation process")
            return jsonify({'success': False, 'error': 'Session expired. Please log in again.'})
        
        response_data = {
            'success': True,
            'processed_count': len(accounts),
            'authenticated_count': authenticated_count,
            'users_retrieved': users_retrieved,
            'all_users': all_users,  # Combined users from all accounts
            'results': results
        }
        
        app.logger.info(f"📤 Sending response with {len(all_users)} users")
        return jsonify(response_data)
        
    except TimeoutError as e:
        signal.alarm(0)  # Cancel timeout
        app.logger.error(f"Automation process timeout after 20 minutes: {e}")
        return jsonify({
            'success': False, 
            'error': f'Process timed out after 20 minutes. Partial results: {authenticated_count} authenticated, {users_retrieved} users retrieved',
            'partial_results': {
                'authenticated_count': authenticated_count,
                'users_retrieved': users_retrieved,
                'processed_count': len(results),
                'results': results
            }
        })
        
    except Exception as e:
        signal.alarm(0)  # Cancel timeout
        import traceback
        error_details = traceback.format_exc()
        app.logger.error(f"Error executing automation process: {e}")
        app.logger.error(f"Full traceback: {error_details}")
        return jsonify({'success': False, 'error': f'Error executing automation process: {str(e)}'})

    finally:
        # Semaphore removed - no cleanup needed
        pass

@app.route('/api/generate-otp', methods=['POST'])
@login_required
def api_generate_otp():
    """Generate OTP with caching to reduce SSH latency. Only remove spaces from secret."""
    try:
        data = request.get_json()
        account_name = data.get('account_name', '').strip()
        
        if not account_name:
            return jsonify({'success': False, 'error': 'Account name is required'})

        # Fast path: serve from cache if available and fresh
        cached_secret = get_cached_otp_secret(account_name)
        if cached_secret:
            totp = pyotp.TOTP(cached_secret)
            otp_code = totp.now()
            return jsonify({
                'success': True,
                'otp_code': otp_code,
                'account_name': account_name,
                'cached': True
            })
        
        # Get SSH Configuration from JSON file
        import json
        import os
        
        config_file = 'otp_ssh_config.json'
        
        if not os.path.exists(config_file):
            return jsonify({
                'success': False, 
                'error': 'OTP SSH configuration not found. Please configure it in Settings first.'
            })
        
        with open(config_file, 'r') as f:
            otp_config = json.load(f)
        
        app.logger.info(f"OTP config from file: {otp_config}")
        
        # Use configured settings
        SSH_CONFIG = {
            "host": otp_config.get('host'),
            "port": otp_config.get('port', 22),
            "user": otp_config.get('username'),
            "pass": otp_config.get('password', ''),
            "auth_method": otp_config.get('auth_method', 'password'),
            "private_key": otp_config.get('private_key', '')
        }
        
        folder_path = f"/home/brightmindscampus/{account_name}"
        
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        
        try:
            # Connect to SSH using configured authentication method
            if SSH_CONFIG.get("auth_method") == "key" and SSH_CONFIG.get("private_key"):
                # Use SSH key authentication
                import tempfile
                import os
                
                with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.key') as key_file:
                    key_file.write(SSH_CONFIG["private_key"])
                    key_file_path = key_file.name
                
                try:
                    ssh.connect(
                        SSH_CONFIG["host"],
                        port=SSH_CONFIG["port"],
                        username=SSH_CONFIG["user"],
                        key_filename=key_file_path,
                        timeout=10,
                        banner_timeout=10,
                        auth_timeout=10
                    )
                finally:
                    # Clean up temporary key file
                    os.unlink(key_file_path)
            else:
                # Use password authentication
                ssh.connect(
                    SSH_CONFIG["host"],
                    port=SSH_CONFIG["port"],
                    username=SSH_CONFIG["user"],
                    password=SSH_CONFIG["pass"],
                    timeout=10,
                    banner_timeout=10,
                    auth_timeout=10
                )
            
            # Read the specific file: {account_name}_authenticator_secret_key.txt
            file_path = f"{folder_path}/{account_name}_authenticator_secret_key.txt"
            cat_cmd = f'cat "{file_path}"'
            stdin, stdout, stderr = ssh.exec_command(cat_cmd)
            content = stdout.read().decode().strip()
            
            if not content:
                raise Exception("No content found")
            
            # Extract key from content
            if ':' in content:
                key = content.split(':')[-1].strip()
            else:
                key = content.strip()
            
            # ONLY REMOVE SPACES - NOTHING ELSE
            secret_key = key.replace(' ', '')

            # Cache secret for subsequent requests
            set_cached_otp_secret(account_name, secret_key)
            
            # Generate OTP
            totp = pyotp.TOTP(secret_key)
            otp_code = totp.now()
            
            return jsonify({
                'success': True,
                'otp_code': otp_code,
                'account_name': account_name,
                'cached': False
            })
            
        except Exception as e:
            return jsonify({'success': False, 'error': str(e)})
        finally:
            ssh.close()
            
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/save-otp-ssh-config', methods=['POST'])
@login_required
def api_save_otp_ssh_config():
    """Save OTP SSH configuration"""
    try:
        data = request.get_json()
        
        # Validate required fields
        required_fields = ['host', 'port', 'username', 'auth_method']
        for field in required_fields:
            if field not in data or not data[field]:
                return jsonify({'success': False, 'error': f'Missing required field: {field}'})
        
        # Validate authentication method specific fields
        if data['auth_method'] == 'password' and not data.get('password'):
            return jsonify({'success': False, 'error': 'Password is required for password authentication'})
        
        if data['auth_method'] == 'key' and not data.get('private_key'):
            return jsonify({'success': False, 'error': 'Private key is required for SSH key authentication'})
        
        # Store configuration in JSON file
        import json
        import os
        
        config_data = {
            'host': data['host'],
            'port': int(data['port']),
            'username': data['username'],
            'auth_method': data['auth_method'],
            'password': data.get('password', ''),
            'private_key': data.get('private_key', ''),
            'configured_at': datetime.now().isoformat()
        }
        
        # Save to JSON file
        config_file = 'otp_ssh_config.json'
        with open(config_file, 'w') as f:
            json.dump(config_data, f, indent=2)
        
        app.logger.info(f"OTP SSH configuration saved to {config_file} for host: {data['host']}")
        
        return jsonify({
            'success': True,
            'message': 'OTP SSH configuration saved successfully'
        })
        
    except Exception as e:
        app.logger.error(f"Error saving OTP SSH config: {e}")
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/test-otp-server-connection', methods=['POST'])
@login_required
def api_test_otp_server_connection():
    """Test OTP server SSH connection"""
    try:
        data = request.get_json()
        
        # Validate required fields
        required_fields = ['host', 'port', 'username', 'auth_method']
        for field in required_fields:
            if field not in data or not data[field]:
                return jsonify({'success': False, 'error': f'Missing required field: {field}'})
        
        # Validate authentication method specific fields
        if data['auth_method'] == 'password' and not data.get('password'):
            return jsonify({'success': False, 'error': 'Password is required for password authentication'})
        
        if data['auth_method'] == 'key' and not data.get('private_key'):
            return jsonify({'success': False, 'error': 'Private key is required for SSH key authentication'})
        
        # Test SSH connection
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        
        try:
            # Connect to SSH
            if data['auth_method'] == 'password':
                ssh.connect(
                    data['host'],
                    port=int(data['port']),
                    username=data['username'],
                    password=data['password']
                )
            else:  # SSH key
                # Create a temporary file for the private key
                import tempfile
                import os
                
                with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.key') as key_file:
                    key_file.write(data['private_key'])
                    key_file_path = key_file.name
                
                try:
                    ssh.connect(
                        data['host'],
                        port=int(data['port']),
                        username=data['username'],
                        key_filename=key_file_path
                    )
                finally:
                    # Clean up temporary key file
                    os.unlink(key_file_path)
            
            # Test if we can access the expected directory structure
            test_cmd = 'ls /home/brightmindscampus 2>/dev/null || echo "Directory not found"'
            stdin, stdout, stderr = ssh.exec_command(test_cmd)
            result = stdout.read().decode().strip()
            
            if "Directory not found" in result:
                return jsonify({
                    'success': False,
                    'error': 'Directory /home/brightmindscampus not found on server'
                })
            
            app.logger.info(f"OTP server connection test successful for host: {data['host']}")
            
            return jsonify({
                'success': True,
                'message': f'Successfully connected to {data["host"]}:{data["port"]} as {data["username"]}'
            })
            
        except Exception as ssh_error:
            app.logger.error(f"SSH connection failed: {ssh_error}")
            return jsonify({
                'success': False,
                'error': f'SSH connection failed: {str(ssh_error)}'
            })
        finally:
            ssh.close()
            
    except Exception as e:
        app.logger.error(f"Error testing OTP server connection: {e}")
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/get-proxy-config', methods=['GET'])
@login_required
def api_get_proxy_config():
    """Get current Proxy configuration"""
    if session.get('role') != 'admin':
        return jsonify({'success': False, 'error': 'Admin privileges required'})
    
    try:
        from database import ProxyConfig
        config = ProxyConfig.query.first()
        if config:
            return jsonify({
                'success': True,
                'config': {
                    'enabled': config.enabled,
                    'proxies': config.proxies if config.proxies else ''
                }
            })
        else:
            return jsonify({'success': True, 'config': {'enabled': False, 'proxies': ''}})
    except Exception as e:
        app.logger.error(f"Error getting proxy config: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/save-proxy-config', methods=['POST'])
@login_required
def api_save_proxy_config():
    """Save Proxy configuration"""
    if session.get('role') != 'admin':
        return jsonify({'success': False, 'error': 'Admin privileges required'})
    
    try:
        data = request.get_json()
        enabled = data.get('enabled', False)
        proxies = data.get('proxies', '').strip()
        
        # Validate proxy format if proxies are provided
        if proxies:
            proxy_lines = [line.strip() for line in proxies.split('\n') if line.strip()]
            proxy_pattern = re.compile(r'^[\d\.]+:\d+:[^:]+:[^:]+$')
            
            for i, line in enumerate(proxy_lines):
                if not proxy_pattern.match(line):
                    return jsonify({
                        'success': False,
                        'error': f'Invalid proxy format on line {i + 1}. Expected: IP:PORT:USERNAME:PASSWORD'
                    })
        
        from database import ProxyConfig
        config = ProxyConfig.query.first()
        
        if config:
            config.enabled = enabled
            config.proxies = proxies
            config.updated_at = datetime.now()
        else:
            config = ProxyConfig(enabled=enabled, proxies=proxies)
            db.session.add(config)
        
        db.session.commit()
        
        return jsonify({
            'success': True,
            'message': 'Proxy configuration saved successfully'
        })
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Error saving proxy config: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/get-twocaptcha-config', methods=['GET'])
@login_required
def api_get_twocaptcha_config():
    """Get current 2Captcha configuration"""
    # Allow admin, mailer, and support to view config
    allowed_roles = ['admin', 'mailer', 'support']
    if session.get('role') not in allowed_roles:
        return jsonify({'success': False, 'error': 'Access denied'})
    
    try:
        from database import TwoCaptchaConfig
        config = TwoCaptchaConfig.query.first()
        if config:
            return jsonify({
                'success': True,
                'config': {
                    'enabled': config.enabled,
                    'api_key': config.api_key if config.api_key else ''
                }
            })
        else:
            return jsonify({'success': True, 'config': {'enabled': False, 'api_key': ''}})
    except Exception as e:
        app.logger.error(f"Error getting 2Captcha config: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/save-twocaptcha-config', methods=['POST'])
@login_required
def api_save_twocaptcha_config():
    """Save 2Captcha configuration"""
    # Allow admin, mailer, and support to save config
    allowed_roles = ['admin', 'mailer', 'support']
    if session.get('role') not in allowed_roles:
        return jsonify({'success': False, 'error': 'Access denied'})
    
    try:
        data = request.get_json()
        enabled = data.get('enabled', False)
        api_key = data.get('api_key', '').strip()
        
        if enabled and not api_key:
            return jsonify({
                'success': False,
                'error': 'API key is required when 2Captcha is enabled'
            })
        
        from database import TwoCaptchaConfig
        config = TwoCaptchaConfig.query.first()
        
        if config:
            config.enabled = enabled
            config.api_key = api_key
            config.updated_at = datetime.now()
        else:
            config = TwoCaptchaConfig(enabled=enabled, api_key=api_key)
            db.session.add(config)
        
        db.session.commit()
        
        return jsonify({
            'success': True,
            'message': '2Captcha configuration saved successfully'
        })
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Error saving 2Captcha config: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/test-twocaptcha', methods=['POST'])
@login_required
def api_test_twocaptcha():
    """Test 2Captcha API key by checking balance"""
    # Allow admin, mailer, and support to test config
    allowed_roles = ['admin', 'mailer', 'support']
    if session.get('role') not in allowed_roles:
        return jsonify({'success': False, 'error': 'Access denied'})
    
    try:
        data = request.get_json()
        api_key = data.get('api_key', '').strip()
        
        if not api_key:
            return jsonify({'success': False, 'error': 'API key is required'})
        
        # Test API key by getting balance using 2Captcha API v2
        # Official 2Captcha API v2 Documentation:
        # POST https://api.2captcha.com/getBalance
        # Reference: https://2captcha.com/api-docs
        import requests
        
        # Validate API key format first (2Captcha keys are typically 32 characters)
        if not api_key or len(api_key) < 20:
            return jsonify({
                'success': False,
                'error': 'API key appears to be invalid (too short). Please check your 2Captcha API key.'
            })
        
        try:
            # Use API v2 getBalance endpoint
            balance_url = 'https://api.2captcha.com/getBalance'
            payload = {
                'clientKey': api_key
            }
            
            app.logger.info(f"[2CAPTCHA TEST] Checking balance using API v2 for API key (length: {len(api_key)})")
            
            # POST request with JSON body (API v2 format)
            response = requests.post(
                balance_url,
                json=payload,
                headers={'Content-Type': 'application/json'},
                timeout=10
            )
            
            app.logger.info(f"[2CAPTCHA TEST] Response status: {response.status_code}")
            app.logger.info(f"[2CAPTCHA TEST] Response text: {response.text[:200]}")
            
            if response.status_code == 200:
                try:
                    result = response.json()
                    
                    # Log full response for debugging
                    app.logger.info(f"[2CAPTCHA TEST] Full API response: {json.dumps(result, indent=2)}")
                    
                    # Check for errors (errorId 0 = success)
                    if result.get('errorId') != 0:
                        error_code = result.get('errorCode', 'Unknown')
                        error_desc = result.get('errorDescription', 'Unknown error')
                        error_id = result.get('errorId', 'Unknown')
                        
                        app.logger.warning(f"[2CAPTCHA TEST] Error received - ErrorId: {error_id}, ErrorCode: {error_code}, ErrorDescription: {error_desc}")
                        app.logger.warning(f"[2CAPTCHA TEST] Full error response: {json.dumps(result, indent=2)}")
                        
                        # Handle specific error codes
                        if error_code in ['ERROR_KEY_DOES_NOT_EXIST', 'ERROR_WRONG_USER_KEY']:
                            return jsonify({
                                'success': False,
                                'error': f'Invalid API key (Error: {error_code}). Please verify your 2Captcha API key is correct and active.',
                                'details': f'Error ID: {error_id}, Code: {error_code}, Description: {error_desc}',
                                'full_response': result
                            })
                        elif error_code == 'ERROR_ZERO_CAPTCHA_FILESIZE':
                            # This is a false positive - it means no CAPTCHA file to check, but API key might be valid
                            # Try API v1 as fallback to verify the key
                            app.logger.info("[2CAPTCHA TEST] ERROR_ZERO_CAPTCHA_FILESIZE detected - trying API v1 fallback...")
                            v1_url = 'http://2captcha.com/res.php'
                            v1_response = requests.get(v1_url, params={'key': api_key, 'action': 'getbalance'}, timeout=10)
                            if v1_response.status_code == 200:
                                v1_result = v1_response.text.strip()
                                app.logger.info(f"[2CAPTCHA TEST] API v1 response: {v1_result}")
                                if not v1_result.startswith('ERROR_'):
                                    try:
                                        balance = float(v1_result)
                                        return jsonify({
                                            'success': True,
                                            'balance': str(balance),
                                            'message': f'API key is valid. Balance: ${balance:.2f} (verified via API v1)',
                                            'note': 'Note: API v2 returned ERROR_ZERO_CAPTCHA_FILESIZE, but API v1 confirmed the key is valid.'
                                        })
                                    except ValueError:
                                        pass
                            
                            # If v1 also fails, return detailed error
                            return jsonify({
                                'success': False,
                                'error': f'API key validation issue (Error: {error_code})',
                                'details': f'Error ID: {error_id}, Code: {error_code}, Description: {error_desc}. This error typically means the API key format is correct but there may be an issue with your account or the API endpoint.',
                                'suggestion': 'Please verify: 1) Your API key is correct, 2) Your 2Captcha account has active balance, 3) Your account is not suspended.',
                                'full_response': result
                            })
                        else:
                            # Generic error with full details
                            return jsonify({
                                'success': False,
                                'error': f'2Captcha API error: {error_desc}',
                                'details': f'Error ID: {error_id}, Error Code: {error_code}, Description: {error_desc}',
                                'full_response': result,
                                'help': 'Check the 2Captcha API documentation for error code meanings: https://2captcha.com/api-docs'
                            })
                    
                    # Extract balance from response
                    balance = result.get('balance')
                    if balance is not None:
                        try:
                            balance_float = float(balance)
                            return jsonify({
                                'success': True,
                                'balance': str(balance_float),
                                'message': f'API key is valid. Balance: ${balance_float:.2f}'
                            })
                        except (ValueError, TypeError):
                            return jsonify({
                                'success': False,
                                'error': f'Invalid balance format: {balance}',
                                'details': f'Received balance value: {balance} (type: {type(balance).__name__})',
                                'full_response': result
                            })
                    else:
                        return jsonify({
                            'success': False,
                            'error': 'No balance in response from 2Captcha API',
                            'details': 'The API response did not contain a balance field.',
                            'full_response': result
                        })
                except json.JSONDecodeError as json_err:
                    app.logger.error(f"[2CAPTCHA TEST] JSON decode error: {json_err}")
                    app.logger.error(f"[2CAPTCHA TEST] Response text: {response.text[:500]}")
                    # Fallback: try API v1 format if v2 fails
                    app.logger.warning("[2CAPTCHA TEST] API v2 JSON parse failed, trying API v1 fallback...")
                    v1_url = 'http://2captcha.com/res.php'
                    v1_response = requests.get(v1_url, params={'key': api_key, 'action': 'getbalance'}, timeout=10)
                    if v1_response.status_code == 200:
                        v1_result = v1_response.text.strip()
                        app.logger.info(f"[2CAPTCHA TEST] API v1 response: {v1_result}")
                        if not v1_result.startswith('ERROR_'):
                            try:
                                balance = float(v1_result)
                                return jsonify({
                                    'success': True,
                                    'balance': str(balance),
                                    'message': f'API key is valid. Balance: ${balance:.2f} (verified via API v1)',
                                    'note': 'Note: API v2 returned invalid JSON, but API v1 confirmed the key is valid.'
                                })
                            except ValueError:
                                pass
                    return jsonify({
                        'success': False,
                        'error': 'Failed to parse response from 2Captcha API',
                        'details': f'JSON decode error: {str(json_err)}. Response text: {response.text[:200]}',
                        'raw_response': response.text[:500]
                    })
            else:
                app.logger.error(f"[2CAPTCHA TEST] HTTP error: Status {response.status_code}, Response: {response.text[:500]}")
                return jsonify({
                    'success': False,
                    'error': f'Failed to connect to 2Captcha API: HTTP {response.status_code}',
                    'details': f'Server returned status code {response.status_code}. Response: {response.text[:200]}',
                    'status_code': response.status_code,
                    'response_preview': response.text[:500]
                })
        except requests.exceptions.Timeout:
            return jsonify({
                'success': False,
                'error': 'Connection timeout. Please check your internet connection and try again.',
                'details': 'The request to 2Captcha API timed out after 10 seconds. This could indicate network issues or the API is temporarily unavailable.',
                'suggestion': 'Please check your internet connection and try again. If the problem persists, 2Captcha API might be experiencing issues.'
            })
        except requests.exceptions.ConnectionError as conn_err:
            app.logger.error(f"[2CAPTCHA TEST] Connection error: {conn_err}")
            return jsonify({
                'success': False,
                'error': 'Failed to connect to 2Captcha API',
                'details': f'Connection error: {str(conn_err)}',
                'suggestion': 'Please check your internet connection and ensure 2captcha.com is accessible.'
            })
        except requests.exceptions.RequestException as e:
            app.logger.error(f"[2CAPTCHA TEST] Network error: {e}")
            import traceback
            app.logger.error(traceback.format_exc())
            return jsonify({
                'success': False,
                'error': f'Network error: {str(e)}',
                'details': f'Request exception occurred: {type(e).__name__}: {str(e)}',
                'suggestion': 'Please check your internet connection and try again.'
            })
    except Exception as e:
        app.logger.error(f"[2CAPTCHA TEST] Unexpected error: {e}")
        import traceback
        app.logger.error(traceback.format_exc())
        return jsonify({
            'success': False,
            'error': f'Unexpected error: {str(e)}',
            'details': f'Error type: {type(e).__name__}, Error message: {str(e)}',
            'traceback': traceback.format_exc()[:500]  # First 500 chars of traceback
        })

@app.route('/api/get-otp-ssh-config', methods=['GET'])
@login_required
def api_get_otp_ssh_config():
    """Get current OTP SSH configuration"""
    try:
        import json
        import os
        
        config_file = 'otp_ssh_config.json'
        
        if not os.path.exists(config_file):
            return jsonify({
                'success': True,
                'config': None,
                'message': 'No OTP SSH configuration found'
            })
        
        with open(config_file, 'r') as f:
            config_data = json.load(f)
        
        # Don't return sensitive data
        safe_config = {
            'host': config_data.get('host', ''),
            'port': config_data.get('port', 22),
            'username': config_data.get('username', ''),
            'auth_method': config_data.get('auth_method', 'password'),
            'configured_at': config_data.get('configured_at', '')
        }
        
        return jsonify({
            'success': True,
            'config': safe_config
        })
        
    except Exception as e:
        app.logger.error(f"Error getting OTP SSH config: {e}")
        return jsonify({'success': False, 'error': str(e)})


@app.route('/api/mark-used-domains', methods=['POST'])
@login_required
def api_mark_used_domains():
    """Automatically mark all domains that are currently in use as used"""
    # FEATURE DISABLED BY USER REQUEST
    return jsonify({
        'success': True,
        'message': 'Feature disabled by administrator request.',
        'stats': {'updated': 0, 'created': 0, 'total_used': 0, 'total_available': 0}
    })
    
    # try:
    #     # Get all Google accounts to find domains that are actually being used
    #     accounts = GoogleAccount.query.all()
    # ... (rest of logic disabled)

@app.route('/api/change-subdomain-status', methods=['POST'])
@login_required
def api_change_subdomain_status():
    """Change the status of a subdomain"""
    try:
        data = request.get_json()
        subdomain = data.get('subdomain', '').strip()
        status = data.get('status', '').strip()
        
        if not subdomain:
            return jsonify({'success': False, 'error': 'Subdomain is required'})
        
        if status not in ['available', 'in_use', 'used']:
            return jsonify({'success': False, 'error': 'Invalid status. Must be: available, in_use, or used'})
        
        # Use the existing UsedDomain table structure
        from database import UsedDomain
        
        # Check if subdomain exists, create if it doesn't
        domain_record = UsedDomain.query.filter_by(domain_name=subdomain).first()
        
        if not domain_record:
            try:
                # Use PostgreSQL UPSERT (INSERT ... ON CONFLICT) for atomic operation
                from sqlalchemy import text
                
                # First try to insert, if it fails due to duplicate, then fetch existing
                insert_sql = text("""
                    INSERT INTO used_domain (domain_name, user_count, is_verified, ever_used, created_at, updated_at)
                    VALUES (:domain_name, :user_count, :is_verified, :ever_used, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    ON CONFLICT (domain_name) DO NOTHING
                """)
                
                result = db.session.execute(insert_sql, {
                    'domain_name': subdomain,
                    'user_count': 0,
                    'is_verified': True,
                    'ever_used': False
                })
                
                # If no row was inserted (conflict), fetch the existing record
                if result.rowcount == 0:
                    domain_record = UsedDomain.query.filter_by(domain_name=subdomain).first()
                else:
                    # Get the newly created record
                    domain_record = UsedDomain.query.filter_by(domain_name=subdomain).first()
                
                if not domain_record:
                    return jsonify({'success': False, 'error': f'Failed to create or find domain record for {subdomain}'})
                    
            except Exception as e:
                # Rollback any failed transaction before trying fallback
                db.session.rollback()
                app.logger.warning(f"UPSERT failed for {subdomain}, using fallback method: {e}")
                
                # Check if this is a sequence issue (primary key conflict)
                if 'duplicate key value violates unique constraint "used_domain_pkey"' in str(e):
                    app.logger.error(f"❌ Sequence issue detected for {subdomain}. The used_domain_id_seq is out of sync.")
                    app.logger.error(f"Please run: python fix_used_domain_sequence.py --all")
                    return jsonify({
                        'success': False, 
                        'error': f'Database sequence error for {subdomain}. Please contact support or run the sequence fix script.',
                        'sequence_error': True
                    })
                
                # Check if this is a domain name conflict
                if 'duplicate key' in str(e).lower() or 'unique constraint' in str(e).lower():
                    # Try to fetch existing record first
                    domain_record = UsedDomain.query.filter_by(domain_name=subdomain).first()
                    if domain_record:
                        app.logger.info(f"Found existing domain record for {subdomain}")
                    else:
                        app.logger.error(f"Domain conflict but no existing record found for {subdomain}")
                        return jsonify({'success': False, 'error': f'Domain conflict detected for {subdomain}'})
                else:
                    # For other errors, try to create the record
                    try:
                        domain_record = UsedDomain.query.filter_by(domain_name=subdomain).first()
                        
                        if not domain_record:
                            # Create new record if it doesn't exist
                            domain_record = UsedDomain(
                                domain_name=subdomain,
                                user_count=0,
                                is_verified=True,
                                ever_used=False
                            )
                            db.session.add(domain_record)
                            db.session.flush()
                            
                    except Exception as e2:
                        # Handle duplicate key violation - another process might have created it
                        if 'duplicate key' in str(e2).lower() or 'unique constraint' in str(e2).lower():
                            db.session.rollback()
                            # Try to fetch the record again
                            domain_record = UsedDomain.query.filter_by(domain_name=subdomain).first()
                            if not domain_record:
                                return jsonify({'success': False, 'error': f'Failed to create domain record for {subdomain}'})
                        else:
                            db.session.rollback()
                            raise e2
        
        # Determine current status
        current_status = 'available'
        if domain_record.user_count > 0:
            current_status = 'in_use'
        elif domain_record.ever_used:
            current_status = 'used'
        
        # Update the status based on the requested status
        if status == 'available':
            domain_record.user_count = 0
            domain_record.ever_used = False
        elif status == 'in_use':
            domain_record.user_count = 1  # Set to 1 to indicate in use
            domain_record.ever_used = True
        elif status == 'used':
            domain_record.user_count = 0
            domain_record.ever_used = True
        
        domain_record.updated_at = db.func.current_timestamp()
        
        db.session.commit()
        
        app.logger.info(f"Subdomain '{subdomain}' status changed from '{current_status}' to '{status}'")
        
        return jsonify({
            'success': True,
            'message': f'Subdomain "{subdomain}" status changed from "{current_status}" to "{status}"',
            'subdomain': subdomain,
            'old_status': current_status,
            'new_status': status
        })
        
    except Exception as e:
        app.logger.error(f"Error changing subdomain status: {e}")
        db.session.rollback()
        return jsonify({'success': False, 'error': str(e)})



@app.route('/api/get-cloudflare-config', methods=['GET'])
@login_required
def api_get_cloudflare_config():
    """Get current Cloudflare configuration"""
    # Allow admin, mailer, and support to access Cloudflare config
    allowed_roles = ['admin', 'mailer', 'support']
    if session.get('role') not in allowed_roles:
        return jsonify({'success': False, 'error': 'Access denied'})
    
    try:
        from database import CloudflareConfig
        config = CloudflareConfig.query.filter_by(is_configured=True).first()
        
        if config:
            return jsonify({
                'success': True,
                'config': {
                    'api_token': config.api_token,
                    'email': config.email,
                    'is_configured': True
                }
            })
        else:
            return jsonify({
                'success': True, 
                'config': {
                    'api_token': '',
                    'email': '',
                    'is_configured': False
                }
            })
    except Exception as e:
        app.logger.error(f"Error getting Cloudflare config: {e}")
        return jsonify({'success': False, 'error': str(e)})

@app.route('/api/save-cloudflare-config', methods=['POST'])
@login_required
def api_save_cloudflare_config():
    """Save Cloudflare configuration"""
    # Allow admin, mailer, and support to save Cloudflare config
    allowed_roles = ['admin', 'mailer', 'support']
    if session.get('role') not in allowed_roles:
        return jsonify({'success': False, 'error': 'Access denied'})
    
    try:
        data = request.get_json()
        api_token = data.get('api_token', '').strip()
        email = data.get('email', '').strip()
        
        if not api_token:
            return jsonify({'success': False, 'error': 'API Token is required'})
            
        # Email is optional for API Tokens but good to have
        
        from database import CloudflareConfig
        config = CloudflareConfig.query.first()
        
        if config:
            config.api_token = api_token
            config.email = email
            config.is_configured = True
            config.updated_at = datetime.now()
        else:
            config = CloudflareConfig(
                api_token=api_token,
                email=email,
                is_configured=True
            )
            db.session.add(config)
        
        db.session.commit()
        
        app.logger.info(f"Cloudflare configuration saved by {session.get('user')}")
        
        return jsonify({
            'success': True,
            'message': 'Cloudflare configuration saved successfully'
        })
        
    except Exception as e:
        app.logger.error(f"Error saving Cloudflare config: {e}")
        return jsonify({'success': False, 'error': str(e)})

# DEBUG ENDPOINT - View all UsedDomain records
@app.route('/api/debug/used-domains', methods=['GET'])
@login_required
def api_debug_used_domains():
    """Debug endpoint to view all UsedDomain records in the database"""
    try:
        from database import UsedDomain
        all_domains = UsedDomain.query.all()
        result = []
        for d in all_domains:
            result.append({
                'domain_name': d.domain_name,
                'ever_used': getattr(d, 'ever_used', None),
                'is_verified': getattr(d, 'is_verified', None),
                'user_count': getattr(d, 'user_count', None),
                'created_at': str(getattr(d, 'created_at', None)),
                'updated_at': str(getattr(d, 'updated_at', None))
            })
        return jsonify({
            'success': True,
            'count': len(result),
            'domains': result
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

if __name__ == '__main__':
    app.run(debug=True)

# Force Reload: v2.5.1


# Force Reload: v2.6 - Fixed MAX_USERS_PER_BATCH from 10 to 50

