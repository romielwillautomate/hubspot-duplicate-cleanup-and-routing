# Setup: HubSpot, Slack and n8n

You need self-hosted n8n reachable over HTTPS (people open the approval links from their phone or laptop), a HubSpot account where you can create a private app, and a Slack channel for the reports. Plan about 30 minutes, plus a few nights of dry runs before the workflow changes anything.

## 1. HubSpot

### Private app token

1. In HubSpot, go to **Development**, then **Legacy apps** in the left sidebar.
2. Click **Create legacy app** and choose **Private**. Give it a name such as "Duplicate cleanup".
3. On the **Scopes** tab, click **Add new scope** and add:

   | Scope | Used for |
   |---|---|
   | `crm.objects.contacts.read` | Reading contacts (the list and the batch reads before and after merging) |
   | `crm.objects.contacts.write` | Merging contacts and the batch updates (field fixes and owners) |
   | `crm.objects.owners.read` | Checking `ROUND_ROBIN_OWNERS` against the owners API |
   | `crm.schemas.contacts.read` | Reading the lifecycle stage and lead source property definitions |

4. Click **Create app**, then on the **Auth** tab click **Show token** and copy it into `HUBSPOT_TOKEN` (it starts with `pat-`).

Treat the token like a password: it can merge any contact. If it leaks, open the app's **Auth** tab and click **Rotate**.

HubSpot ties a private app to the user who created it. If that user is removed from the account, calls start failing; create the app with a long lived admin account.

### Properties

**Lead source.** HubSpot's own source properties (Original Source, Latest Source) are calculated by HubSpot and cannot be changed, so the cleanup works on your own property. If you already have one (for example "Lead source" from a form or an import), put its internal name in `LEAD_SOURCE_PROPERTY`. To find it: **Settings → Properties → Contact properties**, open the property, and copy the **internal name** under the label. It can be a single line text property or a dropdown:

- Text property: the allowed set is the list of values on the right side of `LEAD_SOURCE_MAP`.
- Dropdown: the allowed set is the dropdown's options, and every value in `LEAD_SOURCE_MAP` must be one of their internal values. An entry that is not is ignored and named in the summary.

**Lifecycle stage.** HubSpot's Lifecycle stage only accepts its own stage values, so messy stages usually sit in a text property from an old CRM import or a form ("MQL", "Sales Qualified", "client"). Put that property's internal name in `LIFECYCLE_TEXT_PROPERTY`. The workflow maps its value with `LIFECYCLE_MAP` and writes the result to Lifecycle stage. The map's values are the internal stage values: `subscriber`, `lead`, `marketingqualifiedlead`, `salesqualifiedlead`, `opportunity`, `customer`, `evangelist`, `other`, or the internal values of your custom stages (shown on the property's options). Stage labels are matched too, so "Sales Qualified Lead" works without a map entry.

HubSpot only lets the API move lifecycle stage forward. When the text says an earlier stage than the contact already has, the workflow leaves the stage alone and lists the contact in the summary.

Leave either property setting empty to turn that cleanup off.

### Hub ID (optional)

With `HUBSPOT_PORTAL_ID` set, Slack messages link each record. The Hub ID is in the account menu at the top right of HubSpot. Accounts hosted in the EU also set `HUBSPOT_APP_URL=https://app-eu1.hubspot.com`.

### Owners for the round robin

`ROUND_ROBIN_OWNERS` takes owner emails or owner IDs, separated by commas, in the order people take turns. Emails are easiest. To see every owner with their ID:

```bash
curl "https://api.hubapi.com/crm/v3/owners?limit=100" -H "Authorization: Bearer pat-your-token"
```

Each run checks the list against this API. A deactivated user, an unknown email or a queue is left out of the rotation, named in the summary, and the run fails so someone fixes the list.

Put people who are on leave in `AWAY_OWNERS` (the same emails or IDs). They keep their place in the order and get the next turn after the person before them once you take them out again.

HubSpot notifies owners about each contact assigned to them (depending on their notification settings). On the first live run, an account with years of unowned contacts could send hundreds of notifications, so start with a small `MAX_ASSIGNMENTS_PER_RUN`. The oldest unowned contacts are assigned first and the rest wait for the next runs.

## 2. Slack

1. Create a Slack app at **api.slack.com/apps → Create New App → From scratch**, pick your workspace.
2. Open **Incoming Webhooks**, switch it on, click **Add New Webhook to Workspace** and choose the channel for the reports (a private channel for the sales managers works well, because the approval links are in it).
3. Copy the webhook URL into `SLACK_WEBHOOK_URL`.

Anyone in that channel can approve or skip a merge, so choose the channel with that in mind.

## 3. n8n

1. Add the variables from `.env.example` to your n8n environment and restart n8n. The nodes read them with `$env`, so self-hosted n8n needs `N8N_BLOCK_ENV_ACCESS_IN_NODE=false`. (n8n Cloud does not allow `$env`. There, replace `$env.` with `$vars.` in the workflow and create the same names under Variables.)

   The mapping tables are JSON on one line. In a Docker `env_file` they work as written in `.env.example`. If you set them in a shell instead, wrap them in single quotes.

2. Import `workflow/error-alert-slack.json` and **activate it**. n8n 2.x only runs an error workflow that is active; an inactive one is skipped without any alert.
3. Import `workflow/hubspot-duplicate-cleanup-and-routing.json`. In its settings, choose the error alert workflow as its **Error Workflow**.
4. Activate the cleanup workflow. It listens on:

   - `https://YOUR-N8N-DOMAIN/webhook/hubspot-cleanup-run` (Run Now, POST)
   - `https://YOUR-N8N-DOMAIN/webhook/hubspot-merge-approval` (approval links: GET shows the confirm page, POST acts)

