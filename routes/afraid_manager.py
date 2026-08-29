import logging
import json
import re
import threading
from datetime import datetime, timedelta
from flask import Blueprint, request, jsonify, session, render_template, redirect, url_for
from functools import wraps
from faker import Faker
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from database import (
    db,
    AfraidConfig,
    AfraidDomain,
    AfraidResultList,
    AfraidCloudflareDomainUsage,
    ServiceAccount,
    InboxDeliverabilityTest,
    InboxDeliverabilityMessage,
)
from services.afraid_dns_service import AfraidDNSService
from services.cloudflare_dns_service import CloudflareDNSService

logger = logging.getLogger(__name__)
fake = Faker()
MAX_CLOUDFLARE_DESTINATION_USES = 5
AFRAID_DOMAIN_SYNC_INTERVAL = timedelta(hours=1)
_AFRAID_DOMAIN_SYNC_LOCK = threading.Lock()

afraid_manager = Blueprint('afraid_manager', __name__)

def cookie_preview(cookies_str):
    """Return a safe preview that confirms the saved cookie names without exposing values."""
    preview = []
    for pair in (cookies_str or '').split(';'):
        name, separator, _ = pair.strip().partition('=')
        if not separator or not name:
            continue
        preview.append(f"{name}=***")
    return '; '.join(preview[:4]) + ('; ...' if len(preview) > 4 else '')

def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if 'user' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return wrapper

