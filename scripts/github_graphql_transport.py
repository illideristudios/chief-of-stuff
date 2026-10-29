"""One admitted GitHub GraphQL CLI invocation, for integration by the scan.

Call request() for *each* attempt, including retries; never retry inside the
callback. The caller owns retry policy and must surface admission.status(),
including stale/unavailable states. The result body is untrusted application
data; do not log it or the CLI's stderr. Queries must select rateLimit { cost }.
Only participating clients sharing the same trusted local state directory and
policy are accounted for; other tools and machines are outside this best-effort
ledger. It is not an authoritative GitHub quota.

GitHub CLI handles auth from its environment/configuration. Set host explicitly:
github.com or a GH Enterprise DNS host, without scheme, port or credentials.
The host must be the same host used to construct the ledger. Use gh's normal
GH_TOKEN/GITHUB_TOKEN or GH_ENTERPRISE_TOKEN/GITHUB_ENTERPRISE_TOKEN/host
configuration, as appropriate. Keep GH_DEBUG and similar diagnostic settings
off if they could expose secrets. CLI configuration such as api_host, proxies,
redirects, and refreshable credentials can alter the actual network routing or
cause additional HTTP calls: gh does not expose a no-redirect switch. Therefore
this adapter bounds one gh invocation, not provably one wire-level request; it
must not be wired where the latter is a hard requirement without another HTTP
transport that disables redirects and accounts for auth refresh separately.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import tempfile
import time

import github_admission as admission


class TransportError(RuntimeError):
    """A sanitized transport failure. No server text or process output is logged."""
    def __init__(self, kind, *, status=None):
        self.kind, self.status = kind, status
        super().__init__(f'GitHub GraphQL {kind}' + (f' (HTTP {status})' if status is not None else ''))


@dataclasses.dataclass(frozen=True)
class Result:
    body: dict
    cost: int
    status: int
    # Snapshot after the admitted attempt; may also show another caller's state.
    admission: dict


class GitHubGraphQL:
    """A single-attempt transport. Inject a gh executable and environment for tests."""
    def __init__(self, gate, host, *, executable=None, env=None, timeout=60.0, max_output=8 * 1024 * 1024):
        if not isinstance(host, str):
            raise ValueError('host must be a DNS hostname')
        host = host.lower().removesuffix('.')
        if (not host or len(host) > 253 or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', p) for p in host.split('.'))):
            raise ValueError('host must be a DNS hostname without scheme, port, or credentials')
        if not isinstance(gate, admission.GitHubAdmission) or gate.host_dir.name != hashlib.sha256(host.encode()).hexdigest():
            raise ValueError('admission ledger host mismatch')
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 600:
            raise ValueError('invalid timeout')
        if type(max_output) is not int or not 1024 <= max_output <= 32 * 1024 * 1024:
            raise ValueError('invalid output bound')
        self.gate, self.host, self.timeout, self.max_output = gate, host, timeout, max_output
        self.env = dict(os.environ if env is None else env)
        self.executable = executable if executable is not None else self.env.get('CHIEF_OF_STUFF_GH', 'gh')
        if not isinstance(self.executable, str) or (self.executable != 'gh' and not Path(self.executable).is_absolute()):
            raise ValueError('gh executable override must be absolute')
        # gh otherwise may prompt or dump request/response headers and credentials.
        self.env['GH_PROMPT_DISABLED'] = '1'
        self.env.pop('GH_DEBUG', None)
        self.env.pop('DEBUG', None)
        self.env.pop('GH_FORCE_TTY', None)
        self.env.pop('CLICOLOR_FORCE', None)
        self.env['NO_COLOR'] = '1'

    def status(self):
        return self.gate.status()

    def request(self, query):
        if not isinstance(query, str) or not query or len(query.encode('utf-8')) > 1024 * 1024:
            raise ValueError('invalid GraphQL query')

        def attempt():
            code, output = self._run(query)
            status, headers, body = _parse(output)
            data = body.get('data')
            rate = data.get('rateLimit') if isinstance(data, dict) else None
            cost = rate.get('cost') if isinstance(rate, dict) else None
            # The complete body reaches the ledger so it can identify HTTP 200 errors.
            return admission.Response((code, status, body, cost), cost, status, headers,
                                      secondary_limit=_limited(body))

        code, status, body, cost = self.gate.request(attempt)
        # Raise only after the ledger has recorded response metadata.
        state = self.status()
        if not 200 <= status < 300 or (code and not body.get('errors')):
            raise TransportError('request failed', status=status)
        return Result(body, cost, status, state)

    def _run(self, query):
        args = [self.executable, 'api', 'graphql', '--hostname', self.host, '--include', '--method', 'POST', '--input', '-']
        payload = json.dumps({'query': query}, ensure_ascii=True, separators=(',', ':')).encode()
        # A temporary file avoids an input-pipe deadlock and is unlinked by the OS.
        with tempfile.TemporaryFile() as source:
            source.write(payload); source.seek(0)
            try:
                proc = subprocess.Popen(args, stdin=source, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        env=self.env, start_new_session=True, close_fds=True)
            except OSError:
                raise TransportError('unavailable') from None
            streams = selectors.DefaultSelector()
            output = bytearray()
            total = 0
            deadline = time.monotonic() + self.timeout
            try:
                for stream, label in ((proc.stdout, 'stdout'), (proc.stderr, 'stderr')):
                    os.set_blocking(stream.fileno(), False)
                    streams.register(stream, selectors.EVENT_READ, label)
                while streams.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TransportError('timeout')
                    for key, _ in streams.select(remaining):
                        chunk = os.read(key.fileobj.fileno(), 65536)
                        if not chunk:
                            streams.unregister(key.fileobj); key.fileobj.close()
                            continue
                        total += len(chunk)
                        if total > self.max_output:
                            raise TransportError('output exceeded limit')
                        if key.data == 'stdout': output.extend(chunk)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TransportError('timeout')
                try:
                    code = proc.wait(timeout=remaining)
                except subprocess.TimeoutExpired:
                    raise TransportError('timeout') from None
                return code, bytes(output)
            finally:
                # Kill the entire session even when gh exited but left descendants.
                try: os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError: pass
                proc.wait()
                streams.close()
                for stream in (proc.stdout, proc.stderr):
                    if not stream.closed: stream.close()


def _limited(body):
    if admission.GitHubAdmission._graphql_limited(body):
        return True
    message = body.get('message')
    return isinstance(message, str) and any(phrase in message.lower() for phrase in
                                            ('secondary rate limit', 'abuse detection'))


def _parse(raw):
    try:
        # gh prints the status line with LF, followed by CRLF header lines.
        line, rest = raw.split(b'\n', 1)
        first = line.rstrip(b'\r').decode('iso-8859-1')
        match = re.fullmatch(r'HTTP/\d(?:\.\d)? ([1-5]\d\d)(?: [^\r\n]*)?', first)
        if not match: raise ValueError()
        status = int(match.group(1))
        if status < 200: raise ValueError()
        if rest.startswith(b'\r\n'):
            head, payload = b'', rest[2:]
        else:
            head, payload = rest.split(b'\r\n\r\n', 1)
        lines = head.decode('iso-8859-1').split('\r\n') if head else []
        # Ignore sensitive headers; reject ambiguous duplicate rate-limit headers.
        wanted = {'retry-after', 'x-ratelimit-remaining', 'x-ratelimit-reset'}
        headers = {}
        for line in lines:
            key, value = line.split(':', 1)
            if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", key): raise ValueError()
            key = key.lower()
            if key in wanted:
                if key in headers: raise ValueError()
                headers[key] = value.strip()
    except (ValueError, UnicodeError):
        raise TransportError('invalid HTTP response') from None
    try:
        body = json.loads(payload)
        if not isinstance(body, dict): body = {}
    except (ValueError, UnicodeError, RecursionError):
        body = {}
    # Preserve parsed HTTP limits even when the body/cost is unavailable.
    return status, headers, body
