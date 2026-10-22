#!/usr/bin/env python3
"""Resumable Mixpanel EU-to-EU migration (events, people, group profiles).
Requires mixpanel-utils 2.2.7. Does not migrate Mixpanel configuration/boards.
"""
import argparse
import base64
import hashlib
import json
import logging
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path
from time import sleep
from mixpanel_utils import MixpanelUtils

LOG = logging.getLogger('mixpanel-migration')

def env(name, default=None):
    value = os.getenv(name, default)
    if value is None or str(value).strip() == '':
        raise ValueError(f'Missing environment variable: {name}')
    return value

def make_client(prefix):
    return MixpanelUtils(
        api_secret=env(f'{prefix}_PASSWORD'),
        service_account_username=env(f'{prefix}_USERNAME'),
        project_id=int(env(f'{prefix}_PROJECT_ID')),
        token=env(f'{prefix}_TOKEN'),
        residency='eu',
        max_retries=8,
        strict_import=True,
    )

def data_group_id(group, key):
    value = str(group.get(key, '')).strip()
    if not value.lstrip('-').isdigit():
        raise ValueError(
            f'groups.json entry {group.get("name")!r}: {key} must be the numeric '
            'data_group_id from Project Settings → Group Keys'
        )
    return value

def export_group_profiles(client, path, group_id):
    # Group profiles use the same /engage export as people, filtered by data_group_id.
    client.export_people(str(path), params={'data_group_id': group_id})

def group_updates(profiles, group_key, token):
    updates = []
    skipped = 0
    for profile in profiles:
        if not isinstance(profile, dict):
            raise ValueError('Group export must be a JSON array of profiles')
        group_id = profile.get('$distinct_id', profile.get('distinct_id'))
        if group_id is None or str(group_id).strip() == '':
            raise ValueError('Group profile missing $distinct_id')
        props = profile.get('$properties') or profile.get('properties') or {}
        if not isinstance(props, dict):
            raise ValueError(f'Group profile {group_id} has no properties object')
        custom = {k: v for k, v in props.items() if not str(k).startswith('$')}
        if not custom:
            skipped += 1
            continue
        updates.append({
            '$token': token,
            '$group_key': group_key,
            '$group_id': group_id,
            '$set': custom,
        })
    return updates, skipped

def post_group_batch(client, batch):
    query = urllib.parse.urlencode({
        'ip': 0,
        'verbose': 1,
        'strict': 1,
        'project_id': client.project_id,
    })
    url = f'{client.import_api}/groups?{query}'
    credentials = base64.b64encode(
        f'{client.service_account_username}:{client.api_secret}'.encode('utf-8')
    ).decode('ascii')
    request = urllib.request.Request(
        url,
        data=json.dumps(batch).encode('utf-8'),
        headers={
            'Authorization': f'Basic {credentials}',
            'Content-Type': 'application/json',
            'Accept': 'application/json',
        },
        method='POST',
    )
    try:
        with urllib.request.urlopen(request, timeout=client.timeout) as response:
            payload = response.read().decode('utf-8')
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode('utf-8', errors='replace')
        raise RuntimeError(f'Group import failed ({exc.code}): {detail}') from exc
    if payload.strip() == '1':
        return
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f'Unexpected group import response: {payload[:2000]}') from exc
    failed = parsed.get('failed_records') if isinstance(parsed, dict) else None
    status = parsed.get('status') if isinstance(parsed, dict) else None
    if status in (0, '0') or failed or parsed.get('error'):
        raise RuntimeError(f'Group import rejected: {payload[:2000]}')

def import_group_profiles(client, path, group_key):
    profiles = json.loads(Path(path).read_text(encoding='utf-8'))
    if not isinstance(profiles, list):
        raise ValueError(f'Group export must be a JSON array: {path}')
    updates, skipped = group_updates(profiles, group_key, client.token)
    if skipped:
        LOG.info('Skipped %s group profiles with no custom properties', skipped)
    for start in range(0, len(updates), 200):
        post_group_batch(client, updates[start:start + 200])
    LOG.info('Imported %s group profiles for %s', len(updates), group_key)

def read_json(path, fallback):
    if not path.exists():
        return fallback
    return json.loads(path.read_text(encoding='utf-8'))

def atomic_json(path, value):
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding='utf-8')
    tmp.replace(path)

def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()

def require_event(item, path):
    if not isinstance(item, dict) or 'event' not in item or 'properties' not in item:
        raise ValueError(f'Unexpected raw event format in {path}')

def count_jsonl(path):
    # mixpanel-utils writes a JSON array, including [] when /export is empty.
    # A raw Mixpanel export is one JSON object per line.
    text = path.read_text(encoding='utf-8').strip()
    if not text:
        return 0
    if text[0] == '[':
        items = json.loads(text)
        if not isinstance(items, list):
            raise ValueError(f'Unexpected raw event format in {path}')
        for item in items:
            require_event(item, path)
        return len(items)
    n = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        require_event(json.loads(line), path)
        n += 1
    return n

def retry(action, attempts=4):
    for index in range(attempts):
        try:
            return action()
        except Exception:
            LOG.exception('Attempt %s/%s failed', index + 1, attempts)
            if index + 1 == attempts:
                raise
            sleep(min(60, 2 ** (index + 1)))

