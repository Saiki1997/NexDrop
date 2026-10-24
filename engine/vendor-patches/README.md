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
- `cyberdrop_dl/crawlers/tiktok.py`: a post without a music track no longer fails the whole
  profile with `ValueError('Empty URL')`.

Engine behaviour changed at run time from `engine/bridge.py` instead (no file changes):
16 MB database reserve instead of 250 MB, FlareSolverr retry for downloads that hit a bot-check
page, live per-file speed.
- `cyberdrop_dl/crawlers/tiktok.py`: the post's background sound (.mp3) is only saved when
  `NEXDROP_TIKTOK_AUDIO=1` (the TikTok sounds setting). The bridge sets it for each run.

Also at run time from `engine/bridge.py`: the one-download-per-server lock (Bunkr and similar)
allows N connections per server (default 3) and falls back to 1 for a server that answers 429/503.
