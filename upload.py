#!/usr/bin/env python3
"""YouTube → TeraBox upload via GitHub Actions."""
import os, sys, json, subprocess, hashlib, glob, re, time
from urllib.parse import quote_plus

def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

# --- TeraBox config ---
NDUS = os.environ.get('TERABOX_NDUS', '')
NDUT_FMT = os.environ.get('TERABOX_NDUT_FMT', '')
CSRF = os.environ.get('TERABOX_CSRF', '')
BROWSERID = os.environ.get('TERABOX_BROWSERID', '')
JSTOKEN = os.environ.get('TERABOX_JSTOKEN', '')
REMOTE_DIR = os.environ.get('TERABOX_REMOTE_DIR', '/stream_videos')
UA = 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36'

if not NDUS:
    print("ERROR: TERABOX_NDUS not set")
    sys.exit(1)

COOKIES = {}
if NDUS: COOKIES['ndus'] = NDUS
if NDUT_FMT: COOKIES['ndut_fmt'] = NDUT_FMT
if CSRF: COOKIES['csrf'] = CSRF
if BROWSERID: COOKIES['browserid'] = BROWSERID

log(f"TeraBox: configured (ndus={NDUS[:8]}..., remote={REMOTE_DIR})")

# --- TeraBox API ---
import requests as req

def terabox_precreate(path, md5_list):
    data = {
        'app_id': '250528', 'web': '1', 'channel': 'dubox', 'clienttype': '0',
        'jsToken': JSTOKEN, 'path': path, 'autoinit': '1',
        'target_path': REMOTE_DIR, 'block_list': json.dumps(md5_list),
    }
    r = req.post('https://www.terabox.com/api/precreate',
                 headers={'User-Agent': UA, 'Origin': 'https://www.terabox.com',
                          'Referer': 'https://www.terabox.com/main',
                          'Content-Type': 'application/x-www-form-urlencoded'},
                 cookies=COOKIES, data=data, timeout=30)
    resp = r.json()
    if 'uploadid' in resp:
        return resp['uploadid']
    raise Exception(f"Precreate failed: {resp.get('errmsg', resp)}")

