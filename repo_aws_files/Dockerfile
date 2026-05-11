FROM umihico/aws-lambda-selenium-python:latest

# Copy the Lambda handler
COPY main.py ${LAMBDA_TASK_ROOT}/

# Install required Python dependencies
# Note: The base image already has selenium and Chrome installed
RUN pip install --no-cache-dir \
    boto3 \
    paramiko \
    pyotp \
    selenium-stealth==1.0.6 \
    selenium-wire==5.1.0

# Set environment variables to disable SeleniumManager at container level
ENV SE_SELENIUM_MANAGER=false
ENV SELENIUM_MANAGER=false
ENV SELENIUM_DISABLE_DRIVER_MANAGER=1

# Set the Lambda handler entrypoint
CMD ["main.handler"]