def json_api_errors(f):
    """Keep API failures JSON-shaped so the frontend can report them without an HTML 500 page."""
    @wraps(f)
    def wrapper(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except ValueError as exc:
            db.session.rollback()
            logger.warning('AFRAID API validation error: %s', exc)
            return jsonify({'success': False, 'error': str(exc)}), 400
        except Exception as exc:
            db.session.rollback()
            logger.error('AFRAID API error: %s', exc, exc_info=True)
            return jsonify({'success': False, 'error': f'AFRAID operation failed: {exc}'}), 500
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

def used_cutoff():
    return datetime.utcnow() - timedelta(days=30)

def _completed_inbox_domain_stats():
    """Return observed sender-domain placement counts from completed inbox tests."""
    rows = db.session.query(
        InboxDeliverabilityMessage.workspace_sender,
        InboxDeliverabilityMessage.placement,
    ).join(
        InboxDeliverabilityTest,
        InboxDeliverabilityTest.test_id == InboxDeliverabilityMessage.test_id,
    ).filter(
        func.upper(InboxDeliverabilityTest.status).in_({'COMPLETED', 'PARTIAL'}),
        func.upper(InboxDeliverabilityMessage.placement).in_({'INBOX', 'SPAM'}),
    ).all()
    stats = {}
    for sender, placement in rows:
        sender = (sender or '').strip().lower().rstrip('.')
        if '@' not in sender:
            continue
        domain = sender.rsplit('@', 1)[1].strip().rstrip('.')
        if not domain:
            continue
        item = stats.setdefault(domain, {'inbox': 0, 'spam': 0})
        placement = (placement or '').strip().lower()
        if placement in item:
            item[placement] += 1
    return stats

def _afraid_analytics_tags(domain_names, observed=None):
    """Map AFRAID domains to the spam-inbox tag when completed tests observed them."""
    domain_names = {str(name or '').strip().lower().rstrip('.') for name in (domain_names or []) if str(name or '').strip()}
    if not domain_names:
        return {}
    observed = _completed_inbox_domain_stats() if observed is None else observed
    tags = {}
    for domain_name in domain_names:
        inbox_hits = 0
        spam_hits = 0
        for observed_domain, counts in observed.items():
            if (
                observed_domain == domain_name
                or observed_domain.endswith('.' + domain_name)
                or domain_name.endswith('.' + observed_domain)
            ):
                inbox_hits += counts['inbox']
                spam_hits += counts['spam']
        if inbox_hits or spam_hits:
            tags[domain_name] = {
                'tag': 'spam-inbox',
                'inbox_hits': inbox_hits,
                'spam_hits': spam_hits,
            }
    return tags

def _afraid_analytics_filter_match(analytics, requested_tags):
    """Match an analytics filter against inbox/spam placement counts."""
    requested_tags = {str(tag or '').strip().lower() for tag in (requested_tags or [])}
    if not requested_tags:
        return True
    if 'spam-inbox' in requested_tags and (analytics.get('inbox_hits', 0) or analytics.get('spam_hits', 0)):
        return True
    if 'inbox' in requested_tags and analytics.get('inbox_hits', 0):
        return True
    if 'spam' in requested_tags and analytics.get('spam_hits', 0):
        return True
    return False

def _afraid_analytics_labels(analytics):
    labels = []
    if analytics.get('inbox_hits', 0):
        labels.append('Domain: inbox')
    if analytics.get('spam_hits', 0):
        labels.append('spam-inbox: spam')
    return labels

def _afraid_domain_analytics_fields(domain, tags=None):
    domain_name = (domain.domain_name or '').strip().lower().rstrip('.')
    analytics = (tags or {}).get(domain_name, {})
    return {
        'analytics_tag': analytics.get('tag', ''),
        'analytics_tags': _afraid_analytics_labels(analytics),
        'analytics_inbox_hits': analytics.get('inbox_hits', 0),
        'analytics_spam_hits': analytics.get('spam_hits', 0),
    }

def available_domain_query():
    return AfraidDomain.query.filter(
        AfraidDomain.domain_id.isnot(None),
        AfraidDomain.registry_status == 'public',
        or_(AfraidDomain.last_used_at.is_(None), AfraidDomain.last_used_at < used_cutoff())
    )

def _sync_timestamp_text(value):
    return value.isoformat() + 'Z' if value else None

def sync_afraid_registry_domains(force=False, start_page=1, end_page=212):
    """Refresh the FreeDNS registry cache, removing domains no longer public.

    A successful full refresh records the time on AfraidConfig. Calls made within
    the one-hour freshness window reuse the verified cache instead of hitting
    FreeDNS again.
    """
    config = AfraidConfig.query.first()
    if not config or not config.cookies_str:
        return {'success': False, 'error': 'Afraid cookies not configured. Please import your browser cookies.'}

    now = datetime.utcnow()
    if not force and config.domains_synced_at and now - config.domains_synced_at < AFRAID_DOMAIN_SYNC_INTERVAL:
        return {
            'success': True,
            'skipped': True,
            'message': 'FreeDNS registry cache is still fresh.',
            'synced_at': _sync_timestamp_text(config.domains_synced_at),
            'added': 0,
            'updated': 0,
            'total': AfraidDomain.query.filter(
                AfraidDomain.source == 'registry',
                AfraidDomain.registry_status == 'public',
                AfraidDomain.domain_id.isnot(None),
            ).count(),
            'seen': 0,
            'pages': 0,
            'errors': [],
        }

    with _AFRAID_DOMAIN_SYNC_LOCK:
        db.session.expire_all()
        config = AfraidConfig.query.first()
        now = datetime.utcnow()
        if not force and config and config.domains_synced_at and now - config.domains_synced_at < AFRAID_DOMAIN_SYNC_INTERVAL:
            return {
                'success': True,
                'skipped': True,
                'message': 'FreeDNS registry cache is still fresh.',
                'synced_at': _sync_timestamp_text(config.domains_synced_at),
                'added': 0,
                'updated': 0,
                'total': AfraidDomain.query.filter(
                    AfraidDomain.source == 'registry',
                    AfraidDomain.registry_status == 'public',
                    AfraidDomain.domain_id.isnot(None),
                ).count(),
                'seen': 0,
                'pages': 0,
                'errors': [],
            }

        svc, error = get_service()
        if error:
            return {'success': False, 'error': error}

        start_page = max(1, int(start_page or 1))
        end_page = max(start_page, min(212, int(end_page or 212)))
        added = 0
        updated = 0
        total_seen = 0
        public_seen = 0
        errors = []
        public_domain_names = set()

        for page in range(start_page, end_page + 1):
            try:
                registry_domains = svc.fetch_registry_page(page)
            except Exception as exc:
                errors.append(f'page {page}: {exc}')
                continue

            total_seen += len(registry_domains)
            for item in registry_domains:
                if item.get('status') != 'public':
                    continue
                public_seen += 1
                domain_name = item['domain_name']
                public_domain_names.add(domain_name)
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

        if public_seen == 0:
            db.session.rollback()
            return {
                'success': False,
                'error': '; '.join(errors[:5]) or 'No domains found in the FreeDNS registry pages.',
                'added': added,
                'updated': updated,
                'total': 0,
                'seen': total_seen,
                'pages': end_page - start_page + 1,
                'errors': errors[:10],
            }

        if errors:
            db.session.commit()
            return {
                'success': False,
                'error': f"FreeDNS registry refresh was incomplete ({len(errors)} page error(s)); cached domains were not marked fresh.",
                'added': added,
                'updated': updated,
                'total': public_seen,
                'seen': total_seen,
                'pages': end_page - start_page + 1,
                'errors': errors[:10],
            }

        stale_query = AfraidDomain.query.filter(AfraidDomain.source == 'registry')
        if public_domain_names:
            stale_query = stale_query.filter(~AfraidDomain.domain_name.in_(public_domain_names))
        stale_query.delete(synchronize_session=False)
        config = AfraidConfig.query.first()
        if config:
            config.domains_synced_at = datetime.utcnow()
        db.session.commit()
        return {
            'success': True,
            'skipped': False,
            'message': 'FreeDNS registry cache refreshed successfully.',
            'synced_at': _sync_timestamp_text(config.domains_synced_at if config else None),
            'added': added,
            'updated': updated,
            'total': public_seen,
            'seen': total_seen,
            'pages': end_page - start_page + 1,
            'errors': [],
        }

@afraid_manager.route('/afraid', methods=['GET'])
@login_required
def afraid_page():
    return render_template('afraid.html')

@afraid_manager.route('/api/afraid/config', methods=['GET'])
@login_required
def get_config():
    config = AfraidConfig.query.first()
    if config:
        response = jsonify({
            'success': True,
            'is_configured': bool(config.cookies_str),
            'has_cookies': bool(config.cookies_str),
            'cookies_preview': cookie_preview(config.cookies_str)
        })
        response.headers['Cache-Control'] = 'no-store'
        return response
    response = jsonify({'success': False, 'is_configured': False, 'has_cookies': False})
    response.headers['Cache-Control'] = 'no-store'
    return response

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

    # Cookie replacement should not depend on the add-subdomain form being
    # available. FreeDNS can authenticate successfully while that form is
    # temporarily unavailable, changed, or empty for the account. The
    # protected-page check above is the cookie verification; domain operations
    # perform their own domain-form check when they actually need it.

    config = AfraidConfig.query.first()
    if not config:
        config = AfraidConfig(cookies_str=svc.cookies_str, is_configured=True, domains_synced_at=None)
        db.session.add(config)
    else:
        config.cookies_str = svc.cookies_str
        config.is_configured = True
        config.domains_synced_at = None

    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise

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
    freshness = sync_afraid_registry_domains(force=False)
    if not freshness.get('success'):
        return jsonify(freshness), 503
    tld = request.args.get('tld', '').strip().lower()
    page = max(1, int(request.args.get('page', 1)))
    per_page = min(25, max(1, int(request.args.get('per_page', 5))))
    query = AfraidDomain.query.filter(AfraidDomain.domain_id.isnot(None), AfraidDomain.registry_status == 'public')
    if tld:
        query = query.filter_by(tld=tld)
    total = query.count()
    domains = query.order_by(AfraidDomain.domain_name.asc()).offset((page - 1) * per_page).limit(per_page).all()
    analytics_tags = _afraid_analytics_tags([domain.domain_name for domain in domains])
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
            'used_this_month': bool(d.last_used_at and d.last_used_at >= used_cutoff()),
            'registry_status': d.registry_status,
            'registry_owner': d.registry_owner,
            'hosts_in_use': d.hosts_in_use,
            'registry_age_text': d.registry_age_text,
            'registry_created_on': d.registry_created_on,
            'delivery_status': d.delivery_status or 'inbox',
            **_afraid_domain_analytics_fields(d, analytics_tags)
        } for d in domains]
    })

