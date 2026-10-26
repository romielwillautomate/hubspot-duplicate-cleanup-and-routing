"""End-to-end test for the HubSpot duplicate cleanup and lead routing workflow against MOCK APIs.

No real HubSpot account or Slack workspace is used. The script:
  1. starts a mock HubSpot CRM v3 API and a mock Slack incoming webhook on this machine (port 8803),
  2. imports and activates a temporary copy of workflow/error-alert-slack.json (n8n only runs an active error workflow),
     then a temporary copy of the cleanup workflow that reports to it (API host pointed at the mock, fake keys),
  3. runs the cleanup several times against 60 made up contacts: a refused call, a dry run, a real run with rate limits,
     approval links (approve, skip, reuse, forged, expired, replaced, already merged in HubSpot, a merge whose answer
     was lost), a run with failures and a contact merged by someone else during the run, a run after an away owner
     comes back (with a merge whose answer was lost), a repeat run with nothing left to do and a run with a revoked token,
  4. checks every request the workflow made, every Slack message, the HTML pages, the workflow's memory (static data),
     the node each run ended on, and that every failed run raised exactly one error alert linked to it,
  5. imports a copy with the nightly schedule switched on (DRY_RUN not set, two bad owners in the rotation, automatic
     merging off), a copy for the safeguards (an office line shared by two people, retired IDs answered as not found,
     a batch update refused as a whole), a copy on a day of outages and a copy with broken settings,
  6. prints PASS or FAIL per check and deletes every temporary copy, also after a crash.
Temporary copies are named "TEST hubspot-duplicate-cleanup-and-routing (temporary)". Copies with exactly that name left
by an interrupted run are deleted at the start. No other workflow is touched.

The mock HubSpot keeps real state (contacts, owners, property definitions, merges with new record IDs, old IDs that
point to the merged record) and rejects what the real API would reject: a missing or wrong bearer token, unknown query
parameters or body fields, page sizes over 100, batch inputs over 100, missing required fields (batch read needs
"properties" and "propertiesWithHistory", the property batch read needs "archived" and "dataSensitivity"), read only or
unknown properties, invalid dropdown options and owner IDs, lifecycle stages moved backward, duplicate IDs in a batch,
and more than 100 requests in 10 seconds.

Three behaviors are not stated in HubSpot's docs, so the mock can answer either way and the test runs both:
  - a request with a record ID retired by a merge: by default it leads to the merged record (what HubSpot community
    answers describe); FAIL['old_ids_missing'] answers it as not found instead,
  - a batch update with one invalid input: by default the valid inputs are saved and a 207 names the refused one by its
    objectWriteTraceId (documented for batch create); FAIL['update_atomic'] refuses the whole batch with a 400 instead,
  - merging two IDs that already belong to one record: a 400 with the mock's own text (the workflow does not depend on
    the status or the text; it reads the records again).

Requirements: Python 3.9+, a running n8n with the public API enabled.
Environment:
  N8N_URL      default http://localhost:5678
  N8N_API_KEY  your n8n API key (Settings > n8n API)
  MOCK_HOST    how n8n reaches this machine; default host.docker.internal (n8n in Docker).
               Use 127.0.0.1 if n8n runs directly on this machine.
  SAVE_REQUESTS  optional file path; every request the workflow made to the mocks is saved there as JSON
                 (method, path, query, body, the mock's status and answer, and the time; headers are left out)
Usage: python tests/e2e_mock_test.py
"""
import base64, copy, hashlib, hmac, json, os, re, sys, threading, time, urllib.error, urllib.parse, urllib.request, uuid
from collections import deque
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

N8N = os.environ.get('N8N_URL', 'http://localhost:5678').rstrip('/')
KEY = os.environ.get('N8N_API_KEY', '')
MOCK_PORT = 8803
MOCK = f"http://{os.environ.get('MOCK_HOST', 'host.docker.internal')}:{MOCK_PORT}"
SLUG = 'hubspot-duplicate-cleanup-and-routing'
WORKFLOW = Path(__file__).resolve().parent.parent / 'workflow' / f'{SLUG}.json'
TEMP_NAME = f'TEST {SLUG} (temporary)'

TOKEN = 'pat-na1-mock-0000-1111-2222'
SECRET = 'test-approval-secret-0123456789abcdef0123456789'
POOL = ['201', '202', '203']              # Dan (204) is away until run 3
LEAD_SOURCE_MAP = {'google': 'Paid Search', 'google ads': 'Paid Search', 'adwords': 'Paid Search', 'ppc': 'Paid Search',
                   'facebook': 'Paid Social', 'fb ads': 'Paid Social', 'linkedin': 'Paid Social', 'word of mouth': 'Referral',
                   'trade show': 'Event', 'webinar': 'Event', 'web form': 'Website', 'seo': 'Organic Search', 'partner': 'Partner'}
LIFECYCLE_MAP = {'mql': 'marketingqualifiedlead', 'marketing qualified': 'marketingqualifiedlead', 'sql': 'salesqualifiedlead',
                 'prospect': 'lead', 'new lead': 'lead', 'client': 'customer', 'closed won': 'customer', 'opp': 'opportunity'}


def env_for(suffix=''):
    return {
        'HUBSPOT_TOKEN': TOKEN, 'RUN_NOW_SECRET': 'test-run-secret', 'APPROVAL_SECRET': SECRET,
        'APPROVAL_WEBHOOK_URL': f'{N8N}/webhook/{SLUG}-e2e-approval{suffix}', 'SLACK_WEBHOOK_URL': MOCK + '/slack/hook',
        'DRY_RUN': 'false', 'SCHEDULE_ENABLED': 'false', 'MAX_MERGES_PER_RUN': '4', 'MAX_ASSIGNMENTS_PER_RUN': '100',
        'MAX_APPROVALS_PER_RUN': '20', 'APPROVAL_LINK_HOURS': '72', 'BACKOFF_SECONDS': '0.5', 'HUBSPOT_PAGE_SIZE': '25',
        'MAX_CONTACT_PAGES': '200',
        'ROUND_ROBIN_OWNERS': 'ava.lopez@salesteam.example.com, 202, cleo.park@salesteam.example.com, dan.reed@salesteam.example.com',
        'AWAY_OWNERS': 'dan.reed@salesteam.example.com',
        'LEAD_SOURCE_PROPERTY': 'lead_source', 'LEAD_SOURCE_MAP': json.dumps(LEAD_SOURCE_MAP),
        'LIFECYCLE_TEXT_PROPERTY': 'legacy_lifecycle_stage', 'LIFECYCLE_MAP': json.dumps(LIFECYCLE_MAP),
        'EMAIL_DOT_PLUS_DOMAINS': 'gmail.com, googlemail.com, mail.example.com',
        'FREE_EMAIL_DOMAINS': 'mail.example.com, inbox.example.com',
        'DEFAULT_COUNTRY_CODE': '1', 'REPORT_TIMEZONE': 'UTC', 'HUBSPOT_PORTAL_ID': '4400001', 'HUBSPOT_APP_URL': 'https://app.hubspot.com',
    }


ENV = env_for()
HOSTS = {'https://api.hubapi.com': MOCK}


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


NOW = datetime.now(timezone.utc)
BASE = datetime(2023, 6, 1, 15, 0, tzinfo=timezone.utc)

# ----------------------------------------------------------------------------------------------------------------------
# Mock HubSpot state
# ----------------------------------------------------------------------------------------------------------------------
LOCK = threading.RLock()
CONTACTS, ALIAS, DELETED = {}, {}, set()
COUNTER = {'id': 9000}
FAIL = {'auth': False, 'list_429_once': False, 'merge_429_once': set(), 'merge_500': set(), 'update_invalid': set(), 'merge_on_read': None,
        'update_daily': False, 'slack_down': False, 'merge_lost_answer': set(), 'update_atomic': False, 'old_ids_missing': False}
LOG = []
WINDOW = deque()
RATE = {'max_in_window': 0}

OWNERS = [
    {'id': '201', 'email': 'ava.lopez@salesteam.example.com', 'firstName': 'Ava', 'lastName': 'Lopez', 'type': 'PERSON', 'userId': 7001, 'archived': False},
    {'id': '202', 'email': 'ben.ortiz@salesteam.example.com', 'firstName': 'Ben', 'lastName': 'Ortiz', 'type': 'PERSON', 'userId': 7002, 'archived': False},
    {'id': '203', 'email': 'cleo.park@salesteam.example.com', 'firstName': 'Cleo', 'lastName': 'Park', 'type': 'PERSON', 'userId': 7003, 'archived': False},
    {'id': '204', 'email': 'dan.reed@salesteam.example.com', 'firstName': 'Dan', 'lastName': 'Reed', 'type': 'PERSON', 'userId': 7004, 'archived': False},
    {'id': '205', 'email': 'eve.stone@salesteam.example.com', 'firstName': 'Eve', 'lastName': 'Stone', 'type': 'PERSON', 'userId': 7005, 'archived': True},
    {'id': '206', 'email': 'inbound@salesteam.example.com', 'firstName': 'Inbound', 'lastName': 'Queue', 'type': 'QUEUE', 'archived': False},
    {'id': '207', 'email': 'finn.hale@salesteam.example.com', 'firstName': 'Finn', 'lastName': 'Hale', 'type': 'PERSON', 'userId': 7007, 'archived': False},
]
for o in OWNERS:
    o.update({'createdAt': '2022-01-10T09:00:00.000Z', 'updatedAt': '2026-01-10T09:00:00.000Z',
              'teams': [{'id': '178588', 'name': 'Inside Sales', 'primary': True}]})
ACTIVE_OWNER_IDS = {o['id'] for o in OWNERS if not o['archived']}

STAGES = [('subscriber', 'Subscriber'), ('lead', 'Lead'), ('marketingqualifiedlead', 'Marketing Qualified Lead'),
          ('salesqualifiedlead', 'Sales Qualified Lead'), ('opportunity', 'Opportunity'), ('customer', 'Customer'),
          ('evangelist', 'Evangelist'), ('other', 'Other')]
STAGE_ORDER = {v: i for i, (v, _) in enumerate(STAGES)}


def prop_def(name, label, typ, field, read_only=False, options=None, hubspot=True, group='contactinformation'):
    return {'name': name, 'label': label, 'type': typ, 'fieldType': field, 'description': '', 'groupName': group,
            'options': options or [], 'displayOrder': -1, 'calculated': False, 'externalOptions': name == 'hubspot_owner_id',
            'hasUniqueValue': False, 'hidden': False, 'hubspotDefined': hubspot, 'formField': not read_only,
            'dataSensitivity': 'non_sensitive', 'createdAt': '2019-08-06T02:41:52.000Z', 'updatedAt': '2026-01-01T00:00:00.000Z',
            'modificationMetadata': {'archivable': not hubspot, 'readOnlyDefinition': hubspot, 'readOnlyValue': read_only}}


PROPS = {p['name']: p for p in [
    prop_def('email', 'Email', 'string', 'text'), prop_def('firstname', 'First Name', 'string', 'text'),
    prop_def('lastname', 'Last Name', 'string', 'text'), prop_def('phone', 'Phone Number', 'string', 'phonenumber'),
    prop_def('mobilephone', 'Mobile Phone Number', 'string', 'phonenumber'), prop_def('company', 'Company Name', 'string', 'text'),
    prop_def('lifecyclestage', 'Lifecycle Stage', 'enumeration', 'radio',
             options=[{'label': l, 'value': v, 'displayOrder': i, 'hidden': False} for i, (v, l) in enumerate(STAGES)]),
    prop_def('hubspot_owner_id', 'Contact owner', 'enumeration', 'select'),
    prop_def('createdate', 'Create Date', 'datetime', 'date', read_only=True),
    prop_def('lastmodifieddate', 'Last Modified Date', 'datetime', 'date', read_only=True),
    prop_def('hs_object_id', 'Record ID', 'number', 'number', read_only=True),
    prop_def('hs_merged_object_ids', 'Merged Contact IDs', 'enumeration', 'checkbox', read_only=True),
    prop_def('hs_additional_emails', 'Additional email addresses', 'enumeration', 'checkbox', read_only=True),
    prop_def('hs_analytics_source', 'Original Traffic Source', 'enumeration', 'select', read_only=True),
    prop_def('lead_source', 'Lead source', 'string', 'text', hubspot=False, group='sales'),
    prop_def('legacy_lifecycle_stage', 'Stage (old CRM import)', 'string', 'text', hubspot=False, group='sales'),
]}