def config_groups():
    path = Path(env('GROUPS_CONFIG', 'groups.json'))
    if not path.exists():
        return []
    groups = read_json(path, [])
    if not isinstance(groups, list):
        raise ValueError('groups.json must be an array')
    for g in groups:
        for key in ('name', 'group_key', 'source_data_group_id', 'target_data_group_id'):
            if not g.get(key):
                raise ValueError(f'Group entry missing {key}: {g}')
    return groups

def migrate_events(root, status, save, dry_run=False):
    start = date.fromisoformat(env('START_DATE'))
    end = date.fromisoformat(env('END_DATE'))
    if start > end or end > date.today():
        raise ValueError('Invalid date interval or END_DATE in future')
    tz = int(env('TIMEZONE_OFFSET', '0'))
    src = make_client('SOURCE')
    dst = make_client('TARGET')
    events = root / 'events'
    events.mkdir(exist_ok=True)
    day = start
    while day <= end:
        stamp = day.isoformat()
        file = events / f'{stamp}.jsonl'
        item = status['events'].setdefault(stamp, {})
        if item.get('imported'):
            if not file.is_file() or digest(file) != item.get('sha256'):
                raise RuntimeError(f'Previously imported backup changed or missing: {file}')
            LOG.info('SKIP previously imported day %s', stamp)
            day += timedelta(days=1)
            continue
        if not file.is_file():
            if dry_run:
                LOG.info('DRY RUN export and import %s', stamp)
                day += timedelta(days=1)
                continue
            # export is first written to .part; never trust incomplete exports
            part = file.with_suffix('.part')
            if part.exists():
                part.unlink()
            retry(lambda: src.export_events(str(part), {'from_date': stamp, 'to_date': stamp}))
            part.replace(file)
        total = count_jsonl(file)
        sha = digest(file)
        item.update({'exported': True, 'records': total, 'sha256': sha})
        save()
        LOG.info('Day %s: %s raw records', stamp, total)
        if total and not dry_run:
            retry(lambda: dst.import_events(str(file), timezone_offset=tz))
        if not dry_run:
            item['imported'] = True
            save()
        day += timedelta(days=1)

def migrate_people(root, status, save, dry_run=False):
    f = root / 'people.json'
    record = status['people']
    if record.get('imported'):
        if not f.exists() or digest(f) != record.get('sha256'):
            raise RuntimeError('People backup missing or changed')
        LOG.info('SKIP imported people profiles')
        return
    if not f.exists():
        if dry_run:
            LOG.info('DRY RUN export people')
            return
        part = root / 'people.part'
        if part.exists():
            part.unlink()
        retry(lambda: make_client('SOURCE').export_people(str(part)))
        part.replace(f)
    record.update({'exported': True, 'sha256': digest(f), 'bytes': f.stat().st_size})
    save()
    if not dry_run:
        retry(lambda: make_client('TARGET').import_people(str(f)))
        record['imported'] = True
        save()

def migrate_groups(root, status, save, dry_run=False):
    for group in config_groups():
        name = group['name']
        source_id = data_group_id(group, 'source_data_group_id')
        target_id = data_group_id(group, 'target_data_group_id')
        LOG.info('Group %s key %s source %s target %s', name, group['group_key'], source_id, target_id)
        f = root / f'groups-{name}.json'
        record = status['groups'].setdefault(name, {})
        if record.get('imported'):
            if not f.exists() or digest(f) != record.get('sha256'):
                raise RuntimeError(f'Group backup missing or changed: {name}')
            LOG.info('SKIP imported group %s', name)
            continue
        if not f.exists():
            if dry_run:
                LOG.info('DRY RUN export group %s', name)
                continue
            part = root / f'groups-{name}.part'
            if part.exists():
                part.unlink()
            retry(lambda sid=source_id, dest=part: export_group_profiles(make_client('SOURCE'), dest, sid))
            part.replace(f)
        record.update({'exported': True, 'sha256': digest(f), 'bytes': f.stat().st_size})
        save()
        if not dry_run:
            key = group['group_key']
            retry(lambda dest=f, group_key=key: import_group_profiles(make_client('TARGET'), dest, group_key))
            record['imported'] = True
            save()

def main():
    from dotenv import load_dotenv
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['all', 'events', 'people', 'groups', 'status'])
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    if env('SOURCE_PROJECT_ID') == env('TARGET_PROJECT_ID'):
        raise ValueError('Source and target project IDs must differ')
    root = Path(env('BACKUP_DIR', './backup')).resolve()
    root.mkdir(parents=True, exist_ok=True)
    state_file = root / 'state.json'
    status = read_json(state_file, {'events': {}, 'people': {}, 'groups': {}})
    def save():
        atomic_json(state_file, status)
    if args.command == 'status':
        print(json.dumps(status, indent=2, ensure_ascii=False))
        return
    if args.command in ('all', 'events'):
        migrate_events(root, status, save, args.dry_run)
    if args.command in ('all', 'people'):
        migrate_people(root, status, save, args.dry_run)
    if args.command in ('all', 'groups'):
        migrate_groups(root, status, save, args.dry_run)
    LOG.info('Completed requested steps; perform destination validation manually before cutover.')

if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        LOG.error('Migration interrupted: %s', exc)
        sys.exit(1)
