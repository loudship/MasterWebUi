#!/usr/bin/env python3
import json

# Streamable HTTP MCP Integration with OAuth Static Client
# Dynamic context injection: {{USER_ID}}, {{USER_NAME}}, {{CHAT_ID}}

mcp_config = {
    "type": "mcp_http",
    "url": "http://deep-web-mcp:8000/mcp",
    "auth_type": "oauth2_static",
    "client_id": "openwebui_static_client",
    "client_secret": "secure_static_secret_123",
    "discovery_url": "https://auth.local.company.com/.well-known/openid-configuration",
    "headers": {
        "X-Tenant-User-Id": "{{USER_ID}}",
        "X-Tenant-User-Name": "{{USER_NAME}}",
        "X-Tenant-User-Email": "{{USER_EMAIL}}",
        "X-Tenant-User-Role": "{{USER_ROLE}}",
        "X-Tenant-Chat-Id": "{{CHAT_ID}}",
        "X-Tenant-Message-Id": "{{MESSAGE_ID}}"
    }
}

def generate_config():
    with open("mcp_oauth_config.json", "w") as f:
        json.dump(mcp_config, f, indent=4)
    print("Successfully generated OAuth 2.1 Static Client MCP Configuration payload!")
    print("Please use the Open WebUI Admin Panel to add this configuration:")
    print("1. Go to Admin Panel -> External Tools -> Add Server")
    print("2. Set Type to 'MCP (Streamable HTTP)'")
    print("3. Set Authentication to 'OAuth 2.1 (Static)'")
    print("4. Configure the headers and details as follows:")
    print(json.dumps(mcp_config, indent=2))

if __name__ == "__main__":
    generate_config()
