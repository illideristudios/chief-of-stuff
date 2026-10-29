"""Host-wide durable admission for cooperating GitHub GraphQL requests.

Use the same state directory and Policy for every workspace on a host, and wrap
every network attempt in request(). The callback dispatches exactly one attempt,
without retries, and returns (result, measured_cost), or Response when it has HTTP
rate-limit information. A Response must be returned even for HTTP errors; raise
application errors only after request() returns, so the cooldown is committed while
holding the host lock. Set secondary_limit when the response identifies a secondary
limit. Retry-After and X-RateLimit-Reset use wall-clock time. A 429 or identified
secondary limit without a usable deadline receives a 60-second minimum cooldown.

Exceptions, missing costs, and recovered reservations cause a durable automatic
backoff of at least one policy window, doubling on consecutive unknown outcomes
up to eight windows (and at least 60 seconds). A recovered reservation is charged
at the caller's declared maximum at observation time. The ledger remains marked
stale after recovery: status() exposes that uncertainty, even after later success.
This is best-effort accounting, not a hard budget guarantee: a remote request may
finish or be charged after any finite backoff, at an unknown cost. Callers should
surface stale or unavailable state rather than present the budget as authoritative.
A callback that uses gh should bound its runtime and clean up its own subprocess
and, where supported, its process group; local termination does not prove remote
completion. The callback must still represent exactly one network attempt.

There is no authoritative upper bound on unknown request costs. max_request_cost is
only a caller-declared query-shape bound, not a guarantee about GitHub. Known costs
above it block for a window. Other participating calls serialize under a host lock;
non-participating calls are outside this accounting. The state directory and parents
must be trusted against deletion/replacement and use a local filesystem with reliable
flock, atomic same-directory replace, file fsync and directory fsync. All cooperating
clients must use this version's stable ledger.lock; stop older clients before upgrading.
Do not delete or replace that lock file. A crash can leave temporary files, which may
be removed only while all clients are stopped. Durability across power loss depends
on the filesystem and hardware honoring fsync; network filesystems are unsupported.
A server deadline outside the supported range blocks indefinitely until an operator
verifies it has passed and calls clear_unsupported_cooldown(); clear_quarantine()
does not clear it. A response lost before it is durably recorded, including a crash
after dispatch, cannot communicate its server cooldown to later callers. Forked
callback children must exit or exec and must not continue the parent's request.
Python-managed forks close the lock descriptor in the child, and exec closes it via
close-on-exec. Callbacks must not duplicate it or use native forks that bypass
Python's fork hooks and leave descendants alive. Clock rollback fails closed. Forward clock
jumps can expire known charges or cooldowns early; callers need a reasonably accurate shared system clock.
"""
from __future__ import annotations

import dataclasses
import contextlib
import email.utils
import fcntl
import hashlib
import json
import math
import os
import re
import stat
import tempfile
import threading
import time
from pathlib import Path


# Serialize descriptor registration with fork, including forks from other threads.
_fork_lock = threading.RLock()
_lock_fds = set()


def _after_fork_child():
    for fd in _lock_fds:
        os.close(fd)
    _lock_fds.clear()
    _fork_lock.release()


os.register_at_fork(before=_fork_lock.acquire, after_in_parent=_fork_lock.release,
                    after_in_child=_after_fork_child)


class StateError(RuntimeError):
    """State is unavailable, invalid, or cannot safely account for the request."""


class BudgetExhausted(RuntimeError):
    """The rolling request or point budget has been reached."""


class HostQuarantined(StateError):
    """An unknown outcome is in automatic backoff; the budget is stale."""

    def __init__(self, until):
        self.until = until
        super().__init__(f'GitHub outcome unknown; budget stale, retry after {until}')


class UnsupportedCooldown(StateError):
    """A server deadline cannot be represented; operator verification is required."""


class CooldownActive(BudgetExhausted):
    """GitHub has asked participating clients to pause until `until`."""

    def __init__(self, until):
        self.until = until
        super().__init__(f'GitHub host cooldown active until {until}')


