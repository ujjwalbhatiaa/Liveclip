#!/usr/bin/env python3
"""yt_upload.py - upload a reviewed clip to YOUR OWN YouTube channel via the official YouTube Data API.

Reads a *_upload.json written by liveclipper_v4. Safe by default:
  * dry-run unless you pass --confirm
  * always uploads as PRIVATE unless you pass --privacy public|unlisted
  * the OAuth token (a long-lived refresh token = access to your channel uploads) is stored only in
    ~/.config/liveclipper/youtube_token.json, created owner-only (0600); never inside the clips folder or a git repo
  * refuses unless the file says "rights_cleared": true (set it yourself, only after you have
    permission from the streamer / own the content)
Setup (once): pip install google-api-python-client google-auth-oauthlib
  Create an OAuth "Desktop app" client in Google Cloud Console, enable YouTube Data API v3,
  download client_secret.json (keep it out of git). First run opens a browser to log in.
Usage:
  python3 yt_upload.py clips/xxx_upload.json                 # dry run: prints what would happen
  python3 yt_upload.py clips/xxx_upload.json --confirm       # uploads as private
"""
import argparse, json, os, sys
from pathlib import Path

DEFAULT_TOKEN = Path.home() / ".config" / "liveclipper" / "youtube_token.json"

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pack"); ap.add_argument("--confirm", action="store_true")
    ap.add_argument("--privacy", choices=["private", "unlisted", "public"], default="private")
    ap.add_argument("--secrets", default="client_secret.json"); ap.add_argument("--token", default=str(DEFAULT_TOKEN))
    a = ap.parse_args()
    pack = json.loads(Path(a.pack).read_text()); d = Path(a.pack).parent
    video = d / pack["video"]; thumb = d / pack["thumbnail"] if pack.get("thumbnail") else None
    if not video.exists(): sys.exit(f"video not found: {video}")
    if not pack.get("rights_cleared"): sys.exit('Refusing: set "rights_cleared": true in the pack only if you have the right to publish this clip.')
    if pack.get("review_required") and not pack.get("reviewed_title_thumbnail_hook"):
        sys.exit('Refusing: watch the clip and check the title, burned-in hook and thumbnail headline say something true about it, then set "reviewed_title_thumbnail_hook": true in the pack.')
    title = pack["title"][:100]
    body = {"snippet": {"title": title, "description": pack["description"][:5000], "tags": pack.get("tags", [])[:30], "categoryId": "20"},
            "status": {"privacyStatus": a.privacy, "selfDeclaredMadeForKids": False}}
    print(f"video={video}\ntitle={title}\nprivacy={a.privacy}\nthumbnail={thumb}")
    if not a.confirm:
        print("DRY RUN: nothing uploaded. Re-run with --confirm to upload."); return
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.oauth2.credentials import Credentials
    scopes = ["https://www.googleapis.com/auth/youtube.upload"]
    from google.auth.transport.requests import Request
    tp = Path(a.token).expanduser()
    creds = Credentials.from_authorized_user_file(str(tp), scopes) if tp.exists() else None
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    if not creds or not creds.valid:
        creds = InstalledAppFlow.from_client_secrets_file(a.secrets, scopes).run_local_server(port=0)
    tp.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(str(tp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)   # owner-only, set at creation
    with os.fdopen(fd, "w") as f:
        f.write(creds.to_json())
    os.chmod(tp, 0o600)
    yt = build("youtube", "v3", credentials=creds)
    req = yt.videos().insert(part="snippet,status", body=body, media_body=MediaFileUpload(str(video), chunksize=-1, resumable=True))
    resp = None
    while resp is None:
        st, resp = req.next_chunk()
        if st: print(f"uploaded {int(st.progress()*100)}%")
    print("done: https://youtu.be/" + resp["id"])
    if thumb and thumb.exists():
        try: yt.thumbnails().set(videoId=resp["id"], media_body=MediaFileUpload(str(thumb))).execute(); print("thumbnail set")
        except Exception as e: print("thumbnail not set (channel may need verification):", e)

if __name__ == "__main__":
    main()
