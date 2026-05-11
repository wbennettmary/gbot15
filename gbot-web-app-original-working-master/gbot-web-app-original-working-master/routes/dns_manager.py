"""
DNS Manager routes for domain addition and verification.
"""
import logging
import uuid
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from flask import Blueprint, request, jsonify, session, redirect, url_for
from functools import wraps
from database import db, DomainOperation, GoogleAccount, ServiceAccount, CloudflareConfig
from services.zone_utils import to_apex
from services.google_domains_service import GoogleDomainsService
from services.namecheap_dns_service import NamecheapDNSService
from services.cloudflare_dns_service import CloudflareDNSService

logger = logging.getLogger(__name__)

dns_manager = Blueprint('dns_manager', __name__)

# Login required decorator (matches app.py implementation)
def login_required(f):
    """Decorator to require login"""
    @wraps(f)
    def wrapper(*args, **kwargs):
        if 'user' not in session:
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return wrapper

# Store active jobs
active_jobs = {}
job_lock = threading.Lock()

def process_domain_verification(job_id: str, domain: str, account_name: str, dry_run: bool, skip_verified: bool, provider: str = 'namecheap', stop_event=None):
    """
    Process domain verification for a single domain in background thread.
    REWRITTEN to use SimpleDomainService
    """
    # Create Flask app context for background thread
    from app import app
    from database import db, ServiceAccount, DomainOperation
    from services.simple_domain_service import SimpleDomainService
    from services.cloudflare_dns_service import CloudflareDNSService
    from services.namecheap_dns_service import NamecheapDNSService
    
    with app.app_context():
        # Check stop event at start
        if stop_event and stop_event.is_set():
            logger.info(f"Job {job_id}: Domain {domain} processing stopped by user (before start)")
            return

        operation_id = str(uuid.uuid4())
        operation = DomainOperation(
            id=operation_id,
            job_id=job_id,
            input_domain=domain,
            apex_domain='',
            workspace_status='pending',
            dns_status='pending',
            verify_status='pending',
            message='Initializing...',
            raw_log=[]
        )
        db.session.add(operation)
        db.session.commit()
        
        logger.info(f"Job {job_id}: Started processing domain {domain} (Operation {operation_id})")
        
        log_entry = lambda step, status, msg: {
            'step': step,
            'status': status,
            'message': msg,
            'timestamp': datetime.now().isoformat()
        }
        
        try:
            # Step 1: Find Service Account
            sa = ServiceAccount.query.filter_by(name=account_name).first()
            if not sa:
                # Try fallback lookup
                sa = ServiceAccount.query.filter_by(admin_email=account_name).first()
            
            if not sa:
                error_msg = f"Service Account '{account_name}' not found"
                operation.workspace_status = 'failed'
                operation.message = error_msg
                operation.raw_log.append(log_entry('auth', 'failed', error_msg))
                db.session.commit()
                return

            operation.message = f"Authenticated as {sa.admin_email}"
            operation.raw_log.append(log_entry('auth', 'success', f"Using Service Account: {sa.name}"))
            db.session.commit()

            # Initialize Service
            svc = SimpleDomainService(sa.json_content, sa.admin_email)

            if stop_event and stop_event.is_set(): return

            # Step 2: Full Process (Add + Token)
            # This handles the "Add Full Domain" logic correctly
            operation.message = "Adding domain to Workspace..."
            db.session.commit()
            
            # SimpleDomainService.full_process returns { 'apex_domain', 'txt_host', 'add_success', 'token', ... }
            result = svc.full_process(domain)
            
            # Identify Apex/Host from result
            apex = result.get('apex_domain', domain)
            txt_host = result.get('txt_host', '@')
            operation.apex_domain = apex
            
            # Check Add Status
            if result['add_success']:
                operation.workspace_status = 'success'
                operation.raw_log.append(log_entry('workspace', 'success', result['add_message']))
            else:
                operation.workspace_status = 'failed'
                operation.message = result['add_message']
                operation.raw_log.append(log_entry('workspace', 'failed', result['add_message']))
                db.session.commit()
                return

            # Check Token Status
            token = result.get('token')
            if token:
                operation.message = "Token received, creating DNS..."
                operation.txt_record_value = token # This is the full txt value (e.g. google-site-verification=...)
                operation.raw_log.append(log_entry('token', 'success', result['token_message']))
            else:
                operation.dns_status = 'failed' 
                operation.verify_status = 'failed'
                operation.message = result.get('token_message', 'Failed to get token')
                operation.raw_log.append(log_entry('token', 'failed', operation.message))
                db.session.commit()
                return

            db.session.commit()

            if stop_event and stop_event.is_set(): return

            # Step 3: Create DNS Record
            if dry_run:
                operation.dns_status = 'dry-run'
                operation.message = f"Dry-run: Would add TXT {txt_host} to {apex}"
                operation.raw_log.append(log_entry('dns', 'dry-run', f"Dry-run: TXT @ {txt_host} = {token}"))
            else:
                operation.message = f"Adding DNS record ({provider})..."
                db.session.commit()
                
                try:
                    dns_res = None
                    if provider == 'cloudflare':
                        dns_svc_cf = CloudflareDNSService()
                        dns_res = dns_svc_cf.upsert_txt_record(apex, txt_host, token, ttl=1)
                    else:
                        dns_svc_nc = NamecheapDNSService()
                        dns_res = dns_svc_nc.upsert_txt_record(apex, txt_host, token, ttl=1799)
                    
                    operation.dns_status = 'success'
                    operation.raw_log.append(log_entry('dns', 'success', f"DNS created: {dns_res}"))
                    operation.message = "DNS record created. Verifying..."
                except Exception as e:
                    operation.dns_status = 'failed'
                    operation.message = f"DNS Error: {str(e)}"
                    operation.raw_log.append(log_entry('dns', 'failed', str(e)))
                    db.session.commit()
                    return
            
            db.session.commit()

            # Step 4: Verification Loop
            if stop_event and stop_event.is_set(): return
            
            if not dry_run:
                # Wait for propagation (short wait initially)
                time.sleep(10)
                
                verified = False
                max_attempts = 10
                
                for attempt in range(1, max_attempts + 1):
                    if stop_event and stop_event.is_set(): return
                    
                    operation.message = f"Verifying... (Attempt {attempt}/{max_attempts})"
                    db.session.commit()
                    
                    # Use SimpleDomainService to verify
                    is_verified, v_msg = svc.verify_domain(domain)
                    
                    if is_verified:
                        verified = True
                        operation.verify_status = 'success'
                        operation.message = "Domain verified successfully!"
                        operation.raw_log.append(log_entry('verify', 'success', f"Verified on attempt {attempt}"))
                        break
                    else:
                        operation.raw_log.append(log_entry('verify', 'pending', f"Attempt {attempt}: {v_msg}"))
                        if attempt < max_attempts:
                            time.sleep(30) # Wait between retries
                
                if not verified:
                    operation.verify_status = 'failed'
                    operation.message = "Verification timed out."
                    operation.raw_log.append(log_entry('verify', 'failed', "Max attempts reached"))
            else:
                operation.verify_status = 'skipped'
            
            db.session.commit()

        except Exception as e:
            db.session.rollback()
            logger.error(f"Error in process_domain_verification: {e}", exc_info=True)
            operation.message = f"System Error: {str(e)}"
            operation.raw_log.append(log_entry('error', 'failed', str(e)))
            db.session.commit()

