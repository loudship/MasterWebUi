import urllib.request
import json
import pprint

req = urllib.request.Request(
    'http://qdrant:6333/collections/open-webui_test_collection/points/scroll',
    data=b'{"limit": 1, "with_payload": true}',
    headers={'Content-Type': 'application/json'}
)
try:
    with urllib.request.urlopen(req) as response:
        data = json.loads(response.read())
        pprint.pprint(data['result']['points'][0]['payload']['metadata']['entities'])
except Exception as e:
    print(f"Error: {e}")
