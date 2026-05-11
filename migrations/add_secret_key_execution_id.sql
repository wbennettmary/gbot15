-- Migration: Add secret_key and execution_id columns to aws_generated_password table
-- Date: 2026-02-10
-- Purpose: Support 2FA secret key storage and execution tracking

-- Add secret_key column (for storing 2FA secret keys)
ALTER TABLE aws_generated_password 
ADD COLUMN IF NOT EXISTS secret_key VARCHAR(100);

-- Add execution_id column (for linking passwords to specific bulk executions)
ALTER TABLE aws_generated_password 
ADD COLUMN IF NOT EXISTS execution_id VARCHAR(100);

-- Create index on execution_id for faster queries
CREATE INDEX IF NOT EXISTS idx_aws_generated_password_execution_id 
ON aws_generated_password(execution_id);

-- Verify the changes
SELECT column_name, data_type, is_nullable 
FROM information_schema.columns 
WHERE table_name = 'aws_generated_password'
ORDER BY ordinal_position;
