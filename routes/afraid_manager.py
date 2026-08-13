import logging
import random
import string
from datetime import datetime
from flask import Blueprint, request, jsonify, session, render_template, redirect, url_for
from functools import wraps
from database import db, AfraidConfig, AfraidDomain
from services.afraid_dns_service import AfraidDNSService
from services.cloudflare_dns_service import CloudflareDNSService

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
    tld = request.args.get('tld', '').strip().lower()
    query = AfraidDomain.query
    if tld:
        query = query.filter_by(tld=tld)
    domains = query.order_by(AfraidDomain.domain_name.asc()).all()
    return jsonify({
        'success': True,
        'domains': [{
            'id': d.id,
            'domain_name': d.domain_name,
            'domain_id': d.domain_id,
            'tld': d.tld,
            'source': d.source,
            'rotation_count': d.rotation_count or 0
        } for d in domains]
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

    domain = AfraidDomain(
        domain_name=domain_name,
        tld=domain_name.rsplit('.', 1)[-1] if '.' in domain_name else None,
        source='manual'
    )
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

    data = request.get_json(silent=True) or {}
    start_page = int(data.get('start_page') or 1)
    end_page = int(data.get('end_page') or 212)
    start_page = max(1, start_page)
    end_page = max(start_page, min(212, end_page))

    added = 0
    updated = 0
    total_seen = 0
    errors = []

    for page in range(start_page, end_page + 1):
        try:
            registry_domains = svc.fetch_registry_page(page)
        except Exception as e:
            errors.append(f"page {page}: {e}")
            continue

        total_seen += len(registry_domains)
        for item in registry_domains:
            domain_name = item['domain_name']
            domain = AfraidDomain.query.filter_by(domain_name=domain_name).first()
            if not domain:
                domain = AfraidDomain(domain_name=domain_name)
                db.session.add(domain)
                added += 1
            else:
                updated += 1
            domain.domain_id = item.get('domain_id')
            domain.tld = item.get('tld')
            domain.source = 'registry'

        if page % 10 == 0:
            db.session.commit()

    db.session.commit()

    if total_seen == 0:
        return jsonify({
            'success': False,
            'error': '; '.join(errors[:5]) or 'No domains found in the FreeDNS registry pages.'
        }), 400

    return jsonify({
        'success': True,
        'added': added,
        'updated': updated,
        'total': total_seen,
        'pages': end_page - start_page + 1,
        'errors': errors[:10]
    })

@afraid_manager.route('/api/afraid/tlds', methods=['GET'])
@login_required
def get_tld_groups():
    rows = db.session.query(AfraidDomain.tld, db.func.count(AfraidDomain.id)).filter(
        AfraidDomain.tld.isnot(None)
    ).group_by(AfraidDomain.tld).order_by(AfraidDomain.tld.asc()).all()
    return jsonify({
        'success': True,
        'tlds': [{'tld': tld, 'count': count} for tld, count in rows if tld]
    })

@afraid_manager.route('/api/afraid/cloudflare-domains', methods=['GET'])
@login_required
def get_afraid_cloudflare_domains():
    try:
        service = CloudflareDNSService()
        zones = service.get_zones()
        return jsonify({
            'success': True,
            'domains': [{'name': z['name'], 'id': z['id'], 'status': z.get('status')} for z in zones],
            'total': len(zones)
        })
    except Exception as e:
        logger.error(f"Error fetching Cloudflare domains for Afraid page: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500

def get_rotated_afraid_domain(tld):
    query = AfraidDomain.query.filter(AfraidDomain.domain_id.isnot(None))
    if tld:
        query = query.filter_by(tld=tld)
    return query.order_by(
        AfraidDomain.rotation_count.asc(),
        AfraidDomain.last_used_at.asc(),
        AfraidDomain.id.asc()
    ).first()

@afraid_manager.route('/api/afraid/create-subdomain', methods=['POST'])
@login_required
def create_subdomain():
    data = request.get_json()
    base_domain = data.get('base_domain', '').strip().lower()
    tld = data.get('tld', '').strip().lower()
    rotate_domain = bool(data.get('rotate_domain'))
    destination = data.get('destination', '').strip()
    ttl = data.get('ttl', 300)

    if not destination:
        return jsonify({'success': False, 'error': 'Destination is required'}), 400

    svc, error = get_service()
    if error:
        return jsonify({'success': False, 'error': error}), 401

    domain_record = None
    if rotate_domain:
        domain_record = get_rotated_afraid_domain(tld)
        if not domain_record:
            return jsonify({'success': False, 'error': f"No cached FreeDNS domains found for TLD '{tld}'."}), 404
        base_domain = domain_record.domain_name
        domain_id = domain_record.domain_id
    else:
        if not base_domain:
            return jsonify({'success': False, 'error': 'Base domain is required'}), 400
        domain_record = AfraidDomain.query.filter_by(domain_name=base_domain).first()
        domain_id = domain_record.domain_id if domain_record and domain_record.domain_id else svc.get_domain_id(base_domain)

    if not domain_id:
        return jsonify({
            'success': False,
            'error': f"Domain '{base_domain}' does not have a FreeDNS domain_id. Fetch the FreeDNS registry first or choose a different domain."
        }), 404

    subdomain = ''.join(random.choices(string.ascii_lowercase, k=15))

    success, message = svc.add_cname(subdomain, domain_id, destination, ttl)

    if success:
        if domain_record:
            domain_record.rotation_count = (domain_record.rotation_count or 0) + 1
            domain_record.last_used_at = datetime.utcnow()
            db.session.commit()
        return jsonify({'success': True, 'subdomain': f"{subdomain}.{base_domain}", 'message': message})
    else:
        return jsonify({'success': False, 'error': message}), 400
