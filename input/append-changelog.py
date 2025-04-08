#!/usr/bin/env python3

import sys
import datetime
import re
import os

def add_changelog_entry(spec_file, author_name, author_email, version, changelog_text):
    # Read the spec file
    with open(spec_file, 'r') as f:
        content = f.readlines()
    
    # Format the changelog entry
    today = datetime.datetime.now()
    day_name = today.strftime("%a")
    month_name = today.strftime("%b")
    day = today.day
    year = today.year
    
    changelog_header = f"* {day_name} {month_name} {day} {year} {author_name} <{author_email}> [{version}]\n"
    changelog_items = []
    
    # Format each line of the changelog text as a separate item
    for line in changelog_text.split('\n'):
        if line.strip():
            changelog_items.append(f"- {line.strip()}\n")
    
    # Find where to insert the new changelog entry
    changelog_index = -1
    for i, line in enumerate(content):
        if re.match(r'^\s*%changelog\s*$', line):
            changelog_index = i
            break
    
    if changelog_index == -1:
        print("Error: %changelog section not found in the spec file.")
        sys.exit(1)
    
    # Insert the new changelog entry after the %changelog line
    new_content = content[:changelog_index+1]
    new_content.append("\n")  # Add a blank line for readability
    new_content.append(changelog_header)
    new_content.extend(changelog_items)
    new_content.append("\n")  # Add a blank line after the entry
    new_content.extend(content[changelog_index+1:])
    
    # Write the updated content back to the file
    with open(spec_file, 'w') as f:
        f.writelines(new_content)
    
    print(f"Successfully added changelog entry to {spec_file}")

def main():
    if len(sys.argv) < 5:
        print("Usage: {} SPEC_FILE AUTHOR_NAME AUTHOR_EMAIL VERSION [CHANGELOG_TEXT]".format(sys.argv[0]))
        print("Example: {} kernel.spec 'John Doe' john@example.com 6.12.15-100 'Turn off libbpf dynamic for perf (Justin M. Forbes)'".format(sys.argv[0]))
        sys.exit(1)
    
    spec_file = sys.argv[1]
    author_name = sys.argv[2]
    author_email = sys.argv[3]
    version = sys.argv[4]
    
    # Get changelog text from arguments or stdin
    if len(sys.argv) > 5:
        changelog_text = sys.argv[5]
    else:
        print("Enter changelog text (Ctrl+D to finish):")
        changelog_text = sys.stdin.read().strip()
    
    if not os.path.isfile(spec_file):
        print(f"Error: Spec file '{spec_file}' not found.")
        sys.exit(1)
    
    add_changelog_entry(spec_file, author_name, author_email, version, changelog_text)

if __name__ == "__main__":
    main()
