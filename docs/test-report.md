# Test report

**Date:** October 26, 2026
**n8n:** 2.19.5, self-hosted in Docker
**Test script:** [tests/e2e_mock_test.py](../tests/e2e_mock_test.py)
**Result:** 61 of 61 checks pass across 28 scenarios (after the review fixes in v1.1; v1 had 53 checks across 26 scenarios)

## How it was tested

No real HubSpot account or Slack workspace was used. The test script:

1. starts a mock HubSpot CRM v3 API and a mock Slack incoming webhook on the test machine,
2. imports and activates a temporary copy of the error alert workflow (n8n only runs an active one), then a temporary copy of the cleanup workflow that reports to it, with the HubSpot host pointed at the mock and made up keys,
3. runs the cleanup against 60 made up contacts: a refused call, a dry run, five real runs (rate limits, a failing merge, a refused update, a merge by someone else during the run, an away owner coming back, a merge whose answer is lost, a repeat with nothing to do, an expired link), a run with a revoked token, and the approval links (approve, skip, reuse, forged, expired, replaced, approved after someone merged the pair, an approved merge whose answer is lost),
4. waits for each n8n run to finish, then checks every request the workflow made, every Slack message, the HTML pages and their headers, the workflow's memory (static data, read through the n8n API) and the node each run ended on,
5. checks that every failed run raised exactly one error alert, matched to its run by the execution link inside the alert,
6. imports four more copies: one with the nightly schedule switched on, `DRY_RUN` not set and automatic merging off; one for the safeguards (an office line shared by two people, HubSpot answering retired record IDs as not found, a batch update refused as a whole, a very long contact value); one on a day HubSpot's daily limit is used up and Slack is down; and one with broken settings. Then it deletes every temporary copy.

To let time pass, the test changes the workflow's memory through the n8n API (`PUT /api/v1/workflows/{id}` with `staticData`, which keeps the workflow active): it moves one approval's expiry into the past, and it gives the dry run copy a pair that waits for a decision. To bring the away owner back it swaps the copy's settings the same way, keeping its memory, as an n8n restart with a changed environment would.

This workflow does not call Claude or any other AI model, so there is no Anthropic request to validate.

### The mock HubSpot API

It keeps real state: contacts, owners (with a deactivated user and a queue), property definitions, merges, and the IDs merges retire. Following HubSpot's docs, a merge creates a new record ID, the kept record's values win and empty fields are filled from the other record, the other email becomes an additional email, the furthest lifecycle stage is kept, and the old IDs are listed in `hs_merged_object_ids`. By default the old IDs also still lead to the merged record, which the docs do not state (see the table below). It rejects what the real API rejects:

- a missing or wrong bearer token (401, `INVALID_AUTHENTICATION`), and more than 100 requests in a rolling 10 seconds (429, `TEN_SECONDLY_ROLLING`)
- unknown query parameters and unknown body fields, and writes without a JSON content type
- contact list: `limit` outside 1 to 100, an `after` that is not a record ID
- batch read: no `properties` or no `propertiesWithHistory` (both are required in HubSpot's schema), more than 100 inputs, inputs that are not `{"id": ...}`; IDs that no longer exist come back in a 207 with an `OBJECT_NOT_FOUND` error (the 207 and its `errors` list follow the schema; the message text is the mock's, and so is answering a read where no ID exists with a 207 and no results rather than a 404)
- property batch read: no `archived` or `dataSensitivity` (both required in the schema); unknown properties come back in a 207
- merge: missing or non string IDs, records that do not exist (404), two IDs that already belong to one record
- batch update: more than 100 inputs, the same ID twice, unknown properties, read only properties (`createdate`, `hs_merged_object_ids` and others), invalid dropdown options, owner IDs that are not active owners, a lifecycle stage moved backward. With an `objectWriteTraceId` on every input it answers 207 and saves the valid inputs; without, it refuses the whole batch with 400
- planned faults for the scenarios: a 429 on one contact page and on one merge, a 500 on one merge, a merge HubSpot saves but answers with a 504 (so the retry finds one record and is refused), one refused contact in a batch update, a DAILY 429, a revoked token, and two records merged "by someone else" the moment the run reads them again

Three behaviors are not stated in HubSpot's docs, so the mock makes an assumption for each, and for the first two the test also runs the other answer:

| Behavior | The mock by default | The other answer, also tested |
|---|---|---|
| A request with a record ID retired by a merge | Leads to the merged record, under its new ID (what HubSpot community answers describe) | Not found (`FAIL['old_ids_missing']`): a planned pair merged by someone else during the run is skipped, nothing is merged |
| A batch update where one input is invalid | Saves the others and answers 207, naming the refused input by `objectWriteTraceId` (how the docs describe batch create) | The whole batch refused with a 400 (`FAIL['update_atomic']`): the workflow sends the contacts again one at a time |
| Merging two IDs that already belong to one record | 400 with the mock's own text | Not run both ways: the workflow does not read the status or the text, it reads both records again after any refused merge |

Every response carries HubSpot's rate limit headers (`X-HubSpot-RateLimit-Max`, `-Remaining`, `-Interval-Milliseconds`, `-Daily`, `-Daily-Remaining`). The busiest 10 seconds of the whole test held 19 requests.

### The mock Slack webhook

It accepts a POST with JSON with `text` and `blocks` (the fields Slack's incoming webhook guide documents) and the other message fields `attachments`, `thread_ts`, `mrkdwn`, `unfurl_links` and `unfurl_media`, refuses anything else, requires non empty `text`, allows at most 50 blocks of known types, and section text of 1 to 3,000 characters. It answers `ok` like Slack, `invalid_payload` or `no_text` for bad payloads, and `no_service` (404) in the outage scenario. Error alerts from the error workflow go to a second path, so they can be told apart from the workflow's own messages.

### Checked in a real browser

n8n 2.x serves HTML from a Respond to Webhook node with a `Content-Security-Policy: sandbox ...` header. To prove the confirm page still works for a person, it was opened once in headless Chrome (Puppeteer) against the same mocks, and its button pressed. Result: the page showed "Merge these two contacts?" with a "Merge these records" button, the header was `sandbox allow-downloads allow-forms ...`, pressing the button posted the form, the browser ended on a page titled "Merged" ("Jordan Ellis (record 1014) was merged into Jordan Ellis (record 1013). The merged record's ID is 9005."), and the mock received exactly one merge call. This check is a separate one-off script, not part of `tests/e2e_mock_test.py`, because it needs Node and Puppeteer. It was run on v1; the review changed neither the confirm page nor its form, and the new result page for a merge confirmed by a fresh read uses the same page template.

## Checked against the docs

Every request and response shape was checked against HubSpot's API reference and guides (fetched October 26, 2026) and Slack's documentation:

| Call | Endpoint | Docs | What was checked |
|---|---|---|---|
| List contacts | `GET /crm/v3/objects/contacts` | Contacts v3 reference "Retrieve contacts", Contacts guide (legacy v3) | `limit` up to 100, `after` from `paging.next.after` (the next record ID), `properties` as a comma list, `archived=false`. Response `results` and `paging` |
| Read again | `POST /crm/v3/objects/contacts/batch/read` | "Retrieve a batch of contacts" | `inputs` (up to 100), `properties` and `propertiesWithHistory` (both required in `BatchReadInputSimplePublicObjectId`), 200 or 207 with `errors`. The page says nothing about merged or deleted IDs |
| Merge | `POST /crm/v3/objects/contacts/merge` | "Merge two contacts", Using object APIs guide, knowledge base "Merge records" | `primaryObjectId` and `objectIdToMerge` (strings, required). Response `SimplePublicObject` whose `id` is the merged record's ID, new unless the account is in the Primary ID Preservation beta. Only the generic error shape is documented |
| Update | `POST /crm/v3/objects/contacts/batch/update` | "Update a batch of contacts", error handling guide ("Multi-status errors") | `inputs` of `id`, `properties`, optional `objectWriteTraceId`; read only properties refused; up to 100 (Contacts guide); lifecycle stage only forward (Contacts guide). A 207 `BatchResponseSimplePublicObjectWithErrors` is listed, but partial success with `objectWriteTraceId` is only described for batch create |
| Owners | `GET /crm/v3/owners` | Owners v3 reference | `limit`, `after`, `archived`. `PublicOwner`: `id`, `email`, `firstName`, `lastName`, `type` (`PERSON` or `QUEUE`), `archived`, `teams` |
| Property definitions | `POST /crm/v3/properties/contacts/batch/read` | "Read a batch of properties" | `archived`, `dataSensitivity`, `inputs` of `{name}` (all required). `Property.options` with `value`, `label`, `displayOrder`, `hidden`; `modificationMetadata.readOnlyValue` |
| Errors and limits | all | Error handling guide, API usage guidelines | Error body (`status`, `message`, `correlationId`, `category`, `errors`, `context`); 429 body with `errorType: RATE_LIMIT` and `policyName` (`DAILY` or the 10 second window); 100 requests per 10 seconds and 250,000 a day for private apps on Free and Starter; 423 locks last 2 seconds; 414 after 250 merges of one record |
| Token and scopes | | Private apps overview, scopes reference | Bearer token from a legacy private app; `crm.objects.contacts.read`, `crm.objects.contacts.write`, `crm.objects.owners.read`, `crm.schemas.contacts.read` |
| Slack | incoming webhook | Slack "Sending messages using incoming webhooks", "Formatting message text", section block reference | POST JSON with `text` and `blocks`; answer `ok`; errors as text; `&`, `<`, `>` escaped; `<url\|label>` links; section text up to 3,000 characters |

Notes from the docs that shaped the workflow:

- **A merge creates a new record ID.** The knowledge base says "a new unique Record ID is created for the resulting merged record", and the API guide says the merge response's `id` is the merged record's ID (the old one is only kept in the Primary ID Preservation beta). So the second merge of a group of three goes into the ID the first merge returned, the merged records are read again by their new IDs, and the workflow never assumes the old ID survived.
- **Old IDs.** The knowledge base points to the "Merged record IDs" property (`hs_merged_object_ids`) and says the old records are combined into the merged one. Neither the knowledge base nor the API reference says what a read by an old ID returns. HubSpot community answers (seen in search results; the community site refuses direct fetches) say it returns the merged record under its new ID. The workflow handles both answers: a record returned under another ID, or listed with the old ID in `hs_merged_object_ids`, counts as merged; a missing record counts as "merged or deleted" and is never merged. The test runs the mock both ways.
- **Emails are unique in HubSpot**, including additional emails (Contacts guide). Two contacts cannot hold the same email, so exact email duplicates come from variants of one mailbox (dots and `+tags` at Gmail). The test data respects this: the seed refuses duplicate emails.
- **Lifecycle stage only moves forward** through the API (Contacts guide). The workflow never tries a backward move. The docs give no error text for one, so the mock's error message for it is its own.
- **207 on batch update.** The error handling guide documents multi-status answers "for the object APIs' batch create endpoints" with an `objectWriteTraceId` per input; the batch update reference also lists a 207 response and accepts `objectWriteTraceId`, but does not say when it is used. The workflow sends a trace ID per contact and reads a 207. If HubSpot refuses the whole batch instead (a 4xx that is not about the token or a limit), it sends the contacts of that batch again one at a time, so the valid ones are saved. The mock answers 207 by default and refuses the whole batch in the safeguards scenario.
- **Merging records that are already one.** The docs do not give the error. The mock answers 400 with its own text. The workflow does not depend on the status or the text: after any refused merge it reads both records again (the read after the nightly merges, or a second read of the approved pair), and if both IDs now lead to one record, the merge counts as done. This is how a merge whose answer was lost (a 504 after HubSpot saved it) is recognized.
- **v3 is now listed as legacy.** HubSpot publishes date versioned endpoints (`/crm/objects/2026-09/contacts/...`) with the same request shapes for these calls. The workflow uses v3, which still works.

## Results

```
Run Now, wrong secret
  PASS  refused with 401, nothing read or changed  (401, ended at Stop: Bad Run Secret)

Dry run
  PASS  answers 202 at once, then only reads: no merge, no update, no approval request, memory unchanged  (202 {"started":true,"dryRun":true}, ended at Any Problems?)
  PASS  contacts are read page by page (limit 25, archived=false, the properties it needs, the after cursor)  (3 pages, after = [None, '1026', '1051'])
  PASS  owners and property definitions read with the documented parameters  ({"archived": false, "dataSensitivity": "non_sensitive", "inputs": [{"name": "lifecyclestage"}, {"name": "lead_source"}, {"name": "legacy_lifecycle_stage"}]})
  PASS  Slack report says DRY RUN and lists what a real run would do  (Before and after (if run for real): 60 to 56 contacts, 33 to 3 without an owner, 16 to 2 messy field values)

Real run 1
  PASS  answers 202 with dryRun false, ends without problems  (202 {"started":true,"dryRun":false}, ended at Any Problems?)
  PASS  HubSpot 429 on page 2 of the contacts: the read waits and starts again, then all 60 contacts are read  (statuses [200, 429, 200, 200, 200], waited 5.8s)
  PASS  HubSpot 429 on a merge: it backs off and tries again; the merge happens once  (statuses [429, 200, 200, 200, 200])
  PASS  exact merges with the documented body; the second merge of a group goes into the NEW ID the first returned; the record created first is kept  ([{"primaryObjectId": "1001", "objectIdToMerge": "1002"}, {"primaryObjectId": "9001", "objectIdToMerge": "1003"}, {"primaryObjectId": "1005", "objectId)
  PASS  merge cap: exactly MAX_MERGES_PER_RUN (4) merges, the other 3 wait (Nina Gomez, Owen Brooks group still separate)  (4 merges)
  PASS  each planned merge is checked with a fresh batch read before merging, and the merged records are read again after  (2 batch reads)
  PASS  field fixes only where a value changes, and only the changed properties (clean values, already right stages, unmapped values and backward moves are left alone)  (30 contacts updated)
  PASS  round robin: every unowned contact gets the next owner (Ava, Ben, Cleo; Dan is away), oldest first, one owner per set of duplicates, none when a duplicate already has an owner  (30 expected, 30 sent, last owner 202)
  PASS  close matches go to Slack in one message: 5 pairs, each with an Approve and a Skip link, contact text escaped  (*HubSpot: 5 possible duplicates need a decision*)
  PASS  each link carries a token signed with HMAC SHA-256 (verified here with the secret): pair, action, nonce, expiry in 72 hours; pairs saved as waiting  (rotation 202)
  PASS  Slack summary: merged, waiting for approval, fields fixed, owners assigned, before and after counts  (Before and after: 60 to 56 contacts, 33 to 3 without an owner, 16 to 2 messy field values)

Approve link opened (GET)
  PASS  shows a confirm page with both records and a button; nothing changes in HubSpot or in memory  (200, ended at Respond: Approval Page)

Approve pressed (POST)
  PASS  both records read again, then merged; page and Slack note confirm it; the pair is marked merged  (200, ended at Slack: Decision Note)

Approve link used again
  PASS  refused with 409 (POST and GET), nothing read or merged, no alert  (409/409, ended at Respond: Approval Page)

Skip link opened (GET)
  PASS  confirm page for skipping, nothing changes  (200)

Skip pressed (POST)
  PASS  pair remembered as skipped, HubSpot not touched, Slack note posted  (200, ended at Slack: Decision Note)

Forged link (pair changed, old signature)
  PASS  refused with 403, nothing read or merged, run fails for the alert  (403, ended at Stop: Forged Approval Link)

Forged link (garbage token)
  PASS  refused with 403, alert  (403)

Expired link (valid signature)
  PASS  refused with 410, nothing read or merged, no alert  (410, ended at Respond: Approval Page)

Replaced link (valid signature, older nonce)
  PASS  refused with 409, nothing done  (409)

Confirm page escapes contact text
  PASS  the company "Fernhill R&D <Labs>" shows as text in the HTML  (200)

Approved after someone merged them in HubSpot
  PASS  the fresh read shows one record: nothing merged, page and Slack say so, not a failure  (200, merged by hand into 9006, ended at Slack: Decision Note)

Real run 2
  PASS  a pair merged by someone else during the run is skipped (fresh read before merging), not merged again, not a failure  (Nina Gomez)
  PASS  a merge that keeps failing (500) is tried 5 times with growing waits (0.5, 1, 2, 4 s)  (statuses [200, 500, 500, 500, 500, 500], gaps [3.9, 1.1, 5.8, 4.0])
  PASS  round robin continues after the last owner of run 1 (Ben): Cleo, Ava, Ben, Cleo; the skipped pair's contact is now routed  ([('1016', '203'), ('1061', '201'), ('1062', '202'), ('1063', '203')])
  PASS  nothing half done goes unreported: the summary names the merge that worked, the one that failed, the update HubSpot refused and the pair merged by someone else  (• Merge failed: O. Brooks (1012) into Owen Brooks (9008), after 1 other merge of this group worked: 500 internal error ()
  PASS  the run fails after the summary (error alert), the rotation only counts saved assignments, no new approval request  (ended at Stop: Cleanup Had Problems, rotation 203)

Real run 3
  PASS  the failed merge is finished; HubSpot saves it but the answer is lost (504) and the retry is refused (400, already one record): the read after the merges shows one record, so it counts as merged, not as a failure  (statuses [504, 400], ended at Any Problems?)
  PASS  the away owner who came back gets the next turn (after Cleo comes Dan)  (ended at Any Problems?)

Real run 4 (repeat)
  PASS  nothing new to do: no merge, no update, no approval request, the summary says so  (ended at Any Problems?)

Real run 5 (a link expired unanswered)
  PASS  only that pair is sent again, with a new nonce and a new expiry; the pair that still waits is not sent twice  (ended at Any Problems?)
  PASS  the link from the first message no longer works (409); only the new link is valid  (409)

Approve pressed, HubSpot answer lost
  PASS  HubSpot saves the merge but answers 504 and refuses the retries (400): the records are read again, found to be one, and the page and Slack say merged, with no false alarm  (200, merge statuses [504, 400, 400], ended at Slack: Decision Note)

HubSpot token refused (401)
  PASS  tried 4 times, nothing changed, run fails with the reason and the setting to check  (ended at Stop: HubSpot Read Failed)

All runs
  PASS  every merge and update sent the bearer token, and no 10 second window went over HubSpot's 100 requests  (busiest 10 seconds: 19 requests)

Schedule with SCHEDULE_ENABLED=false
  PASS  scheduled runs stop at "Schedule On?" and do nothing  (2 scheduled run(s))

Workflow memory
  PASS  only approvals and the rotation are kept; decided pairs keep record IDs only, no names or emails  (5 pairs, 4 decided)

Error alert workflow
  PASS  every failed run raised exactly one Slack alert with its execution link (wrong secret, 2 forged links, run 2, revoked token)  (5 failed runs, 5 alerts)
  PASS  the alert names the node and every problem of the run  (:rotating_light: *TEST hubspot-duplicate-cleanup-and-routing (temporary)* failed at *Stop: Cleanup Had Problems*)
  PASS  a forged approval link raises an alert that says why it was refused  (An approval link was refused because its signature does not match. Nothing was changed. If)

Nightly schedule on, DRY_RUN not set
  PASS  the scheduled run is a dry run: it reads and reports, and changes nothing  (1 scheduled run(s), ended at Stop: Cleanup Had Problems)
  PASS  owners that are not active people are left out of the rotation and fail the run  (ended at Stop: Cleanup Had Problems)
  PASS  with MAX_MERGES_PER_RUN=0 nothing is merged, the summary says automatic merging is off, and a close match next to an exact duplicate is asked about now  (Would ask for approval: 3 close matches)
  PASS  the failed nightly run raised its alert
  PASS  an approval pressed while DRY_RUN is on changes nothing in HubSpot and the pair keeps waiting  (409)

Safeguards
  PASS  an office line shared by two people (one without an email) is not merged: it goes to a person as a close match  (*3. Grace Hill*: same phone, different names)
  PASS  a very long contact value is shortened, so the approval section stays within Slack's 3,000 characters and Slack accepts it  (longest section 1105 characters)
  PASS  retired IDs answered as not found, pair merged by someone else during the run: nothing merged, the pair is reported, not called deleted  (Not merged: Ivy Moss (1068) and Ivy Moss (1067): the record to keep is no longer in HubSpot under this ID (merged or deleted since the read))
  PASS  HubSpot refuses a whole batch update because of one contact: the contacts are sent again one at a time, the others are saved, the refused one is named and the run fails  (batch [400], one at a time {'1065': 200, '1066': 400, '1069': 200, '1070': 200}, ended at Stop: Cleanup Had Problems)
  PASS  the failed run raised its alert, naming the refused contact

HubSpot daily limit used up, Slack down
  PASS  the DAILY 429 is not retried: one batch update attempt, nothing saved, no rotation remembered  (1 attempt(s))
  PASS  the approval request and the summary are each tried 3 times; the pairs are not saved, so the next run asks again  (6 Slack attempts)
  PASS  the run fails and the error names all three problems  (ended at Stop: Cleanup Had Problems)
  PASS  the error alert still arrives (the error workflow posts to its own webhook in this test)

Run Now with broken settings
  PASS  answered 500 with the exact list of problems, no HubSpot call, run ends as failed  (500, ended at Stop: Bad Settings)
  PASS  the error alert names every problem  (Cleanup settings are not valid, so nothing was read or changed. MAX_MERGES_PER_RUN must be)

61/61 checks passed
```

## What the workflow sent

All requests below were captured in the final run (`SAVE_REQUESTS`). Every HubSpot call carried `Authorization: Bearer <token>`.

**Contact list, page 1** (`GET /crm/v3/objects/contacts`, query string; page 2 adds `after=1026`, page 3 `after=1051`)

```
limit=25&archived=false&properties=email,firstname,lastname,phone,mobilephone,company,lifecyclestage,hubspot_owner_id,createdate,hs_merged_object_ids,lead_source,legacy_lifecycle_stage
```

**Property definitions** (`POST /crm/v3/properties/contacts/batch/read`)

```json
{"archived": false, "dataSensitivity": "non_sensitive", "inputs": [{"name": "lifecyclestage"}, {"name": "lead_source"}, {"name": "legacy_lifecycle_stage"}]}
```

**Fresh read before merging** (`POST /crm/v3/objects/contacts/batch/read`)

```json
{"properties": ["email", "firstname", "lastname", "hs_merged_object_ids"], "propertiesWithHistory": [], "inputs": [{"id": "1001"}, {"id": "1002"}, {"id": "1003"}, {"id": "1005"}, {"id": "1004"}, {"id": "1006"}, {"id": "1007"}]}
```

**The first merges** (`POST /crm/v3/objects/contacts/merge`; the first answer was a 429, so the same body went again 0.5 seconds later, after the backoff (BACKOFF_SECONDS is 0.5 in the test); the second merge of the group goes into the new ID)

```json
{"primaryObjectId": "1001", "objectIdToMerge": "1002"}  -> 429
{"primaryObjectId": "1001", "objectIdToMerge": "1002"}  -> 200 (new ID 9001)
{"primaryObjectId": "9001", "objectIdToMerge": "1003"}  -> 200 (new ID 9002)
{"primaryObjectId": "1005", "objectIdToMerge": "1004"}  -> 200 (new ID 9003)
{"primaryObjectId": "1006", "objectIdToMerge": "1007"}  -> 200 (new ID 9004)
```

**Reading the merged records** (`POST /crm/v3/objects/contacts/batch/read`, by their new IDs)

```json
{"properties": ["email", "firstname", "lastname", "phone", "mobilephone", "company", "lifecyclestage", "hubspot_owner_id", "createdate", "hs_merged_object_ids", "lead_source", "legacy_lifecycle_stage"], "propertiesWithHistory": [], "inputs": [{"id": "9001"}, {"id": "9002"}, {"id": "9003"}, {"id": "9004"}]}
```

**Field fixes and owners** (`POST /crm/v3/objects/contacts/batch/update`, 30 inputs in run 1; a few of them: a lead source fix on an owned contact, a fix plus an owner, an owner only, a lifecycle stage fix, two fixes plus an owner, the merged Marcus Lee record)

```json
{"id": "1030", "properties": {"lead_source": "Paid Search"}, "objectWriteTraceId": "contact-1030"}
{"id": "1031", "properties": {"lead_source": "Paid Search", "hubspot_owner_id": "201"}, "objectWriteTraceId": "contact-1031"}
{"id": "1037", "properties": {"hubspot_owner_id": "201"}, "objectWriteTraceId": "contact-1037"}
{"id": "1041", "properties": {"lifecyclestage": "salesqualifiedlead"}, "objectWriteTraceId": "contact-1041"}
{"id": "1048", "properties": {"lead_source": "Paid Social", "lifecyclestage": "salesqualifiedlead", "hubspot_owner_id": "201"}, "objectWriteTraceId": "contact-1048"}
{"id": "9002", "properties": {"hubspot_owner_id": "201"}, "objectWriteTraceId": "contact-9002"}
```

**A batch update with one refused contact** (run 2, HubSpot answered 207; the error part of the answer)

```json
{"status": "COMPLETE", "numErrors": 1, "errors": [{"status": "error", "category": "VALIDATION_ERROR", "links": {}, "errors": [], "message": "Property values were not valid: [{\"isValid\": false, \"message\": \"Property values were not valid for contact 1062\", \"error\": \"VALIDATION_ERROR\"}]", "context": {"objectWriteTraceId": ["contact-1062"]}}]}
```

**A merge whose answer was lost** (run 3: the mock saved the merge but answered 504; the retry 0.5 seconds later was refused because both IDs now lead to one record; the read after the merges asked for both IDs and got one record back, so the merge counts as done)

```json
{"primaryObjectId": "9008", "objectIdToMerge": "1012"}  -> 504 (merge saved as 9009, answer lost)
{"primaryObjectId": "9008", "objectIdToMerge": "1012"}  -> 400
POST /crm/v3/objects/contacts/batch/read  {"properties": [...the 12 contact properties...], "propertiesWithHistory": [], "inputs": [{"id": "9008"}, {"id": "1012"}]}  -> 200, one record: 9009
```

The summary of that run says `• Owen Brooks: kept 9008, merged in 1012 (same phone), now record 9009` and `HubSpot answered 1 merge with an error, but the records are now one when read again, so it counts as merged.`, with no problem. The approved merge in the scenario after run 5 went the same way: 504, 400, 400 (three tries), then a second read of the pair found one record, and the page said "Merged".

**A batch update refused as a whole** (safeguards scenario, the mock in its all or nothing mode: the batch of four got a 400, then each contact went alone, about a quarter second apart)

```json
{"inputs": [{"id": "1065", ...}, {"id": "1066", ...}, {"id": "1069", ...}, {"id": "1070", ...}]}  -> 400 VALIDATION_ERROR
{"inputs": [{"id": "1065", "properties": {"hubspot_owner_id": "201"}, "objectWriteTraceId": "contact-1065"}]}  -> 200
{"inputs": [{"id": "1066", "properties": {"hubspot_owner_id": "201"}, "objectWriteTraceId": "contact-1066"}]}  -> 400
{"inputs": [{"id": "1069", "properties": {"lead_source": "Paid Search", "hubspot_owner_id": "203"}, "objectWriteTraceId": "contact-1069"}]}  -> 200
{"inputs": [{"id": "1070", "properties": {"hubspot_owner_id": "202"}, "objectWriteTraceId": "contact-1070"}]}  -> 200
```

**An office line in the approval request** (safeguards scenario: Leo Hart has no email and shares Grace Hill's phone, so a person decides; his 2,900 character company value is cut)

```
*3. Grace Hill*: same phone, different names
Keep: Grace Hill, grace.hill@harbordental.example.com, +1 555 010 1065, Harbor Dental Group, created Oct 26, 2026 (record 1065)
Merge in: Leo Hart, (555) 010-1065, Harbor Dental Group, main line, ask the front desk for whoever handles new patients; main line, ask the front desk for whoever handles new patients; m..., created Oct 26, 2026 (record 1066)
```

**One section of the approval request** (Slack blocks, links shortened)

```
*1. Jordan Ellis*: same name and company; same name and email domain
Keep: Jordan Ellis, jordan.ellis@northwind.example.com, Northwind Traders, created Nov 4, 2023 (<https://app.hubspot.com/contacts/4400001/record/0-1/1013|record 1013>)
Merge in: Jordan Ellis, jellis@northwind.example.com, Northwind Traders Inc., created Nov 16, 2023 (<https://app.hubspot.com/contacts/4400001/record/0-1/1014|record 1014>)
<http://localhost:5678/webhook/hubspot-duplicate-cleanup-and-routing-e2e-approval?token=eyJ2Ijox...(signature)|Approve merge>  |  <http://localhost:5678/webhook/hubspot-duplicate-cleanup-and-routing-e2e-approval?token=eyJ2Ijox...(signature)|Skip>
```

The token in each link is base64url JSON plus an HMAC SHA-256 signature in hex. Decoded, the Approve token above is:

```json
{"v": 1, "a": "approve", "k": "1013|1014", "p": "1013", "s": "1014", "n": "j4ybkljtmvpb461n", "e": 1793281387}
```

**Slack summary of run 2** (a merge failed after one in its group worked, HubSpot refused one contact, and a pair was merged by someone else during the run)

```
*HubSpot cleanup* (Run Now)
Before and after: 57 to 56 contacts, 6 to 2 without an owner, 3 to 2 messy field values
Merged: 1 exact duplicate
• Owen Brooks: kept 1010, merged in 1011 (same phone), now record 9008
Not merged: Nina Gomez (1009) and Nina Gomez (1008): already merged in HubSpot by someone else
Waiting for approval: 2 close matches (0 sent now, 2 from earlier runs)
Fields fixed: 1 (lead source 1)
Values not in the mapping table (left as they are): lead source "Tradeshow booth 2024" (1), lifecycle stage "Hot lead!!" (1)
Lifecycle stage kept for 1 contact: the text says an earlier stage, and HubSpot only moves lifecycle stage forward (Oli Shaw: customer)
Owners assigned: 3 (Cleo Park 2, Ava Lopez 1). Away: Dan Reed
Left unowned: 1 contact that duplicates a record with an owner (the merge keeps that owner).
*Problems: 2*
• HubSpot did not save the update for 1 contact(s): Omar Haddad (1062): Property values were not valid: [{"isValid": false, "message": "Property values were not valid for contact 1062", "error": "VALIDATION_ERROR"}]
• Merge failed: O. Brooks (1012) into Owen Brooks (9008), after 1 other merge of this group worked: 500 internal error (5 tries)
```

**Error alerts** (from the error alert workflow; the link opens the failed run)

```
:rotating_light: *TEST hubspot-duplicate-cleanup-and-routing (temporary)* failed at *Stop: Cleanup Had Problems*
HubSpot cleanup finished with 2 problem(s): HubSpot did not save the update for 1 contact(s): Omar Haddad (1062): Property values were not valid: [{"isValid": false, "message": "Property values were not valid for contact 1062", "error": "VALIDATION_ERROR"}] | Merge failed: O. Brooks (1012) into Owen Brooks (9008), after 1 other merge of this group worked: 500 internal error (5 tries)
http://localhost:5678/workflow/.../executions/...
```

```
:rotating_light: *TEST hubspot-duplicate-cleanup-and-routing (temporary)* failed at *Stop: Forged Approval Link*
An approval link was refused because its signature does not match. Nothing was changed. If nobody on the team changed a link by hand, someone may be guessing links.
http://localhost:5678/workflow/.../executions/...
```

```
:rotating_light: *TEST hubspot-duplicate-cleanup-and-routing (temporary)* failed at *Stop: HubSpot Read Failed*
HubSpot did not answer "HubSpot: Get Owners", so nothing was changed: 401 - "{"status":"error","message":"Authentication credentials not found. This API supports OAuth 2.0 authentication and you can find more details at https://developers.hubspot.com/docs/methods/auth/oauth-overview","correlationId":"211ecf70-bcc8-4a1e-93fe-65d2c7f6581c","category":"INVALID_AUTHENTICATION"}". Check HUBSPOT_TOKEN (a private app token with the scopes in the setup guide) and HubSpot's status page.
http://localhost:5678/workflow/.../executions/...
```

```
:rotating_light: *TEST hubspot-duplicate-cleanup-and-routing (temporary)* failed at *Stop: Bad Settings*
Cleanup settings are not valid, so nothing was read or changed. MAX_MERGES_PER_RUN must be a whole number from 0 to 50 (it is "99"). LEAD_SOURCE_MAP is not a JSON object like {"google ads": "Paid Search"}. APPROVAL_SECRET must be at least 32 characters, so approval links cannot be forged. APPROVAL_WEBHOOK_URL must be the full https URL of the approval webhook. REPORT_TIMEZONE "Mars/Olympus" is not a valid time zone.
http://localhost:5678/workflow/.../executions/...
```

## Findings

Issues found and fixed while building and testing are listed in [CHANGELOG.md](../CHANGELOG.md).

All names, companies, phone numbers and emails above are made up.
