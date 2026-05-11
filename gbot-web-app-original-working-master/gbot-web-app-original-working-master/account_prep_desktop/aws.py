import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
import boto3
from botocore.exceptions import ClientError
import json
import io
import zipfile
import time
import traceback
import struct
import base64
import hmac
import hashlib
import os
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

# ======================================================================
# Config / Constants
# ======================================================================

APP_TITLE = "Google Workspace Automation – Production Lambda Controller"

# Core resources
LAMBDA_ROLE_NAME = "edu-gw-app-password-lambda-role"
PRODUCTION_LAMBDA_NAME = "edu-gw-chromium"
S3_BUCKET_NAME = "edu-gw-app-passwords"

# ECR
ECR_REPO_NAME = "edu-gw-app-password-worker-repo"
ECR_IMAGE_TAG = "latest"

# Prep Process Resources
PREP_LAMBDA_PREFIX = "edu-gw-prep-worker"
PREP_ECR_REPO_NAME = "edu-gw-prep-worker-repo"

# GitHub repo for Selenium Lambda image
GITHUB_REPO_URL = "https://github.com/umihico/docker-selenium-lambda.git"

# EC2 build box configuration
EC2_INSTANCE_NAME = "edu-gw-ec2-build-box"
EC2_ROLE_NAME = "edu-gw-ec2-build-role"
EC2_INSTANCE_PROFILE_NAME = "edu-gw-ec2-build-instance-profile"
EC2_SECURITY_GROUP_NAME = "edu-gw-ec2-build-sg"
EC2_KEY_PAIR_NAME = "edu-gw-ec2-build-key"
EC2_KEY_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    f"{EC2_KEY_PAIR_NAME}.pem"
)


# ======================================================================
# Main Tkinter Application
# ======================================================================

class AwsEducationApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1250x900")

        self.session = None
        self.aws_account_id = None
        
        # Initialize execution mode variable early
        self.execution_mode_var = tk.StringVar(value="single")
        
        # Thread management for local prep
        self.local_prep_thread = None
        self.stop_event = None

        self._build_ui()

    # ------------------------------------------------------------------
    # UI
    # ------------------------------------------------------------------

    def _build_ui(self):
        main = ttk.Frame(self, padding=10)
        main.pack(fill="both", expand=True)

        # ---------- Main Layout (PanedWindow) ----------
        # Use a PanedWindow to allow resizing between the top content and the logs
        self.paned_window = tk.PanedWindow(main, orient=tk.VERTICAL, sashrelief=tk.RAISED)
        self.paned_window.pack(fill="both", expand=True)
        
        # Top Frame (Scrollable Canvas for Inputs/Tabs)
        # We wrap the top part in a canvas to allow scrolling if the window is too small
        self.top_canvas_frame = ttk.Frame(self.paned_window)
        self.paned_window.add(self.top_canvas_frame, minsize=400)
        
        # Create canvas and scrollbar for top content
        self.canvas = tk.Canvas(self.top_canvas_frame)
        self.scrollbar = ttk.Scrollbar(self.top_canvas_frame, orient="vertical", command=self.canvas.yview)
        self.scrollable_frame = ttk.Frame(self.canvas)
        
        self.scrollable_frame.bind(
            "<Configure>",
            lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        )
        
        self.canvas.create_window((0, 0), window=self.scrollable_frame, anchor="nw")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        
        self.canvas.pack(side="left", fill="both", expand=True)
        self.scrollbar.pack(side="right", fill="y")
        
        # Move existing content into scrollable_frame instead of 'main'
        # ---------- Credentials ----------
        creds_frame = ttk.LabelFrame(self.scrollable_frame, text="AWS & G-Workspace Credentials", padding=10)
        creds_frame.pack(fill="x", padx=5, pady=5)

        aws_frame = ttk.LabelFrame(creds_frame, text="AWS Setup", padding=10)
        aws_frame.grid(row=0, column=0, padx=10, pady=5, sticky="n")

        ttk.Label(aws_frame, text="Access Key ID:").grid(row=0, column=0, sticky="e", padx=5, pady=2)
        self.access_key_var = tk.StringVar()
        ttk.Entry(aws_frame, textvariable=self.access_key_var, width=30).grid(
            row=0, column=1, sticky="w", padx=5, pady=2
        )

        ttk.Label(aws_frame, text="Secret Access Key:").grid(row=1, column=0, sticky="e", padx=5, pady=2)
        self.secret_key_var = tk.StringVar()
        ttk.Entry(aws_frame, textvariable=self.secret_key_var, width=30, show="*").grid(
            row=1, column=1, sticky="w", padx=5, pady=2
        )

        ttk.Label(aws_frame, text="Region:").grid(row=2, column=0, sticky="e", padx=5, pady=2)
        self.region_var = tk.StringVar(value="eu-west-1")
        ttk.Entry(aws_frame, textvariable=self.region_var, width=18).grid(
            row=2, column=1, sticky="w", padx=5, pady=2
        )

        self.connect_button = ttk.Button(aws_frame, text="Test Connection", command=self.on_test_connection)
        self.connect_button.grid(row=0, column=2, rowspan=3, padx=10, pady=2, sticky="ns")

        gw_frame = ttk.LabelFrame(creds_frame, text="ECR Image & Configuration", padding=10)
        gw_frame.grid(row=0, column=1, padx=10, pady=5, sticky="n")

        ttk.Label(gw_frame, text="ECR Image URI (Chromium Lambda):").grid(
            row=0, column=0, sticky="e", padx=5, pady=2
        )
        self.ecr_uri_var = tk.StringVar(value="(connect first)")
        ttk.Entry(gw_frame, textvariable=self.ecr_uri_var, width=55, state="readonly").grid(
            row=0, column=1, sticky="w", padx=5, pady=2
        )
        ttk.Label(gw_frame, text="Built on EC2 from repo: docker-selenium-lambda").grid(
            row=1, column=1, sticky="w", padx=5, pady=2
        )
        
        # S3 Configuration
        ttk.Separator(gw_frame, orient="horizontal").grid(row=2, column=0, columnspan=2, sticky="ew", pady=5)
        ttk.Label(gw_frame, text="S3 Bucket (App Passwords):").grid(row=3, column=0, sticky="e", padx=5, pady=2)
        self.s3_bucket_var = tk.StringVar(value=S3_BUCKET_NAME)
        ttk.Entry(gw_frame, textvariable=self.s3_bucket_var, width=30).grid(
            row=3, column=1, sticky="w", padx=5, pady=2
        )
        
        # SFTP Configuration
        ttk.Separator(gw_frame, orient="horizontal").grid(row=4, column=0, columnspan=2, sticky="ew", pady=5)
        ttk.Label(gw_frame, text="SFTP Host:").grid(row=5, column=0, sticky="e", padx=5, pady=2)
        self.sftp_host_var = tk.StringVar(value="46.224.9.127")
        ttk.Entry(gw_frame, textvariable=self.sftp_host_var, width=30).grid(
            row=5, column=1, sticky="w", padx=5, pady=2
        )
        
        ttk.Label(gw_frame, text="SFTP User:").grid(row=6, column=0, sticky="e", padx=5, pady=2)
        self.sftp_user_var = tk.StringVar()
        ttk.Entry(gw_frame, textvariable=self.sftp_user_var, width=30).grid(
            row=6, column=1, sticky="w", padx=5, pady=2
        )
        
        ttk.Label(gw_frame, text="SFTP Password:").grid(row=7, column=0, sticky="e", padx=5, pady=2)
        self.sftp_password_var = tk.StringVar()
        ttk.Entry(gw_frame, textvariable=self.sftp_password_var, width=30, show="*").grid(
            row=7, column=1, sticky="w", padx=5, pady=2
        )
        
        ttk.Label(gw_frame, text="SFTP Remote Dir:").grid(row=8, column=0, sticky="e", padx=5, pady=2)
        self.sftp_dir_var = tk.StringVar(value="/home/brightmindscampus/")
        ttk.Entry(gw_frame, textvariable=self.sftp_dir_var, width=30).grid(
            row=8, column=1, sticky="w", padx=5, pady=2
        )

        # ---------- Notebook ----------
        notebook = ttk.Notebook(self.scrollable_frame)
        notebook.pack(fill="both", expand=True, padx=5, pady=5)

        infra_tab = ttk.Frame(notebook)
        lambda_tab = ttk.Frame(notebook)
        ec2_tab = ttk.Frame(notebook)

        notebook.add(infra_tab, text="1) Core Infrastructure")
        notebook.add(lambda_tab, text="2) Production Lambda")
        notebook.add(ec2_tab, text="3) EC2 Build Box (Docker)")
        
        prep_tab = ttk.Frame(notebook)
        notebook.add(prep_tab, text="4) Prep Process")
        self._build_prep_tab(prep_tab)

        # ----- Infra tab -----
        infra_frame = ttk.LabelFrame(infra_tab, text="Core AWS Resources", padding=10)
        infra_frame.pack(fill="x", padx=5, pady=5)

        ttk.Button(
            infra_frame,
            text="Create Core Resources (IAM, ECR, S3)",
            command=self.on_create_infrastructure,
        ).pack(fill="x", pady=4)
        
        ttk.Button(
            infra_frame,
            text="Create ECR Repository (Manual)",
            command=self.on_create_ecr_manual,
        ).pack(fill="x", pady=4)

        ttk.Button(
            infra_frame,
            text="Inspect Resources (IAM / ECR / Lambdas)",
            command=self.on_inspect_resources,
        ).pack(fill="x", pady=4)

        # ----- Lambda tab -----
        lambda_frame = ttk.LabelFrame(lambda_tab, text="Production Lambda Management", padding=10)
        lambda_frame.pack(fill="x", padx=5, pady=5)

        ttk.Label(
            lambda_frame,
            text=(
                "Production Workflow:\n"
                "1) Create Core Resources (IAM, ECR, S3)\n"
                "2) Use EC2 tab to build/push Docker image\n"
                "3) Create/Update Production Lambda\n"
                "4) Invoke Lambda (Single or Multiple accounts)"
            ),
        ).pack(fill="x", pady=4)

        ttk.Button(
            lambda_frame,
            text="Create / Update Production Lambda",
            command=self.on_create_lambdas,
        ).pack(fill="x", pady=4)

        # Execution mode selection
        mode_frame = ttk.LabelFrame(lambda_frame, text="Execution Mode", padding=5)
        mode_frame.pack(fill="x", pady=4)
        
        # execution_mode_var is already initialized in __init__
        ttk.Radiobutton(
            mode_frame,
            text="Single Account",
            variable=self.execution_mode_var,
            value="single"
        ).pack(side="left", padx=10)
        
        ttk.Radiobutton(
            mode_frame,
            text="Multiple Accounts (Parallel)",
            variable=self.execution_mode_var,
            value="multiple"
        ).pack(side="left", padx=10)
        
        # Account input section (works for both single and multiple modes)
        multi_frame = ttk.LabelFrame(lambda_frame, text="Account Input (user:password, one per line)", padding=5)
        multi_frame.pack(fill="both", expand=True, pady=4)
        
        ttk.Label(
            multi_frame,
            text="Single Account Mode: Enter one line (email:password)\nMultiple Accounts Mode: Enter multiple lines (one per line)",
            font=("Arial", 9)
        ).pack(anchor="w", pady=2)
        
        self.multiple_users_text = scrolledtext.ScrolledText(
            multi_frame,
            height=8,
            width=60,
            wrap=tk.WORD
        )
        self.multiple_users_text.pack(fill="both", expand=True, pady=2)
        
        # Status label for multiple executions
        self.multi_status_label = ttk.Label(
            multi_frame,
            text="Ready",
            font=("Arial", 9)
        )
        self.multi_status_label.pack(anchor="w", pady=2)
        
        ttk.Button(
            lambda_frame,
            text="Invoke Production Lambda",
            command=self.on_invoke_production_lambda,
        ).pack(fill="x", pady=4)
        
        ttk.Button(
            lambda_frame,
            text="Delete All Lambdas",
            command=self.on_delete_all_lambdas,
        ).pack(fill="x", pady=4)
        
        # Cleanup buttons frame
        cleanup_label = ttk.Label(lambda_frame, text="--- Cleanup Operations ---", font=("Arial", 9, "bold"))
        cleanup_label.pack(fill="x", pady=(8, 4))
        
        ttk.Button(
            lambda_frame,
            text="Delete S3 Bucket Content",
            command=self.on_delete_s3_content,
        ).pack(fill="x", pady=2)
        
        ttk.Button(
            lambda_frame,
            text="Delete ECR Repository",
            command=self.on_delete_ecr_repo,
        ).pack(fill="x", pady=2)
        
        ttk.Button(
            lambda_frame,
            text="Delete CloudWatch Logs",
            command=self.on_delete_cloudwatch_logs,
        ).pack(fill="x", pady=2)

        # ----- EC2 tab -----
        ec2_frame = ttk.LabelFrame(ec2_tab, text="EC2 Build Box (Free Tier t2.micro, Amazon Linux 2)", padding=10)
        ec2_frame.pack(fill="x", padx=5, pady=5)

        ttk.Label(
            ec2_frame,
            text=(
                "This EC2 instance automatically:\n"
                " - Installs Docker + AWS CLI + git\n"
                " - Clones docker-selenium-lambda\n"
                " - Builds image based on umihico/aws-lambda-selenium-python\n"
                " - Pushes it to your ECR repo as :latest\n"
            ),
        ).pack(fill="x", pady=4)

        ttk.Button(
            ec2_frame,
            text="Create / Prepare EC2 Build Box",
            command=self.on_ec2_create_build_box,
        ).pack(fill="x", pady=4)

        ttk.Button(
            ec2_frame,
            text="Show EC2 Build Box Status",
            command=self.on_ec2_show_status,
        ).pack(fill="x", pady=4)

        ttk.Button(
            ec2_frame,
            text="Terminate EC2 Build Box",
            command=self.on_ec2_terminate,
        ).pack(fill="x", pady=4)

        # ----- Logs (Bottom Pane) -----
        log_frame = ttk.LabelFrame(self.paned_window, text="Log Output", padding=5)
        self.paned_window.add(log_frame, minsize=150)

        self.log_text = scrolledtext.ScrolledText(log_frame, wrap="word", height=10)
        self.log_text.pack(fill="both", expand=True)

        self.status_var = tk.StringVar(value="Ready.")
        status_bar = ttk.Label(self, textvariable=self.status_var, anchor="w", relief="sunken")
        status_bar.pack(fill="x", side="bottom")

    def _build_prep_tab(self, parent):
        """Build the UI for the Prep Process tab."""
        # 1. Infrastructure
        infra_frame = ttk.LabelFrame(parent, text="1. Prep Infrastructure", padding=10)
        infra_frame.pack(fill="x", padx=5, pady=5)
        
        ttk.Button(infra_frame, text="Create Prep Infrastructure (ECR)", command=self.on_prep_create_infrastructure).pack(fill="x", pady=2)
        
        # 2. Build
        build_frame = ttk.LabelFrame(parent, text="2. Build Prep Image (EC2)", padding=10)
        build_frame.pack(fill="x", padx=5, pady=5)
        
        ttk.Label(build_frame, text="Launches EC2 to build image from repo_aws_files/prep.py & Dockerprep").pack(anchor="w")
        ttk.Button(build_frame, text="Launch Prep Build Box", command=self.on_prep_launch_build_box).pack(fill="x", pady=2)
        ttk.Button(build_frame, text="Terminate Prep Build Box", command=self.on_prep_terminate_build_box).pack(fill="x", pady=2)
        
        # 3. Deployment
        deploy_frame = ttk.LabelFrame(parent, text="3. Deploy Prep Lambdas", padding=10)
        deploy_frame.pack(fill="x", padx=5, pady=5)
        
        ttk.Label(deploy_frame, text="Target Region:").pack(anchor="w")
        self.prep_region_var = tk.StringVar(value="Default (All Regions)")
        regions = ["Default (All Regions)", "us-east-1", "us-west-2", "eu-west-1", "ap-northeast-1", "sa-east-1"]
        ttk.Combobox(deploy_frame, textvariable=self.prep_region_var, values=regions, state="readonly").pack(fill="x", pady=2)
        
        ttk.Label(deploy_frame, text="Deploys 'edu-gw-prep-worker-{region}-{id}' across selected region(s)").pack(anchor="w")
        ttk.Button(deploy_frame, text="Create/Update Prep Lambdas", command=self.on_prep_create_lambdas).pack(fill="x", pady=2)
        
        # 4. Execution
        exec_frame = ttk.LabelFrame(parent, text="4. Execute Prep Process", padding=10)
        exec_frame.pack(fill="both", expand=True, padx=5, pady=5)
        
        ttk.Label(exec_frame, text="Input Users (email:password) - One per line").pack(anchor="w")
        self.prep_users_text = scrolledtext.ScrolledText(exec_frame, height=10)
        self.prep_users_text.pack(fill="both", expand=True, pady=5)
        
        ttk.Button(exec_frame, text="Invoke Workspace Prep (Batch)", command=self.on_prep_invoke).pack(fill="x", pady=2)
    
        ttk.Separator(exec_frame, orient="horizontal").pack(fill="x", pady=5)
        ttk.Label(exec_frame, text="Alternative: Run Locally (Bypasses Lambda)", font=("", 9, "bold")).pack(anchor="w")
        
        # Concurrency settings frame
        concurrency_frame = ttk.Frame(exec_frame)
        concurrency_frame.pack(fill="x", pady=5)
        
        ttk.Label(concurrency_frame, text="Concurrent Accounts:").pack(side="left", padx=(0, 5))
        self.concurrent_accounts_var = tk.IntVar(value=4)
        concurrent_spinbox = ttk.Spinbox(concurrency_frame, from_=1, to=10, width=5, 
                                          textvariable=self.concurrent_accounts_var)
        concurrent_spinbox.pack(side="left", padx=(0, 10))
        
        ttk.Label(concurrency_frame, text="(Windows will be tiled on screen)", 
                  foreground="gray").pack(side="left")
        
        ttk.Button(exec_frame, text="Run Prep Locally (Desktop - Parallel)", command=self.on_prep_run_locally).pack(fill="x", pady=2)
        
        self.stop_button = ttk.Button(exec_frame, text="Stop All Local Prep", command=self.on_stop_local_prep, state="disabled")
        self.stop_button.pack(fill="x", pady=2)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def log(self, message):
        """Thread-safe logging to the text area."""
        def _log():
            timestamp = time.strftime("%H:%M:%S")
            self.log_text.insert(tk.END, f"[{timestamp}] {message}\n")
            self.log_text.see(tk.END)
            print(f"[{timestamp}] {message}") # Keep console logging for debugging/visibility
            self.status_var.set(message) # Update status bar with the latest message
        self.after(0, _log)

    def get_session(self):
        access_key = self.access_key_var.get().strip()
        secret_key = self.secret_key_var.get().strip()
        region = self.region_var.get().strip()
        if not access_key or not secret_key or not region:
            raise ValueError("Please provide Access Key, Secret Key and Region.")
        session = boto3.Session(
            aws_access_key_id=access_key,
            aws_secret_access_key=secret_key,
            region_name=region,
        )
        self.session = session
        return session

    def _get_account_id(self, session):
        if self.aws_account_id:
            return self.aws_account_id
        sts = session.client("sts")
        ident = sts.get_caller_identity()
        self.aws_account_id = ident["Account"]
        return self.aws_account_id

    # ------------------------------------------------------------------
    # Button handlers
    # ------------------------------------------------------------------

    def on_test_connection(self):
        try:
            session = self.get_session()
            account_id = self._get_account_id(session)
            region = self.region_var.get().strip()
            self.log(f"Connected to AWS account {account_id} (Region: {region})")
            messagebox.showinfo("Connection OK", f"Connected to AWS account {account_id}")
            repo_uri = f"{account_id}.dkr.ecr.{region}.amazonaws.com/{ECR_REPO_NAME}:{ECR_IMAGE_TAG}"
            self.ecr_uri_var.set(repo_uri)
            
            # Auto-fix S3 bucket name if default is taken/inaccessible
            current_bucket = self.s3_bucket_var.get().strip()
            s3 = session.client("s3")
            try:
                s3.head_bucket(Bucket=current_bucket)
            except ClientError as e:
                error_code = e.response.get('Error', {}).get('Code', '')
                if error_code == '403' or error_code == 'AccessDenied':
                    # Bucket exists but we don't have access (likely owned by someone else)
                    new_bucket = f"{current_bucket}-{account_id}"
                    self.log(f"Default bucket {current_bucket} is inaccessible. Switching to unique name: {new_bucket}")
                    self.s3_bucket_var.set(new_bucket)
                    global S3_BUCKET_NAME
                    S3_BUCKET_NAME = new_bucket
                elif error_code == '404':
                    # Bucket doesn't exist, which is fine, we'll create it later
                    pass
        except Exception as e:
            self.log(f"ERROR connecting to AWS: {e}")
            traceback.print_exc()
            messagebox.showerror("Connection Failed", str(e))

    def on_create_infrastructure(self):
        try:
            session = self.get_session()
            self._get_account_id(session)
        except Exception as e:
            self.log(f"ERROR: {e}")
            messagebox.showerror("Error", str(e))
            return

        try:
            lambda_policies = [
                "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
                "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly",
                "arn:aws:iam::aws:policy/AmazonS3FullAccess",
            ]

            lambda_role_arn = self._create_iam_role(
                session,
                role_name=LAMBDA_ROLE_NAME,
                service_principal="lambda.amazonaws.com",
                policy_arns=lambda_policies,
            )

            self._create_ecr_repo(session)
            self._create_s3_bucket(session)

            self.log("Infrastructure setup completed.")
            self.log(f"IAM Lambda Role ARN: {lambda_role_arn}")
            messagebox.showinfo("Success", "Core infrastructure created successfully.")
        except Exception as e:
            self.log(f"ERROR creating infrastructure: {e}")
            traceback.print_exc()
            messagebox.showerror("Error", str(e))

    def on_create_ecr_manual(self):
        """Manually create ECR repository."""
        try:
            session = self.get_session()
        except Exception as e:
            self.log(f"ERROR: {e}")
            messagebox.showerror("Error", str(e))
            return
        
        try:
            self.log("Manually creating ECR repository...")
            if self._create_ecr_repo(session):
                # Verify it exists
                ecr = session.client("ecr")
                resp = ecr.describe_repositories(repositoryNames=[ECR_REPO_NAME])
                repo_uri = resp['repositories'][0]['repositoryUri']
                self.log(f"ECR repository verified: {repo_uri}")
                messagebox.showinfo("Success", f"ECR repository created/verified:\n{repo_uri}")
            else:
                raise Exception("Failed to create ECR repository")
        except Exception as e:
            self.log(f"ERROR creating ECR repository: {e}")
            traceback.print_exc()
            messagebox.showerror("Error", str(e))

    def on_inspect_resources(self):
        try:
            session = self.get_session()
        except Exception as e:
            self.log(f"ERROR: {e}")
            messagebox.showerror("Error", str(e))
            return

        try:
            self.log("Inspecting resources...")
            self._inspect_iam(session)
            self._inspect_ecr(session)
            self._inspect_s3(session)
            self._inspect_lambdas(session)
            self.log("Resource inspection completed.")
        except Exception as e:
            self.log(f"ERROR inspecting resources: {e}")
            traceback.print_exc()
            messagebox.showerror("Error", str(e))

    def on_create_lambdas(self):
        """
        PRODUCTION: create / update the Chromium container Lambda only.

        This Lambda will:
          - Log in to Google
          - Set up authenticator + 2-Step
          - Generate an app password
          - Save the secret key to your server via SFTP
          - Append the app password into ONE global S3 text file
        """
        try:
            session = self.get_session()
        except Exception as e:
            self.log(f"ERROR: {e}")
            messagebox.showerror("Error", str(e))
            return

        # GW credentials are no longer required - accounts are passed in event when invoking
        ecr_uri = self.ecr_uri_var.get().strip()
        s3_bucket = self.s3_bucket_var.get().strip()
        sftp_host = self.sftp_host_var.get().strip()
        sftp_user = self.sftp_user_var.get().strip()
        sftp_password = self.sftp_password_var.get().strip()
        sftp_dir = self.sftp_dir_var.get().strip() or "/home/brightmindscampus/"

        if "amazonaws.com" not in ecr_uri:
            msg = "ECR Image URI is not set. Connect and prepare EC2 build box first."
            self.log(f"ERROR: {msg}")
            messagebox.showerror("Missing ECR Image", msg)
            return

        if not s3_bucket:
            msg = "Please enter S3 Bucket name for app passwords storage."
            self.log(f"ERROR: {msg}")
            messagebox.showerror("Missing S3 Bucket", msg)
            return

        if not all([sftp_host, sftp_user]):
            msg = "Please enter SFTP Host and User for secret key storage."
            self.log(f"ERROR: {msg}")
            messagebox.showerror("Missing SFTP Configuration", msg)
            return

        # Verify that ECR image exists
        ecr = session.client("ecr")
        try:
            ecr.describe_images(
                repositoryName=ECR_REPO_NAME,
                imageIds=[{"imageTag": ECR_IMAGE_TAG}],
            )
            self.log(f"ECR image found for repo {ECR_REPO_NAME}:{ECR_IMAGE_TAG}")
        except ClientError as ce:
            self.log(
                "ERROR: ECR image does not appear to exist yet.\n"
                "Launch EC2 build box, wait a few minutes, then try again."
            )
            self.log(f"ECR describe_images error: {ce}")
            messagebox.showerror(
                "ECR Image Missing",
                "ECR image not found. Use EC2 tab to build & push image, wait 5–10 minutes, then retry.",
            )
            return

        try:
            self.log("Creating / updating PRODUCTION Lambda (container image)...")

            # Ensure IAM role has S3 + ECR + logs permissions
            role_arn = self._ensure_lambda_role(session)
            self.log(f"Using IAM Role ARN: {role_arn}")

            # Environment variables consumed by main.py inside the container
            chromium_env = {
                # Account credentials are passed in event when invoking Lambda
                # (No longer using GW_EMAIL/GW_PASSWORD env vars)

                # Global S3 app passwords file
                "APP_PASSWORDS_S3_BUCKET": s3_bucket,
                "APP_PASSWORDS_S3_KEY": "app-passwords.txt",

                # SFTP target where the TOTP secret will be saved
                "SECRET_SFTP_HOST": sftp_host,
                "SECRET_SFTP_USER": sftp_user,
                "SECRET_SFTP_PASSWORD": sftp_password,
                "SECRET_SFTP_PORT": "22",
                "SECRET_SFTP_REMOTE_DIR": sftp_dir,
            }

            # Create / update the single production Lambda
            self._create_or_update_lambda(
                session=session,
                function_name=PRODUCTION_LAMBDA_NAME,
                role_arn=role_arn,
                timeout=600,
                env_vars=chromium_env,
                package_type="Image",
                code_str=None,
                image_uri=ecr_uri,
            )

            self.log("PRODUCTION Lambda is ready.")
            self.log(f"Lambda configured using ECR image URI: {ecr_uri}")
            messagebox.showinfo("Success", "Production Lambda created/updated successfully.")
        except Exception as e:
            self.log(f"ERROR creating production Lambda: {e}")
            traceback.print_exc()
            messagebox.showerror("Error", str(e))


    def on_invoke_production_lambda(self):
        """
        PRODUCTION: Invoke the Lambda based on selected mode.
        - Single mode: Invoke once for the configured account
        - Multiple mode: Invoke in parallel for all accounts in the text area
        """
        try:
            # Get execution mode
            execution_mode = self.execution_mode_var.get() if hasattr(self, 'execution_mode_var') else "single"
            self.log(f"[MODE] Execution mode selected: {execution_mode}")
            
            # Fallback: Check if multiple users text area has content
            if execution_mode != "multiple":
                try:
                    multi_text = self.multiple_users_text.get("1.0", tk.END).strip()
                    if multi_text and ':' in multi_text:
                        # User has entered multiple accounts but mode is single - ask to switch
                        if messagebox.askyesno(
                            "Mode Mismatch",
                            "You have entered multiple accounts in the text area, but 'Single Account' mode is selected.\n\n"
                            "Would you like to switch to 'Multiple Accounts (Parallel)' mode?"
                        ):
                            self.execution_mode_var.set("multiple")
                            execution_mode = "multiple"
                            self.log("[MODE] Switched to MULTIPLE accounts mode based on user input")
                except:
                    pass
            
            if execution_mode == "single":
                self.log("[MODE] Executing in SINGLE account mode")
                self._invoke_single_account()
            else:
                self.log("[MODE] Executing in MULTIPLE accounts mode")
                self._invoke_multiple_accounts()
        except AttributeError as e:
            self.log(f"ERROR: Missing UI component - {e}")
            self.log("Please restart the application to load the updated interface.")
            messagebox.showerror("Error", f"Missing UI component. Please restart the application.\n\nError: {e}")
        except Exception as e:
            self.log(f"ERROR in on_invoke_production_lambda: {e}")
            traceback.print_exc()
            messagebox.showerror("Error", f"Failed to invoke Lambda: {e}")
    
    def _invoke_single_account(self):
        """
        Invoke Lambda once for a single account from the text area.
        """
        try:
            session = self.get_session()
        except Exception as e:
            self.log(f"ERROR: {e}")
            messagebox.showerror("Error", str(e))
            return

        # Parse first account from text area
        users = self._parse_multiple_users()
        
        if not users:
            msg = "Please enter at least one account in the format 'email:password' (one per line)."
            self.log(f"ERROR: {msg}")
            messagebox.showerror("Missing Credentials", msg)
            return
        
        if len(users) > 1:
            msg = f"Single Account mode selected, but {len(users)} accounts found.\n\nUsing first account only. Switch to 'Multiple Accounts' mode to process all accounts."
            self.log(f"WARNING: {msg}")
            if not messagebox.askyesno("Multiple Accounts Detected", msg + "\n\nContinue with first account only?"):
                return
        
        # Use first account
        gw_username, gw_password = users[0]
        self.log(f"Using account: {gw_username}")

        lam = session.client("lambda")

        # Discover Lambda functions dynamically (they may have region suffixes)
        try:
            self.log("Discovering Lambda functions...")
            all_functions = lam.list_functions()
            all_function_names = [fn['FunctionName'] for fn in all_functions.get('Functions', [])]
            self.log(f"Found {len(all_function_names)} total Lambda function(s) in region")
            
            matching_functions = [
                fn['FunctionName'] for fn in all_functions.get('Functions', [])
                if fn['FunctionName'].startswith(PRODUCTION_LAMBDA_NAME) or 'edu-gw-chromium' in fn['FunctionName']
            ]
            
            self.log(f"Found {len(matching_functions)} matching function(s): {matching_functions}")
            
            if not matching_functions:
                self.log(f"ERROR: No Lambda functions found matching '{PRODUCTION_LAMBDA_NAME}'")
                self.log(f"Available functions: {', '.join(all_function_names[:10])}{'...' if len(all_function_names) > 10 else ''}")
                messagebox.showerror(
                    "Lambda Not Found",
                    f"No Lambda functions found matching '{PRODUCTION_LAMBDA_NAME}'.\n\n"
                    f"Found {len(all_function_names)} total function(s) in region.\n\n"
                    "Please create Lambda functions first using 'Create / Update Production Lambda'."
                )
                return
            
            # Use first matching function (or distribute if multiple)
            if len(matching_functions) > 1:
                # Multiple functions - use hash to pick one consistently
                import hashlib
                user_hash = int(hashlib.md5(gw_username.encode()).hexdigest(), 16)
                function_index = user_hash % len(matching_functions)
                lambda_function_name = matching_functions[function_index]
                self.log(f"Found {len(matching_functions)} Lambda functions. Using: {lambda_function_name} (distributed)")
            else:
                lambda_function_name = matching_functions[0]
                self.log(f"Found Lambda function: {lambda_function_name}")
            
        except Exception as e:
            self.log(f"ERROR discovering Lambda functions: {e}")
            messagebox.showerror("Error", f"Failed to discover Lambda functions: {e}")
            return

        # Invoke production Lambda for ONE real account
        try:
            self.log(f"Invoking Lambda function: {lambda_function_name}...")

            # Send email/password in the event
            event = {
                "email": gw_username,
                "password": gw_password,
            }

            resp = lam.invoke(
                FunctionName=lambda_function_name,
                InvocationType="RequestResponse",
                Payload=json.dumps(event).encode("utf-8"),
            )
            payload = resp.get("Payload")
            body = payload.read().decode("utf-8") if payload else ""
            
            # Parse and display response
            try:
                response_data = json.loads(body)
                self.log("=" * 60)
                self.log("LAMBDA RESPONSE:")
                self.log(f"Status: {response_data.get('status', 'unknown')}")
                self.log(f"Step Completed: {response_data.get('step_completed', 'unknown')}")
                if response_data.get('error_message'):
                    self.log(f"Error: {response_data.get('error_message')}")
                if response_data.get('app_password'):
                    self.log(f"App Password: {response_data.get('app_password')[:8]}****")
                if response_data.get('secret_key'):
                    self.log(f"Secret Key: {response_data.get('secret_key')}")
                if response_data.get('timings'):
                    self.log(f"Timings: {json.dumps(response_data.get('timings'), indent=2)}")
                self.log("=" * 60)
            except:
                self.log(f"Raw response: {body}")
            
            messagebox.showinfo("Lambda Invocation", f"Lambda execution completed.\n\nResponse logged above.")
        except lam.exceptions.ResourceNotFoundException:
            self.log(f"Lambda function {lambda_function_name} not found.")
            messagebox.showerror(
                "Lambda Not Found",
                f"Lambda function {lambda_function_name} not found. Create it first.",
            )
        except Exception as e:
            self.log(f"ERROR invoking Lambda: {e}")
            traceback.print_exc()
            messagebox.showerror("Error", str(e))
    
    def _parse_multiple_users(self):
        """
        Parse the multiple users text area and return a list of (email, password) tuples.
        Returns empty list if parsing fails.
        """
        text_content = self.multiple_users_text.get("1.0", tk.END).strip()
        if not text_content:
            return []
        
        users = []
        lines = text_content.split('\n')
        
        for line_num, line in enumerate(lines, start=1):
            line = line.strip()
            if not line:
                continue
            
            if ':' not in line:
                self.log(f"WARNING: Line {line_num} skipped (invalid format, expected 'email:password'): {line[:50]}")
                continue
            
            try:
                email, password = line.split(':', 1)
                email = email.strip()
                password = password.strip()
                
                if email and password:
                    users.append((email, password))
                else:
                    self.log(f"WARNING: Line {line_num} skipped (empty email or password): {line[:50]}")
            except Exception as e:
                self.log(f"WARNING: Line {line_num} skipped (parse error): {e}")
        
        return users
    
    def _discover_all_lambdas(self, access_key, secret_key):
        """
        Discover ALL Lambda functions across ALL regions.
        Returns: dict {region: [function_names]}
        """
        all_lambdas_by_region = {}
        
        # List of all AWS regions to search
        all_regions = [
            'us-east-1', 'us-east-2', 'us-west-1', 'us-west-2',
            'eu-west-1', 'eu-west-2', 'eu-west-3', 'eu-central-1', 'eu-central-2', 'eu-north-1', 'eu-south-1', 'eu-south-2',
            'ap-southeast-1', 'ap-southeast-2', 'ap-southeast-3', 'ap-southeast-4', 'ap-southeast-5',
            'ap-northeast-1', 'ap-northeast-2', 'ap-northeast-3',
            'ap-south-1', 'ap-south-2', 'ap-east-1',
            'ca-central-1', 'ca-west-1',
            'af-south-1',
            'cn-north-1', 'cn-northwest-1',
            'ap-south-1',
            'mx-central-1',
            'me-south-1', 'me-central-1', 'il-central-1',
            'sa-east-1',
        ]
        
        for region in all_regions:
            try:
                region_session = boto3.Session(
                    aws_access_key_id=access_key,
                    aws_secret_access_key=secret_key,
                    region_name=region
                )
                lam = region_session.client("lambda")
                all_functions = lam.list_functions()
                
                matching_functions = [
                    fn['FunctionName'] for fn in all_functions.get('Functions', [])
                    if fn['FunctionName'].startswith(PRODUCTION_LAMBDA_NAME) or PRODUCTION_LAMBDA_NAME in fn['FunctionName']
                ]
                
                if matching_functions:
                    all_lambdas_by_region[region] = matching_functions
            except Exception as e:
                # Skip regions that fail (permissions, etc.)
                continue
        
        return all_lambdas_by_region
    
    def _invoke_single_lambda(self, session, email, password, index, total, all_lambdas_by_region=None):
        """
        Invoke Lambda for a single user account.
        Uses ALL discovered lambdas across ALL regions for distribution.
        Returns (email, success, response_data, error_message)
        """
        try:
            access_key = self.access_key_var.get().strip()
            secret_key = self.secret_key_var.get().strip()
            
            # Discover all lambdas if not provided
            if all_lambdas_by_region is None:
                all_lambdas_by_region = self._discover_all_lambdas(access_key, secret_key)
            
            # Flatten all lambdas with their regions: [(region, function_name), ...]
            all_lambdas_flat = []
            for region, func_names in all_lambdas_by_region.items():
                for func_name in func_names:
                    all_lambdas_flat.append((region, func_name))
            
            if not all_lambdas_flat:
                return (email, False, None, f"No Lambda functions found matching '{PRODUCTION_LAMBDA_NAME}' across any region")
            
            # Use hash to distribute across ALL functions in ALL regions
            user_hash = int(hashlib.md5(email.encode()).hexdigest(), 16)
            function_index = user_hash % len(all_lambdas_flat)
            target_region, lambda_function_name = all_lambdas_flat[function_index]
            
            # Create session for the target region
            target_session = boto3.Session(
                aws_access_key_id=access_key,
                aws_secret_access_key=secret_key,
                region_name=target_region
            )
            lam = target_session.client("lambda")
            
            event = {
                "email": email,
                "password": password,
            }
            
            # Use asynchronous invocation for better parallel execution
            resp = lam.invoke(
                FunctionName=lambda_function_name,
                InvocationType="Event",  # Asynchronous invocation
                Payload=json.dumps(event).encode("utf-8"),
            )
            
            # For async invocations, we get a 202 Accepted response
            status_code = resp.get("StatusCode", 0)
            
            if status_code == 202:
                return (email, True, {"status": "invoked", "region": target_region, "function": lambda_function_name}, None)
            else:
                # Try to read response if it's synchronous
                payload = resp.get("Payload")
                if payload:
                    body = payload.read().decode("utf-8") if payload else ""
                    try:
                        response_data = json.loads(body)
                        return (email, response_data.get('status') == 'success', response_data, None)
                    except:
                        return (email, False, None, f"Invalid response: {body[:100]}")
                else:
                    return (email, False, None, f"Unexpected status code: {status_code}")
                    
        except Exception as e:
            return (email, False, None, str(e))
    
    def _invoke_multiple_accounts(self):
        """
        Invoke Lambda in parallel for multiple accounts.
        Supports up to 1000 concurrent executions.
        """
        try:
            session = self.get_session()
        except Exception as e:
            self.log(f"ERROR: {e}")
            messagebox.showerror("Error", str(e))
            return
        
        # Parse users from text area
        users = self._parse_multiple_users()
        
        if not users:
            msg = "Please enter at least one user in the format 'email:password' (one per line)."
            self.log(f"ERROR: {msg}")
            messagebox.showerror("No Users", msg)
            return
        
        total_users = len(users)
        
        if total_users > 1000:
            msg = f"Too many users ({total_users}). Maximum is 1000. Please reduce the number of users."
            self.log(f"ERROR: {msg}")
            messagebox.showerror("Too Many Users", msg)
            return
        
        # Confirm before starting
        confirm_msg = f"Are you sure you want to process {total_users} accounts in parallel?\n\nThis will invoke the Lambda {total_users} times concurrently."
        if not messagebox.askyesno("Confirm Multiple Invocations", confirm_msg):
            return
        
        self.log("=" * 60)
        self.log(f"Starting parallel Lambda invocations for {total_users} accounts...")
        self.log("=" * 60)
        
        # Discover ALL lambdas across ALL regions ONCE
        self.log("[INVOKE] Discovering Lambda functions across all regions...")
        try:
            access_key = self.access_key_var.get().strip()
            secret_key = self.secret_key_var.get().strip()
            all_lambdas_by_region = self._discover_all_lambdas(access_key, secret_key)
            
            # Log discovered lambdas
            total_lambdas = sum(len(funcs) for funcs in all_lambdas_by_region.values())
            self.log(f"[INVOKE] Discovered {total_lambdas} Lambda function(s) across {len(all_lambdas_by_region)} region(s):")
            for region, func_names in sorted(all_lambdas_by_region.items()):
                self.log(f"[INVOKE]   - {region}: {len(func_names)} function(s) - {', '.join(func_names)}")
            
            if total_lambdas == 0:
                self.log("ERROR: No Lambda functions found! Please create Lambda functions first.")
                messagebox.showerror("No Lambdas", "No Lambda functions found across any region. Please create Lambda functions first.")
                return
        except Exception as e:
            self.log(f"ERROR discovering lambdas: {e}")
            messagebox.showerror("Error", f"Failed to discover Lambda functions: {e}")
            return
        
        self.log("=" * 60)
        
        # Update status
        self.multi_status_label.config(text=f"Processing: 0/{total_users} completed")
        
        # Track results
        results = {
            'success': 0,
            'failed': 0,
            'details': []
        }
        
        start_time = time.time()
        
        # Use ThreadPoolExecutor for parallel invocations
        # Limit to 100 concurrent threads to avoid overwhelming the system
        max_workers = min(100, total_users)
        
        def update_progress():
            completed = results['success'] + results['failed']
            self.multi_status_label.config(text=f"Processing: {completed}/{total_users} completed (Success: {results['success']}, Failed: {results['failed']})")
        
        def process_user(user_data):
            email, password = user_data
            index = users.index((email, password)) + 1
            return self._invoke_single_lambda(session, email, password, index, total_users, all_lambdas_by_region)
        
        # Execute in parallel
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            # Submit all tasks
            future_to_user = {executor.submit(process_user, user): user for user in users}
            
            # Process completed tasks
            for future in as_completed(future_to_user):
                user = future_to_user[future]
                email, password = user
                
                try:
                    email_result, success, response_data, error_msg = future.result()
                    
                    if success:
                        results['success'] += 1
                        self.log(f"[{results['success'] + results['failed']}/{total_users}] ✅ {email_result}: Invoked successfully")
                    else:
                        results['failed'] += 1
                        error_display = error_msg or "Unknown error"
                        self.log(f"[{results['success'] + results['failed']}/{total_users}] ❌ {email_result}: {error_display}")
                    
                    results['details'].append({
                        'email': email_result,
                        'success': success,
                        'error': error_msg
                    })
                    
                    # Update progress
                    update_progress()
                    
                except Exception as e:
                    results['failed'] += 1
                    self.log(f"[{results['success'] + results['failed']}/{total_users}] ❌ {email}: Exception - {str(e)}")
                    results['details'].append({
                        'email': email,
                        'success': False,
                        'error': str(e)
                    })
                    update_progress()
        
        elapsed_time = time.time() - start_time
        
        # Final summary
        self.log("=" * 60)
        self.log("PARALLEL EXECUTION SUMMARY:")
        self.log(f"Total Accounts: {total_users}")
        self.log(f"Successful: {results['success']}")
        self.log(f"Failed: {results['failed']}")
        self.log(f"Total Time: {elapsed_time:.2f} seconds")
        self.log(f"Average Time per Account: {elapsed_time/total_users:.2f} seconds")
        self.log("=" * 60)
        
        # Update final status
        self.multi_status_label.config(
            text=f"Completed: {results['success']}/{total_users} successful, {results['failed']} failed"
        )
        
        # Show summary dialog
        summary_msg = (
            f"Parallel execution completed!\n\n"
            f"Total: {total_users}\n"
            f"Successful: {results['success']}\n"
            f"Failed: {results['failed']}\n"
            f"Time: {elapsed_time:.2f}s\n\n"
            f"Check logs for detailed results."
        )
        messagebox.showinfo("Execution Complete", summary_msg)
    
    def on_delete_s3_content(self):
        """Delete all contents from S3 bucket (but keep the bucket)."""
        try:
            session = self.get_session()
        except Exception as e:
            self.log(f"ERROR: {e}")
            messagebox.showerror("Error", str(e))
            return
        
        s3 = session.client("s3")
        
        try:
            self.log(f"Deleting all contents from S3 bucket: {S3_BUCKET_NAME} ...")
            
            # List and delete all objects
            paginator = s3.get_paginator('list_objects_v2')
            for page in paginator.paginate(Bucket=S3_BUCKET_NAME):
                objects = page.get('Contents', [])
                if objects:
                    delete_keys = [{'Key': obj['Key']} for obj in objects]
                    s3.delete_objects(
                        Bucket=S3_BUCKET_NAME,
                        Delete={'Objects': delete_keys}
                    )
                    self.log(f"Deleted {len(delete_keys)} objects from {S3_BUCKET_NAME}")
            
            # List and delete all versions if versioning is enabled
            try:
                version_paginator = s3.get_paginator('list_object_versions')
                for page in version_paginator.paginate(Bucket=S3_BUCKET_NAME):
                    versions = page.get('Versions', [])
                    delete_markers = page.get('DeleteMarkers', [])
                    
                    to_delete = []
                    for version in versions:
                        to_delete.append({'Key': version['Key'], 'VersionId': version['VersionId']})
                    for marker in delete_markers:
                        to_delete.append({'Key': marker['Key'], 'VersionId': marker['VersionId']})
                    
                    if to_delete:
                        s3.delete_objects(
                            Bucket=S3_BUCKET_NAME,
                            Delete={'Objects': to_delete}
                        )
                        self.log(f"Deleted {len(to_delete)} versions/markers from {S3_BUCKET_NAME}")
            except Exception as ve:
                self.log(f"Warning: Could not delete versions (may not be enabled): {ve}")
            
            self.log(f"S3 bucket {S3_BUCKET_NAME} contents deleted successfully.")
            messagebox.showinfo("Success", f"All contents deleted from {S3_BUCKET_NAME}")
        except ClientError as e:
            error_code = e.response.get('Error', {}).get('Code', '')
            if error_code == 'NoSuchBucket':
                self.log(f"S3 bucket {S3_BUCKET_NAME} does not exist.")
            else:
                self.log(f"ERROR deleting S3 contents: {e}")
                messagebox.showerror("Error", str(e))
        except Exception as e:
            self.log(f"ERROR deleting S3 contents: {e}")
            messagebox.showerror("Error", str(e))
    
    def on_delete_ecr_repo(self):
        """Delete ECR repository and all images."""
        try:
            session = self.get_session()
        except Exception as e:
            self.log(f"ERROR: {e}")
            messagebox.showerror("Error", str(e))
            return
        
        ecr = session.client("ecr")
        region = self.region_var.get().strip()
        
        try:
            self.log(f"Deleting ECR repository: {ECR_REPO_NAME} ...")
            
            # Delete repository (force=True deletes all images)
            ecr.delete_repository(
                repositoryName=ECR_REPO_NAME,
                force=True  # Deletes all images in the repo
            )
            
            self.log(f"ECR repository {ECR_REPO_NAME} deleted successfully.")
            messagebox.showinfo("Success", f"ECR repository {ECR_REPO_NAME} deleted")
        except ecr.exceptions.RepositoryNotFoundException:
            self.log(f"ECR repository {ECR_REPO_NAME} not found.")
            messagebox.showinfo("Info", f"ECR repository {ECR_REPO_NAME} does not exist")
        except Exception as e:
            self.log(f"ERROR deleting ECR repository: {e}")
            messagebox.showerror("Error", str(e))
    
    def on_delete_cloudwatch_logs(self):
        """Delete CloudWatch log groups for our Lambdas."""
        try:
            session = self.get_session()
        except Exception as e:
            self.log(f"ERROR: {e}")
            messagebox.showerror("Error", str(e))
            return
        
        logs = session.client("logs")
        
        try:
            self.log("Deleting CloudWatch log groups for edu-gw Lambdas...")
            
            # List all log groups
            paginator = logs.get_paginator('describe_log_groups')
            deleted_count = 0
            
            for page in paginator.paginate():
                for log_group in page.get('logGroups', []):
                    log_group_name = log_group['logGroupName']
                    # Delete log groups for our Lambda functions
                    if '/aws/lambda/edu-gw' in log_group_name:
                        try:
                            logs.delete_log_group(logGroupName=log_group_name)
                            self.log(f"Deleted log group: {log_group_name}")
                            deleted_count += 1
                        except Exception as e:
                            self.log(f"Error deleting {log_group_name}: {e}")
            
            if deleted_count > 0:
                self.log(f"CloudWatch log cleanup completed. Deleted {deleted_count} log groups.")
                messagebox.showinfo("Success", f"Deleted {deleted_count} log groups")
            else:
                self.log("No edu-gw log groups found.")
                messagebox.showinfo("Info", "No log groups found to delete")
        except Exception as e:
            self.log(f"ERROR deleting CloudWatch logs: {e}")
            messagebox.showerror("Error", str(e))

    def on_delete_all_lambdas(self):
        """Delete all production Lambdas."""
        try:
            session = self.get_session()
        except Exception as e:
            self.log(f"ERROR: {e}")
            messagebox.showerror("Error", str(e))
            return

        lam = session.client("lambda")
        
        try:
            # Delete production Lambda
            try:
                lam.delete_function(FunctionName=PRODUCTION_LAMBDA_NAME)
                self.log(f"Deleted Lambda: {PRODUCTION_LAMBDA_NAME}")
            except lam.exceptions.ResourceNotFoundException:
                self.log(f"Lambda {PRODUCTION_LAMBDA_NAME} not found, skipping.")
            
            # Also check for any other edu-gw lambdas
            paginator = lam.get_paginator("list_functions")
            for page in paginator.paginate():
                for fn in page.get("Functions", []):
                    if "edu-gw" in fn["FunctionName"]:
                        try:
                            lam.delete_function(FunctionName=fn["FunctionName"])
                            self.log(f"Deleted Lambda: {fn['FunctionName']}")
                        except Exception as e:
                            self.log(f"Error deleting {fn['FunctionName']}: {e}")
            
            self.log("Lambda cleanup completed.")
            messagebox.showinfo("Success", "All Lambdas deleted successfully.")
        except Exception as e:
            self.log(f"ERROR deleting Lambdas: {e}")
            traceback.print_exc()
            messagebox.showerror("Error", str(e))

    # ----------------------------- EC2 BUILD BOX -----------------------------

    def on_ec2_create_build_box(self):
        try:
            session = self.get_session()
            account_id = self._get_account_id(session)
            region = self.region_var.get().strip()
        except Exception as e:
            self.log(f"ERROR: {e}")
            messagebox.showerror("Error", str(e))
            return

        try:
            # Ensure ECR repo exists BEFORE launching EC2
            self.log("Ensuring ECR repository exists before launching EC2...")
            if not self._create_ecr_repo(session):
                raise Exception("Failed to create or verify ECR repository")
            
            # Verify ECR repo exists by describing it
            ecr = session.client("ecr")
            try:
                resp = ecr.describe_repositories(repositoryNames=[ECR_REPO_NAME])
                repo_uri = resp['repositories'][0]['repositoryUri']
                self.log(f"Verified ECR repository exists: {repo_uri}")
            except Exception as e:
                self.log(f"ERROR: Could not verify ECR repository: {e}")
                raise Exception(f"ECR repository verification failed: {e}")
            
            role_arn = self._ensure_ec2_role_profile(session)
            sg_id = self._ensure_ec2_security_group(session)
            self._ensure_ec2_key_pair(session)

            self._create_ec2_build_box(session, account_id, region, role_arn, sg_id)

            self.log(
                "EC2 build box launch requested.\n"
                "Wait ~5–10 minutes for Docker build & ECR push to complete, "
                "then create Lambdas from the Lambdas tab."
            )
        except Exception as e:
            self.log(f"ERROR creating EC2 build box: {e}")
            traceback.print_exc()
            messagebox.showerror("Error", str(e))

    def on_ec2_show_status(self):
        try:
            session = self.get_session()
        except Exception as e:
            self.log(f"ERROR: {e}")
            messagebox.showerror("Error", str(e))
            return

        try:
            inst = self._find_ec2_build_instance(session)
            if not inst:
                self.log("No EC2 build box found.")
                messagebox.showinfo("EC2 Status", "No EC2 build box found.")
                return

            state = inst["State"]["Name"]
            iid = inst["InstanceId"]
            pubip = inst.get("PublicIpAddress", "N/A")

            self.log(f"EC2 build box status: {iid} | state={state} | public_ip={pubip}")
            
            # Check if build is complete by looking for the completion marker
            status_msg = f"Instance: {iid}\nState: {state}\nPublic IP: {pubip}\n\n"
            
            # Try to get console output to check build progress
            try:
                ec2 = session.client("ec2")
                console_output = ec2.get_console_output(InstanceId=iid)
                output = console_output.get('Output', '')
                
                if output:
                    # Check for success/failure markers
                    if "ECR_PUSH_DONE" in output or "EC2 Build Box User Data Script Completed Successfully" in output:
                        status_msg += "✅ BUILD COMPLETED SUCCESSFULLY!\n\n"
                        self.log("✅ EC2 build completed successfully!")
                    elif "FATAL:" in output or "ERROR:" in output:
                        status_msg += "❌ BUILD FAILED - Check logs below\n\n"
                        self.log("❌ EC2 build failed!")
                    elif state == "running":
                        status_msg += "⏳ BUILD IN PROGRESS...\n\n"
                        self.log("⏳ EC2 build in progress...")
                    
                    # Show last 50 lines of output
                    lines = output.split('\n')
                    recent_lines = lines[-50:] if len(lines) > 50 else lines
                    status_msg += "Recent Console Output (last 50 lines):\n"
                    status_msg += "=" * 60 + "\n"
                    status_msg += '\n'.join(recent_lines)
                    
                    # Log full output to console
                    self.log("=" * 60)
                    self.log("EC2 CONSOLE OUTPUT:")
                    self.log(output)
                    self.log("=" * 60)
                else:
                    status_msg += "No console output available yet. Instance may still be initializing."
            except Exception as console_err:
                status_msg += f"Could not retrieve console output: {console_err}\n"
                self.log(f"Warning: Could not get console output: {console_err}")
            
            messagebox.showinfo("EC2 Build Status", status_msg)
            
        except Exception as e:
            self.log(f"ERROR checking EC2 status: {e}")
            traceback.print_exc()
            messagebox.showerror("Error", str(e))

    def on_ec2_terminate(self):
        try:
            session = self.get_session()
        except Exception as e:
            self.log(f"ERROR: {e}")
            messagebox.showerror("Error", str(e))
            return

        try:
            inst = self._find_ec2_build_instance(session)
            if not inst:
                self.log("No EC2 build box to terminate.")
                messagebox.showinfo("EC2 Terminate", "No EC2 build box found.")
                return

            iid = inst["InstanceId"]
            ec2 = session.client("ec2")
            ec2.terminate_instances(InstanceIds=[iid])
            self.log(f"Terminate requested for EC2 build box: {iid}")
            messagebox.showinfo("EC2 Terminate", f"Terminate requested for {iid}")
        except Exception as e:
            self.log(f"ERROR terminating EC2 instance: {e}")
            traceback.print_exc()
            messagebox.showerror("Error", str(e))

    # ------------------------------------------------------------------
    # Infra utils
    # ------------------------------------------------------------------


    def _create_iam_role(self, session, role_name, service_principal, policy_arns):
        iam = session.client("iam")
        self.log(f"Creating/Updating IAM role: {role_name} ...")

        assume_role_doc = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {"Service": service_principal},
                    "Action": "sts:AssumeRole",
                }
            ],
        }

        try:
            resp = iam.get_role(RoleName=role_name)
            self.log("IAM role already exists.")
            role_arn = resp["Role"]["Arn"]
            iam.update_assume_role_policy(
                RoleName=role_name,
                PolicyDocument=json.dumps(assume_role_doc),
            )
        except iam.exceptions.NoSuchEntityException:
            resp = iam.create_role(
                RoleName=role_name,
                AssumeRolePolicyDocument=json.dumps(assume_role_doc),
                Description=f"Education case study role for {role_name}",
            )
            role_arn = resp["Role"]["Arn"]
            self.log(f"IAM role created: {role_arn}")

        for p in policy_arns:
            try:
                iam.attach_role_policy(RoleName=role_name, PolicyArn=p)
            except Exception as e:
                self.log(f"WARNING: could not attach policy {p} to {role_name}: {e}")

        self.log(f"Waiting for IAM role {role_name} propagation...")
        time.sleep(10)
        return role_arn

    def _ensure_lambda_role(self, session):
        """
        Lambda IAM role used by the production container.

        It must be able to:
          - Write logs (AWSLambdaBasicExecutionRole)
          - Read image from ECR
          - Read/Write S3 (global app_passwords.txt)
        """
        iam = session.client("iam")
        lambda_policies = [
            "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
            "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly",
            "arn:aws:iam::aws:policy/AmazonS3FullAccess",
        ]
        
        try:
            resp = iam.get_role(RoleName=LAMBDA_ROLE_NAME)
            role_arn = resp["Role"]["Arn"]
            self.log(f"Using existing Lambda IAM role: {role_arn}")
            
            # Ensure all required policies are attached
            try:
                attached_policies = iam.list_attached_role_policies(RoleName=LAMBDA_ROLE_NAME)
                attached_policy_arns = [p['PolicyArn'] for p in attached_policies['AttachedPolicies']]
                
                for policy_arn in lambda_policies:
                    if policy_arn not in attached_policy_arns:
                        self.log(f"Attaching missing policy {policy_arn} to Lambda role...")
                        iam.attach_role_policy(RoleName=LAMBDA_ROLE_NAME, PolicyArn=policy_arn)
                        time.sleep(2)  # Brief wait for propagation
                    else:
                        self.log(f"Policy {policy_arn} already attached to Lambda role")
            except Exception as policy_err:
                self.log(f"WARNING: Could not verify/attach policies: {policy_err}")
            
            return role_arn
        except iam.exceptions.NoSuchEntityException:
            self.log("Lambda IAM role not found, creating new one...")
            return self._create_iam_role(
                session,
                role_name=LAMBDA_ROLE_NAME,
                service_principal="lambda.amazonaws.com",
                policy_arns=lambda_policies,
            )

    def _create_ecr_repo(self, session):
        ecr = session.client("ecr")
        region = self.region_var.get().strip()
        self.log(f"Creating ECR repository: {ECR_REPO_NAME} in region {region}...")
        try:
            resp = ecr.describe_repositories(repositoryNames=[ECR_REPO_NAME])
            repo_uri = resp['repositories'][0]['repositoryUri']
            self.log(f"ECR repository already exists: {repo_uri}")
            return True
        except ecr.exceptions.RepositoryNotFoundException:
            try:
                resp = ecr.create_repository(
                    repositoryName=ECR_REPO_NAME,
                    imageTagMutability='MUTABLE',
                    imageScanningConfiguration={'scanOnPush': False}
                )
                repo_uri = resp['repository']['repositoryUri']
                self.log(f"ECR repository created successfully: {repo_uri}")
                # Wait a moment for propagation
                time.sleep(2)
                return True
            except Exception as e:
                self.log(f"ERROR creating ECR repository: {e}")
                raise
        except Exception as e:
            self.log(f"ERROR checking ECR repository: {e}")
            raise

    def _create_s3_bucket(self, session):
        """Create S3 bucket for app passwords storage."""
        global S3_BUCKET_NAME  # Declare at function start
        s3 = session.client("s3")
        region = self.region_var.get().strip()
        self.log(f"Creating S3 bucket: {S3_BUCKET_NAME} ...")
        
        # Check if bucket exists and is accessible
        try:
            s3.list_objects_v2(Bucket=S3_BUCKET_NAME, MaxKeys=1)
            self.log(f"S3 bucket {S3_BUCKET_NAME} already exists and is accessible.")
            return  # Bucket exists and we have access
        except ClientError as list_err:
            list_error_code = list_err.response.get('Error', {}).get('Code', '')
            
            if list_error_code == 'NoSuchBucket':
                # Bucket doesn't exist, proceed to create below
                self.log(f"S3 bucket {S3_BUCKET_NAME} does not exist, creating...")
            elif list_error_code == '403' or list_error_code == 'AccessDenied':
                # Bucket exists but owned by another account - use unique suffix
                account_id = session.client('sts').get_caller_identity()['Account']
                unique_bucket_name = f"{S3_BUCKET_NAME}-{account_id}"
                self.log(f"WARNING: Bucket {S3_BUCKET_NAME} exists in another account. Using unique name: {unique_bucket_name}")
                # Update the global bucket name
                S3_BUCKET_NAME = unique_bucket_name
                # Update UI
                if hasattr(self, 's3_bucket_var'):
                    self.s3_bucket_var.set(unique_bucket_name)
                # Recursively try with new name
                return self._create_s3_bucket(session)
            else:
                # Unknown error
                self.log(f"ERROR checking S3 bucket: {list_err}")
                raise list_err
        
        # If we reach here, bucket doesn't exist - create it
        try:
            if region == 'us-east-1':
                # us-east-1 doesn't need LocationConstraint
                s3.create_bucket(Bucket=S3_BUCKET_NAME)
            else:
                s3.create_bucket(
                    Bucket=S3_BUCKET_NAME,
                    CreateBucketConfiguration={'LocationConstraint': region}
                )
            self.log(f"S3 bucket {S3_BUCKET_NAME} created in region {region}.")
            
            # Enable versioning (optional but recommended)
            try:
                s3.put_bucket_versioning(
                    Bucket=S3_BUCKET_NAME,
                    VersioningConfiguration={'Status': 'Enabled'}
                )
                self.log(f"Versioning enabled for {S3_BUCKET_NAME}.")
            except Exception as ve:
                self.log(f"Warning: Could not enable versioning: {ve}")
            
            # Block public access (security best practice)
            try:
                s3.put_public_access_block(
                    Bucket=S3_BUCKET_NAME,
                    PublicAccessBlockConfiguration={
                        'BlockPublicAcls': True,
                        'IgnorePublicAcls': True,
                        'BlockPublicPolicy': True,
                        'RestrictPublicBuckets': True
                    }
                )
                self.log(f"Public access blocked for {S3_BUCKET_NAME}.")
            except Exception as pae:
                self.log(f"Warning: Could not block public access: {pae}")
                
        except ClientError as ce:
            self.log(f"ERROR creating S3 bucket: {ce}")
            raise

    def _inspect_iam(self, session):
        iam = session.client("iam")
        self.log("Listing IAM roles (filter: 'edu-gw')...")
        paginator = iam.get_paginator("list_roles")
        for page in paginator.paginate():
            for role in page.get("Roles", []):
                if "edu-gw" in role["RoleName"]:
                    self.log(f"IAM Role: {role['RoleName']} | ARN: {role['Arn']}")

    def _inspect_ecr(self, session):
        ecr = session.client("ecr")
        self.log("Listing ECR repositories...")
        paginator = ecr.get_paginator("describe_repositories")
        for page in paginator.paginate():
            for repo in page.get("repositories", []):
                self.log(f"ECR Repo: {repo['repositoryName']} | URI: {repo['repositoryUri']}")

    def _inspect_s3(self, session):
        s3 = session.client("s3")
        self.log("Listing S3 buckets (filter: 'edu-gw')...")
        try:
            resp = s3.list_buckets()
            for bucket in resp.get("Buckets", []):
                if "edu-gw" in bucket["Name"]:
                    self.log(f"S3 Bucket: {bucket['Name']} | Created: {bucket.get('CreationDate', 'N/A')}")
        except Exception as e:
            self.log(f"Error listing S3 buckets: {e}")

    def _inspect_lambdas(self, session):
        lam = session.client("lambda")
        self.log("Listing Lambda functions (filter: 'edu-gw')...")
        paginator = lam.get_paginator("list_functions")
        for page in paginator.paginate():
            for fn in page.get("Functions", []):
                if "edu-gw" in fn["FunctionName"]:
                    self.log(
                        f"Lambda: {fn['FunctionName']} | Runtime: {fn.get('Runtime', 'N/A')} | PackageType: {fn.get('PackageType', 'N/A')} | Role: {fn.get('Role', 'N/A')}"
                    )

    # ------------------------------------------------------------------
    # Lambda helpers
    # ------------------------------------------------------------------

    def create_lambda_zip(self, code_str: str) -> bytes:
        """Build ZIP for Lambda from code string (for Zip package type)."""
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("lambda_function.py", code_str)
        buf.seek(0)
        return buf.read()

    def _create_or_update_lambda(
        self,
        session,
        function_name,
        role_arn,
        timeout=60,
        env_vars=None,
        package_type="Zip",
        code_str=None,
        image_uri=None,
    ):
        if env_vars is None:
            env_vars = {}

        lam = session.client("lambda")

        if package_type == "Zip":
            if code_str is None:
                raise ValueError("code_str is required for Zip package type")
            code_params = {"ZipFile": self.create_lambda_zip(code_str)}
            runtime = "python3.11"
            handler = "lambda_function.lambda_handler"
        elif package_type == "Image":
            if image_uri is None:
                raise ValueError("image_uri is required for Image package type")
            code_params = {"ImageUri": image_uri}
            runtime = None
            handler = None
        else:
            raise ValueError(f"Unsupported package type: {package_type}")

        try:
            # Update path
            lam.get_function(FunctionName=function_name)
            self.log(f"Updating existing Lambda: {function_name} ({package_type})")

            lam.update_function_code(FunctionName=function_name, **code_params, Publish=True)
            waiter = lam.get_waiter("function_updated")
            waiter.wait(FunctionName=function_name, WaiterConfig={"Delay": 5, "MaxAttempts": 12})

            config_update_params = {
                "FunctionName": function_name,
                "Role": role_arn,
                "Timeout": timeout,
                "MemorySize": 2048 if package_type == "Image" else 256,
                "Environment": {"Variables": env_vars},
            }
            if package_type == "Zip":
                config_update_params["Runtime"] = runtime
                config_update_params["Handler"] = handler
            
            # Set ephemeral storage for container images (needed for Chrome/Selenium)
            if package_type == "Image":
                config_update_params["EphemeralStorage"] = {"Size": 2048}  # 2GB for Chrome/Selenium

            lam.update_function_configuration(**config_update_params)
        except lam.exceptions.ResourceNotFoundException:
            # Create path
            self.log(f"Creating new Lambda: {function_name} ({package_type})")
            create_params = {
                "FunctionName": function_name,
                "Role": role_arn,
                "Code": code_params,
                "Timeout": timeout,
                "MemorySize": 2048 if package_type == "Image" else 256,
                "Publish": True,
                "PackageType": package_type,
                "Environment": {"Variables": env_vars},
            }
            if package_type == "Zip":
                create_params["Runtime"] = runtime
                create_params["Handler"] = handler
            
            # Set ephemeral storage for container images (needed for Chrome/Selenium)
            if package_type == "Image":
                create_params["EphemeralStorage"] = {"Size": 2048}  # 2GB for Chrome/Selenium

            lam.create_function(**create_params)

        self.log(f"Lambda ready: {function_name}")


    # ------------------------------------------------------------------
    # EC2 helpers
    # ------------------------------------------------------------------

    def _get_free_tier_instance_type(self, session):
        """
        Select instance type for Docker builds.
        t3.micro (1GB RAM) often fails with OOM during Docker builds.
        Using t3.small (2GB RAM) for reliable builds.
        Note: t3.small costs ~$0.02/hour but is necessary for Docker builds.
        """
        # For Docker builds, we need at least 2GB RAM
        # t3.micro (1GB) frequently runs out of memory during large image builds
        # t3.small (2GB) is the minimum recommended for Docker builds
        
        self.log("Selecting instance type for Docker build...")
        self.log("Using 't3.small' (2GB RAM) for reliable Docker builds.")
        self.log("Note: t3.small costs ~$0.023/hour. Build typically takes 5-10 minutes (~$0.004-$0.008).")
        
        return "t3.small"

    def _ensure_ec2_role_profile(self, session):
        iam = session.client("iam")

        ec2_policies = [
            "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryFullAccess",
            "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore",
            "arn:aws:iam::aws:policy/CloudWatchAgentServerPolicy",
            "arn:aws:iam::aws:policy/AmazonS3ReadOnlyAccess",  # For downloading build files
        ]

        role_arn = self._create_iam_role(
            session,
            role_name=EC2_ROLE_NAME,
            service_principal="ec2.amazonaws.com",
            policy_arns=ec2_policies,
        )

        # Instance profile
        try:
            iam.get_instance_profile(InstanceProfileName=EC2_INSTANCE_PROFILE_NAME)
            self.log(f"EC2 instance profile already exists: {EC2_INSTANCE_PROFILE_NAME}")
        except iam.exceptions.NoSuchEntityException:
            iam.create_instance_profile(InstanceProfileName=EC2_INSTANCE_PROFILE_NAME)
            self.log(f"EC2 instance profile created: {EC2_INSTANCE_PROFILE_NAME}")

        # Attach role to profile
        try:
            iam.add_role_to_instance_profile(
                InstanceProfileName=EC2_INSTANCE_PROFILE_NAME,
                RoleName=EC2_ROLE_NAME,
            )
            self.log("EC2 role added to instance profile.")
        except iam.exceptions.LimitExceededException:
            self.log("EC2 role already attached to instance profile.")

        self.log("Waiting briefly for EC2 IAM role/profile propagation...")
        time.sleep(10)
        return role_arn

    def _ensure_ec2_security_group(self, session):
        ec2 = session.client("ec2")
        vpcs = ec2.describe_vpcs()
        default_vpc_id = vpcs["Vpcs"][0]["VpcId"]
        self.log(f"Using VPC {default_vpc_id} for EC2 security group...")

        try:
            resp = ec2.describe_security_groups(
                Filters=[
                    {"Name": "group-name", "Values": [EC2_SECURITY_GROUP_NAME]},
                    {"Name": "vpc-id", "Values": [default_vpc_id]},
                ]
            )
            if resp["SecurityGroups"]:
                sg_id = resp["SecurityGroups"][0]["GroupId"]
                self.log(f"EC2 security group already exists: {sg_id}")
                return sg_id
        except Exception:
            pass

        resp = ec2.create_security_group(
            GroupName=EC2_SECURITY_GROUP_NAME,
            Description="EC2 build box security group for docker-selenium-lambda",
            VpcId=default_vpc_id,
        )
        sg_id = resp["GroupId"]
        self.log(f"EC2 security group created: {sg_id}")

        # SSH open for demo (you can restrict later)
        ec2.authorize_security_group_ingress(
            GroupId=sg_id,
            IpPermissions=[
                {
                    "IpProtocol": "tcp",
                    "FromPort": 22,
                    "ToPort": 22,
                    "IpRanges": [{"CidrIp": "0.0.0.0/0", "Description": "SSH from anywhere (demo)"}],
                }
            ],
        )

        return sg_id

    def _ensure_ec2_key_pair(self, session):
        ec2 = session.client("ec2")
        try:
            ec2.describe_key_pairs(KeyNames=[EC2_KEY_PAIR_NAME])
            self.log(f"EC2 key pair already exists: {EC2_KEY_PAIR_NAME}")
        except ClientError:
            self.log(f"Creating EC2 key pair: {EC2_KEY_PAIR_NAME} ...")
            resp = ec2.create_key_pair(KeyName=EC2_KEY_PAIR_NAME)
            private_key = resp["KeyMaterial"]
            with open(EC2_KEY_PATH, "w", encoding="utf-8") as f:
                f.write(private_key)
            os.chmod(EC2_KEY_PATH, 0o400)
            self.log(f"EC2 key pair saved locally: {EC2_KEY_PATH}")

    def _create_ec2_build_box(self, session, account_id, region, role_arn, sg_id):
        ec2 = session.client("ec2")
        ssm = session.client("ssm")

        self.log("Resolving latest Amazon Linux 2 AMI via SSM parameter...")
        param = ssm.get_parameter(
            Name="/aws/service/ami-amazon-linux-latest/amzn2-ami-hvm-x86_64-gp2"
        )
        ami_id = param["Parameter"]["Value"]
        self.log(f"Using latest Amazon Linux 2 AMI: {ami_id}")

        # >>> NEW: pick a truly free-tier-eligible instance type for your region/account
        instance_type = self._get_free_tier_instance_type(session)
        self.log(f"Launching EC2 build box with instance type: {instance_type}")

        repo_uri_base = f"{account_id}.dkr.ecr.{region}.amazonaws.com/{ECR_REPO_NAME}"
        
        # Upload main.py and Dockerfile to S3 for EC2 to download
        # Use a temporary S3 bucket/key for build files
        s3_build_bucket = S3_BUCKET_NAME  # Use the same bucket we created
        s3_build_key_prefix = "ec2-build-files"
        
        repo_folder = os.path.join(os.path.dirname(os.path.abspath(__file__)), "repo_aws_files")
        main_py_path = os.path.join(repo_folder, "main.py")
        dockerfile_path = os.path.join(repo_folder, "Dockerfile")
        
        try:
            # Ensure S3 bucket exists
            self._create_s3_bucket(session)
            
            # Upload files to S3
            s3 = session.client("s3")
            self.log("Uploading main.py and Dockerfile to S3 for EC2 download...")
            
            with open(main_py_path, 'rb') as f:
                s3.put_object(
                    Bucket=s3_build_bucket,
                    Key=f"{s3_build_key_prefix}/main.py",
                    Body=f.read(),
                    ContentType="text/plain"
                )
            
            with open(dockerfile_path, 'rb') as f:
                s3.put_object(
                    Bucket=s3_build_bucket,
                    Key=f"{s3_build_key_prefix}/Dockerfile",
                    Body=f.read(),
                    ContentType="text/plain"
                )
            
            self.log(f"Files uploaded to s3://{s3_build_bucket}/{s3_build_key_prefix}/")
        except Exception as e:
            self.log(f"ERROR: Could not upload files to S3: {e}")
            raise

        user_data = f"""#!/bin/bash
set -xe
exec > >(tee /var/log/user-data.log) 2>&1
echo "=== EC2 Build Box User Data Script Started ==="
date

# Install dependencies
yum update -y
amazon-linux-extras install docker -y || yum install -y docker
systemctl enable docker
systemctl start docker
usermod -a -G docker ec2-user
yum install -y git unzip

# Install AWS CLI
curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "awscliv2.zip"
unzip -q awscliv2.zip
./aws/install

# Clone base repo
cd /home/ec2-user
echo "Cloning docker-selenium-lambda repo..."
git clone https://github.com/umihico/docker-selenium-lambda.git
cd docker-selenium-lambda

# Download our custom main.py and Dockerfile from S3
echo "Downloading custom main.py and Dockerfile from S3..."
aws s3 cp s3://{s3_build_bucket}/{s3_build_key_prefix}/main.py ./main.py
aws s3 cp s3://{s3_build_bucket}/{s3_build_key_prefix}/Dockerfile ./Dockerfile
chmod 644 main.py Dockerfile

# Verify ECR repo exists (wait up to 60 seconds, create if needed)
echo "Verifying ECR repository exists..."
ECR_FOUND=0
for i in {{1..60}}; do
    if aws ecr describe-repositories --repository-names {ECR_REPO_NAME} --region {region} 2>/dev/null; then
        echo "ECR repository found!"
        ECR_FOUND=1
        break
    fi
    echo "Waiting for ECR repository... ($i/60)"
    sleep 1
done

# If ECR repo still doesn't exist, try to create it
if [ $ECR_FOUND -eq 0 ]; then
    echo "WARNING: ECR repository {ECR_REPO_NAME} not found after 60 seconds!"
    echo "Attempting to create ECR repository as fallback..."
    if aws ecr create-repository --repository-name {ECR_REPO_NAME} --region {region} --image-tag-mutability MUTABLE 2>/dev/null; then
        echo "ECR repository created successfully!"
        sleep 3
        ECR_FOUND=1
    else
        echo "ERROR: Failed to create ECR repository. Checking if it exists now..."
        if aws ecr describe-repositories --repository-names {ECR_REPO_NAME} --region {region} 2>/dev/null; then
            echo "ECR repository now exists!"
            ECR_FOUND=1
        else
            echo "FATAL: ECR repository still not available. Cannot proceed with Docker push."
            exit 1
        fi
    fi
fi

if [ $ECR_FOUND -eq 0 ]; then
    echo "FATAL: ECR repository verification failed. Exiting."
    exit 1
fi

# Login to ECR
echo "Logging into ECR..."
aws ecr get-login-password --region {region} | docker login --username AWS --password-stdin {account_id}.dkr.ecr.{region}.amazonaws.com

# Build Docker image
echo "Building Docker image..."
docker build -t {ECR_REPO_NAME}:{ECR_IMAGE_TAG} .

# Tag image
echo "Tagging Docker image..."
docker tag {ECR_REPO_NAME}:{ECR_IMAGE_TAG} {repo_uri_base}:{ECR_IMAGE_TAG}

# Push to ECR
echo "Pushing Docker image to ECR..."
docker push {repo_uri_base}:{ECR_IMAGE_TAG}

# Verify push
echo "Verifying image push..."
aws ecr describe-images --repository-name {ECR_REPO_NAME} --image-ids imageTag={ECR_IMAGE_TAG} --region {region}

# Create completion marker
touch /home/ec2-user/ECR_PUSH_DONE
echo "=== EC2 Build Box User Data Script Completed Successfully ==="
date
"""

        self.log("Launching EC2 build box (free-tier-eligible instance)...")
        resp = ec2.run_instances(
            ImageId=ami_id,
            InstanceType=instance_type,
            MinCount=1,
            MaxCount=1,
            IamInstanceProfile={"Name": EC2_INSTANCE_PROFILE_NAME},
            SecurityGroupIds=[sg_id],
            KeyName=EC2_KEY_PAIR_NAME,
            UserData=user_data,
            TagSpecifications=[
                {
                    "ResourceType": "instance",
                    "Tags": [
                        {"Key": "Name", "Value": EC2_INSTANCE_NAME},
                        {"Key": "Purpose", "Value": "docker-selenium-lambda-build"},
                    ],
                }
            ],
        )
        iid = resp["Instances"][0]["InstanceId"]
        self.log(f"EC2 build box launched: {iid} (type={instance_type})")

    def _find_ec2_build_instance(self, session):
        ec2 = session.client("ec2")
        resp = ec2.describe_instances(
            Filters=[
                {"Name": "tag:Name", "Values": [EC2_INSTANCE_NAME]},
                {
                    "Name": "instance-state-name",
                    "Values": ["pending", "running", "stopping", "stopped"],
                },
            ]
        )
        for r in resp.get("Reservations", []):
            for inst in r.get("Instances", []):
                return inst
        return None



    # ------------------------------------------------------------------
    # Prep Process Handlers
    # ------------------------------------------------------------------

    def on_prep_create_infrastructure(self):
        try:
            session = self.get_session()
            ecr = session.client("ecr")
            region = self.region_var.get().strip()
            
            self.log(f"Creating Prep ECR repo: {PREP_ECR_REPO_NAME}...")
            try:
                ecr.create_repository(repositoryName=PREP_ECR_REPO_NAME)
                self.log("Prep ECR repo created.")
            except ecr.exceptions.RepositoryAlreadyExistsException:
                self.log("Prep ECR repo already exists.")
                
            messagebox.showinfo("Success", "Prep Infrastructure Ready")
        except Exception as e:
            self.log(f"Error: {e}")
            messagebox.showerror("Error", str(e))

    def on_prep_launch_build_box(self):
        try:
            session = self.get_session()
            account_id = self._get_account_id(session)
            region = self.region_var.get().strip()
            
            # 1. Upload files to S3
            s3_bucket = self.s3_bucket_var.get().strip()
            s3_prefix = "prep-build-files"
            s3 = session.client("s3")
            
            # Ensure bucket exists (using the name from UI)
            global S3_BUCKET_NAME
            S3_BUCKET_NAME = s3_bucket
            self._create_s3_bucket(session)
            
            repo_folder = os.path.join(os.path.dirname(os.path.abspath(__file__)), "repo_aws_files")
            prep_py = os.path.join(repo_folder, "prep.py")
            docker_prep = os.path.join(repo_folder, "Dockerprep")
            req_txt = os.path.join(repo_folder, "requirements_prep.txt")
            
            if not os.path.exists(prep_py) or not os.path.exists(docker_prep) or not os.path.exists(req_txt):
                raise FileNotFoundError("prep.py, Dockerprep, or requirements_prep.txt not found in repo_aws_files/")
                
            self.log("Uploading prep files to S3...")
            s3.upload_file(prep_py, s3_bucket, f"{s3_prefix}/prep.py")
            s3.upload_file(docker_prep, s3_bucket, f"{s3_prefix}/Dockerprep")
            s3.upload_file(req_txt, s3_bucket, f"{s3_prefix}/requirements_prep.txt")
            
            # 2. Prepare User Data
            user_data = self._create_prep_user_data(s3_bucket, s3_prefix, account_id, region)
            
            # 3. Launch EC2
            ec2 = session.client("ec2")
            ami_id = session.client("ssm").get_parameter(Name="/aws/service/ami-amazon-linux-latest/amzn2-ami-hvm-x86_64-gp2")["Parameter"]["Value"]
            
            # Ensure Security Group and Key Pair
            sg_id = self._ensure_ec2_security_group(session)
            self._ensure_ec2_key_pair(session)
            
            self.log("Launching Prep Build Box (t3.small)...")
            ec2.run_instances(
                ImageId=ami_id,
                InstanceType="t3.small", # Requested by user
                MinCount=1,
                MaxCount=1,
                IamInstanceProfile={"Name": EC2_INSTANCE_PROFILE_NAME},
                SecurityGroupIds=[sg_id],
                KeyName=EC2_KEY_PAIR_NAME,
                UserData=user_data,
                TagSpecifications=[{"ResourceType": "instance", "Tags": [{"Key": "Name", "Value": "prep-ec2-build-box"}]}]
            )
            self.log("Prep Build Box Launched. Check EC2 console or wait for ECR image.")
            messagebox.showinfo("Success", "Prep Build Box Launched")
            
        except Exception as e:
            self.log(f"Error: {e}")
            traceback.print_exc()
            messagebox.showerror("Error", str(e))

    def on_prep_terminate_build_box(self):
        try:
            session = self.get_session()
            ec2 = session.client("ec2")
            
            self.log("Searching for Prep Build Box to terminate...")
            resp = ec2.describe_instances(
                Filters=[
                    {"Name": "tag:Name", "Values": ["prep-ec2-build-box"]},
                    {"Name": "instance-state-name", "Values": ["pending", "running", "stopping", "stopped"]}
                ]
            )
            
            instances_to_terminate = []
            for r in resp.get("Reservations", []):
                for inst in r.get("Instances", []):
                    instances_to_terminate.append(inst["InstanceId"])
            
            if not instances_to_terminate:
                self.log("No Prep Build Box instances found to terminate.")
                messagebox.showinfo("Info", "No instances found.")
                return

            self.log(f"Terminating instances: {instances_to_terminate}")
            ec2.terminate_instances(InstanceIds=instances_to_terminate)
            self.log("Termination request sent.")
            messagebox.showinfo("Success", f"Terminating {len(instances_to_terminate)} instance(s).")
            
        except Exception as e:
            self.log(f"Error: {e}")
            messagebox.showerror("Error", str(e))

    def _create_prep_user_data(self, bucket, prefix, account_id, region):
        return f"""#!/bin/bash
set -xe
exec > >(tee /var/log/user-data.log) 2>&1
yum update -y
amazon-linux-extras install docker -y
systemctl start docker
yum install -y git unzip

# AWS CLI
curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "awscliv2.zip"
unzip -q awscliv2.zip
./aws/install

# Clone Base
git clone https://github.com/umihico/docker-selenium-lambda.git
cd docker-selenium-lambda

# Download Custom Files
aws s3 cp s3://{bucket}/{prefix}/prep.py ./prep.py
aws s3 cp s3://{bucket}/{prefix}/Dockerprep ./Dockerfile
aws s3 cp s3://{bucket}/{prefix}/requirements_prep.txt ./requirements_prep.txt

# Build & Push to Multiple Regions
aws ecr get-login-password --region {region} | docker login --username AWS --password-stdin {account_id}.dkr.ecr.{region}.amazonaws.com
docker build -t {PREP_ECR_REPO_NAME}:latest .

# Define target regions for multi-region deployment
REGIONS=("us-east-1" "us-west-2" "eu-west-1" "ap-northeast-1" "sa-east-1")

for TARGET_REGION in "${{REGIONS[@]}}"; do
  echo "Processing region: $TARGET_REGION"
  
  # Create repo in target region if not exists (best effort)
  aws ecr create-repository --repository-name {PREP_ECR_REPO_NAME} --region $TARGET_REGION || true
  
  # Login to target region
  aws ecr get-login-password --region $TARGET_REGION | docker login --username AWS --password-stdin {account_id}.dkr.ecr.$TARGET_REGION.amazonaws.com
  
  # Tag and Push
  TARGET_URI="{account_id}.dkr.ecr.$TARGET_REGION.amazonaws.com/{PREP_ECR_REPO_NAME}:latest"
  docker tag {PREP_ECR_REPO_NAME}:latest $TARGET_URI
  docker push $TARGET_URI
done
"""

    def on_prep_create_lambdas(self):
        try:
            session = self.get_session()
            account_id = self._get_account_id(session)
            
            # 1. Calculate needed Lambdas based on users
            text_content = self.prep_users_text.get("1.0", tk.END).strip()
            if not text_content:
                messagebox.showerror("Error", "Please enter users in the 'Execute Prep Process' box first.\n\nWe need to calculate the number of Lambdas required (1 per 10 users).")
                return
                
            users = []
            for line in text_content.split('\n'):
                if ':' in line:
                    users.append(line.strip())
            
            total_users = len(users)
            if total_users == 0:
                messagebox.showerror("Error", "No valid users found.")
                return
            
            # 1 Lambda per 10 users
            import math
            num_lambdas = math.ceil(total_users / 10)
            self.log(f"Calculating resources: {total_users} users => {num_lambdas} Lambdas needed (max 10 users/lambda).")
            
            # 2. Define target regions
            selected_region_option = self.prep_region_var.get()
            if selected_region_option == "Default (All Regions)":
                target_regions = ["us-east-1", "us-west-2", "eu-west-1", "ap-northeast-1", "sa-east-1"]
            else:
                target_regions = [selected_region_option]
            
            self.log(f"Distributing {num_lambdas} Lambdas across regions: {', '.join(target_regions)}")
            
            success_count = 0
            created_lambdas = []
            
            for i in range(num_lambdas):
                # Round-robin distribution
                region = target_regions[i % len(target_regions)]
                
                # Lambda name: edu-gw-prep-worker-{region}-{index}
                # We use a unique index for the region
                # Count how many we've assigned to this region so far
                region_index = (i // len(target_regions)) + 1
                func_name = f"{PREP_LAMBDA_PREFIX}-{region}-{region_index}"
                
                try:
                    self.log(f"[{i+1}/{num_lambdas}] Deploying {func_name} to {region}...")
                    
                    region_session = boto3.Session(
                        aws_access_key_id=self.access_key_var.get().strip(),
                        aws_secret_access_key=self.secret_key_var.get().strip(),
                        region_name=region
                    )
                    
                    # Ensure Role (global ARN, but need to check if we need to recreate in region? No, IAM is global)
                    # But we need to pass the ARN.
                    role_arn = self._ensure_lambda_role(session) # Use main session for IAM
                    
                    # Image URI in TARGET region
                    image_uri = f"{account_id}.dkr.ecr.{region}.amazonaws.com/{PREP_ECR_REPO_NAME}:latest"
                    
                    self._create_or_update_lambda(
                        region_session, 
                        func_name, 
                        role_arn, 
                        timeout=900, 
                        package_type="Image",
                        image_uri=image_uri,
                        env_vars={"S3_BUCKET": self.s3_bucket_var.get().strip()}
                    )
                    success_count += 1
                    created_lambdas.append(func_name)
                    
                except Exception as e:
                    error_msg = str(e)
                    self.log(f"ERROR creating {func_name} in {region}: {error_msg}")
                    if "RepositoryNotFoundException" in error_msg or "ImageNotFoundException" in error_msg or "404" in error_msg:
                        self.log(f"  -> HINT: The Docker image likely doesn't exist in {region}. Run 'Launch Prep Build Box' again to push images to all regions.")
                    
            if success_count > 0:
                messagebox.showinfo("Success", f"Provisioned {success_count} Lambdas across {len(target_regions)} regions.")
            else:
                msg = "Failed to provision any Lambdas.\n\nMost likely cause: Docker images missing in target regions.\n\nSOLUTION: Terminate the old Build Box and click 'Launch Prep Build Box' again to push images to all regions."
                messagebox.showerror("Provisioning Failed", msg)
            
        except Exception as e:
            self.log(f"Error: {e}")
            messagebox.showerror("Error", str(e))

    def on_prep_invoke(self):
        # 1. Parse Users (UI interaction must be on main thread)
        self.log("Parsing users from text area...")
        text_content = self.prep_users_text.get("1.0", tk.END).strip()
        if not text_content:
            self.log("ERROR: No text content in users box.")
            messagebox.showerror("Error", "Please enter users (email:password)")
            return
            
        users = []
        for line in text_content.split('\n'):
            if ':' in line:
                parts = line.split(':', 1)
                users.append((parts[0].strip(), parts[1].strip()))
            elif line.strip():
                self.log(f"WARNING: Skipping invalid line: {line}")
        
        total_users = len(users)
        self.log(f"Found {total_users} valid users.")
        
        if total_users == 0:
            self.log("ERROR: No valid users found after parsing.")
            messagebox.showerror("Error", "No valid users found (format: email:password)")
            return
        
        # 2. Identify Target Lambdas (UI interaction)
        selected_region_option = self.prep_region_var.get()
        if selected_region_option == "Default (All Regions)":
            target_regions = ["us-east-1", "us-west-2", "eu-west-1", "ap-northeast-1", "sa-east-1"]
        else:
            target_regions = [selected_region_option]
        
        self.log(f"Target Regions: {target_regions}")
        
        # 3. Prepare Batched Tasks (group users by region and lambda)
        # Batch size: process up to 10 users per Lambda invocation
        BATCH_SIZE = 10
        batched_tasks = []
        
        for i, (email, password) in enumerate(users):
            region = target_regions[i % len(target_regions)]
            lambda_index = (i // (BATCH_SIZE * len(target_regions))) + 1
            func_name = f"{PREP_LAMBDA_PREFIX}-{region}-{lambda_index}"
            
            # Find or create batch for this function
            batch_found = False
            for batch in batched_tasks:
                if batch['func_name'] == func_name and len(batch['users']) < BATCH_SIZE:
                    batch['users'].append({'email': email, 'password': password})
                    batch_found = True
                    break
            
            if not batch_found:
                batched_tasks.append({
                    'region': region,
                    'func_name': func_name,
                    'users': [{'email': email, 'password': password}]
                })
        
        self.log(f"Prepared {len(batched_tasks)} batched tasks for {len(users)} users across regions.")
        self.log(f"Average batch size: {len(users) / len(batched_tasks):.1f} users per Lambda")
        
        messagebox.showinfo("Started", "Background process started. Check logs for progress.")

        # 4. Run Execution in Background Thread
        def _run_background():
            self.log("=" * 60)
            self.log("STARTING DISTRIBUTED PREP PROCESS (Background - Batched Mode)")
            self.log("=" * 60)
            
            results = {'success': 0, 'failed': 0, 'total_users': 0}
            
            # Helper for batch invocation
            def invoke_batch_prep(batch_task):
                region = batch_task['region']
                func_name = batch_task['func_name']
                users = batch_task['users']
                
                try:
                    # Create session for this region
                    region_session = boto3.Session(
                        aws_access_key_id=self.access_key_var.get().strip(),
                        aws_secret_access_key=self.secret_key_var.get().strip(),
                        region_name=region
                    )
                    lam = region_session.client("lambda")
                    
                    # Send batch of users to Lambda
                    event = {"users": users}
                    resp = lam.invoke(
                        FunctionName=func_name,
                        InvocationType="Event", # Async
                        Payload=json.dumps(event).encode("utf-8")
                    )
                    
                    user_count = len(users)
                    if resp.get("StatusCode") == 202:
                        return (True, f"Sent {user_count} user(s) to {func_name} ({region})")
                    else:
                        return (False, f"Status {resp.get('StatusCode')} for {user_count} user(s)")
                except Exception as e:
                    return (False, f"Exception: {str(e)}")

            # Execute batches in parallel
            max_workers = min(20, len(batched_tasks))  # Limit concurrent Lambda invocations
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_to_batch = {executor.submit(invoke_batch_prep, batch): batch for batch in batched_tasks}
                
                for future in as_completed(future_to_batch):
                    batch = future_to_batch[future]
                    try:
                        success, msg = future.result()
                        user_count = len(batch['users'])
                        results['total_users'] += user_count
                        if success:
                            results['success'] += user_count
                            self.log(f"  ✅ Batch ({user_count} users): {msg}")
                        else:
                            results['failed'] += user_count
                            self.log(f"  ❌ Batch ({user_count} users): {msg}")
                    except Exception as e:
                        user_count = len(batch['users'])
                        results['failed'] += user_count
                        self.log(f"  ❌ Batch ({user_count} users): Exception {e}")
            
            self.log("=" * 60)
            self.log(f"PROCESS COMPLETE: {results['success']} users triggered, {results['failed']} failed")
            self.log(f"Total users: {results['total_users']}")
            self.log("=" * 60)
            
            # Show completion popup on main thread
            self.after(0, lambda: messagebox.showinfo("Complete", f"Triggered: {results['success']}\nFailed: {results['failed']}"))

        threading.Thread(target=_run_background, daemon=True).start()

    def on_stop_local_prep(self):
        """Signal the running local prep process to stop."""
        if self.stop_event:
            self.log("Signal sent to stop local prep process...")
            self.stop_event.set()
            self.stop_button.config(state="disabled")

    def on_prep_run_locally(self):
        """Run the prep process locally on the desktop (Threaded)."""
        # Check if already running
        if self.local_prep_thread and self.local_prep_thread.is_alive():
            if messagebox.askyesno("Running", "A process is already running. Stop it and start new?"):
                self.on_stop_local_prep()
                # Wait for it to stop (non-blocking wait would be better but simple join is ok for now)
                self.log("Waiting for previous process to stop...")
                self.local_prep_thread.join(timeout=10)
                if self.local_prep_thread.is_alive():
                    self.log("WARNING: Previous process did not stop gracefully.")
            else:
                return

        try:
            # 1. Get Credentials (UI thread)
            text_content = self.prep_users_text.get("1.0", tk.END).strip()
            if not text_content:
                messagebox.showerror("Error", "Please enter users in the 'Execute Prep Process' box first (email:password).")
                return
                
            lines = [l.strip() for l in text_content.split('\n') if ':' in l]
            if not lines:
                messagebox.showerror("Error", "No valid users found (format: email:password).")
                return
                
            # 2. Check/Install Dependencies
            self.log("Checking local dependencies (selenium, undetected-chromedriver)...")
            import subprocess
            import sys
            
            try:
                import selenium
                import undetected_chromedriver
            except ImportError:
                self.log("Installing missing dependencies...")
                subprocess.check_call([sys.executable, "-m", "pip", "install", "selenium", "undetected-chromedriver"])
                self.log("Dependencies installed.")
                
            # 3. Import prep_local
            try:
                import prep_local
            except ImportError:
                # Try to add current dir to path
                sys.path.append(os.path.dirname(os.path.abspath(__file__)))
                import prep_local
                
            # 4. Start Thread with parallel parameters
            self.stop_event = threading.Event()
            self.stop_button.config(state="normal")
            
            session = self.get_session()
            s3_bucket = self.s3_bucket_var.get().strip()
            
            # Get concurrency setting
            max_concurrent = self.concurrent_accounts_var.get()
            
            # Get screen dimensions
            screen_width = self.winfo_screenwidth()
            screen_height = self.winfo_screenheight()
            
            self.log(f"Starting parallel prep with {max_concurrent} concurrent windows")
            self.log(f"Detected screen: {screen_width}x{screen_height}")
            
            self.local_prep_thread = threading.Thread(
                target=self._run_local_prep_thread,
                args=(lines, session, s3_bucket, prep_local, max_concurrent, screen_width, screen_height)
            )
            self.local_prep_thread.daemon = True
            self.local_prep_thread.start()
            
        except Exception as e:
            self.log(f"Error starting local prep: {e}")
            traceback.print_exc()
            messagebox.showerror("Error", str(e))

    def _run_local_prep_thread(self, lines, session, s3_bucket, prep_module, max_concurrent, screen_width, screen_height):
        """Background thread for local prep execution - PARALLEL with window tiling."""
        try:
            total_accounts = len(lines)
            self.log(f"Starting PARALLEL prep for {total_accounts} account(s) with {max_concurrent} concurrent windows")
            self.log(f"Screen resolution: {screen_width}x{screen_height}")
            
            # Track results
            results = {'success': 0, 'failed': 0, 'stopped': 0}
            
            def process_single_account(args):
                """Process a single account with window positioning"""
                window_index, line = args
                
                if self.stop_event.is_set():
                    return ('stopped', None, None)
                
                email, password = line.split(':', 1)
                email = email.strip()
                password = password.strip()
                
                self.log(f"[Window {window_index + 1}] Starting prep for {email}...")
                
                try:
                    result = prep_module.run_prep_process(
                        email, password, session, s3_bucket,
                        stop_event=self.stop_event,
                        window_index=window_index,
                        total_windows=max_concurrent,
                        screen_width=screen_width,
                        screen_height=screen_height
                    )
                    self.log(f"[Window {window_index + 1}] {email}: {result}")
                    return ('success' if result and 'Failed' not in str(result) else 'failed', email, result)
                except Exception as e:
                    self.log(f"[Window {window_index + 1}] {email}: ERROR - {e}")
                    return ('failed', email, str(e))
            
            # Process in batches of max_concurrent
            from concurrent.futures import ThreadPoolExecutor, as_completed
            
            for batch_start in range(0, total_accounts, max_concurrent):
                if self.stop_event.is_set():
                    self.log("Process stopped by user.")
                    break
                
                batch_end = min(batch_start + max_concurrent, total_accounts)
                batch_lines = lines[batch_start:batch_end]
                batch_size = len(batch_lines)
                
                self.log(f"\n{'='*50}")
                self.log(f"Processing batch: accounts {batch_start + 1}-{batch_end} of {total_accounts}")
                self.log(f"{'='*50}")
                
                # Create indexed tasks for this batch
                batch_tasks = [(i, line) for i, line in enumerate(batch_lines)]
                
                with ThreadPoolExecutor(max_workers=batch_size) as executor:
                    futures = {executor.submit(process_single_account, task): task for task in batch_tasks}
                    
                    for future in as_completed(futures):
                        status, email, result = future.result()
                        if status == 'success':
                            results['success'] += 1
                        elif status == 'stopped':
                            results['stopped'] += 1
                        else:
                            results['failed'] += 1
                
                if batch_end < total_accounts and not self.stop_event.is_set():
                    self.log(f"\nBatch complete. Waiting 5 seconds before next batch...")
                    time.sleep(5)
            
            # Summary
            self.log(f"\n{'='*50}")
            self.log(f"PARALLEL PREP COMPLETE")
            self.log(f"Success: {results['success']}, Failed: {results['failed']}, Stopped: {results['stopped']}")
            self.log(f"{'='*50}")
            
            if not self.stop_event.is_set():
                self.after(0, lambda: messagebox.showinfo("Complete", 
                    f"Parallel prep finished.\n\nSuccess: {results['success']}\nFailed: {results['failed']}"))
            else:
                self.after(0, lambda: messagebox.showinfo("Stopped", "Process stopped by user."))
                
        except Exception as e:
            self.log(f"Error in local prep thread: {e}")
            traceback.print_exc()
        finally:
            self.after(0, lambda: self.stop_button.config(state="disabled"))

# ======================================================================
# Entry point
# ======================================================================

if __name__ == "__main__":
    app = AwsEducationApp()
    app.mainloop()
