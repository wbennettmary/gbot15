"""
DigitalOcean Bulk Execution Orchestrator

Handles the complete workflow for distributing users across multiple droplets
and executing automation in parallel.
"""

import os
import json
import logging
import threading
import time
from typing import List, Dict, Optional, Tuple
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

from database import db, DigitalOceanDroplet, DigitalOceanExecution, AwsGeneratedPassword
from services.digitalocean_service import DigitalOceanService

logger = logging.getLogger(__name__)


class BulkExecutionOrchestrator:
    """Orchestrates bulk execution across multiple DigitalOcean droplets"""
    
    def __init__(self, config: Dict, service: DigitalOceanService, app=None):
        """
        Initialize orchestrator.
        
        Args:
            config: DigitalOcean configuration dict
            service: DigitalOceanService instance
        """
        self.config = config
        self.service = service
        self.app = app or current_app
        self.execution_id = None
        self.droplets_created = [] # Tracker for cleanup

    def set_app(self, app):
        """Set Flask app instance for context"""
        self.app = app
        
    def execute_bulk(
        self,
        users: List[Dict],
        droplet_count: Optional[int] = None,
        snapshot_id: str = None,
        region: str = None,
        size: str = None,
        auto_destroy: bool = True,
        parallel_users: int = 5,
        users_per_droplet: int = 50,
        execution_id: str = None
    ) -> Dict:
        """
        Execute bulk automation across multiple droplets.
        
        Workflow:
        1. Distribute users across droplets
        2. Create droplets from snapshot
        3. Wait for droplets to be active
        4. Execute automation on each droplet via SSH
        5. Collect results
        6. Destroy droplets (if auto_destroy=True)
        
        Args:
            users: List of user dicts with email/password
            droplet_count: Number of droplets to create
            snapshot_id: Snapshot ID to use as base image
            region: Region to create droplets in
            size: Droplet size
            auto_destroy: Whether to destroy droplets after completion
            parallel_users: Total parallel users across all droplets
            users_per_droplet: Max users per droplet (to determine droplet count)
            execution_id: Optional existing execution ID to use
            
        Returns:
            Dict with execution results
        """
        exec_start = datetime.utcnow()
        self.execution_id = execution_id or f"exec_{int(time.time())}"
        
    def _log_to_droplet(self, droplet_id: str, message: str, append: bool = True):
        """Helper to write logs to a specific droplet's monitor file"""
        try:
            root_path = self.app.root_path if self.app else os.getcwd()
            log_dir = os.path.join(root_path, 'logs', 'bulk_executions', self.execution_id)
            os.makedirs(log_dir, exist_ok=True)
            log_file = os.path.join(log_dir, f"{droplet_id}.log")
            
            mode = 'a' if append else 'w'
            with open(log_file, mode, encoding='utf-8') as f:
                f.write(message)
        except Exception as e:
            logger.error(f"Failed to write to monitor log {droplet_id}: {e}")

    def execute_bulk(
        self,
        users: List[Dict],
        droplet_count: Optional[int] = None,
        snapshot_id: str = None,
        region: str = None,
        size: str = None,
        auto_destroy: bool = True,
        parallel_users: int = 5,
        users_per_droplet: int = 50,
        execution_id: str = None
    ) -> Dict:
        """
        Execute bulk automation across multiple droplets.
        """
        exec_start = datetime.utcnow()
        self.execution_id = execution_id or f"exec_{int(time.time())}"
        
        # 0. Ensure log directory exists
        root_path = self.app.root_path if self.app else os.getcwd()
        log_dir = os.path.join(root_path, 'logs', 'bulk_executions', self.execution_id)
        os.makedirs(log_dir, exist_ok=True)

        try:
            # 1. Distribute users
            # If droplet_count is 0 or None, calculate it
            if not droplet_count or droplet_count <= 0:
                user_batches = self.service.distribute_users(users, max_users_per_droplet=users_per_droplet)
                droplet_count = len(user_batches)
            else:
                user_batches = self.service.distribute_users(users, droplet_count=droplet_count)
                droplet_count = len(user_batches)

            logger.info(f"[{self.execution_id}] Distributing {len(users)} users across {droplet_count} droplets")
            
            # 2. Create droplets
            droplet_info, creation_errors = self._create_droplets_parallel(
                count=droplet_count,
                snapshot_id=snapshot_id,
                region=region,
                size=size
            )
            
            if not droplet_info:
                error_msg = f"Failed to create droplets: {'; '.join(creation_errors[:3])}" if creation_errors else 'Failed to create droplets (Unknown error)'
                return {
                    'success': False,
                    'error': error_msg,
                    'execution_id': self.execution_id
                }
            
            logger.info(f"[{self.execution_id}] Created {len(droplet_info)} droplets")
            
            # 3. Execute automation
            # Use parallel_users as threads per droplet (as indicated in the UI)
            results = self._execute_on_droplets_parallel(
                droplet_info, 
                user_batches, 
                parallel_users,
                auto_destroy=auto_destroy
            )
            
            # 5. Compile results
            exec_end = datetime.utcnow()
            execution_time = (exec_end - exec_start).total_seconds()
            
            success_count = sum(1 for r in results if r.get('success'))
            
            return {
                'success': True,
                'execution_id': self.execution_id,
                'total_users': len(users),
                'droplets_used': len(droplet_info),
                'success_count': success_count,
                'fail_count': len(results) - success_count,
                'execution_time_seconds': execution_time,
                'results': results
            }
            
        except Exception as e:
            import traceback
            logger.error(f"[{self.execution_id}] Bulk execution CRASHED: {e}")
            logger.error(traceback.format_exc())
            return {
                'success': False,
                'error': f"Crash: {str(e)}",
                'execution_id': self.execution_id
            }
        finally:
            # FINAL SAFETY SWEEP: If auto_destroy is enabled, ensure ALL tracked droplets are gone
            is_auto_destroy = bool(auto_destroy)
            if isinstance(auto_destroy, str):
                is_auto_destroy = auto_destroy.lower() == 'true'
            
            # Fallback to config if not explicitly False
            if auto_destroy is None:
                is_auto_destroy = self.config.get('auto_destroy_droplets', True)

            if is_auto_destroy and self.droplets_created:
                logger.info(f"[{self.execution_id}] Running final safety cleanup of {len(self.droplets_created)} droplets...")
                self._destroy_droplets_parallel(self.droplets_created)
    
    def _create_droplets_parallel(
        self,
        count: int,
        snapshot_id: str,
        region: str,
        size: str
    ) -> Tuple[List[Dict], List[str]]:
        """Create multiple droplets in parallel"""
        droplets = []
        errors = []
        
        with ThreadPoolExecutor(max_workers=min(count, 10)) as executor:
            futures = []
            
            for i in range(count):
                # Sanitize name: replace underscores with hyphens for DO compliance
                safe_exec_id = self.execution_id.replace('_', '-')
                name = f"bulk-exec-{safe_exec_id}-{i+1}"
                future = executor.submit(
                    self._create_and_wait_for_droplet,
                    name, snapshot_id, region, size
                )
                futures.append(future)
            
            for future in as_completed(futures):
                try:
                    droplet = future.result()
                    if droplet:
                        droplets.append(droplet)
                        self.droplets_created.append(droplet)
                    else:
                        errors.append("Droplet creation returned None without exception")
                except Exception as e:
                    error_str = str(e)
                    logger.error(f"[{self.execution_id}] Future creation failed: {error_str}")
                    errors.append(error_str)
        
        return droplets, errors
    
    def _create_and_wait_for_droplet(
        self,
        name: str,
        snapshot_id: str,
        region: str,
        size: str
    ) -> Optional[Dict]:
        """Create a droplet and wait for it to be active"""
        # No broad try-except here - let exceptions bubble up to the parallel executor
        # PRIORITY: Look for SSH key named 'Default' in DigitalOcean account
        ssh_keys = []
        try:
            # Attempt 1: Look for "Default" key
            default_key = self.service.get_ssh_key_by_name('Default')
            if default_key:
                ssh_keys.append(default_key['id'])
                logger.info(f"[{self.execution_id}] Using 'Default' SSH key ID: {default_key['id']}")
            else:
                logger.warning(f"[{self.execution_id}] 'Default' SSH key NOT found on DigitalOcean.")
                
                # Attempt 2: Use configured key from Settings
                ssh_key_id = self.config.get('ssh_key_id')
                if ssh_key_id:
                    logger.info(f"[{self.execution_id}] Using configured SSH key ID from settings: {ssh_key_id}")
                    ssh_keys.append(int(ssh_key_id) if str(ssh_key_id).isdigit() else ssh_key_id)
                else:
                    # Attempt 3: Last resort - use the FIRST key found in account
                    all_keys = self.service.list_keys()
                    if all_keys:
                        last_resort_key = all_keys[0]
                        logger.warning(f"[{self.execution_id}] Using FIRST available key as last resort: {last_resort_key.get('name')} ({last_resort_key.get('id')})")
                        ssh_keys.append(last_resort_key['id'])
        
        except Exception as e:
            logger.error(f"[{self.execution_id}] Error resolving SSH keys: {e}")
            # Critical fallback
            ssh_key_id = self.config.get('ssh_key_id')
            if ssh_key_id:
                ssh_keys.append(int(ssh_key_id) if str(ssh_key_id).isdigit() else ssh_key_id)
        
        if not ssh_keys:
             logger.error(f"[{self.execution_id}] ❌ NO SSH KEYS FOUND. Droplet creation will fail.")
             raise Exception("No usable SSH key found. Please add a key named 'Default' to your DigitalOcean account or configure one in Settings.")

        # Create droplet
        logger.info(f"[{self.execution_id}] Creating droplet {name} in {region} (Size: {size}, Image: {snapshot_id})")
        result, error_msg = self.service.create_droplet(
            name=name,
            region=region,
            size=size,
            image=snapshot_id,
            ssh_keys=ssh_keys,
            tags=['bulk-execution', self.execution_id]
        )
        
        if not result:
            logger.error(f"[{self.execution_id}] ❌ API Error creating droplet {name}: {error_msg}")
            raise Exception(f"DigitalOcean API Error: {error_msg}")
        
        droplet_id = result['id']
        
        # --- INITIALIZATION LOGS FOR MONITOR ---
        self._log_to_droplet(droplet_id, f"[{datetime.utcnow().isoformat()}] CREATION: Initializing droplet {name} in {region}...\n", append=False)
        self._log_to_droplet(droplet_id, f"[{datetime.utcnow().isoformat()}] CREATION: Waiting for DigitalOcean to allocate IP...\n")
        # --- END INITIALIZATION LOGS ---

        # Wait for active status and IP
        ip_address = self.service.wait_for_droplet_active(droplet_id, timeout=300)
        
        if not ip_address:
            msg = f"[{datetime.utcnow().isoformat()}] CREATION ERROR: Timeout waiting for IP address.\n"
            self._log_to_droplet(droplet_id, msg)
            logger.error(f"[{self.execution_id}] Droplet {droplet_id} did not become active")
            raise Exception(f"Droplet {droplet_id} timed out waiting for IP address after activation")
        
        # Wait for SSH to be ready with a robust handshake
        ssh_ready = self.service.wait_for_ssh(
            ip_address=ip_address,
            username='root',
            timeout=300,
            ssh_key_path=self.config.get('ssh_private_key_path'),
            log_callback=lambda msg, append=True: self._log_to_droplet(droplet_id, msg, append=append)
        )

        if not ssh_ready:
            msg = f"[{datetime.utcnow().isoformat()}] CREATION ERROR: SSH did not become ready after 5 minutes.\n"
            self._log_to_droplet(droplet_id, msg)
            logger.error(f"[{self.execution_id}] Droplet {droplet_id} failed SSH handshake")
            raise Exception("Droplet failed SSH readiness check")
        
        self._log_to_droplet(droplet_id, f"[{datetime.utcnow().isoformat()}] CREATION: Boot complete. Ready for orchestration.\n")
        
        # Save droplet to database
        if self.app:
            with self.app.app_context():
                try:
                    droplet_record = DigitalOceanDroplet(
                        droplet_id=str(droplet_id),
                        droplet_name=name,
                        ip_address=ip_address,
                        region=region,
                        size=size,
                        status='active',
                        execution_task_id=self.execution_id,
                        created_by_username=self.config.get('username', 'system'),
                        auto_destroy=True
                    )
                    db.session.add(droplet_record)
                    db.session.commit()
                    logger.info(f"[{self.execution_id}] Saved droplet {droplet_id} to DB")
                except Exception as db_e:
                    logger.error(f"[{self.execution_id}] Failed to save droplet record: {db_e}")
        else:
             logger.warning(f"[{self.execution_id}] No app context - skipping DB save for droplet {droplet_id}")

        return {
            'id': droplet_id,
            'name': name,
            'ip_address': ip_address,
            'region': region,
            'size': size
        }
    
    def _execute_on_droplets_parallel(
        self,
        droplets: List[Dict],
        user_batches: List[List[Dict]],
        workers_per_droplet: int = 1,
        auto_destroy: bool = False,
        log_callback=None
    ) -> List[Dict]:
        """Execute automation on multiple droplets in parallel"""
        all_results = []
        
        with ThreadPoolExecutor(max_workers=len(droplets)) as executor:
            futures = {}
            
            for droplet, users in zip(droplets, user_batches):
                future = executor.submit(
                    self._execute_and_destroy_droplet,
                    droplet, users, workers_per_droplet, auto_destroy, log_callback
                )
                futures[future] = droplet
            
            for future in as_completed(futures):
                droplet = futures[future]
                try:
                    results = future.result()
                    all_results.extend(results)
                    logger.info(f"[{self.execution_id}] Completed execution flow on {droplet['name']}")
                except Exception as e:
                    logger.error(f"[{self.execution_id}] Execution failed on {droplet['name']}: {e}")
        
        return all_results
    
    def _execute_and_destroy_droplet(
        self,
        droplet: Dict,
        users: List[Dict],
        workers_per_droplet: int,
        auto_destroy: bool,
        log_callback=None
    ) -> List[Dict]:
        """Wrapper to execute on a droplet and immediately destroy it regardless of outcome"""
        # Improved boolean conversion: treat everything truthy or string "true" as True
        is_auto_destroy = bool(auto_destroy)
        if isinstance(auto_destroy, str):
            is_auto_destroy = auto_destroy.lower() == 'true'
        
        # Fallback to config if not explicitly False
        if auto_destroy is None:
            is_auto_destroy = self.config.get('auto_destroy_droplets', True)

        droplet_id = str(droplet['id'])
        
        try:
            logger.info(f"[{self.execution_id}] EXECUTOR: Starting batch on {droplet['name']} ({droplet['ip_address']})")
            self._log_to_droplet(droplet_id, f"[{datetime.utcnow().isoformat()}] EXECUTOR: Starting automation on {droplet['name']}... \n")
            
            results = self._execute_on_single_droplet(droplet, users, workers_per_droplet)
            
        except Exception as e:
            logger.error(f"[{self.execution_id}] EXECUTOR: Execution error in single droplet wrapper for {droplet['name']}: {e}")
            self._log_to_droplet(droplet_id, f"[{datetime.utcnow().isoformat()}] EXECUTOR ERROR: {str(e)}\n")
            results = [] 
        finally:
            logger.info(f"[{self.execution_id}] EXECUTOR: Finalizing droplet {droplet['name']}. auto_destroy={is_auto_destroy}")
            
            if is_auto_destroy:
                try:
                    msg = f"[{datetime.utcnow().isoformat()}] EXECUTOR: Triggering AUTO-DESTRUCTION for {droplet['name']}...\n"
                    logger.info(f"[{self.execution_id}] " + msg.strip())
                    self._log_to_droplet(droplet_id, msg)
                    
                    deletion_success = self.service.delete_droplet(droplet['id'])
                    
                    if deletion_success:
                        succ_msg = f"[{datetime.utcnow().isoformat()}] EXECUTOR: ✓ Droplet deleted successfully.\n"
                        logger.info(f"[{self.execution_id}] " + succ_msg.strip())
                        self._log_to_droplet(droplet_id, succ_msg)
                    else:
                        fail_msg = f"[{datetime.utcnow().isoformat()}] EXECUTOR: ⚠ API Deletion call failed.\n"
                        logger.warning(f"[{self.execution_id}] " + fail_msg.strip())
                        self._log_to_droplet(droplet_id, fail_msg)
                    
                    # Update DB status
                    if self.app:
                        with self.app.app_context():
                            from database import db, DigitalOceanDroplet
                            db_droplet = DigitalOceanDroplet.query.filter_by(droplet_id=droplet_id).first()
                            if db_droplet:
                                db_droplet.status = 'destroyed'
                                db_droplet.destroyed_at = datetime.utcnow()
                                db.session.commit()
                
                except Exception as cleanup_error:
                    logger.error(f"[{self.execution_id}] EXECUTOR: ✗ Failed to auto-destroy {droplet['name']}: {cleanup_error}")
                    self._log_to_droplet(droplet_id, f"[{datetime.utcnow().isoformat()}] EXECUTOR CLEANUP ERROR: {str(cleanup_error)}\n")
            else:
                logger.info(f"[{self.execution_id}] EXECUTOR: Skipping auto-destruction (auto_destroy is False)")
                self._log_to_droplet(droplet_id, f"[{datetime.utcnow().isoformat()}] EXECUTOR: Preservation mode. Droplet {droplet['name']} will NOT be destroyed.\n")
        
        return results

    def _execute_on_single_droplet(
        self,
        droplet: Dict,
        users: List[Dict],
        parallel_users: int = 5
    ) -> List[Dict]:
        """Execute automation for a batch of users on a single droplet"""
        results = []
        ip_address = droplet['ip_address']
        droplet_id = str(droplet['id'])
        
        # 1. Prepare users
        enriched_users = [u.copy() for u in users]

        # 3. Execute Batch
        try:
            # Idempotency set to prevent saving same user twice (real-time + final)
            saved_emails = set()
            
            def droplet_log_callback(msg, append=True):
                """Pass through to the centralized log helper"""
                self._log_to_droplet(droplet_id, msg, append=append)

            def on_realtime_result(res):
                email = res.get('email')
                if email and email not in saved_emails:
                    if res.get('success'):
                        droplet_log_callback(f"[{datetime.utcnow().isoformat()}] EXECUTOR: Saving password for {email} (Real-time)...\n")
                        self._save_app_password_with_backup(
                            email, 
                            res.get('app_password'), 
                            res.get('secret_key'),
                            log_callback=droplet_log_callback
                        )
                        saved_emails.add(email)

            batch_result = self.service.run_automation_script_async_poll(
                ip_address=ip_address,
                ssh_key_path=self.config.get('ssh_private_key_path'),
                log_callback=droplet_log_callback,
                twocaptcha_config=getattr(self, 'twocaptcha_config', None),
                users=enriched_users,
                parallel_users=parallel_users,
                on_result=on_realtime_result
            )
            
            # Check for failure in service level
            if not batch_result.get('success'):
                error_msg = batch_result.get('error', 'Unknown service error')
                logger.error(f"[{self.execution_id}] Automation service failed on {droplet['name']}: {error_msg}")
                self._log_to_droplet(droplet_id, f"[{datetime.utcnow().isoformat()}] SERVICE ERROR: {error_msg}\n")
            
            # 4. Process Results (Final Summary)
            logger.info(f"[{self.execution_id}] Raw batch result from {droplet['name']}: {json.dumps(batch_result, default=str)}")
            
            if batch_result.get('results'):
                user_results = batch_result['results']
                logger.info(f"[{self.execution_id}] Batch on {droplet['name']} returned {len(user_results)} results")
                
                for res in user_results:
                    email = res.get('email')
                    is_success = res.get('success') or res.get('status') == 'success'
                    
                    if is_success and email not in saved_emails:
                         droplet_log_callback(f"[{datetime.utcnow().isoformat()}] EXECUTOR: Saving password for {email} (Final summary)...\n")
                         self._save_app_password_with_backup(
                             email, 
                             res.get('app_password'), 
                             res.get('secret_key'),
                             log_callback=droplet_log_callback
                         )
                         saved_emails.add(email)
                    
                    # Standardize result format
                    res['droplet_id'] = droplet_id
                    res['success'] = is_success
                    results.append(res)
                    
        except Exception as e:
            logger.error(f"[{self.execution_id}] Batch execution exception on {droplet['name']}: {e}")
            self._log_to_droplet(droplet_id, f"[{datetime.utcnow().isoformat()}] BATCH EXCEPTION: {str(e)}\n")
            for user in users:
                results.append({
                    'success': False, 
                    'email': user['email'], 
                    'error': str(e), 
                    'droplet_id': droplet_id
                })

        return results

    def _save_app_password_with_backup(self, email: str, app_password: str, secret_key: str = None, log_callback=None):
        """
        Dual-save system: Save app password to both database AND backup file.
        """
        from database import db, AwsGeneratedPassword, UserAppPassword
        
        # 1. IMMEDIATE BACKUP TO FILE ... (using robust naming)
        backup_success = False
        try:
            root_path = self.app.root_path if self.app else os.getcwd()
            backup_dir = os.path.join(root_path, 'do_app_passwords_backup')
            os.makedirs(backup_dir, exist_ok=True)
            
            # NEW ROBUST FILENAME: {email_slug}___{execution_id}.json
            email_slug = email.replace('@', '_at_')
            backup_file = os.path.join(backup_dir, f"{email_slug}___{self.execution_id}.json")
            
            backup_data = {
                'email': email,
                'app_password': app_password,
                'secret_key': secret_key,
                'execution_id': self.execution_id,
                'timestamp': datetime.utcnow().isoformat(),
                'saved_to_db': False
            }
            
            with open(backup_file, 'w') as f:
                json.dump(backup_data, f, indent=2)
            
            backup_success = True
            if log_callback: log_callback(f"[{datetime.utcnow().isoformat()}] EXECUTOR: ✓ Backup file saved for {email}\n", append=True)
        except Exception as backup_error:
            logger.error(f"[{self.execution_id}] ✗ Backup file failed for {email}: {backup_error}")
            if log_callback: log_callback(f"[{datetime.utcnow().isoformat()}] EXECUTOR: ✗ Backup FAILED for {email}\n", append=True)
        
        # 2. SAVE TO DATABASE ...
        db_success = False
        user_app_success = False
        context_manager = self.app.app_context() if self.app else None
        try:
            if context_manager: context_manager.push()
            
            # === SAVE TO AwsGeneratedPassword ===
            # Use query to find existing entry for this email
            record = AwsGeneratedPassword.query.filter_by(email=email).first()
            
            if record:
                # Update existing record
                record.app_password = app_password
                record.secret_key = secret_key
                record.execution_id = self.execution_id
                record.created_at = datetime.utcnow()
            else:
                # Create new record
                new_p = AwsGeneratedPassword(
                    email=email,
                    app_password=app_password,
                    secret_key=secret_key,
                    execution_id=self.execution_id,
                    created_at=datetime.utcnow()
                )
                db.session.add(new_p)
            
            db.session.commit()
            db_success = True
            if log_callback: log_callback(f"[{datetime.utcnow().isoformat()}] EXECUTOR: ✓ Database record saved for {email}\n", append=True)
            
            # === SAVE TO UserAppPassword (App Password Management UI) ===
            try:
                if '@' in email:
                    u_name, u_domain = email.split('@', 1)
                else:
                    u_name = email
                    u_domain = '*'
                    
                u_name = u_name.lower()
                u_domain = u_domain.lower()
                
                user_app_record = UserAppPassword.query.filter_by(
                    username=u_name,
                    domain=u_domain
                ).first()
                
                if user_app_record:
                    user_app_record.app_password = app_password
                    user_app_record.updated_at = db.func.current_timestamp()
                else:
                    new_user_app = UserAppPassword(
                        username=u_name,
                        domain=u_domain,
                        app_password=app_password
                    )
                    db.session.add(new_user_app)
                    
                db.session.commit()
                user_app_success = True
                if log_callback: log_callback(f"[{datetime.utcnow().isoformat()}] EXECUTOR: ✓ Synced to App Password Management UI for {email}\n", append=True)
            except Exception as u_db_e:
                db.session.rollback()
                logger.error(f"[{self.execution_id}] ✗ UserAppPassword DB sync failed for {email}: {u_db_e}")
                if log_callback: log_callback(f"[{datetime.utcnow().isoformat()}] EXECUTOR: ⚠ UserAppPassword sync failed: {str(u_db_e)}\n", append=True)
                
        except Exception as db_e:
            db.session.rollback()
            logger.error(f"[{self.execution_id}] ✗ Database save failed for {email}: {db_e}")
            if log_callback: log_callback(f"[{datetime.utcnow().isoformat()}] EXECUTOR: ⚠ Database save failed: {str(db_e)}\n", append=True)
        finally:
            if context_manager: context_manager.pop()
        
        if db_success and backup_success and user_app_success:
            if log_callback: log_callback(f"[{datetime.utcnow().isoformat()}] EXECUTOR: ✓✓ MULTI-SAVE COMPLETE: {email}\n", append=True)
        elif db_success and backup_success:
            if log_callback: log_callback(f"[{datetime.utcnow().isoformat()}] EXECUTOR: ✓✓ DUAL-SAVE COMPLETE: {email}\n", append=True)
    
    def _destroy_droplets_parallel(self, droplets: List[Dict]):
        """Destroy multiple droplets in parallel"""
        logger.info(f"[{self.execution_id}] Destroying {len(droplets)} droplets")
        
        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = []
            
            for droplet in droplets:
                future = executor.submit(self.service.delete_droplet, droplet['id'])
                futures.append(future)
            
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as e:
                    logger.error(f"[{self.execution_id}] Error destroying droplet: {e}")
        
        logger.info(f"[{self.execution_id}] Droplet destruction complete")
