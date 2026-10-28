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

2.4.3:
- `cyberdrop_dl/filepath.py` (new in this folder): `get_filename_and_ext` accepts a short list of real extensions longer than
  5 characters (`.blend`, `.blend1`, `.unitypackage`, `.safetensors`, `.procreate`, `.aseprite`, `.torrent`, `.sketch`)
  instead of raising `InvalidExtensionError`.

Also at run time from `engine/bridge.py` (2.4.3): downloads are counted per site (`n3.coomer.st` counts as `coomer.st`).
After 8 connection timeouts in a row with no successful download in between, the site's remaining downloads fail at once
with `DownloadError("Server unreachable")` instead of each waiting out the connect timeout.

Smart naming (2.5.0):
- `cyberdrop_dl/smart_naming.py` (new file): the one place that names folders and files. Crawlers only report metadata
  (album, thread, post title, caption, page title); `name_file` picks the title in the spec's priority order with a
  confidence score, keeps meaningful original names, leaves hashes/ids/camera names (`a9c82fa8`, `IMG_8403`) out, adds the
  source-order number (`name-001.ext` by default) and makes the name Windows-safe. `FolderRegistry` keeps two collections with
  the same title apart (`allecakes`, `allecakes (2)`) and writes the assignment to `appdata/folder-registry.jsonl`, so retries,
  restarts and resumes land in the same folder. `note_page` keeps the Open Graph / Twitter card / JSON-LD / `<title>` of pages
  the crawler already fetched (no extra requests). Default settings are the old behaviour; the bridge turns the new ones on.
- `cyberdrop_dl/source_order.py`: `NUMBER_MODE` (suffix, prefix, off), `split_name` (keeps `.tar.gz`), and job-scoped numbers:
  `reset(manifest, job)` starts each job at 1 and reuses only that job's rows (`stored()` returns the saved name).
- `cyberdrop_dl/url_objects.py`: `ScrapeItem.naming` (album, thread, profile, post title, caption) and `folder_meta` (kind and
  owner of each folder). `append_folders` reads them from the folder titles and from `PostTitle`.
- `cyberdrop_dl/crawlers/crawler.py`: `handle_file` asks `smart_naming.name_file` for the name; `_prepare_download_path` builds
  content-title folders without the site suffix (no `Loose Files` folder since V2); `create_title` records
  the folder's site and album/thread id; `create_separate_post_title` returns a `PostTitle` that also carries the post's title
  and an optional `caption=`. The "Source order" log line carries the naming record for the job database.
- `cyberdrop_dl/crawlers/_forum.py`, `twitter/crawler.py`, `bluesky/crawler.py`: pass the post's first line / tweet text /
  post text as `caption=`.
- `cyberdrop_dl/clients/response.py`: `soup()` hands each parsed page to `smart_naming.note_page`.
- `cyberdrop_dl/clients/downloads.py`, `database/history.py`: a URL with an unfinished `.part` file keeps the folder and name
  it was started with (`get_unfinished_location`), so changed naming rules never restart or orphan a half-downloaded file.

