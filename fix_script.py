# Fix script to restore prep_local.py
import os

# Read the correct function from prep_local_CORRECT.py
with open('prep_local_CORRECT.py', 'r', encoding='utf-8') as f:
    correct_content = f.read()

# Read the current prep_local.py to get the helper functions (lines 1-494)
with open('prep_local.py', 'r', encoding='utf-8', errors='ignore') as f:
    lines = f.readlines()

# Find where 'def run_prep_process' first appears
helper_lines = []
for i, line in enumerate(lines):
    if 'def run_prep_process' in line:
        break
    helper_lines.append(line)

# Combine: helper functions + correct run_prep_process function (skip first 2 comment lines)
correct_function = correct_content.split('def run_prep_process', 1)[1]
new_content = ''.join(helper_lines) + '\ndef run_prep_process' + correct_function

# Write the fixed file
with open('prep_local.py', 'w', encoding='utf-8') as f:
    f.write(new_content)

print("File fixed! prep_local.py has been restored.")
