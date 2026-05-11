# This is the CORRECT workflow order for run_prep_process function
# Copy this to replace the corrupted section in prep_local.py

def run_prep_process(email, password, aws_session, s3_bucket):
    """Main execution function called by aws.py"""
    driver = None
    try:
        driver = get_local_driver()
        
        if not login_google(driver, email, password):
            return "Login Failed"
            
        if not open_cloud_shell(driver):
            return "Cloud Shell Failed"
            
        # Generate IDs
        timestamp = str(int(time.time()))
        project_id = f"edu-gw-{timestamp}"
        sa_name = f"sa-{timestamp}"
        key_path = f"~/edu-gw-{timestamp}.json"
        
        # Press Enter to clear terminal
        print("Pressing Enter to clear terminal...")
        send_terminal_command(driver, "")
        time.sleep(2)
        
        # ============================================================
        # STEP 1: Create Project and Service Account FIRST
        # ============================================================
        print("\n" + "="*80)
        print("STEP 1: Creating GCP Project and Service Account")
        print("="*80 + "\n")
        
        send_terminal_command(driver, f"gcloud projects create {project_id} --name 'my first project'")
        time.sleep(10)
        
        send_terminal_command(driver, f"gcloud config set project {project_id}")
        time.sleep(5)
        
        send_terminal_command(driver, f"gcloud iam service-accounts create {sa_name} --project {project_id} --display-name 'Automation SA'")
        time.sleep(10)
        
        # ============================================================
        # STEP 2: Grant Organization Permissions
        # ============================================================
        print("\n" + "="*80)
        print("STEP 2: Granting Organization Policy Administrator Role")
        print("="*80 + "\n")
        
        send_terminal_command(driver, "gcloud organizations list")
        time.sleep(5)
        
        send_terminal_command(driver, "ORG_ID=$(gcloud organizations list --format='value(name)' --limit=1)")
        time.sleep(2)
        
        send_terminal_command(driver, f"gcloud organizations add-iam-policy-binding $ORG_ID --member='user:{email}' --role='roles/orgpolicy.policyAdmin'")
        time.sleep(8)
        
        # ============================================================
        # STEP 3: Disable IAM Policy
        # ============================================================
        print("\n" + "="*80)
        print("STEP 3: Disabling Service Account Key Creation Policy")
        print("="*80 + "\n")
        
        send_terminal_command(driver, f"gcloud resource-manager org-policies disable-enforce iam.disableServiceAccountKeyCreation --project={project_id}")
        time.sleep(8)
        
        # ============================================================
        # STEP 4: Wait for Policy Propagation
        # ============================================================
        print("\n" + "="*80)
        print("⏳ STEP 4: WAITING FOR ORGANIZATION POLICY PROPAGATION...")
        print("Checking every 30 seconds (max 5 minutes)...")
        print("="*80 + "\n")
        
        for attempt in range(1, 11):
            print(f"[Attempt {attempt}/10] Checking if policy has propagated...")
            send_terminal_command(driver, f"gcloud iam service-accounts keys list --iam-account={sa_name}@{project_id}.iam.gserviceaccount.com 2>&1")
            time.sleep(3)
            
            if attempt < 10:
                print(f"Waiting 30 seconds...")
                time.sleep(30)
        
        print("\n✅ Policy propagation wait complete\n")
        
        # ============================================================
        # STEP 5: Enable APIs and Create Key
        # ============================================================
        print("\n" + "="*80)
        print("STEP 5: Enabling APIs and Creating Service Account Key")
        print("="*80 + "\n")
        
        send_terminal_command(driver, f"gcloud services enable admin.googleapis.com --project {project_id}")
        time.sleep(10)
        
        send_terminal_command(driver, f"gcloud services enable siteverification.googleapis.com --project {project_id}")
        time.sleep(10)
        
        send_terminal_command(driver, f"gcloud iam service-accounts keys create {key_path} --project {project_id} --iam-account {sa_name}@{project_id}.iam.gserviceaccount.com")
        time.sleep(10)
        
        # ============================================================
        # STEP 6: Download Key
        # ============================================================
        print("\n" + "="*80)
        print("STEP 6: Downloading Service Account Key")
        print("="*80 + "\n")
        
        send_terminal_command(driver, f"cloudshell download {key_path}")
        time.sleep(3)
        
        # Switch out of iframe to click download button
        driver.switch_to.default_content()
        time.sleep(2)
        
        # Click download button
        clicked = False
        try:
            dialog = WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.TAG_NAME, "dialog-overlay"))
            )
            download_btn = dialog.find_element(By.XPATH, ".//button[.//span[contains(text(), 'Download')]]")
            download_btn.click()
            print("✅ Clicked Download button!")
            clicked = True
        except:
            pass
        
        if not clicked:
            try:
                download_btn = WebDriverWait(driver, 5).until(
                    EC.element_to_be_clickable((By.XPATH, "//mat-dialog-container//button[contains(., 'Download')]"))
                )
                download_btn.click()
                print("✅ Clicked Download button!")
                clicked = True
            except:
                print("⚠ Could not click download button")
        
        time.sleep(10)
        
        # ============================================================
        # STEP 7: Upload to S3
        # ============================================================
        downloads_dir = os.path.join(os.path.expanduser("~"), "Downloads")
        downloaded_file = os.path.join(downloads_dir, f"edu-gw-{timestamp}.json")
        
        if os.path.exists(downloaded_file):
            print(f"\n✅ Found downloaded key: {downloaded_file}")
            print(f"📤 Uploading to S3 bucket: {s3_bucket}...")
            
            s3 = aws_session.client("s3")
            s3_key = f"workspace-keys/{email}.json"
            s3.upload_file(downloaded_file, s3_bucket, s3_key)
            
            print(f"✅ SUCCESS! Key uploaded to s3://{s3_bucket}/{s3_key}")
            return f"Success! Key uploaded to s3://{s3_bucket}/{s3_key}"
        else:
            print(f"❌ File not found in {downloads_dir}")
            return "Failed to find downloaded key file"

    except Exception as e:
        print(f"❌ Error: {e}")
        return str(e)
    finally:
        if driver:
            print("\nClosing browser...")
            driver.quit()
