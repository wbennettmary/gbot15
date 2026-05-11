import os

file_path = r'c:\Users\PC\Desktop\Gbot-v15\templates\base.html'

with open(file_path, 'r', encoding='utf-8') as f:
    lines = f.readlines()

# Find the end of the good top part
# Look for the script ending at line ~1161
start_marker = '            </script>'
start_index = -1

# Look for the start of the good bottom part
# Look for the main-wrapper div to remove it
end_marker = '<div class="main-wrapper">'
end_index = -1

for i, line in enumerate(lines):
    # We look for the specific script tag end around line 1150-1170
    if start_marker in line and i > 1150 and i < 1170:
        start_index = i
    
    # We look for the main-wrapper around 2500
    if end_marker in line and i > 2500:
        end_index = i
        break

if start_index != -1 and end_index != -1:
    print(f"Found cut points: Start {start_index} (Line {start_index+1}), End {end_index} (Line {end_index+1})")
    
    # We want to keep up to start_index (inclusive, keeping </script>)
    # We want to skip from start_index + 1 up to end_index (inclusive, removing <div class="main-wrapper">)
    # So we resume at end_index + 1
    
    new_lines = lines[:start_index+1] + lines[end_index+1:]
    
    with open(file_path, 'w', encoding='utf-8') as f:
        f.writelines(new_lines)
    print("Successfully patched base.html")
else:
    print("Could not find markers!")
    print(f"Start index: {start_index}")
    print(f"End index: {end_index}")
