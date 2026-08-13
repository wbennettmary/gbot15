import logging
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

def get_service():
    """Get an authenticated AfraidDNSService from stored cookies."""
    config = AfraidConfig.query.first()
    if not config or not config.cookies_str:
        return None, "Afraid cookies not configured. Please import your browser cookies."
    svc = AfraidDNSService(config.cookies_str)
    if not svc.logged_in:
        return None, svc.auth_error or "Afraid cookies are expired or invalid. Please re-import your browser cookies."
    return svc, None

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
            'is_configured': bool(config.cookies_str),
            'has_cookies': bool(config.cookies_str),
            # Show a short preview of the cookie to confirm it's saved
            'cookies_preview': config.cookies_str[:40] + '...' if config.cookies_str and len(config.cookies_str) > 40 else (config.cookies_str or '')
        })
    return jsonify({'success': False, 'is_configured': False, 'has_cookies': False})

@afraid_manager.route('/api/afraid/config', methods=['POST'])
@login_required
def save_config():
    data = request.get_json(silent=True) or {}
    cookies_str = data.get('cookies_str', '').strip()

    if not cookies_str:
        return jsonify({'success': False, 'error': 'Cookie string is required'}), 400

    # Quick validation — test the cookies before saving
    svc = AfraidDNSService(cookies_str)
    if not svc.logged_in:
        return jsonify({
            'success': False,
            'error': svc.auth_error or 'Cookies appear to be invalid or expired. Please re-export fresh cookies from your browser.'
        }), 401

    domain_map = svc.get_domains_with_ids()
    if not domain_map:
        return jsonify({
            'success': False,
            'error': svc.last_error or 'Cookies authenticated, but no FreeDNS domains were found in the add-subdomain form.'
        }), 401

    config = AfraidConfig.query.first()
    if not config:
        config = AfraidConfig(cookies_str=svc.cookies_str, is_configured=True)
        db.session.add(config)
    else:
        config.cookies_str = svc.cookies_str
        config.is_configured = True

    db.session.commit()
    return jsonify({'success': True, 'message': 'Cookies saved and verified successfully!'})

@afraid_manager.route('/api/afraid/config/test', methods=['POST'])
@login_required
def test_config():
    """Test if current cookies are still valid."""
    svc, error = get_service()
    if error:
        return jsonify({'success': False, 'error': error})
    domain_map = svc.get_domains_with_ids()
    if not domain_map:
        return jsonify({
            'success': False,
            'error': svc.last_error or 'Cookies authenticated, but no FreeDNS domains were found in the add-subdomain form.'
        })
    return jsonify({'success': True, 'message': f'Cookies are valid. Found {len(domain_map)} FreeDNS domain(s).'})

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
    domain_name = data.get('domain_name', '').strip().lower()
    if not domain_name:
        return jsonify({'success': False, 'error': 'Domain name required'}), 400

    exists = AfraidDomain.query.filter_by(domain_name=domain_name).first()
    if exists:
        return jsonify({'success': False, 'error': 'Domain already exists'}), 400

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
    svc, error = get_service()
    if error:
        return jsonify({'success': False, 'error': error}), 401

    domain_map = svc.get_domains_with_ids()
    if not domain_map:
        return jsonify({
            'success': False,
            'error': svc.last_error or 'No domains found. Make sure you have domains in your FreeDNS account.'
        }), 400

    added = 0
    for domain_name in domain_map.keys():
        if not AfraidDomain.query.filter_by(domain_name=domain_name).first():
            db.session.add(AfraidDomain(domain_name=domain_name))
            added += 1

    db.session.commit()
    return jsonify({'success': True, 'added': added, 'total': len(domain_map)})

@afraid_manager.route('/api/afraid/create-subdomain', methods=['POST'])
@login_required
def create_subdomain():
    data = request.get_json()
    base_domain = data.get('base_domain', '').strip()
    destination = data.get('destination', '').strip()
    ttl = data.get('ttl', 300)

    if not base_domain or not destination:
        return jsonify({'success': False, 'error': 'Base domain and destination are required'}), 400

    svc, error = get_service()
    if error:
        return jsonify({'success': False, 'error': error}), 401

    domain_map = svc.get_domains_with_ids()
    domain_id = domain_map.get(base_domain.lower())
    if not domain_id:
        available = ', '.join(sorted(domain_map.keys())[:10])
        details = f" Available FreeDNS domains: {available}." if available else f" {svc.last_error}" if svc.last_error else ""
        return jsonify({
            'success': False,
            'error': f"Domain '{base_domain}' was not found in the FreeDNS add-subdomain form.{details}"
        }), 404

    subdomain = ''.join(random.choices(string.ascii_lowercase, k=15))

    success, message = svc.add_cname(subdomain, domain_id, destination, ttl)

    if success:
        return jsonify({'success': True, 'subdomain': f"{subdomain}.{base_domain}", 'message': message})
    else:
        return jsonify({'success': False, 'error': message}), 400