@afraid_manager.route('/api/afraid/used-domains', methods=['GET'])
@login_required
def get_used_domains():
    domains = AfraidDomain.query.filter(
        AfraidDomain.domain_id.isnot(None),
        AfraidDomain.registry_status == 'public',
        AfraidDomain.last_used_at.isnot(None),
        AfraidDomain.last_used_at >= used_cutoff()
    ).order_by(AfraidDomain.last_used_at.desc(), AfraidDomain.domain_name.asc()).all()
    analytics_tags = _afraid_analytics_tags([domain.domain_name for domain in domains])
    return jsonify({
        'success': True,
        'domains': [{
            'id': d.id,
            'domain_name': d.domain_name,
            'tld': d.tld,
            'hosts_in_use': d.hosts_in_use,
            'registry_created_on': d.registry_created_on,
            'last_used_at': d.last_used_at.isoformat() + 'Z' if d.last_used_at else None,
            'delivery_status': d.delivery_status or 'inbox',
            **_afraid_domain_analytics_fields(d, analytics_tags)
        } for d in domains]
    })

@afraid_manager.route('/api/afraid/used-domains/<int:domain_id>/status', methods=['PUT'])
@login_required
def update_used_domain_status(domain_id):
    data = request.get_json(silent=True) or {}
    status = data.get('delivery_status', '').strip().lower()
    if status not in {'inbox', 'spam', 'bounce'}:
        return jsonify({'success': False, 'error': 'Status must be inbox, spam, or bounce'}), 400
    domain = AfraidDomain.query.get(domain_id)
    if not domain:
        return jsonify({'success': False, 'error': 'Domain not found'}), 404
    domain.delivery_status = status
    db.session.commit()
    return jsonify({'success': True, 'domain': {
        'id': domain.id,
        'domain_name': domain.domain_name,
        'delivery_status': domain.delivery_status
    }})

