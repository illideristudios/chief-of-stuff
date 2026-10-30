"""Crash-resumable GitHub source scan. The caller serializes access to the checkpoint.

Two bounded sweeps retain their combined evidence and revalidate selected items.
Detected pagination inconsistencies prevent publication. GitHub does not offer an
atomic snapshot: movement unseen by both sweeps, and changes after revalidation,
can still escape detection; observations span an interval, not a transaction.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import backlog

VERSION = 6
CHECKPOINT = '.github-scan.json'
MAX_BYTES = 24 * 1024 * 1024
MAX_ITEMS = 20000
MAX_TARGETS = 5000
BUDGET_STALE = 'GitHub shared budget is stale after an unknown outcome; coordination is best effort'
BUDGET_UNAVAILABLE = 'GitHub shared budget status unavailable; coordination is best effort'
INVENTORY_PAGE = 100
ISSUE_FIELDS = frozenset(('number', 'url', 'title', 'state', 'body', 'labels', 'closedAt', 'milestone'))
CHANGE_FIELDS = frozenset(('number', 'url', 'title', 'state', 'isDraft', 'mergedAt', 'baseRefName', 'body',
                           'headRefOid', 'reviewDecision', 'mergeable', 'headRefName', 'headRef',
                           'headRepository', 'closingIssuesReferences', 'files', 'latestReviews', 'reviewThreads', 'commits'))


def _complete_node(node, kind):
    fields = ISSUE_FIELDS if kind == 'Issue' else CHANGE_FIELDS
    if not fields <= node.keys() or not all(isinstance(node.get(k), str) for k in ('url', 'title', 'state')):
        return False
    if kind == 'Issue':
        if node['state'] not in ('OPEN', 'CLOSED'):
            return False
        connections = ('labels',)
    else:
        if node['state'] not in ('OPEN', 'CLOSED', 'MERGED'):
            return False
        connections = ('closingIssuesReferences', 'files', 'latestReviews', 'reviewThreads', 'commits')
    try:
        for field in (('closedAt',) if kind == 'Issue' else ('mergedAt',)):
            if node[field] is not None:
                if datetime.fromisoformat(node[field].replace('Z', '+00:00')).utcoffset() is None:
                    return False
        if kind != 'Issue' and node['state'] == 'MERGED' and node['mergedAt'] is None:
            return False
    except (ValueError, TypeError, AttributeError):
        return False
    return all(isinstance(node[k], dict) and isinstance(node[k].get('nodes'), list) and
               all(isinstance(item, dict) for item in node[k]['nodes']) for k in connections)


class ReadError(Exception):
    pass


class Admission:
    """Per-refresh admission boundary; a shared ledger can supply the same interface."""
    def __init__(self, gh, requests, points, reserve, *, transport=None):
        self.gh, self.requests, self.points, self.reserve = gh, requests, points, reserve
        self.transport = transport
        self.calls = self.cost = 0
        self.warning = None

    def read(self, selection):
        if self.calls >= self.requests or self.cost >= self.points:
            raise ReadError('GitHub refresh budget exhausted; scan will resume')
        self.calls += 1
        query = 'query { ' + selection + ' rateLimit { cost remaining limit resetAt } }'
        if self.transport is None:
            code, out, err = self.gh(['api', 'graphql', '-f', 'query=' + query])
        else:
            try:
                result = self.transport.request(query)
                if result.admission['stale']:
                    self.warning = BUDGET_STALE
                code, out, err = 0, json.dumps(result.body), ''
            except Exception as exc:
                # Transport and admission exceptions are sanitized; do not expose arbitrary errors.
                import github_admission
                import github_graphql_transport
                if isinstance(exc, (github_admission.StateError, github_admission.BudgetExhausted, github_graphql_transport.TransportError)):
                    raise ReadError(str(exc)) from None
                raise ReadError('GitHub transport unavailable') from None
        try:
            body = json.loads(out)
            data = body['data']
            rate = data['rateLimit']
            if not isinstance(data, dict) or not all(type(rate[k]) is int for k in ('cost', 'remaining', 'limit')):
                raise ValueError('bad rate limit')
        except (ValueError, KeyError, TypeError):
            raise ReadError((err.strip() or f'GitHub read failed (exit {code})')[:500])
        self.cost += rate['cost']
        if rate['remaining'] < self.reserve or self.cost > self.points:
            raise ReadError('GitHub rate limit or refresh budget reached; scan will resume')
        problems = [e for e in body.get('errors') or [] if not (
            isinstance(e, dict) and e.get('type') == 'NOT_FOUND' and
            len(e.get('path') or ()) == 2 and str(e['path'][0]).startswith('r') and
            str(e['path'][1]).startswith('n'))]
        if problems and self.transport is not None:
            raise ReadError('GitHub GraphQL returned errors; scan will resume')
        if problems or (code and not body.get('errors')):
            raise ReadError(('; '.join(str(e.get('message', e)) for e in problems if isinstance(e, dict))
                             or err.strip() or f'gh exited {code}')[:500])
        return data


def _save(path, state):
    payload = json.dumps(state, separators=(',', ':')).encode()
    if len(payload) > MAX_BYTES:
        raise ReadError('GitHub scan checkpoint exceeds storage limit')
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile('wb', dir=path.parent, prefix='.github-scan-', delete=False) as out:
            name = out.name
            out.write(payload)
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, path)
        name = None
        # The rename must survive a crash too.
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if name:
            os.unlink(name)


def _timestamp(value):
    if not isinstance(value, str):
        raise ValueError('invalid timestamp')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.utcoffset() is None:
        raise ValueError('timestamp has no timezone')
    return parsed


def _inventory_reset(state):
    state.update(sweep=0, phase='open', cursor='', cursors=[], page_numbers=[], last_updated=None,
                 inventory=[], first=[], details={}, selected=[], observed=None,
                 merged_observed=None, merged_only=False, seed_details={}, detail_order={}, seed_order={}, shifted=False)


def _refresh_merged(state):
    # Retain the open inventory and completed details while re-scanning the
    # time-sensitive merged inventory; completed details can be reused when
    # their inventory fields still match.
    candidates = [p for p in state['selected'] if p['state'] == 'OPEN']
    state.update(sweep=0, phase='merged', cursor='', cursors=[], page_numbers=[], last_updated=None,
                 first=[], inventory=candidates, selected=[], seed_details=state['details'], seed_order=state['detail_order'], details={}, detail_order={}, shifted=False,
                 merged_observed=None, merged_only=True)



def scan(path: Path, home, wanted, since, approver, every_open, admission, now, bs):
    """Return (issues, changes, errors, observed). None means retain the old panel."""
    targets = [(repo, n) for repo in sorted(wanted, key=str.casefold) for n in sorted(wanted[repo])]
    if len(targets) > MAX_TARGETS:
        msg = 'GitHub named target limit exceeded; data unavailable or stale'
        return None, None, dict.fromkeys(('kanban', 'merge requests'), msg), None
    identity = [VERSION, home.repo.casefold(), approver, every_open,
                bs.GH_ISSUE, bs.GH_CHANGE, INVENTORY_PAGE]
    fingerprint = hashlib.sha256(json.dumps(identity, separators=(',', ':')).encode()).hexdigest()
    try:
        if path.stat().st_size > MAX_BYTES:
            raise ValueError('large checkpoint')
        state = json.loads(path.read_text())
        if not isinstance(state, dict) or state.get('fingerprint') != fingerprint:
            raise ValueError('different scan')
        # A malformed or old checkpoint must never be interpreted as complete.
        for key in ('named_index', 'named', 'sweep', 'cursor', 'cursors', 'inventory', 'first', 'details', 'selected', 'observed', 'named_observed', 'named_times', 'phase', 'turn', 'home_verified', 'since', 'merged_observed', 'merged_only', 'seed_details', 'page_numbers', 'last_updated', 'detail_order', 'seed_order', 'named_order', 'sequence', 'shifted'):
            if key not in state:
                raise ValueError('incomplete checkpoint')
        index = state['named_index']
        if (type(index) is not int or index != len(state['named']) or
            not isinstance(state['named'], dict) or not isinstance(state['named_times'], dict) or
            set(state['named']) != set(state['named_times']) or state['phase'] not in ('open', 'merged') or
            state['sweep'] not in (0, 1, 2) or type(state['home_verified']) is not bool or
            state['turn'] not in ('named', 'inventory') or not isinstance(state['cursor'], str) or
            not isinstance(state['cursors'], list) or not all(isinstance(c, str) for c in state['cursors']) or
            not all(isinstance(state[k], list) and len(state[k]) <= MAX_ITEMS for k in ('inventory', 'first', 'selected')) or
            not isinstance(state['details'], dict) or not isinstance(state['seed_details'], dict) or
            type(state['merged_only']) is not bool or type(state['shifted']) is not bool or
            type(state['sequence']) is not int or state['sequence'] < 0 or
            any(not isinstance(state[k], dict) or any(type(v) is not int or not 0 < v <= state['sequence'] for v in state[k].values()) for k in ('named_order', 'detail_order', 'seed_order')) or
            set(state['named_order']) != set(state['named']) or set(state['detail_order']) != set(state['details']) or set(state['seed_order']) != set(state['seed_details']) or not isinstance(state['page_numbers'], list) or
            len(state['page_numbers']) > MAX_ITEMS or any(type(n) is not int for n in state['page_numbers'])):
            raise ValueError('invalid checkpoint')
        for key in ('inventory', 'first', 'selected'):
            entries = state[key]
            if any(not isinstance(n, dict) or type(n.get('number')) is not int or
                   n.get('state') not in ('OPEN', 'MERGED', 'CLOSED') or not isinstance(n.get('updatedAt'), str) or
                   'mergedAt' not in n for n in entries) or len({n['number'] for n in entries}) != len(entries):
                raise ValueError('invalid inventory checkpoint')
            for n in entries:
                _timestamp(n['updatedAt'])
                if n['mergedAt'] is not None:
                    _timestamp(n['mergedAt'])
                if n['state'] == 'MERGED' and n['mergedAt'] is None:
                    raise ValueError('missing merged timestamp')
        _timestamp(state['since'])
        for key in ('merged_observed', 'last_updated'):
            if state[key] is not None:
                _timestamp(state[key])
        if (not set(state['details']) <= {str(n['number']) for n in state['selected']} or
            (state['sweep'] == 2 and (not isinstance(state['observed'], str) or
                                      not isinstance(state['merged_observed'], str)))):
            raise ValueError('invalid detail checkpoint')
        for target, node in state['named'].items():
            repo, _, number = target.rpartition('#')
            n = int(number)
            if node is not None and (not isinstance(node, dict) or node.get('number') != n or
                                     node.get('__typename') not in ('Issue', 'PullRequest') or
                                     not _complete_node(node, node['__typename'])):
                raise ValueError('invalid named checkpoint')
        selected = {str(n['number']): n for n in state['selected']}
        for number, node in state['details'].items():
            if (not isinstance(node, dict) or not _complete_node(node, 'PullRequest') or
                node.get('number') != int(number) or any(node.get(k) != selected[number][k]
                                                        for k in ('state', 'updatedAt', 'mergedAt'))):
                raise ValueError('invalid detail checkpoint')
            _timestamp(node.get('updatedAt'))
        for key in ('observed', 'named_observed'):
            if state[key] is not None:
                _timestamp(state[key])
        for value in state['named_times'].values():
            _timestamp(value)
        for number, node in state['seed_details'].items():
            if (not number.isdigit() or not isinstance(node, dict) or node.get('number') != int(number) or
                not _complete_node(node, 'PullRequest')):
                raise ValueError('invalid retained detail')
            _timestamp(node.get('updatedAt'))
    except (OSError, ValueError, TypeError, AttributeError):
        state = {'fingerprint': fingerprint, 'named_index': 0, 'named': {}, 'named_times': {}, 'named_order': {}, 'sequence': 0, 'turn': 'named', 'home_verified': False, 'named_observed': None, 'since': since.isoformat()}
        _inventory_reset(state)

    # A shifted sweep can never establish completeness. Keep its progress until
    # finished, then retry on the next refresh while retaining named observations.
    if state['shifted'] and state['sweep'] == 2 and len(state['details']) == len(state['selected']):
        _inventory_reset(state)
    old_since = _timestamp(state['since'])
    if since < old_since:
        # Earlier cutoffs require history that may not have been retained.
        _inventory_reset(state)
        state['since'] = since.isoformat()
    elif (since > old_since and state['sweep'] == 2 and
          len(state['details']) == len(state['selected'])):
        _refresh_merged(state)
        state['since'] = since.isoformat()

    # Named targets change independently of repository inventory. Keep completed
    # observations for the intersection and fetch only newly requested targets.
    keys = {f'{repo}#{n}' for repo, n in targets}
    state['named'] = {k: v for k, v in state['named'].items() if k in keys}
    state['named_times'] = {k: v for k, v in state['named_times'].items() if k in keys}
    state['named_order'] = {k: v for k, v in state['named_order'].items() if k in keys}
    pending = [target for target in targets if f'{target[0]}#{target[1]}' not in state['named']]
    state['named_index'] = len(targets) - len(pending)
    state['named_observed'] = min(state['named_times'].values(), key=datetime.fromisoformat, default=None)

    def checkpoint():
        _save(path, state)

    def named_step():
        repo = pending[0][0]
        batch = []
        while len(batch) < len(pending) and len(batch) < bs.GH_PAGE and pending[len(batch)][0] == repo:
            batch.append(pending[len(batch)][1])
        owner, name = repo.split('/', 1)
        items = ' '.join(f'n{n}: issueOrPullRequest(number: {n}) {{ __typename ... on Issue {{ {bs.GH_ISSUE} }} '
                         f'... on PullRequest {{ {bs.GH_CHANGE} }} }}' for n in batch)
        data = admission.read(f'r0: repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) {{ {items} }}')
        nodes = data.get('r0')
        if not isinstance(nodes, dict) or any(f'n{n}' not in nodes for n in batch):
            raise ReadError('GitHub named items or repository unavailable')
        for n in batch:
            node = nodes[f'n{n}']
            if node is not None and (not isinstance(node, dict) or node.get('number') != n or
                                     node.get('__typename') not in ('Issue', 'PullRequest') or
                                     not _complete_node(node, node['__typename'])):
                raise ReadError('GitHub named item incomplete')
            if node is not None:
                try:
                    if node['__typename'] == 'PullRequest':
                        bs._gh_change(f'#{n}', node, approver, home.repo, repo)
                    else:
                        tuple(x['name'] for x in node['labels']['nodes'])
                        m = node.get('milestone')
                        if m:
                            bs.Milestone(m['title'], m['url'], bs._due(m.get('dueOn')), m['state'].lower(), int((m.get('issues') or {}).get('totalCount') or 0))
                except (ValueError, KeyError, TypeError, AttributeError, IndexError) as exc:
                    raise ReadError(f'GitHub named item incomplete: {exc}')
        state['sequence'] += 1
        for n in batch:
            state['named_order'][f'{repo}#{n}'] = state['sequence']
            state['named'][f'{repo}#{n}'] = nodes[f'n{n}']
            state['named_times'][f'{repo}#{n}'] = now.isoformat()
        del pending[:len(batch)]
        state['named_index'] = len(targets) - len(pending)
        state['named_observed'] = min(state['named_times'].values(), key=datetime.fromisoformat)
        if repo.casefold() == home.repo.casefold():
            state['home_verified'] = True

    def inventory_step():
        owner, name = home.repo.split('/', 1)
        if state['sweep'] < 2:
            if not every_open and state['phase'] == 'open':
                state.update(phase='merged', cursor='', cursors=[], page_numbers=[], last_updated=None)
            if state['phase'] == 'merged' and state['sweep'] == 1 and not state['cursor']:
                state['merged_observed'] = now.isoformat()
            after = f', after: {json.dumps(state["cursor"])}' if state['cursor'] else ''
            data = admission.read(f'inventory: repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) '
                                  f'{{ pullRequests(first: {INVENTORY_PAGE}, states: [{state["phase"].upper()}], '
                                  f'orderBy: {{field: {"CREATED_AT, direction: ASC" if state["phase"] == "open" else "UPDATED_AT, direction: DESC"}}}{after}) '
                                  '{ nodes { number state updatedAt mergedAt } pageInfo { hasNextPage endCursor } } }')
            repo = data.get('inventory')
            page = repo.get('pullRequests') if isinstance(repo, dict) else None
            info = page.get('pageInfo') if isinstance(page, dict) else None
            nodes = page.get('nodes') if isinstance(page, dict) else None
            if not isinstance(nodes, list) or not isinstance(info, dict) or type(info.get('hasNextPage')) is not bool:
                raise ReadError('GitHub pull request inventory unavailable')
            prior = set(state['page_numbers'])
            unique = []
            for node in nodes:
                if (not isinstance(node, dict) or type(node.get('number')) is not int or node['number'] <= 0 or
                    node.get('state') != state['phase'].upper() or not isinstance(node.get('updatedAt'), str) or
                    'mergedAt' not in node or (node['state'] == 'MERGED' and not isinstance(node['mergedAt'], str))):
                    _inventory_reset(state)
                    checkpoint()
                    raise ReadError('GitHub inventory shifted or returned an incomplete item; retrying sweep')
                try:
                    for value in (node['updatedAt'], node['mergedAt']):
                        if value is not None and datetime.fromisoformat(value.replace('Z', '+00:00')).utcoffset() is None:
                            raise ValueError('timezone missing')
                except (ValueError, TypeError, AttributeError):
                    _inventory_reset(state)
                    checkpoint()
                    raise ReadError('GitHub inventory returned an invalid timestamp; retrying sweep')
                if node['number'] in prior:
                    # Keep the cursor and accumulated evidence, but never publish
                    # this sweep as complete: a shift can also have skipped items.
                    state['shifted'] = True
                    continue
                if state['phase'] == 'merged' and state['last_updated'] is not None and _timestamp(node['updatedAt']) > _timestamp(state['last_updated']):
                    _inventory_reset(state)
                    checkpoint()
                    raise ReadError('GitHub inventory order shifted; retrying sweep')
                if state['phase'] == 'merged':
                    state['last_updated'] = node['updatedAt']
                unique.append(node)
                prior.add(node['number'])
            if len(prior) > MAX_ITEMS:
                raise ReadError('GitHub inventory exceeds storage limit')
            state['page_numbers'] = list(prior)
            cursor = info.get('endCursor')
            if info['hasNextPage'] and (not nodes or not isinstance(cursor, str) or not cursor or cursor in state['cursors']):
                _inventory_reset(state)
                checkpoint()
                raise ReadError('GitHub inventory pagination repeated or missing a cursor; retrying sweep')
            state['home_verified'] = True
            if not targets and state['named_observed'] is None:
                state['named_observed'] = now.isoformat()
            if state['observed'] is None:
                state['observed'] = now.isoformat()
            if state['phase'] == 'merged':
                # Updated-at bounds merged-at, so older updates cannot contain
                # today's merges. Keep the 50 most recently updated as well.
                merged_seen = len(state['page_numbers']) - len(unique)
                useful = [p for i, p in enumerate(unique) if
                          (every_open and merged_seen + i < 50) or
                          _timestamp(p['mergedAt']) >= _timestamp(state['since'])]
                combined = {p['number']: p for p in state['inventory']}
                combined.update({p['number']: p for p in useful})
                state['inventory'] = list(combined.values())
                old = bool(nodes) and _timestamp(nodes[-1]['updatedAt']) < _timestamp(state['since'])
                if old and (not every_open or merged_seen + len(unique) >= 50):
                    info = {**info, 'hasNextPage': False}
            else:
                state['inventory'].extend(unique)
            if len(state['inventory']) > MAX_ITEMS:
                raise ReadError('GitHub inventory exceeds storage limit')
            if info['hasNextPage']:
                state['cursor'] = cursor
                state['cursors'].append(cursor)
                return
            if state['phase'] == 'open':
                state.update(phase='merged', cursor='', cursors=[], page_numbers=[], last_updated=None)
                return
            if state['sweep'] == 0:
                state['first'] = state['inventory']
                state.update(sweep=1, phase='merged' if state['merged_only'] or not every_open else 'open',
                             cursor='', cursors=[], page_numbers=[], last_updated=None, inventory=[])
                return
            # Union the observations: updated-at ordering can move a persistent
            # PR ahead of a later cursor, so the second sweep alone is insufficient.
            entries = {p['number']: p for p in state['first']}
            entries.update({p['number']: p for p in state['inventory']})
            if len(entries) > MAX_ITEMS:
                raise ReadError('GitHub inventory exceeds storage limit')
            if state['merged_observed'] is None or _timestamp(state['merged_observed']) < since:
                state.update(sweep=0, phase='merged', cursor='', cursors=[], page_numbers=[], last_updated=None,
                             inventory=[p for p in entries.values() if p['state'] == 'OPEN'], first=[],
                             merged_only=True, merged_observed=None,
                             since=since.isoformat())
                return
            merged = sorted((p for p in entries.values() if p['state'] == 'MERGED'),
                            key=lambda p: (_timestamp(p['updatedAt']), p['number']), reverse=True)
            selected = ([p for p in entries.values() if p['state'] == 'OPEN'] if every_open else [])
            selected += [p for i, p in enumerate(merged) if
                         (every_open and i < 50) or _timestamp(p['mergedAt']) >= since]
            retained = {str(p['number']): state['seed_details'][str(p['number'])] for p in selected
                        if str(p['number']) in state['seed_details'] and
                        all(state['seed_details'][str(p['number'])].get(k) == p[k]
                            for k in ('state', 'updatedAt', 'mergedAt'))}
            retained_order = {n: state['seed_order'][n] for n in retained}
            state.update(sweep=2, selected=selected, details=retained, detail_order=retained_order, seed_details={}, seed_order={}, first=[], inventory=[],
                         cursor='', cursors=[], page_numbers=[], last_updated=None, since=since.isoformat())
            return
        remaining = [p for p in state['selected'] if str(p['number']) not in state['details']]
        batch = remaining[:bs.GH_PAGE]
        items = ' '.join(f'n{p["number"]}: pullRequest(number: {p["number"]}) {{ {bs.GH_CHANGE} updatedAt }}' for p in batch)
        data = admission.read(f'r0: repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) {{ {items} }}')
        nodes = data.get('r0')
        if not isinstance(nodes, dict):
            raise ReadError('GitHub pull request details unavailable')
        for p in batch:
            node = nodes.get(f'n{p["number"]}')
            if (not isinstance(node, dict) or node.get('number') != p['number'] or
                not isinstance(node.get('updatedAt'), str) or not _complete_node(node, 'PullRequest')):
                raise ReadError('GitHub pull request detail incomplete; retrying detail')
            try:
                _timestamp(node['updatedAt'])
                bs._gh_change(f'#{p["number"]}', node, approver, home.repo)
            except (ValueError, KeyError, TypeError, AttributeError, IndexError) as exc:
                raise ReadError(f'GitHub pull request detail incomplete: {exc}')
        state['sequence'] += 1
        for p in batch:
            node = nodes[f'n{p["number"]}']
            state['detail_order'][str(p['number'])] = state['sequence']
            # Revalidate each changed item in place; a closed PR no longer
            # contributes to the open inventory. Preserve completed siblings.
            p.update({k: node[k] for k in ('state', 'updatedAt', 'mergedAt')})
            state['details'][str(p['number'])] = node

    error = None
    checkpoint_failed = False
    try:
        try:
            checkpoint()
        except (ReadError, OSError):
            checkpoint_failed = True
            raise
        while True:
            # Finish already selected detail batches before a rollover refresh.
            # Otherwise daily restarts can starve a scan with a small budget.
            if (state['sweep'] == 2 and len(state['details']) == len(state['selected']) and
                since > _timestamp(state['since'])):
                _refresh_merged(state)
                state['since'] = since.isoformat()
                try:
                    checkpoint()
                except (ReadError, OSError):
                    checkpoint_failed = True
                    raise
            named_pending = state['named_index'] < len(targets)
            inventory_pending = state['sweep'] < 2 or len(state['details']) < len(state['selected'])
            if not named_pending and not inventory_pending:
                break
            if named_pending and (not inventory_pending or state['turn'] == 'named'):
                named_step()
                state['turn'] = 'inventory'
            else:
                inventory_step()
                state['turn'] = 'named'
            try:
                checkpoint()
            except (ReadError, OSError):
                checkpoint_failed = True
                raise
    except (ReadError, OSError, ValueError, KeyError, TypeError) as exc:
        error = str(exc) or 'GitHub scan checkpoint unavailable'

    if checkpoint_failed:
        return None, None, dict.fromkeys(('kanban', 'merge requests'), error), None
    named_done = state['named_index'] == len(targets) and state['home_verified']
    inventory_done = state['sweep'] == 2 and len(state['details']) == len(state['selected'])
    errors = {}
    if not named_done:
        errors['kanban'] = error or 'GitHub named scan in progress'
    if not named_done or not inventory_done or state['shifted']:
        errors['merge requests'] = error or ('GitHub inventory pagination shifted; incomplete scan will retry' if state['shifted'] else 'GitHub inventory scan in progress')
    if getattr(admission, "transport", None) is not None:
        try:
            if admission.transport.status()['stale']:
                admission.warning = BUDGET_STALE
        except Exception:
            admission.warning = BUDGET_UNAVAILABLE
        if admission.warning:
            for panel in ('kanban', 'merge requests'):
                errors[panel] = (errors[panel] + '; ' if panel in errors else '') + admission.warning
    if not named_done:
        return None, None, errors, None
    try:
        issues, changes = {}, {}
        for target, node in state['named'].items():
            if node is None:
                continue
            repo, _, n = target.rpartition('#')
            key = backlog.IssueRef(repo, int(n)).label(home)
            if node['__typename'] == 'PullRequest':
                changes[key] = bs._gh_change(key, node, approver, home.repo, repo)
            else:
                m = node.get('milestone')
                issues[key] = bs.Issue(key, node['url'], node['title'], node['state'].lower(),
                                       tuple(x['name'] for x in (node.get('labels') or {}).get('nodes') or []),
                                       node.get('body') or '', bs._when(node.get('closedAt')),
                                       bs.Milestone(m['title'], m['url'], bs._due(m.get('dueOn')), m['state'].lower(),
                                                    int((m.get('issues') or {}).get('totalCount') or 0)) if m else None)
        if not inventory_done or state['shifted']:
            return issues, None, errors, None
        home_named = {target.casefold(): target for target in state['named']}
        for node in state['details'].values():
            key = f'#{node["number"]}'
            named_key = home_named.get(f'{home.repo}#{node["number"]}'.casefold())
            if named_key and state['named_order'][named_key] > state['detail_order'][str(node['number'])]:
                continue
            if key not in changes and (node['state'] == 'CLOSED' or
                (not every_open and node['state'] == 'MERGED' and _timestamp(node['mergedAt']) < since)):
                continue
            changes[key] = bs._gh_change(key, node, approver, home.repo)
        return issues, changes, errors, datetime.fromisoformat(state['observed'])
    except (ValueError, KeyError, TypeError, AttributeError, IndexError) as exc:
        msg = f'GitHub incomplete scan data: {exc}'
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass
        return None, None, dict.fromkeys(('kanban', 'merge requests'), msg), None