def new_id():
    COUNTER['id'] += 1
    return str(COUNTER['id'])


def add_contact(cid, first, last, email=None, phone=None, mobile=None, company=None, owner=None, source=None, legacy=None,
                stage=None, created=None):
    created = created or (BASE + timedelta(days=12 * (int(cid) - 1000)))
    props = {'firstname': first, 'lastname': last, 'email': email, 'phone': phone, 'mobilephone': mobile, 'company': company,
             'hubspot_owner_id': owner, 'lead_source': source, 'legacy_lifecycle_stage': legacy, 'lifecyclestage': stage,
             'createdate': iso(created), 'lastmodifieddate': iso(NOW - timedelta(days=3)), 'hs_object_id': cid}
    CONTACTS[cid] = {'id': cid, 'properties': {k: v for k, v in props.items() if v not in (None, '')},
                     'createdAt': iso(created), 'updatedAt': iso(NOW - timedelta(days=3)), 'archived': False}


def resolve(cid):
    cid = str(cid)
    hops = 0
    while cid not in CONTACTS and cid in ALIAS and hops < 50:
        cid, hops = ALIAS[cid], hops + 1
    return cid if cid in CONTACTS else None


def lookup(cid):
    """How the mock API answers a record ID in a request. HubSpot's API reference does not say what a request with an
    ID retired by a merge returns. HubSpot community answers say it returns the merged record (under its new ID), so
    that is the default here. With FAIL['old_ids_missing'] the mock answers retired IDs as not found instead, the other
    possible behavior, and the test checks that the workflow stays safe either way."""
    cid = str(cid)
    if FAIL['old_ids_missing']:
        return cid if cid in CONTACTS else None
    return resolve(cid)


def do_merge(primary, secondary):
    """HubSpot's documented merge: the primary's values win, its empty fields are filled from the secondary, the
    secondary email becomes an additional email, the furthest lifecycle stage is kept, and the merged record gets a NEW
    record ID. The old IDs are listed in hs_merged_object_ids and keep pointing to the merged record."""
    p, s = CONTACTS.pop(primary), CONTACTS.pop(secondary)
    nid = new_id()
    props = dict(p['properties'])
    for k, v in s['properties'].items():
        if props.get(k) in (None, '') and v not in (None, ''):
            props[k] = v
    if s['properties'].get('email') and s['properties']['email'] != props.get('email'):
        extra = [x for x in (p['properties'].get('hs_additional_emails', '').split(';') + [s['properties']['email']]) if x]
        props['hs_additional_emails'] = ';'.join(extra)
    a, b = p['properties'].get('lifecyclestage'), s['properties'].get('lifecyclestage')
    if a and b:
        props['lifecyclestage'] = a if STAGE_ORDER.get(a, -1) >= STAGE_ORDER.get(b, -1) else b
    props['createdate'] = min(p['properties']['createdate'], s['properties']['createdate'])
    history = [primary, secondary] + [x for x in (p['properties'].get('hs_merged_object_ids', '') + ';' + s['properties'].get('hs_merged_object_ids', '')).split(';') if x]
    props['hs_merged_object_ids'] = ';'.join(dict.fromkeys(history))
    props['hs_object_id'] = nid
    props['lastmodifieddate'] = iso(datetime.now(timezone.utc))
    CONTACTS[nid] = {'id': nid, 'properties': props, 'createdAt': props['createdate'], 'updatedAt': props['lastmodifieddate'], 'archived': False}
    for old in history:
        ALIAS[old] = nid
    return nid


def view(c, wanted):
    """A contact as the API returns it: the requested properties that have a value, plus the default ones."""
    props = {k: c['properties'][k] for k in set(wanted) | {'createdate', 'hs_object_id', 'lastmodifieddate'} if c['properties'].get(k) not in (None, '')}
    return {'id': c['id'], 'properties': props, 'createdAt': c['createdAt'], 'updatedAt': c['updatedAt'], 'archived': False}


def hs_error(status, category, message, **extra):
    return status, {'status': 'error', 'message': message, 'correlationId': str(uuid.uuid4()), 'category': category, **extra}


def bad(message, **extra):
    return hs_error(400, 'VALIDATION_ERROR', message, **extra)


def rate_headers():
    return {'X-HubSpot-RateLimit-Daily': '250000', 'X-HubSpot-RateLimit-Daily-Remaining': '249000',
            'X-HubSpot-RateLimit-Interval-Milliseconds': '10000', 'X-HubSpot-RateLimit-Max': '100',
            'X-HubSpot-RateLimit-Remaining': str(max(0, 100 - len(WINDOW)))}


RATE_LIMIT_BODY = {'status': 'error', 'message': 'You have reached your ten_secondly_rolling limit.', 'errorType': 'RATE_LIMIT',
                   'correlationId': 'c033cdaa-2c40-4a64-ae48-b4cec88dad24', 'policyName': 'TEN_SECONDLY_ROLLING',
                   'requestId': '3d3e35b7-0dae-4b9f-a6e3-9c230cbcf8dd'}
OBJECT_PATH = '/crm/v3/objects/contacts'


def hubspot(method, path, query, headers, body):
    """Returns (status, json). Validates like the HubSpot CRM v3 API and keeps state."""
    now = time.time()
    WINDOW.append(now)
    while WINDOW and WINDOW[0] < now - 10:
        WINDOW.popleft()
    RATE['max_in_window'] = max(RATE['max_in_window'], len(WINDOW))
    if headers.get('authorization') != 'Bearer ' + TOKEN or FAIL['auth']:
        return hs_error(401, 'INVALID_AUTHENTICATION', 'Authentication credentials not found. This API supports OAuth 2.0 authentication '
                        'and you can find more details at https://developers.hubspot.com/docs/methods/auth/oauth-overview')
    if len(WINDOW) > 100:
        return 429, dict(RATE_LIMIT_BODY)
    if method == 'POST' and not str(headers.get('content-type', '')).startswith('application/json'):
        return bad('Content-Type must be application/json')
    q = {k: v[0] for k, v in query.items()}

    def unknown_query(allowed):
        extra = sorted(set(q) - allowed)
        return bad(f'Unknown query parameter(s): {", ".join(extra)}') if extra else None

    def unknown_body(allowed, required=()):
        if not isinstance(body, dict):
            return bad('Invalid input JSON: the body must be an object')
        extra = sorted(set(body) - set(allowed))
        if extra:
            return bad(f'Invalid input JSON on line 1: Unrecognized field(s) {", ".join(extra)}')
        missing = [k for k in required if k not in body]
        return bad(f'Invalid input JSON: missing required field(s) {", ".join(missing)}') if missing else None

    # ---------------------------------------------------------------- owners
    if method == 'GET' and path in ('/crm/v3/owners', '/crm/v3/owners/'):
        err = unknown_query({'limit', 'after', 'archived', 'email'})
        if err:
            return err
        limit = int(q.get('limit', '100')) if q.get('limit', '100').isdigit() else 0
        if not 1 <= limit <= 500:
            return bad('limit must be from 1 to 500')
        archived = q.get('archived', 'false') == 'true'
        rows = [o for o in OWNERS if o['archived'] == archived]
        start = int(q.get('after', '0') or 0)
        page = rows[start:start + limit]
        out = {'results': copy.deepcopy(page)}
        if start + limit < len(rows):
            out['paging'] = {'next': {'after': str(start + limit), 'link': f'https://api.hubapi.com/crm/v3/owners?after={start + limit}'}}
        return 200, out

    # ---------------------------------------------------------------- property definitions
    if method == 'POST' and path == '/crm/v3/properties/contacts/batch/read':
        err = unknown_query({'locale'}) or unknown_body({'archived', 'dataSensitivity', 'inputs'}, ('archived', 'dataSensitivity', 'inputs'))
        if err:
            return err
        if body['dataSensitivity'] not in ('non_sensitive', 'sensitive', 'highly_sensitive') or not isinstance(body['archived'], bool):
            return bad('dataSensitivity must be non_sensitive, sensitive or highly_sensitive, and archived a boolean')
        names = [i.get('name') for i in body['inputs'] if isinstance(i, dict)]
        found = [copy.deepcopy(PROPS[n]) for n in names if n in PROPS]
        missing = [n for n in names if n not in PROPS]
        stamp = {'startedAt': iso(datetime.now(timezone.utc)), 'completedAt': iso(datetime.now(timezone.utc))}
        if missing:
            return 207, {'status': 'COMPLETE', 'results': found, 'numErrors': len(missing), **stamp,
                         'errors': [{'status': 'error', 'category': 'OBJECT_NOT_FOUND', 'message': f'Unable to find property {n}',
                                     'context': {'properties': [n]}, 'links': {}, 'errors': []} for n in missing]}
        return 200, {'status': 'COMPLETE', 'results': found, **stamp}

    # ---------------------------------------------------------------- list contacts
    if method == 'GET' and path == OBJECT_PATH:
        err = unknown_query({'limit', 'after', 'properties', 'propertiesWithHistory', 'associations', 'archived'})
        if err:
            return err
        if not re.fullmatch(r'\d{1,3}', q.get('limit', '10')) or not 1 <= int(q.get('limit', '10')) <= 100:
            return bad('limit must be a number from 1 to 100')
        if q.get('archived', 'false') != 'false':
            return bad('this mock only serves active contacts (archived=false)')
        if 'after' in q and not re.fullmatch(r'\d+', q['after']):
            return bad('after must be a record ID returned in paging.next.after')
        if 'after' in q and FAIL['list_429_once']:
            FAIL['list_429_once'] = False
            return 429, dict(RATE_LIMIT_BODY)
        limit = int(q.get('limit', '10'))
        wanted = [x for x in q.get('properties', 'firstname,lastname,email').split(',') if x]
        rows = sorted(CONTACTS.values(), key=lambda c: int(c['id']))
        if 'after' in q:
            rows = [c for c in rows if int(c['id']) >= int(q['after'])]
        page, rest = rows[:limit], rows[limit:]
        out = {'results': [view(c, wanted) for c in page]}
        if rest:
            out['paging'] = {'next': {'after': rest[0]['id'], 'link': f'https://api.hubapi.com/crm/v3/objects/contacts?after={rest[0]["id"]}'}}
        return 200, out

    # ---------------------------------------------------------------- batch read
    if method == 'POST' and path == OBJECT_PATH + '/batch/read':
        err = unknown_query({'archived'}) or unknown_body({'inputs', 'properties', 'propertiesWithHistory', 'idProperty'},
                                                          ('inputs', 'properties', 'propertiesWithHistory'))
        if err:
            return err
        inputs = body['inputs']
        if not isinstance(inputs, list) or not inputs or len(inputs) > 100 or any(not isinstance(i, dict) or set(i) != {'id'} for i in inputs):
            return bad('inputs must hold 1 to 100 objects like {"id": "123"}')
        if FAIL['merge_on_read'] and any(i['id'] in FAIL['merge_on_read'] for i in inputs):
            a, b = FAIL['merge_on_read']
            FAIL['merge_on_read'] = None
            do_merge(a, b)                      # somebody merges these two in HubSpot while the run is going
        results, missing = {}, []
        for i in inputs:
            rid = lookup(i['id'])
            if rid:
                results[rid] = view(CONTACTS[rid], body['properties'])
            else:
                missing.append(i['id'])
        stamp = {'startedAt': iso(datetime.now(timezone.utc)), 'completedAt': iso(datetime.now(timezone.utc))}
        if missing:
            return 207, {'status': 'COMPLETE', 'results': list(results.values()), 'numErrors': 1, **stamp, 'errors': [{
                'status': 'error', 'category': 'OBJECT_NOT_FOUND', 'links': {}, 'errors': [],
                'message': 'Could not get some CONTACT objects, they may be deleted or not exist. Check that ids are valid.',
                'context': {'ids': missing}}]}
        return 200, {'status': 'COMPLETE', 'results': list(results.values()), **stamp}

    # ---------------------------------------------------------------- merge
    if method == 'POST' and path == OBJECT_PATH + '/merge':
        err = unknown_query(set()) or unknown_body({'primaryObjectId', 'objectIdToMerge'}, ('primaryObjectId', 'objectIdToMerge'))
        if err:
            return err
        if not all(isinstance(body[k], str) and body[k] for k in ('primaryObjectId', 'objectIdToMerge')):
            return bad('primaryObjectId and objectIdToMerge must be non empty strings')
        sec = body['objectIdToMerge']
        if sec in FAIL['merge_429_once']:
            FAIL['merge_429_once'].discard(sec)
            return 429, dict(RATE_LIMIT_BODY)
        if sec in FAIL['merge_500']:
            return hs_error(500, 'INTERNAL_ERROR', 'internal error')
        p, s = lookup(body['primaryObjectId']), lookup(sec)
        if not p or not s:
            return hs_error(404, 'OBJECT_NOT_FOUND', 'Object not found.  objectId are usually numeric.',
                            context={'id': [body['primaryObjectId'] if not p else sec]})
        if p == s:
            # HubSpot's docs do not give the error for two IDs that already belong to one record; status and text are
            # the mock's own. The workflow does not depend on them: it reads the records again after a refused merge.
            return bad('These records are already merged: both IDs belong to one record.')
        nid = do_merge(p, s)
        if sec in FAIL['merge_lost_answer']:
            # HubSpot saved the merge, but the answer never arrived (a gateway timeout). A retry finds one record.
            FAIL['merge_lost_answer'].discard(sec)
            return 504, 'upstream request timeout'
        return 200, view(CONTACTS[nid], ['email', 'firstname', 'lastname'])

    # ---------------------------------------------------------------- batch update
    if method == 'POST' and path == OBJECT_PATH + '/batch/update':
        err = unknown_query(set()) or unknown_body({'inputs'}, ('inputs',))
        if err:
            return err
        inputs = body['inputs']
        if not isinstance(inputs, list) or not inputs or len(inputs) > 100:
            return bad('inputs must hold 1 to 100 records')
        if FAIL['update_daily']:
            return 429, dict(RATE_LIMIT_BODY, message='You have reached your daily limit.', policyName='DAILY')
        ids = [i.get('id') for i in inputs if isinstance(i, dict)]
        dups = sorted({x for x in ids if ids.count(x) > 1})
        if dups:
            return bad(f'Duplicate IDs found in batch input: {dups}')
        errors, good = [], []
        for i in inputs:
            problems = []
            if not isinstance(i, dict) or set(i) - {'id', 'idProperty', 'objectWriteTraceId', 'properties'} or 'id' not in i or not isinstance(i.get('properties'), dict):
                return bad('each input needs "id" and "properties" and nothing unknown')
            rid = lookup(i['id'])
            if not rid:
                problems.append(('OBJECT_NOT_FOUND', f'Contact {i["id"]} does not exist'))
            for k, v in i['properties'].items():
                d = PROPS.get(k)
                if not d:
                    problems.append(('PROPERTY_DOESNT_EXIST', f'Property "{k}" does not exist'))
                elif d['modificationMetadata']['readOnlyValue']:
                    problems.append(('READ_ONLY_VALUE', f'Property "{k}" is read only'))
                elif not isinstance(v, str):
                    problems.append(('INVALID_TYPE', f'Property "{k}" needs a text value'))
                elif k == 'hubspot_owner_id' and v and v not in ACTIVE_OWNER_IDS:
                    problems.append(('INVALID_OWNER_ID', f'{v} is not a valid owner id'))
                elif k == 'lifecyclestage' and v:
                    if v not in STAGE_ORDER:
                        problems.append(('INVALID_OPTION', f'{v} was not one of the allowed options'))
                    elif rid and STAGE_ORDER.get(CONTACTS[rid]['properties'].get('lifecyclestage', ''), -1) > STAGE_ORDER[v]:
                        # The docs say lifecycle stage only moves forward; the exact error text is the mock's own.
                        problems.append(('INVALID_OPTION', 'lifecyclestage can only move forward; clear it first to move it back'))
            if i['id'] in FAIL['update_invalid']:
                problems.append(('VALIDATION_ERROR', f'Property values were not valid for contact {i["id"]}'))
            if problems:
                errors.append((i, problems))
            else:
                good.append((i, rid))
        stamp = {'startedAt': iso(datetime.now(timezone.utc)), 'completedAt': iso(datetime.now(timezone.utc))}
        traced = all(isinstance(i, dict) and i.get('objectWriteTraceId') for i in inputs)
        # HubSpot documents partial success (207, refused inputs named by objectWriteTraceId) for batch CREATE. For batch
        # update the reference lists a 207 response but does not say when it is used. By default the mock answers like
        # batch create; with FAIL['update_atomic'] one bad input refuses the whole batch, the other possible behavior.
        if errors and (not traced or FAIL['update_atomic']):
            i, problems = errors[0]
            return bad('Property values were not valid: ' + json.dumps([{'isValid': False, 'message': m, 'error': c} for c, m in problems]))
        results = []
        for i, rid in good:
            c = CONTACTS[rid]
            for k, v in i['properties'].items():
                if v == '':
                    c['properties'].pop(k, None)
                else:
                    c['properties'][k] = v
            c['properties']['lastmodifieddate'] = c['updatedAt'] = iso(datetime.now(timezone.utc))
            results.append(view(c, list(i['properties'])))
        if errors:
            return 207, {'status': 'COMPLETE', 'results': results, 'numErrors': len(errors), **stamp, 'errors': [
                {'status': 'error', 'category': 'VALIDATION_ERROR', 'links': {}, 'errors': [],
                 'message': 'Property values were not valid: ' + json.dumps([{'isValid': False, 'message': m, 'error': c} for c, m in problems]),
                 'context': {'objectWriteTraceId': [i['objectWriteTraceId']]}} for i, problems in errors]}
        return 200, {'status': 'COMPLETE', 'results': results, **stamp}

    return hs_error(404, 'NOT_FOUND', f'No route for {method} {path}')


