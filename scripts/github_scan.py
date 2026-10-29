"""Crash-resumable GitHub source scan. The caller serializes access to the checkpoint.

Two equal inventories detect many pagination shifts, but GitHub does not offer an
atomic snapshot: the result represents the last observed sweep, not a transaction.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import backlog

VERSION = 3
CHECKPOINT = '.github-scan.json'
MAX_BYTES = 24 * 1024 * 1024
MAX_ITEMS = 20000
MAX_TARGETS = 5000
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
    def __init__(self, gh, requests, points, reserve):
        self.gh, self.requests, self.points, self.reserve = gh, requests, points, reserve
        self.calls = self.cost = 0

    def read(self, selection):
        if self.calls >= self.requests or self.cost >= self.points:
            raise ReadError('GitHub refresh budget exhausted; scan will resume')
        self.calls += 1
        code, out, err = self.gh(['api', 'graphql', '-f', 'query=query { ' + selection +
                                  ' rateLimit { cost remaining limit } }'])
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


def _inventory_reset(state):
    state.update(sweep=0, cursor='', cursors=[], inventory=[], first=[], details={}, selected=[], observed=None)


def scan(path: Path, home, wanted, since, approver, every_open, admission, now):
    """Return (issues, changes, errors, observed). None means retain the old panel."""
    import board_sources as bs

    targets = [(repo, n) for repo in sorted(wanted, key=str.casefold) for n in sorted(wanted[repo])]
    if len(targets) > MAX_TARGETS:
        msg = 'GitHub named target limit exceeded; data unavailable or stale'
        return None, None, dict.fromkeys(('kanban', 'merge requests'), msg), None
    identity = [VERSION, home.repo.casefold(), targets, since.isoformat(), approver, every_open,
                bs.GH_ISSUE, bs.GH_CHANGE, INVENTORY_PAGE]
    fingerprint = hashlib.sha256(json.dumps(identity, separators=(',', ':')).encode()).hexdigest()
    try:
        if path.stat().st_size > MAX_BYTES:
            raise ValueError('large checkpoint')
        state = json.loads(path.read_text())
        if not isinstance(state, dict) or state.get('fingerprint') != fingerprint:
            raise ValueError('different scan')
        # A malformed or old checkpoint must never be interpreted as complete.
        for key in ('named_index', 'named', 'sweep', 'cursor', 'cursors', 'inventory', 'first', 'details', 'selected', 'observed', 'named_observed', 'turn', 'home_verified'):
            if key not in state:
                raise ValueError('incomplete checkpoint')
        index = state['named_index']
        if (type(index) is not int or not 0 <= index <= len(targets) or
            not isinstance(state['named'], dict) or
            set(state['named']) != {f'{repo}#{n}' for repo, n in targets[:index]} or
            state['sweep'] not in (0, 1, 2) or type(state['home_verified']) is not bool or
            state['turn'] not in ('named', 'inventory') or not isinstance(state['cursor'], str) or
            not isinstance(state['cursors'], list) or not all(isinstance(c, str) for c in state['cursors']) or
            not all(isinstance(state[k], list) and len(state[k]) <= MAX_ITEMS for k in ('inventory', 'first', 'selected')) or
            not isinstance(state['details'], dict)):
            raise ValueError('invalid checkpoint')
        for key in ('inventory', 'first', 'selected'):
            entries = state[key]
            if any(not isinstance(n, dict) or type(n.get('number')) is not int or
                   n.get('state') not in ('OPEN', 'MERGED') or not isinstance(n.get('updatedAt'), str) or
                   'mergedAt' not in n for n in entries) or len({n['number'] for n in entries}) != len(entries):
                raise ValueError('invalid inventory checkpoint')
        if (not set(state['details']) <= {str(n['number']) for n in state['selected']} or
            (state['sweep'] == 2 and not isinstance(state['observed'], str))):
            raise ValueError('invalid detail checkpoint')
        for repo, n in targets[:index]:
            node = state['named'][f'{repo}#{n}']
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
        for key in ('observed', 'named_observed'):
            if state[key] is not None and datetime.fromisoformat(state[key]).utcoffset() is None:
                raise ValueError('invalid checkpoint time')
    except (OSError, ValueError, TypeError):
        state = {'fingerprint': fingerprint, 'named_index': 0, 'named': {}, 'turn': 'named', 'home_verified': False, 'named_observed': None}
        _inventory_reset(state)

    def checkpoint():
        _save(path, state)

    def named_step():
        index = state['named_index']
        repo = targets[index][0]
        batch = []
        while index + len(batch) < len(targets) and len(batch) < bs.GH_PAGE and targets[index + len(batch)][0] == repo:
            batch.append(targets[index + len(batch)][1])
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
        for n in batch:
            state['named'][f'{repo}#{n}'] = nodes[f'n{n}']
        state['named_index'] += len(batch)
        if repo.casefold() == home.repo.casefold():
            state['home_verified'] = True
        if state['named_index'] == len(targets):
            state['named_observed'] = now.isoformat()

    def inventory_step():
        owner, name = home.repo.split('/', 1)
        if state['sweep'] < 2:
            after = f', after: {json.dumps(state["cursor"])}' if state['cursor'] else ''
            data = admission.read(f'inventory: repository(owner: {json.dumps(owner)}, name: {json.dumps(name)}) '
                                  f'{{ pullRequests(first: {INVENTORY_PAGE}, states: [OPEN, MERGED], '
                                  f'orderBy: {{field: CREATED_AT, direction: ASC}}{after}) '
                                  '{ nodes { number state updatedAt mergedAt } pageInfo { hasNextPage endCursor } } }')
            repo = data.get('inventory')
            page = repo.get('pullRequests') if isinstance(repo, dict) else None
            info = page.get('pageInfo') if isinstance(page, dict) else None
            nodes = page.get('nodes') if isinstance(page, dict) else None
            if not isinstance(nodes, list) or not isinstance(info, dict) or type(info.get('hasNextPage')) is not bool:
                raise ReadError('GitHub pull request inventory unavailable')
            prior = {v['number'] for v in state['inventory']}
            for node in nodes:
                if (not isinstance(node, dict) or type(node.get('number')) is not int or node['number'] <= 0 or
                    node.get('state') not in ('OPEN', 'MERGED') or not isinstance(node.get('updatedAt'), str) or
                    'mergedAt' not in node or (node['state'] == 'MERGED' and not isinstance(node['mergedAt'], str)) or
                    node['number'] in prior):
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
                prior.add(node['number'])
            if len(prior) > MAX_ITEMS:
                raise ReadError('GitHub inventory exceeds storage limit')
            cursor = info.get('endCursor')
            if info['hasNextPage'] and (not nodes or not isinstance(cursor, str) or not cursor or cursor in state['cursors']):
                _inventory_reset(state)
                checkpoint()
                raise ReadError('GitHub inventory pagination repeated or missing a cursor; retrying sweep')
            state['home_verified'] = True
            if not targets and state['named_observed'] is None:
                state['named_observed'] = now.isoformat()
            state['inventory'].extend(nodes)
            if info['hasNextPage']:
                state['cursor'] = cursor
                state['cursors'].append(cursor)
                return
            if state['sweep'] == 0:
                state['first'] = state['inventory']
                state.update(sweep=1, cursor='', cursors=[], inventory=[])
                return
            if state['first'] != state['inventory']:
                # Use the latest complete sweep as the next baseline for convergence.
                state.update(first=state['inventory'], cursor='', cursors=[], inventory=[])
                return
            state['observed'] = now.isoformat()
            entries = state['inventory']
            merged = [p for p in entries if p['state'] == 'MERGED']
            if every_open:
                merged = sorted(merged, key=lambda p: (p['updatedAt'], p['number']), reverse=True)[:50]
                selected = [p for p in entries if p['state'] == 'OPEN'] + merged
            else:
                selected = [p for p in merged if datetime.fromisoformat(p['mergedAt'].replace('Z', '+00:00')) >= since]
            state.update(sweep=2, selected=selected, details={}, first=[], inventory=[], cursor='', cursors=[])
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
                any(node.get(k) != p[k] for k in ('state', 'updatedAt', 'mergedAt')) or
                not _complete_node(node, 'PullRequest')):
                _inventory_reset(state)
                checkpoint()
                raise ReadError('GitHub pull request changed during scan; retrying inventory')
        for p in batch:
            state['details'][str(p['number'])] = nodes[f'n{p["number"]}']

    error = None
    checkpoint_failed = False
    try:
        while True:
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
    if not named_done or not inventory_done:
        errors['merge requests'] = error or 'GitHub inventory scan in progress'
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
        if not inventory_done:
            return issues, None, errors, None
        for node in state['details'].values():
            key = f'#{node["number"]}'
            changes[key] = bs._gh_change(key, node, approver, home.repo)
        return issues, changes, errors, datetime.fromisoformat(state['observed'])
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        msg = f'GitHub incomplete scan data: {exc}'
        return None, None, dict.fromkeys(('kanban', 'merge requests'), msg), None