@afraid_manager.route('/api/afraid/domain-options', methods=['GET'])
@login_required
def get_domain_options():
    freshness = sync_afraid_registry_domains(force=False)
    if not freshness.get('success'):
        return jsonify(freshness), 503
    tld = request.args.get('tld', '').strip().lower()
    limit = min(5000, max(1, int(request.args.get('limit', 1000))))
    include_used = request.args.get('include_used', '').lower() == 'true'
    query = AfraidDomain.query.filter(AfraidDomain.domain_id.isnot(None), AfraidDomain.registry_status == 'public') if include_used else available_domain_query()
    if tld:
        query = query.filter_by(tld=tld)
    analytics_tag = request.args.get('analytics_tag', '').strip().lower()
    requested_analytics_tags = [tag for tag in analytics_tag.split(',') if tag in {'inbox', 'spam', 'spam-inbox'}]
    if requested_analytics_tags:
        candidate_names = [row[0] for row in query.with_entities(AfraidDomain.domain_name).all()]
        observed = _completed_inbox_domain_stats()
        tagged_names = _afraid_analytics_tags(candidate_names, observed)
        matching_names = [
            name for name, analytics in tagged_names.items()
            if _afraid_analytics_filter_match(analytics, requested_analytics_tags)
        ]
        query = query.filter(AfraidDomain.domain_name.in_(matching_names))
    if include_used:
        domains = query.order_by(
            AfraidDomain.last_used_at.is_(None).asc(),
            AfraidDomain.last_used_at.desc(),
            AfraidDomain.domain_name.asc()
        ).limit(limit).all()
    else:
        domains = query.order_by(
            AfraidDomain.rotation_count.asc(),
            AfraidDomain.last_used_at.asc(),
            AfraidDomain.domain_name.asc()
        ).limit(limit).all()
    analytics_tags = _afraid_analytics_tags([domain.domain_name for domain in domains])
    return jsonify({
        'success': True,
        'domains': [{
            'domain_name': d.domain_name,
            'domain_id': d.domain_id,
            'tld': d.tld,
            'used_this_month': bool(d.last_used_at and d.last_used_at >= used_cutoff()),
            'hosts_in_use': d.hosts_in_use,
            'registry_created_on': d.registry_created_on,
            **_afraid_domain_analytics_fields(d, analytics_tags)
        } for d in domains]
    })

@afraid_manager.route('/api/afraid/domain-search', methods=['GET'])
@login_required
def search_afraid_domain():
    freshness = sync_afraid_registry_domains(force=False)
    if not freshness.get('success'):
        return jsonify(freshness), 503
    q = request.args.get('q', '').strip().lower()
    if not q:
        return jsonify({'success': False, 'error': 'Domain search is required'}), 400
    domain = AfraidDomain.query.filter(
        AfraidDomain.domain_id.isnot(None),
        AfraidDomain.registry_status == 'public',
        AfraidDomain.domain_name == q
    ).first()
    if not domain:
        domain = AfraidDomain.query.filter(
            AfraidDomain.domain_id.isnot(None),
            AfraidDomain.registry_status == 'public',
            AfraidDomain.domain_name.ilike(f"%{q}%")
        ).order_by(AfraidDomain.domain_name.asc()).first()
    if not domain:
        return jsonify({'success': False, 'error': 'Domain not found in cached public FreeDNS registry'}), 404
    return jsonify({'success': True, 'domain': {
        'domain_name': domain.domain_name,
        'tld': domain.tld,
        'used_this_month': bool(domain.last_used_at and domain.last_used_at >= used_cutoff())
    }})

