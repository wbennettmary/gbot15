import boto3
import json
import sys

import os

# Configuration
REGION = "eu-west-1"
FUNC_NAME = "edu-gw-prep-worker-eu-west-1-1"
PAYLOAD = {
    "email": "test@example.com",
    "password": "password123"
}

def debug_invoke():
    print(f"Attempting to invoke {FUNC_NAME} in {REGION}...")
    
    aws_access_key = os.environ.get("AWS_ACCESS_KEY_ID")
    aws_secret_key = os.environ.get("AWS_SECRET_ACCESS_KEY")
    
    if not aws_access_key:
        aws_access_key = input("Enter AWS Access Key ID: ").strip()
    if not aws_secret_key:
        aws_secret_key = input("Enter AWS Secret Access Key: ").strip()
    
    if not aws_access_key or not aws_secret_key:
        print("ERROR: Credentials are required.")
        return

    try:
        session = boto3.Session(
            aws_access_key_id=aws_access_key,
            aws_secret_access_key=aws_secret_key,
            region_name=REGION
        )
        lam = session.client("lambda")
        
        print("Invoking (RequestResponse)...")
        resp = lam.invoke(
            FunctionName=FUNC_NAME,
            InvocationType="RequestResponse", # Sync for debugging to see output
            Payload=json.dumps(PAYLOAD).encode("utf-8")
        )
        
        status = resp.get("StatusCode")
        print(f"Status Code: {status}")
        
        payload = resp.get("Payload").read().decode("utf-8")
        print(f"Response Payload: {payload}")
        
        if status == 200:
            print("SUCCESS: Lambda invoked.")
        else:
            print("FAILURE: Unexpected status code.")
            
    except Exception as e:
        print(f"ERROR: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    debug_invoke()
