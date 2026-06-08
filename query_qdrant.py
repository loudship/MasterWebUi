import urllib.request
import json
import pprint

def query_qdrant_safe(url, limit=1):
    """Query Qdrant with error handling and bounds checking."""
    try:
        req = urllib.request.Request(
            url,
            data=json.dumps({'limit': limit, 'with_payload': True}).encode('utf-8'),
            headers={'Content-Type': 'application/json'}
        )
        
        with urllib.request.urlopen(req) as response:
            data = json.loads(response.read())
            
            # Safe navigation with bounds checking
            points = data.get('result', {}).get('points', [])
            
            if not points:
                print("Error: No points returned from Qdrant")
                return None
            
            first_point = points[0]
            entities = first_point.get('payload', {}).get('metadata', {}).get('entities')
            
            return entities
    
    except urllib.error.HTTPError as e:
        print(f"HTTP Error {e.code}: {e.read().decode()}")
        return None
    except urllib.error.URLError as e:
        print(f"URL Error: {e.reason}")
        return None
    except json.JSONDecodeError as e:
        print(f"JSON decode error: {e}")
        return None
    except Exception as e:
        print(f"Error: {e}")
        return None

if __name__ == "__main__":
    url = 'http://qdrant:6333/collections/open-webui_test_collection/points/scroll'
    result = query_qdrant_safe(url)
    
    if result:
        print("Entities found:")
        pprint.pprint(result)
    else:
        print("Failed to retrieve entities")
