import logging
import uuid
import random
import string
from flask import Blueprint, request, jsonify, session, render_template, redirect, url_for
from functools import wraps
from database import db, AfraidConfig, AfraidDomain
from services.afraid_dns_service import AfraidDNSService

logger = logging.getLogger(__name__)

afraid_manager = Blueprint('afraid_manager', __name__)

def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if 'user' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return wrapper

@afraid_manager.route('/afraid', methods=['GET'])
@login_required
def afraid_page():
    return render_template('afraid.html')

@afraid_manager.route('/api/afraid/config', methods=['GET'])
@login_required
def get_config():
    config = AfraidConfig.query.first()
    if config:
        return jsonify({
            'success': True,
            'username': config.username,
            'is_configured': config.is_configured
        })
    return jsonify({'success': False})

@afraid_manager.route('/api/afraid/config', methods=['POST'])
@login_required
def save_config():
    data = request.get_json()
    username = data.get('username', '').strip()
    password = data.get('password', '').strip()  # May be absent if user didn't change it
    
    if not username:
        return jsonify({'success': False, 'error': 'Username is required'}), 400
        
    config = AfraidConfig.query.first()
    if not config:
        # First-time save – password required
        if not password:
            return jsonify({'success': False, 'error': 'Password is required for initial setup'}), 400
        config = AfraidConfig(username=username, password=password, is_configured=True)
        db.session.add(config)
    else:
        config.username = username
        # Only update password if a new one was actually provided
        if password:
            config.password = password
        config.is_configured = True
        
    db.session.commit()
    return jsonify({'success': True})

@afraid_manager.route('/api/afraid/domains', methods=['GET'])
@login_required
def get_domains():
    domains = AfraidDomain.query.all()
    return jsonify({
        'success': True,
        'domains': [{'id': d.id, 'domain_name': d.domain_name} for d in domains]
    })

@afraid_manager.route('/api/afraid/domains', methods=['POST'])
@login_required
def add_domain():
    data = request.get_json()
    domain_name = data.get('domain_name')
    if not domain_name:
        return jsonify({'success': False, 'error': 'Domain name required'}), 400
        
    domain_name = domain_name.strip().lower()
    exists = AfraidDomain.query.filter_by(domain_name=domain_name).first()
    if exists:
        return jsonify({'success': False, 'error': 'Domain already exists in database'}), 400
        
    domain = AfraidDomain(domain_name=domain_name)
    db.session.add(domain)
    db.session.commit()
    
    return jsonify({'success': True, 'domain': {'id': domain.id, 'domain_name': domain.domain_name}})

@afraid_manager.route('/api/afraid/domains/<int:domain_id>', methods=['DELETE'])
@login_required
def delete_domain(domain_id):
    domain = AfraidDomain.query.get(domain_id)
    if not domain:
        return jsonify({'success': False, 'error': 'Domain not found'}), 404
        
    db.session.delete(domain)
    db.session.commit()
    return jsonify({'success': True})

@afraid_manager.route('/api/afraid/fetch-domains', methods=['POST'])
@login_required
def fetch_domains_from_afraid():
    config = AfraidConfig.query.first()
    if not config or not config.is_configured:
        return jsonify({'success': False, 'error': 'Afraid credentials not configured'}), 400
        
    service = AfraidDNSService(config.username, config.password)
    if not service.logged_in:
        return jsonify({'success': False, 'error': 'Failed to login to Afraid'}), 401
        
    domain_map = service.get_domains_with_ids()
    if not domain_map:
        return jsonify({'success': False, 'error': 'No domains found or failed to parse'}), 400
        
    added = 0
    for domain_name, _ in domain_map.items():
        exists = AfraidDomain.query.filter_by(domain_name=domain_name).first()
        if not exists:
            db.session.add(AfraidDomain(domain_name=domain_name))
            added += 1
            
    db.session.commit()
    return jsonify({'success': True, 'added': added})

@afraid_manager.route('/api/afraid/create-subdomain', methods=['POST'])
@login_required
def create_subdomain():
    data = request.get_json()
    base_domain = data.get('base_domain')
    destination = data.get('destination')
    ttl = data.get('ttl', 300)
    
    if not base_domain or not destination:
        return jsonify({'success': False, 'error': 'Base domain and destination required'}), 400
        
    config = AfraidConfig.query.first()
    if not config or not config.is_configured:
        return jsonify({'success': False, 'error': 'Afraid credentials not configured'}), 400
        
    service = AfraidDNSService(config.username, config.password)
    if not service.logged_in:
        return jsonify({'success': False, 'error': 'Failed to login to Afraid'}), 401
        
    domain_map = service.get_domains_with_ids()
    if base_domain not in domain_map:
        return jsonify({'success': False, 'error': f"Domain '{base_domain}' not found in your Afraid account. Cannot get domain_id."}), 404
        
    domain_id = domain_map[base_domain]
    
    # Generate a random subdomain (e.g. 15 chars)
    # The screenshot used words, but random letters is easier and unique
    import string, random
    subdomain = ''.join(random.choices(string.ascii_lowercase, k=15))
    
    success, message = service.add_cname(subdomain, domain_id, destination, ttl)
    
    if success:
        return jsonify({'success': True, 'subdomain': f"{subdomain}.{base_domain}", 'message': message})
    else:
        return jsonify({'success': False, 'error': message}), 400
