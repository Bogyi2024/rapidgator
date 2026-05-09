import os
import re
import requests
import subprocess
import sys
from huggingface_hub import HfApi

# === READ SECRETS FROM GITHUB ACTIONS ENVIRONMENT ===
rg_email = os.environ.get('RG_EMAIL')
rg_password = os.environ.get('RG_PASSWORD')
rg_link = os.environ.get('RG_LINK')
hf_token = os.environ.get('HF_TOKEN')
repo_id = os.environ.get('REPO_ID')
repo_type = os.environ.get('REPO_TYPE', 'model')

output_path = 'download'

def resolve_short_url(url):
    """Resolves URL shorteners if they are used."""
    try:
        response = requests.head(url, allow_redirects=True, timeout=10)
        return response.url
    except Exception:
        return url

def extract_file_id(url):
    """Pulls the 32-character Rapidgator file ID from the link."""
    resolved = resolve_short_url(url)
    match = re.search(r'/file/([a-zA-Z0-9]{32})', resolved)
    return match.group(1) if match else None

def rapidgator_login(email, password):
    """Logs into Rapidgator and returns the session token."""
    url = 'https://rapidgator.net/api/v2/user/login'
    res = requests.get(url, params={'login': email, 'password': password})
    data = res.json()
    if res.status_code == 200 and data.get('response'):
        return data['response']['token']
    raise Exception(f"Rapidgator Login failed. Check your credentials.")

def main():
    os.makedirs(output_path, exist_ok=True)
    
    print(f"[*] Processing Link: {rg_link}")
    file_id = extract_file_id(rg_link)
    if not file_id:
        raise Exception("Could not extract File ID from the provided link.")
        
    print("[*] Logging into Rapidgator...")
    token = rapidgator_login(rg_email, rg_password)
    
    print(f"[*] Fetching file info for ID: {file_id}")
    info_res = requests.get('https://rapidgator.net/api/v2/file/info', params={'file_id': file_id, 'token': token}).json()
    
    if not info_res.get('response'):
        raise Exception("Failed to get file info. The file might be deleted or offline.")
        
    filename = info_res['response']['file']['name']
    print(f"[*] Found file: {filename}")
    
    print("[*] Generating premium download URL...")
    dl_res = requests.get('https://rapidgator.net/api/v2/file/download', params={'file_id': file_id, 'token': token}).json()
    
    # === NEW: SAFE URL EXTRACTION & WEBHOOK HANDOFF ===
    try:
        if dl_res and dl_res.get('status') == 400:
            raise ValueError(dl_res.get('error', 'API Refused'))
        download_url = dl_res['response']['download_url']
        
    except (TypeError, KeyError, ValueError) as e:
        print(f"⚠️ Rapidgator API Failure / IP Blocked. Reason: {e}")
        print("📞 Calling back to Master Workflow for a runner switch...")
        
        master_owner = os.environ.get('MASTER_OWNER')
        master_repo = os.environ.get('MASTER_REPO')
        master_token = os.environ.get('MASTER_TOKEN')
        worker_pool_str = os.environ.get('WORKER_POOL', '[]')
        current_worker = os.environ.get('GITHUB_REPOSITORY_OWNER')
        
        if not master_token or not master_owner:
            print("❌ No Master credentials provided. Cannot trigger handoff.")
            sys.exit(1)
            
        url = f"https://api.github.com/repos/{master_owner}/{master_repo}/dispatches"
        payload = {
            "event_type": "child_failed",
            "client_payload": {
                "failed_link": rg_link,
                "failed_worker": current_worker,
                "worker_pool": worker_pool_str,
                # Pass back the original inputs so Master can fire the new job identically
                "rg_email": rg_email,
                "rg_password": rg_password,
                "hf_token": hf_token,
                "repo_id": repo_id,
                "repo_type": repo_type
            }
        }
        
        res = requests.post(
            url, json=payload, 
            headers={"Authorization": f"Bearer {master_token}", "Accept": "application/vnd.github.v3+json"}
        )

        if res.status_code == 204:
            print("✅ Successfully notified Master to take over. Shutting down cleanly.")
            sys.exit(42) # Exit 42 tells bash script it was a deliberate handoff!
        else:
            print(f"❌ Failed to notify Master: {res.text}")
            sys.exit(1)
    
    # === END NEW CODE ===

    local_filepath = os.path.join(output_path, filename)
    
    print(f"[*] Downloading with aria2c (16 connections)...")
    subprocess.run([
        'aria2c', '-x', '16', '-s', '16', 
        '-d', output_path, '-o', filename, download_url
    ], check=True)
    
    print(f"[*] Authenticating with Hugging Face...")
    api = HfApi(token=hf_token)
    
    try:
        api.create_repo(repo_id=repo_id, repo_type=repo_type, exist_ok=True)
        print(f"[*] Verified repository: {repo_id}")
    except Exception as e:
        print(f"[*] Repo status: {e}")
        
    print(f"[*] Uploading {filename} to Hugging Face...")
    api.upload_file(
        path_or_fileobj=local_filepath,
        path_in_repo=filename,
        repo_id=repo_id,
        repo_type=repo_type,
        commit_message=f"Upload {filename} via Rapidgator Batch Transfer"
    )
    print("[*] 🎉 Transfer Complete!")

if __name__ == "__main__":
    if not rg_link:
        print("No link provided. Exiting.")
    else:
        main()
