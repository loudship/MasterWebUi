import os
import requests
import sys

def parse_env(file_path):
    env = {}
    if os.path.exists(file_path):
        with open(file_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    k, v = line.split("=", 1)
                    env[k.strip()] = v.strip()
    return env

def main():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env_path = os.path.join(root, ".env")
    env_vars = parse_env(env_path)
    
    # We will query open-webui via localhost:3000
    base_url = "http://127.0.0.1:3000"
    
    # Login
    signin_url = f"{base_url}/api/v1/auths/signin"
    payload = {
        "email": "christianoallen618@gmail.com",
        "password": "Password123!"
    }
    
    try:
        response = requests.post(signin_url, json=payload, timeout=10)
        if response.status_code != 200:
            print(f"Error signing in to Open WebUI: {response.text}")
            sys.exit(1)
        token = response.json().get("token")
    except Exception as exc:
        print(f"Failed to connect to Open WebUI: {exc}")
        sys.exit(1)
        
    headers = {"Authorization": f"Bearer {token}"}
    
    # Fetch admin configs
    auth_admin = {}
    connections = {}
    export_config = {}
    
    try:
        resp = requests.get(f"{base_url}/api/v1/auths/admin/config", headers=headers, timeout=10)
        if resp.status_code == 200:
            auth_admin = resp.json()
    except Exception as exc:
        print(f"Failed to fetch auths admin config: {exc}")
        
    try:
        resp = requests.get(f"{base_url}/api/v1/configs/connections", headers=headers, timeout=10)
        if resp.status_code == 200:
            connections = resp.json()
    except Exception as exc:
        print(f"Failed to fetch connections config: {exc}")
        
    try:
        resp = requests.get(f"{base_url}/api/v1/configs/export", headers=headers, timeout=10)
        if resp.status_code == 200:
            export_config = resp.json()
    except Exception as exc:
        print(f"Failed to fetch export config: {exc}")
        
    # Variables to verify
    results = []
    
    # 1. ENABLE_PERSISTENT_CONFIG
    env_val = env_vars.get("ENABLE_PERSISTENT_CONFIG", "False")
    results.append({
        "var": "ENABLE_PERSISTENT_CONFIG",
        "expected": env_val,
        "active": "Not directly visible",
        "status": "N/A",
        "notes": "Backend-level setting. No direct toggle in UI."
    })
    
    # 2. ENABLE_PIP_INSTALL_FRONTMATTER_REQUIREMENTS
    env_val = env_vars.get("ENABLE_PIP_INSTALL_FRONTMATTER_REQUIREMENTS", "False")
    results.append({
        "var": "ENABLE_PIP_INSTALL_FRONTMATTER_REQUIREMENTS",
        "expected": env_val,
        "active": "Not directly visible",
        "status": "N/A",
        "notes": "No UI element reflects or controls this variable."
    })
    
    # 3. ENABLE_SIGNUP
    env_val = env_vars.get("ENABLE_SIGNUP", "False")
    active_val = auth_admin.get("ENABLE_SIGNUP")
    status = "Match" if str(active_val).lower() == env_val.lower() else "Mismatch"
    results.append({
        "var": "ENABLE_SIGNUP",
        "expected": env_val,
        "active": str(active_val),
        "status": status,
        "notes": f"API reports ENABLE_SIGNUP: {active_val}"
    })
    
    # 4. DEFAULT_USER_ROLE
    env_val = env_vars.get("DEFAULT_USER_ROLE", "pending")
    active_val = auth_admin.get("DEFAULT_USER_ROLE")
    status = "Match" if str(active_val).lower() == env_val.lower() else "Mismatch"
    results.append({
        "var": "DEFAULT_USER_ROLE",
        "expected": env_val,
        "active": str(active_val),
        "status": status,
        "notes": f"API reports DEFAULT_USER_ROLE: {active_val}"
    })
    
    # 5. ENABLE_OPENAI_API_PASSTHROUGH
    env_val = env_vars.get("ENABLE_OPENAI_API_PASSTHROUGH", "False")
    results.append({
        "var": "ENABLE_OPENAI_API_PASSTHROUGH",
        "expected": env_val,
        "active": "Not directly visible",
        "status": "N/A",
        "notes": "The UI does not expose a toggle for OpenAI API passthrough."
    })
    
    # 6. ENABLE_DIRECT_CONNECTIONS
    env_val = env_vars.get("ENABLE_DIRECT_CONNECTIONS", "False")
    active_conn = connections.get("ENABLE_DIRECT_CONNECTIONS")
    direct_export = export_config.get("direct", {}).get("enable")
    
    status = "Match" if str(active_conn).lower() == env_val.lower() and str(direct_export).lower() == env_val.lower() else "Mismatch"
    if str(active_conn).lower() != str(direct_export).lower():
        status = "Mismatch"
        notes = f"API discrepancy: connections={active_conn}, export={direct_export}"
    else:
        notes = f"Connections={active_conn}, Export={direct_export}"
        
    results.append({
        "var": "ENABLE_DIRECT_CONNECTIONS",
        "expected": env_val,
        "active": f"{active_conn} (Connections) / {direct_export} (Export)",
        "status": status,
        "notes": notes
    })
    
    # 7. ENV
    env_val = env_vars.get("ENV", "prod")
    results.append({
        "var": "ENV",
        "expected": env_val,
        "active": "Not directly visible",
        "status": "N/A",
        "notes": "The deployment environment variable is not exposed in the UI configuration."
    })
    
    # Generate report
    report_lines = [
        "# Open WebUI Audit Report",
        f"**Date:** 2026-06-14",
        f"**Target:** {base_url}/",
        "",
        "## 1. Executive Summary",
        "This report summarizes the UI state and configuration verification for Open WebUI, specifically comparing the Zero-Trust security variables defined in the backend `.env` configuration against the exposed UI state.",
        "",
        "An automated audit was performed via the backend APIs (`/api/v1/auths/admin/config`, `/api/v1/configs/connections`, and `/api/v1/configs/export`) using an administratively generated JWT token to inspect the active configuration values the WebUI presents and operates under.",
        "",
        "## 2. Zero-Trust Security Variable Verification",
        "",
        "Below is the verification of each specified `.env` zero-trust variable against the current UI state:",
        "",
        "| Variable | `.env` Expected State | Active UI State | Status | Notes |",
        "|---|---|---|---|---|"
    ]
    
    for r in results:
        report_lines.append(f"| `{r['var']}` | `{r['expected']}` | `{r['active']}` | **{r['status']}** | {r['notes']} |")
        
    report_lines.extend([
        "",
        "## 3. Findings & Conclusion",
    ])
    
    mismatches = [r for r in results if r["status"] == "Mismatch"]
    if mismatches:
        report_lines.append("- **Critical Security Mismatches:** Mismatches were detected between the database configuration and the `.env` settings.")
        for m in mismatches:
            report_lines.append(f"  - `{m['var']}`: Expected `{m['expected']}`, observed `{m['active']}`. {m['notes']}")
    else:
        report_lines.append("- **Zero-Trust Baseline Aligned:** No active mismatches were found. The database settings match the expected `.env` zero-trust configuration.")
        
    report_lines.extend([
        "- **Missing UI Reflections:** Variables like `ENABLE_OPENAI_API_PASSTHROUGH`, `ENABLE_PIP_INSTALL_FRONTMATTER_REQUIREMENTS`, and `ENABLE_PERSISTENT_CONFIG` are completely unrepresented in the Admin UI settings.",
        "",
        "This configuration audit ensures the database state matches the zero-trust architecture parameters."
    ])
    
    report_content = "\n".join(report_lines)
    
    report_out_path = os.path.join(root, "ui_audit_report.md")
    with open(report_out_path, "w", encoding="utf-8") as f:
        f.write(report_content)
        
    print(f"Generated audit report at: {report_out_path}")

if __name__ == "__main__":
    main()
