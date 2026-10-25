# Engine patches

Files here replace the same paths in `engine/vendor` (patched Cyberdrop-DL 10.10.0).
The build script copies them over the vendor tree. Each change is marked `NEXDROP patch`.

- `cyberdrop_dl/crawlers/crawler.py`: imports `aio` and `env`. Both were used at run time without
  an import, so `new_task_group()` raised `NameError: name 'aio' is not defined` for Turbo, GoonBox
  albums, Kemono, Kick, SpankBang, Pornpics, Koofr and the forum crawler, and debug crawlers could
  not register.
- `cyberdrop_dl/crawlers/telegram.py` (new file): public Telegram channels and posts through
  t.me's web preview. Files the preview leaves out are logged as `Telegram needs sign-in`, which
  the bridge turns into Skipped rows. History is kept per message link, because Telegram's CDN
  links change on every visit. A channel link found on another page is not followed.
- `cyberdrop_dl/crawlers/monotirou.py` (new file): Monotirou profile, video, search (`/?q=`) and tag
  (`/tags/<tag>`) links. Downloads use the media URL from the video API. Profiles page with the API's
  timestamp-and-ID cursor, searches and tags with `/api/videos/search` offsets; a repeated page stops
  with an error instead of looping.
- `cyberdrop_dl/crawlers/goonbox.py`: old Chevereto direct links on the main host (`goonbox.cr/images/<file>`)
  are downloaded as files, so a dead link fails with the server's answer instead of "Unknown URL path".
- `cyberdrop_dl/crawlers/pixeldrain.py`: a file Pixeldrain locks behind a captcha
  (`file_rate_limited_captcha_required`) fails up front with a clear reason when no API key is set,
  instead of downloading into a 403.
- `cyberdrop_dl/crawlers/tiktok.py`: a post without a music track no longer fails the whole
  profile with `ValueError('Empty URL')`.
  Post sounds on the US signed host (`sf16-sign.tiktokcdn-us.com`, refused outside the US with 403
  "ForbiddenByWAF") are fetched from TikTok's global music CDN (`sf16-ies-music.tiktokcdn.com`), same `/obj/` path.

- `cyberdrop_dl/progress/__init__.py` and `cyberdrop_dl/clients/downloads.py`: `ProgressHook` has an optional
  `resume` callback. A resumed download reports the bytes already on disk through it, so the bridge moves
  the file's bar without counting those bytes as newly downloaded or in the speed.

- `cyberdrop_dl/source_order.py`, `cyberdrop_dl/url_objects.py` and `cyberdrop_dl/crawlers/crawler.py`: album filenames
  (`ALBUM_NAMES`, the "Name files after the album" setting). `setup_as_album` keeps the album's folder title, each
  `create_child` gets its place on the album page, and `handle_file` names the file `<album name> 001.ext` (counted per album
  folder, one URL keeps its number, no `001_` prefix on top). `create_title` remembers the album name without the
  ` (Bunkr)` / album ID suffix. Thumbnails and single files keep their names.
- `POST_FOLDERS` ("Folder for each post"): when off, `create_separate_post_title` returns no folder, even on crawlers that force
  post folders (X, Bluesky, Patreon, TikTok...). Crawlers whose post title is the gallery itself (`POST_TITLE_IS_ALBUM`:
  `girlsreleased.py`, `fsiblog.py`, `nsfw_xxx.py`, `wordpress/__init__.py`) keep the plain title.

Engine behaviour changed at run time from `engine/bridge.py` instead (no file changes):
16 MB database reserve instead of 250 MB, FlareSolverr retry for downloads that hit a bot-check
page, live per-file speed.
- `cyberdrop_dl/crawlers/tiktok.py`: the post's background sound (.mp3) is only saved when
  `NEXDROP_TIKTOK_AUDIO=1` (the TikTok sounds setting). The bridge sets it for each run.

Also at run time from `engine/bridge.py`: the one-download-per-server lock (Bunkr and similar)
allows N connections per server (default 3) and falls back to 1 for a server that answers 429/503.

2.4.1:
- `cyberdrop_dl/url_objects.py`: with `ALBUM_NAMES` on, `setup_as_album` names the album's folder with the album name
  (`album_name`), the same name its files get, instead of `<title> <id> (<Site>)`. Profiles, threads and posts are unchanged.
- `cyberdrop_dl/clients/downloads.py`: `DownloadClient.__repr__` named a missing `_supports_ranges` attribute, so every
  traceback showed a repr-error for the download client. It names `SUPPORTS_RANGES`.
- `cyberdrop_dl/crawlers/fileditch.py` (new in this folder): signed-path download links (`walk10ksteps.xyz/<base64 of
  "<expiry>:<signature>:<id>/<path>">`) are accepted, and the file keeps the name from the Fileditch link.
- `cyberdrop_dl/crawlers/pornhub.py` (new in this folder): a profile page whose header has none of the name selectors
  (regional mirrors such as jp.pornhub.com) uses the name from the link instead of failing with 422.

Also at run time from `engine/bridge.py` (2.4.1): every request passes `HTTPClient.raw_request` through a guard. Old Coomer
and Kemono hosts (`*.coomer.party`, `coomer.su`, `*.kemono.party`, `kemono.su`) are sent to `coomer.st` / `kemono.cr`.
SimpCity requests are paced (2 at once, `simpcityGap` seconds apart), a 429 or a 503 with Retry-After pauses the site and
retries, and after 5 limits in a row the run stops contacting SimpCity. With `proxyScope: "simpcity"` only SimpCity requests
(and their FlareSolverr requests) use the proxy. SimpCity cookies are saved to `appdata/cookies/session/simpcity.txt`
after a run and loaded next run where the user's cookie file doesn't set the same cookie.

2.4.2:
- `cyberdrop_dl/crawlers/kemono/kemono_coomer.py` (new file): Kemono (`kemono.cr`) and Coomer (`coomer.st`) crawlers on the
  shared Kemono code. 10.10.0 ships the base class but no crawler for either site, so creator, post, search and favorites
  links were "Unsupported URL". API requests send `Accept: text/css` (the sites answer 403 otherwise) at 2 a second. The full
  creator list is not fetched at start (tens of MB on Coomer); names come from each creator's profile. Old hosts
  (`*.coomer.party`, `coomer.su`, `*.kemono.party`, `kemono.su`) are moved to the main domain.