def terabox_upload_chunk(filepath, cloud_path, upload_id, md5, part_seq=0):
    cookie_str = '; '.join(f'{k}={v}' for k, v in COOKIES.items())
    upload_url = (
        f"https://c-jp.terabox.com/rest/2.0/pcs/superfile2?"
        f"method=upload&type=tmpfile&app_id=250528"
        f"&path={quote_plus(cloud_path)}&uploadid={upload_id}&partseq={part_seq}"
    )
    cmd = [
        'curl', '-s', '-X', 'POST',
        '-H', f'User-Agent:{UA}',
        '-H', 'Origin:https://www.terabox.com',
        '-H', 'Referer:https://www.terabox.com/main',
        '-H', 'Content-Type:multipart/form-data',
        '-b', cookie_str,
        '-F', f'file=@{filepath}',
        upload_url,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    resp = json.loads(proc.stdout)
    if 'error_code' in resp:
        raise Exception(f"Upload chunk failed: {resp}")
    return resp.get('md5', md5)

def terabox_create(cloud_path, upload_id, size, md5_list):
    params = {'isdir': '0', 'rtype': '1', 'app_id': '250528', 'jsToken': JSTOKEN}
    data = {
        'path': cloud_path, 'uploadid': upload_id,
        'target_path': REMOTE_DIR + '/',
        'size': str(size), 'block_list': json.dumps(md5_list),
    }
    r = req.post('https://www.terabox.com/api/create',
                 headers={'User-Agent': UA, 'Origin': 'https://www.terabox.com',
                          'Content-Type': 'application/x-www-form-urlencoded'},
                 cookies=COOKIES, params=params, data=data, timeout=30)
    return r.json()

def terabox_share(cloud_path):
    data = {
        'app_id': '250528', 'sids': '', 'channel': 'dubox', 'clienttype': '0',
        'jsToken': JSTOKEN,
        'period': '7', 'perm': '1',
        'pwd': '',
        'title': os.path.basename(cloud_path),
        'list': json.dumps([{'path': cloud_path, 'isdir': '0'}]),
    }
    r = req.post('https://www.terabox.com/share/set',
                 headers={'User-Agent': UA, 'Origin': 'https://www.terabox.com',
                          'Referer': 'https://www.terabox.com/main',
                          'Content-Type': 'application/x-www-form-urlencoded'},
                 cookies=COOKIES, data=data, timeout=30)
    resp = r.json()
    if resp.get('errno', -1) != 0:
        raise Exception(f"Share failed: {resp}")
    return resp.get('url', '') or resp.get('short_url', '')

# --- Main pipeline ---
def main():
    url = os.environ['URL']
    quality = os.environ.get('QUALITY', '720p')
    task_id = os.environ.get('TASK_ID', 'gh_' + hashlib.md5(url.encode()).hexdigest()[:8])
    CHUNK_SIZE = 120 * 1024 * 1024

    QUALITY_MAP = {
        '360p': 'bestvideo[height<=360]+bestaudio/best[height<=360]',
        '480p': 'bestvideo[height<=480]+bestaudio/best[height<=480]',
        '720p': 'bestvideo[height<=720]+bestaudio/best[height<=720]',
        '1080p': 'bestvideo[height<=1080]+bestaudio/best[height<=1080]',
        'best': 'bestvideo+bestaudio/best',
    }
    fmt = QUALITY_MAP.get(quality, QUALITY_MAP['720p'])

    env = os.environ.copy()

    # Step 1: Get video info
    log("Getting video info...")
    info_cmd = f'yt-dlp --js-runtimes node -j --no-warnings "{url}"'
    info_proc = subprocess.run(info_cmd, shell=True, capture_output=True, text=True, timeout=60, env=env)
    try:
        info = json.loads(info_proc.stdout.strip().split('\n')[0])
        title = info.get('title', 'Unknown')
        duration = info.get('duration', 0)
        log(f"Title: {title} ({duration//60}m)")
    except:
        title = 'Unknown'

    # Step 2: Download via yt-dlp
    output_path = f'/tmp/{task_id}.mp4'
    log(f"Downloading @ {quality}...")
    dl_cmd = (
        f'yt-dlp --js-runtimes node --socket-timeout 30 '
        f'-f "{fmt}" --merge-output-format mp4 --remux-video mp4 '
        f'--newline -o "{output_path}" --no-part "{url}"'
    )
    log(f"CMD: {dl_cmd[:200]}")
    proc = subprocess.Popen(dl_cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1, env=env)
    output_lines = []
    for line in proc.stdout:
        line = line.strip()
        output_lines.append(line)
        match = re.search(r'\[download\]\s+([\d.]+)%', line)
        if match:
            log(f"Download: {match.group(1)}%")
    proc.wait()

    if proc.returncode != 0 or not os.path.exists(output_path):
        tail = '\n'.join(output_lines[-20:])
        log(f"ERROR: Download failed (rc={proc.returncode})")
        log(f"yt-dlp output:\n{tail}")
        sys.exit(1)

    file_mb = round(os.path.getsize(output_path) / 1024 / 1024, 1)
    log(f"Download complete: {file_mb}MB")

    # Step 3: Hash local file
    log("Hashing chunks...")
    md5_list = []
    total_bytes = 0
    with open(output_path, 'rb') as f:
        while True:
            chunk = f.read(CHUNK_SIZE)
            if not chunk:
                break
            md5_list.append(hashlib.md5(chunk).hexdigest())
            total_bytes += len(chunk)

    log(f"{len(md5_list)} chunks, {total_bytes/1024/1024:.0f}MB total")

    # Step 4: Precreate on TeraBox
    cloud_path = f"{REMOTE_DIR}/{title[:80]}.mp4"
    log(f"Precreating on TeraBox...")
    try:
        upload_id = terabox_precreate(cloud_path, md5_list)
        log(f"upload_id={upload_id}")
    except Exception as e:
        log(f"ERROR: Precreate failed: {e}")
        sys.exit(1)

    # Step 5: Upload chunks
    log("Uploading to TeraBox...")
    uploaded = 0
    with open(output_path, 'rb') as f:
        for idx in range(len(md5_list)):
            chunk = f.read(CHUNK_SIZE)
            if not chunk:
                break

            chunk_path = f"/tmp/tb_chunk_{idx}"
            with open(chunk_path, 'wb') as cf:
                cf.write(chunk)

            log(f"Uploading chunk {idx+1}/{len(md5_list)}...")
            terabox_upload_chunk(chunk_path, cloud_path, upload_id, md5_list[idx], idx)
            os.remove(chunk_path)

            uploaded += len(chunk)
            pct = round(uploaded / total_bytes * 100, 1) if total_bytes else 0
            log(f"Progress: {pct}%")

    # Step 6: Finalize
    log("Finalizing on TeraBox...")
    try:
        create_resp = terabox_create(cloud_path, upload_id, total_bytes, md5_list)
        if create_resp.get('errno') != 0:
            raise Exception(f"create failed: {create_resp}")
    except Exception as e:
        log(f"ERROR: Create failed: {e}")
        sys.exit(1)

    # Step 7: Share
    log("Creating share link...")
    try:
        share_url = terabox_share(cloud_path)
        log(f"DONE! Share URL: {share_url}")
        result = {'share_url': share_url, 'title': title, 'size_mb': round(total_bytes/1024/1024, 1)}
        with open('/tmp/tb_result.json', 'w') as f:
            json.dump(result, f)
    except Exception as e:
        log(f"ERROR: Share failed: {e}")
        sys.exit(1)

    # Cleanup
    try: os.remove(output_path)
    except: pass

if __name__ == '__main__':
    main()
