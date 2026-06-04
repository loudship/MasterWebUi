import urllib.request
import urllib.error
import json

with open('/app/out.txt', 'w') as f:
    req = urllib.request.Request(
        'http://ha-mcp:8080/mcp',
        data=b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}',
        headers={'Content-Type': 'application/json'}
    )
    try:
        f.write("Trying /mcp:\n")
        f.write(urllib.request.urlopen(req).read().decode() + "\n")
    except urllib.error.HTTPError as e:
        f.write(e.read().decode() + "\n")
    except Exception as e:
        f.write(str(e) + "\n")

    req = urllib.request.Request(
        'http://ha-mcp:8080/',
        data=b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}',
        headers={'Content-Type': 'application/json'}
    )
    try:
        f.write("Trying /:\n")
        f.write(urllib.request.urlopen(req).read().decode() + "\n")
    except urllib.error.HTTPError as e:
        f.write(e.read().decode() + "\n")
    except Exception as e:
        f.write(str(e) + "\n")