SLACK_KEYS = {'text', 'blocks', 'attachments', 'thread_ts', 'mrkdwn', 'unfurl_links', 'unfurl_media'}


def slack_problems(body):
    if not isinstance(body, dict) or set(body) - SLACK_KEYS:
        return 'invalid_payload'
    if not isinstance(body.get('text'), str) or not body['text'].strip():
        return 'no_text'
    blocks = body.get('blocks', [])
    if not isinstance(blocks, list) or len(blocks) > 50:
        return 'invalid_blocks'
    for b in blocks:
        t = b.get('text') if isinstance(b, dict) else None
        if not isinstance(b, dict) or b.get('type') not in ('section', 'divider', 'header', 'context', 'actions'):
            return 'invalid_blocks'
        if b['type'] == 'section' and (not isinstance(t, dict) or t.get('type') not in ('mrkdwn', 'plain_text') or not 1 <= len(t.get('text', '')) <= 3000):
            return 'invalid_blocks'
    return None


class Mock(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, obj, extra_headers=None):
        data = (obj if isinstance(obj, str) else json.dumps(obj)).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'text/plain' if isinstance(obj, str) else 'application/json;charset=utf-8')
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _handle(self, method):
        url = urlparse(self.path)
        raw = self.rfile.read(int(self.headers.get('Content-Length') or 0))
        headers = {k.lower(): v for k, v in self.headers.items()}
        try:
            body = json.loads(raw) if raw else None
        except ValueError:
            return self._send(400, {'status': 'error', 'message': 'Invalid input JSON', 'category': 'VALIDATION_ERROR'})
        path = url.path
        service = 'alert' if path.startswith('/slack/errors') else 'slack' if path.startswith('/slack') else 'hubspot'
        extra = {}
        with LOCK:
            if service == 'hubspot':
                status, resp = hubspot(method, path, parse_qs(url.query), headers, body)
                extra = rate_headers()
            else:
                problem = slack_problems(body) if method == 'POST' else 'invalid_payload'
                status, resp = (400, problem) if problem else (200, 'ok')
                if service == 'slack' and FAIL['slack_down']:
                    status, resp = 404, 'no_service'
            LOG.append({'service': service, 'method': method, 'path': path, 'query': {k: v[0] for k, v in parse_qs(url.query).items()},
                        'headers': headers, 'body': body, 'status': status, 'response': resp, 't': time.time()})
        return self._send(status, resp, extra)

    def do_GET(self):
        self._handle('GET')

    def do_POST(self):
        self._handle('POST')


CONNECTION_ERRORS = []


class MockServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        CONNECTION_ERRORS.append(repr(sys.exc_info()[1]))


# ----------------------------------------------------------------------------------------------------------------------
# Test data: 60 made up contacts (example.com addresses, 555 numbers, made up companies)
# ----------------------------------------------------------------------------------------------------------------------
def seed():
    # Exact duplicates
    add_contact('1001', 'Marcus', 'Lee', 'marcus.lee@mail.example.com', '+1 555 010 1001', company='Harborline Freight', source='Referral')
    add_contact('1002', 'Marcus', 'Lee', 'marcuslee+promo@mail.example.com')             # same mailbox: dots and +tag
    add_contact('1003', 'Marc', 'Lee', mobile='(555) 010-1001')                            # same phone, no email
    add_contact('1004', 'Jamie', 'Rivera', 'jamierivera+webinar@mail.example.com', owner='207', created=datetime(2026, 9, 1, tzinfo=timezone.utc))
    add_contact('1005', 'Jamie', 'Rivera', 'jamie.rivera@mail.example.com')                # created first, so it is kept
    add_contact('1006', 'Priya', 'Shah', 'priya.shah@brightpath.example.com', '+1 (555) 010-1006')
    add_contact('1007', 'Priya', 'Shah', phone='555.010.1006', owner='202')
    add_contact('1008', 'Nina', 'Gomez', 'nina.gomez@mail.example.com')
    add_contact('1009', 'Nina', 'Gomez', 'ninagomez@mail.example.com')
    add_contact('1010', 'Owen', 'Brooks', 'owen.brooks@larkspur.example.com', '+15550101010', owner='203')
    add_contact('1011', 'Owen', 'Brooks', phone='555-010-1010')
    add_contact('1012', 'O.', 'Brooks', mobile='1-555-010-1010')
    # Close matches
    add_contact('1013', 'Jordan', 'Ellis', 'jordan.ellis@northwind.example.com', company='Northwind Traders')
    add_contact('1014', 'Jordan', 'Ellis', 'jellis@northwind.example.com', company='Northwind Traders Inc.')
    add_contact('1015', 'Chris', 'Morgan', 'chris.morgan@globex.example.com', company='Globex', owner='201')
    add_contact('1016', 'Chris', 'Morgan', 'cmorgan@globex.example.com', company='Globex')
    add_contact('1017', 'Taylor', 'Brooks', 'taylor@fernhill.example.com', company='Fernhill R&D <Labs>', owner='203')
    add_contact('1018', 'Taylor', 'Brooks', 'tbrooks@fernhill.example.com', owner='203')
    add_contact('1019', 'Sam', 'Patel', 'sam.patel@cobaltworks.example.com', '+1 555 010 1019')
    add_contact('1020', 'Sam', 'Patel', 'spatel@quartzline.example.com', '(555) 010-1019')
    add_contact('1021', 'Jos\u00e9', 'N\u00fa\u00f1ez', 'jose.nunez@tidewater.example.com', company='Tidewater Supply & Co', owner='201')
    add_contact('1022', 'Jose', 'Nunez', 'jnunez@tidewater.example.com', company='Tidewater Supply', owner='202')
    # Look alike, but not duplicates
    add_contact('1023', 'Alex', 'Kim', 'alex.kim@mail.example.com')                        # free mail domains do not count
    add_contact('1024', 'Alex', 'Kim', 'alexkim77@inbox.example.com')
    add_contact('1025', 'Morgan', 'Price', 'morgan.price@acme.example.com', company='Acme', owner='207')
    add_contact('1026', 'Morgan', 'Price', 'mprice@initech.example.com', company='Initech')
    add_contact('1027', 'Lena', 'Ortiz', 'lena.ortiz@pinecrest.example.com', '000-000-0000')    # placeholder phones
    add_contact('1028', 'Hugo', 'Baker', 'hugo.baker@pinecrest.example.com', '000-000-0000', owner='202')
    # Lead source values
    for cid, first, last, owner, source in (
            ('1029', 'Ava', 'Ng', None, 'google ads'), ('1030', 'Ben', 'Hart', '201', 'AdWords'), ('1031', 'Cora', 'Diaz', None, ' PPC '),
            ('1032', 'Dev', 'Rao', '202', 'Facebook'), ('1033', 'Eli', 'Ward', None, 'word of mouth'), ('1034', 'Fay', 'Cho', '203', 'referral'),
            ('1035', 'Gus', 'Lane', None, 'Trade-Show'), ('1036', 'Hal', 'Moss', '207', 'Tradeshow booth 2024'),
            ('1037', 'Ivy', 'Bell', None, 'Referral'), ('1038', 'Jon', 'Pike', '201', 'Website'), ('1039', 'Kai', 'Roy', None, 'SEO')):
        add_contact(cid, first, last, f'{first.lower()}.{last.lower()}@clientco.example.com', owner=owner, source=source)
    # Lifecycle stage text from an old CRM import
    for cid, first, last, owner, legacy, stage in (
            ('1040', 'Lia', 'Fox', None, 'MQL', None), ('1041', 'Max', 'Cole', '202', 'Sales Qualified Lead', 'lead'),
            ('1042', 'Noa', 'Kerr', None, 'client', 'opportunity'), ('1043', 'Oli', 'Shaw', '203', 'MQL', 'customer'),
            ('1044', 'Pia', 'West', None, 'Lead', 'lead'), ('1045', 'Quin', 'Hays', '207', 'Hot lead!!', None),
            ('1046', 'Rex', 'Long', None, 'prospect', None), ('1047', 'Sia', 'Ford', '201', 'closed won', 'customer')):
        add_contact(cid, first, last, f'{first.lower()}.{last.lower()}@prospectco.example.com', owner=owner, legacy=legacy, stage=stage)
    add_contact('1048', 'Tom', 'Gray', 'tom.gray@prospectco.example.com', source='fb ads', legacy='sql', stage='marketingqualifiedlead')
    # Everyone else
    for cid, first, last, owner in (('1049', 'Riley', 'Quinn', None), ('1050', 'Sara', 'Lund', '202'), ('1051', 'Theo', 'Marsh', '203'),
                                    ('1052', 'Uma', 'Reyes', None), ('1053', 'Vik', 'Sen', '201'), ('1054', 'Wes', 'Tran', '207'),
                                    ('1055', 'Xia', 'Wong', None), ('1056', 'Yara', 'Bose', '202'), ('1057', 'Zed', 'Ames', '203'),
                                    ('1058', 'Ada', 'Ruiz', '201'), ('1059', 'Bea', 'Lowe', '207'), ('1060', 'Cal', 'Dunn', None)):
        add_contact(cid, first, last, None if cid == '1049' else f'{first.lower()}.{last.lower()}@regularco.example.com', owner=owner)
    emails = [c['properties'].get('email', '').lower() for c in CONTACTS.values() if c['properties'].get('email')]
    assert len(emails) == len(set(emails)), 'HubSpot keeps emails unique; the test data must too'
    assert len(CONTACTS) == 60


