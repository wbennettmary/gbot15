
import logging
import sys

# Setup logging to console
logging.basicConfig(level=logging.INFO, format='%(message)s', handlers=[logging.StreamHandler(sys.stdout)])
logger = logging.getLogger()

def test_distribution():
    print("Testing User Distribution Logic...")
    
    # Mock inputs
    users = [{"email": f"user{i}@test.com", "password": "pw"} for i in range(12)]
    users_per_function = 12
    all_lambdas_flat = [('af-south-1', 'admin-chromium')] # Single lambda case
    
    USERS_PER_FUNCTION = users_per_function
    
    user_batches = []
    current_lambda_idx = 0
    current_batch_users = []
    
    # Copy of the SEQUENTIAL FILL logic from aws_manager.py
    for user_idx, user in enumerate(users):
        current_batch_users.append(user)
        
        # If we've reached the limit for this Lambda, or this is the last user
        if len(current_batch_users) >= USERS_PER_FUNCTION or user_idx == len(users) - 1:
            # Get the Lambda for this batch
            geo, function_name = all_lambdas_flat[current_lambda_idx % len(all_lambdas_flat)]
            
            # Add the batch
            user_batches.append([geo, function_name, current_batch_users.copy()])
            logger.info(f"[BULK] Created batch for {function_name}: {len(current_batch_users)} users")
            
            # Move to next Lambda and reset batch
            current_lambda_idx += 1
            current_batch_users = []

    print("\nResults:")
    print(f"Total Batches: {len(user_batches)}")
    for i, batch in enumerate(user_batches):
        print(f"Batch {i+1}: Lambda={batch[1]}, Users={len(batch[2])}")
        
    if len(user_batches) == 1 and len(user_batches[0][2]) == 12:
        print("\n✅ SUCCESS: 12 users distributed to 1 batch of 12.")
    else:
        print("\n❌ FAILURE: Distribution did not match expectations.")

if __name__ == "__main__":
    test_distribution()

import os
import time

print('
Checking routes/aws_manager.py timestamp...')
mtime = os.path.getmtime('routes/aws_manager.py')
print(f'Last Modified: {time.ctime(mtime)}')
