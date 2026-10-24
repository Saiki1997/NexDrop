# Sites in NEXDROP

## How a link is handled

1. **Site policy** (`desktop/site-policy.json`, plus your sites in Settings > Sites):
   - **Supported**: downloads.
   - **Review required**: one of your sites that hasn't been confirmed yet. **Test page** reads one page and reports what it would download, without downloading anything. Pasted links are refused, and links to the site found on other pages are skipped.
   - **Unsupported**: NEXDROP assessed the site and it doesn't meet the legal, safety or technical requirements. The reason and the date are in the policy file.

   Sites in neither list keep the engine's normal behaviour. Every refused or skipped link is written to `engine\appdata\logs\blocked.jsonl`.
2. **Built-in handlers**: the bundled Cyberdrop-DL engine has about 400, for example Erome (`/a/<album>`, `/<profile>`, `/search?q=`), Bunkr, GoFile, Pixeldrain, MEGA, SimpCity and other XenForo forums, TikTok and Telegram (`t.me/<channel>`, `t.me/<channel>/<post>`; `t.me/c/<id>` with the Telegram sign-in). Settings > Sites > Check sites lists them all.
3. **Your sites**: each supported site gets one of the engine's generic handlers:

   | Kind | Reads |
   | --- | --- |
   | Media pages | JSON-LD `contentUrl`, `<video>`/`<audio>`/`<source>`, `og:video`/`og:audio`, links to media files, images inside gallery or album containers, `og:image` when nothing else is found. HLS (`.m3u8`) through the engine's HLS downloader. Follows `rel="next"` (or your selector) from the page you paste; with an item selector, opens item pages from listing pages. Same host only, and pages it opens are not paginated again. |
   | Video pages | one `<video>` per page (the engine's GenericVideoCrawler, iframes included) |
   | KVS, Chevereto, WordPress (posts or media library), Discourse | the engine's handlers for those platforms |

4. **Anything else** is tried as a direct file over HTTP (up to 80 MB). Private and local addresses are refused, including after redirects.

Everything above goes through the same pipeline: numbered in page order, many files at once with per-site limits, retries and the automatic retry pass, Pause and Resume, history (already-saved files are skipped), file-type filters, and per-site cookie files for pages behind your own login.

## What NEXDROP doesn't do

- Get around sign-ins, paywalls, CAPTCHAs (beyond the FlareSolverr bot check the engine already uses), DRM or Media Source (`blob:`) players. DASH (`.mpd`) isn't downloaded. These are reported in Activity.
- Screen individual files by title or keywords. Titles are not a reliable signal, and NEXDROP can't see what a video shows. Content safety is decided per site, from what the site verifies about uploaders, age and consent .

## Adding a site

1. Settings > Sites: paste one page from the site and choose how it should be read. For listing pages, add the item-link selector.
2. Press **Test page**. NEXDROP reports the files and item pages it found, and notes such as DRM.
3. Press **Add site**. It starts in review.
4. Tick the confirmation that you own the content or are authorized to download it, and that it shows no one under 18 or anyone who didn't consent. Then press **Turn on downloads**.

**Turn off downloads** puts the site back in review. A site NEXDROP assessed as unsupported can't be added.
