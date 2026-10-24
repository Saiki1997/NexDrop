# monotirou.com: site assessment

| | |
|---|---|
| State | **Unsupported** |
| Assessed | 2026-10-24 |
| Policy entry | `desktop/site-policy.json` |
| Reassess when | the requirements in *What would change this* are met |

## What was checked

On 2026-10-24 the public home page was checked once, by reading its HTML. No video page or media was downloaded.

## Technical compatibility

On its own, the site would fit NEXDROP's generic media handler:

- Video pages at `/video/<id>`, plus listing feeds (`/new`, `/trending`, `/top-rated`, `/popular`) with a "load more" control.
- Pages and media come from the site's own domain; no separate CDN was visible on the home page.
- It has accounts and uploads (`/upload`, `/login`). No public API was documented.

Technology is not what decides this site's state.

## Content eligibility

NEXDROP downloads only from sites where the content can reasonably be expected to be lawful, and where the operator, not NEXDROP, carries the age and consent checks. On this site:

- Uploads are anonymous. Nothing on the site identifies uploaders or verifies the age or consent of the people shown, and it publishes no record-keeping or takedown statement.
- Listings on the home page point to material involving minors and to recordings shared without the consent of the people in them.

Some items on the site may well be lawful. But nothing on it tells lawful items apart from the rest, so no item can be shown to be eligible. Blocking single items doesn't help either: NEXDROP can't see what a video shows. Titles, tags and uploader names are not a reliable signal, and screening by keywords would miss material while giving false confidence. Hash matching against known abuse material (for example PhotoDNA or similar programmes) is only open to vetted organisations, so a desktop downloader can't do it.

## Result

**Unsupported.** Pasted links to the site or its subdomains are refused with the reason above. Links to it found on other pages are skipped before any request is made. Media files hosted there and embedded elsewhere are skipped too. Every refusal is logged to `engine\appdata\logs\blocked.jsonl`. You can't override this from Settings > Sites. It changes only through a new assessment recorded in this file.

## What would change this

A reassessment is due when all of the following are true, and can be checked on the site:

1. Uploaders are identity-verified, and the site keeps age and consent records for everyone shown (for example under 18 U.S.C. 2257), with a public statement saying so.
2. There is a working report and takedown process for non-consensual and underage material, with a named contact.
3. Its listings no longer point to minors or to material shared without consent.

If these are met, the site would go to **review**. A page test (Settings > Sites > Test page) would then confirm the generic handler finds the media. **Supported** would follow only for content the user is authorized to download.
