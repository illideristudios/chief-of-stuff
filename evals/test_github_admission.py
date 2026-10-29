"""Durable host-wide GitHub request admission."""
import json
import multiprocessing
import os
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import github_admission as admission


def policy():
    return admission.Policy(window_seconds=60, max_requests=3, point_budget=10, max_request_cost=7)


def ledger(root, clock=None, timeout=1):
    return admission.GitHubAdmission(root, 'github.com', policy(), clock=clock or time.time, lock_timeout=timeout)


def crash(root):
    def dispatch():
        os._exit(7)
    ledger(root).request(dispatch)


def concurrent(root, started, release, result):
    def dispatch():
        started.set()
        release.wait(5)
        return 'ok', 6
    try:
        result.put(ledger(root, timeout=2).request(dispatch))
    except Exception as exc:
        result.put(type(exc).__name__)


def crash_with_orphan(root, workspace, ready, release, finished, pidfile):
    os.chdir(workspace)
    def dispatch():
        code = ("import pathlib,time,sys; "
                "ready,release,finished=map(pathlib.Path,sys.argv[1:]); "
                "ready.touch(); "
                "exec('while not release.exists():\\n time.sleep(0.02)'); "
                "finished.touch()")
        child = subprocess.Popen([sys.executable, '-c', code, ready, release, finished],
                                 stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
        Path(pidfile).write_text(str(child.pid))
        deadline = time.monotonic() + 5
        while not Path(ready).exists() and time.monotonic() < deadline:
            time.sleep(.01)
        os._exit(7)
    ledger(root).request(dispatch)


def cooldown_writer(root, workspace, started, release):
    os.chdir(workspace)
    def dispatch():
        started.set()
        release.wait(5)
        return admission.Response('limited', 1, 429, {'Retry-After': '120'})
    ledger(root, timeout=5).request(dispatch)


def unknown_limit_writer(root, workspace, now, result):
    os.chdir(workspace)
    gate = ledger(root, lambda: now)
    try:
        gate.request(lambda: admission.Response(
            {'errors': [{'type': 'RATE_LIMITED'}]}, None, 200, {'Retry-After': '120'}))
    except admission.StateError:
        result.put('unavailable')


def recovery_reader(root, workspace, now, result):
    os.chdir(workspace)
    gate = ledger(root, lambda: now)
    try:
        gate.request(lambda: ('ok', 1))
        result.put(('ok', gate.status()['stale']))
    except admission.HostQuarantined:
        result.put(('backoff', gate.status()['stale']))
    except admission.CooldownActive:
        result.put(('cooldown', gate.status()['stale']))


def cooldown_reader(root, workspace, result, started=None):
    os.chdir(workspace)
    if started is not None:
        started.set()
    try:
        ledger(root, timeout=5).request(lambda: result.put('DISPATCHED') or ('ok', 1))
    except Exception as exc:
        result.put(type(exc).__name__)


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = 1000.0
        self.gate = ledger(self.tmp.name, lambda: self.now)

    def test_actual_cost_bounded_overshoot_and_rolling_expiry(self):
        self.assertEqual(self.gate.request(lambda: ('first', 6)), 'first')
        self.assertEqual(self.gate.request(lambda: ('second', 7)), 'second')
        with self.assertRaises(admission.BudgetExhausted):
            self.gate.request(lambda: self.fail('must not dispatch'))
        self.now += 60
        self.assertEqual(self.gate.request(lambda: ('after', 1)), 'after')

    def test_request_count_and_every_retry_is_charged(self):
        self.gate = admission.GitHubAdmission(self.tmp.name, 'github.com', admission.Policy(60, 3, 100, 7), clock=lambda: self.now)
        for _ in range(3):
            self.gate.request(lambda: ('attempt', 1))
        with self.assertRaises(admission.BudgetExhausted):
            self.gate.request(lambda: self.fail('retry must be admitted'))

    def test_unknown_recovers_with_stale_budget_and_clock_rollback_fails_closed(self):
        with self.assertRaises(TimeoutError):
            self.gate.request(lambda: (_ for _ in ()).throw(TimeoutError()))
        self.now -= 1
        with self.assertRaises(admission.StateError):
            self.gate.request(lambda: self.fail('clock rolled back'))
        self.now += 1
        with self.assertRaises(admission.HostQuarantined) as exc:
            self.gate.request(lambda: self.fail('backoff'))
        self.assertEqual(exc.exception.until, 1060)
        self.now = 1060
        self.assertEqual(self.gate.request(lambda: ('ok', 1)), 'ok')
        self.assertTrue(self.gate.status()['stale'])
        self.gate.clear_quarantine()  # Test attests that the recovery contract is met.
        self.assertFalse(self.gate.status()['stale'])

    def test_bad_cost_fails_closed_and_preserves_reservation(self):
        for value in (None, -1, True, 8):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as root:
                gate = ledger(root, lambda: self.now)
                with self.assertRaises(admission.StateError):
                    gate.request(lambda: ('bad', value))
                with self.assertRaises(admission.BudgetExhausted if value == 8 else admission.HostQuarantined):
                    gate.request(lambda: self.fail('reservation was lost'))

    def test_corrupt_and_mismatched_policy_fail_closed(self):
        self.gate.request(lambda: ('ok', 1))
        path = next(Path(self.tmp.name).rglob('*.json'))
        original = path.read_text()
        for bad in ('', '{', '{"a":1,"a":2}', original.replace('"version":3', '"version":99')):
            path.write_text(bad)
            with self.assertRaises(admission.StateError):
                self.gate.request(lambda: self.fail('corrupt state'))
        path.write_text(original)
        with self.assertRaises(admission.StateError):
            admission.GitHubAdmission(self.tmp.name, 'github.com', admission.Policy(60, 3, 11, 7)).request(lambda: self.fail('policy mismatch'))

    def test_crash_quarantines_on_recovery(self):
        p = multiprocessing.Process(target=crash, args=(self.tmp.name,))
        p.start(); p.join(5)
        self.assertEqual(p.exitcode, 7)
        gate = ledger(self.tmp.name)
        with self.assertRaises(admission.HostQuarantined):
            gate.request(lambda: self.fail('crashed dispatch was not quarantined'))

    def test_cross_process_lock_covers_dispatch_and_lock_timeout(self):
        started, release = multiprocessing.Event(), multiprocessing.Event()
        result = multiprocessing.Queue()
        p = multiprocessing.Process(target=concurrent, args=(self.tmp.name, started, release, result))
        p.start()
        try:
            self.assertTrue(started.wait(5))
            with self.assertRaises(admission.LockTimeout):
                ledger(self.tmp.name, timeout=0.05).request(lambda: self.fail('locked'))
        finally:
            release.set(); p.join(5)
        self.assertEqual(p.exitcode, 0)
        self.assertEqual(result.get(timeout=1), 'ok')
        gate = ledger(self.tmp.name)
        gate.request(lambda: ('second', 6))
        with self.assertRaises(admission.BudgetExhausted):
            gate.request(lambda: self.fail('cross-process accounting'))

    def test_clock_rollback_during_dispatch_leaves_durable_reservation(self):
        def dispatch():
            self.now -= 1
            return 'ok', 1
        with self.assertRaises(admission.StateError):
            self.gate.request(dispatch)
        self.now += 1
        with self.assertRaises(admission.HostQuarantined):
            self.gate.request(lambda: self.fail('reservation lost'))

    def test_missing_established_ledger_fails_closed(self):
        self.gate.request(lambda: ('ok', 1))
        next(Path(self.tmp.name).rglob('*.json')).unlink()
        with self.assertRaises(admission.StateError):
            self.gate.request(lambda: self.fail('deleted ledger'))

    def test_unavailable_state_and_failed_fsync_never_dispatch(self):
        root = Path(self.tmp.name) / 'ordinary-file'
        root.write_text('not a directory')
        with self.assertRaises(admission.StateError):
            ledger(root).request(lambda: self.fail('unavailable'))
        with self.assertRaises(admission.StateError):
            ledger(root).status()
        with mock.patch.object(admission.os, 'fsync', side_effect=OSError('disk failure')):
            with self.assertRaises(admission.StateError):
                self.gate.request(lambda: self.fail('not durable'))

    def test_failure_recording_outcome_keeps_durable_reservation(self):
        self.gate.request(lambda: ('initial', 0))
        write = admission.GitHubAdmission._write
        count = 0
        def fail_finish(fd, state):
            nonlocal count
            count += 1
            if count == 3:
                raise OSError('outcome write failed')
            write(fd, state)
        with mock.patch.object(admission.GitHubAdmission, '_write', side_effect=fail_finish):
            with self.assertRaises(admission.StateError):
                self.gate.request(lambda: ('dispatched', 1))
        with self.assertRaises(admission.HostQuarantined):
            self.gate.request(lambda: self.fail('failed outcome write lost reservation'))

    def test_reservation_is_durable_before_dispatch_and_has_no_request_data(self):
        def dispatch():
            state = json.loads(next(Path(self.tmp.name).rglob('*.json')).read_text())
            self.assertEqual(state['events'], [[self.now, 7, True, False]])
            return {'token': 'secret'}, 1
        self.assertEqual(self.gate.request(dispatch), {'token': 'secret'})
        self.assertNotIn('secret', next(Path(self.tmp.name).rglob('*.json')).read_text())

    def test_expired_events_are_pruned_and_policy_is_bounded(self):
        for _ in range(50):
            self.gate.request(lambda: ('ok', 1))
            self.now += 60
        state = json.loads(next(Path(self.tmp.name).rglob('*.json')).read_text())
        self.assertEqual(len(state['events']), 1)
        with self.assertRaises(ValueError):
            admission.Policy(60, 10001, 10, 7)

    def test_reported_above_bound_blocks_until_expiry(self):
        with self.assertRaises(admission.StateError):
            self.gate.request(lambda: ('ok', 8))
        with self.assertRaises(admission.BudgetExhausted):
            self.gate.request(lambda: self.fail('cost bound violated'))
        self.now += 60
        self.gate.request(lambda: ('ok', 1))

    def test_orphan_survives_crash_and_quarantine_across_workspaces(self):
        root = Path(self.tmp.name).resolve()
        one, two = root / 'one', root / 'two'
        one.mkdir(); two.mkdir()
        state = root / 'shared'
        ready, release, finished, pidfile = (str(root / name) for name in ('ready', 'release', 'finished', 'pid'))
        parent = multiprocessing.Process(target=crash_with_orphan,
                                         args=(state, one, ready, release, finished, pidfile))
        parent.start(); parent.join(10)
        try:
            self.assertEqual(parent.exitcode, 7)
            self.assertTrue(Path(ready).exists())
            os.kill(int(Path(pidfile).read_text()), 0)
            result = multiprocessing.Queue()
            reader = multiprocessing.Process(target=cooldown_reader, args=(state, two, result))
            reader.start(); reader.join(10)
            self.assertEqual(reader.exitcode, 0)
            self.assertEqual(result.get(timeout=1), 'HostQuarantined')
            # Local completion does not establish when GitHub might charge the request.
            Path(release).touch()
            deadline = time.monotonic() + 5
            while not Path(finished).exists() and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(Path(finished).exists())
            self.assertTrue(ledger(state).status()['stale'])
        finally:
            Path(release).touch()
            if parent.is_alive():
                parent.terminate(); parent.join()

    def test_real_process_timeout_recovers_without_claiming_child_finished(self):
        process = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            with self.assertRaises(subprocess.TimeoutExpired):
                self.gate.request(lambda: (process.communicate(timeout=.1), 1))
            self.assertIsNone(process.poll())
            self.now += 10_000
            self.assertEqual(self.gate.request(lambda: ('ok', 1)), 'ok')
            self.assertIsNone(process.poll())
            self.assertTrue(self.gate.status()['stale'])
        finally:
            process.kill()
            process.communicate(timeout=5)

    def test_cooldown_is_host_scoped_and_unrelated_403_is_not_limited(self):
        self.gate.request(lambda: admission.Response('forbidden', 1, 403, {}))
        self.gate.request(lambda: admission.Response('limited', 1, 429, {}))
        with self.assertRaises(admission.CooldownActive):
            self.gate.request(lambda: self.fail('limited host'))
        other = admission.GitHubAdmission(self.tmp.name, 'enterprise.example', policy(), clock=lambda: self.now)
        self.assertEqual(other.request(lambda: ('other host', 1)), 'other host')

    def test_cooldown_is_atomic_between_processes_and_workspaces(self):
        root = Path(self.tmp.name).resolve()
        one, two = root / 'one', root / 'two'
        one.mkdir(); two.mkdir()
        state = root / 'shared'
        started, release, reader_started, result = multiprocessing.Event(), multiprocessing.Event(), multiprocessing.Event(), multiprocessing.Queue()
        writer = multiprocessing.Process(target=cooldown_writer, args=(state, one, started, release))
        writer.start()
        try:
            self.assertTrue(started.wait(5))
            reader = multiprocessing.Process(target=cooldown_reader, args=(state, two, result, reader_started))
            reader.start()
            self.assertTrue(reader_started.wait(5))
            with self.assertRaises(admission.LockTimeout):
                ledger(state, timeout=.05).request(lambda: self.fail('writer still holds lock'))
            release.set()
            writer.join(10); reader.join(10)
            self.assertEqual(writer.exitcode, 0)
            self.assertEqual(reader.exitcode, 0)
            self.assertEqual(result.get(timeout=1), 'CooldownActive')
            with self.assertRaises(admission.CooldownActive):
                ledger(state).request(lambda: self.fail('cooldown must persist'))
        finally:
            release.set()
            if writer.is_alive():
                writer.terminate(); writer.join()

    def test_unknown_graphql_limit_recovery_across_processes(self):
        root = Path(self.tmp.name).resolve()
        one, two = root / 'one', root / 'two'
        one.mkdir(); two.mkdir()
        state = root / 'shared'
        result = multiprocessing.Queue()
        writer = multiprocessing.Process(target=unknown_limit_writer, args=(state, one, 1000, result))
        writer.start(); writer.join(5)
        self.assertEqual(writer.exitcode, 0)
        self.assertEqual(result.get(timeout=1), 'unavailable')
        for now, expected in ((1000, 'backoff'), (1060, 'cooldown'), (1120, 'ok')):
            reader = multiprocessing.Process(target=recovery_reader, args=(state, two, now, result))
            reader.start(); reader.join(5)
            self.assertEqual(reader.exitcode, 0)
            self.assertEqual(result.get(timeout=1), (expected, True))

    def test_version_two_quarantine_migrates_with_bounded_recovery(self):
        self.gate.request(lambda: ('ok', 1))
        path = next(Path(self.tmp.name).rglob('*.json'))
        state = json.loads(path.read_text())
        state['version'] = 2
        state['quarantined'] = True
        for key in ('stale', 'recovery_until', 'unknown_count'):
            del state[key]
        path.write_text(json.dumps(state))
        with self.assertRaises(admission.HostQuarantined):
            self.gate.request(lambda: self.fail('legacy quarantine'))
        self.now += 60
        self.assertEqual(self.gate.request(lambda: ('ok', 1)), 'ok')
        self.assertTrue(self.gate.status()['stale'])

    def test_rate_limit_headers_and_cooldown_expiry(self):
        cases = [
            (429, {}, False, 1060),
            (200, {'X-RateLimit-Remaining': '0', 'X-RateLimit-Reset': '1300'}, False, 1300),
            (200, {'X-RateLimit-Remaining': '0'}, False, 1060),
            (200, {'Retry-After': '120'}, True, 1120),
            (403, {'Retry-After': '120'}, False, 1120),
            (403, {'X-RateLimit-Remaining': '0', 'X-RateLimit-Reset': '1300'}, False, 1300),
            (403, {}, True, 1060),
            (403, {'Retry-After': 'Thu, 01 Jan 1970 00:20:00 GMT', 'X-RateLimit-Reset': '1300'}, True, 1300),
            (403, {'Retry-After': 'garbage'}, False, 1060),
            (403, {'X-RateLimit-Remaining': '0', 'X-RateLimit-Reset': '900'}, False, 1060),
        ]
        for status, headers, secondary, until in cases:
            with self.subTest(status=status, headers=headers), tempfile.TemporaryDirectory() as root:
                self.now = 1000
                gate = ledger(root, lambda: self.now)
                self.assertEqual(gate.request(lambda: admission.Response('response', 0, status, headers, secondary)), 'response')
                with self.assertRaises(admission.CooldownActive) as exc:
                    gate.request(lambda: self.fail('cooldown'))
                self.assertEqual(exc.exception.until, until)
                self.now = until
                self.assertEqual(gate.request(lambda: ('ok', 1)), 'ok')

    def test_unknown_cost_preserves_rate_limit_through_administrative_clear(self):
        with self.assertRaises(admission.StateError):
            self.gate.request(lambda: admission.Response('limited', None, 429, {'Retry-After': '120'}))
        with self.assertRaises(admission.HostQuarantined):
            self.gate.request(lambda: self.fail('unknown cost'))
        self.now += 60  # Simulate externally verified completion and a full window.
        self.gate.clear_quarantine()
        with self.assertRaises(admission.CooldownActive):
            self.gate.request(lambda: self.fail('cooldown survives clearing'))

    def test_unrepresentable_cooldown_quarantines_instead_of_shortening(self):
        with self.assertRaises(admission.StateError):
            self.gate.request(lambda: admission.Response('limited', 1, 429, {'Retry-After': '9999999999999'}))
        with self.assertRaises(admission.HostQuarantined):
            self.gate.request(lambda: self.fail('unsupported cooldown must fail closed'))

    def test_graphql_200_secondary_error_sets_cooldown(self):
        result = {'errors': [{'message': 'You have exceeded a secondary rate limit'}]}
        self.assertEqual(self.gate.request(lambda: admission.Response(result, 1, 200, {'Retry-After': '120'})), result)
        with self.assertRaises(admission.CooldownActive) as exc:
            self.gate.request(lambda: self.fail('secondary limit'))
        self.assertEqual(exc.exception.until, 1120)

    def test_unknown_backoff_escalates_and_survives_restart(self):
        for expected in (1060, 1180, 1420, 1900, 2380):
            with self.assertRaises(ConnectionError):
                self.gate.request(lambda: (_ for _ in ()).throw(ConnectionError()))
            restarted = ledger(self.tmp.name, lambda: self.now)
            self.assertTrue(restarted.status()['stale'])
            with self.assertRaises(admission.HostQuarantined) as exc:
                restarted.request(lambda: self.fail('backoff survives restart'))
            self.assertEqual(exc.exception.until, expected)
            self.now = expected
        self.assertEqual(self.gate.request(lambda: ('ok', 1)), 'ok')
        self.assertTrue(self.gate.status()['stale'])

    def test_429_without_cost_retains_backoff_and_retry_after(self):
        with self.assertRaises(admission.StateError):
            self.gate.request(lambda: admission.Response('limited', None, 429, {'Retry-After': '120'}))
        status = ledger(self.tmp.name, lambda: self.now).status()
        self.assertEqual((status['stale'], status['recovery_until'], status['cooldown_until']), (True, 1060, 1120))
        self.now = 1060
        with self.assertRaises(admission.CooldownActive):
            self.gate.request(lambda: self.fail('retry-after still active'))
        self.now = 1120
        self.assertEqual(self.gate.request(lambda: ('ok', 1)), 'ok')
        self.assertTrue(self.gate.status()['stale'])

    def test_crash_recovery_is_bounded_from_first_observation(self):
        p = multiprocessing.Process(target=crash, args=(self.tmp.name,))
        p.start(); p.join(5)
        self.assertEqual(p.exitcode, 7)
        state_file = next(Path(self.tmp.name).rglob('*.json'))
        stamp = json.loads(state_file.read_text())['last']
        self.now = stamp + 1000
        gate = ledger(self.tmp.name, lambda: self.now)
        with self.assertRaises(admission.HostQuarantined) as exc:
            gate.request(lambda: self.fail('crash recovery'))
        self.assertEqual(exc.exception.until, self.now + 60)
        self.now += 60
        self.assertEqual(ledger(self.tmp.name, lambda: self.now).request(lambda: ('ok', 1)), 'ok')
        self.assertTrue(gate.status()['stale'])

    def test_replaced_ledger_symlink_and_hardlink_fail_closed(self):
        self.gate.request(lambda: ('ok', 1))
        path = next(Path(self.tmp.name).rglob('*.json'))
        saved = path.with_name('saved')
        path.rename(saved)
        path.symlink_to(saved)
        with self.assertRaises(admission.StateError):
            self.gate.request(lambda: self.fail('symlink'))
        path.unlink()
        os.link(saved, path)
        with self.assertRaises(admission.StateError):
            self.gate.request(lambda: self.fail('hardlink'))

    def test_legacy_events_quarantine_because_unknown_cost_is_indistinguishable(self):
        self.gate.request(lambda: ('ok', 1))
        path = next(Path(self.tmp.name).rglob('*.json'))
        state = json.loads(path.read_text())
        state['version'] = 1
        for key in ('cooldown_until', 'stale', 'recovery_until', 'unknown_count'):
            del state[key]
        path.write_text(json.dumps(state))
        with self.assertRaises(admission.HostQuarantined):
            self.gate.request(lambda: self.fail('legacy state is ambiguous'))

    def test_host_normalization_and_no_credential_input(self):
        ledger(self.tmp.name).request(lambda: ('ok', 6))
        other = admission.GitHubAdmission(self.tmp.name, 'GITHUB.COM.', policy())
        other.request(lambda: ('ok', 6))
        with self.assertRaises(admission.BudgetExhausted):
            other.request(lambda: self.fail('same host'))
        for host in ('https://user:secret@github.com', 'github.com/path', ''):
            with self.assertRaises(ValueError):
                admission.GitHubAdmission(self.tmp.name, host, policy())
        self.assertNotIn('secret', ''.join(p.read_text() for p in Path(self.tmp.name).rglob('*.json')))


if __name__ == '__main__':
    unittest.main()
