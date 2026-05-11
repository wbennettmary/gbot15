"""
DigitalOcean Management routes for droplet creation, snapshot management,
and automation execution.
"""
import os
import json
import uuid
import logging
import threading
from datetime import datetime
from flask import Blueprint, render_template, request, jsonify, current_app, session, render_template
from functools import wraps
from database import db, DigitalOceanConfig, DigitalOceanDroplet, DigitalOceanExecution, AwsGeneratedPassword
from services.digitalocean_service import DigitalOceanService

logger = logging.getLogger(__name__)

digitalocean_manager = Blueprint('digitalocean_manager', __name__)


# Login required decorator
def login_required(f):
    """Decorator to require login"""
    from flask import redirect, url_for
    @wraps(f)
    def wrapper(*args, **kwargs):
        if 'user' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return wrapper


def get_current_username():
    """Get current logged-in user's username"""
    username = session.get('user')
    if username:
        return username.split('@')[0].lower()
    return None


@digitalocean_manager.route('/digitalocean')
@login_required
def digitalocean_management():
    """DigitalOcean Management page"""
    return render_template('digitalocean_management.html', user=session.get('user'), role=session.get('role'))


# Configuration Routes
@digitalocean_manager.route('/api/do/test-connection', methods=['POST'])
@login_required
def test_connection():
    """Test DigitalOcean API connection"""
    try:
        data = request.get_json()
        api_token = data.get('api_token', '').strip()
        
        # If no token provided, try to use stored token
        if not api_token:
            config = DigitalOceanConfig.query.first()
            if config and config.api_token:
                api_token = config.api_token
            else:
                return jsonify({'success': False, 'error': 'API token is required'}), 400
        
        service = DigitalOceanService(api_token)
        account = service.get_account()
        
        if account:
            return jsonify({
                'success': True, 
                'message': f"Connected to account: {account.get('email', 'Unknown')}"
            })
        else:
            return jsonify({'success': False, 'error': 'Invalid API token'}), 400
    except Exception as e:
        logger.error(f"Test connection error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@digitalocean_manager.route('/api/do/config', methods=['GET'])
@login_required
def get_config():
    """Get DigitalOcean configuration"""
    try:
        config = DigitalOceanConfig.query.first()
        
        if not config:
            return jsonify({'success': True, 'config': None})
        
        return jsonify({
            'success': True,
            'config': {
                'id': config.id,
                'name': config.name,
                'api_token_masked': f"{config.api_token[:4]}***" if config.api_token else "",
                'default_region': config.default_region,
                'default_size': config.default_size,
                'automation_snapshot_id': config.automation_snapshot_id,
                'ssh_key_id': config.ssh_key_id,
                'auto_destroy_droplets': config.auto_destroy_droplets,
                'parallel_users': config.parallel_users,
                'users_per_droplet': config.users_per_droplet,
                'is_configured': config.is_configured
            }
        })
    except Exception as e:
        logger.error(f"Get config error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@digitalocean_manager.route('/api/do/config', methods=['POST'])
@login_required
def save_config():
    """Save DigitalOcean configuration"""
    try:
        data = request.get_json()
        api_token = data.get('api_token', '').strip()
        
        config = DigitalOceanConfig.query.first()
        
        # Validate token requirement
        if not config and not api_token:
            # New configuration requires token
            return jsonify({'success': False, 'error': 'API token is required for first-time setup'}), 400
        
        if not config:
            config = DigitalOceanConfig()
            db.session.add(config)
        
        config.name = data.get('name', 'Default DigitalOcean Account').strip()
        
        # Only update token if provided
        if api_token:
            config.api_token = api_token
        
        config.default_region = data.get('default_region', 'nyc3').strip()
        config.default_size = data.get('default_size', 's-1vcpu-1gb').strip()
        config.automation_snapshot_id = data.get('automation_snapshot_id', '').strip() or None
        config.ssh_key_id = data.get('ssh_key_id', '').strip() or None
        config.auto_destroy_droplets = data.get('auto_destroy_droplets', True)
        config.parallel_users = data.get('parallel_users', 5)
        config.users_per_droplet = data.get('users_per_droplet', 50)
        
        # Handle SSH private key
        ssh_private_key = data.get('ssh_private_key', '').strip()
        if ssh_private_key:
            import os
            
            # Save to a persistent file in the app directory
            # Use 'digitalocean_key.pem' in the instance folder or root
            # Creating in root for simplicity as per user setup
            key_path = os.path.abspath('digitalocean_key.pem')
            
            with open(key_path, 'w') as f:
                f.write(ssh_private_key)
            
            # Set restrictive permissions (best effort on Windows)
            try:
                os.chmod(key_path, 0o600)
            except:
                pass
            
            config.ssh_private_key_path = key_path
            logger.info(f"Saved SSH private key to {key_path}")
        
        config.is_configured = True
        
        db.session.commit()
        
        logger.info(f"DigitalOcean config saved: {config.name}")
        return jsonify({'success': True, 'message': 'Configuration saved successfully'})
    except Exception as e:
        db.session.rollback()
        logger.error(f"Save config error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


# Region and Size Routes
@digitalocean_manager.route('/api/do/regions', methods=['GET'])
@login_required
def list_regions():
    """List available DigitalOcean regions"""
    try:
        config = DigitalOceanConfig.query.first()
        if not config or not config.api_token:
            return jsonify({'success': False, 'error': 'DigitalOcean not configured'}), 400
        
        service = DigitalOceanService(config.api_token)
        regions = service.list_regions()
        
        return jsonify({'success': True, 'regions': regions})
    except Exception as e:
        logger.error(f"List regions error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@digitalocean_manager.route('/api/do/sizes', methods=['GET'])
@login_required
def list_sizes():
    """List available droplet sizes"""
    try:
        config = DigitalOceanConfig.query.first()
        if not config or not config.api_token:
            return jsonify({'success': False, 'error': 'DigitalOcean not configured'}), 400
        
        service = DigitalOceanService(config.api_token)
        sizes = service.list_sizes()
        
        return jsonify({'success': True, 'sizes': sizes})
    except Exception as e:
        logger.error(f"List sizes error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@digitalocean_manager.route('/api/do/regions-sizes', methods=['GET'])
@login_required
def list_regions_and_sizes():
    """List both regions and sizes in a single call for efficiency"""
    try:
        config = DigitalOceanConfig.query.first()
        if not config or not config.api_token:
            return jsonify({'success': False, 'error': 'DigitalOcean not configured'}), 400
        
        service = DigitalOceanService(config.api_token)
        
        # Get both regions and sizes
        regions = service.list_regions()
        sizes = service.list_sizes()
        
        return jsonify({
            'success': True,
            'regions': regions,
            'sizes': sizes
        })
    except Exception as e:
        logger.error(f"List regions/sizes error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


# Droplet Routes
@digitalocean_manager.route('/api/do/droplets', methods=['GET'])
@login_required
def list_droplets():
    """List all droplets"""
    try:
        config = DigitalOceanConfig.query.first()
        if not config or not config.api_token:
            return jsonify({'success': False, 'error': 'DigitalOcean not configured'}), 400
       
        service = DigitalOceanService(config.api_token)
        droplets = service.list_droplets()
        
        # Also get droplets from database for tracking info
        db_droplets = {d.droplet_id: d for d in DigitalOceanDroplet.query.all()}
        
        # Merge data
        for droplet in droplets:
            db_droplet = db_droplets.get(droplet['id'])
            if db_droplet:
                droplet['assigned_users_count'] = db_droplet.assigned_users_count
                droplet['execution_task_id'] = db_droplet.execution_task_id
                droplet['auto_destroy'] = db_droplet.auto_destroy
        
        return jsonify({'success': True, 'droplets': droplets})
    except Exception as e:
        logger.error(f"List droplets error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@digitalocean_manager.route('/api/do/droplets/<droplet_id>', methods=['DELETE'])
@login_required
def delete_droplet(droplet_id):
    """Delete a droplet"""
    try:
        logger.info(f"API Request to delete droplet ID: {droplet_id}")
        config = DigitalOceanConfig.query.first()
        if not config or not config.api_token:
            return jsonify({'success': False, 'error': 'DigitalOcean not configured'}), 400
        
        service = DigitalOceanService(config.api_token)
        # Ensure droplet_id is stripped and valid
        clean_id = str(droplet_id).strip()
        
        success = service.delete_droplet(clean_id)
        
        if success:
            # Update database
            db_droplet = DigitalOceanDroplet.query.filter_by(droplet_id=clean_id).first()
            if db_droplet:
                db_droplet.status = 'destroyed'
                db_droplet.destroyed_at = datetime.utcnow()
                db.session.commit()
                logger.info(f"Droplet {clean_id} marked as destroyed in database.")
            else:
                logger.warning(f"Droplet {clean_id} deleted from DO but not found in local database.")
            
            return jsonify({'success': True, 'message': 'Droplet deleted successfully'})
        else:
            logger.error(f"DigitalOceanService failed to delete droplet {clean_id}")
            return jsonify({'success': False, 'error': 'Failed to delete droplet via DigitalOcean API'}), 500
    except Exception as e:
        logger.error(f"Delete droplet exception for ID {droplet_id}: {traceback.format_exc()}")
        return jsonify({'success': False, 'error': str(e)}), 500


@digitalocean_manager.route('/api/do/droplets/create', methods=['POST'])
@login_required
def create_droplet():
    """Create a new droplet"""
    try:
        data = request.get_json()
        name = (data.get('name') or '').strip()
        region = (data.get('region') or '').strip()
        size = (data.get('size') or '').strip()
        image = data.get('image')  # Optional
        ssh_key = (data.get('ssh_key') or '').strip()
        
        if not name or not region or not size:
            return jsonify({'success': False, 'error': 'Name, region, and size are required'}), 400
        
        config = DigitalOceanConfig.query.first()
        if not config or not config.api_token:
            return jsonify({'success': False, 'error': 'DigitalOcean not configured. Please configure in Settings first.'}), 400
        
        # Get current username for droplet naming
        username = get_current_username() or 'user'
        
        # Create droplet name with username if not already included
        if username not in name.lower():
            full_name = f"{name}-{username}"
        else:
            full_name = name
            
        # Sanitize name to contain only valid hostname characters (a-z, A-Z, 0-9, . and -)
        import re
        full_name = re.sub(r'[^a-zA-Z0-9.-]', '-', full_name)
        # Remove consecutive hyphens and leading/trailing special chars
        full_name = re.sub(r'-+', '-', full_name).strip('.-')
        
        service = DigitalOceanService(config.api_token)
        
        # Use Ubuntu 22.04 as default image if not specified
        if not image:
            image = 'ubuntu-22-04-x64'
        
        # Create cloud-init script to inject local files directly
        # This avoids git clone issues and ensures local changes are applied immediately
        
        # Read setup_droplet.sh
        setup_script_path = os.path.join(current_app.root_path, 'repo_digitalocean_files', 'setup_droplet.sh')
        try:
            with open(setup_script_path, 'r', encoding='utf-8') as f:
                setup_script_content = f.read()
        except Exception as e:
            logger.error(f"Failed to read setup_droplet.sh: {e}")
            return jsonify({'success': False, 'error': f'Failed to read setup script: {e}'}), 500

        # Construct the User Data script (Bash)
        # We use a heredoc with quoted delimiter 'EOF' to prevent variable expansion during creation.
        # We will use a unique delimiter to avoid conflicts.
        DELIM = "GBOT_FILE_DELIMITER_EOF_123456789"
        
        # NOTE: do_automation.py is NOT injected here because it exceeds the 64KB User Data limit.
        # It is uploaded automatically by run_automation_script() via SFTP when needed.
        
        cloud_init_script = f"""#!/bin/bash
# Auto-generated Cloud-Init for GBot
# Injected local setup_droplet.sh directly

# 1. Create directory
mkdir -p /opt/automation

# 2. Write setup script
cat > /tmp/setup_droplet.sh << '{DELIM}'
{setup_script_content}
{DELIM}
chmod +x /tmp/setup_droplet.sh

# 3. Run setup script
echo "Running injected setup script..."
bash /tmp/setup_droplet.sh
"""
        
        # Convert SSH key string to list if provided
        ssh_keys_list = []
        
        # PRIORITY: Look for SSH key named 'Default' in DigitalOcean account
        try:
            default_key = service.get_ssh_key_by_name('Default')
            if default_key:
                ssh_keys_list.append(default_key['id'])
                logger.info(f"Found and using 'Default' SSH key ID: {default_key['id']}")
            else:
                logger.warning("SSH key named 'Default' not found in DigitalOcean account.")
                # Fallback to configured key if 'Default' not found
                if config.ssh_key_id:
                    ssh_keys_list.append(int(config.ssh_key_id) if str(config.ssh_key_id).isdigit() else config.ssh_key_id)
        except Exception as e:
            logger.error(f"Error looking up 'Default' SSH key: {e}")
            # Fallback on error
            if config.ssh_key_id:
                ssh_keys_list.append(int(config.ssh_key_id) if str(config.ssh_key_id).isdigit() else config.ssh_key_id)

        if not ssh_keys_list:
            ssh_keys_list = None
            logger.warning("No SSH keys found for droplet creation")
            ssh_keys_list = None
            logger.warning("No SSH keys found for droplet creation")
            
        # Get root password if provided
        root_password = (data.get('root_password') or '').strip()
        
        # Validation: If no password provided (SSH mode), we MUST have keys
        if not root_password and not ssh_keys_list:
            return jsonify({
                'success': False, 
                'error': 'No SSH keys configured in Settings. Please add an SSH key ID in Settings or choose Password authentication.'
            }), 400
            
        # If password provided, pre-configure SSHD to allow password auth IMMEDIATELY
        if root_password:
            # We prepend this to ensure it runs before any long apt-get/git operations
            password_setup = f"""
# Ensure PasswordAuthentication is enabled immediately
echo "root:{root_password}" | chpasswd
# User-provided robust configuration
sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin yes/' /etc/ssh/sshd_config
sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication yes/' /etc/ssh/sshd_config
# Also ensure host key generation happens if it hasn't
ssh-keygen -A
systemctl restart sshd
"""
            # Insert after shebang
            if cloud_init_script.startswith('#!/bin/bash'):
                cloud_init_script = cloud_init_script.replace('#!/bin/bash', f'#!/bin/bash\n{password_setup}', 1)
            else:
                cloud_init_script = f'#!/bin/bash\n{password_setup}\n{cloud_init_script}'
                
            logger.info(f"Added password auth configuration for droplet {full_name}")
        
        result, error_msg = service.create_droplet(
            name=full_name,
            region=region,
            size=size,
            image=image,
            ssh_keys=ssh_keys_list,
            user_data=cloud_init_script,  # Auto-setup via cloud-init
            root_password=root_password   # pass password to API
        )
        
        if result and 'id' in result:
            droplet_id = result['id']
            
            # Wait for droplet to be active and get IP
            ip_address = service.wait_for_droplet_active(droplet_id, timeout=300)
            
            if ip_address:
                # Store in database
                db_droplet = DigitalOceanDroplet()
                db_droplet.droplet_id = str(droplet_id)
                db_droplet.droplet_name = full_name
                db_droplet.region = region
                db_droplet.size = size
                db_droplet.ip_address = ip_address
                db_droplet.status = 'active'
                db_droplet.created_by_username = username
                db_droplet.auto_destroy = config.auto_destroy_droplets
                
                db.session.add(db_droplet)
                db.session.commit()
                
                logger.info(f"Droplet created with auto-setup: {full_name} ({droplet_id}) by {username}")
                
                return jsonify({
                    'success': True,
                    'message': 'Droplet created successfully with auto-setup from GitHub',
                    'droplet_id': droplet_id,
                    'name': full_name,
                    'ip_address': ip_address,
                    'note': 'Cloud-init is running setup script. Wait 5-10 minutes before creating snapshot.'
                })
            else:
                return jsonify({'success': False, 'error': 'Droplet created but did not become active'}), 500
        else:
            return jsonify({'success': False, 'error': f"Failed to create droplet: {error_msg}"}), 500
            
    except Exception as e:
        db.session.rollback()
        logger.error(f"Create droplet error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@digitalocean_manager.route('/api/do/droplets/<droplet_id>/snapshot', methods=['POST'])
@login_required
def create_droplet_snapshot(droplet_id):
    """Create a snapshot from a specific droplet"""
    try:
        data = request.get_json()
        snapshot_name = data.get('name', '').strip()
        
        if not snapshot_name:
            return jsonify({'success': False, 'error': 'Snapshot name is required'}), 400
        
        config = DigitalOceanConfig.query.first()
        if not config or not config.api_token:
            return jsonify({'success': False, 'error': 'DigitalOcean not configured'}), 400
        
        service = DigitalOceanService(config.api_token)
        result = service.create_snapshot(droplet_id, snapshot_name)
        
        if result and result.get('action_id'):
            # Snapshot creation is async, return action ID
            action_id = result['action_id']
            logger.info(f"Snapshot creation started: {snapshot_name} (Action ID: {action_id}) from droplet {droplet_id}")
            
            return jsonify({
                'success': True,
                'message': 'Snapshot creation started',
                'snapshot_id': 'Running...', # Placeholder until complete
                'action_id': action_id
            })
        else:
            return jsonify({'success': False, 'error': 'Failed to start snapshot creation'}), 500
            
    except Exception as e:
        logger.error(f"Create snapshot error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@digitalocean_manager.route('/api/do/droplets/<droplet_id>/test-automation', methods=['POST'])
@login_required
def test_droplet_automation(droplet_id):
    """Run automation script on a single droplet for testing"""
    try:
        data = request.json
        email = data.get('email')
        password = data.get('password')
        
        if not email or not password:
            return jsonify({'success': False, 'error': 'Email and password are required'}), 400
            
        config = DigitalOceanConfig.query.first()
        if not config or not config.api_token:
            return jsonify({'success': False, 'error': 'DigitalOcean not configured'}), 400
            
        service = DigitalOceanService(config.api_token)
        
        # Get droplet info to check status and get IP
        droplet = service.get_droplet(droplet_id)
        if not droplet:
            return jsonify({'success': False, 'error': 'Droplet not found'}), 404
            
        if droplet['status'] != 'active':
            return jsonify({'success': False, 'error': f"Droplet is not active (status: {droplet['status']})"}), 400
            
        ip_address = droplet.get('ip_address')
        if not ip_address:
            return jsonify({'success': False, 'error': 'Droplet has no IP address'}), 400
            
        # 1. Get SSH key path (Robust resolution)
        ssh_key_path = None
        if config.ssh_private_key_path and os.path.exists(config.ssh_private_key_path):
            ssh_key_path = config.ssh_private_key_path
        elif os.path.exists(os.path.abspath("digitalocean_key.pem")):
            ssh_key_path = os.path.abspath("digitalocean_key.pem")
        elif os.path.exists("edu-gw-creation-key.pem"):
             ssh_key_path = os.path.abspath("edu-gw-creation-key.pem")
             
        if not ssh_key_path:
             return jsonify({'success': False, 'error': 'SSH key not found on server. Please save settings again.'}), 500

        # 2. Check if setup is complete
        # We need to verify the droplet is actually ready before we try to run the automation
        check_cmd = "[ -f /opt/automation/setup_complete ] && echo 'yes' || echo 'no'"
        success, stdout, stderr = service.execute_ssh_command(
            ip_address=ip_address,
            command=check_cmd,
            username='root',
            ssh_key_path=ssh_key_path
        )
        
        if not success:
             return jsonify({'success': False, 'error': f'Failed to check droplet status: {stderr}'}), 500
             
        if stdout.strip() != 'yes':
             return jsonify({'success': False, 'error': 'Droplet is still setting up. Please wait for "Setup complete!" in the logs.'}), 400

        # 3. Run automation (Async Start)
        result = service.start_automation_script(
            ip_address=ip_address,
            email=email,
            password=password,
            ssh_key_path=ssh_key_path
        )
        
        if result.get('success'):
            return jsonify({
                'success': True, 
                'message': 'Automation started', 
                'status': 'running',
                'log_file': result.get('log_file'),
                'result_file': result.get('result_file')
            })
        else:
             return jsonify({'success': False, 'error': result.get('error', 'Failed to start automation')}), 500

    except Exception as e:
        logger.error(f"Test automation error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@digitalocean_manager.route('/api/do/droplets/<droplet_id>/automation-status', methods=['POST'])
@login_required
def get_automation_status(droplet_id):
    """Check status of running automation"""
    try:
        data = request.json
        log_file = data.get('log_file')
        result_file = data.get('result_file')
        
        if not log_file or not result_file:
            return jsonify({'success': False, 'error': 'Log file and result file paths required'}), 400
            
        config = DigitalOceanConfig.query.first()
        if not config or not config.api_token:
            return jsonify({'success': False, 'error': 'DigitalOcean not configured'}), 400
            
        service = DigitalOceanService(config.api_token)
        droplet = service.get_droplet(droplet_id)
        if not droplet or not droplet.get('ip_address'):
             return jsonify({'success': False, 'error': 'Droplet not found or no IP'}), 404
             
        # Resolve SSH key
        ssh_key_path = None
        if config.ssh_private_key_path and os.path.exists(config.ssh_private_key_path):
            ssh_key_path = config.ssh_private_key_path
        elif os.path.exists(os.path.abspath("digitalocean_key.pem")):
            ssh_key_path = os.path.abspath("digitalocean_key.pem")
        elif os.path.exists("edu-gw-creation-key.pem"):
             ssh_key_path = os.path.abspath("edu-gw-creation-key.pem")
             
        status_result = service.check_automation_status(
            ip_address=droplet['ip_address'],
            log_file=log_file,
            result_file=result_file,
            ssh_key_path=ssh_key_path
        )

        # Save to DB if success and app_password present
        if status_result.get('status') == 'completed' and status_result.get('result', {}).get('status') == 'success':
            try:
                result_data = status_result.get('result', {})
                email = result_data.get('email')
                app_password = result_data.get('app_password')
                
                if email and app_password:
                    # Check if exists
                    existing = AwsGeneratedPassword.query.filter_by(email=email).first()
                    if existing:
                        existing.app_password = app_password
                        existing.updated_at = datetime.utcnow()
                    else:
                        new_pwd = AwsGeneratedPassword(email=email, app_password=app_password)
                        db.session.add(new_pwd)
                    
                    db.session.commit()
                    logger.info(f"Saved app password for {email} to DB")
            except Exception as db_e:
                logger.error(f"Failed to save credential to DB: {db_e}")
        
        return jsonify({'success': True, 'data': status_result})
        
    except Exception as e:
        logger.error(f"Automation status check error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


# Snapshot Routes
@digitalocean_manager.route('/api/do/snapshots', methods=['GET'])
@login_required
def list_snapshots():
    """List all snapshots"""
    try:
        config = DigitalOceanConfig.query.first()
        if not config or not config.api_token:
            return jsonify({'success': False, 'error': 'DigitalOcean not configured'}), 400
        
        service = DigitalOceanService(config.api_token)
        snapshots = service.list_snapshots()
        
        return jsonify({'success': True, 'snapshots': snapshots})
    except Exception as e:
        logger.error(f"List snapshots error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@digitalocean_manager.route('/api/do/snapshots/create', methods=['POST'])
@login_required
def create_snapshot():
    """Create a snapshot from a droplet"""
    try:
        data = request.get_json()
        droplet_id = data.get('droplet_id')
        snapshot_name = data.get('snapshot_name')
        
        if not droplet_id or not snapshot_name:
            return jsonify({'success': False, 'error': 'Droplet ID and snapshot name are required'}), 400
        
        config = DigitalOceanConfig.query.first()
        if not config or not config.api_token:
            return jsonify({'success': False, 'error': 'DigitalOcean not configured'}), 400
        
        service = DigitalOceanService(config.api_token)
        result = service.create_snapshot(droplet_id, snapshot_name)
        
        if result:
            return jsonify({'success': True, 'message': 'Snapshot creation started', 'action': result})
        else:
            return jsonify({'success': False, 'error': 'Failed to create snapshot'}), 500
    except Exception as e:
        logger.error(f"Create snapshot error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@digitalocean_manager.route('/api/do/droplets/<droplet_id>/setup-logs', methods=['GET'])
@login_required
def get_droplet_setup_logs(droplet_id):
    """Get the cloud-init setup logs from the droplet"""
    try:
        config = DigitalOceanConfig.query.first()
        if not config or not config.api_token:
            return jsonify({'success': False, 'error': 'DigitalOcean not configured'}), 400
        
        service = DigitalOceanService(config.api_token)
        droplet = service.get_droplet(droplet_id)
        
        if not droplet:
            return jsonify({'success': False, 'error': 'Droplet not found'}), 404
            
        ip_address = droplet.get('ip_address')
        
        if not ip_address:
            return jsonify({'success': False, 'error': 'Droplet has no IP address yet'}), 400
            
        # Get SSH key path
        ssh_key_path = None
        
        # 1. Try configured path
        if config.ssh_private_key_path and os.path.exists(config.ssh_private_key_path):
            ssh_key_path = config.ssh_private_key_path
        # 2. Try default/fallback persistent key
        elif os.path.exists(os.path.abspath("digitalocean_key.pem")):
            ssh_key_path = os.path.abspath("digitalocean_key.pem")
        # 3. Try legacy key
        elif os.path.exists("edu-gw-creation-key.pem"):
             ssh_key_path = os.path.abspath("edu-gw-creation-key.pem")
             
        if not ssh_key_path:
             # Just for debugging, print what we tried
             logger.error(f"SSH Key missing. Config path: {config.ssh_private_key_path} (Exists: {os.path.exists(config.ssh_private_key_path) if config.ssh_private_key_path else 'N/A'})")
             return jsonify({'success': False, 'error': 'SSH key not found on server'}), 500

        # Command to read the log file
        # We use strict host key checking=no to avoid interactive prompts
        # We read /var/log/cloud-init-output.log which contains stdout/stderr of user-data script
        cmd = "cat /var/log/cloud-init-output.log"
        
        success, stdout, stderr = service.execute_ssh_command(
            ip_address=ip_address,
            command=cmd,
            username='root',
            ssh_key_path=ssh_key_path
        )
        
        # Filter logs to show only user setup process if it has started
        marker = "===== DigitalOcean"
        if success and marker in stdout:
             # Keep the marker and everything after it
             stdout = marker + stdout.split(marker, 1)[1]
        elif success:
             # If setup hasn't started, show a waiting message or the last few lines of boot
             stdout = "Waiting for setup script to start...\n" + "\n".join(stdout.splitlines()[-5:])

        # Check completion status
        is_complete = False
        if success:
            check_cmd = "[ -f /opt/automation/setup_complete ] && echo 'yes' || echo 'no'"
            c_success, c_stdout, _ = service.execute_ssh_command(ip_address, check_cmd, 'root', ssh_key_path)
            if c_success and c_stdout.strip() == 'yes':
                is_complete = True
        
        if not success:
             # Return success=True but with error message in logs to display it in the console
             return jsonify({
                 'success': True, 
                 'logs': f"[Error connecting to droplet: {stderr}]",
                 'droplet_name': droplet.get('name'),
                 'ip_address': ip_address,
                 'setup_complete': False
             })
             
        return jsonify({
            'success': True, 
            'logs': stdout,
            'droplet_name': droplet.get('name'),
            'ip_address': ip_address,
            'setup_complete': is_complete
        })

    except Exception as e:
        logger.error(f"Get setup logs error: {e}")
        # Return the actual exception message so frontend can show it
        return jsonify({
            'success': True, 
            'logs': f"[System Error: {str(e)}]",
            'droplet_name': 'Unknown',
            'ip_address': 'Unknown'
        })


@digitalocean_manager.route('/api/do/snapshots/<snapshot_id>', methods=['DELETE'])
@login_required
def delete_snapshot(snapshot_id):
    """Delete a snapshot"""
    try:
        config = DigitalOceanConfig.query.first()
        if not config or not config.api_token:
            return jsonify({'success': False, 'error': 'DigitalOcean not configured'}), 400
        
        service = DigitalOceanService(config.api_token)
        success = service.delete_snapshot(snapshot_id)
        
        if success:
            return jsonify({'success': True, 'message': 'Snapshot deleted successfully'})
        else:
            return jsonify({'success': False, 'error': 'Failed to delete snapshot'}), 500
    except Exception as e:
        logger.error(f"Delete snapshot error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


# Execution Routes
@digitalocean_manager.route('/api/do/execute', methods=['POST'])
@login_required
def execute_automation():
    """Execute bulk automation on droplets"""
    try:
        data = request.get_json()
        
        # Get users list
        users = data.get('users', [])
        if not users:
            return jsonify({'success': False, 'error': 'No users provided'}), 400
        
        # Get execution parameters
        droplet_count = int(data.get('droplet_count', 1))
        snapshot_id = data.get('snapshot_id', '').strip()
        region = data.get('region', '').strip()
        size = data.get('size', '').strip()
        parallel_users = int(data.get('parallel_users', 5))
        users_per_droplet = int(data.get('users_per_droplet', 50))
        raw_auto_destroy = data.get('auto_destroy', True)
        auto_destroy = str(raw_auto_destroy).lower() == 'true' if isinstance(raw_auto_destroy, str) else bool(raw_auto_destroy)
        
        # Validation
        if droplet_count < 1:
            return jsonify({'success': False, 'error': 'Droplet count must be at least 1'}), 400
        
        if not snapshot_id:
            return jsonify({'success': False, 'error': 'Snapshot ID is required'}), 400
        
        # Get DO config
        config = DigitalOceanConfig.query.first()
        if not config or not config.api_token:
            return jsonify({'success': False, 'error': 'DigitalOcean not configured'}), 400
        
        # Use config defaults if not provided
        if not region:
            region = config.default_region or 'nyc3'
        if not size:
            size = config.default_size or 's-1vcpu-1gb'
        
        # Prepare clean config dict (avoiding full SQLAlchemy object)
        clean_config = {
            'api_token': config.api_token,
            'ssh_key_id': config.ssh_key_id,
            'ssh_private_key_path': config.ssh_private_key_path,
            'default_region': config.default_region,
            'default_size': config.default_size,
            'auto_destroy_droplets': config.auto_destroy_droplets
        }
        
        # Initialize service and orchestrator
        from services.digitalocean_bulk_executor import BulkExecutionOrchestrator
        service = DigitalOceanService(config.api_token)
        orchestrator = BulkExecutionOrchestrator(
            config=clean_config, 
            service=service,
            app=current_app._get_current_object()
        )
        
        # Create execution ID and DB record immediately
        import time
        execution_id = f"exec_{int(time.time())}"
        
        execution = DigitalOceanExecution()
        execution.task_id = execution_id
        execution.username = get_current_username()
        execution.total_users = len(users)
        execution.status = 'running'
        execution.snapshot_id = snapshot_id
        execution.region = region
        execution.size = size
        execution.started_at = datetime.utcnow()
        
        db.session.add(execution)
        db.session.commit()
        
        # Execute in background thread
        # Pass the actual app object to avoiding import issues in thread
        app = current_app._get_current_object()
        
        try:
            execution_thread = threading.Thread(
                target=_run_bulk_execution_background,
                args=(app, orchestrator, users, droplet_count, snapshot_id, region, size, auto_destroy, execution_id, parallel_users, users_per_droplet)
            )
            execution_thread.daemon = True
            execution_thread.start()
            logger.info(f"Background thread started for execution {execution_id}")
        except Exception as thread_err:
            logger.error(f"Failed to start execution thread: {thread_err}")
            return jsonify({'success': False, 'error': f"Failed to start worker thread: {thread_err}"}), 500
        
        return jsonify({
            'success': True,
            'message': 'Bulk execution started',
            'execution_id': execution_id,
            'total_users': len(users),
            'droplet_count': droplet_count
        })
        
    except Exception as e:
        logger.error(f"Execute automation error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


def _run_bulk_execution_background(
    app,
    orchestrator,
    users,
    droplet_count,
    snapshot_id,
    region,
    size,
    auto_destroy,
    execution_id,
    parallel_users=5,
    users_per_droplet=50
):
    """Background task for bulk execution"""
    # Create a new app context for the thread using the passed app object
    with app.app_context():
        # Inject app into orchestrator for DB access
        if hasattr(orchestrator, 'set_app'):
            orchestrator.set_app(app)
            
        try:
            logger.info(f"THREAD START [{execution_id}]: Starting background execution for {len(users)} users")
            
            # Check if we can see the DB record
            check_exec = DigitalOceanExecution.query.filter_by(task_id=execution_id).first()
            if not check_exec:
                logger.error(f"THREAD ERROR [{execution_id}]: Execution record NOT FOUND in DB at start!")
            else:
                logger.info(f"THREAD DEBUG [{execution_id}]: Found DB record, status={check_exec.status}")

            result = orchestrator.execute_bulk(
                users=users,
                droplet_count=droplet_count,
                snapshot_id=snapshot_id,
                region=region,
                size=size,
                auto_destroy=auto_destroy,
                execution_id=execution_id,
                parallel_users=parallel_users,
                users_per_droplet=users_per_droplet
            )
            
            logger.info(f"THREAD RESULT [{execution_id}]: Orchestrator finished. Success={result.get('success')}, Error={result.get('error')}")

            # Update results in database
            execution = DigitalOceanExecution.query.filter_by(task_id=execution_id).first()
            if execution:
                execution.droplets_created = result.get('droplets_used', 0)
                execution.success_count = result.get('success_count', 0)
                execution.failure_count = result.get('fail_count', 0)
                execution.results_json = json.dumps(result.get('results', []))
                execution.status = 'completed' if result['success'] else 'failed'
                execution.error_message = result.get('error')
                execution.completed_at = datetime.utcnow()
                
                db.session.commit()
                logger.info(f"THREAD SUCCESS [{execution_id}]: DB updated successfully")
            else:
                logger.error(f"THREAD ERROR [{execution_id}]: Execution record lost during processing!")
            
        except Exception as e:
            logger.error(f"THREAD EXCEPTION [{execution_id}]: {e}", exc_info=True)
            # Try to update status to failed
            try:
                execution = DigitalOceanExecution.query.filter_by(task_id=execution_id).first()
                if execution:
                    execution.status = 'failed'
                    execution.error_message = f"Internal Error: {str(e)}"
                    execution.completed_at = datetime.utcnow()
                    db.session.commit()
                    logger.info(f"THREAD RECOVERY [{execution_id}]: Updated status to failed")
            except Exception as db_e:
                 logger.error(f"THREAD FATAL [{execution_id}]: Could not update DB after exception: {db_e}")


@digitalocean_manager.route('/api/do/execution/<execution_id>/status', methods=['GET'])
@login_required
def get_execution_status(execution_id):
    """Get status of a bulk execution"""
    try:
        execution = DigitalOceanExecution.query.filter_by(task_id=execution_id).first()
        if not execution:
            return jsonify({'success': False, 'error': 'Execution not found'}), 404
            
        # Fetch droplets for this execution
        droplets = DigitalOceanDroplet.query.filter_by(execution_task_id=execution_id).all()
        
        return jsonify({
            'success': True,
            'status': execution.status,
            'droplets_created': execution.droplets_created,
            'success_count': execution.success_count,
            'failure_count': execution.failure_count,
            'error_message': execution.error_message,
            'completed': execution.status in ['completed', 'failed'],
            'droplets': [{
                'droplet_id': d.droplet_id,
                'droplet_name': d.droplet_name,
                'ip_address': d.ip_address,
                'status': d.status
            } for d in droplets]
        })
    except Exception as e:
        logger.error(f"Get execution status error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@digitalocean_manager.route('/api/do/monitor/active-droplets', methods=['GET'])
@login_required
def get_active_monitor_droplets():
    """Fetch all active droplets for the persistent monitor (Synced with API)"""
    try:
        config = DigitalOceanConfig.query.first()
        if not config or not config.api_token:
             return jsonify({'success': False, 'error': 'DigitalOcean not configured'}), 400

        # 1. Fetch live droplets from API
        service = DigitalOceanService(config.api_token)
        api_droplets = service.list_droplets()
        api_droplet_ids = {str(d['id']) for d in api_droplets}
        
        # 2. Get droplets from DB that aren't marked as destroyed
        db_droplets = DigitalOceanDroplet.query.filter(DigitalOceanDroplet.status != 'destroyed').all()
        
        # 3. Filter DB droplets: Only keep those that still exist in the DO account
        results = []
        for d in db_droplets:
            if d.droplet_id in api_droplet_ids:
                # Find the matching API droplet to get current IP/status if needed
                api_match = next((ad for ad in api_droplets if str(ad['id']) == d.droplet_id), None)
                
                results.append({
                    'droplet_id': d.droplet_id,
                    'droplet_name': d.droplet_name,
                    'ip_address': api_match.get('ip_address') if api_match else d.ip_address,
                    'status': api_match.get('status') if api_match else d.status,
                    'execution_id': d.execution_task_id,
                    'created_at': d.created_at.isoformat() if d.created_at else None
                })
        
        # Sort by creation date descending
        results.sort(key=lambda x: x['created_at'] or '', reverse=True)

        return jsonify({
            'success': True,
            'droplets': results
        })
    except Exception as e:
        logger.error(f"Get active monitor droplets error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@digitalocean_manager.route('/api/do/generated-passwords/<execution_id>', methods=['GET'])
@login_required
def get_generated_passwords(execution_id):
    """Fetch generated passwords from specific execution via backup files OR database"""
    try:
        passwords = []
        is_all = (execution_id.lower() == 'all')
        
        # 1. Try Backup Files (Primary Source for this execution)
        backup_dir = os.path.join(current_app.root_path, 'do_app_passwords_backup')
        
        logger.info(f"Checking for passwords in: {backup_dir} (Execution ID: {execution_id})")
        
        if os.path.exists(backup_dir):
            files = os.listdir(backup_dir)
            logger.info(f"Found {len(files)} files in backup dir")
            
            # Find all backup files matching this execution OR all if requested
            for filename in files:
                if filename.endswith('.json'):
                    # Match exact execution ID or "all"
                    if is_all or filename.startswith(f"{execution_id}_"):
                        try:
                            filepath = os.path.join(backup_dir, filename)
                            with open(filepath, 'r') as f:
                                data = json.load(f)
                            
                            passwords.append({
                                'email': data.get('email'),
                                'app_password': data.get('app_password'),
                                'secret_key': data.get('secret_key'),
                                'created_at': data.get('timestamp'),
                                'updated_at': data.get('db_save_timestamp'),
                                'saved_to_db': data.get('saved_to_db', False),
                                'source': 'backup_file'
                            })
                        except Exception as e:
                            logger.error(f"Error reading backup file {filename}: {e}")
                            continue

        # 2. ALSO Fetch from Database if 'all' is requested OR if backups are missing
        # This ensures we get everything even if files were deleted
        if is_all or not passwords:
            logger.info(f"Fetching passwords from DB for execution_id={execution_id}")
            try:
                # Filter by execution_id if provided (and not 'all')
                if is_all:
                    db_passwords = AwsGeneratedPassword.query.all()
                else:
                    # NEW: Try to filter by execution_id (requires migration)
                    try:
                        db_passwords = AwsGeneratedPassword.query.filter_by(execution_id=execution_id).all()
                    except Exception:
                        # Fallback for old schema: Get all and filter in python (inefficient but safe)
                        # or just return empty if we strictly need execution_id match
                        db.session.rollback()
                        logger.warning("execution_id column might be missing, skipping DB filter by ID")
                        db_passwords = [] 
                    
                # Deduplicate based on email
                existing_emails = set(p['email'] for p in passwords)
                
                for db_p in db_passwords:
                    if db_p.email not in existing_emails:
                        passwords.append({
                            'email': db_p.email,
                            'app_password': db_p.app_password,
                            'secret_key': db_p.secret_key,
                            'created_at': db_p.created_at.isoformat() if db_p.created_at else None,
                            'source': 'database'
                        })
                        existing_emails.add(db_p.email)
            except Exception as db_e:
                logger.error(f"Error fetching from DB: {db_e}")
        
        # Sort by email
        passwords.sort(key=lambda x: x['email'])
        
        return jsonify({
            'success': True,
            'execution_id': execution_id,
            'passwords': passwords
        })
        
    except Exception as e:
        logger.error(f"Error fetching passwords for execution {execution_id}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@digitalocean_manager.route('/api/do/generated-passwords/batch-fetch', methods=['POST'])
@login_required
def batch_fetch_generated_passwords():
    """Fetch generated passwords for a specific list of emails (Strict Filtering)"""
    try:
        data = request.json
        emails = data.get('emails', [])
        
        if not emails:
             return jsonify({'success': True, 'passwords': []})
             
        # Normalize emails (lowercase, strip)
        clean_emails = [e.strip().lower() for e in emails if e and e.strip()]
        
        if not clean_emails:
             return jsonify({'success': True, 'passwords': []})
             
        logger.info(f"Batch fetching passwords for {len(clean_emails)} users")
        
        # Query DB for these emails
        # We use 'in_' clause for efficiency
        db_passwords = AwsGeneratedPassword.query.filter(AwsGeneratedPassword.email.in_(clean_emails)).all()
        
        found_emails = {p.email.lower() for p in db_passwords}
        results = []
        
        # 1. Add DB results
        for p in db_passwords:
            results.append({
                'email': p.email,
                'app_password': p.app_password,
                'secret_key': p.secret_key,
                'created_at': p.created_at.isoformat() if p.created_at else None,
                'execution_id': p.execution_id,
                'source': 'database'
            })
            
        # 2. Recovery: Check backup files for MISSING emails
        missing_emails = [e for e in clean_emails if e not in found_emails]
        if missing_emails:
            logger.info(f"Searching backup files for {len(missing_emails)} missing users")
            backup_dir = os.path.join(current_app.root_path, 'do_app_passwords_backup')
            if os.path.exists(backup_dir):
                try:
                    backup_files = os.listdir(backup_dir)
                    for filename in backup_files:
                        if not filename.endswith('.json'):
                            continue
                        
                        try:
                            # NEW ROBUST FILENAME: {email_slug}___{execution_id}.json
                            if '___' in filename:
                                email_slug = filename.split('___', 1)[0]
                                email_from_file = email_slug.replace('_at_', '@').lower()
                                
                                if email_from_file in missing_emails:
                                    with open(os.path.join(backup_dir, filename), 'r') as f:
                                        bkp = json.load(f)
                                        results.append({
                                            'email': bkp.get('email'),
                                            'app_password': bkp.get('app_password'),
                                            'secret_key': bkp.get('secret_key'),
                                            'created_at': bkp.get('timestamp'),
                                            'execution_id': bkp.get('execution_id'),
                                            'source': 'backup_file'
                                        })
                                        found_emails.add(email_from_file)
                                        missing_emails.remove(email_from_file)
                                        if not missing_emails:
                                            break
                        except:
                            continue
                except Exception as bkp_err:
                    logger.error(f"Backup recovery error: {bkp_err}")

        logger.info(f"Found {len(results)} passwords matching the requested list ({len(results) - len(db_passwords)} from backups)")
        
        return jsonify({
            'success': True,
            'passwords': results,
            'found_count': len(results),
            'requested_count': len(clean_emails)
        })
        
    except Exception as e:
        logger.error(f"Batch fetch passwords error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@digitalocean_manager.route('/api/do/execution/<execution_id>/droplets', methods=['GET'])
@login_required
def get_execution_droplets(execution_id):
    """Get list of droplets associated with an execution"""
    try:
        droplets = DigitalOceanDroplet.query.filter_by(execution_task_id=execution_id).all()
        
        return jsonify({
            'success': True,
            'droplets': [{
                'id': d.droplet_id,
                'name': d.droplet_name,
                'ip_address': d.ip_address,
                'status': d.status,
                'region': d.region,
                'size': d.size
            } for d in droplets]
        })
    except Exception as e:
        logger.error(f"Error fetching droplets for execution {execution_id}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@digitalocean_manager.route('/api/do/execution/<execution_id>/droplets/<droplet_id>/logs', methods=['GET'])
@login_required
def get_bulk_droplet_logs(execution_id, droplet_id):
    """Get logs for a specific droplet in a bulk execution (Supports Incremental Polling)"""
    try:
        from flask import request
        cursor = request.args.get('cursor', 0, type=int)
        
        # Logs are stored in logs/bulk_executions/<execution_id>/<droplet_id>.log
        # Use absolute path to ensure we find it regardless of CWD
        log_dir = os.path.join(current_app.root_path, 'logs', 'bulk_executions', execution_id)
        log_file = os.path.join(log_dir, f"{droplet_id}.log")
        
        if not os.path.exists(log_file):
            return jsonify({
                'success': True,
                'logs': "Initializing connection to droplet... (Please wait a moment)" if cursor == 0 else "",
                'next_cursor': 0
            })
            
        from database import DigitalOceanExecution
        execution = DigitalOceanExecution.query.filter_by(task_id=execution_id).first()
        is_active = True
        if execution and execution.status in ['completed', 'failed']:
            is_active = False

        file_size = os.path.getsize(log_file)
        logs = ""
        
        if cursor < file_size:
            with open(log_file, 'r', encoding='utf-8') as f:
                f.seek(cursor)
                logs = f.read()
            
        return jsonify({
            'success': True,
            'logs': logs,
            'next_cursor': file_size,
            'is_active': is_active
        })
    except Exception as e:
        logger.error(f"Error fetching logs for droplet {droplet_id}: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@digitalocean_manager.route('/api/do/generated-passwords/download/<execution_id>', methods=['GET'])
@login_required
def download_generated_passwords(execution_id):
    """Download generated passwords as a TXT file"""
    try:
        from flask import Response
        
        # reuse get_generated_passwords logic or call it
        # simpler to just fetch again to ensure we get the latest
        passwords = []
        is_all = (execution_id.lower() == 'all')
        
        # 1. Try Backup Files
        backup_dir = os.path.join(current_app.root_path, 'do_app_passwords_backup')
        
        if os.path.exists(backup_dir):
            for filename in os.listdir(backup_dir):
                if filename.endswith('.json'):
                    # The filename format is {email_slug}___{execution_id}.json
                    if is_all or f"___{execution_id}.json" in filename or filename.startswith(f"{execution_id}_"):
                        try:
                            filepath = os.path.join(backup_dir, filename)
                            with open(filepath, 'r') as f:
                                data = json.load(f)
                            
                            if data.get('email') and data.get('app_password'):
                                passwords.append({
                                    'email': data.get('email'),
                                    'app_password': data.get('app_password')
                                })
                        except Exception:
                            continue

        # 2. Database Fallback
        try:
            from database import AwsGeneratedPassword
            if is_all:
                db_passwords = AwsGeneratedPassword.query.all()
            else:
                db_passwords = AwsGeneratedPassword.query.filter_by(execution_id=execution_id).all()
                
            existing = set(p['email'] for p in passwords)
            for dp in db_passwords:
                if (dp.email not in existing) and dp.app_password:
                    passwords.append({'email': dp.email, 'app_password': dp.app_password})
                    existing.add(dp.email)
        except Exception as db_e:
            logger.error(f"DB fallback fetch failed for password download: {db_e}")
        
        passwords.sort(key=lambda x: x['email'])
        
        # Generate TXT content
        content = ""
        for p in passwords:
            if p.get('app_password'):
                 content += f"{p['email']}:{p['app_password']}\n"
        
        return Response(
            content,
            mimetype="text/plain",
            headers={"Content-disposition": f"attachment; filename=app_passwords_{execution_id}.txt"}
        )

    except Exception as e:
        logger.error(f"Download error: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500
