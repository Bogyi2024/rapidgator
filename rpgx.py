#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rpgx.py - Rapidgator -> Hugging Face transfer worker (GitHub Actions)

FIX APPLIED (based on 4 live diagnostic runs against real Rapidgator links):
  Run 1 (production, Linux): curl_cffi login -> SSL_ERROR_SYSCALL (reset)
  Run 2 (diagnostic, Linux):  requests OK (0.74s) | curl_cffi FAILED (timeout 30s)
  Run 3 (diagnostic, Windows): requests OK (0.58s) | curl_cffi OK (3.97s)
  Run 4 (full chain + real aria2c download): requests OK end-to-end

  Across these runs, plain `requests` succeeded every time. `curl_cffi`
  (the "Chrome fingerprint fix" this script previously hard-committed to)
  only succeeded 1 of 3 attempts - it is not a reliable fix and was in fact
  the backend that failed in the original production incident. The old
  theory ("curl_cffi is the actual fix: requests' fingerprint is what
  rapidgator.net drops") is not supported by this evidence; if anything the
  reverse looks true on these runners.

  This version no longer hard-commits to one backend. Every Rapidgator HTTP
  call now tries `requests` first (fast, and empirically reliable so far),
  and only falls back to `curl_cffi` (Chrome impersonation) if `requests`
  exhausts its retries. This survives either direction of block:
    - If a future runner IP gets blocked on the `requests` fingerprint,
      curl_cffi is tried automatically before giving up.
    - If curl_cffi is the one blocked (as seen twice here), requests
      already succeeded and curl_cffi is never needed.

  Nothing else about the retry/exit-code/handoff contract changed:
        0  success
        1  retryable failure on this runner        -> bash retries here
        2  permanent failure (file gone, bad creds,
           HF auth rejected)                       -> bash stops, no handoff
       42  IP/WAF block AND handoff accepted       -> bash stops cleanly,
                                                      Master switches worker
  aria2c is still hardened (retries, browser UA, no pre-allocation, real
  exit-code check) and the downloaded file is still size-verified before
  uploading. Logging is still unbuffered. Handoff still probes which pool
  token has push access to MASTER_OWNER/MASTER_REPO before falling back to
  dispatching into this repo with GITHUB_TOKEN.

INPUTS - unchanged, all still come from the frontend / workflow_dispatch:
  RG_EMAIL  RG_PASSWORD  RG_LINK  HF_TOKEN  REPO_ID  REPO_TYPE
  WORKER_POOL  MASTER_OWNER  MASTER_REPO  MASTER_TOKEN  FALLBACK_ATTEMPT
"""

import json
import os
import re
import sys
import time
import subprocess

# Unbuffered output so the Actions log shows real order/timing.
try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except Exception:
    pass


# --------------------------------------------------------------------------- #
# HTTP backends: requests (primary, empirically reliable) + curl_cffi
# (Chrome TLS fingerprint, automatic fallback only). Both are tried per call;
# nothing is hard-committed to a single fingerprint any more - see docstring.
# --------------------------------------------------------------------------- #
import requests as _requests_mod  # type: ignore

_HAVE_CURL_CFFI = False
try:
    from curl_cffi import requests as _curl_cffi_mod  # type: ignore
    _HAVE_CURL_CFFI = True
    print("[*] HTTP backends available: requests (primary), curl_cffi (fallback)")
except ImportError:  # pragma: no cover
    _curl_cffi_mod = None
    print("[*] HTTP backend available: requests only (curl_cffi not installed)")
    print("[!] Fallback backend disabled. Recommended: pip install curl_cffi")

from huggingface_hub import HfApi  # type: ignore

RG_API = "https://rapidgator.net/api/v2"
GITHUB_API = "https://api.github.com"
TIMEOUT = 60
OUTPUT_PATH = "download"

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


class RetryableError(Exception):
    """Transient / network / TLS / WAF problem. Another runner may succeed."""


class PermanentError(Exception):
    """Retrying anywhere will not help (file deleted, bad link, bad token)."""


# --------------------------------------------------------------------------- #
# Inputs (unchanged sources)
# --------------------------------------------------------------------------- #
rg_email = os.environ.get("RG_EMAIL")
rg_password = os.environ.get("RG_PASSWORD")
rg_link = os.environ.get("RG_LINK")
hf_token = os.environ.get("HF_TOKEN")
repo_id = os.environ.get("REPO_ID")
repo_type = os.environ.get("REPO_TYPE", "model")


def new_session(backend):
    """backend is 'requests' or 'curl_cffi'."""
    if backend == "curl_cffi" and _HAVE_CURL_CFFI:
        return _curl_cffi_mod.Session(impersonate="chrome")
    return _requests_mod.Session()


# Used for the GitHub handoff calls, which never showed a problem in testing;
# keep those on requests directly (no need for dual-backend there).
def new_github_session():
    return _requests_mod.Session()


def _do_get(session, path, params):
    res = session.get(
        f"{RG_API}{path}",
        params=params,
        timeout=TIMEOUT,
        headers={"Accept": "application/json", "User-Agent": BROWSER_UA},
    )
    if res.status_code in (429, 500, 502, 503, 504):
        raise RetryableError(f"HTTP {res.status_code} from {path}")
    try:
        return res, res.json()
    except ValueError:
        snippet = (res.text or "")[:200].replace("\n", " ")
        raise RetryableError(
            f"Non-JSON reply from {path} (HTTP {res.status_code}): {snippet!r} "
            "- this is a WAF/anti-bot page, not the API"
        )


def rg_get(path, params, tries_per_backend=2):
    """
    GET <RG_API><path> with timeout + backoff, trying `requests` first and
    only falling back to `curl_cffi` if every requests attempt fails.
    Returns (res, json).
    """
    backends = ["requests"]
    if _HAVE_CURL_CFFI:
        backends.append("curl_cffi")

    last = None
    for backend in backends:
        session = new_session(backend)
        for i in range(1, tries_per_backend + 1):
            try:
                return _do_get(session, path, params)
            except RetryableError as exc:
                last = exc
            except Exception as exc:  # SSLError, ConnectionError, Timeout, ...
                last = RetryableError(f"{type(exc).__name__}: {exc}")

            if i < tries_per_backend:
                wait = 5 * i
                print(f"[!] [{backend}] {path} failed: {last}")
                print(f"[!] [{backend}] retry {i}/{tries_per_backend - 1} in {wait}s")
                time.sleep(wait)
            else:
                print(f"[!] [{backend}] {path} failed after {tries_per_backend} "
                      f"attempt(s): {last}")

        if backend != backends[-1]:
            print(f"[*] Switching HTTP backend: requests -> curl_cffi for {path}")

    raise last


# --------------------------------------------------------------------------- #
# Rapidgator
# --------------------------------------------------------------------------- #
def extract_file_id(url):
    """32-char Rapidgator file id. No HEAD request when the link has it already."""
    match = re.search(r"/file/([a-zA-Z0-9]{32})", url or "")
    if match:
        return match.group(1)
    try:
        session = new_session("requests")
        res = session.head(url, allow_redirects=True, timeout=30)
        match = re.search(r"/file/([a-zA-Z0-9]{32})", res.url or "")
        return match.group(1) if match else None
    except Exception:
        return None


def rapidgator_login(email, password):
    print("[*] Logging into Rapidgator...")
    _, data = rg_get("/user/login", {"login": email, "password": password})
    token = (data.get("response") or {}).get("token")
    if token:
        print("[*] Login OK.")
        return token
    # A rejected login is not an IP problem: another runner will not fix it.
    raise PermanentError(f"Rapidgator login rejected: {data}")


def file_info(file_id, token):
    _, data = rg_get("/file/info", {"file_id": file_id, "token": token})
    info = (data.get("response") or {}).get("file") or {}
    name = info.get("name")
    if not name:
        raise PermanentError(f"File {file_id} is offline or deleted: {data}")
    return name, info


def file_download_url(file_id, token):
    _, data = rg_get("/file/download", {"file_id": file_id, "token": token})
    url = (data.get("response") or {}).get("download_url")
    if not url:
        raise PermanentError(f"Rapidgator refused the download URL: {data}")
    return url


def aria2_download(url, outdir, filename):
    path = os.path.join(outdir, filename)
    cmd = [
        "aria2c", url,
        "-x", "16", "-s", "16", "-k", "1M",
        "-d", outdir, "-o", filename,
        "--allow-overwrite=true",
        "--auto-file-renaming=false",
        "--file-allocation=none",
        "--max-tries=5", "--retry-wait=10",
        "--timeout=60", "--connect-timeout=30",
        f"--user-agent={BROWSER_UA}",
        "--referer=https://rapidgator.net/",
        "--summary-interval=30",
        "--console-log-level=warn",
    ]
    print("[*] Downloading with aria2c (16 connections)...")
    rc = subprocess.run(cmd).returncode
    if rc != 0:
        raise RetryableError(f"aria2c exited with code {rc} (download link likely expired)")
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        raise RetryableError(f"aria2c produced no usable file: {filename}")
    return path


# --------------------------------------------------------------------------- #
# Hugging Face
# --------------------------------------------------------------------------- #
def upload_to_hf(local_path, filename):
    print("[*] Authenticating with Hugging Face...")
    api = HfApi(token=hf_token)

    try:
        api.create_repo(repo_id=repo_id, repo_type=repo_type, exist_ok=True)
        print(f"[*] Verified repository: {repo_id}")
    except Exception as exc:
        message = str(exc)
        if any(k in message for k in ("401", "403", "Unauthorized", "Forbidden",
                                      "Invalid username or password", "token")):
            raise PermanentError(f"HF token rejected for {repo_id}: {message}")
        print(f"[*] Repo status: {message}")

    print(f"[*] Uploading {filename} to Hugging Face...")
    try:
        api.upload_file(
            path_or_fileobj=local_path,
            path_in_repo=filename,
            repo_id=repo_id,
            repo_type=repo_type,
            commit_message=f"Upload {filename} via Rapidgator Batch Transfer",
        )
    except Exception as exc:
        raise RetryableError(f"HF upload failed: {type(exc).__name__}: {exc}")


# --------------------------------------------------------------------------- #
# Master handoff (payload kept identical - the Master workflow expects these keys)
# --------------------------------------------------------------------------- #
def _pool_tokens():
    """Every distinct token in WORKER_POOL, in pool order. No name matching."""
    try:
        pool = json.loads(os.environ.get("WORKER_POOL") or "[]")
    except ValueError:
        print("[!] WORKER_POOL is not valid JSON; ignoring it.")
        return []
    tokens = []
    for worker in pool if isinstance(pool, list) else []:
        token = worker.get("token")
        if token and token not in tokens:
            tokens.append(token)
    return tokens


def _token_can_push(owner, repo, token):
    """True only if this token actually has write access to owner/repo.

    Capability check instead of matching user names, so switching the Master to
    another account (or a bot/org account) keeps working without code changes.
    """
    try:
        session = new_github_session()
        res = session.get(
            f"{GITHUB_API}/repos/{owner}/{repo}",
            timeout=30,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "User-Agent": "handoff-probe",
            },
        )
    except Exception:
        return False
    if res.status_code != 200:
        return False
    try:
        return bool((res.json().get("permissions") or {}).get("push"))
    except ValueError:
        return False


def _dispatch_once(owner, repo, token, payload):
    """POST /repos/{owner}/{repo}/dispatches. Returns (ok, status, body)."""
    try:
        session = new_github_session()
        res = session.post(
            f"{GITHUB_API}/repos/{owner}/{repo}/dispatches",
            json=payload,
            timeout=TIMEOUT,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
    except Exception as exc:
        return False, 0, f"{type(exc).__name__}: {exc}"

    if res.status_code == 204:
        return True, 204, ""
    return False, res.status_code, (res.text or "").strip()


def trigger_master_handoff(reason):
    print(f"[!] Runner Failed. Reason: {reason}")
    print("[*] Calling back to Master Workflow for a runner switch...")

    master_owner = os.environ.get("MASTER_OWNER")
    master_repo = os.environ.get("MASTER_REPO")
    master_token = os.environ.get("MASTER_TOKEN")
    worker_pool_str = os.environ.get("WORKER_POOL", "[]")
    current_worker = os.environ.get("GITHUB_REPOSITORY_OWNER")
    current_attempt = int(os.environ.get("FALLBACK_ATTEMPT", "0") or 0)

    payload = {
        "event_type": "child_failed",
        "client_payload": {
            "failed_link": rg_link,
            "failed_worker": current_worker,
            "worker_pool": worker_pool_str,
            "fallback_attempt": str(current_attempt),
            "rg_email": rg_email,
            "rg_password": rg_password,
            "hf_token": hf_token,
            "repo_id": repo_id,
            "repo_type": repo_type,
        },
    }

    # Target list, in order of preference:
    #   1. the Master the frontend told us about (MASTER_OWNER/MASTER_REPO + MASTER_TOKEN)
    #   2. this very repo, using the workflow's own GITHUB_TOKEN
    # (2) exists because this repo is a FORK of the Master repo, and a PAT that
    # belongs to the fork owner has no write access on the upstream - GitHub
    # answers 404 and the link used to be dropped. Every fork also carries
    # master.yml, so dispatching into ourselves still reassigns the link.
    targets = []

    # Which token can dispatch into the Master repo? Don't guess by name or by
    # index - ask GitHub: probe every pool token (plus MASTER_TOKEN) and take the
    # first one that really has push access. Switch the Master to another
    # account and this keeps working, as long as that account's token is in the
    # pool.
    if master_owner and master_repo:
        candidates = _pool_tokens()
        if master_token and master_token not in candidates:
            candidates.append(master_token)

        chosen = None
        for token in candidates:
            if _token_can_push(master_owner, master_repo, token):
                chosen = token
                break

        if chosen:
            targets.append((master_owner, master_repo, chosen,
                            f"pool token with write access to {master_owner}/{master_repo}"))
        else:
            print(f"[!] None of the {len(candidates)} known token(s) can write to "
                  f"{master_owner}/{master_repo}.")
            print("[!] Add the Master owner's PAT to WORKER_POOL / MASTER_TOKEN.")
    else:
        print("[!] MASTER_OWNER/MASTER_REPO not supplied by the frontend.")

    self_repo = os.environ.get("GITHUB_REPOSITORY", "")
    self_owner, _, self_name = self_repo.partition("/")
    gh_token = os.environ.get("GITHUB_TOKEN")
    if self_owner and self_name and gh_token:
        if (self_owner, self_name) != (master_owner, master_repo):
            targets.append((self_owner, self_name, gh_token, "this repo (fallback)"))

    if not targets:
        print("[!] No usable dispatch target. Cannot hand off.")
        return False

    for owner, repo, token, label in targets:
        ok, status, body = _dispatch_once(owner, repo, token, payload)
        if ok:
            print(f"[*] Handoff delivered to {owner}/{repo} ({label}).")
            return True

        print(f"[!] {label} dispatch to {owner}/{repo} failed: HTTP {status} {body}")
        if status == 404:
            print("[!] 404 = that token cannot see/write this repo. A PAT from a fork")
            print("    owner has no rights on the upstream Master repo; a fine-grained")
            print("    PAT must also list the repo with 'Contents: read and write'.")
        elif status == 401:
            print("[!] 401 = MASTER_TOKEN invalid or expired.")
        elif status == 403:
            print("[!] 403 = token lacks 'contents: write' on that repo.")
        elif status == 0:
            print("[!] Network error while reaching api.github.com.")

    return False


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    if not rg_link:
        print("[!] No link provided. Exiting.")
        return

    os.makedirs(OUTPUT_PATH, exist_ok=True)
    print(f"[*] Processing Link: {rg_link}")

    file_id = extract_file_id(rg_link)
    if not file_id:
        raise PermanentError("Could not extract the 32-char File ID from the link.")

    token = rapidgator_login(rg_email, rg_password)

    print(f"[*] Fetching file info for ID: {file_id}")
    filename, _info = file_info(file_id, token)
    print(f"[*] Found file: {filename}")

    print("[*] Generating premium download URL...")
    download_url = file_download_url(file_id, token)

    local_path = aria2_download(download_url, OUTPUT_PATH, filename)
    size_mb = os.path.getsize(local_path) / 1048576
    print(f"[*] Downloaded {size_mb:.1f} MB")

    upload_to_hf(local_path, filename)
    print("[*] Transfer Complete!")


if __name__ == "__main__":
    try:
        main()
        sys.exit(0)
    except PermanentError as exc:
        print(f"[x] PERMANENT FAILURE: {exc}")
        print("[x] Not retrying and not handing off (would just loop).")
        sys.exit(2)
    except RetryableError as exc:
        print(f"[!] NETWORK/BLOCK FAILURE: {exc}")
        sys.exit(42 if trigger_master_handoff(str(exc)) else 1)
    except Exception as exc:  # anything unexpected
        print(f"[!] UNEXPECTED FAILURE: {type(exc).__name__}: {exc}")
        sys.exit(42 if trigger_master_handoff(f"{type(exc).__name__}: {exc}") else 1)
