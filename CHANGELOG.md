# Changelog

## v1.1, review fixes (October 26, 2026)

A review of v1 that tried to break it. Each fix below has a check in `tests/e2e_mock_test.py` that failed on v1 and passes now (61 checks across 28 scenarios, up from 53 across 26).

- **A shared phone could merge two different people.** A phone match counted as exact whenever the records did not hold two different emails, so a record without an email (a front desk contact on an office line, for example) was merged into whoever else used that number, without asking anyone. HubSpot cannot undo a merge. A phone match is now only exact when the names agree as well (the same last name, first names that match or where one starts the other, like "Marc" and "Marcus" or "O." and "Owen"). Anything else on one phone goes to a person as a close match ("same phone, different names", "a last name is missing" or "no name to compare").
- **A merge whose answer was lost was reported as a failure.** When HubSpot saves a merge but the answer never arrives (a timeout), the retry is refused because both IDs already belong to one record, and v1 failed the run with an alert for a merge that had worked. The read after the merges now also covers merges HubSpot refused: if both IDs lead to one record, the merge counts as done and the summary says so.
- **The same for an approved merge.** The page said "HubSpot did not confirm the merge" and an alert went out, also when a double click merged the pair first. A failed merge call now reads both records again: one record means the page says Merged and the decision is saved; two records still give the error page and the alert.
- **One bad contact could hold back a whole batch update, every night.** HubSpot documents partial success (a 207 that names refused inputs by `objectWriteTraceId`) for batch create only. If batch update refuses the whole batch instead, v1 marked all of its up to 100 contacts as failed, and the same batch failed again the next night. A batch refused as a whole is now sent again one contact at a time (up to 100 a run, spaced out), so the valid ones are saved and only the bad one is named. The mock answers both ways.
- **With `MAX_MERGES_PER_RUN=0`, some close matches were never sent.** A close match next to an exact duplicate waits until that duplicate is merged, which never happens with automatic merging off. Such pairs are now sent at once, and the summary says automatic merging is off instead of "The next run continues".
- **A very long contact value could stop the approval request for good.** A company or name value of a few thousand characters (a pasted note, for example) pushed a Slack section past Slack's 3,000 character limit, so Slack refused the message every night. Values are now cut to 150 characters in the approval message; the confirm page still shows them in full.
- **Honest wording where HubSpot's docs are silent.** A record that no longer comes back under its ID is now reported as "merged or deleted since the read", not as deleted, because HubSpot's docs do not say what a read by a retired ID returns. The README and the test report now say which behaviors are documented and which the mock assumes (retired IDs, partial success of batch update, the error for merging records that are already one), and the mock can answer the first two either way. `DRY_RUN` was described as "exactly false" although the check ignores case and spaces, and the daily limit row said reads are not retried although they are tried 4 times.

## v1 (October 26, 2026)

First build: a nightly HubSpot cleanup in n8n that merges exact duplicate contacts with safeguards, sends close matches to Slack for approval through signed links, maps messy lifecycle stage and lead source values to the allowed set, and assigns unowned contacts by round robin. With an error alert workflow and an end to end mock test (26 scenarios, 53 checks).

### Runs and safety

- Nightly schedule (only when `SCHEDULE_ENABLED` is true) and a Run Now webhook protected by the `X-Run-Secret` header. Both share the same logic.
- `DRY_RUN` is on unless it is exactly `false`. The Run Now body can ask for a dry run but never turn it off. A dry run reads, plans and reports, and writes nothing: no merge, no update, no approval request, and nothing in the workflow's memory.
- Every setting is checked before any HubSpot call. Run Now answers 500 with the list of problems; the nightly run fails with the same list.

### Duplicates

