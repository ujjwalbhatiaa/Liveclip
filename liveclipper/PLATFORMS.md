# Where clips can go (research, checked 19 Nov 2026)

Status: YouTube, Facebook, Instagram and TikTok upload are all BUILT (publisher.py, platforms.py, Upload tab). None is tested against the real services yet:
YouTube was tested against a mocked HTTP layer, the other three against a fake HTTP layer that checks the request shapes against the docs linked below.
Twitch has no upload at all.

## YouTube (built)
- Official YouTube Data API v3, `videos.insert`, scope `youtube.upload`. You need your own Google Cloud project, the API enabled,
  an OAuth consent screen (add yourself as a test user) and a "Desktop app" OAuth client saved as
  `~/.config/liveclipper/client_secret.json`. The login token is stored in `~/.config/liveclipper/youtube_token.json` (mode 600).
- Default quota: 100 `videos.insert` calls a day; more needs a Google compliance audit.
  Source: https://developers.google.com/youtube/data/guides/quota_and_compliance_audits
- Older rule: uploads from unaudited API projects were locked to private. Google's current docs do not mention it. Check your first upload.
- Not proven against the real API from our side: the upload code was tested against a mocked HTTP layer only.

## TikTok (built, untested live)
- Content Posting API. Two modes: Upload (video lands in the user's TikTok inbox, user taps Post) and Direct Post (posts straight to the profile).
- You need a TikTok developer app and the `video.publish` (Direct Post) or `video.upload` scope.
- Unaudited apps: Direct Post is limited to private ("only me") posts, max 5 users per 24 h, account must be private when posting.
  Public posting requires a TikTok audit, and the guidelines say apps must not be limited to private/internal use, so a personal tool may not pass.
  Source: https://developers.tiktok.com/doc/content-sharing-guidelines
- Realistic route for one person: build the Upload mode (clip goes to your TikTok inbox, you finish and post it in the app). Effort: about a day, plus waiting for developer app approval.

## Instagram Reels (built, untested live)
- Instagram Graph API content publishing. Needs a Professional (Business/Creator) Instagram account, a Meta developer app, and the
  `instagram_content_publish` permission. Your own account works in development mode without app review; other people's accounts need review.
- The video must be at a public URL at publish time (Meta fetches it) or use the resumable upload for apps using Facebook Login for Business.
  So a local-only tool needs a temporary public host (a tunnel or a bucket). That is the awkward part.
- Limit: 100 API-published posts per 24 h.
  Source: https://developers.facebook.com/docs/instagram-platform/content-publishing/
- Effort: 1 to 2 days, plus Meta app setup and a tunnel/host decision.

## Facebook Reels (built, untested live)
- Reels Publishing API on a Facebook **Page** (not a personal profile). Permissions `pages_show_list`, `pages_read_engagement`, `pages_manage_posts`,
  Page access token from someone with CREATE_CONTENT on the Page. Three calls: start upload session, upload to rupload.facebook.com (a local file can be uploaded directly), publish.
- 9:16, limit 30 API-published reels per 24 h. Your own Page works in development mode; others need app review.
  Source: https://developers.facebook.com/docs/video-api/guides/reels-publishing/
- Effort: about a day. Easier than Instagram because the file can be pushed directly.

## Twitch
There is no API to upload videos to Twitch. Twitch clips are made from a live stream, so liveclipper cannot publish anywhere on Twitch. It can read Twitch streams and VODs as a source.


## What the code does and what you must set up (all keys are saved only on this computer, ~/.config/liveclipper/platforms.json, mode 600)

### Facebook (platforms.facebook_upload)
Calls: POST /{page}/video_reels upload_phase=start, POST the file to rupload.facebook.com, POST upload_phase=finish video_state=PUBLISHED.
Setup: a Meta developer app (developers.facebook.com), a Facebook Page you manage, a Page access token with pages_show_list,
pages_read_engagement, pages_manage_posts. Easiest in development mode: Graph API Explorer > pick your app > get a User token with those
permissions > exchange it for a long-lived token > GET /me/accounts gives the Page ID and a Page token. Paste Page ID + Page token in the Upload tab.
Your own Page works without app review while the app is in development mode. Reels are public on a Page. Reel length 3-90 s, 9:16.

### Instagram (platforms.instagram_upload)
Calls: POST /{ig}/media (REELS, upload_type=resumable), POST the file to the returned rupload URI, poll status_code until FINISHED, POST /{ig}/media_publish.
Setup: a Professional (Business/Creator) Instagram account linked to a Facebook Page, same Meta app, permissions instagram_basic and
instagram_content_publish, token as above, and the Instagram account ID (GET /{page}?fields=instagram_business_account).
RISK: Meta's doc says the resumable upload is only for apps using Facebook Login for Business. If Instagram rejects the resumable upload, the fallback is
publishing from a public video_url (needs a public host or tunnel); that fallback is NOT built.

### TikTok (platforms.tiktok_upload)
Calls: OAuth (PKCE, browser login on this computer, redirect http://127.0.0.1:8766/callback/ (fixed port)), creator_info/query, video/init (FILE_UPLOAD),
chunked PUT, status/fetch. Inbox mode uses inbox/video/init instead and the clip lands in your TikTok app to finish.
Setup: TikTok for Developers app with Login Kit and Content Posting API, scopes video.publish and video.upload, redirect URI registered EXACTLY as
http://127.0.0.1:8766/callback/ (with the trailing slash; TikTok matches it exactly, so the port is fixed at 8766), client key + secret pasted in the Upload tab, then "Log in to TikTok". Unaudited apps can only post private (SELF_ONLY).
Public posting needs a TikTok audit. Tokens refresh themselves (refresh token valid 365 days).
