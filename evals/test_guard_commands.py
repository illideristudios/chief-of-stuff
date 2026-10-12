"""Parser edge cases: unsupported syntax must not hide an engine-owned write."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from guard_commands import denial_reason


class CommandTest(unittest.TestCase):
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