# What the first real run must do. "@1001" is the record kept for 1001's group, which gets a new ID when it merges.
EXPECTED_SLOTS_RUN1 = [['@1001'], ['1008', '1009'], ['1013', '1014'], ['1019', '1020'], ['1023'], ['1024'], ['1026'], ['1027'],
                       ['1029'], ['1031'], ['1033'], ['1035'], ['1037'], ['1039'], ['1040'], ['1042'], ['1044'], ['1046'], ['1048'],
                       ['1049'], ['1052'], ['1055'], ['1060']]
EXPECTED_FIXES_RUN1 = {
    '1029': {'lead_source': 'Paid Search'}, '1030': {'lead_source': 'Paid Search'}, '1031': {'lead_source': 'Paid Search'},
    '1032': {'lead_source': 'Paid Social'}, '1033': {'lead_source': 'Referral'}, '1034': {'lead_source': 'Referral'},
    '1035': {'lead_source': 'Event'}, '1039': {'lead_source': 'Organic Search'},
    '1040': {'lifecyclestage': 'marketingqualifiedlead'}, '1041': {'lifecyclestage': 'salesqualifiedlead'},
    '1042': {'lifecyclestage': 'customer'}, '1046': {'lifecyclestage': 'lead'},
    '1048': {'lead_source': 'Paid Social', 'lifecyclestage': 'salesqualifiedlead'},
}
CONTACT_PROPERTIES = ['email', 'firstname', 'lastname', 'phone', 'mobilephone', 'company', 'lifecyclestage', 'hubspot_owner_id',
                      'createdate', 'hs_merged_object_ids', 'lead_source', 'legacy_lifecycle_stage']


