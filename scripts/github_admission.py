"""Host-wide durable admission for cooperating GitHub GraphQL requests.

Use the same state directory and Policy for every workspace on a host, and wrap
every network attempt in request(). The callback dispatches exactly one attempt,
without retries, and returns (result, measured_cost), or Response when it has HTTP
rate-limit information. A Response must be returned even for HTTP errors; raise
application errors only after request() returns, so the cooldown is committed while
holding the host lock. Set secondary_limit when the response identifies a secondary
limit. Retry-After and X-RateLimit-Reset use wall-clock time. A 429 or identified
secondary limit without a usable deadline receives a 60-second minimum cooldown.

Exceptions, invalid or missing costs, and recovered in-flight reservations quarantine
the entire host indefinitely. Neither a transport timeout nor process death proves
that GitHub has stopped processing a request. An operator may clear quarantine only
after establishing that all unknown requests have finished and that a full policy
window has passed since the last possible remote charge. There is no automatic safe
recovery without such an external guarantee. Clearing quarantine is an explicit
administrative action; it does not clear a cooldown or known charges.

There is no authoritative upper bound on unknown request costs. max_request_cost is
only a caller-declared query-shape bound, not a guarantee about GitHub. Known costs
above it block for a window. Other participating calls serialize under a host lock;
non-participating calls are outside this accounting. The state directory and parents
must be trusted against deletion/replacement and use a local filesystem with reliable
flock and fsync. Clock rollback fails closed. Forward clock jumps can expire known
charges or cooldowns early; callers need a reasonably accurate shared system clock.
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
import time
from pathlib import Path


class StateError(RuntimeError):
    """State is unavailable, invalid, or cannot safely account for the request."""


class BudgetExhausted(RuntimeError):
    """The rolling request or point budget has been reached."""


class HostQuarantined(StateError):
    """An unknown request outcome requires explicit administrative recovery."""


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
        self.policy, self.lock_timeout, self.clock = policy, lock_timeout, clock

    def _now(self):
        now = self.clock()
        if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now) or now < 0:
            raise StateError('invalid clock')
        return now

    def _read(self, fd):
        os.lseek(fd, 0, os.SEEK_SET)
        raw = os.read(fd, 2_000_001)
        if len(raw) > 2_000_000:
            raise StateError('ledger exceeds size bound')
        try:
            state = json.loads(raw, object_pairs_hook=_unique)
            if not isinstance(state, dict) or type(state.get('version')) is not int or state['version'] not in (1, 2):
                raise ValueError('ledger schema mismatch')
            expected = {'version', 'policy', 'last', 'events'}
            if state['version'] == 2:
                expected |= {'quarantined', 'cooldown_until'}
            if set(state) != expected or state['policy'] != dataclasses.asdict(self.policy) or not isinstance(state['policy'], dict) or any(type(v) is not int for v in state['policy'].values()):
                raise ValueError('ledger schema or policy mismatch')
            if state['version'] == 2:
                cooldown = state['cooldown_until']
                if type(state['quarantined']) is not bool or isinstance(cooldown, bool) or not isinstance(cooldown, (int, float)) or not math.isfinite(cooldown) or cooldown < 0:
                    raise ValueError('invalid quarantine or cooldown')
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
            if state['version'] == 1:
                # Version 1 may have discarded unknown outcomes even if its events are empty.
                state.update(version=2, quarantined=True, cooldown_until=0)
            return state
        except (ValueError, TypeError, UnicodeError, RecursionError) as exc:
            raise StateError('corrupt or incompatible GitHub admission ledger') from exc

    @staticmethod
    def _write(fd, state):
        data = json.dumps(state, separators=(',', ':'), allow_nan=False).encode()
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            if not written:
                raise OSError('short ledger write')
            view = view[written:]
        os.fsync(fd)

    @contextlib.contextmanager
    def _locked(self):
        fd = None
        locked = False
        yielded = False
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
            flags = os.O_RDWR | os.O_NOFOLLOW | getattr(os, 'O_NONBLOCK', 0)
            fd = os.open(self.path, flags | (os.O_CREAT | os.O_EXCL if created else 0), 0o600)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid():
                raise StateError('unsafe ledger file')
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
            if fd is not None:
                if locked:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)

    def _load(self, fd, created):
        now = self._now()
        if created:
            state = {'version': 2, 'policy': dataclasses.asdict(self.policy), 'last': now,
                     'events': [], 'quarantined': False, 'cooldown_until': 0}
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
        if events and events[-1][2]:
            # Keep the reservation as evidence, but do not infer completion or cost.
            state['quarantined'] = True
        self._write(fd, state)
        return state, now

    def request(self, dispatch):
        """Execute one attempt and atomically record cost and any cooldown."""
        dispatch_error = None
        try:
            with self._locked() as (fd, created):
                state, now = self._load(fd, created)
                if state['quarantined']:
                    raise HostQuarantined('unknown GitHub request outcome; host quarantined')
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
                        cooldown = self._cooldown(outcome, self._now())
                    else:
                        result, cost = outcome
                        cooldown = 0
                except BaseException as exc:
                    dispatch_error = exc
                    self._finish(fd, state, None, unknown=True)
                    raise
                self._finish(fd, state, cost, cooldown=cooldown)
                return result

        except OSError as exc:
            if exc is dispatch_error:
                raise
            raise StateError('GitHub admission state unavailable') from exc

    def clear_quarantine(self):
        """Administrative recovery after externally verifying the module contract.

        The operator must establish remote completion and wait a full window since
        the last possible charge. This method cannot verify that evidence.
        """
        try:
            with self._locked() as (fd, created):
                state, now = self._load(fd, created)
                if state['quarantined']:
                    state['events'] = [e for e in state['events'] if not e[2]]
                    state['quarantined'] = False
                    self._write(fd, state)

        except OSError as exc:
            raise StateError('GitHub admission state unavailable') from exc

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
        limited = response.status == 429 or (response.status == 403 and
                   (response.secondary_limit or 'retry-after' in headers or headers.get('x-ratelimit-remaining') == '0'))
        if not limited:
            return 0
        deadlines = [now + 60] if response.status == 429 or response.secondary_limit else []
        retry = headers.get('retry-after')
        if retry:
            if re.fullmatch(r'[0-9]+', retry):
                if len(retry) > 12:
                    raise StateError('Retry-After exceeds supported range')
                deadlines.append(now + int(retry))
            else:
                try:
                    date = email.utils.parsedate_to_datetime(retry)
                    if date.tzinfo is None:
                        raise ValueError('missing timezone')
                    stamp = date.timestamp()
                    if math.isfinite(stamp):
                        deadlines.append(stamp)
                except (ValueError, TypeError, OverflowError, OSError):
                    pass
        reset = headers.get('x-ratelimit-reset')
        if reset and re.fullmatch(r'[0-9]+', reset):
            if len(reset) > 12:
                raise StateError('rate-limit reset exceeds supported range')
            deadlines.append(int(reset))
        # Invalid/missing deadlines cannot be interpreted as permission to retry.
        return max(deadlines) if deadlines and max(deadlines) > now else now + 60

    def _finish(self, fd, state, cost, *, unknown=False, cooldown=0):
        now = self._now()
        if now < state['last']:
            raise StateError('clock moved backwards during request; reservation retained')
        valid = type(cost) is int and cost >= 0
        violation = valid and cost > self.policy.max_request_cost
        state['last'] = now
        state['cooldown_until'] = max(state['cooldown_until'], cooldown)
        if unknown or not valid:
            state['quarantined'] = True
        else:
            state['events'][-1] = [now, min(cost, 1_000_000_000), False, violation]
        self._write(fd, state)
        if not unknown and (not valid or violation):
            raise StateError('missing, invalid, or above-bound GraphQL cost')
