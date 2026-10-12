"""Parser edge cases: unsupported syntax must not hide an engine-owned write."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from guard_commands import OWNED, denial_reason


class CommandTest(unittest.TestCase):
    def test_unlisted_forge_commands_are_denied_as_writes(self):
        for command in ('gh label delete stage:done --yes', 'gh label edit stage:done --name x',
                        'gh project item-archive 1 --owner owner --id item',
                        'gh project item-add 1 --owner owner --url https://github.com/o/r/issues/12',
                        'gh repo sync owner/repo --branch main --force', 'gh pr frobnicate',
                        'gh custom-merge-alias 12', 'gh alias set merge "pr merge"',
                        'gh extension exec custom-write', 'glab mr frobnicate',
                        'glab label delete stage:done', 'glab repo delete owner/repo',
                        'gh mr create', 'glab pr create', 'glab project item-add 1'):
            with self.subTest(command=command):
                self.assertEqual(denial_reason(command, ("main", "develop")), OWNED)

    def test_allowlisted_forge_reads_are_available(self):
        reads = {
            'gh': {'pr': ('view', 'list', 'diff', 'checks', 'status'),
                   'issue': ('view', 'list', 'status'), 'run': ('view', 'list', 'watch'),
                   'workflow': ('view', 'list'), 'repo': ('view', 'clone'),
                   'release': ('view', 'list'), 'label': ('list',),
                   'project': ('view', 'list', 'item-list', 'field-list'), 'auth': ('status',)},
            'glab': {'mr': ('view', 'list', 'diff'), 'issue': ('view', 'list'),
                     'ci': ('view', 'list', 'status', 'trace'), 'repo': ('view', 'clone'),
                     'label': ('list',), 'auth': ('status',)},
        }
        for client, groups in reads.items():
            for group, verbs in groups.items():
                for verb in verbs:
                    command = f'{client} {group} {verb}'
                    with self.subTest(command=command):
                        self.assertIsNone(denial_reason(command, ("main", "develop")))
        for command in ('gh search prs widget', 'gh search anything widget', 'gh status',
                        'gh help', 'gh help pr create', 'gh version',
                        'gh -R owner/repo pr view 12', 'glab mr --repo owner/repo view 12'):
            with self.subTest(command=command):
                self.assertIsNone(denial_reason(command, ("main", "develop")))

    def test_allowlisted_worker_writes_are_available(self):
        for command in ('gh pr create --title widget --body details', 'gh pr comment 12 --body feedback',
                        'gh issue comment 12 --body feedback', 'gh pr review 12 --comment --body feedback',
                        'gh pr review 12 -c -b feedback', 'gh pr checkout 12',
                        'glab mr create --title widget --description details', 'glab mr note 12 --message feedback',
                        'glab issue note 12 --message feedback', 'glab mr checkout 12'):
            with self.subTest(command=command):
                self.assertIsNone(denial_reason(command, ("main", "develop")))

    def test_allowed_issue_and_pr_verbs_still_protect_flags_and_approval(self):
        for command in ('gh pr create --draft', 'gh pr create --ready', 'gh pr create --label stage:done',
                        'glab mr create --draft', 'glab mr create --ready', 'glab mr create --wip',
                        'glab mr create --label stage:done', 'gh pr review 12 --approve', 'gh pr review 12 -a',
                        'gh pr review 12', 'gh pr review 12 --comment --approve',
                        'gh pr review 12 --request-changes -a', 'gh pr review 12 --request-changes --body x',
                        'gh pr review 12 -r -b x', 'gh issue view 12 --state closed',
                        'gh pr comment 12 --add-label stage:done', 'glab issue note 12 --close',
                        'gh pr create -lstage:done', 'glab mr create --labels=stage:done'):
            with self.subTest(command=command):
                self.assertEqual(denial_reason(command, ("main", "develop")), OWNED)

    def test_forge_api_preserves_read_only_method_rules(self):
        for client in ('gh', 'glab'):
            for arguments in ('repos/o/r/issues/12', '-X GET repos/o/r/issues/12',
                              '--method=GET repos/o/r/issues/12 -f page=1'):
                command = f'{client} api {arguments}'
                with self.subTest(command=command):
                    self.assertIsNone(denial_reason(command, ("main", "develop")))
            for arguments in ('-X POST repos/o/r/issues/12', '--method=PATCH repos/o/r/issues/12',
                              'repos/o/r/issues/12 -f state=closed',
                              "graphql -X GET -f query='mutation { write }'"):
                command = f'{client} api {arguments}'
                with self.subTest(command=command):
                    self.assertEqual(denial_reason(command, ("main", "develop")), OWNED)

    def test_forge_help_remains_available(self):
        for command in ('gh --help', 'glab --help', 'gh --version', 'glab --version',
                        'gh pr merge --help', 'glab mr merge --help', 'gh pr frobnicate --help'):
            with self.subTest(command=command):
                self.assertIsNone(denial_reason(command, ("main", "develop")))

    def test_dashed_git_executables_and_hub_use_git_checks(self):
        for command in ('/usr/lib/git-core/git-push origin HEAD:main', 'git-merge topic',
                        'git-push origin HEAD:refs/heads/develop', 'git-push', 'git-push --all',
                        'git-pull origin main', 'env FLAG=yes /usr/lib/git-core/git-merge topic',
                        'hub merge topic', 'hub push origin HEAD:main', 'hub status', 'env X=1 hub push'):
            with self.subTest(command=command):
                self.assertEqual(denial_reason(command, ("main", "develop")), OWNED)
        for command in ('git-status', '/usr/lib/git-core/git-diff',
                        'git-push -u origin HEAD:feat/widget', 'git-push origin feat/widget'):
            with self.subTest(command=command):
                self.assertIsNone(denial_reason(command, ("main", "develop")))
        for command in ('git-custom-merge-alias', 'docker exec container git-merge topic',
                        'docker exec container /usr/lib/git-core/git-push origin HEAD:main'):
            with self.subTest(command=command):
                self.assertIsNotNone(denial_reason(command, ("main", "develop")))

    def test_ordinary_worker_shell_commands_are_available(self):
        for command in ('echo $HOME', 'for f in *.py; do wc -l $f; done',
                        'pytest -k "a or b" 2>&1 | tail', 'python -c "print(1)"',
                        'sh -c "echo hello"', 'bash -lc "echo $HOME"',
                        'eval "echo hello"', 'find . | xargs wc -l'):
            with self.subTest(command=command):
                self.assertIsNone(denial_reason(command, ("main", "develop")))

    def test_unparsed_guarded_commands_fail_closed(self):
        for command in ('sh -c "gh pr merge 3"', 'git push origin $BRANCH',
                        "bash -lc 'cd x && git push origin HEAD:main'",
                        '$(printf gh) pr merge 3', '$(printf git) push origin HEAD:main',
                        'python -c "import os; os.system(\'gh pr merge 3\')"',
                        'python3.14 -c "import os; os.system(\'gh pr merge 3\')"',
                        '/usr/bin/pypy3 -c "import os; os.system(\'gh pr merge 3\')"',
                        'echo gh | xargs', 'echo glab | xargs', 'eval "hub merge 3"',
                        'sh -c "git status"',
                        'curl -X PATCH https://api.github.com/repos/$REPO/issues/3',
                        'curl -X PUT https://gitlab.com/api/v4/projects/$PROJECT/issues/3',
                        'git log --format=%H $x'):
            with self.subTest(command=command):
                self.assertIsNotNone(denial_reason(command, ("main", "develop")))

    def test_fallback_matches_guarded_program_words(self):
        for command in ('echo $github $gitlab $legit $hubcap',
                        'python -c "print(\'github\')"'):
            with self.subTest(command=command):
                self.assertIsNone(denial_reason(command, ("main", "develop")))

    def test_plain_clients_cannot_access_forge_api_endpoints(self):
        clients = ('curl', 'wget', 'http GET', 'https GET',
                   'python request.py', 'python3 request.py', 'node request.js', 'request-forge')
        endpoints = ('https://api.github.com/repos/o/r/issues/12',
                     'https://uploads.github.com/repos/o/r/releases/12/assets',
                     'https://github.com/api/graphql',
                     'https://github.example.com/api/v3/repos/o/r/issues/12',
                     'https://forge.example.com/api/graphql',
                     'https://forge.example.com/api/v3/repos/o/r/issues/12',
                     'https://gitlab.com/api/v4/projects/12/issues/3',
                     'https://labs.example.test/api/v4/projects/12/issues/3')
        for client in clients:
            for endpoint in endpoints:
                command = f'{client} {endpoint}'
                with self.subTest(command=command):
                    self.assertIsNotNone(denial_reason(command, ("main", "develop")))

    def test_forge_api_writes_wrappers_and_url_options_are_denied(self):
        for command in ('curl -X PATCH https://api.github.com/repos/o/r/issues/12 -d state=closed',
                        'curl -X POST https://uploads.github.com/repos/o/r/releases/12/assets -d payload',
                        'wget --method=PUT https://labs.example.test/api/v4/projects/12/issues/3',
                        'http PATCH https://api.github.com/repos/o/r/issues/12 state=closed',
                        'https PUT https://labs.example.test/api/v4/projects/12/issues/3 state_event=close',
                        'curl --url=https://API.GITHUB.COM/repos/o/r/issues/12',
                        'curl https://labs.example.test:8443/api/v4?query=issues',
                        'env FLAG=yes /usr/bin/curl https://uploads.github.com/repos/o/r/releases',
                        'true && curl https://api.github.com/repos/o/r/issues/12',
                        'docker exec container curl https://labs.example.test/api/v4/projects/12'):
            with self.subTest(command=command):
                self.assertIsNotNone(denial_reason(command, ("main", "develop")))

    def test_forge_api_text_in_unparsed_commands_fails_closed(self):
        for command in ('curl https://uploads.github.com/repos/$REPO/releases/12/assets',
                        'curl https://forge.example.com/api/v3/repos/$REPO/issues/12',
                        'curl https://forge.example.com/api/graphql -d "$QUERY"',
                        'curl https://labs.example.test/api/v4/projects/$PROJECT/issues/3'):
            with self.subTest(command=command):
                self.assertIsNotNone(denial_reason(command, ("main", "develop")))

    def test_other_http_endpoints_and_printed_forge_urls_are_available(self):
        for command in ('curl https://example.com', 'wget https://example.com',
                        'http GET https://example.com', 'https GET https://example.com',
                        'python request.py https://example.com', 'node request.js https://example.com',
                        'curl https://example.com/api/v40', 'curl https://example.com/api/graphqlish',
                        'echo https://api.github.com', 'printf "%s\\n" https://uploads.github.com',
                        'env FLAG=yes echo https://labs.example.test/api/v4/projects/12',
                        'printf "%s" https://github.example.com/api/graphql'):
            with self.subTest(command=command):
                self.assertIsNone(denial_reason(command, ("main", "develop")))

    def test_redirections_preserve_command_judgements(self):
        for command in ('git status > status.txt', 'git diff 2>&1 | tail',
                        'git status < input.txt', 'git status >> status.txt',
                        'git status 2> errors.txt', 'git status &> status.txt',
                        'git status > /tmp/gh', '> output.txt git status', '2> errors.txt git status',
                        "git log --format='a>b'"):
            with self.subTest(command=command):
                self.assertIsNone(denial_reason(command, ("main", "develop")))
        for command in ('gh pr merge 3 2>&1 | tail', 'git push origin HEAD:main > push.txt',
                        'gh issue edit 3 --add-label stage:done 2> errors.txt',
                        'gh project item-edit --id item > output.txt',
                        'true > output.txt; gh pr merge 3', 'git push origin > main',
                        "git push origin '>' HEAD:main"):
            with self.subTest(command=command):
                self.assertIsNotNone(denial_reason(command, ("main", "develop")))
        # A separated number is a refspec, not the redirection's file descriptor.
        self.assertIsNotNone(denial_reason('git push origin HEAD:feat/widget 2 > output.txt', ("2",)))

    def test_unparsed_execution_and_comments_cannot_hide_writes(self):
        for command in ("true # a comment\ngh pr merge 12", "time gh pr merge 12", "timeout 5 gh pr merge 12",
                        "nice -n 10 glab mr merge 12", "setsid git merge topic",
                        "docker exec container gh pr merge 12", "gh custom-merge-alias 12",
                        "true;\n\ngh pr merge 12", "true &&\n gh pr merge 12",
                        "git custom-merge-alias", "git pull origin main",
                        "env -S 'gh pr merge 12'", "git -c alias.m='merge topic' m",
                        "gh api --method=PATCH /repos/example/repo/issues/12 --input payload.json"):
            with self.subTest(command=command):
                self.assertIsNotNone(denial_reason(command, ("main", "develop")))

    def test_supported_wrapper_reads_are_available(self):
        for command in ("time git status", "timeout 5 gh pr view 12", "nice -n 10 git diff",
                        "true # a comment\ngit status", "env -u TOKEN git status",
                        "git status;\ngit diff", "git status\n\ngit diff", "git status &&\n git diff"):
            with self.subTest(command=command):
                self.assertIsNone(denial_reason(command, ("main", "develop")))


if __name__ == "__main__":
    unittest.main()
