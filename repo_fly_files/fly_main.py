import os
import time
import json
import logging
import threading
from flask import Flask, request, jsonify
import fly_worker

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - [API] - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# Check for Batch Mode on Import/Start
if os.environ.get('BATCH_DATA') or os.environ.get('BATCH_DATA_B64'):
    logger.info("BATCH_DATA detected. Running in Batch Mode...")
    # Delay flask start (or don't start it main block if we run this directly)
    pass 

@app.route('/health', methods=['GET'])
def health_check():
    """
    Health check endpoint for Fly.io and clients.
    """
    return jsonify({"status": "healthy", "service": "gbot-fly-worker"}), 200

@app.route('/process', methods=['POST'])
def process_batch():
    """
    Process a batch of users provided in the JSON body.
    Expected JSON: { "users": [ ... ] }
    """
    try:
        data = request.json
        if not data or 'users' not in data:
            return jsonify({"error": "Invalid payload, 'users' list required"}), 400
            
        users = data['users']
        logger.info(f"Received batch of {len(users)} users for processing")
        
        results = []
        driver = None
        
        try:
            # Initialize Driver
            logger.info("Initializing Chrome Driver...")
            driver = fly_worker.get_chrome_driver()
            
            # Process users
            for i, user in enumerate(users):
                logger.info(f"Processing user {i+1}/{len(users)}: {user.get('email')}")
                
                # Check for shutdown signal? (Optional)
                
                # Run worker logic
                user_result = fly_worker.process_single_user(driver, user)
                results.append(user_result)
                
                # Clean up between users
                try:
                    driver.delete_all_cookies()
                except: pass
                
                # Small delay
                if i < len(users) - 1:
                    time.sleep(2)
                    
        except Exception as e:
            logger.error(f"Batch processing error: {e}")
            return jsonify({"error": str(e), "partial_results": results}), 500
            
        finally:
            if driver:
                logger.info("Quitting Chrome Driver...")
                driver.quit()
        
        logger.info("Batch processing complete")
        return jsonify({"status": "completed", "results": results})

    except Exception as e:
        logger.error(f"Global endpoint error: {e}")
        return jsonify({"error": "Internal Server Error"}), 500

if __name__ == '__main__':
    if os.environ.get('BATCH_DATA') or os.environ.get('BATCH_DATA_B64'):
        logger.info("Starting Worker in BATCH MODE")
        fly_worker.main()
    else:
        logger.info("Starting Worker in SERVER MODE")
        # Get port from env (Fly.io sets PORT)
        port = int(os.environ.get('PORT', 8080))
        # Run Flask
        app.run(host='0.0.0.0', port=port)
