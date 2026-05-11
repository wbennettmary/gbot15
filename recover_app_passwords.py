"""
Recovery script to restore app passwords from backup files.
Run this if server crashed during bulk execution.

Usage:
    python recover_app_passwords.py
"""

import os
import json
from datetime import datetime
from database import db, AwsGeneratedPassword
from app import app

def recover_from_backups():
    """Scan backup directory and restore any passwords not in database"""
    
    backup_dir = 'do_app_passwords_backup'
    
    if not os.path.exists(backup_dir):
        print("❌ No backup directory found")
        return
    
    backup_files = [f for f in os.listdir(backup_dir) if f.endswith('.json')]
    
    if not backup_files:
        print("📭 No backup files found")
        return
    
    print(f"🔍 Found {len(backup_files)} backup files")
    print("=" * 60)
    
    recovered = 0
    already_in_db = 0
    failed = 0
    
    with app.app_context():
        for filename in backup_files:
            filepath = os.path.join(backup_dir, filename)
            
            try:
                with open(filepath, 'r') as f:
                    data = json.load(f)
                
                email = data.get('email')
                app_password = data.get('app_password')
                saved_to_db = data.get('saved_to_db', False)
                
                if not email or not app_password:
                    print(f"⚠️  Skipping invalid backup: {filename}")
                    continue
                
                # Check if already in database
                existing = AwsGeneratedPassword.query.filter_by(email=email).first()
                
                if existing:
                    already_in_db += 1
                    print(f"✓ Already in DB: {email}")
                else:
                    # Recover to database
                    new_password = AwsGeneratedPassword()
                    new_password.email = email
                    new_password.app_password = app_password
                    db.session.add(new_password)
                    db.session.commit()
                    
                    recovered += 1
                    print(f"🔄 RECOVERED: {email}")
                
            except Exception as e:
                failed += 1
                print(f"❌ Failed to process {filename}: {e}")
    
    print("=" * 60)
    print(f"📊 Recovery Summary:")
    print(f"   ✓ Recovered: {recovered}")
    print(f"   ✓ Already in DB: {already_in_db}")
    print(f"   ✗ Failed: {failed}")
    print(f"   📁 Total backups: {len(backup_files)}")
    
    if recovered > 0:
        print(f"\n✅ Successfully recovered {recovered} app passwords!")
    elif already_in_db > 0:
        print(f"\n✅ All passwords already in database. No recovery needed.")
    else:
        print(f"\n⚠️  No passwords recovered.")

if __name__ == '__main__':
    print("🔧 DigitalOcean App Password Recovery Tool")
    print("=" * 60)
    recover_from_backups()