5. Set `APPROVAL_WEBHOOK_URL` to the second URL exactly as people will open it, and restart n8n if you changed it.
6. Make up two secrets: `RUN_NOW_SECRET` (any long random string) and `APPROVAL_SECRET` (at least 32 characters). For example: `openssl rand -hex 32`. Changing `APPROVAL_SECRET` later voids every open approval link; the next run sends new ones.

The nightly run starts at 2:30 in the n8n server's time zone (`GENERIC_TIMEZONE`) once `SCHEDULE_ENABLED=true`. Change the time in the **Schedule: Nightly** node.

## 4. First runs

Start with a dry run. It reads everything and reports, and changes nothing:

```bash
curl -X POST "https://YOUR-N8N-DOMAIN/webhook/hubspot-cleanup-run" \
  -H "X-Run-Secret: YOUR_RUN_NOW_SECRET" -H "Content-Type: application/json" \
  -d '{"dryRun": true}'
```

You get `{"started":true,"dryRun":true}` right away, and Slack gets a summary marked DRY RUN with the merges, approvals, field fixes and owner assignments a real run would make. Check:

- the "Would merge" list: are these really the same people?
- "Values not in the mapping table": add the ones you want to clean to `LEAD_SOURCE_MAP` or `LIFECYCLE_MAP`
- the problems: every owner in the rotation must be valid

Turn on the schedule with `DRY_RUN` still on for a few nights if you want to watch the reports first. Then go live in small steps:

1. Set `DRY_RUN=false`, `MAX_MERGES_PER_RUN=1` and `MAX_ASSIGNMENTS_PER_RUN=5`, restart n8n and send the same request without `dryRun`.
2. Open the merged record in HubSpot and check that it looks right.
3. Open one Approve link from the Slack message, check the confirm page and press the button. Press Skip on a pair that is not one person.
4. Raise the limits.

## 5. How the approval links work

- Each close match gets an Approve and a Skip link. Both carry a token with the pair, the action, a one-time nonce and the expiry, signed with `APPROVAL_SECRET`.
- Opening a link shows a confirm page with both records. Slack link previews, email scanners and browsers that open links in advance therefore change nothing. Only the button on the page acts.
- A link works for `APPROVAL_LINK_HOURS` (72 by default). If nobody answers, the next run after the expiry sends the pair again with a new link, and the old link stops working.
- Every link works once. Opening a used link shows when the pair was decided.
- Skipped pairs are remembered and never sent again, even after one of the records is merged with a third record (HubSpot keeps the old IDs in `hs_merged_object_ids`).
- The workflow remembers waiting pairs, decisions and the rotation in its static data, which n8n stores with the workflow in its database (the editor does not show it). Re-importing the workflow starts with an empty memory: open links stop working, skipped pairs are asked about again, and the rotation starts over.

## Troubleshooting

Open the workflow's **Executions** list in n8n:

| You see | Likely cause |
|---|---|
| Run stopped at "Stop: Bad Run Secret" | The `X-Run-Secret` header does not match `RUN_NOW_SECRET`, or `RUN_NOW_SECRET` is empty |
| Run Now answered 500, run stopped at "Stop: Bad Settings" | A setting is missing or invalid. The response and the alert list every problem |
| A run failed but Slack stayed quiet | The error alert workflow is not active, is not chosen as the Error Workflow, or Slack itself refused posts |
| Run stopped at "Stop: HubSpot Read Failed" with 401 | `HUBSPOT_TOKEN` is wrong or was rotated, or the user who created the private app was removed |
| ... with 403 and "MISSING_SCOPES" | A scope from the table above is missing on the private app |
| ... with 429 and "DAILY" | The account's daily API limit is used up (other apps share it). It resets at midnight in the account's time zone |
| Summary problem: "is not an active HubSpot owner" | Fix `ROUND_ROBIN_OWNERS`: the user was deactivated, the email is misspelled, or the entry is a queue |
| Summary problem: "LIFECYCLE_MAP: ... is not a lifecycle stage" | Use the internal stage value (`marketingqualifiedlead`, not "MQL") or your custom stage's internal value |
| Summary problem: "LEAD_SOURCE_PROPERTY ... is read only" | It points to a HubSpot calculated property such as `hs_analytics_source`. Use your own property |
| "Merge failed ... 400" | HubSpot refused the merge, and reading both records again after the merges still showed two records (a merge whose answer was lost is counted as merged, not listed here). Read HubSpot's message, for example the limit of 250 merges per record |
| Summary note: "HubSpot answered 1 merge with an error, but the records are now one" | HubSpot saved the merge but its answer was lost (a timeout), and the retry was refused. Nothing to do |
| "HubSpot did not save the update ... (sent alone after HubSpot refused its batch)" | HubSpot refused a whole batch because of one contact, so its contacts were sent again one at a time. The named contacts hold a value HubSpot does not accept (check the property's validation rules); the others were saved |
| Approval link page: "This link is not valid" | The link was changed or cut short (some mail clients break long links), or `APPROVAL_SECRET` changed. An alert was sent |
| Approval link page: "This link is no longer active" | A newer link was sent for the pair (the old one expired unanswered), or the workflow was re-imported |
| Approval link page: "Dry run is on" | `DRY_RUN` is not `false`, so approvals do not change HubSpot |
| Approval link page: "HubSpot did not confirm the merge" | HubSpot failed after 3 tries, and reading the records again showed two records or did not work. Check the records in HubSpot; if they are still separate, use the same link again later |
| The same close match comes back after you pressed Skip | A nightly run that was still going when you pressed Skip saved the memory it loaded at its start (see Limitations in the README). Press Skip again |
