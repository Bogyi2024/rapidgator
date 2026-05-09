import os
import re
import requests
import subprocess
from huggingface_hub import HfApi

# === READ SECRETS FROM GITHUB ACTIONS ENVIRONMENT ===
# These are passed automatically by your React Web UI!
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
    download_url = dl_res['response']['download_url']
    
    local_filepath = os.path.join(output_path, filename)
    
    print(f"[*] Downloading with aria2c (16 connections)...")
    subprocess.run([
        'aria2c', '-x', '16', '-s', '16', 
        '-d', output_path, '-o', filename, download_url
    ], check=True)
    
    print(f"[*] Authenticating with Hugging Face...")
    api = HfApi(token=hf_token)
    
    # Auto-create the repo if it doesn't exist yet
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