@afraid_manager.route('/api/afraid/service-accounts', methods=['GET'])
@login_required
def get_afraid_service_accounts():
    accounts = ServiceAccount.query.filter_by(is_active=True).order_by(ServiceAccount.name.asc()).all()
    return jsonify({
        'success': True,
        'accounts': [
            {
                'id': account.id,
                'name': account.name,
                'admin_email': account.admin_email
            }
            for account in accounts
        ]
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

@afraid_manager.route('/api/afraid/domains/reactivate', methods=['POST'])
@login_required
def reactivate_domains():
    data = request.get_json(silent=True) or {}
    domain_names = [d.strip().lower() for d in data.get('domain_names', []) if d.strip()]
    domain_ids = [int(d) for d in data.get('domain_ids', []) if str(d).isdigit()]
    query = AfraidDomain.query
    if domain_names:
        query = query.filter(AfraidDomain.domain_name.in_(domain_names))
    elif domain_ids:
        query = query.filter(AfraidDomain.id.in_(domain_ids))
    else:
        return jsonify({'success': False, 'error': 'Select at least one used domain to reactivate'}), 400
    domains = query.all()
    for domain in domains:
        domain.last_used_at = None
    db.session.commit()
    return jsonify({
        'success': True,
        'reactivated': len(domains),
        'domains': [domain.domain_name for domain in domains]
    })

@afraid_manager.route('/api/afraid/fetch-domains', methods=['POST'])
@login_required
@json_api_errors
def fetch_domains_from_afraid():
    data = request.get_json(silent=True) or {}
    result = sync_afraid_registry_domains(
        force=bool(data.get('force', True)),
        start_page=data.get('start_page') or 1,
        end_page=data.get('end_page') or 212,
    )
    return jsonify(result), 200 if result.get('success') else 400

@afraid_manager.route('/api/afraid/tlds', methods=['GET'])
@login_required
def get_tld_groups():
    freshness = sync_afraid_registry_domains(force=False)
    if not freshness.get('success'):
        return jsonify(freshness), 503
    rows = db.session.query(AfraidDomain.tld, func.count(AfraidDomain.id)).filter(
        AfraidDomain.tld.isnot(None),
        AfraidDomain.domain_id.isnot(None),
        AfraidDomain.registry_status == 'public'
    ).group_by(AfraidDomain.tld).order_by(AfraidDomain.tld.asc()).all()
    used_rows = db.session.query(AfraidDomain.tld, func.count(AfraidDomain.id)).filter(
        AfraidDomain.tld.isnot(None),
        AfraidDomain.last_used_at >= used_cutoff(),
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
        zone_names = [z['name'].strip().lower() for z in zones if z.get('name')]
        usage_rows = {
            row.domain_name: row
            for row in AfraidCloudflareDomainUsage.query.filter(AfraidCloudflareDomainUsage.domain_name.in_(zone_names)).all()
        } if zone_names else {}
        return jsonify({
            'success': True,
            'domains': [{
                'name': z['name'],
                'id': z['id'],
                'status': z.get('status'),
                'afraid_use_count': usage_rows.get(z['name'].strip().lower()).use_count if usage_rows.get(z['name'].strip().lower()) else 0,
                'afraid_skipped': (usage_rows.get(z['name'].strip().lower()).use_count if usage_rows.get(z['name'].strip().lower()) else 0) >= MAX_CLOUDFLARE_DESTINATION_USES
            } for z in zones],
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

def get_or_create_cloudflare_usage(domain_name):
    domain_name = (domain_name or '').strip().lower()
    usage = AfraidCloudflareDomainUsage.query.filter_by(domain_name=domain_name).first()
    if not usage:
        usage = AfraidCloudflareDomainUsage(domain_name=domain_name, use_count=0)
        db.session.add(usage)
    return usage

def select_cloudflare_destinations(zone_names, limit, manual_destinations=None):
    ordered_names = []
    for name in (manual_destinations or []) + (zone_names or []):
        clean = (name or '').strip().lower()
        if clean and clean not in ordered_names:
            ordered_names.append(clean)

    usage_rows = {row.domain_name: row for row in AfraidCloudflareDomainUsage.query.filter(
        AfraidCloudflareDomainUsage.domain_name.in_(ordered_names)
    ).all()} if ordered_names else {}

    for name in ordered_names:
        if name not in usage_rows:
            usage_rows[name] = get_or_create_cloudflare_usage(name)

    available = [
        name for name in ordered_names
        if (usage_rows[name].use_count or 0) < MAX_CLOUDFLARE_DESTINATION_USES
    ]

    manual_set = set(manual_destinations or [])
    if manual_set:
        selected = [name for name in ordered_names if name in manual_set and name in available]
        if len(selected) < len(manual_set):
            exhausted = sorted(manual_set - set(selected))
            return [], f"Cloudflare destination(s) reached {MAX_CLOUDFLARE_DESTINATION_USES} process uses and were skipped: {', '.join(exhausted)}"
        return selected[:limit], None

    selected = sorted(
        available,
        key=lambda name: (usage_rows[name].use_count or 0, usage_rows[name].last_used_at or datetime.min, name)
    )[:limit]
    return selected, None

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
    try:
        db.session.commit()
    except IntegrityError:
        # Two create requests for the same user can calculate the same counter
        # concurrently. Preserve the completed CNAME batch with a unique suffix.
        db.session.rollback()
        lst.name = f"{prefix}-{existing + 1}-{datetime.utcnow().strftime('%H%M%S%f')}"
        db.session.add(lst)
        db.session.commit()
    return lst

def _afraid_domains_in_result_list(lst):
    """Return AFRAID base-domain names represented by successful list rows."""
    rows = []
    if lst.results_json:
        try:
            parsed = json.loads(lst.results_json)
            if isinstance(parsed, list):
                rows = [item for item in parsed if isinstance(item, dict) and item.get('success', True)]
        except (TypeError, ValueError):
            rows = []
    if not rows:
        rows = [
            {'subdomain': line.split(',', 1)[0].strip()}
            for line in (lst.raw_results or '').splitlines()
            if line.strip()
        ]

    known_domains = sorted(
        (domain.domain_name for domain in AfraidDomain.query.filter(
            AfraidDomain.registry_status == 'public',
            AfraidDomain.domain_id.isnot(None),
        ).all()),
        key=len,
        reverse=True,
    )
    matched = set()
    for row in rows:
        direct_name = (row.get('base_domain') or '').strip().lower().rstrip('.')
        if direct_name:
            matched.add(direct_name)
            continue
        fqdn = (row.get('subdomain') or '').strip().lower().rstrip('.')
        for domain_name in known_domains:
            if fqdn == domain_name or fqdn.endswith('.' + domain_name):
                matched.add(domain_name)
                break
    return matched

def mark_afraid_domains_used(domain_names, used_at=None):
    domain_names = {str(name).strip().lower().rstrip('.') for name in (domain_names or []) if str(name).strip()}
    if not domain_names:
        return 0
    used_at = used_at or datetime.utcnow()
    domains = AfraidDomain.query.filter(AfraidDomain.domain_name.in_(domain_names)).all()
    for domain in domains:
        domain.last_used_at = used_at
    return len(domains)

@afraid_manager.route('/api/afraid/lists', methods=['GET'])
@login_required
def get_afraid_lists():
    lists = AfraidResultList.query.order_by(AfraidResultList.created_at.desc()).all()
    return jsonify({'success': True, 'lists': [lst.to_dict() for lst in lists]})

@afraid_manager.route('/api/afraid/lists/<int:list_id>/used', methods=['POST'])
@login_required
def mark_afraid_list_used(list_id):
    """Persist that a saved generated list was sent through All or Pick."""
    lst = AfraidResultList.query.get(list_id)
    if not lst:
        return jsonify({'success': False, 'error': 'Afraid list not found'}), 404
    data = request.get_json(silent=True) or {}
    mode = (data.get('mode') or '').strip().lower()
    if mode not in {'all', 'pick'}:
        return jsonify({'success': False, 'error': 'Usage mode must be all or pick'}), 400
    used_at = datetime.utcnow()
    lst.is_used = True
    lst.used_at = used_at
    lst.used_mode = mode
    domains_marked_used = mark_afraid_domains_used(_afraid_domains_in_result_list(lst), used_at)
    db.session.commit()
    return jsonify({'success': True, 'list': lst.to_dict(), 'domains_marked_used': domains_marked_used})

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
    lst.is_used = False
    lst.used_at = None
    lst.used_mode = None
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

def normalize_subdomain_label(value, base_domain=None):
    label = (value or '').strip().lower().rstrip('.')
    base_domain = (base_domain or '').strip().lower().rstrip('.')
    if base_domain and label.endswith(f".{base_domain}"):
        label = label[:-(len(base_domain) + 1)]
    label = label.strip('.')
    if not label:
        return ''
    label_pattern = r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?'
    if not re.fullmatch(rf'{label_pattern}(?:\.{label_pattern})*', label):
        raise ValueError(f"Invalid subdomain '{value}'. Use DNS-safe labels with letters, numbers, hyphens, and optional dots.")
    return label

def parse_manual_subdomain_labels(values):
    labels = []
    seen = set()
    duplicates = []
    for raw in values or []:
        label = (raw or '').strip().lower().rstrip('.')
        if not label:
            continue
        if label in seen:
            duplicates.append(label)
            continue
        seen.add(label)
        labels.append(label)
    if duplicates:
        raise ValueError(f"Duplicate manual subdomain(s): {', '.join(sorted(set(duplicates)))}")
    return labels

def normalize_domain_name(value):
    domain = (value or '').strip().lower().rstrip('.')
    if not domain:
        return ''
    if not re.fullmatch(r'(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', domain):
        raise ValueError(f"Invalid domain '{value}'. Enter one domain per line, like example.com.")
    return domain

def unique_generated_label(base_domain, used_fqdns, max_attempts=100):
    for _ in range(max_attempts):
        label = normalize_subdomain_label(generated_label())
        fqdn = f"{label}.{base_domain}".lower()
        if fqdn not in used_fqdns:
            return label
    raise ValueError("Could not generate a unique subdomain label after several attempts. Try again.")

def is_quota_error(message):
    text = (message or '').lower()
    exact_terms = ['no more subdomain capacity', 'more hostnames', 'subdomain capacity allocated']
    quota_terms = ['quota', 'limit', 'maximum', 'max', 'too many', 'capacity', 'allocated', '50']
    record_terms = ['subdomain', 'record', 'dns', 'entries', 'entry', 'hostname', 'hostnames']
    if any(term in text for term in exact_terms):
        return True
    return any(term in text for term in quota_terms) and any(term in text for term in record_terms)

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
@json_api_errors
def create_batch_subdomains():
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({'success': False, 'error': 'Invalid CNAME creation payload.'}), 400
    freshness = sync_afraid_registry_domains(force=False)
    if not freshness.get('success'):
        return jsonify({
            'success': False,
            'error': f"Cannot create CNAME subdomains until the FreeDNS registry is refreshed: {freshness.get('error', 'refresh failed')}"
        }), 503
    tld = str(data.get('tld') or '').strip().lower()
    try:
        base_domain = normalize_domain_name(data.get('base_domain', ''))
        base_domains = []
        raw_base_domains = data.get('base_domains') or []
        if not isinstance(raw_base_domains, list):
            raise ValueError('Base domains must be provided as a list.')
        for raw_domain in raw_base_domains:
            domain_name = normalize_domain_name(raw_domain)
            if domain_name and domain_name not in base_domains:
                base_domains.append(domain_name)
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    try:
        selected_registry_domains = set()
        raw_selected_registry_domains = data.get('selected_registry_domains') or []
        if not isinstance(raw_selected_registry_domains, list):
            raise ValueError('Selected registry domains must be provided as a list.')
        for raw_domain in raw_selected_registry_domains:
            domain_name = normalize_domain_name(raw_domain)
            if domain_name:
                selected_registry_domains.add(domain_name)
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    if base_domain and base_domain not in base_domains:
        base_domains.append(base_domain)
    if selected_registry_domains:
        current_registry_domains = {
            row.domain_name
            for row in AfraidDomain.query.filter(
                AfraidDomain.domain_name.in_(selected_registry_domains),
                AfraidDomain.source == 'registry',
                AfraidDomain.registry_status == 'public',
                AfraidDomain.domain_id.isnot(None),
            ).all()
        }
        missing_registry_domains = sorted(selected_registry_domains - current_registry_domains)
        if missing_registry_domains:
            return jsonify({
                'success': False,
                'error': 'The following selected FreeDNS domain(s) are no longer available after the latest registry sync: '
                         + ', '.join(missing_registry_domains)
            }), 409
    try:
        raw_afraid_count = data.get('afraid_count')
        afraid_count = None if raw_afraid_count in (None, '') else max(1, min(5000, int(raw_afraid_count)))
        raw_cloudflare_count = data.get('cloudflare_count')
        cloudflare_count = max(1, min(50, int(raw_cloudflare_count or 1)))
        raw_total_count = data.get('total_count')
        total_count_value = max(1, min(50, int(raw_total_count or afraid_count or 1)))
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': 'Domain and subdomain counts must be valid numbers.'}), 400
    ttl = data.get('ttl', 300)
    raw_destinations = data.get('destinations') or []
    if not isinstance(raw_destinations, list):
        return jsonify({'success': False, 'error': 'Cloudflare destinations must be provided as a list.'}), 400
    manual_destinations = [str(d).strip().lower() for d in raw_destinations if str(d).strip()]
    try:
        raw_subdomains = data.get('subdomains') or []
        if not isinstance(raw_subdomains, list):
            raise ValueError('Manual subdomains must be provided as a list.')
        manual_subdomains = parse_manual_subdomain_labels(raw_subdomains)
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    total_count = len(manual_subdomains) if manual_subdomains else total_count_value

    svc, error = get_service()
    if error:
        return jsonify({'success': False, 'error': error}), 401

    if base_domains:
        afraid_domains = []
        for selected_domain in base_domains:
            domain_record = AfraidDomain.query.filter_by(domain_name=selected_domain).first()
            if not domain_record:
                domain_record = AfraidDomain(
                    domain_name=selected_domain,
                    tld=selected_domain.rsplit('.', 1)[-1] if '.' in selected_domain else None,
                    source='manual'
                )
                db.session.add(domain_record)
            # Manual domains are an explicit override. Do not block them because
            # the local rotation cache says they were recently used.
            if not domain_record.domain_id:
                domain_record.domain_id = svc.get_domain_id(selected_domain)
            if not domain_record.domain_id:
                return jsonify({
                    'success': False,
                    'error': svc.last_error or f"FreeDNS domain '{selected_domain}' is not a usable public registry domain."
                }), 400
            if domain_record.registry_status not in ('public', None):
                return jsonify({'success': False, 'error': f"FreeDNS domain '{selected_domain}' is not a usable public registry domain."}), 400
            domain_record.registry_status = 'public'
            afraid_domains.append(domain_record)
    else:
        afraid_domains = get_rotated_afraid_domains(tld, afraid_count or 5000)
    if not afraid_domains:
        return jsonify({'success': False, 'error': f"No cached FreeDNS domains found for TLD '{tld}'."}), 404

    try:
        cf_zones = [z['name'] for z in CloudflareDNSService().get_zones()]
    except Exception as e:
        cf_zones = []

    destinations, destination_error = select_cloudflare_destinations(
        cf_zones,
        cloudflare_count,
        manual_destinations=manual_destinations
    )
    if destination_error:
        return jsonify({'success': False, 'error': destination_error}), 400
    if not destinations:
        return jsonify({'success': False, 'error': f'No Cloudflare destination domains available under {MAX_CLOUDFLARE_DESTINATION_USES} Afraid process uses.'}), 400

    results = []
    used_base_domains = set()
    used_destinations = set()
    existing_fqdns = {
        (record.get('fqdn') or '').strip().lower().rstrip('.')
        for record in svc.get_existing_subdomains()
        if record.get('fqdn')
    }
    planned_fqdns = set(existing_fqdns)
    manual_plan = []
    if manual_subdomains:
        for index, raw_label in enumerate(manual_subdomains):
            domain_record = afraid_domains[index % len(afraid_domains)]
            try:
                label = normalize_subdomain_label(raw_label, domain_record.domain_name)
            except ValueError as e:
                return jsonify({'success': False, 'error': str(e)}), 400
            fqdn = f"{label}.{domain_record.domain_name}".lower()
            if fqdn in planned_fqdns:
                return jsonify({'success': False, 'error': f"Duplicate subdomain blocked: {fqdn}"}), 400
            planned_fqdns.add(fqdn)
            manual_plan.append((label, fqdn))
    else:
        planned_fqdns = set(existing_fqdns)
    quota_reached = False
    quota_message = ''
    for index in range(total_count):
        domain_record = afraid_domains[index % len(afraid_domains)]
        destination = destinations[index % len(destinations)]
        try:
            if manual_subdomains:
                label, fqdn = manual_plan[index]
            else:
                label = unique_generated_label(domain_record.domain_name, planned_fqdns)
                fqdn = f"{label}.{domain_record.domain_name}".lower()
                planned_fqdns.add(fqdn)
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        try:
            success, message = svc.add_cname(label, domain_record.domain_id, destination, ttl)
        except Exception as exc:
            success = False
            message = f'FreeDNS CNAME request failed: {exc}'
            logger.error('AFRAID CNAME request failed for %s: %s', fqdn, exc, exc_info=True)
        result = {
            'success': success,
            'subdomain': fqdn,
            'base_domain': domain_record.domain_name,
            'destination': destination,
            'message': message
        }
        results.append(result)
        if success:
            domain_record.rotation_count = (domain_record.rotation_count or 0) + 1
            domain_record.last_used_at = datetime.utcnow()
            domain_record.delivery_status = 'inbox'
            used_base_domains.add(domain_record.domain_name)
            used_destinations.add(destination)
        elif is_quota_error(message):
            quota_reached = True
            quota_message = message or 'FreeDNS quota reached. Cleanup existing subdomains before continuing.'
            break
    for destination in used_destinations:
        usage = get_or_create_cloudflare_usage(destination)
        usage.use_count = (usage.use_count or 0) + 1
        usage.last_used_at = datetime.utcnow()
    domains_marked_used = mark_afraid_domains_used(used_base_domains)
    db.session.commit()
    lst = create_afraid_result_list(results)
    return jsonify({
        'success': True,
        'results': results,
        'created': sum(1 for r in results if r['success']),
        'failed': sum(1 for r in results if not r['success']),
        'quota_reached': quota_reached,
        'quota_message': quota_message,
        'used_destinations': sorted(used_destinations),
        'used_base_domains': sorted(used_base_domains),
        'domains_marked_used': domains_marked_used,
        'list': lst.to_dict() if lst else None
    })

@afraid_manager.route('/api/afraid/create-subdomain', methods=['POST'])
@login_required
def create_subdomain():
    data = request.get_json()
    freshness = sync_afraid_registry_domains(force=False)
    if not freshness.get('success'):
        return jsonify({
            'success': False,
            'error': f"Cannot create a CNAME subdomain until the FreeDNS registry is refreshed: {freshness.get('error', 'refresh failed')}"
        }), 503
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

    existing_fqdns = {
        (record.get('fqdn') or '').strip().lower().rstrip('.')
        for record in svc.get_existing_subdomains()
        if record.get('fqdn')
    }
    try:
        subdomain = unique_generated_label(base_domain, existing_fqdns)
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400

    success, message = svc.add_cname(subdomain, domain_id, destination, ttl)

    if success:
        if domain_record:
            domain_record.rotation_count = (domain_record.rotation_count or 0) + 1
            domain_record.last_used_at = datetime.utcnow()
            db.session.commit()
        return jsonify({'success': True, 'subdomain': f"{subdomain}.{base_domain}", 'message': message})
    else:
        return jsonify({'success': False, 'error': message}), 400
