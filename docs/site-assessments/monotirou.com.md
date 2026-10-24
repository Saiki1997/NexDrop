# monotirou.com: site assessment

| | |
|---|---|
| State | **Unsupported** |
| Assessed | 2026-10-24, reassessed the same day (see *Reassessment 1*) |
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

- Uploads are anonymous. Nothing on the site identifies uploaders or verifies the age or consent of the people shown, and it publishes no record-keeping statement. (Its reporting and removal pages are covered under *Reassessment 1*.)
- Listings on the home page point to material involving minors and to recordings shared without the consent of the people in them.

Some items on the site may well be lawful. But nothing on it tells lawful items apart from the rest, so no item can be shown to be eligible. Blocking single items doesn't help either: NEXDROP can't see what a video shows. Titles, tags and uploader names are not a reliable signal, and screening by keywords would miss material while giving false confidence. Hash matching against known abuse material (for example PhotoDNA or similar programmes) is only open to vetted organisations, so a desktop downloader can't do it.

## Result

**Unsupported.** Pasted links to the site or its subdomains are refused with the reason above. Links to it found on other pages are skipped before any request is made. Media files hosted there and embedded elsewhere are skipped too. Every refusal is logged to `engine\appdata\logs\blocked.jsonl`. You can't override this from Settings > Sites. It changes only through a new assessment recorded in this file.

## Reassessment 1 (2026-10-24)

Prompted by the site's article "How adult platforms handle trust and safety". These pages were read: the article, `/terms`, `/complaints`, `/content-removal`, `/dmca` and `/contact`.

**What the site does publish (verified on its pages):**
- **Terms of service.** Users must be 18 or the age of majority. Uploads must not be "illegal, non-consensual, copyrighted, underage, private, or exploitative". The site "may remove content, restrict accounts, preserve evidence, or report suspected illegal material".
- **Complaints.** A support email, plus a separate email for urgent abuse and illegal-content reports. Reports should include the content URL, the reason and supporting details.
- **Content removal.** An email route for suspected underage content, non-consensual content, private information, impersonation, illegal material and copyright.
- **DMCA.** A list of what a notice must contain and a "Legal" email. Repeat infringers "may be restricted or removed".

**What it doesn't publish:**
- Any check of uploaders' identity, or of the age or consent of the people shown, and any age or consent records (2257 or similar).
- Who runs the site: no legal name, address or jurisdiction.
- Response times, how reports are reviewed, what happens to reported content, or referral to authorities.
- A designated DMCA agent or a counter-notice procedure.
- Any rule on downloading or automated access. The terms are silent on it.

**The article itself** discusses "adult platforms" in general and offers consulting. It makes no commitments about how monotirou.com itself operates, so it isn't evidence of the site's practice.

**Result: still unsupported.**
- *Rules and reporting.* The terms forbid the right things, and there are reporting addresses. That partly meets requirement 2 below.
- *Verification.* Rules that rely on uploaders' self-declaration don't verify anyone. Requirement 1 (verified age and consent of the people shown) isn't met.
- *Listings.* Nothing published changes what the home page listings pointed to. Requirement 3 isn't met.
- *Profiles.* Per-uploader pages (`/users/<name>`) were not examined or parsed. A profile parser would catalogue the same content, so it waits on the same requirements.

## What would change this

A reassessment is due when all of the following are true, and can be checked on the site:

1. Uploaders are identity-verified, and the site keeps age and consent records for everyone shown (for example under 18 U.S.C. 2257), with a public statement saying so.
2. There is a working report and takedown process for non-consensual and underage material, with a named operator, response times and referral to authorities. *(Partly met: reporting emails exist; the operator, timelines and handling aren't published.)*
3. Its listings no longer point to minors or to material shared without consent.

If these are met, the site would go to **review**. A page test (Settings > Sites > Test page) would then confirm the generic handler finds the media. **Supported** would follow only for content the user is authorized to download.
