import logging
import json
from datetime import datetime
from flask import Blueprint, request, jsonify, session, render_template, redirect, url_for
from functools import wraps
from faker import Faker
from sqlalchemy import func, or_
from database import db, AfraidConfig, AfraidDomain, AfraidResultList
from services.afraid_dns_service import AfraidDNSService
from services.cloudflare_dns_service import CloudflareDNSService

logger = logging.getLogger(__name__)
fake = Faker()

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

def month_start():
    now = datetime.utcnow()
    return datetime(now.year, now.month, 1)

def available_domain_query():
    return AfraidDomain.query.filter(
        AfraidDomain.domain_id.isnot(None),
        AfraidDomain.registry_status == 'public',
        or_(AfraidDomain.last_used_at.is_(None), AfraidDomain.last_used_at < month_start())
    )

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
    page = max(1, int(request.args.get('page', 1)))
    per_page = min(25, max(1, int(request.args.get('per_page', 5))))
    query = AfraidDomain.query.filter(AfraidDomain.domain_id.isnot(None), AfraidDomain.registry_status == 'public')
    if tld:
        query = query.filter_by(tld=tld)
    total = query.count()
    domains = query.order_by(AfraidDomain.domain_name.asc()).offset((page - 1) * per_page).limit(per_page).all()
    return jsonify({
        'success': True,
        'total': total,
        'page': page,
        'per_page': per_page,
        'pages': (total + per_page - 1) // per_page if per_page else 1,
        'domains': [{
            'id': d.id,
            'domain_name': d.domain_name,
            'domain_id': d.domain_id,
            'tld': d.tld,
            'source': d.source,
            'rotation_count': d.rotation_count or 0,
            'used_this_month': bool(d.last_used_at and d.last_used_at >= month_start()),
            'registry_status': d.registry_status,
            'registry_owner': d.registry_owner,
            'hosts_in_use': d.hosts_in_use,
            'registry_age_text': d.registry_age_text,
            'registry_created_on': d.registry_created_on
        } for d in domains]
    })

