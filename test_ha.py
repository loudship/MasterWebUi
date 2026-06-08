import urllib.request
import urllib.error
import json
import os
from pathlib import Path

OUTPUT_DIR = os.getenv('TEST_OUTPUT_DIR', './outputs')
OUTPUT_FILE = os.getenv('TEST_OUTPUT_FILE', 'ha_test.txt')

# Ensure output directory exists
Path(OUTPUT_DIR).mkdir(parents=True, exist_ok=True)
output_path = os.path.join(OUTPUT_DIR, OUTPUT_FILE)

with open(output_path, 'w') as f:
    req = urllib.request.Request(
        'http://ha-mcp:8080/mcp',
        data=b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}',
        headers={'Content-Type': 'application/json'}
    )
    try:
        f.write("Trying /mcp:\n")
        response = urllib.request.urlopen(req)
        f.write(response.read().decode() + "\n")
    except urllib.error.HTTPError as e:
        f.write(f"HTTP Error {e.code}: {e.read().decode()}\n")
    except urllib.error.URLError as e:
        f.write(f"URL Error: {e.reason}\n")
    except Exception as e:
        f.write(f"Error: {str(e)}\n")

    req = urllib.request.Request(
        'http://ha-mcp:8080/',
        data=b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}',
        headers={'Content-Type': 'application/json'}
    )
    try:
        f.write("Trying /:\n")
        response = urllib.request.urlopen(req)
        f.write(response.read().decode() + "\n")
    except urllib.error.HTTPError as e:
        f.write(f"HTTP Error {e.code}: {e.read().decode()}\n")
    except urllib.error.URLError as e:
        f.write(f"URL Error: {e.reason}\n")
    except Exception as e:
        f.write(f"Error: {str(e)}\n")

print(f"Test output written to: {output_path}")