@dns_manager.route('/api/domains/add-and-verify', methods=['POST'])
@login_required
def add_and_verify_domains():
    """
    Start domain addition and verification process.
    
    Request body:
        {
            "domains": ["example.com", "sub.team.io"],
            "dryRun": false,
            "skipVerified": true,
            "provider": "namecheap" (or "cloudflare")
        }
    
    Returns:
        {
            "job_id": "<uuid>",
            "accepted": <n>
        }
    """
    try:
        data = request.get_json()
        domains = data.get('domains', [])
        dry_run = data.get('dryRun', False)
        skip_verified = data.get('skipVerified', True)
        provider = data.get('provider', 'namecheap') # Default to namecheap
        
        if not domains:
            return jsonify({'success': False, 'error': 'No domains provided'}), 400
        
        # Get current account name from session
        account_name = session.get('current_account_name')
        if not account_name:
            return jsonify({'success': False, 'error': 'No authenticated account'}), 401
        
        # Verify account exists (Check Service Account first, then Google Account)
        service_account = ServiceAccount.query.filter_by(name=account_name).first()
        account = None
        
        if not service_account:
            # Fallback to old Google Account (deprecated but kept for compatibility)
            account = GoogleAccount.query.filter_by(account_name=account_name).first()
            
        if not service_account and not account:
            return jsonify({'success': False, 'error': 'Account not found'}), 404
        
        # Normalize domains: trim, lowercase, remove duplicates, ignore empty
        normalized_domains = []
        seen = set()
        for domain in domains:
            domain = domain.strip().lower()
            if domain and domain not in seen:
                normalized_domains.append(domain)
                seen.add(domain)
        
        if not normalized_domains:
            return jsonify({'success': False, 'error': 'No valid domains after normalization'}), 400
        
        # Create job
        job_id = str(uuid.uuid4())
        
        with job_lock:
            active_jobs[job_id] = {
                'status': 'running',
                'total': len(normalized_domains),
                'started_at': datetime.now().isoformat(),
                'stop_event': threading.Event()
            }
        
        # Stop any other running jobs
        with job_lock:
            for jid, job in active_jobs.items():
                if jid != job_id and job.get('status') == 'running':
                    logger.info(f"Stopping existing job {jid} to start new job {job_id}")
                    if 'stop_event' in job:
                        job['stop_event'].set()
                    job['status'] = 'stopped'
        
        # Start background processing in a separate thread to allow immediate return
        def run_batch():
            # Create app context for the batch thread
            from app import app
            with app.app_context():
                max_workers = min(5, len(normalized_domains))  # Cap at 5 parallel domains
                logger.info(f"Job {job_id}: Starting batch processing with {max_workers} workers")
                
                try:
                    with ThreadPoolExecutor(max_workers=max_workers) as executor:
                        futures = []
                        for domain in normalized_domains:
                            # Check stop event before submitting
                            if active_jobs[job_id]['stop_event'].is_set():
                                logger.info(f"Job {job_id}: Stop event detected, halting submission")
                                break
                                
                            future = executor.submit(
                                process_domain_verification,
                                job_id,
                                domain,
                                account_name,
                                dry_run,
                                skip_verified,
                                provider,
                                active_jobs[job_id]['stop_event']
                            )
                            futures.append(future)
                        
                        # Wait for all tasks to complete and check for exceptions
                        for future in futures:
                            try:
                                future.result()
                            except Exception as exc:
                                logger.error(f"Job {job_id}: Thread generated an exception: {exc}")
                        
                    # Update final status
                    with job_lock:
                        if job_id in active_jobs:
                            if active_jobs[job_id]['stop_event'].is_set():
                                active_jobs[job_id]['status'] = 'stopped'
                                logger.info(f"Job {job_id}: Marked as stopped")
                            else:
                                active_jobs[job_id]['status'] = 'completed'
                                logger.info(f"Job {job_id}: Marked as completed")
                                
                except Exception as e:
                    logger.error(f"Job {job_id}: Error in batch processing: {e}", exc_info=True)
                    with job_lock:
                        if job_id in active_jobs:
                            active_jobs[job_id]['status'] = 'failed'

        # Start the batch thread
        batch_thread = threading.Thread(target=run_batch)
        batch_thread.daemon = True
        batch_thread.start()
        
        logger.info(f"Started domain verification job {job_id} for {len(normalized_domains)} domains (Provider: {provider})")
        
        return jsonify({
            'success': True,
            'job_id': job_id,
            'accepted': len(normalized_domains)
        })
    
    except Exception as e:
        logger.error(f"Error starting domain verification: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@dns_manager.route('/api/domains/stop', methods=['POST'])
@login_required
def stop_domain_verification():
    """
    Stop a running domain verification job.
    """
    try:
        data = request.get_json()
        job_id = data.get('job_id')
        
        count = 0
        with job_lock:
            if job_id:
                # Stop specific job
                if job_id in active_jobs and active_jobs[job_id]['status'] == 'running':
                    if 'stop_event' in active_jobs[job_id]:
                        active_jobs[job_id]['stop_event'].set()
                    active_jobs[job_id]['status'] = 'stopped'
                    count = 1
            else:
                # Stop ALL running jobs
                for jid, job in active_jobs.items():
                    if job.get('status') == 'running':
                        if 'stop_event' in job:
                            job['stop_event'].set()
                        job['status'] = 'stopped'
                        count += 1
        
        return jsonify({'success': True, 'stopped_count': count})
    except Exception as e:
        logger.error(f"Error stopping domain verification: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@dns_manager.route('/api/domains/verify-unverified', methods=['POST'])
@login_required
def verify_unverified_domains():
    """
    Verify all domains that are not yet verified in Google Workspace.
    This endpoint:
    1. Fetches all domains from Admin SDK
    2. Filters to only unverified domains
    3. Triggers verification for each in parallel
    """
    try:
        data = request.get_json() or {}
        
        # Get current account name from session
        account_name = session.get('current_account_name')
        if not account_name:
            return jsonify({'success': False, 'error': 'No authenticated account'}), 401
        
        logger.info(f"=== VERIFY UNVERIFIED DOMAINS === Account: {account_name}")
        
        # Verify account exists (Check Service Account first)
        service_account = ServiceAccount.query.filter_by(name=account_name).first()
        if not service_account:
            account = GoogleAccount.query.filter_by(account_name=account_name).first()
            if not account:
                return jsonify({'success': False, 'error': 'Account not found'}), 404
        
        # Initialize Google Domains Service
        google_service = GoogleDomainsService(account_name=account_name)
        
        # Get all domains from Admin SDK
        try:
            admin_service = google_service._get_admin_service()
            response = admin_service.domains().list(customer='my_customer').execute()
            all_domains = response.get('domains', [])
            logger.info(f"Found {len(all_domains)} total domains in Workspace")
        except Exception as e:
            logger.error(f"Error fetching domains from Admin SDK: {e}")
            return jsonify({'success': False, 'error': f'Error fetching domains: {str(e)}'}), 500
        
        # Filter to unverified domains only
        unverified_domains = []
        for domain in all_domains:
            domain_name = domain.get('domainName', '')
            is_verified = domain.get('verified', False)
            logger.info(f"Domain: {domain_name} - Verified: {is_verified}")
            if not is_verified and domain_name:
                unverified_domains.append(domain_name)
        
        if not unverified_domains:
            logger.info("No unverified domains found")
            return jsonify({
                'success': True,
                'message': 'All domains are already verified!',
                'total_domains': 0,
                'domains': []
            })
        
        logger.info(f"Found {len(unverified_domains)} unverified domains: {unverified_domains}")
        
        # Create job for verification
        job_id = str(uuid.uuid4())
        
        with job_lock:
            active_jobs[job_id] = {
                'status': 'running',
                'total': len(unverified_domains),
                'started_at': datetime.now().isoformat(),
                'stop_event': threading.Event()
            }
        
        # Start background processing for verification
        def verify_domains_batch():
            from app import app
            with app.app_context():
                max_workers = min(5, len(unverified_domains))
                logger.info(f"Job {job_id}: Starting parallel verification with {max_workers} workers")
                
                try:
                    with ThreadPoolExecutor(max_workers=max_workers) as executor:
                        futures = []
                        for domain in unverified_domains:
                            if active_jobs[job_id]['stop_event'].is_set():
                                logger.info(f"Job {job_id}: Stop requested during submission")
                                break
                            
                            # Create operation record
                            operation = DomainVerificationOperation(
                                job_id=job_id,
                                domain=domain,
                                apex_domain=domain,  # These are apex domains from Admin SDK
                                account_name=account_name,
                                workspace_status='skipped',  # Domain already added
                                dns_status='skipped',  # TXT record already exists
                                verify_status='pending',
                                message='Starting verification...',
                                raw_log=[]
                            )
                            db.session.add(operation)
                            db.session.commit()
                            
                            # Submit verification task
                            future = executor.submit(
                                verify_single_domain,
                                job_id,
                                domain,
                                account_name,
                                active_jobs[job_id]['stop_event']
                            )
                            futures.append((domain, future))
                        
                        # Wait for all futures to complete
                        for domain, future in futures:
                            try:
                                result = future.result(timeout=300)  # 5 min timeout per domain
                                logger.info(f"Job {job_id}: Verification result for {domain}: {result}")
                            except Exception as exc:
                                logger.error(f"Job {job_id}: Verification error for {domain}: {exc}")
                        
                        # Update final status
                        with job_lock:
                            if job_id in active_jobs:
                                if active_jobs[job_id]['stop_event'].is_set():
                                    active_jobs[job_id]['status'] = 'stopped'
                                else:
                                    active_jobs[job_id]['status'] = 'completed'
                                    
                except Exception as e:
                    logger.error(f"Job {job_id}: Error in verification batch: {e}", exc_info=True)
                    with job_lock:
                        if job_id in active_jobs:
                            active_jobs[job_id]['status'] = 'failed'
        
        # Start the batch thread
        batch_thread = threading.Thread(target=verify_domains_batch)
        batch_thread.daemon = True
        batch_thread.start()
        
        logger.info(f"Started verification job {job_id} for {len(unverified_domains)} domains")
        
        return jsonify({
            'success': True,
            'job_id': job_id,
            'total_domains': len(unverified_domains),
            'domains': unverified_domains,
            'message': f'Started verification for {len(unverified_domains)} unverified domains'
        })
        
    except Exception as e:
        logger.error(f"Error in verify-unverified endpoint: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


def verify_single_domain(job_id: str, domain: str, account_name: str, stop_event):
    """
    Verify a single domain - called from the parallel executor.
    """
    from app import app
    with app.app_context():
        try:
            # Get the operation record
            operation = DomainVerificationOperation.query.filter_by(
                job_id=job_id, domain=domain
            ).first()
            
            if not operation:
                logger.error(f"Operation not found for {domain}")
                return {'success': False, 'error': 'Operation not found'}
            
            if stop_event.is_set():
                operation.verify_status = 'stopped'
                operation.message = 'Stopped by user'
                db.session.commit()
                return {'success': False, 'error': 'Stopped by user'}
            
            logger.info(f"=== VERIFYING DOMAIN {domain} ===")
            operation.message = 'Calling verification API...'
            db.session.commit()
            
            # Initialize Google service and verify
            google_service = GoogleDomainsService(account_name=account_name)
            verify_result = google_service.verify_domain(domain)
            
            logger.info(f"Verification result for {domain}: {verify_result}")
            
            if verify_result.get('verified'):
                operation.verify_status = 'success'
                operation.message = 'Domain verified successfully!'
                logger.info(f"✅ Domain {domain} verified successfully!")
            else:
                error_msg = verify_result.get('error', 'Verification pending')
                operation.verify_status = 'failed'
                operation.message = error_msg
                logger.warning(f"❌ Domain {domain} verification failed: {error_msg}")
            
            db.session.commit()
            return verify_result
            
        except Exception as e:
            logger.error(f"Error verifying {domain}: {e}", exc_info=True)
            if operation:
                operation.verify_status = 'failed'
                operation.message = str(e)
                db.session.commit()
            return {'success': False, 'error': str(e)}

# ===== BULK MULTI-ACCOUNT DOMAIN VERIFICATION =====
bulk_multi_jobs = {}
bulk_multi_lock = threading.Lock()

@dns_manager.route('/api/domains/bulk-multi-account/start', methods=['POST'])
@login_required
def start_bulk_multi_account():
    """
    Start bulk multi-account domain verification.
    Each entry has: domain, adminEmail, accountDomain, password
    """
    try:
        data = request.get_json()
        entries = data.get('entries', [])
        provider = data.get('provider', 'namecheap')
        
        if not entries:
            return jsonify({'success': False, 'error': 'No entries provided'}), 400
        
        job_id = str(uuid.uuid4())
        logger.info(f"=== BULK MULTI-ACCOUNT START === Job: {job_id}, Entries: {len(entries)}, Provider: {provider}")
        
        # Initialize job state
        with bulk_multi_lock:
            bulk_multi_jobs[job_id] = {
                'status': 'running',
                'stop_event': threading.Event(),
                'entries': [],
                'started_at': datetime.now().isoformat()
            }
            
            # Initialize entry statuses
            for entry in entries:
                bulk_multi_jobs[job_id]['entries'].append({
                    'index': entry.get('index'),
                    'domain': entry.get('domain'),
                    'adminEmail': entry.get('adminEmail'),
                    'accountDomain': entry.get('accountDomain'),
                    'authStatus': 'pending',
                    'workspaceStatus': 'pending',
                    'dnsStatus': 'pending',
                    'verifyStatus': 'pending',
                    'message': 'Queued'
                })
        
        # Start background processing with PARALLEL execution
        def process_bulk_multi():
            from app import app
            from concurrent.futures import ThreadPoolExecutor
            
            # DEBUG: Explicit print to see if thread starts
            print(f"[DEBUG] process_bulk_multi STARTED for job {job_id}")
            logger.info(f"[DEBUG] process_bulk_multi STARTED for job {job_id}")
            
            def process_single_entry(entry_data):
                """Process a single entry using SimpleDomainService - REWRITTEN"""
                entry_idx, entry, job, provider_name = entry_data
                
                # DEBUG: Print at very start
                print(f"[DEBUG] process_single_entry STARTED for {entry.get('domain')}")
                logger.info(f"[DEBUG] process_single_entry STARTED for {entry.get('domain')}")
                
                with app.app_context():
                    if job['stop_event'].is_set():
                        entry['message'] = 'Stopped'
                        return
                    
                    try:
                        domain = entry['domain']
                        admin_email = entry['adminEmail']
                        account_domain = entry['accountDomain']
                        
                        logger.info(f"[BULK] Processing: {domain} -> {admin_email}")
                        
                        # ========== STEP 1: Find Service Account ==========
                        entry['authStatus'] = 'running'
                        entry['message'] = 'Finding account...'
                        
                        # Try multiple lookup methods
                        service_account = ServiceAccount.query.filter_by(name=admin_email).first()
                        if not service_account:
                            service_account = ServiceAccount.query.filter_by(admin_email=admin_email).first()
                        if not service_account and account_domain:
                            service_account = ServiceAccount.query.filter_by(name=account_domain).first()
                        
                        if not service_account:
                            entry['authStatus'] = 'failed'
                            entry['message'] = f'Account not found for {admin_email}'
                            logger.error(f"[BULK] Account not found: {admin_email}")
                            return
                        
                        entry['authStatus'] = 'success'
                        entry['message'] = f'Using: {service_account.name}'
                        logger.info(f"[BULK] Found account: {service_account.name}")
                        
                        # ========== STEP 2: Use SimpleDomainService ==========
                        from services.simple_domain_service import SimpleDomainService
                        
                        entry['workspaceStatus'] = 'running'
                        entry['message'] = 'Adding to Workspace...'
                        
                        try:
                            svc = SimpleDomainService(
                                service_account_json=service_account.json_content,
                                admin_email=service_account.admin_email
                            )
                            
                            # Run the full process
                            result = svc.full_process(domain)
                            
                            if result['add_success']:
                                entry['workspaceStatus'] = 'success'
                                entry['message'] = result['add_message']
                            else:
                                entry['workspaceStatus'] = 'failed'
                                entry['message'] = result['add_message']
                                logger.error(f"[BULK] Add failed for {domain}: {result['add_message']}")
                                return
                            
                            # ========== STEP 3: Create DNS Record ==========
                            if result['token']:
                                entry['dnsStatus'] = 'running'
                                entry['message'] = 'Creating DNS TXT record...'
                                
                                apex = result['apex_domain']
                                txt_host = result['txt_host']
                                txt_value = result['token']
                                
                                try:
                                    if provider_name == 'cloudflare':
                                        from services.cloudflare_dns_service import CloudflareDNSService
                                        dns_svc = CloudflareDNSService()
                                        dns_svc.upsert_txt_record(apex, txt_host, txt_value, ttl=1)
                                    else:
                                        from services.namecheap_dns_service import NamecheapDNSService
                                        dns_svc = NamecheapDNSService()
                                        dns_svc.upsert_txt_record(apex, txt_host, txt_value, ttl=1799)
                                    
                                    entry['dnsStatus'] = 'success'
                                    entry['message'] = 'DNS record created, verifying...'
                                    logger.info(f"[BULK] DNS created for {domain}")
                                    
                                except Exception as dns_err:
                                    entry['dnsStatus'] = 'failed'
                                    entry['message'] = f'DNS error: {str(dns_err)[:80]}'
                                    logger.error(f"[BULK] DNS failed for {domain}: {dns_err}")
                                    return
                                
                                # ========== STEP 4: Verify Domain ==========
                                entry['verifyStatus'] = 'running'
                                entry['message'] = 'Verifying domain...'
                                
                                # Wait for DNS propagation
                                import time
                                time.sleep(10)
                                
                                verified, verify_msg = svc.verify_domain(domain)
                                
                                if verified:
                                    entry['verifyStatus'] = 'success'
                                    entry['message'] = 'Domain verified!'
                                    logger.info(f"[BULK] Verified: {domain}")
                                else:
                                    entry['verifyStatus'] = 'failed'
                                    entry['message'] = verify_msg
                                    logger.warning(f"[BULK] Verify failed for {domain}: {verify_msg}")
                            else:
                                entry['dnsStatus'] = 'failed'
                                entry['message'] = result['token_message']
                                logger.error(f"[BULK] Token failed for {domain}: {result['token_message']}")
                                
                        except Exception as svc_err:
                            logger.error(f"[BULK] Service error for {domain}: {svc_err}", exc_info=True)
                            entry['workspaceStatus'] = 'failed'
                            entry['message'] = f'Error: {str(svc_err)[:80]}'
                            
                    except Exception as e:
                        logger.error(f"[BULK] Fatal error for entry: {e}", exc_info=True)
                        entry['message'] = f'Error: {str(e)[:80]}'
                        entry['workspaceStatus'] = 'failed'
            
            # Main thread function
            with app.app_context():
                job = bulk_multi_jobs[job_id]
                entries = job['entries']
                
                # Process entries in PARALLEL (max 5 concurrent)
                max_workers = min(5, len(entries))
                logger.info(f"Job {job_id}: Starting parallel processing with {max_workers} workers")
                
                try:
                    with ThreadPoolExecutor(max_workers=max_workers) as executor:
                        # Prepare entry data for parallel processing
                        entry_data_list = [
                            (i, entry, job, provider) 
                            for i, entry in enumerate(entries)
                        ]
                        
                        # Submit all tasks
                        futures = [executor.submit(process_single_entry, data) for data in entry_data_list]
                        
                        # Wait for all to complete
                        for future in futures:
                            try:
                                future.result(timeout=600)  # 10 min per entry max
                            except Exception as e:
                                logger.error(f"Job {job_id}: Thread error: {e}")
                                
                except Exception as e:
                    logger.error(f"Job {job_id}: Executor error: {e}", exc_info=True)
                
                # Mark job complete
                if job['stop_event'].is_set():
                    job['status'] = 'stopped'
                else:
                    job['status'] = 'completed'
                logger.info(f"Job {job_id}: Finished with status {job['status']}")
        
        # Start the thread
        thread = threading.Thread(target=process_bulk_multi)
        thread.daemon = True
        thread.start()
        
        return jsonify({
            'success': True,
            'job_id': job_id,
            'message': f'Started processing {len(entries)} entries'
        })
        
    except Exception as e:
        logger.error(f"Error starting bulk multi-account: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@dns_manager.route('/api/domains/bulk-multi-account/stop', methods=['POST'])
@login_required
def stop_bulk_multi_account():
    """Stop a bulk multi-account job."""
    try:
        data = request.get_json()
        job_id = data.get('job_id')
        
        with bulk_multi_lock:
            if job_id and job_id in bulk_multi_jobs:
                bulk_multi_jobs[job_id]['stop_event'].set()
                bulk_multi_jobs[job_id]['status'] = 'stopped'
                return jsonify({'success': True})
        
        return jsonify({'success': False, 'error': 'Job not found'})
        
    except Exception as e:
        logger.error(f"Error stopping bulk multi-account: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@dns_manager.route('/api/domains/bulk-multi-account/status/<job_id>', methods=['GET'])
@login_required
def get_bulk_multi_account_status(job_id):
    """Get status of a bulk multi-account job - MEMORY ONLY for stability."""
    try:
        with bulk_multi_lock:
            if job_id not in bulk_multi_jobs:
                return jsonify({'success': False, 'error': 'Job not found'}), 404
            
            job = bulk_multi_jobs[job_id]
            
            # Return memory state directly - no DB queries to avoid deadlocks
            return jsonify({
                'success': True,
                'job_id': job_id,
                'status': job['status'],
                'entries': job['entries']
            })
            
    except Exception as e:
        logger.error(f"Status endpoint error: {e}", exc_info=True)
        return jsonify({'success': False, 'error': str(e)}), 500


@dns_manager.route('/api/namecheap-domains', methods=['GET'])
@login_required
def get_namecheap_domains():
    """
    Get list of domains from Namecheap account.
    
    Returns:
        {
            "success": bool,
            "domains": [{"name": "...", "expire_date": "..."}, ...],
            "error": "..." (if failed),
            "debug_info": "..." (if available)
        }
    """
    try:
        logger.info("API: Fetching Namecheap domains...")
        dns_service = NamecheapDNSService()
        domains = dns_service.get_domains_list()
        
        logger.info(f"API: Successfully retrieved {len(domains)} domains")
        return jsonify({
            'success': True,
            'domains': domains,
            'total': len(domains)
        })
    
    except Exception as e:
        error_msg = str(e)
        logger.error(f"Error fetching Namecheap domains: {error_msg}", exc_info=True)
        
        # Provide more detailed error information
        debug_info = None
        troubleshooting = []
        
        if "configuration not found" in error_msg.lower():
            debug_info = "Namecheap credentials not configured. Please save configuration in Settings first."
            troubleshooting.append("1. Fill in all Namecheap API credentials in Settings")
            troubleshooting.append("2. Click 'Save Namecheap Configuration'")
        elif "client ip" in error_msg.lower() or "whitelist" in error_msg.lower():
            debug_info = "Client IP may not be whitelisted in Namecheap account settings."
            troubleshooting.append("1. Log in to your Namecheap account")
            troubleshooting.append("2. Go to Profile > Tools > API Access")
            troubleshooting.append("3. Add your server's IP address to the whitelist")
        elif "api error" in error_msg.lower() or "invalid" in error_msg.lower():
            debug_info = "Check API credentials (API User, API Key, Username) and ensure they are correct."
            troubleshooting.append("1. Verify API User matches your Namecheap API username")
            troubleshooting.append("2. Verify API Key is correct")
            troubleshooting.append("3. Verify Username is correct")
        
        return jsonify({
            'success': False,
            'error': error_msg,
            'debug_info': debug_info,
            'troubleshooting': troubleshooting
        }), 500

@dns_manager.route('/api/domains/status', methods=['GET'])
@login_required
def get_domain_verification_status():
    """
    Get status of domain verification job.
    Supports both DomainOperation (add/verify) and DomainVerificationOperation (verify existing).
    """
    job_id = request.args.get('job_id')
    if not job_id:
        return jsonify({'success': False, 'error': 'No job_id provided'}), 400
    
    try:
        from database import DomainOperation, DomainVerificationOperation
        
        # 1. Try DomainOperation (standard add+verify flow)
        operations = DomainOperation.query.filter_by(job_id=job_id).order_by(DomainOperation.updated_at.desc()).all()
        
        # 2. If no operations found, try DomainVerificationOperation (verify-only flow)
        if not operations:
             operations = DomainVerificationOperation.query.filter_by(job_id=job_id).all()
        
        if not operations:
            # Fallback to active_jobs check just in case DB write hasn't happened yet
            with job_lock:
                if job_id in active_jobs:
                     return jsonify({'success': True, 'status': active_jobs[job_id]['status'], 'results': []})
            
            return jsonify({'success': False, 'error': 'Job not found'}), 404
            
        # Determine overall status
        # If any operation is pending, job is running
        is_running = any(op.verify_status == 'pending' or (hasattr(op, 'workspace_status') and op.workspace_status == 'pending') or (hasattr(op, 'dns_status') and op.dns_status == 'pending') for op in operations)
        status = 'running' if is_running else 'completed'
        
        results = []
        for op in operations:
            # Normalize fields (DomainVerificationOperation might differ slightly)
            domain_name = getattr(op, 'input_domain', getattr(op, 'domain', 'Unknown'))
            workspace_status = getattr(op, 'workspace_status', 'N/A')
            dns_status = getattr(op, 'dns_status', 'N/A')
            app_updated = getattr(op, 'updated_at', None)
            
            results.append({
                'domain': domain_name,
                'workspace': workspace_status,
                'dns': dns_status,
                'verify': op.verify_status,
                'message': op.message,
                'updated_at': app_updated.isoformat() if app_updated else None
            })
            
        return jsonify({
            'success': True,
            'status': status,
            'results': results
        })
        
    except Exception as e:
        logger.error(f"Error fetching job status: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@dns_manager.route('/api/cloudflare-domains', methods=['GET'])
@login_required
def get_cloudflare_domains():
    """
    Get list of domains (zones) from Cloudflare account.
    """
    try:
        logger.info("API: Fetching Cloudflare domains...")
        dns_service = CloudflareDNSService()
        zones = dns_service.get_zones()
        
        # Format for frontend
        domains = []
        for zone in zones:
            domains.append({
                'name': zone['name'],
                'id': zone['id'],
                'status': zone['status'],
                'expire_date': 'N/A' # Cloudflare doesn't provide expiry in basic zone info
            })
            
        logger.info(f"API: Successfully retrieved {len(domains)} Cloudflare domains")
        return jsonify({
            'success': True,
            'domains': domains,
            'total': len(domains)
        })
    
    except Exception as e:
        error_msg = str(e)
        logger.error(f"Error fetching Cloudflare domains: {error_msg}", exc_info=True)
        return jsonify({
            'success': False,
            'error': error_msg
        }), 500

