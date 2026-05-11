"""
Script to fix the gcloud auth race condition by rewriting the output reading logic
This replaces the complex threading approach with a simpler, robust polling approach
"""

# Read the current file
with open('repo_aws_files/prep.py', 'r', encoding='utf-8') as f:
    content = f.read()

# Find and replace the _gcloud_auth_flow_internal function
old_function_start = "def _gcloud_auth_flow_internal(email, password):"

if old_function_start not in content:
    print("ERROR: Could not find _gcloud_auth_flow_internal function")
    exit(1)

# Find the start position
start_idx = content.find(old_function_start)
print(f"Found function at position: {start_idx}")

# Find the end of this function (next def at same indentation level)
after_start = content[start_idx:]
lines = after_start.split('\n')

end_line_idx = None
for i, line in enumerate(lines):
    if i > 0 and line.startswith('def ') and not line.startswith('def _gcloud'):
        end_line_idx = i
        break

if end_line_idx is None:
    print("ERROR: Could not find end of function")
    exit(1)

print(f"Function ends at line {end_line_idx} of the function")

# Reconstruct the content
before_func = content[:start_idx]
after_func = '\n'.join(lines[end_line_idx:])

# New function implementation
new_function = '''def _gcloud_auth_flow_internal(email, password):
    """Internal gcloud auth flow (called with lock held) - SIMPLIFIED VERSION"""
    driver = None
    process = None
    
    try:
        # Start gcloud auth login process with required scopes
        # The scope is CRITICAL - without it, OAuth returns "Missing required parameter: scope"
        cmd = [
            "gcloud", "auth", "login", 
            "--no-launch-browser",
            "--scopes=https://www.googleapis.com/auth/cloud-platform,https://www.googleapis.com/auth/cloudplatformprojects"
        ]
        print(f"Executing: {' '.join(cmd)}")
        process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,  # Combine stderr with stdout
            text=True,
            bufsize=1  # Line buffered for better output handling
        )
        
        # Read output to get auth URL - SIMPLIFIED ROBUST APPROACH
        auth_url = None
        full_output = ""
        
        print("Waiting for gcloud to output auth URL...")
        timeout = 60  # Generous timeout
        start_time = time.time()
        
        # Use threading for reading but with proper synchronization
        output_ready = threading.Event()
        
        def read_all_output():
            """Read all available output from the process"""
            nonlocal full_output
            try:
                for line in iter(process.stdout.readline, ''):
                    if line:
                        full_output += line
                        print(f"gcloud output: {line.strip()[:100]}")
                        # Signal that we have some output
                        output_ready.set()
                    if process.poll() is not None:
                        break
            except Exception as e:
                print(f"Error reading output: {e}")
        
        # Start reader thread
        reader_thread = threading.Thread(target=read_all_output, daemon=True)
        reader_thread.start()
        
        # Wait for output and try to extract URL periodically
        while time.time() - start_time < timeout:
            # Wait up to 2 seconds for new output
            if output_ready.wait(timeout=2):
                # We have some output, try to extract URL
                auth_url = extract_auth_url_from_gcloud(full_output)
                if auth_url:
                    print(f"Found auth URL: {auth_url[:60]}... (length: {len(auth_url)})")
                    break
                output_ready.clear()
            
            # Check if process ended
            if process.poll() is not None:
                # Give reader thread time to finish
                reader_thread.join(timeout=2)
                # Final attempt to extract URL
                auth_url = extract_auth_url_from_gcloud(full_output)
                break
        
        # If still no URL, do one final read
        if not auth_url:
            reader_thread.join(timeout=3)
            auth_url = extract_auth_url_from_gcloud(full_output)
            if auth_url:
                print(f"Found auth URL in final check: {auth_url[:60]}...")
        
        if not auth_url:
            print("ERROR: Could not extract auth URL from gcloud output")
            print(f"Accumulated output length: {len(full_output)}")
            if full_output:
                print(f"Output: {full_output[:800]}")
            if process:
                try:
                    process.kill()
                    process.wait(timeout=5)
                except:
                    pass
            return False
        
        print(f"SUCCESS: Extracted auth URL ({len(auth_url)} chars)")
        
        # Initialize Chrome driver
        print("Initializing Chrome driver...")
        driver = get_chrome_driver()
        
        # Get verification code
        code = get_gcloud_verification_code(driver, auth_url, email, password)
        if not code:
            print("Failed to get verification code.")
            if process:
                process.kill()
            return False
        
        # Send code to gcloud
        print("Sending verification code to gcloud...")
        process.stdin.write(code + "\\n")
        process.stdin.flush()
        process.stdin.close()
        
        # Wait for process to finish
        stdout, stderr = process.communicate(timeout=60)
        print(f"gcloud auth finished. Return code: {process.returncode}")
        if stdout:
            print(f"stdout: {stdout[:500]}")
        if stderr:
            print(f"stderr: {stderr[:500]}")
        
        if process.returncode == 0:
            print("gcloud auth successful.")
            return True
        else:
            print("gcloud auth failed.")
            return False
            
    except subprocess.TimeoutExpired:
        print("gcloud auth process timed out")
        if process:
            process.kill()
        return False
    except Exception as e:
        print(f"Exception during auth flow: {e}")
        import traceback
        traceback.print_exc()
        if process:
            try:
                process.kill()
            except:
                pass
        return False
    finally:
        if driver:
            try:
                driver.quit()
            except:
                pass

'''

# Write the updated content
new_content = before_func + new_function + after_func

with open('repo_aws_files/prep.py', 'w', encoding='utf-8') as f:
    f.write(new_content)

print("✓ Successfully updated _gcloud_auth_flow_internal function")
print("✓ Added OAuth scopes to gcloud auth command")
print("✓ Simplified output reading logic to fix race condition")