@dataclasses.dataclass(frozen=True)
class Response:
    """An HTTP response and its measured GraphQL cost, including on errors."""
    result: object
    cost: object
    status: int
    headers: object
    secondary_limit: bool = False


class LockTimeout(StateError):
    """The host lock could not be acquired within the configured wait."""


@dataclasses.dataclass(frozen=True)
class Policy:
    window_seconds: int
    max_requests: int
    point_budget: int
    max_request_cost: int

    def __post_init__(self):
        for field in dataclasses.fields(self):
            value = getattr(self, field.name)
            if type(value) is not int or value <= 0 or value > 1_000_000:
                raise ValueError(f'{field.name} must be a positive bounded integer')
        if self.max_requests > 10_000:
            raise ValueError('max_requests exceeds the state bound')


def default_state_dir() -> Path:
    return Path(os.environ.get('XDG_STATE_HOME') or Path.home() / '.local/state') / 'chief-of-stuff/github-admission'


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate state key')
        result[key] = value
    return result


class GitHubAdmission:
    def __init__(self, state_dir: Path | str | None, host: str, policy: Policy, *,
                 lock_timeout: float = 10, clock=time.time):
        if not isinstance(host, str):
            raise ValueError('host must be a DNS hostname')
        host = host.lower().removesuffix('.')
        if len(host) > 253 or not host or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label) for label in host.split('.')):
            raise ValueError('host must be a DNS hostname without scheme, port, or credentials')
        if not isinstance(policy, Policy):
            raise ValueError('policy must be a Policy')
        if isinstance(lock_timeout, bool) or not isinstance(lock_timeout, (int, float)) or not math.isfinite(lock_timeout) or lock_timeout < 0:
            raise ValueError('invalid lock timeout')
        self.path = Path(state_dir) if state_dir is not None else default_state_dir()
        self.host_dir = self.path / hashlib.sha256(host.encode()).hexdigest()
        self.path = self.host_dir / 'ledger.json'
        self.lock_path = self.host_dir / 'ledger.lock'
        self.policy, self.lock_timeout, self.clock = policy, lock_timeout, clock

    def _now(self):
        now = self.clock()
        if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now) or now < 0:
            raise StateError('invalid clock')
        return now

    def _read(self, fd):
        record = os.open(self.path, os.O_RDWR | os.O_NOFOLLOW | getattr(os, 'O_NONBLOCK', 0))
        try:
            self._check_file(record)
            os.fchmod(record, 0o600)
            raw = os.read(record, 2_000_001)
        finally:
            os.close(record)
        if len(raw) > 2_000_000:
            raise StateError('ledger exceeds size bound')
        try:
            state = json.loads(raw, object_pairs_hook=_unique)
            if not isinstance(state, dict) or type(state.get('version')) is not int or state['version'] not in (1, 2, 3, 4):
                raise ValueError('ledger schema mismatch')
            expected = {'version', 'policy', 'last', 'events'}
            if state['version'] == 2:
                expected |= {'quarantined', 'cooldown_until'}
            if state['version'] in (3, 4):
                expected |= {'stale', 'recovery_until', 'unknown_count', 'cooldown_until'}
            if state['version'] == 4:
                expected.add('unsupported_cooldown')
                if type(state.get('unsupported_cooldown')) is not bool:
                    raise ValueError('invalid unsupported cooldown')
            if set(state) != expected or state['policy'] != dataclasses.asdict(self.policy) or not isinstance(state['policy'], dict) or any(type(v) is not int for v in state['policy'].values()):
                raise ValueError('ledger schema or policy mismatch')
            if state['version'] == 2:
                cooldown = state['cooldown_until']
                if type(state['quarantined']) is not bool or isinstance(cooldown, bool) or not isinstance(cooldown, (int, float)) or not math.isfinite(cooldown) or cooldown < 0:
                    raise ValueError('invalid quarantine or cooldown')
            if state['version'] in (3, 4):
                recovery, count, cooldown = state['recovery_until'], state['unknown_count'], state['cooldown_until']
                if (type(state['stale']) is not bool or type(count) is not int or not 0 <= count <= 4
                        or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0 for v in (recovery, cooldown))):
                    raise ValueError('invalid recovery state')
            last, events = state['last'], state['events']
            if isinstance(last, bool) or not isinstance(last, (int, float)) or not math.isfinite(last) or last < 0 or not isinstance(events, list) or len(events) > self.policy.max_requests:
                raise ValueError('invalid ledger')
            previous = 0
            for event in events:
                if not isinstance(event, list) or len(event) != 4:
                    raise ValueError('invalid event')
                stamp, cost, pending, violation = event
                if isinstance(stamp, bool) or not isinstance(stamp, (int, float)) or not math.isfinite(stamp) or not previous <= stamp <= last or type(cost) is not int or not 0 <= cost <= 1_000_000_000 or type(pending) is not bool or type(violation) is not bool or (pending and (cost != self.policy.max_request_cost or violation)) or (cost > self.policy.max_request_cost and not violation):
                    raise ValueError('invalid event values')
                previous = stamp
            if sum(e[2] for e in events) > 1 or any(e[2] for e in events[:-1]):
                raise ValueError('invalid reservations')
            return state
        except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
            raise StateError('corrupt or incompatible GitHub admission ledger') from exc

    @staticmethod
    def _check_file(fd):
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid():
            raise StateError('unsafe admission state file')

    def _write(self, fd, state):
        data = json.dumps(state, separators=(',', ':'), allow_nan=False).encode()
        temp_fd, temp_path = tempfile.mkstemp(prefix='.ledger-', dir=self.host_dir)
        try:
            # Only the temporary file is truncated; the committed record stays intact.
            os.ftruncate(temp_fd, 0)
            view = memoryview(data)
            while view:
                written = os.write(temp_fd, view)
                if not written:
                    raise OSError('short ledger write')
                view = view[written:]
            os.fsync(temp_fd)
            os.replace(temp_path, self.path)
            directory = os.open(self.host_dir, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            os.close(temp_fd)
            try:
                os.unlink(temp_path)
            except FileNotFoundError:
                pass

    @contextlib.contextmanager
    def _locked(self):
        fd = None
        locked = False
        yielded = False
        owner = os.getpid()
        try:
            self.host_dir.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            try:
                os.mkdir(self.host_dir, 0o700)
                created = True
                directory = os.open(self.host_dir.parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            except FileExistsError:
                created = False
            if not hasattr(os, 'O_NOFOLLOW'):
                raise StateError('O_NOFOLLOW is required')
            flags = os.O_RDWR | os.O_NOFOLLOW | getattr(os, 'O_NONBLOCK', 0) | os.O_CREAT
            with _fork_lock:
                fd = os.open(self.lock_path, flags, 0o600)
                _lock_fds.add(fd)
            self._check_file(fd)
            os.fchmod(fd, 0o600)
            deadline = time.monotonic() + self.lock_timeout
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    locked = True
                    break
                except BlockingIOError:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise LockTimeout('GitHub admission lock timed out')
                    time.sleep(min(0.02, remaining))
            yielded = True
            yield fd, created
        except OSError as exc:
            if yielded:
                raise
            raise StateError('GitHub admission state unavailable') from exc
        finally:
            if fd is not None and os.getpid() == owner:
                with _fork_lock:
                    if locked:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                    _lock_fds.discard(fd)
                    os.close(fd)

    def _load(self, fd, created):
        now = self._now()
        if created:
            state = {'version': 4, 'policy': dataclasses.asdict(self.policy), 'last': now,
                     'events': [], 'stale': False, 'recovery_until': 0,
                     'unknown_count': 0, 'cooldown_until': 0, 'unsupported_cooldown': False}
            self._write(fd, state)
            directory = os.open(self.path.parent, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        else:
            state = self._read(fd)
        if now < state['last']:
            raise StateError('clock moved backwards')
        state['last'] = now
        events = state['events']
        legacy_unknown = state['version'] == 1 or (state['version'] == 2 and state['quarantined'])
        if state['version'] in (1, 2):
            cooldown = state.get('cooldown_until', 0)
            state.pop('quarantined', None)
            state.update(version=3, stale=False, recovery_until=0, unknown_count=0,
                         cooldown_until=cooldown)
        if state['version'] == 3:
            state.update(version=4, unsupported_cooldown=False)
        if legacy_unknown or (events and events[-1][2]):
            self._mark_unknown(state, now)
        self._write(fd, state)
        return state, now

    def request(self, dispatch):
        """Execute one attempt and atomically record cost and any cooldown."""
        dispatch_error = None
        try:
            with self._locked() as (fd, created):
                owner = os.getpid()
                state, now = self._load(fd, created)
                if state['unsupported_cooldown']:
                    raise UnsupportedCooldown('unsupported server deadline requires operator verification')
                if now < state['recovery_until']:
                    raise HostQuarantined(state['recovery_until'])
                if now < state['cooldown_until']:
                    raise CooldownActive(state['cooldown_until'])
                state['events'] = events = [e for e in state['events'] if now - e[0] < self.policy.window_seconds]
                if len(events) >= self.policy.max_requests or sum(e[1] for e in events) >= self.policy.point_budget or any(e[3] for e in events):
                    raise BudgetExhausted('GitHub admission budget exhausted')
                events.append([now, self.policy.max_request_cost, True, False])
                self._write(fd, state)  # Durable before the callback can dispatch.
                try:
                    outcome = dispatch()
                    if isinstance(outcome, Response):
                        result, cost = outcome.result, outcome.cost
                        cooldown, unsupported = self._cooldown(outcome, self._now())
                    else:
                        result, cost = outcome
                        cooldown, unsupported = 0, False
                    if os.getpid() != owner:
                        raise StateError('forked callback child must exit or exec')
                except BaseException as exc:
                    dispatch_error = exc
                    if os.getpid() == owner:
                        self._finish(fd, state, None, unknown=True)
                    raise
                self._finish(fd, state, cost, cooldown=cooldown, unsupported=unsupported)
                return result

        except OSError as exc:
            if exc is dispatch_error:
                raise
            raise StateError('GitHub admission state unavailable') from exc

    def status(self):
        """Return durable uncertainty and retry deadlines, or raise StateError if unavailable.

        A stale budget remains best effort after the automatic backoff expires.
        """
        try:
            with self._locked() as (fd, created):
                state, now = self._load(fd, created)
                return {'stale': state['stale'], 'recovery_until': state['recovery_until'],
                        'cooldown_until': state['cooldown_until'],
                        'unsupported_cooldown': state['unsupported_cooldown'], 'as_of': now}
        except OSError as exc:
            raise StateError('GitHub admission state unavailable') from exc

    def clear_quarantine(self):
        """Clear uncertainty only after externally establishing remote completion and
        waiting a full window since the last possible charge. This is an operator
        attestation, not evidence supplied by this module. Cooldowns are retained.
        """
        try:
            with self._locked() as (fd, created):
                state, now = self._load(fd, created)
                state.update(stale=False, recovery_until=0, unknown_count=0)
                self._write(fd, state)
        except OSError as exc:
            raise StateError('GitHub admission state unavailable') from exc

    def clear_unsupported_cooldown(self):
        """Clear only after an operator verifies that the server deadline has passed."""
        try:
            with self._locked() as (fd, created):
                state, _ = self._load(fd, created)
                state['unsupported_cooldown'] = False
                self._write(fd, state)
        except OSError as exc:
            raise StateError('GitHub admission state unavailable') from exc

    def _mark_unknown(self, state, now):
        state['stale'] = True
        state['unknown_count'] = min(state['unknown_count'] + 1, 4)
        delay = max(60, self.policy.window_seconds * 2 ** (state['unknown_count'] - 1))
        state['recovery_until'] = max(state['recovery_until'], now + delay)
        if state['events'] and state['events'][-1][2]:
            state['events'][-1] = [now, self.policy.max_request_cost, False, False]

    @staticmethod
    def _graphql_limited(result):
        if not isinstance(result, dict) or not isinstance(result.get('errors'), list):
            return False
        for error in result['errors']:
            if not isinstance(error, dict):
                continue
            kind = str(error.get('type', '')).upper()
            message = str(error.get('message', '')).lower()
            if kind in ('RATE_LIMITED', 'RATE_LIMIT') or 'secondary rate limit' in message or 'abuse detection' in message:
                return True
        return False

    @staticmethod
    def _cooldown(response, now):
        if type(response.status) is not int or not 100 <= response.status <= 599 or type(response.secondary_limit) is not bool or not hasattr(response.headers, 'items'):
            raise StateError('invalid HTTP response metadata')
        headers = {}
        for key, value in response.headers.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise StateError('invalid HTTP response headers')
            key = key.lower()
            if key in headers:
                raise StateError('duplicate HTTP response header')
            headers[key] = value.strip()
        secondary = response.secondary_limit or GitHubAdmission._graphql_limited(response.result)
        limited = (response.status == 429 or secondary or headers.get('x-ratelimit-remaining') == '0'
                   or (response.status == 403 and 'retry-after' in headers))
        if not limited:
            return 0, False
        fallback = now + 60
        unsupported = not math.isfinite(fallback) or fallback - now < 60
        if unsupported:
            fallback = now
        deadlines = [fallback] if response.status == 429 or secondary else []
        retry = headers.get('retry-after')
        if retry:
            if re.fullmatch(r'[0-9]+', retry):
                if len(retry.lstrip('0')) > 12:
                    unsupported = True
                else:
                    seconds = int(retry.lstrip('0') or '0')
                    deadline = now + seconds
                    if not math.isfinite(deadline) or deadline - now < seconds:
                        unsupported = True
                    else:
                        deadlines.append(deadline)
            else:
                try:
                    date = email.utils.parsedate_to_datetime(retry)
                    if date.tzinfo is None:
                        raise ValueError('missing timezone')
                    stamp = date.timestamp()
                    if math.isfinite(stamp):
                        deadlines.append(stamp)
                except (ValueError, TypeError, OverflowError, OSError):
                    # A syntactically date-like future year outside datetime's range
                    # cannot safely be treated as a missing header.
                    if re.search(r'\b[0-9]{5,}\b', retry):
                        unsupported = True
        reset = headers.get('x-ratelimit-reset')
        if reset and re.fullmatch(r'[0-9]+', reset):
            if len(reset.lstrip('0')) > 12:
                unsupported = True
            else:
                deadlines.append(int(reset.lstrip('0') or '0'))
        # Invalid/missing deadlines cannot be interpreted as permission to retry.
        return (max(deadlines) if deadlines and max(deadlines) > now else fallback), unsupported

    def _finish(self, fd, state, cost, *, unknown=False, cooldown=0, unsupported=False):
        now = self._now()
        if now < state['last']:
            raise StateError('clock moved backwards during request; reservation retained')
        valid = type(cost) is int and cost >= 0
        violation = valid and cost > self.policy.max_request_cost
        state['last'] = now
        state['cooldown_until'] = max(state['cooldown_until'], cooldown)
        state['unsupported_cooldown'] |= unsupported
        if unknown or not valid:
            self._mark_unknown(state, now)
        else:
            state['unknown_count'] = 0
            state['events'][-1] = [now, min(cost, 1_000_000_000), False, violation]
        self._write(fd, state)
        if not unknown and not valid:
            raise StateError(f'missing or invalid GraphQL cost; budget stale, retry after {state["recovery_until"]}')
        if unsupported:
            raise UnsupportedCooldown('unsupported server deadline requires operator verification')
        if not unknown and violation:
            raise StateError('above-bound GraphQL cost')
