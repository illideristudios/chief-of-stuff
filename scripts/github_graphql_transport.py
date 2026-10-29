"""One admitted HTTP POST per GitHub GraphQL attempt.

Use the same host, trusted local state directory and admission policy in all
participating workspaces. Call request for every attempt, and surface status()
(including stale/unavailable). Queries must select rateLimit { cost }. HTTP 200
GraphQL errors are returned in Result.body; HTTP failures raise after cooldown
metadata has been committed. Result bodies are untrusted and must not be logged.

Credentials are obtained before admission from gh's environment precedence or
`gh auth token --no-refresh --hostname HOST`. This deliberately does not refresh
expired credentials; callers must arrange refresh separately. Authentication is separate from
the admitted GraphQL POST. The HTTP worker does not redirect or retry; failures
and timeouts leave the ledger stale when the outcome or cost is unknown. Neither
a killed worker nor any finite backoff establishes remote completion. Auth, other
requests, nonparticipating clients and other machines are not accounted for, so
this is best-effort coordination, not an account-wide hard limit.

The endpoint is https://api.github.com/graphql for github.com and
https://HOST/api/graphql for Enterprise Server, and
https://api.SUBDOMAIN.ghe.com/graphql for Enterprise Cloud with data residency. An HTTPS endpoint override is
available for Enterprise ports and installations; its hostname must match the
ledger host. Python's standard TLS CA configuration (including SSL_CERT_FILE/
SSL_CERT_DIR) and urllib proxy environment/no_proxy settings apply; gh-specific
api_host, CA or proxy configuration is not automatically imported. The system
Python/OpenSSL trust store can differ from gh's. Proxy CONNECT and auth retrieval
are outside the single GraphQL request; a proxy or server may itself replay traffic.
The timeout bounds auth, lock waiting and the worker, with up to one second for
process reaping. Cleanup kills the process group, but cannot control a helper
that escapes it or a process stuck in uninterruptible OS I/O. Filesystem operations
needed for durable admission are subject to the underlying filesystem and OS.
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
import sys
import time
from urllib.parse import urlsplit

import github_admission as admission


class TransportError(RuntimeError):
    """Sanitized failure; subprocess output and server error text are never included."""
    def __init__(self, kind, *, status=None):
        self.kind, self.status = kind, status
        super().__init__(f'GitHub GraphQL {kind}' + (f' (HTTP {status})' if status is not None else ''))


@dataclasses.dataclass(frozen=True)
class Result:
    body: dict
    cost: int
    status: int
    admission: dict


class GitHubGraphQL:
    def __init__(self, gate, host, *, executable=None, env=None, timeout=60.0,
                 max_output=8 * 1024 * 1024, endpoint=None):
        if not isinstance(host, str):
            raise ValueError('host must be a DNS hostname')
        host = host.lower().removesuffix('.')
        if not host or len(host) > 253 or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', p) for p in host.split('.')):
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
        api_host = ('api.github.com' if host == 'github.com' else
                    f'api.{host}' if host.endswith('.ghe.com') else host)
        default = f'https://{api_host}/' + ('graphql' if api_host != host else 'api/graphql')
        self.endpoint = endpoint or default
        try:
            parsed = urlsplit(self.endpoint)
            valid_host = parsed.hostname in ({host, api_host} if endpoint else {api_host})
            if (parsed.scheme != 'https' or not valid_host or parsed.username is not None or parsed.password is not None
                    or parsed.query or parsed.fragment or not parsed.path.startswith('/') or not parsed.port and parsed.netloc.endswith(':')):
                raise ValueError()
        except (ValueError, TypeError):
            raise ValueError('invalid HTTPS GraphQL endpoint') from None
        for key in ('GH_DEBUG', 'DEBUG', 'GH_FORCE_TTY', 'CLICOLOR_FORCE'):
            self.env.pop(key, None)
        self.env.update(GH_PROMPT_DISABLED='1', NO_COLOR='1', GH_TELEMETRY='false', GH_NO_UPDATE_NOTIFIER='1', GH_NO_EXTENSION_UPDATE_NOTIFIER='1')

    def status(self):
        return self.gate.status()

    def _token(self, deadline):
        names = ('GH_TOKEN', 'GITHUB_TOKEN') if self.host == 'github.com' or self.host.endswith('.ghe.com') else ('GH_ENTERPRISE_TOKEN', 'GITHUB_ENTERPRISE_TOKEN')
        token = next((self.env.get(name) for name in names if self.env.get(name)), None)
        if token is None:
            code, raw = _process([self.executable, 'auth', 'token', '--hostname', self.host, '--no-refresh'],
                                 b'', self.env, deadline, 65536)
            if code:
                raise TransportError('authentication unavailable')
            try:
                token = raw.decode('utf-8').strip()
            except UnicodeError:
                raise TransportError('authentication unavailable') from None
        if not isinstance(token, str) or not token or len(token.encode('utf-8')) > 65536 or any(c in token for c in '\r\n\x00'):
            raise TransportError('authentication unavailable')
        return token

    def request(self, query):
        if not isinstance(query, str) or not query or len(query.encode('utf-8')) > 1024 * 1024:
            raise ValueError('invalid GraphQL query')
        deadline = time.monotonic() + self.timeout
        token = self._token(deadline)
        worker_env = {k: v for k, v in self.env.items() if k not in ('GH_TOKEN', 'GITHUB_TOKEN', 'GH_ENTERPRISE_TOKEN', 'GITHUB_ENTERPRISE_TOKEN')}
        payload = json.dumps({'query': query, 'token': token, 'endpoint': self.endpoint, 'max_output': self.max_output}).encode()

        def attempt():
            if time.monotonic() >= deadline:
                raise TransportError('timeout')
            code, output = _process([sys.executable, str(Path(__file__).resolve())], payload,
                                    worker_env, deadline, self.max_output * 2 + 65536)
            if code:
                raise TransportError('request failed')
            try:
                result = json.loads(output)
                status, headers, body = result['status'], result['headers'], result['body']
                if type(status) is not int or not 200 <= status <= 599 or not isinstance(headers, dict) or not isinstance(body, dict):
                    raise ValueError()
            except (ValueError, TypeError, KeyError, RecursionError):
                raise TransportError('invalid HTTP response') from None
            body = _redact(body, token)
            data = body.get('data')
            rate = data.get('rateLimit') if isinstance(data, dict) else None
            cost = rate.get('cost') if isinstance(rate, dict) else None
            return admission.Response((status, body, cost), cost, status, headers, secondary_limit=_limited(body))

        try:
            status, body, cost = self.gate.request(attempt, deadline=deadline)
            if time.monotonic() >= deadline:
                raise TransportError('timeout')
            state = self.gate.status(deadline=deadline)
        except admission.LockTimeout:
            if time.monotonic() >= deadline:
                raise TransportError('timeout') from None
            raise
        if time.monotonic() >= deadline:
            raise TransportError('timeout')
        if not 200 <= status < 300:
            raise TransportError('request failed', status=status)
        return Result(body, cost, status, state)


def _redact(value, token):
    if isinstance(value, str):
        return value.replace(token, '[REDACTED]')
    if isinstance(value, list):
        return [_redact(v, token) for v in value]
    if isinstance(value, dict):
        return {_redact(k, token): _redact(v, token) for k, v in value.items()}
    return value


def _limited(body):
    if admission.GitHubAdmission._graphql_limited(body):
        return True
    message = body.get('message')
    return isinstance(message, str) and any(p in message.lower() for p in ('secondary rate limit', 'abuse detection'))


def _process(args, payload, env, deadline, limit):
    """Bound input, output and wall time, including inherited pipes and descendants."""
    if time.monotonic() >= deadline:
        raise TransportError('timeout')
    try:
        proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                env=env, start_new_session=True, close_fds=True)
    except OSError:
        raise TransportError('unavailable') from None
    sel = selectors.DefaultSelector()
    output, total, offset = bytearray(), 0, 0
    try:
        for stream, kind in ((proc.stdout, 'out'), (proc.stderr, 'err'), (proc.stdin, 'in')):
            os.set_blocking(stream.fileno(), False)
            sel.register(stream, selectors.EVENT_WRITE if kind == 'in' else selectors.EVENT_READ, kind)
        while sel.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TransportError('timeout')
            for key, _ in sel.select(remaining):
                if key.data == 'in':
                    try:
                        n = os.write(key.fd, payload[offset:offset + 65536]) if offset < len(payload) else 0
                    except BrokenPipeError:
                        n = 0
                    offset += n
                    if not n or offset == len(payload):
                        sel.unregister(key.fileobj); key.fileobj.close()
                else:
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        sel.unregister(key.fileobj); key.fileobj.close(); continue
                    total += len(chunk)
                    if total > limit:
                        raise TransportError('output exceeded limit')
                    if key.data == 'out': output.extend(chunk)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TransportError('timeout')
        try:
            return proc.wait(timeout=remaining), bytes(output)
        except subprocess.TimeoutExpired:
            raise TransportError('timeout') from None
    finally:
        try: os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError: pass
        try: proc.wait(timeout=1)
        except subprocess.TimeoutExpired: pass
        sel.close()
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if not stream.closed: stream.close()


def _worker():
    import urllib.error
    import urllib.request

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None
    request = json.load(sys.stdin)
    data = json.dumps({'query': request['query']}, ensure_ascii=True, separators=(',', ':')).encode()
    req = urllib.request.Request(request['endpoint'], data=data, method='POST', headers={
        'Authorization': 'Bearer ' + request['token'], 'Content-Type': 'application/json',
        'Accept': 'application/vnd.github+json', 'User-Agent': 'chief-of-stuff-graphql'})
    opener = urllib.request.build_opener(NoRedirect())
    try:
        response = opener.open(req)
    except urllib.error.HTTPError as exc:
        response = exc
    with response:
        status = response.status
        wanted = ('retry-after', 'x-ratelimit-remaining', 'x-ratelimit-reset')
        headers = {}
        for key in wanted:
            values = response.headers.get_all(key, [])
            if len(values) > 1:
                raise ValueError('duplicate header')
            if values:
                headers[key] = values[0]
        raw = response.read(request['max_output'] + 1)
        if len(raw) > request['max_output']:
            raise ValueError('response too large')
        try:
            body = json.loads(raw)
            if not isinstance(body, dict): body = {}
        except (ValueError, UnicodeError, RecursionError):
            body = {}
        sys.stdout.buffer.write(json.dumps({'status': status, 'headers': headers, 'body': body}, ensure_ascii=False).encode('utf-8'))


if __name__ == '__main__':
    try:
        _worker()
    except Exception:
        # Never expose credentials, URLs, request data or server output.
        sys.exit(1)