@afraid_manager.route('/api/afraid/domain-options', methods=['GET'])
@login_required
def get_domain_options():
    tld = request.args.get('tld', '').strip().lower()
    limit = min(5000, max(1, int(request.args.get('limit', 1000))))
    include_used = request.args.get('include_used', '').lower() == 'true'
    query = AfraidDomain.query.filter(AfraidDomain.domain_id.isnot(None), AfraidDomain.registry_status == 'public') if include_used else available_domain_query()
    if tld:
        query = query.filter_by(tld=tld)
    domains = query.order_by(
        AfraidDomain.rotation_count.asc(),
        AfraidDomain.last_used_at.asc(),
        AfraidDomain.domain_name.asc()
    ).limit(limit).all()
    return jsonify({
        'success': True,
        'domains': [{
            'domain_name': d.domain_name,
            'domain_id': d.domain_id,
            'tld': d.tld,
            'used_this_month': bool(d.last_used_at and d.last_used_at >= month_start()),
            'hosts_in_use': d.hosts_in_use,
            'registry_created_on': d.registry_created_on
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
    public_seen = 0
    errors = []

    for page in range(start_page, end_page + 1):
        try:
            registry_domains = svc.fetch_registry_page(page)
        except Exception as e:
            errors.append(f"page {page}: {e}")
            continue

        total_seen += len(registry_domains)
        for item in registry_domains:
            if item.get('status') != 'public':
                continue
            public_seen += 1
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
            domain.registry_status = item.get('status')
            domain.registry_owner = item.get('owner')
            domain.hosts_in_use = item.get('hosts_in_use')
            domain.registry_age_text = item.get('age_text')
            domain.registry_created_on = item.get('created_on')

        if page % 10 == 0:
            db.session.commit()

    AfraidDomain.query.filter(
        AfraidDomain.source == 'registry'
    ).filter(
        or_(AfraidDomain.registry_status != 'public', AfraidDomain.registry_status.is_(None), AfraidDomain.domain_id.is_(None))
    ).delete(synchronize_session=False)
    db.session.commit()

    if public_seen == 0:
        return jsonify({
            'success': False,
            'error': '; '.join(errors[:5]) or 'No domains found in the FreeDNS registry pages.'
        }), 400

    return jsonify({
        'success': True,
        'added': added,
        'updated': updated,
        'total': public_seen,
        'seen': total_seen,
        'pages': end_page - start_page + 1,
        'errors': errors[:10]
    })

@afraid_manager.route('/api/afraid/tlds', methods=['GET'])
@login_required
def get_tld_groups():
    rows = db.session.query(AfraidDomain.tld, func.count(AfraidDomain.id)).filter(
        AfraidDomain.tld.isnot(None),
        AfraidDomain.domain_id.isnot(None),
        AfraidDomain.registry_status == 'public'
    ).group_by(AfraidDomain.tld).order_by(AfraidDomain.tld.asc()).all()
    used_rows = db.session.query(AfraidDomain.tld, func.count(AfraidDomain.id)).filter(
        AfraidDomain.tld.isnot(None),
        AfraidDomain.last_used_at >= month_start(),
        AfraidDomain.domain_id.isnot(None),
        AfraidDomain.registry_status == 'public'
    ).group_by(AfraidDomain.tld).all()
    used_by_tld = {tld: count for tld, count in used_rows}
    return jsonify({
        'success': True,
        'tlds': [{'tld': tld, 'count': count, 'used': used_by_tld.get(tld, 0), 'available': count - used_by_tld.get(tld, 0)} for tld, count in rows if tld]
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
    query = available_domain_query()
    if tld:
        query = query.filter_by(tld=tld)
    return query.order_by(
        AfraidDomain.rotation_count.asc(),
        AfraidDomain.last_used_at.asc(),
        AfraidDomain.id.asc()
    ).first()

def get_rotated_afraid_domains(tld, limit):
    query = available_domain_query()
    if tld:
        query = query.filter_by(tld=tld)
    return query.order_by(AfraidDomain.rotation_count.asc(), AfraidDomain.last_used_at.asc(), AfraidDomain.id.asc()).limit(limit).all()

def create_afraid_result_list(results):
    username = (session.get('user') or 'user').split('@')[0].lower()
    date_part = datetime.utcnow().strftime('%d/%m')
    prefix = f"{username}_list_{date_part}"
    existing = AfraidResultList.query.filter(AfraidResultList.name.like(f"{prefix}-%")).count()
    name = f"{prefix}-{existing + 1}"
    lines = []
    for item in results:
        if item.get('success'):
            lines.append(f"{item.get('subdomain')},{item.get('destination')}")
    if not lines:
        return None
    lst = AfraidResultList(
        name=name,
        created_by=username,
        raw_results="\n".join(lines),
        results_json=json.dumps(results),
        created_count=sum(1 for item in results if item.get('success')),
        failed_count=sum(1 for item in results if not item.get('success'))
    )
    db.session.add(lst)
    db.session.commit()
    return lst

@afraid_manager.route('/api/afraid/lists', methods=['GET'])
@login_required
def get_afraid_lists():
    lists = AfraidResultList.query.order_by(AfraidResultList.created_at.desc()).all()
    return jsonify({'success': True, 'lists': [lst.to_dict() for lst in lists]})

@afraid_manager.route('/api/afraid/lists', methods=['POST'])
@login_required
def create_manual_afraid_list():
    data = request.get_json(silent=True) or {}
    name = data.get('name', '').strip()
    raw_results = data.get('raw_results', '').strip()
    username = (session.get('user') or 'user').split('@')[0].lower()
    if not name or not raw_results:
        return jsonify({'success': False, 'error': 'List name and content are required'}), 400
    if AfraidResultList.query.filter_by(name=name).first():
        return jsonify({'success': False, 'error': 'A list with this name already exists'}), 400
    lines = [line for line in raw_results.splitlines() if line.strip()]
    lst = AfraidResultList(
        name=name,
        created_by=username,
        raw_results="\n".join(lines),
        created_count=len(lines),
        failed_count=0
    )
    db.session.add(lst)
    db.session.commit()
    return jsonify({'success': True, 'message': 'Afraid list created', 'list': lst.to_dict()})

@afraid_manager.route('/api/afraid/lists/<int:list_id>', methods=['PUT'])
@login_required
def update_afraid_list(list_id):
    data = request.get_json(silent=True) or {}
    lst = AfraidResultList.query.get(list_id)
    if not lst:
        return jsonify({'success': False, 'error': 'Afraid list not found'}), 404
    name = data.get('name', '').strip()
    raw_results = data.get('raw_results', '').strip()
    if not name or not raw_results:
        return jsonify({'success': False, 'error': 'List name and content are required'}), 400
    duplicate = AfraidResultList.query.filter(AfraidResultList.name == name, AfraidResultList.id != list_id).first()
    if duplicate:
        return jsonify({'success': False, 'error': 'A list with this name already exists'}), 400
    lines = [line for line in raw_results.splitlines() if line.strip()]
    lst.name = name
    lst.raw_results = "\n".join(lines)
    lst.created_count = len(lines)
    lst.failed_count = 0
    lst.results_json = None
    db.session.commit()
    return jsonify({'success': True, 'message': 'Afraid list updated', 'list': lst.to_dict()})

@afraid_manager.route('/api/afraid/lists/<int:list_id>', methods=['DELETE'])
@login_required
def delete_afraid_list(list_id):
    lst = AfraidResultList.query.get(list_id)
    if not lst:
        return jsonify({'success': False, 'error': 'Afraid list not found'}), 404
    db.session.delete(lst)
    db.session.commit()
    return jsonify({'success': True})

def generated_label():
    return ''.join(fake.word() for _ in range(3)).lower()

@afraid_manager.route('/api/afraid/subdomains', methods=['GET'])
@login_required
def get_existing_subdomains():
    svc, error = get_service()
    if error:
        return jsonify({'success': False, 'error': error}), 401
    records = svc.get_existing_subdomains()
    return jsonify({'success': True, 'records': records, 'total': len(records), 'error': svc.last_error})

@afraid_manager.route('/api/afraid/subdomains/delete', methods=['POST'])
@login_required
def delete_existing_subdomains():
    data = request.get_json(silent=True) or {}
    delete_all = bool(data.get('delete_all'))
    selected = set(data.get('delete_values') or [])
    svc, error = get_service()
    if error:
        return jsonify({'success': False, 'error': error}), 401
    records = svc.get_existing_subdomains()
    targets = records if delete_all else [r for r in records if r.get('delete_value') in selected]
    success, message = svc.delete_subdomains(targets)
    return jsonify({'success': success, 'message': message, 'deleted': len(targets)})

@afraid_manager.route('/api/afraid/create-batch', methods=['POST'])
@login_required
def create_batch_subdomains():
    data = request.get_json(silent=True) or {}
    tld = data.get('tld', '').strip().lower()
    base_domain = data.get('base_domain', '').strip().lower()
    base_domains = [d.strip().lower() for d in data.get('base_domains', []) if d.strip()]
    if base_domain and base_domain not in base_domains:
        base_domains.append(base_domain)
    afraid_count = max(1, min(50, int(data.get('afraid_count') or 1)))
    cloudflare_count = max(1, min(50, int(data.get('cloudflare_count') or 1)))
    total_count = max(1, min(50, int(data.get('total_count') or afraid_count)))
    ttl = data.get('ttl', 300)
    manual_destinations = [d.strip().lower() for d in data.get('destinations', []) if d.strip()]

    svc, error = get_service()
    if error:
        return jsonify({'success': False, 'error': error}), 401

    if base_domains:
        afraid_domains = []
        for selected_domain in base_domains:
            domain_record = AfraidDomain.query.filter_by(domain_name=selected_domain).first()
            if not domain_record:
                return jsonify({'success': False, 'error': f"FreeDNS domain '{selected_domain}' is not cached. Fetch Registry first."}), 404
            if domain_record.last_used_at and domain_record.last_used_at >= month_start():
                return jsonify({'success': False, 'error': f"FreeDNS domain '{selected_domain}' is already marked used for this month."}), 400
            if domain_record.registry_status != 'public' or not domain_record.domain_id:
                return jsonify({'success': False, 'error': f"FreeDNS domain '{selected_domain}' is not a usable public registry domain."}), 400
            afraid_domains.append(domain_record)
    else:
        afraid_domains = get_rotated_afraid_domains(tld, afraid_count)
    if not afraid_domains:
        return jsonify({'success': False, 'error': f"No cached FreeDNS domains found for TLD '{tld}'."}), 404

    try:
        cf_zones = [z['name'] for z in CloudflareDNSService().get_zones()]
    except Exception as e:
        cf_zones = []

    destinations = manual_destinations[:cloudflare_count]
    if not destinations:
        destinations = cf_zones[:cloudflare_count]
    elif len(destinations) < cloudflare_count:
        for zone_name in cf_zones:
            if zone_name not in destinations:
                destinations.append(zone_name)
            if len(destinations) >= cloudflare_count:
                break
    if not destinations:
        return jsonify({'success': False, 'error': 'No Cloudflare destination domains available.'}), 400

    results = []
    used_base_domains = set()
    for index in range(total_count):
        domain_record = afraid_domains[index % len(afraid_domains)]
        destination = destinations[index % len(destinations)]
        label = generated_label()
        success, message = svc.add_cname(label, domain_record.domain_id, destination, ttl)
        result = {
            'success': success,
            'subdomain': f"{label}.{domain_record.domain_name}",
            'base_domain': domain_record.domain_name,
            'destination': destination,
            'message': message
        }
        results.append(result)
        if success:
            domain_record.rotation_count = (domain_record.rotation_count or 0) + 1
            domain_record.last_used_at = datetime.utcnow()
            used_base_domains.add(domain_record.domain_name)
    db.session.commit()
    lst = create_afraid_result_list(results)
    return jsonify({
        'success': True,
        'results': results,
        'created': sum(1 for r in results if r['success']),
        'failed': sum(1 for r in results if not r['success']),
        'used_base_domains': sorted(used_base_domains),
        'list': lst.to_dict() if lst else None
    })

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

    subdomain = generated_label()

    success, message = svc.add_cname(subdomain, domain_id, destination, ttl)

    if success:
        if domain_record:
            domain_record.rotation_count = (domain_record.rotation_count or 0) + 1
            domain_record.last_used_at = datetime.utcnow()
            db.session.commit()
        return jsonify({'success': True, 'subdomain': f"{subdomain}.{base_domain}", 'message': message})
    else:
        return jsonify({'success': False, 'error': message}), 400
