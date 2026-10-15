"""Parser edge cases: unsupported syntax must not hide an engine-owned write."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from guard_commands import OWNED, UNPARSED, denial_reason


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
        for command in ('gh pr create --head feat/widget --title widget --body details', 'gh pr comment 12 --body feedback',
                        'gh issue comment 12 --body feedback', 'gh pr review 12 --comment --body feedback',
                        'gh pr review 12 -c -b feedback', 'gh pr checkout 12',
                        'glab mr create --title widget --description details', 'glab mr note 12 --message feedback',
                        'glab issue note 12 --message feedback', 'glab mr checkout 12'):
            with self.subTest(command=command):
                self.assertIsNone(denial_reason(command, ("main", "develop")))

    def test_comment_mutation_flags_are_denied(self):
        for group in ('issue', 'pr'):
            for flags in ('--delete-last --yes', '--edit-last --body replacement',
                          '--edit-last --create-if-none --body replacement',
                          '--create-if-none --edit-last=true --body replacement',
                          '--delete-last=true --yes', '--edit-last=false --body replacement'):
                for prefix in (f'gh {group} comment 12',
                               f'env FLAG=yes gh -R owner/repo {group} comment 12',
                               f'gh {group} --repo owner/repo comment 12'):
                    command = f'{prefix} {flags}'
                    with self.subTest(command=command):
                        self.assertEqual(denial_reason(command, ("main", "develop")), OWNED)

    def test_glab_note_mutations_are_denied(self):
        for command in ('glab mr note delete 99 12', 'glab mr note update 99 12 --message replacement',
                        'glab mr note --message replacement update 99 12',
                        'glab mr note -R owner/repo delete 99 12 --yes',
                        'glab mr note 12 --resolve thread', 'glab mr note 12 --unresolve thread',
                        'glab mr note resolve thread 12', 'glab mr note reopen thread 12'):
            with self.subTest(command=command):
                self.assertEqual(denial_reason(command, ("main", "develop")), OWNED)

    def test_additive_comments_remain_allowed(self):
        for group in ('issue', 'pr'):
            for flags in ('--body feedback', '-b feedback', '--body-file feedback.txt', '-F feedback.txt', '--editor'):
                command = f'gh {group} comment 12 {flags}'
                with self.subTest(command=command):
                    self.assertIsNone(denial_reason(command, ("main", "develop")))
        for command in ('glab mr note 12 --message feedback', 'glab issue note 12 -m feedback',
                        'glab mr note create 12 --message feedback', 'glab mr note -m feedback',
                        'glab mr note --message delete', 'glab mr note --message=feedback',
                        'glab mr note --message feedback 12',
                        'glab issue note 12 --message "see /close in the docs"'):
            with self.subTest(command=command):
                self.assertIsNone(denial_reason(command, ("main", "develop")))

    def test_glab_note_quick_actions_are_denied(self):
        for command in ('glab issue note 12 --message /close', 'glab mr note 12 --message /merge',
                        'glab issue note 12 --message="/approve"', 'glab mr note 12 -m "/label ~bug"',
                        'glab issue note 12 --message "LGTM\n/close"',
                        'glab mr note 12 --message=/close',
                        'glab issue note 12 --file note.md', 'glab mr note 12 --file=note.md'):
            with self.subTest(command=command):
                self.assertEqual(denial_reason(command, ("main", "develop")), OWNED)

    def test_rebase_execution_options_are_opaque(self):
        for command in ('git rebase -x "gh pr merge 12" HEAD~1',
                        'git rebase --exec "gh pr merge 12" HEAD~1',
                        'git rebase --exec="gh pr merge 12" HEAD~1',
                        'git rebase -ix "gh pr merge 12" HEAD~1',
                        'git rebase -xtrue HEAD~1', 'git-rebase -x true HEAD~1'):
            with self.subTest(command=command):
                self.assertEqual(denial_reason(command, ("main", "develop")), UNPARSED)
        self.assertIsNone(denial_reason('git rebase origin/develop', ("main", "develop")))

    def test_git_grep_pager_options_are_opaque(self):
        for command in ('git grep -O"sh -c \'gh pr merge 12; exit 1\' --" needle',
                        'git grep -O less needle', 'git grep -Oless needle',
                        'git grep --open-files-in-pager=less needle',
                        'git grep --open-files-in-pager less needle',
                        'git --no-pager grep -O less needle'):
            with self.subTest(command=command):
                self.assertEqual(denial_reason(command, ("main", "develop")), UNPARSED)
        self.assertIsNone(denial_reason('git grep needle', ("main", "develop")))

    def test_other_git_command_runners_remain_denied(self):
        for command in ('git bisect run true', 'git submodule foreach true',
                        'git difftool -x true', 'git difftool --extcmd=true',
                        'git filter-branch --tree-filter true HEAD'):
            with self.subTest(command=command):
                self.assertEqual(denial_reason(command, ("main", "develop")), UNPARSED)

    def test_forge_clone_forwarded_flags_use_git_clone_checks(self):
        for client in ('gh', 'glab'):
            for flags in ('--config core.sshCommand="sh -c \'gh pr merge 12; exit 1\' --"',
                          '-c core.sshCommand=true', '-ccore.sshCommand=true',
                          '--config=core.sshCommand=true', '--config credential.helper=true',
                          '--config=protocol.ext.allow=always', 'ext::true', 'fd::3'):
                for prefix in (f'{client} repo clone owner/repo dest',
                               f'env FLAG=yes {client} -R owner/repo repo clone owner/repo',
                               f'{client} repo --repo owner/repo clone owner/repo dest'):
                    command = f'{prefix} -- {flags}'
                    with self.subTest(command=command):
                        self.assertEqual(denial_reason(command, ("main", "develop")), UNPARSED)
            for flags in ('--depth 1', '--branch feature --single-branch', '--quiet', ''):
                command = f'{client} repo clone owner/repo dest -- {flags}'
                with self.subTest(command=command):
                    self.assertIsNone(denial_reason(command, ("main", "develop")))

    def test_pr_create_requires_an_explicit_nonempty_head(self):
        for command in ('gh pr create --title x --body y', 'gh pr create',
                        'gh pr create --head', 'gh pr create --head="" --title x --body y',
                        'gh pr create -H "" --title x --body y',
                        'gh pr create --title "--head" feat/x --body y',
                        'gh pr create --body "--head" feat/x --title x',
                        'gh pr create --head feat/x --head="" --title x --body y'):
            with self.subTest(command=command):
                reason = denial_reason(command, ("main", "develop"))
                self.assertIsNotNone(reason)
                self.assertIn('Push with git first', reason)
                self.assertIn('--head', reason)
        for command in ('gh pr create --head feat/x --title x --body y',
                        'gh pr create --head=feat/x --title x --body y',
                        'gh pr create -H feat/x --title x --body y',
                        'gh pr create --title x --body y --head owner:feat/x',
                        'gh pr create --head feat/x --base main --title x --body y'):
            with self.subTest(command=command):
                self.assertIsNone(denial_reason(command, ("main", "develop")))

    def test_pr_create_head_uses_push_branch_protection(self):
        for head in ('main', 'develop', 'owner:main', 'refs/heads/main',
                     'owner:refs/heads/develop', 'HEAD', 'feat/*', 'feat/x~1'):
            for flag in ('--head', '-H', '--head='):
                command = f'gh pr create {flag}{" " if flag != "--head=" else ""}{head} --title x --body y'
                with self.subTest(command=command):
                    self.assertEqual(denial_reason(command, ("main", "develop")), OWNED)
        self.assertIsNone(denial_reason('gh pr create --head main --title x --body y', ("release",)))

    def test_glab_create_push_options_are_denied(self):
        for flag in ('--push', '--push=true', '--fill', '--fill=true', '-f'):
            command = f'glab mr create {flag} --title x --description y'
            with self.subTest(command=command):
                self.assertEqual(denial_reason(command, ("main", "develop")), OWNED)
        self.assertIsNone(denial_reason('glab mr create --title x --description y', ("main", "develop")))

    def test_glab_mr_create_description_quick_actions_are_denied(self):
        for command in ('glab mr create --title x --description /merge',
                        'glab mr create --title x --description="/label ~bug"',
                        'glab mr create -t x -d "LGTM\n/merge"',
                        'glab mr create --title x --description=/close',
                        'glab mr create --title x --description-file note.md',
                        'glab mr create --title x --description-file=-',
                        'glab mr create --title x --description -'):
            with self.subTest(command=command):
                self.assertEqual(denial_reason(command, ("main", "develop")), OWNED)
        self.assertIsNone(denial_reason(
            'glab mr create --title x --description "see /merge in the docs"', ("main", "develop")))

    def test_project_membership_writes_are_denied(self):
        for command in ('gh pr create --head feat/x --title x --body y --project Roadmap',
                        'gh pr create --head feat/x --title x --body y -p Roadmap',
                        'gh pr create --head feat/x --title x --body y --project=Roadmap',
                        'gh issue create --title x --body y --project Roadmap',
                        'gh issue create --title x --body y -p Roadmap',
                        'gh pr edit 12 --add-project Roadmap', 'gh issue edit 12 --remove-project Roadmap'):
            with self.subTest(command=command):
                self.assertEqual(denial_reason(command, ("main", "develop")), OWNED)

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
                        'gh help pr create', 'gh pr create --help', 'glab mr create --help'):
            with self.subTest(command=command):
                self.assertIsNone(denial_reason(command, ("main", "develop")))

    def test_help_cannot_bypass_denied_verbs_or_protected_flags(self):
        for command in ('gh pr merge --help', 'glab mr merge --help', 'gh pr frobnicate --help',
                        'gh pr create --label stage:done --title --help --body x'):
            with self.subTest(command=command):
                self.assertEqual(denial_reason(command, ("main", "develop")), OWNED)

    def test_forge_bundles_and_attached_short_values_are_denied(self):
        for command in ('gh pr create -dlstage:done --title x --body y',
                        'gh pr create -ad --title x --body y', 'glab mr create -ad --title x',
                        'gh pr create -lstage:done', 'glab mr create -lstage:done',
                        'gh pr create -bfoo', 'glab mr create -bfoo',
                        'gh api -XGET repos/o/r/issues/12'):
            with self.subTest(command=command):
                self.assertEqual(denial_reason(command, ("main", "develop")), OWNED)
        for command in ('gh pr create -H feat/x -t x -b y', 'gh -R owner/repo pr create -H feat/x -t x -b y',
                        'gh pr -R owner/repo create -H feat/x -t x -b y',
                        'glab -R owner/repo mr view 12'):
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
                        'pytest -k "a or b" 2>&1 | tail', 'awk -f count.awk data.txt',
                        'sh -c "echo hello"', 'bash -lc "echo $HOME"',
                        'eval "echo hello"', 'find . | xargs wc -l', "ssh host 'ls -la'",
                        'grep -rn "a b" scripts', 'git commit -m "say gh pr merge is denied"',
                        'git config --get user.email', 'git config --list'):
            with self.subTest(command=command):
                self.assertIsNone(denial_reason(command, ("main", "develop")))

    def test_git_config_read_actions_must_precede_positionals(self):
        for command in ("git config core.sshCommand \"sh -c 'gh pr merge 12'\" --list",
                        'git config core.x y --get', 'git config core.x get',
                        'git config core.x list', 'git config --get core.x --list',
                        'git config get core.x --global', 'git config --unknown --get core.x',
                        'git config get --unknown core.x', 'git config --file --get core.x y',
                        'git config --get --unset core.x'):
            with self.subTest(command=command):
                self.assertEqual(denial_reason(command, ("main", "develop")), OWNED)

    def test_git_config_read_actions_remain_available(self):
        for command in ('git config --get user.email', 'git config --list', 'git config -l',
                        'git config --get-all remote.origin.fetch',
                        'git config --global --get-regexp ^remote\\.',
                        'git config --file settings --get core.x',
                        'git config --file=--get --list',
                        'git config get user.email', 'git config list',
                        'git config get --global user.email', 'git config list --local'):
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
                        'git log --format=%H $x',
                        "python3 -c'import os; os.system(\"gh pr merge 12\")'",
                        "perl -e'system(\"gh pr merge 12\")'", "ruby -we'system(\"gh pr merge 12\")'",
                        "php -r 'system(chr(103).chr(104).\" pr merge 12\");'", "lua -e 'os.execute(x)'",
                        "Rscript -e 'system(x)'", "osascript -e 'do shell script x'", "deno eval 'Deno.Command'",
                        "pwsh -Command 'x'", "php --run 'system(x);'",
                        "git clone -c \"core.sshCommand=sh -c 'gh pr merge 12; exit 1' --\" git@github.com:o/r /tmp/x",
                        "git clone --config=core.sshCommand=x git@github.com:o/r /tmp/x",
                        "git -c core.sshCommand=x fetch origin",
                        "node -p 'require(\"child_process\").execSync(\"gh pr merge 12\")'",
                        "ssh host 'gh pr merge 12'",
                        "tar -cf out.tar --checkpoint=1 --checkpoint-action=exec='gh pr merge 12' file",
                        "ssh host 'cd repo && git push origin HEAD:main'",
                        "git config core.sshCommand \"sh -c 'gh pr merge 12'\" && git fetch ssh://github.com/o/r",
                        'git config alias.ship "!gh pr merge 12"', 'git config --global credential.helper x',
                        "GIT_SSH_COMMAND=\"sh -c 'gh pr merge 12; exit 1' --\" git fetch ssh://github.com/o/r",
                        "env GIT_SSH_COMMAND=\"sh -c 'gh pr merge 12'\" git fetch origin",
                        "GIT_ALLOW_PROTOCOL=ext git fetch 'ext::sh -c gh% pr% merge% 12'",
                        "git fetch 'ext::sh -c x'", "git clone fd::3 dest",
                        "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=protocol.ext.allow GIT_CONFIG_VALUE_0=always "
                        "git push 'ext::sh -c gh% pr% merge% 12' HEAD:feature",
                        "git push 'ext::sh -c x' HEAD:feature", "GIT_CONFIG_GLOBAL=./evil git fetch origin",
                        "awk 'BEGIN{system(sprintf(\"%c%c\",103,104)\" pr merge 12\")}' /dev/null",
                        "python3 -c 'import os; os.system(chr(103)+chr(104)+\" pr merge 12\")'",
                        'python -c "print(1)"'):
            with self.subTest(command=command):
                self.assertIsNotNone(denial_reason(command, ("main", "develop")))

    def test_fallback_matches_guarded_program_words(self):
        for command in ('echo $github $gitlab $legit $hubcap',
                        'cat $(ls github*.txt)'):
            with self.subTest(command=command):
                self.assertIsNone(denial_reason(command, ("main", "develop")))

    def test_tokenization_errors_cannot_hide_inline_interpreters(self):
        commands = (
            'python3 -c \'import os;print("$");os.system(chr(103)+chr(104)+" pr merge 12")\'',
            '/usr/bin/python3.14 -c\'print("$")\'',
            'env FLAG=yes /usr/bin/pypy3 -c \'print("`")\'',
            'python3 -c \'print(1)',
            'python3 -c \'print("\x00")\'',
            '\'py\'thon3 "-c" \'print("$")\'',
            'perl -ne \'print "$x"\'', 'ruby -we \'puts "$"\'',
            'nodejs -p \'"$"\'', 'php --run=\'echo "$x";\'',
            'lua -e \'print("$")\'', 'Rscript -e \'print("$")\'',
            'osascript -e \'return "$"\'', 'deno eval \'"$"\'',
            'bun --eval=\'"$"\'', 'pwsh -Command \'Write-Output "$x"\'',
            'echo $HOME; python3 -c "print(1)"',
        )
        for command in commands:
            with self.subTest(command=command):
                self.assertEqual(denial_reason(command, ("main", "develop")), UNPARSED)
        for command in ('python3 script.py "$HOME"', 'node script.js "$HOME"'):
            with self.subTest(command=command):
                self.assertIsNone(denial_reason(command, ("main", "develop")))

    def test_bun_print_and_deno_eval_are_opaque(self):
        for command in ('bun -p "1 + 1"', 'bun -p\'1 + 1\'', 'bun -ip "1 + 1"',
                        'bun --print "1 + 1"', 'bun --print="1 + 1"',
                        'env FLAG=yes /usr/local/bin/bun -p "1 + 1"',
                        'bun1.2 -p "1 + 1"', 'bun -p \'"$"\'',
                        'bun --print=\'"$"\'', 'bun -e "1 + 1"',
                        'deno eval "1 + 1"', 'deno eval --print "1 + 1"',
                        'deno eval -p "1 + 1"', 'deno eval \'"$"\'',
                        'env FLAG=yes /usr/local/bin/deno eval "1 + 1"'):
            with self.subTest(command=command):
                self.assertEqual(denial_reason(command, ("main", "develop")), UNPARSED)
        for command in ('bun script.js', 'bun run script.js', 'deno run script.ts', 'deno --version'):
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



class WildcardProtectedBranchTest(unittest.TestCase):
    def denied(self, command, protected=("main", "release/*")):
        return denial_reason(command, protected) is not None

    def test_wildcard_protected_branch_is_denied(self):
        for command in ("git push origin HEAD:release/1.0", "git push origin +HEAD:refs/heads/release/1.0",
                        "git push origin HEAD:release/1.0/hotfix"):
            with self.subTest(command=command):
                self.assertTrue(self.denied(command))

    def test_unprotected_and_exact_branches_still_decided_exactly(self):
        self.assertFalse(self.denied("git push origin HEAD:work/issue-1"))
        self.assertFalse(self.denied("git push origin HEAD:releases/1.0"))
        self.assertTrue(self.denied("git push origin HEAD:main"))

    def test_gh_pr_create_head_matches_wildcards(self):
        self.assertTrue(self.denied("gh pr create --head release/1.0 --title t --body b"))


if __name__ == "__main__":
    unittest.main()
