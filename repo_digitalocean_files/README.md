# DigitalOcean Droplet Files

This directory contains files used for DigitalOcean droplet preparation and automation.

## Files:

### setup_droplet.sh
Bash script that prepares an Ubuntu 22.04 droplet with:
- Google Chrome (latest stable)
- ChromeDriver (matching Chrome version)
- Python 3 and required packages (selenium, paramiko, pyotp, etc.)

**Note:** You don't need to manually upload this - it's automatically downloaded from GitHub!

### do_automation.py
Python script for Google Workspace automation that runs on the droplet.
This is a simplified version of the AWS Lambda automation adapted for running on regular VMs.

**Note:** This is currently a placeholder structure. The full automation logic from
`repo_aws_files/main.py` needs to be adapted and implemented here.

### cloud_init.sh
Cloud-init script that automatically runs when a droplet is created.
This is embedded in the droplet creation API call.

## 🚀 Automated Workflow (GitHub Integration):

### How It Works:
When you create an initial droplet through the UI, the system automatically:

1. **Creates Ubuntu 22.04 droplet** with cloud-init user_data
2. **Cloud-init runs automatically** on first boot:
   - Clones your GitHub repository
   - Runs `setup_droplet.sh` to install Chrome, Python, etc.
   - Copies `do_automation.py` to `/opt/automation/`
   - Creates marker file `/root/.setup_complete`
3. **Wait 5-10 minutes** for setup to complete
4. **Verify setup**: SSH into droplet and check:
   ```bash
   cat /root/.setup_complete
   ls -la /opt/automation/
   google-chrome --version
   ```
5. **Create snapshot** via the UI once setup is verified

### OS Used:
- **Ubuntu 22.04 LTS** (`ubuntu-22-04-x64`)
- This is the official DigitalOcean Ubuntu 22.04 image
- Fully compatible with Chrome/ChromeDriver installation
- Matches the AWS Lambda environment closely

### No Manual Uploads Required!
✅ Files are downloaded directly from GitHub  
✅ Setup runs automatically via cloud-init  
✅ Just wait for completion and create snapshot  

## Manual Setup (Optional/Legacy):

If you prefer manual setup:

1. Create droplet manually
2. SSH into droplet
3. Clone repository or download files
4. Run `bash setup_droplet.sh`

## Next Steps After Snapshot:

1. **Note the Snapshot ID** from the snapshots list
2. **Use snapshot for bulk execution** - select it in the bulk execution form
3. **Bulk execution will**:
   - Create multiple droplets from your snapshot
   - Each droplet already has Chrome, Python, and automation script installed
   - Execute automation on assigned users via SSH
   - Collect results and destroy droplets (if enabled)