Smart naming V2 (2.5.0):
- `cyberdrop_dl/smart_naming.py`: every candidate title (original filename, the file's own title/caption/page, the post's
  caption or title, album, thread, creator, link text) gets a score = how much that kind of metadata says about one file x
  what the text is worth. Hashes, generic words, site names, file-host taglines, adverts, bot-check/error pages, download
  instructions, forum thank-you/credit replies and questions score 0. The best score wins at 0.5 or more; otherwise the
  original name stays. A linking post's own title outranks a file host's album name. Captions lose lead-ins ("Here is my
  new ..."), calls to action ("Check my profile ..."), trailing thanks, picture counts and hashtag runs. A host's random id
  is taken off a real filename ("Beach Sunset-aB3dE9fG" -> "Beach Sunset"). `explain_title` returns every candidate with
  its verdict; `REPORT` writes them to the run's `.naming.log` when "Explain file names" is on; `CORRECTIONS` applies names
  you changed in History, only for the same post, album or thread; `embedded_name` names files nothing else could name from
  the title stored inside them. `safe_url` keeps scheme, host, port and path only.
- `cyberdrop_dl/media_tags.py` (new file): reads a title from MP4/MOV/M4A (`©nam`), Matroska/WebM (`Title`), MP3 (`TIT2`),
  FLAC (`TITLE`), JPEG (EXIF XPTitle/ImageDescription, XMP dc:title), PNG (`tEXt Title`) and the single top folder of a zip.
  Bounded reads, nothing extracted or decoded; encoder and camera names are ignored.
- `cyberdrop_dl/url_objects.py`: `naming` also records `post_url` and the owner keys of the post, album, thread and creator.
- `cyberdrop_dl/crawlers/crawler.py`: `handle_file(..., media_title=, media_caption=)` for what a site says about one file;
  the "Source order" line ends with the file's identity as JSON (links without query strings).
- `cyberdrop_dl/crawlers/_chevereto.py` (ImgBB and other Chevereto hosts), `imgur.py`, `flickr.py`, `redgifs.py`: pass each
  image's or gif's own title and description.
- Folder for each title (`titleFolders`, on by default): `title_folder` gives a decision's `group`, a folder named after the
  caption, post, album or thread title the file was named from ("Leaked set pics-193.jpg" -> "Leaked set pics/"), unless
  the file already sits in a folder with that name. `handle_file` saves the file there.
- No more `Loose Files` folders: `_prepare_download_path` puts a file linked on its own in the folder of the page that
  linked it (or the download folder). The desktop app's plain-HTTP fallback does the same.

3.0 (download integrity and speed):
- `cyberdrop_dl/clients/downloads.py` and `cyberdrop_dl/clients/resume_validators.py` (new): a resumed download only appends
  bytes of the same file. Beside each `.part` file a `.part.nxmeta` sidecar keeps the link (no query string), ETag,
  Last-Modified and full size from the answer that started it. A resume sends `If-Range`; a `206` must start at the resume
  point, report the same size and ETag, or the partial file is deleted and the file starts again. A `200` to a range request
  no longer counts the deleted bytes as progress. `416` restarts the file instead of failing it. The finished file must have
  exactly the announced size (not "at least"), and an image, video or audio file whose first bytes are an HTML page fails
  with "Got a web page instead of the file". A compressed answer (Content-Encoding) is not size-checked.
- `cyberdrop_dl/clients/http.py`: `check_http_status` marks 408, 425, 429, 500, 502, 503, 504 and 520-524 as retryable and
  attaches the server's `Retry-After` (seconds or HTTP date). Before, every HTTP error was final inside the run.
- `cyberdrop_dl/downloader/http.py`: a retry waits first: the server's Retry-After (at most 60 s), else 2 s, 4 s, 8 s ... up
  to 20 s with ±25% jitter. Before, the retry was sent at once.
- `cyberdrop_dl/hasher.py`: xxh128, MD5 and SHA-256 come from one read of the file instead of one full read each.
- `cyberdrop_dl/models/__init__.py`: no `time.sleep(2)` after a deprecated-option warning. With the bundled config's old
  `ssl_context` line this cost 4 s on every engine start (the config is now version 2 without it, and the bridge replaces an
  older installed copy).

3.0 (Smart Naming 3.0):
- `cyberdrop_dl/smart_naming.py`: optional filename templates (`nameTemplate`, for example `{creator}-{title}-{number}`).
  Variables: title (the name smart naming chose), original, album, thread, post, creator (profile name), date (upload
  date, UTC), site, type, number, ext. A variable the crawl did not provide drops out with the separator in front of it;
  when every name variable is empty the automatic name is used. Only `{title}` is shortened to fit the length limit.
  A template with an unknown variable, a stray brace or a character Windows refuses is ignored and the run log says why.
  `numberWidth` sets 3, 4 or 5 digits. The fingerprint that decides whether a resumed job keeps its names only changes when
  these settings are not at their defaults, so 2.5 jobs resume with the same names.
- `cyberdrop_dl/source_order.py`: `NUMBER_WIDTH` and `padded()`; prefix, suffix and album numbering use them.
- `cyberdrop_dl/crawlers/crawler.py`: passes the file's upload date to `name_file` for `{date}`.

- `cyberdrop_dl/clients/segments.py` (new, 3.1) and `cyberdrop_dl/clients/downloads.py`: multi-segment downloads. A file of
  `MIN_SIZE` (64 MB) or more whose server sends `Accept-Ranges: bytes` and a strong ETag or Last-Modified, uncompressed and not
  impersonated, downloads in up to `MAX_PARTS` parts at once (set by the bridge from the download mode). The first answer fills
  part 1 in the `.part` file; other parts are `If-Range` requests into `.part.segN`, each checked for exactly its range, size and
  validators. The layout is kept in the `.nxmeta` sidecar so each part resumes from its own size. A wrong answer deletes the parts,
  marks the link "one stream" for the run and retries it whole. Hosts that asked to slow down get one stream.
- `cyberdrop_dl/clients/downloads.py` (3.1): a file a size rule skips before any byte is written no longer leaves its
  `.part.nxmeta` sidecar behind (`resume_validators.forget`).

3.1.2:
- `cyberdrop_dl/clients/downloads.py`: the resume sidecar (`.part.nxmeta`) is written after the "already downloaded"
  checks, beside the `.part` file the download really uses; a skipped file removes a sidecar an older version left.
- `cyberdrop_dl/smart_naming.py`: `context_folder()` gives a post without a usable title a `Post <id>` folder;
  `clean_title()` drops download prompts, `@@`, inner file extensions and sizes; more junk titles ("discussion thread");
  `embedded_name()` takes dates out like the other names.

3.1.1:
- `cyberdrop_dl/name_dates.py` (new): `strip_dates()` removes whole calendar dates from a name.
- `cyberdrop_dl/smart_naming.py`: "Take dates out of names" (`stripDates`, applied to the title before numbering and to
  title folders; part of the fingerprint as `.nd` so names kept from an older run are made again) and "Folder for each post
  or album" (`contextFolders`, `context_folder()`: the post title, caption or album title of a file that would otherwise
  get no folder, when it scores as a real title).