# ----------------------------------------------------------------------------------------------------------------------
# n8n helpers
# ----------------------------------------------------------------------------------------------------------------------
def api(method, path, body=None):
    req = urllib.request.Request(N8N + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={'X-N8N-API-KEY': KEY, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=60) as r:
        text = r.read().decode()
        return json.loads(text) if text else {}


def test_copy(env, suffix=''):
    """The workflow with $env values replaced by test values, the API host pointed at the mock and test webhook paths."""
    wf = json.loads(WORKFLOW.read_text(encoding='utf-8'))

    def fix(value):
        if isinstance(value, dict):
            return {k: fix(v) for k, v in value.items()}
        if isinstance(value, list):
            return [fix(v) for v in value]
        if not isinstance(value, str):
            return value
        value = re.sub(r'\$env\.([A-Z0-9_]+)', lambda m: json.dumps(env.get(m.group(1), '')), value)
        for real, mock in HOSTS.items():
            value = value.replace(real, mock)
        return value

    for node in wf['nodes']:
        node['parameters'] = fix(node.get('parameters', {}))
        if 'webhookId' in node:
            node['webhookId'] = str(uuid.uuid4())
        if node['type'] == 'n8n-nodes-base.webhook':
            node['parameters']['path'] = f"{SLUG}-e2e-{'run' if node['name'] == 'Run Now' else 'approval'}{suffix}"
        if node['type'] == 'n8n-nodes-base.scheduleTrigger':
            node['parameters']['rule'] = {'interval': [{'field': 'minutes', 'minutesInterval': 1}]}   # so the test does not wait a day
    return wf


def import_active(env, suffix, error_wid):
    wf = test_copy(env, suffix)
    settings = {**wf.get('settings', {}), 'errorWorkflow': error_wid}
    wid = api('POST', '/api/v1/workflows', {'name': TEMP_NAME, 'nodes': wf['nodes'], 'connections': wf['connections'], 'settings': settings})['id']
    api('POST', f'/api/v1/workflows/{wid}/activate')
    time.sleep(2)
    return wid


def import_error_workflow():
    wf = json.loads((WORKFLOW.parent / 'error-alert-slack.json').read_text(encoding='utf-8'))
    for node in wf['nodes']:
        node['parameters'] = json.loads(json.dumps(node.get('parameters', {})).replace('$env.SLACK_WEBHOOK_URL', json.dumps(MOCK + '/slack/errors').replace('"', '\\"')))
    wid = api('POST', '/api/v1/workflows', {'name': TEMP_NAME, 'nodes': wf['nodes'], 'connections': wf['connections'], 'settings': wf.get('settings', {})})['id']
    api('POST', f'/api/v1/workflows/{wid}/activate')
    return wid


def remove(wid):
    try:
        api('POST', f'/api/v1/workflows/{wid}/deactivate')
    finally:
        api('DELETE', f'/api/v1/workflows/{wid}')


def remove_leftovers():
    cursor, found = None, []
    while True:
        page = api('GET', '/api/v1/workflows?limit=250' + (f'&cursor={cursor}' if cursor else ''))
        found += [w['id'] for w in page.get('data', []) if w.get('name') == TEMP_NAME]
        cursor = page.get('nextCursor')
        if not cursor:
            break
    for wid in found:
        remove(wid)
    return len(found)


def static_of(wid):
    st = api('GET', f'/api/v1/workflows/{wid}').get('staticData') or {}
    st = json.loads(st) if isinstance(st, str) else st
    return st.get('global') or {}


def update_env(wid, env, suffix=''):
    """Swaps the settings of the active test copy and keeps its memory (static data), node IDs and webhook IDs, as if
    the n8n environment had been changed and n8n restarted."""
    cur = api('GET', f'/api/v1/workflows/{wid}')
    fresh = {n['name']: n for n in test_copy(env, suffix)['nodes']}
    for n in cur['nodes']:
        n['parameters'] = fresh[n['name']]['parameters']
    settings = {k: v for k, v in (cur.get('settings') or {}).items() if k in ('executionOrder', 'errorWorkflow')}
    st = cur.get('staticData') or {}
    api('PUT', f'/api/v1/workflows/{wid}', {'name': cur['name'], 'nodes': cur['nodes'], 'connections': cur['connections'],
                                           'settings': settings, 'staticData': json.loads(st) if isinstance(st, str) else st})
    time.sleep(2)


def edit_static(wid, change):
    """Changes the workflow's memory (static data) through the n8n API, keeping the workflow active. Used to let a link
    expire without waiting 72 hours, and to give a copy a pair that waits for a decision."""
    cur = api('GET', f'/api/v1/workflows/{wid}')
    st = cur.get('staticData') or {}
    st = json.loads(st) if isinstance(st, str) else st
    change(st.setdefault('global', {}))
    settings = {k: v for k, v in (cur.get('settings') or {}).items() if k in ('executionOrder', 'errorWorkflow')}
    api('PUT', f'/api/v1/workflows/{wid}', {'name': cur['name'], 'nodes': cur['nodes'], 'connections': cur['connections'],
                                           'settings': settings, 'staticData': st})
    time.sleep(2)


def alerts_for(ids, timeout=90):
    end = time.time() + timeout
    while True:
        got = [e for e in LOG if e['service'] == 'alert']
        linked = {i: [e for e in got if re.search(rf'/executions/{i}(?!\d)', e['body']['text'])] for i in ids}
        if all(linked.values()) or time.time() > end:
            return linked, got
        time.sleep(1)


def b64url(obj):
    return base64.urlsafe_b64encode(json.dumps(obj, separators=(',', ':')).encode()).decode().rstrip('=')


def sign(part):
    return hmac.new(SECRET.encode(), part.encode(), hashlib.sha256).hexdigest()


def token_payload(token):
    part = token.split('.')[0]
    return json.loads(base64.urlsafe_b64decode(part + '=' * (-len(part) % 4)))


class Runner:
    def __init__(self, wid, suffix=''):
        self.wid, self.suffix, self.sent, self.results, self.mark = wid, suffix, 0, [], 0
        self.ids = {}

    def executions(self):
        return api('GET', f'/api/v1/executions?workflowId={self.wid}&limit=250').get('data', [])

    def wait(self, timeout=180):
        end = time.time() + timeout
        while time.time() < end:
            done = [e for e in self.executions() if e.get('mode') == 'webhook' and e.get('stoppedAt')]
            if len(done) >= self.sent:
                return max(done, key=lambda e: int(e['id']))
            time.sleep(0.4)
        raise TimeoutError('the workflow did not finish in time')

    def call(self, method, which, payload=None, headers=None, query=None, form=None):
        self.mark = len(LOG)
        url = f'{N8N}/webhook/{SLUG}-e2e-{which}{self.suffix}' + (('?' + urllib.parse.urlencode(query)) if query else '')
        data, h = None, dict(headers or {})
        if payload is not None:
            data = json.dumps(payload).encode()
            h['Content-Type'] = 'application/json'
        if form is not None:
            data = urllib.parse.urlencode(form).encode()
            h['Content-Type'] = 'application/x-www-form-urlencoded'
        req = urllib.request.Request(url, method=method, data=data, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                status, text, rh = r.status, r.read().decode(), {k.lower(): v for k, v in r.headers.items()}
        except urllib.error.HTTPError as e:
            status, text, rh = e.code, e.read().decode(), {k.lower(): v for k, v in e.headers.items()}
        self.sent += 1
        return status, text, rh, self.wait()

    def run(self, dry=None, secret='test-run-secret'):
        status, text, _, ex = self.call('POST', 'run', {'dryRun': True} if dry else {}, {'X-Run-Secret': secret} if secret is not None else {})
        return status, text, ex

    def open_link(self, token):
        return self.call('GET', 'approval', query={'token': token})

    def press(self, token):
        return self.call('POST', 'approval', form={'token': token})

    def stopped_at(self, ex):
        data = api('GET', f"/api/v1/executions/{ex['id']}?includeData=true").get('data') or {}
        result = data.get('resultData') or {}
        return result.get('lastNodeExecuted'), (result.get('error') or {}).get('message', '')

    def new(self, service=None, method=None, path=None):
        out = []
        for e in LOG[self.mark:]:
            if e['service'] == 'alert' and service != 'alert':
                continue
            if service and e['service'] != service or method and e['method'] != method:
                continue
            if path and not re.fullmatch(path, e['path']):
                continue
            out.append(e)
        return out

    def check(self, scenario, label, ok, detail=''):
        self.results.append((scenario, label, bool(ok), detail))


def hs_writes(entries):
    return [e for e in entries if e['service'] == 'hubspot' and e['method'] == 'POST' and e['path'].endswith(('/merge', '/batch/update'))]


def merges_in(entries):
    return [e for e in entries if e['service'] == 'hubspot' and e['path'] == OBJECT_PATH + '/merge']


def updates_in(entries):
    return [e for e in entries if e['service'] == 'hubspot' and e['path'] == OBJECT_PATH + '/batch/update']


def slack_texts(entries):
    return [e['body']['text'] for e in entries if e['service'] == 'slack']


def applied_inputs(entries):
    """Every input of every batch update HubSpot accepted, by contact ID."""
    out = {}
    for e in updates_in(entries):
        if e['status'] in (200, 207):
            refused = {t for err in (e['response'].get('errors') or []) for t in err['context'].get('objectWriteTraceId', [])}
            for i in e['body']['inputs']:
                if i['objectWriteTraceId'] not in refused:
                    out[i['id']] = i['properties']
    return out


def expected_assignments(slots, pool, start_after=None):
    """Round robin over the pool in order, one owner per slot, starting after the last owner used."""
    idx = pool.index(start_after) if start_after in pool else -1
    out = {}
    for slot in slots:
        idx = (idx + 1) % len(pool)
        for cid in slot:
            out[cid] = pool[idx]
    return out


def approval_links(text_blocks):
    """(pair key, approve token, skip token) per section of the approval message."""
    out = []
    for b in text_blocks[1:]:
        t = b['text']['text']
        a = re.search(r'<([^|>]+)\|Approve merge>', t)
        s = re.search(r'<([^|>]+)\|Skip>', t)
        if a and s:
            ta = parse_qs(urlparse(a.group(1)).query)['token'][0]
            ts = parse_qs(urlparse(s.group(1)).query)['token'][0]
            out.append((token_payload(ta)['k'], ta, ts, t))
    return out


# ----------------------------------------------------------------------------------------------------------------------
# Scenarios
# ----------------------------------------------------------------------------------------------------------------------
def run(r):
    # ---------------------------------------------------------------- refused
    status, body, ex = r.run(secret='wrong-secret')
    node, _ = r.stopped_at(ex)
    r.check('Run Now, wrong secret', 'refused with 401, nothing read or changed', status == 401 and not r.new() and node == 'Stop: Bad Run Secret', f'{status}, ended at {node}')

    # ---------------------------------------------------------------- dry run
    before_static = static_of(r.wid)
    status, body, ex = r.run(dry=True)
    node, _ = r.stopped_at(ex)
    lists = r.new('hubspot', 'GET', OBJECT_PATH)
    owners = r.new('hubspot', 'GET', r'/crm/v3/owners/?')
    props = r.new('hubspot', 'POST', '/crm/v3/properties/contacts/batch/read')
    texts = slack_texts(r.new())
    summary = texts[-1] if texts else ''
    r.check('Dry run', 'answers 202 at once, then only reads: no merge, no update, no approval request, memory unchanged',
            status == 202 and json.loads(body) == {'started': True, 'dryRun': True} and not hs_writes(r.new()) and len(texts) == 1
            and static_of(r.wid) == before_static and node == 'Any Problems?', f'{status} {body}, ended at {node}')
    q = lists[0]['query'] if lists else {}
    r.check('Dry run', 'contacts are read page by page (limit 25, archived=false, the properties it needs, the after cursor)',
            len(lists) == 3 and q.get('limit') == '25' and q.get('archived') == 'false' and sorted(q.get('properties', '').split(',')) == sorted(CONTACT_PROPERTIES)
            and 'after' not in q and [e['query'].get('after') for e in lists[1:]] == ['1026', '1051'] and all(e['status'] == 200 for e in lists),
            f"{len(lists)} pages, after = {[e['query'].get('after') for e in lists]}")
    r.check('Dry run', 'owners and property definitions read with the documented parameters',
            len(owners) == 1 and owners[0]['query'] == {'limit': '25', 'archived': 'false'} and len(props) == 1
            and props[0]['body'] == {'archived': False, 'dataSensitivity': 'non_sensitive', 'inputs': [{'name': 'lifecyclestage'}, {'name': 'lead_source'}, {'name': 'legacy_lifecycle_stage'}]},
            json.dumps(props[0]['body']) if props else 'no property read')
    r.check('Dry run', 'Slack report says DRY RUN and lists what a real run would do',
            summary.startswith('*HubSpot cleanup* (Run Now) *DRY RUN*: nothing was changed.')
            and 'Before and after (if run for real): 60 to 56 contacts, 33 to 3 without an owner, 16 to 2 messy field values' in summary
            and 'Would merge: 4 exact duplicates' in summary and '\u2022 Marcus Lee: keep 1001, merge in 1002, 1003 (same email and same phone)' in summary
            and '\u2022 Jamie Rivera: keep 1005, merge in 1004 (same email)' in summary
            and 'Not merged this run: 3 exact duplicates over the cap of 4' in summary and 'Would ask for approval: 5 close matches' in summary
            and 'Would fix: 14 (lead source 9, lifecycle stage 5)' in summary
            and 'Would assign owners: 26 (Ava Lopez 9, Ben Ortiz 9, Cleo Park 8). Away: Dan Reed' in summary and 'Problems: none' in summary,
            summary.splitlines()[1] if summary.count('\n') else summary[:120])

    # ---------------------------------------------------------------- real run 1
    FAIL['list_429_once'] = True
    FAIL['merge_429_once'].add('1002')
    status, body, ex = r.run()
    node, error = r.stopped_at(ex)
    lists = r.new('hubspot', 'GET', OBJECT_PATH)
    merges = merges_in(r.new())
    r.check('Real run 1', 'answers 202 with dryRun false, ends without problems', status == 202 and json.loads(body) == {'started': True, 'dryRun': False}
            and node == 'Any Problems?' and ex['status'] == 'success', f'{status} {body}, ended at {node}')
    r.check('Real run 1', 'HubSpot 429 on page 2 of the contacts: the read waits and starts again, then all 60 contacts are read',
            [e['status'] for e in lists] == [200, 429, 200, 200, 200] and lists[2]['t'] - lists[1]['t'] >= 4.5,
            f"statuses {[e['status'] for e in lists]}, waited {round(lists[2]['t'] - lists[1]['t'], 1) if len(lists) > 2 else '?'}s")
    n1 = merges[1]['response']['id'] if len(merges) > 1 and merges[1]['status'] == 200 else None
    r.check('Real run 1', 'HubSpot 429 on a merge: it backs off and tries again; the merge happens once',
            [e['status'] for e in merges[:2]] == [429, 200] and merges[0]['body'] == merges[1]['body'] == {'primaryObjectId': '1001', 'objectIdToMerge': '1002'}
            and merges[1]['t'] - merges[0]['t'] >= 0.45, f"statuses {[e['status'] for e in merges]}")
    r.check('Real run 1', 'exact merges with the documented body; the second merge of a group goes into the NEW ID the first returned; the record created first is kept',
            [e['body'] for e in merges[1:]] == [{'primaryObjectId': '1001', 'objectIdToMerge': '1002'}, {'primaryObjectId': n1, 'objectIdToMerge': '1003'},
                                               {'primaryObjectId': '1005', 'objectIdToMerge': '1004'}, {'primaryObjectId': '1006', 'objectIdToMerge': '1007'}]
            and all(e['status'] == 200 for e in merges[1:]), json.dumps([e['body'] for e in merges[1:]])[:150])
    r.check('Real run 1', 'merge cap: exactly MAX_MERGES_PER_RUN (4) merges, the other 3 wait (Nina Gomez, Owen Brooks group still separate)',
            len([e for e in merges if e['status'] == 200]) == 4 and all(resolve(x) == x for x in ('1008', '1009', '1010', '1011', '1012')),
            f"{len([e for e in merges if e['status'] == 200])} merges")
    checks = r.new('hubspot', 'POST', OBJECT_PATH + '/batch/read')
    r.check('Real run 1', 'each planned merge is checked with a fresh batch read before merging, and the merged records are read again after',
            len(checks) == 2 and sorted(i['id'] for i in checks[0]['body']['inputs']) == ['1001', '1002', '1003', '1004', '1005', '1006', '1007']
            and checks[0]['body']['propertiesWithHistory'] == [] and checks[0]['t'] < merges[0]['t']
            and sorted(i['id'] for i in checks[1]['body']['inputs']) == sorted(e['response']['id'] for e in merges if e['status'] == 200)
            and checks[1]['t'] > merges[-1]['t'], f'{len(checks)} batch reads')
    survivor = resolve('1001')
    slots = [[survivor if x == '@1001' else x for x in slot] for slot in EXPECTED_SLOTS_RUN1]
    want_owner = expected_assignments(slots, POOL)
    want = {cid: dict(EXPECTED_FIXES_RUN1.get(cid, {})) for cid in set(EXPECTED_FIXES_RUN1) | set(want_owner)}
    for cid, owner in want_owner.items():
        want[cid]['hubspot_owner_id'] = owner
    got = applied_inputs(r.new())
    ups = updates_in(r.new())
    r.check('Real run 1', 'field fixes only where a value changes, and only the changed properties (clean values, already right stages, unmapped values and backward moves are left alone)',
            {k: v for k, v in got.items() if k in EXPECTED_FIXES_RUN1} == {k: want[k] for k in EXPECTED_FIXES_RUN1}
            and not any(k in got for k in ('1036', '1038', '1043', '1045', '1047')) and got.get('1037') == {'hubspot_owner_id': want_owner['1037']}
            and got.get('1044') == {'hubspot_owner_id': want_owner['1044']}, f"{len(got)} contacts updated")
    r.check('Real run 1', 'round robin: every unowned contact gets the next owner (Ava, Ben, Cleo; Dan is away), oldest first, one owner per set of duplicates, none when a duplicate already has an owner',
            got == want and all(i.get('objectWriteTraceId') == f"contact-{i['id']}" for e in ups for i in e['body']['inputs'])
            and len(ups) == 1 and CONTACTS[resolve('1004')]['properties'].get('hubspot_owner_id') == '207'
            and CONTACTS[resolve('1006')]['properties'].get('hubspot_owner_id') == '202'
            and not any(CONTACTS[x]['properties'].get('hubspot_owner_id') for x in ('1011', '1012', '1016')),
            f"{len(want)} expected, {len(got)} sent, last owner {want_owner[slots[-1][0]]}")
    texts = slack_texts(r.new())
    ask = [e for e in r.new('slack') if e['body'].get('blocks')]
    links = approval_links(ask[0]['body']['blocks']) if ask else []
    r.check('Real run 1', 'close matches go to Slack in one message: 5 pairs, each with an Approve and a Skip link, contact text escaped',
            len(ask) == 1 and [k for k, *_ in links] == ['1013|1014', '1015|1016', '1017|1018', '1019|1020', '1021|1022']
            and 'Fernhill R&amp;D &lt;Labs&gt;' in links[2][3] and '<https://app.hubspot.com/contacts/4400001/record/0-1/1013|record 1013>' in links[0][3]
            and ask[0]['body']['text'].startswith('HubSpot: 5 possible duplicates need a decision') and ask[0]['t'] < [e for e in r.new('slack')][-1]['t'],
            ask[0]['body']['blocks'][0]['text']['text'].splitlines()[0] if ask else 'no approval message')
    tokens_ok = bool(links)
    for key, ta, ts, _ in links:
        for tok, action in ((ta, 'approve'), (ts, 'skip')):
            part, sig = tok.split('.')
            p = token_payload(tok)
            tokens_ok &= hmac.compare_digest(sign(part), sig) and p['a'] == action and p['k'] == key and p['v'] == 1 \
                and abs(p['e'] - (time.time() + 72 * 3600)) < 300
    st = static_of(r.wid)
    r.check('Real run 1', 'each link carries a token signed with HMAC SHA-256 (verified here with the secret): pair, action, nonce, expiry in 72 hours; pairs saved as waiting',
            tokens_ok and sorted(st.get('approvals', {})) == [k for k, *_ in links] and all(e['status'] == 'waiting' for e in st['approvals'].values())
            and st.get('rotation', {}).get('lastOwnerId') == '202', f"rotation {st.get('rotation', {}).get('lastOwnerId')}")
    summary = texts[-1] if texts else ''
    r.check('Real run 1', 'Slack summary: merged, waiting for approval, fields fixed, owners assigned, before and after counts',
            summary.startswith('*HubSpot cleanup* (Run Now)\n')
            and 'Before and after: 60 to 56 contacts, 33 to 3 without an owner, 16 to 2 messy field values' in summary
            and 'Merged: 4 exact duplicates' in summary and f'\u2022 Marcus Lee: kept 1001, merged in 1002, 1003 (same email and same phone), now record {survivor}' in summary
            and 'Waiting for approval: 5 close matches (5 sent now, 0 from earlier runs)' in summary
            and 'Fields fixed: 14 (lead source 9, lifecycle stage 5)' in summary
            and 'Values not in the mapping table (left as they are): lead source "Tradeshow booth 2024" (1), lifecycle stage "Hot lead!!" (1)' in summary
            and 'Lifecycle stage kept for 1 contact' in summary and 'Owners assigned: 26 (Ava Lopez 9, Ben Ortiz 9, Cleo Park 8). Away: Dan Reed' in summary
            and 'Left unowned: 3 contacts that duplicate a record with an owner' in summary and 'Problems: none' in summary,
            summary.splitlines()[1] if summary.count('\n') else summary[:100])
    pair = {k: (ta, ts) for k, ta, ts, _ in links}
    if len(pair) < 5:
        return

    # ---------------------------------------------------------------- approval links
    status, page, headers, ex = r.open_link(pair['1013|1014'][0])
    node, _ = r.stopped_at(ex)
    r.check('Approve link opened (GET)', 'shows a confirm page with both records and a button; nothing changes in HubSpot or in memory',
            status == 200 and 'Merge these two contacts?' in page and 'jordan.ellis@northwind.example.com' in page and 'jellis@northwind.example.com' in page
            and f'action="{ENV["APPROVAL_WEBHOOK_URL"]}"' in page and f'name="token" value="{pair["1013|1014"][0]}"' in page
            and not r.new('hubspot') and static_of(r.wid)['approvals']['1013|1014']['status'] == 'waiting'
            and headers.get('referrer-policy') == 'no-referrer' and headers.get('cache-control') == 'no-store' and headers.get('content-type', '').startswith('text/html'),
            f'{status}, ended at {node}')
    status, page, _, ex = r.press(pair['1013|1014'][0])
    node, _ = r.stopped_at(ex)
    m = merges_in(r.new())
    reads = r.new('hubspot', 'POST', OBJECT_PATH + '/batch/read')
    notes = slack_texts(r.new())
    new_c1 = m[0]['response']['id'] if m and m[0]['status'] == 200 else None
    r.check('Approve pressed (POST)', 'both records read again, then merged; page and Slack note confirm it; the pair is marked merged',
            status == 200 and '<h1>Merged</h1>' in page and len(reads) == 1 and sorted(i['id'] for i in reads[0]['body']['inputs']) == ['1013', '1014']
            and len(m) == 1 and m[0]['body'] == {'primaryObjectId': '1013', 'objectIdToMerge': '1014'} and resolve('1014') == new_c1
            and len(notes) == 1 and notes[0] == f'*Duplicate approved and merged*: Jordan Ellis. Kept record 1013, merged in 1014, new record ID {new_c1}.'
            and static_of(r.wid)['approvals']['1013|1014']['status'] == 'merged' and node == 'Slack: Decision Note', f'{status}, ended at {node}')
    status, page, _, ex = r.press(pair['1013|1014'][0])
    node, _ = r.stopped_at(ex)
    status2, page2, _, ex2 = r.open_link(pair['1013|1014'][0])
    r.check('Approve link used again', 'refused with 409 (POST and GET), nothing read or merged, no alert',
            status == 409 and 'Already merged' in page and status2 == 409 and not r.new('hubspot') and node == 'Respond: Approval Page'
            and ex['status'] == 'success', f'{status}/{status2}, ended at {node}')

    status, page, _, ex = r.open_link(pair['1015|1016'][1])
    r.check('Skip link opened (GET)', 'confirm page for skipping, nothing changes', status == 200 and 'Skip this pair?' in page and not r.new('hubspot')
            and static_of(r.wid)['approvals']['1015|1016']['status'] == 'waiting', f'{status}')
    status, page, _, ex = r.press(pair['1015|1016'][1])
    node, _ = r.stopped_at(ex)
    notes = slack_texts(r.new())
    r.check('Skip pressed (POST)', 'pair remembered as skipped, HubSpot not touched, Slack note posted',
            status == 200 and '<h1>Skipped</h1>' in page and not r.new('hubspot') and static_of(r.wid)['approvals']['1015|1016']['status'] == 'skipped'
            and notes == ['*Duplicate skipped*: Chris Morgan (record 1015) and Chris Morgan (record 1016) are kept apart and will not be suggested again.'],
            f'{status}, ended at {node}')

    ta = pair['1019|1020'][0]
    payload = token_payload(ta)
    forged_payload = dict(payload, s='1022')
    forged = b64url(forged_payload) + '.' + ta.split('.')[1]
    status, page, _, ex = r.press(forged)
    r.ids['forged'] = ex['id']
    node, _ = r.stopped_at(ex)
    r.check('Forged link (pair changed, old signature)', 'refused with 403, nothing read or merged, run fails for the alert',
            status == 403 and 'This link is not valid' in page and not r.new('hubspot') and node == 'Stop: Forged Approval Link' and ex['status'] == 'error', f'{status}, ended at {node}')
    status, page, _, ex = r.open_link('not-a-real-token')
    node, _ = r.stopped_at(ex)
    r.check('Forged link (garbage token)', 'refused with 403, alert', status == 403 and not r.new('hubspot') and node == 'Stop: Forged Approval Link', f'{status}')
    expired_part = b64url(dict(payload, e=int(time.time()) - 60))
    status, page, _, ex = r.press(expired_part + '.' + sign(expired_part))
    node, _ = r.stopped_at(ex)
    r.check('Expired link (valid signature)', 'refused with 410, nothing read or merged, no alert',
            status == 410 and 'This link has expired' in page and not r.new('hubspot') and ex['status'] == 'success', f'{status}, ended at {node}')
    old_part = b64url(dict(payload, n='an-older-nonce'))
    status, page, _, ex = r.press(old_part + '.' + sign(old_part))
    r.check('Replaced link (valid signature, older nonce)', 'refused with 409, nothing done',
            status == 409 and 'no longer active' in page and not r.new('hubspot') and static_of(r.wid)['approvals']['1019|1020']['status'] == 'waiting', f'{status}')

    status, page, _, ex = r.open_link(pair['1017|1018'][0])
    r.check('Confirm page escapes contact text', 'the company "Fernhill R&D <Labs>" shows as text in the HTML',
            status == 200 and 'Fernhill R&amp;D &lt;Labs&gt;' in page and '<Labs>' not in page, f'{status}')
    with LOCK:
        someone = do_merge('1017', '1018')       # a rep merges the two in HubSpot before the manager presses Approve
    status, page, _, ex = r.press(pair['1017|1018'][0])
    node, _ = r.stopped_at(ex)
    notes = slack_texts(r.new())
    r.check('Approved after someone merged them in HubSpot', 'the fresh read shows one record: nothing merged, page and Slack say so, not a failure',
            status == 200 and 'Already merged' in page and not merges_in(r.new()) and len(r.new('hubspot', 'POST', OBJECT_PATH + '/batch/read')) == 1
            and notes and notes[0].startswith('*Duplicate approved, already merged*: Taylor Brooks') and ex['status'] == 'success'
            and static_of(r.wid)['approvals']['1017|1018']['status'] == 'already_merged', f'{status}, merged by hand into {someone}, ended at {node}')

    # ---------------------------------------------------------------- real run 2: failures, a merge by someone else, new leads
    add_contact('1061', 'Lena', 'Fox', 'lena.fox@newleads.example.com', source='linkedin', created=NOW - timedelta(hours=3))
    add_contact('1062', 'Omar', 'Haddad', 'omar.haddad@newleads.example.com', created=NOW - timedelta(hours=2))
    add_contact('1063', 'Ruth', 'Kemp', 'ruth.kemp@newleads.example.com', created=NOW - timedelta(hours=1))
    FAIL['merge_on_read'] = ('1008', '1009')
    FAIL['merge_500'].add('1012')
    FAIL['update_invalid'].add('1062')
    status, body, ex = r.run()
    r.ids['run2'] = ex['id']
    node, error = r.stopped_at(ex)
    m = merges_in(r.new())
    owen = m[0]['response']['id'] if m and m[0]['status'] == 200 else None
    r.check('Real run 2', 'a pair merged by someone else during the run is skipped (fresh read before merging), not merged again, not a failure',
            not any(e['body']['objectIdToMerge'] == '1009' for e in m) and resolve('1008') == resolve('1009'), 'Nina Gomez')
    r.check('Real run 2', 'a merge that keeps failing (500) is tried 5 times with growing waits (0.5, 1, 2, 4 s)',
            [e['status'] for e in m] == [200, 500, 500, 500, 500, 500] and m[0]['body'] == {'primaryObjectId': '1010', 'objectIdToMerge': '1011'}
            and all(e['body'] == {'primaryObjectId': owen, 'objectIdToMerge': '1012'} for e in m[1:])
            and all(b['t'] - a['t'] >= w for (a, b), w in zip(zip(m[1:], m[2:]), (0.45, 0.95, 1.95, 3.95))),
            f"statuses {[e['status'] for e in m]}, gaps {[round(b['t'] - a['t'], 1) for a, b in zip(m[1:], m[2:])]}")
    got = applied_inputs(r.new())
    ups = updates_in(r.new())
    r.check('Real run 2', 'round robin continues after the last owner of run 1 (Ben): Cleo, Ava, Ben, Cleo; the skipped pair\'s contact is now routed',
            [(i['id'], i['properties'].get('hubspot_owner_id')) for e in ups for i in e['body']['inputs']]
            == [('1016', '203'), ('1061', '201'), ('1062', '202'), ('1063', '203')]
            and ups[0]['status'] == 207 and got.get('1061') == {'lead_source': 'Paid Social', 'hubspot_owner_id': '201'} and '1062' not in got,
            f"{[(i['id'], i['properties'].get('hubspot_owner_id')) for e in ups for i in e['body']['inputs']]}")
    summary = (slack_texts(r.new()) or [''])[-1]
    r.check('Real run 2', 'nothing half done goes unreported: the summary names the merge that worked, the one that failed, the update HubSpot refused and the pair merged by someone else',
            f'\u2022 Owen Brooks: kept 1010, merged in 1011 (same phone), now record {owen}' in summary
            and f'Merge failed: O. Brooks (1012) into Owen Brooks ({owen}), after 1 other merge of this group worked: 500 internal error (5 tries)' in summary
            and 'Not merged: Nina Gomez (1009) and Nina Gomez (1008): already merged in HubSpot by someone else' in summary
            and 'HubSpot did not save the update for 1 contact(s): Omar Haddad (1062)' in summary
            and 'Waiting for approval: 2 close matches (0 sent now, 2 from earlier runs)' in summary
            and 'Owners assigned: 3 (Cleo Park 2, Ava Lopez 1)' in summary and '*Problems: 2*' in summary,
            summary.splitlines()[-1][:120] if summary else 'no summary')
    st = static_of(r.wid)
    r.check('Real run 2', 'the run fails after the summary (error alert), the rotation only counts saved assignments, no new approval request',
            node == 'Stop: Cleanup Had Problems' and ex['status'] == 'error' and 'Merge failed' in error and 'Omar Haddad' in error
            and st.get('rotation', {}).get('lastOwnerId') == '203' and not [e for e in r.new('slack') if e['body'].get('blocks')],
            f'ended at {node}, rotation {st.get("rotation", {}).get("lastOwnerId")}')

    # ---------------------------------------------------------------- real run 3: Dan is back, failures gone, one lost answer
    FAIL['merge_500'].clear()
    FAIL['update_invalid'].clear()
    FAIL['merge_lost_answer'].add('1012')      # HubSpot saves the merge, but its answer is lost
    update_env(r.wid, {**ENV, 'AWAY_OWNERS': ''})
    status, body, ex = r.run()
    node, _ = r.stopped_at(ex)
    m = merges_in(r.new())
    ups = updates_in(r.new())
    reads = r.new('hubspot', 'POST', OBJECT_PATH + '/batch/read')
    summary = (slack_texts(r.new()) or [''])[-1]
    final = resolve('1012')
    r.check('Real run 3', 'the failed merge is finished; HubSpot saves it but the answer is lost (504) and the retry is refused (400, already one record): the read after the merges shows one record, so it counts as merged, not as a failure',
            [e['status'] for e in m] == [504, 400] and all(e['body'] == {'primaryObjectId': owen, 'objectIdToMerge': '1012'} for e in m)
            and resolve(owen) == final and len(reads) == 2 and sorted(i['id'] for i in reads[1]['body']['inputs']) == sorted([owen, '1012'])
            and f'• Owen Brooks: kept {owen}, merged in 1012 (same phone), now record {final}' in summary
            and 'HubSpot answered 1 merge with an error, but the records are now one' in summary and 'Problems: none' in summary
            and node == 'Any Problems?' and ex['status'] == 'success', f"statuses {[e['status'] for e in m]}, ended at {node}")
    r.check('Real run 3', 'the away owner who came back gets the next turn (after Cleo comes Dan)',
            [(i['id'], i['properties']) for e in ups for i in e['body']['inputs']] == [('1062', {'hubspot_owner_id': '204'})]
            and 'Owners assigned: 1 (Dan Reed 1)' in summary and 'Away:' not in summary
            and static_of(r.wid).get('rotation', {}).get('lastOwnerId') == '204', f'ended at {node}')

    # ---------------------------------------------------------------- real run 4: nothing left
    status, body, ex = r.run()
    node, _ = r.stopped_at(ex)
    summary = (slack_texts(r.new()) or [''])[-1]
    r.check('Real run 4 (repeat)', 'nothing new to do: no merge, no update, no approval request, the summary says so',
            not hs_writes(r.new()) and len(slack_texts(r.new())) == 1 and 'Merged: 0 exact duplicates' in summary and 'Fields fixed: 0' in summary
            and 'Owners assigned: 0' in summary and 'Waiting for approval: 2 close matches (0 sent now, 2 from earlier runs)' in summary
            and 'Problems: none' in summary and 'Not merged' not in summary and node == 'Any Problems?' and ex['status'] == 'success', f'ended at {node}')

    # ---------------------------------------------------------------- real run 5: an unanswered link expired
    old_link = pair['1019|1020'][0]
    old_nonce = token_payload(old_link)['n']
    edit_static(r.wid, lambda g: g['approvals']['1019|1020'].update(expiresAt=iso(datetime.now(timezone.utc) - timedelta(hours=1))))
    status, body, ex = r.run()
    node, _ = r.stopped_at(ex)
    ask = [e for e in r.new('slack') if e['body'].get('blocks')]
    links5 = approval_links(ask[0]['body']['blocks']) if ask else []
    summary = (slack_texts(r.new()) or [''])[-1]
    st = static_of(r.wid)
    r.check('Real run 5 (a link expired unanswered)', 'only that pair is sent again, with a new nonce and a new expiry; the pair that still waits is not sent twice',
            len(ask) == 1 and [k for k, *_ in links5] == ['1019|1020'] and token_payload(links5[0][1])['n'] != old_nonce
            and st['approvals']['1019|1020']['nonce'] == token_payload(links5[0][1])['n'] and not hs_writes(r.new())
            and 'Waiting for approval: 2 close matches (1 sent now, 1 from earlier runs)' in summary and node == 'Any Problems?',
            f'ended at {node}')
    status, page, _, ex = r.press(old_link)
    r.check('Real run 5 (a link expired unanswered)', 'the link from the first message no longer works (409); only the new link is valid',
            status == 409 and 'no longer active' in page and not r.new('hubspot'), f'{status}')

    # ---------------------------------------------------------------- approval: HubSpot saves the merge, the answer is lost
    FAIL['merge_lost_answer'].add('1022')
    status, page, _, ex = r.press(pair['1021|1022'][0])
    node, _ = r.stopped_at(ex)
    m = merges_in(r.new())
    reads = r.new('hubspot', 'POST', OBJECT_PATH + '/batch/read')
    notes = slack_texts(r.new())
    final = resolve('1022')
    r.check('Approve pressed, HubSpot answer lost', 'HubSpot saves the merge but answers 504 and refuses the retries (400): the records are read again, found to be one, and the page and Slack say merged, with no false alarm',
            status == 200 and '<h1>Merged</h1>' in page and f"The merged record's ID is {final}" in page and [e['status'] for e in m] == [504, 400, 400]
            and resolve('1021') == final and len(reads) == 2 and len(notes) == 1 and notes[0].startswith('*Duplicate approved and merged*: Jos')
            and f'new record ID {final}' in notes[0] and static_of(r.wid)['approvals']['1021|1022']['status'] == 'merged'
            and node == 'Slack: Decision Note' and ex['status'] == 'success', f"{status}, merge statuses {[e['status'] for e in m]}, ended at {node}")

    # ---------------------------------------------------------------- revoked token
    FAIL['auth'] = True
    status, body, ex = r.run()
    FAIL['auth'] = False
    r.ids['auth'] = ex['id']
    node, error = r.stopped_at(ex)
    r.check('HubSpot token refused (401)', 'tried 4 times, nothing changed, run fails with the reason and the setting to check',
            len(r.new('hubspot')) == 4 and all(e['status'] == 401 for e in r.new('hubspot')) and not hs_writes(r.new())
            and node == 'Stop: HubSpot Read Failed' and 'HubSpot: Get Owners' in error and '401' in error and 'HUBSPOT_TOKEN' in error, f'ended at {node}')

    # ---------------------------------------------------------------- overall
    r.check('All runs', 'every merge and update sent the bearer token, and no 10 second window went over HubSpot\'s 100 requests',
            all(e['headers'].get('authorization') == 'Bearer ' + TOKEN for e in LOG if e['service'] == 'hubspot' and e['status'] != 401)
            and RATE['max_in_window'] <= 100, f"busiest 10 seconds: {RATE['max_in_window']} requests")
    timed = [e for e in r.executions() if e.get('mode') == 'trigger' and e.get('stoppedAt')]
    ends = [r.stopped_at(e)[0] for e in timed]
    r.check('Schedule with SCHEDULE_ENABLED=false', 'scheduled runs stop at "Schedule On?" and do nothing',
            all(n == 'Schedule On?' for n in ends), f'{len(timed)} scheduled run(s)')
    failed = sorted((e['id'] for e in r.executions() if e.get('status') == 'error'), key=int)
    linked, got = alerts_for(failed)
    st = static_of(r.wid)
    decided = {k: e for k, e in st.get('approvals', {}).items() if e['status'] != 'waiting'}
    r.check('Workflow memory', 'only approvals and the rotation are kept; decided pairs keep record IDs only, no names or emails',
            set(st) == {'approvals', 'rotation'} and sorted(e['status'] for e in decided.values()) == ['already_merged', 'merged', 'merged', 'skipped']
            and all(e['primary'] == {'id': e['primaryId']} and e['secondary'] == {'id': e['secondaryId']} for e in decided.values())
            and all(e['primary'].get('email') for e in st['approvals'].values() if e['status'] == 'waiting'),
            f"{len(st.get('approvals', {}))} pairs, {len(decided)} decided")
    r.check('Error alert workflow', 'every failed run raised exactly one Slack alert with its execution link (wrong secret, 2 forged links, run 2, revoked token)',
            len(failed) == 5 and [len(v) for v in linked.values()] == [1] * 5 and len(got) == 5, f'{len(failed)} failed runs, {len(got)} alerts')
    text = (linked.get(r.ids.get('run2')) or [{'body': {'text': ''}}])[0]['body']['text']
    r.check('Error alert workflow', 'the alert names the node and every problem of the run',
            f'*{TEMP_NAME}* failed at *Stop: Cleanup Had Problems*' in text and 'Merge failed: O. Brooks (1012)' in text and 'Omar Haddad (1062)' in text,
            text.splitlines()[0] if text else 'no alert')
    text = (linked.get(r.ids.get('forged')) or [{'body': {'text': ''}}])[0]['body']['text']
    r.check('Error alert workflow', 'a forged approval link raises an alert that says why it was refused',
            'failed at *Stop: Forged Approval Link*' in text and 'its signature does not match' in text, text.splitlines()[1][:90] if text.count('\n') else 'no alert')


def run_schedule(results, error_wid):
    """A copy with the schedule on and DRY_RUN not set, plus two bad owners in the rotation: the nightly run must be a
    dry run (DRY_RUN is on by default) and must fail with both problems named. Automatic merging is off in this copy
    (MAX_MERGES_PER_RUN=0), and a new exact duplicate has a close match next to it: that close match must be asked
    about now, because the merge it would otherwise wait for never comes."""
    add_contact('1067', 'Ivy', 'Moss', 'ivy.moss@mail.example.com', company='Brightwave', created=NOW - timedelta(minutes=40))
    add_contact('1068', 'Ivy', 'Moss', 'ivymoss@mail.example.com', created=NOW - timedelta(minutes=35))   # same mailbox
    add_contact('1070', 'Ivy', 'Moss', 'ivy@brightwave.example.com', company='Brightwave Inc.', created=NOW - timedelta(minutes=30))
    env = {**ENV, 'SCHEDULE_ENABLED': 'true', 'MAX_MERGES_PER_RUN': '0',
           'ROUND_ROBIN_OWNERS': ENV['ROUND_ROBIN_OWNERS'] + ', eve.stone@salesteam.example.com, 206'}
    env.pop('DRY_RUN')
    mark = len(LOG)
    wid = import_active(env, '-2', error_wid)
    r = Runner(wid, '-2')
    try:
        end, timed = time.time() + 150, []
        while time.time() < end:
            timed = [e for e in r.executions() if e.get('mode') == 'trigger' and e.get('stoppedAt')]
            if timed:
                break
            time.sleep(2)
        node, error = r.stopped_at(timed[0]) if timed else ('none', '')
        entries = LOG[mark:]
        summary = (slack_texts(entries) or [''])[0]
        results.append(('Nightly schedule on, DRY_RUN not set', 'the scheduled run is a dry run: it reads and reports, and changes nothing',
                        bool(timed) and not hs_writes(entries) and summary.startswith('*HubSpot cleanup* (nightly run) *DRY RUN*') and static_of(wid) == {},
                        f'{len(timed)} scheduled run(s), ended at {node}'))
        results.append(('Nightly schedule on, DRY_RUN not set', 'owners that are not active people are left out of the rotation and fail the run',
                        node == 'Stop: Cleanup Had Problems' and '"eve.stone@salesteam.example.com" is not an active HubSpot owner' in error
                        and '"206" is a queue, not a person' in error and '*Problems: 2*' in summary, f'ended at {node}'))
        results.append(('Nightly schedule on, DRY_RUN not set', 'with MAX_MERGES_PER_RUN=0 nothing is merged, the summary says automatic merging is off, and a close match next to an exact duplicate is asked about now',
                        'Would merge: 0 exact duplicates' in summary and 'Automatic merging is off (MAX_MERGES_PER_RUN is 0), so 1 exact duplicate was not merged' in summary
                        and 'Would ask for approval: 3 close matches' in summary and 'will be sent once their exact duplicates are merged' not in summary
                        and 'The next run continues' not in summary,
                        next((ln for ln in summary.splitlines() if 'approval' in ln), 'no approval line')))
        linked, _ = alerts_for([timed[0]['id']] if timed else [])
        results.append(('Nightly schedule on, DRY_RUN not set', 'the failed nightly run raised its alert',
                        bool(timed) and len(linked.get(timed[0]['id'], [])) == 1, ''))
        # A pair waits for a decision in this copy, and someone presses Approve while DRY_RUN is on.
        card = lambda cid: {'id': cid, 'name': 'Sam Patel', 'email': '', 'phone': '', 'company': '', 'created': ''}
        edit_static(wid, lambda g: g.setdefault('approvals', {}).update({'1019|1020': {
            'status': 'waiting', 'nonce': 'dry-nonce', 'expiresAt': iso(datetime.now(timezone.utc) + timedelta(hours=5)),
            'postedAt': iso(datetime.now(timezone.utc)), 'primaryId': '1019', 'secondaryId': '1020', 'reason': 'same phone, different emails',
            'primary': card('1019'), 'secondary': card('1020')}}))
        part = b64url({'v': 1, 'a': 'approve', 'k': '1019|1020', 'p': '1019', 's': '1020', 'n': 'dry-nonce', 'e': int(time.time()) + 3600})
        status, page, _, ex = r.press(part + '.' + sign(part))
        results.append(('Nightly schedule on, DRY_RUN not set', 'an approval pressed while DRY_RUN is on changes nothing in HubSpot and the pair keeps waiting',
                        status == 409 and 'Dry run is on' in page and not r.new('hubspot') and static_of(wid)['approvals']['1019|1020']['status'] == 'waiting',
                        f'{status}'))
    finally:
        remove(wid)


def run_safeguards(results, error_wid):
    """A fresh copy (empty memory) and three cases where being careful matters more than being fast:
    - an office line shared by two people, one of them without an email: a phone alone must not merge them;
    - HubSpot answering retired record IDs as not found (its docs do not say which it does) while someone merges a
      planned pair during the run: nothing may be merged;
    - HubSpot refusing a whole batch update because of one contact (its docs only promise partial success for batch
      create): the other contacts must still be saved, one at a time, and the refused one named."""
    filler = 'main line, ask the front desk for whoever handles new patients; '
    add_contact('1065', 'Grace', 'Hill', 'grace.hill@harbordental.example.com', '+1 555 010 1065', company='Harbor Dental Group',
                created=NOW - timedelta(minutes=50))
    add_contact('1066', 'Leo', 'Hart', phone='(555) 010-1065', company='Harbor Dental Group, ' + filler * 45,
                created=NOW - timedelta(minutes=45))                     # same office line, no email, a very long company value
    add_contact('1069', 'Mia', 'Chen', 'mia.chen@newleads.example.com', source='Google', created=NOW - timedelta(minutes=20))
    FAIL['old_ids_missing'] = True
    FAIL['merge_on_read'] = ('1067', '1068')     # someone merges the two Ivy Moss records when the run reads them again
    FAIL['update_atomic'] = True
    FAIL['update_invalid'].add('1066')
    wid = import_active(ENV, '-5', error_wid)
    r = Runner(wid, '-5')
    try:
        status, body, ex = r.run()
        node, error = r.stopped_at(ex)
        entries = r.new()
        merges = merges_in(entries)
        ups = updates_in(entries)
        ask = [e for e in entries if e['service'] == 'slack' and e['body'].get('blocks')]
        links = approval_links(ask[0]['body']['blocks']) if ask else []
        office = [t for k, _, _, t in links if k == '1065|1066']
        summary = (slack_texts(entries) or [''])[-1]
        results.append(('Safeguards', 'an office line shared by two people (one without an email) is not merged: it goes to a person as a close match',
                        not any('1066' in (e['body']['primaryObjectId'], e['body']['objectIdToMerge']) for e in merges) and '1066' in CONTACTS
                        and bool(office) and 'same phone, different names' in office[0], office[0].splitlines()[0] if office else 'pair not in the approval request'))
        results.append(('Safeguards', 'a very long contact value is shortened, so the approval section stays within Slack\'s 3,000 characters and Slack accepts it',
                        bool(office) and ask[0]['status'] == 200 and '...' in office[0] and all(len(b['text']['text']) <= 3000 for b in ask[0]['body']['blocks']),
                        f"longest section {max((len(b['text']['text']) for b in ask[0]['body']['blocks']), default=0) if ask else 0} characters"))
        results.append(('Safeguards', 'retired IDs answered as not found, pair merged by someone else during the run: nothing merged, the pair is reported, not called deleted',
                        not merges and 'Not merged: Ivy Moss (1068) and Ivy Moss (1067): the record to keep is no longer in HubSpot under this ID (merged or deleted since the read)' in summary,
                        next((ln for ln in summary.splitlines() if 'Ivy Moss' in ln), 'no line about Ivy Moss')))
        batch = [e for e in ups if len(e['body']['inputs']) > 1]
        singles = {e['body']['inputs'][0]['id']: e['status'] for e in ups if len(e['body']['inputs']) == 1}
        results.append(('Safeguards', 'HubSpot refuses a whole batch update because of one contact: the contacts are sent again one at a time, the others are saved, the refused one is named and the run fails',
                        len(batch) == 1 and batch[0]['status'] == 400 and sorted(i['id'] for i in batch[0]['body']['inputs']) == sorted(singles)
                        and singles.get('1066') == 400 and all(v == 200 for k, v in singles.items() if k != '1066')
                        and bool(CONTACTS['1065']['properties'].get('hubspot_owner_id')) and not CONTACTS['1066']['properties'].get('hubspot_owner_id')
                        and CONTACTS['1069']['properties'].get('lead_source') == 'Paid Search'
                        and 'HubSpot did not save the update for 1 contact(s): Leo Hart (1066)' in summary and '*Problems: 1*' in summary
                        and static_of(wid).get('rotation', {}).get('lastOwnerId') == CONTACTS['1069']['properties'].get('hubspot_owner_id')
                        and node == 'Stop: Cleanup Had Problems' and ex['status'] == 'error',
                        f"batch {[e['status'] for e in batch]}, one at a time {singles}, ended at {node}"))
        linked, _ = alerts_for([ex['id']])
        results.append(('Safeguards', 'the failed run raised its alert, naming the refused contact',
                        len(linked[ex['id']]) == 1 and 'Leo Hart (1066)' in linked[ex['id']][0]['body']['text'], ''))
    finally:
        FAIL['old_ids_missing'] = False
        FAIL['merge_on_read'] = None
        FAIL['update_atomic'] = False
        FAIL['update_invalid'].clear()
        remove(wid)


def run_outages(results, error_wid):
    """A fresh copy (empty memory) on a day HubSpot's daily API limit is used up and Slack is down. The daily 429 must not
    be retried, the approval request must not be saved when Slack refused it, and the run must fail with every reason."""
    add_contact('1064', 'Nora', 'Vance', 'nora.vance@newleads.example.com', source='webinar', created=NOW - timedelta(minutes=30))
    FAIL['update_daily'] = True
    FAIL['slack_down'] = True
    wid = import_active(ENV, '-4', error_wid)
    r = Runner(wid, '-4')
    try:
        status, body, ex = r.run()
        node, error = r.stopped_at(ex)
        ups = updates_in(r.new())
        posts = r.new('slack')
        results.append(('HubSpot daily limit used up, Slack down', 'the DAILY 429 is not retried: one batch update attempt, nothing saved, no rotation remembered',
                        len(ups) == 1 and ups[0]['status'] == 429 and CONTACTS['1064']['properties'].get('hubspot_owner_id') is None and static_of(wid) == {},
                        f'{len(ups)} attempt(s)'))
        results.append(('HubSpot daily limit used up, Slack down', 'the approval request and the summary are each tried 3 times; the pairs are not saved, so the next run asks again',
                        len([e for e in posts if e['body'].get('blocks')]) == 3 and len([e for e in posts if not e['body'].get('blocks')]) == 3
                        and 'approvals' not in static_of(wid), f'{len(posts)} Slack attempts'))
        results.append(('HubSpot daily limit used up, Slack down', 'the run fails and the error names all three problems',
                        node == 'Stop: Cleanup Had Problems' and "HubSpot's daily API limit is used up (429 DAILY), so it was not tried again" in error
                        and 'could not be posted to Slack' in error and 'The Slack summary could not be posted' in error, f'ended at {node}'))
        linked, _ = alerts_for([ex['id']])
        results.append(('HubSpot daily limit used up, Slack down', 'the error alert still arrives (the error workflow posts to its own webhook in this test)',
                        len(linked[ex['id']]) == 1, ''))
    finally:
        FAIL['update_daily'] = False
        FAIL['slack_down'] = False
        remove(wid)


def run_bad_settings(results, error_wid):
    env = {**ENV, 'APPROVAL_SECRET': 'short', 'LEAD_SOURCE_MAP': '{not json', 'MAX_MERGES_PER_RUN': '99', 'APPROVAL_WEBHOOK_URL': '',
           'REPORT_TIMEZONE': 'Mars/Olympus'}
    wid = import_active(env, '-3', error_wid)
    r = Runner(wid, '-3')
    try:
        status, body, ex = r.run()
        node, _ = r.stopped_at(ex)
        try:
            answer = json.loads(body)
        except ValueError:
            answer = body
        problems = ['MAX_MERGES_PER_RUN must be a whole number from 0 to 50 (it is "99")',
                    'LEAD_SOURCE_MAP is not a JSON object like {"google ads": "Paid Search"}',
                    'APPROVAL_SECRET must be at least 32 characters, so approval links cannot be forged',
                    'APPROVAL_WEBHOOK_URL must be the full https URL of the approval webhook',
                    'REPORT_TIMEZONE "Mars/Olympus" is not a valid time zone']
        results.append(('Run Now with broken settings', 'answered 500 with the exact list of problems, no HubSpot call, run ends as failed',
                        status == 500 and answer == {'started': False, 'error': 'settings', 'problems': problems} and not r.new('hubspot')
                        and node == 'Stop: Bad Settings' and ex['status'] == 'error', f'{status}, ended at {node}'))
        linked, _ = alerts_for([ex['id']])
        text = (linked[ex['id']] or [{'body': {'text': ''}}])[0]['body']['text']
        results.append(('Run Now with broken settings', 'the error alert names every problem',
                        len(linked[ex['id']]) == 1 and 'Cleanup settings are not valid, so nothing was read or changed. ' + '. '.join(problems) + '.' in text,
                        text.splitlines()[1][:90] if text.count('\n') else 'no alert'))
    finally:
        remove(wid)


def main():
    if not KEY:
        raise SystemExit('Set N8N_API_KEY first.')
    leftovers = remove_leftovers()
    if leftovers:
        print(f'Removed {leftovers} temporary workflow(s) left by an interrupted run.')
    seed()
    server = MockServer(('127.0.0.1', MOCK_PORT), Mock)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    results = []
    try:
        error_wid = import_error_workflow()
        try:
            wid = import_active(ENV, '', error_wid)
            r = Runner(wid)
            try:
                run(r)
            finally:
                results = r.results
                remove(wid)
            run_schedule(results, error_wid)
            run_safeguards(results, error_wid)
            run_outages(results, error_wid)
            run_bad_settings(results, error_wid)
        finally:
            remove(error_wid)
    finally:
        server.shutdown()
        if os.environ.get('SAVE_REQUESTS'):
            saved = [{k: e[k] for k in ('service', 'method', 'path', 'query', 'body', 'status', 'response', 't')} for e in LOG]
            Path(os.environ['SAVE_REQUESTS']).write_text(json.dumps(saved, indent=1, ensure_ascii=False), encoding='utf-8')
    passed = sum(ok for *_, ok, _ in results)
    last = None
    for scenario, label_text, ok, detail in results:
        if scenario != last:
            print(f'\n{scenario}')
            last = scenario
        print(f"  {'PASS' if ok else 'FAIL'}  {label_text}" + (f'  ({detail})' if detail else ''))
    print(f'\n{passed}/{len(results)} checks passed')
    if CONNECTION_ERRORS:
        print(f'Note: {len(CONNECTION_ERRORS)} connection(s) to the mock broke before a response: ' + '; '.join(CONNECTION_ERRORS[:5]))
    sys.exit(0 if results and passed == len(results) else 1)


if __name__ == '__main__':
    main()
