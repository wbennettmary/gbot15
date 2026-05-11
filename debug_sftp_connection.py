import paramiko
import os
import time

# Configuration from your screenshot and codebase
DROPLET_IP = "164.92.153.156"
SSH_KEY_PATH = "C:/Users/PC/Desktop/Gbot-v15/edu-gw-creation-key.pem"
REMOTE_DIR = "/opt/automation"

def test_connection():
    print(f"Testing connection to {DROPLET_IP}...")
    
    if not os.path.exists(SSH_KEY_PATH):
        print(f"❌ SSH Key file NOT FOUND at: {SSH_KEY_PATH}")
        return

    try:
        # 1. SSH Connection
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        
        print(f"Attempting SSH connection using key: {SSH_KEY_PATH}")
        k = paramiko.RSAKey.from_private_key_file(SSH_KEY_PATH)
        ssh.connect(DROPLET_IP, username="root", pkey=k)
        print("✅ SSH Connection Successful")

        # 2. mkdir -p /opt/automation
        print(f"Attempting: mkdir -p {REMOTE_DIR}")
        stdin, stdout, stderr = ssh.exec_command(f"mkdir -p {REMOTE_DIR}")
        exit_status = stdout.channel.recv_exit_status()
        
        if exit_status == 0:
            print(f"✅ Created/Verified directory: {REMOTE_DIR}")
        else:
            print(f"❌ Failed to create directory. Exit code: {exit_status}")
            print(f"Stderr: {stderr.read().decode()}")
            return

        # 3. SFTP Session
        print("Opening SFTP session...")
        sftp = ssh.open_sftp()
        print("✅ SFTP Session Opened")

        # 4. Upload Test
        local_test_file = "test_upload.txt"
        with open(local_test_file, "w") as f:
            f.write("Test upload content")
        
        remote_test_file = f"{REMOTE_DIR}/test_upload.txt"
        print(f"Attempting to upload {local_test_file} to {remote_test_file}...")
        
        try:
            sftp.put(local_test_file, remote_test_file)
            print(f"✅ SFTP Upload Successful!")
        except Exception as e:
            print(f"❌ SFTP Upload FAILED: {str(e)}")
        
        sftp.close()
        ssh.close()
        
        # Cleanup local file
        if os.path.exists(local_test_file):
            os.remove(local_test_file)

    except Exception as e:
        print(f"\n❌ CRITICAL ERROR: {str(e)}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    test_connection()
