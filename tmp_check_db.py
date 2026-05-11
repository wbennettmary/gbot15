
import os
import sys
from datetime import datetime

# Set dummy environment variables to avoid app startup errors
os.environ['SECRET_KEY'] = 'dummy'
os.environ['WHITELIST_TOKEN'] = 'dummy'
os.environ['FLASK_ENV'] = 'development'

# Add the project directory to sys.path
sys.path.append(os.getcwd())

from app import app
from database import db, DigitalOceanExecution, DigitalOceanDroplet

with app.app_context():
    count = DigitalOceanExecution.query.count()
    print(f"Total DigitalOcean Executions: {count}")
    print("--- DigitalOcean Executions (Last 5) ---")
    executions = DigitalOceanExecution.query.order_by(DigitalOceanExecution.started_at.desc()).limit(5).all()
    for e in executions:
        print(f"ID: {e.id}, TaskID: {e.task_id}, Status: {e.status}, Created: {e.started_at}")
        print(f"  Droplets Created: {e.droplets_created}, Success: {e.success_count}, Failure: {e.failure_count}")
        print(f"  Error: {e.error_message}")
        
        # Check associated droplets
        droplets = DigitalOceanDroplet.query.filter_by(execution_task_id=e.task_id).all()
        print(f"  Associated Droplets: {len(droplets)}")
        for d in droplets:
            print(f"    - DropletID: {d.droplet_id}, IP: {d.ip_address}, Status: {d.status}")
        print("-" * 40)