- Contacts are read with `GET /crm/v3/objects/contacts`, page by page with the `after` cursor and the properties the workflow needs.
- Exact matches: the same normalized email (lower case, trimmed, Gmail dots and `+tags` ignored for `EMAIL_DOT_PLUS_DOMAINS`, `googlemail.com` read as `gmail.com`) or the same normalized phone (digits with the country code; extensions dropped; placeholders ignored). A shared phone is only exact when the records do not hold two different emails.
- Close matches: the same name (accents and case ignored) plus the same company (legal suffixes and "&" ignored) or the same email domain (free mail domains excluded), or one phone with two different emails.
- The record created first is kept (ties: the lower record ID).
- Exact matches are merged one at a time with `POST /crm/v3/objects/contacts/merge`, up to `MAX_MERGES_PER_RUN`, after a fresh batch read that skips anything merged or deleted in the meantime. The second merge of a group goes into the new ID the first merge returned.
- Writes back off on 429, 423, 5xx and lost connections (`BACKOFF_SECONDS`, doubled each try, up to 5 tries, longer when HubSpot sends `Retry-After`). A DAILY 429 is not retried. Reads are tried 4 times, 5 seconds apart, and paged about 6 requests a second.

### Approvals

- One Slack message per run with an Approve and a Skip link per close match (up to `MAX_APPROVALS_PER_RUN`). Each link carries a base64url token with the pair, the action, a nonce and the expiry, signed with HMAC SHA-256 (`APPROVAL_SECRET`, through n8n's Crypto node).
- Opening a link only shows a confirm page. The button on that page sends a POST with the same token, and only that POST acts.
- Forged links get a 403 and an alert, expired links a 410, used or replaced links a 409. Pairs are remembered only after Slack accepted the message; skipped pairs are never suggested again; an unanswered pair whose link expired is sent again with a new nonce.
- An approved pair is read again before merging. If someone merged it in HubSpot in the meantime, or a record is gone, nothing is merged and the page and Slack say so.

### Fields and owners

- Lead source values are mapped in place with `LEAD_SOURCE_MAP`. Values from `LIFECYCLE_TEXT_PROPERTY` are mapped with `LIFECYCLE_MAP` to HubSpot's lifecycle stage, only forward. Mapping targets are checked against the property definitions (`POST /crm/v3/properties/contacts/batch/read`), and only properties whose value changes are sent.
- Unowned contacts get the next owner from `ROUND_ROBIN_OWNERS`, checked against `GET /crm/v3/owners` (active people only); `AWAY_OWNERS` are skipped. Oldest contacts first, up to `MAX_ASSIGNMENTS_PER_RUN`. Records that may be one person share one owner, and get none when one of them already has an owner. The last owner who got a contact is remembered between runs.
- Field fixes and owners go out together, 100 contacts per `POST /crm/v3/objects/contacts/batch/update`, with an `objectWriteTraceId` per contact so a partial (207) answer names the refused contacts.

### Report and failures

- One Slack summary per run with before and after counts, the merges, the approvals, the field fixes, the owners and every problem. Any problem (a failed merge or update, a refused Slack post, a bad owner in the list, a bad mapping entry) fails the run after the summary, for the error workflow.

### Found and fixed while testing

- **The shared retry code shipped with its placeholders.** The merge and update checks share one source with the node names filled in by the build script, and the first build left them unfilled, so the first run stopped at "Check Merge" right after a 429. The build script now fills them and refuses to build if any are left.
- **"Tidewater Supply & Co" and "Tidewater Supply" were not one company.** "&" became "and", and "and" stayed in the name. It is now dropped like the legal suffixes.
- **Decided pairs kept contact details forever.** Skipped pairs are kept so they are never suggested again, and they kept names, emails and phones in n8n's memory. A decided pair now keeps only its record IDs.
- **Small wording fixes in the summary:** the header read "(run now run)", and "1 contact that duplicate a record" missed the singular verb.
- **Gaps in the first test.** Added scenarios for an approval pressed while `DRY_RUN` is on, an unanswered link that expires and is sent again, HubSpot's DAILY limit (must not be retried) and Slack refusing both the approval request and the summary (the pairs must not be remembered). The confirm page was also checked once in headless Chrome, because n8n serves HTML responses with a `sandbox` content security policy and an HTTP test cannot show that the form still submits.
