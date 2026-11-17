# pippa-builders-pipeline

The build that publishes a Pippa hackathon app. Every app repository created from the hackathon template calls it on each push to `main`: it checks the app, publishes it to Pippa and reports the result on the pushed commit.

## Calling it

`.github/workflows/publish.yml` in the app repository:

```yaml
name: Publish to Pippa

on:
  push:
    branches: [main]

permissions:
  contents: write
  id-token: write
  statuses: write

jobs:
  publish:
    uses: Pippa-Labs/pippa-builders-pipeline/.github/workflows/reusable-hack-publish.yml@main
```

Input (optional):

| Input | Default | Description |
|-------|---------|-------------|
| `log_level` | `INFO` | `DEBUG`, `INFO`, `WARN` or `ERROR` |

Publishing works only from this workflow on `main` and only for app repositories of the hackathon; a copy of it elsewhere is refused.

## Who it runs for

Only a **push** to `main` of a `Pippa-Labs` repository named `pippa-hack-*` that is not a fork and not one of the two templates (`pippa-hack-template`, `pippa-hack-template-public`) gets past the first job (`prepare`). Every other run (started by hand, by a pull request, on another branch, in a fork or a copy) runs only the `skipped` job, which writes one line to the run summary, for example `Not published: only a push to main publishes; this run was started by workflow_dispatch.`, and publishes nothing. Pippa checks the same things again on its side from the run's identity token, and also that the push was made by the builder the repository belongs to.

A newer push to `main` cancels the run of an older one (the files are scanned on every run, so a cancelled run's keys are still caught by the next). Any other run gets a concurrency group of its own and cancels nothing, so a "Run workflow" click can never stop a push's run halfway through publishing. A re-run publishes only when the account that pushed starts it (`github.triggering_actor == github.actor`): a re-run keeps the pusher as `actor`, also in the token, so one started by anybody else is skipped with `Not published: this re-run was started by <account>, not by <pusher>, who pushed; ...`. "Re-run failed jobs" and "Re-run this job" keep the results of the jobs they do not run again, so every job (`prepare`, `sdk`, `check`, `publish`) and the step that assumes the publish role check this themselves; such a re-run by anybody else runs nothing (its jobs show as skipped, and the `skipped` job is not run again, so there is no summary line). `publish.py` checks it once more before anything else, because a re-run of single jobs replays the workflow of the first attempt (which, for a run made before this check existed, has none of these conditions) while the `hack-publish` action comes from `@main`: when `GITHUB_TRIGGERING_ACTOR` is not exactly `GITHUB_ACTOR`, or either is missing, it requests no token, sends nothing to Pippa and exits 77, and the job fails with `Not published: only the account that pushed can re-run a publish; nothing is reported on the commit`. Nothing is posted on the commit and no platform error is counted: the pusher's own result stays as it was.

## What it does

| Job | Permissions | Runs app code | What it does |
|-----|-------------|---------------|--------------|
| `prepare` | `contents: read` | no | Personal data, committed keys, nothing to publish yet, dependencies (steps 1 to 4) |
| `sdk` | `id-token: write` | no, and checks nothing out | Fetches the Pippa SDK with the read-only SDK role and seals it for the check job (step 5) |
| `check` | `contents: read` | **yes** | Installs the SDK and runs the app's checks (steps 6 to 8). It has no credentials and cannot request an OIDC token; `check-app.sh` refuses to run app code if it could |
| `publish` | `contents: write` (commit comment), `id-token: write`, `statuses: write`, environment `pippa-publish` | no | Packages and publishes the app and reports the result |
| `skipped` | none | no | Only when `prepare` does not run: says why in the run summary |

**Prepare** (runs no app code, has no credentials)

1. Looks for personal data, on every push and before anything else (`.github/scripts/hack/personal_data.py`). It cannot be skipped: it runs before any app code, and the run's identity is pinned to this workflow.
   - **Files**: every tracked text file (binary files and files over 1 MB are skipped). An email address is refused as a code error, unless it is a GitHub no-reply address (`@users.noreply.github.com`) or at a domain reserved for examples (`example.com`, `example.org`, `example.net` and their subdomains, `*.example`, `*.test`, `*.invalid`, `localhost`).
   - **Commits being pushed**: an author or committer email that is not a GitHub no-reply address (`...@users.noreply.github.com`, or `noreply@github.com` for edits made on github.com) is refused in a public repository, where everybody can read it for good, and is a warning in a private one. The repository's visibility comes from the push event. Which commits: from the event's `before` to the pushed commit; for a force push whose old tip the clone does not have, the commits the event lists (else just the pushed commit); for the first push of the branch, its whole history (at most 500 commits). Commits that were already on `main` are not checked again.
   - An address is never printed in full: only `file:line` (or the commit) and `a***@domain`.
   - It runs even when there is nothing to publish yet: if it refuses something, the run reports it on the commit like any code error.
2. Looks for committed keys, on every push (`.github/scripts/hack/secret_scan.py`). It runs [gitleaks](https://github.com/gitleaks/gitleaks) 8.30.1, downloaded from its GitHub release and refused unless its SHA-256 is the pinned one.
   - **What is scanned**: the files of the checkout on every run (what would be published), so a key that is still in the app fails every run until it is taken out: pushing again, or a newer push cancelling the run, never lets it through, and neither does a key committed before this check existed. On a push whose `before` is in the clone, also the pushed commits (`before..after`), because a key added and removed within one push is still in the history. Files up to 32 MB are scanned, the most the publisher accepts for a whole app.
   - **Rules**: gitleaks' own, plus the allowlists in `.github/scripts/hack/gitleaks.toml`: `.env.example` (key names only), provider demo keys such as NASA's `DEMO_KEY`, obvious placeholders (`your-key-here`, `changeme`, `xxx`, `<...>`, `${NAME}`) and lines marked `pragma: allowlist secret` (the marker the template's own secret check accepts).
   - **The app's exceptions**: the allowlists (`[allowlist]` / `[[allowlists]]`) of a `.gitleaks.toml` at the top of the app repository are added; its rules and `[extend]` are ignored. An allowlist with an entry that would skip nearly every key is not used, with a note: a `paths` pattern that matches an ordinary file such as `app.py`, `config.py` or `.env`, a `regexes` pattern that matches an ordinary key or a line holding one, a stopword shorter than 4 characters or found in an ordinary key, or a pattern Python's `re` cannot read (so it cannot be checked). So an app can skip a false positive but not switch the check off. Fingerprints in `.gitleaksignore` are skipped, and so is a line ending in a `gitleaks:allow` comment (the simplest way to mark a false positive). An allowlist gitleaks cannot use is dropped (with a note) and the scan runs again without it.
   - **A finding** is refused as a code error: `FAIL a value that looks like a key was committed in <file>:<line> (commit <sha>, gitleaks rule <rule>); remove it, rotate the key, use app_secrets`, followed by what to do (rotate the key at the provider first, declare it under `app_secrets` in `manifest.yaml`, add the value in Pippa under Created by me). The value never appears: gitleaks runs with `--redact`, and only the file, line, commit and rule are read from its report.
   - It runs even when there is nothing to publish yet, like the personal data check. If gitleaks cannot be downloaded or fails, the run is a platform error.
3. Skips the run ("Nothing to publish yet" in the run summary; no commit status, no comment) when `manifest.yaml` still has the placeholder app id `hack.your_app_name_here`, or has an `app_author_name` key whose value is blank (the template ships `app_author_name: ""`; Claude asks the builder for their public author name). A manifest without the key is not held back.
4. Refuses, as a code error, an app that:
   - names the Pippa SDK in a requirements file (the build installs it);
   - sets a package index or download location (`-i`, `--index-url`, `--extra-index-url`, `-f`, `--find-links`);
   - installs from a wheel, a local file, a folder, a URL or a VCS link, or includes a requirements file from outside the repository;
   - has a `.whl` file anywhere in the repository.

The prepare job checks out the whole history (`fetch-depth: 0`), so the personal data and secret checks can read the pushed commits.

**SDK** (runs no app code, checks nothing out)

5. Assumes `PippaHackSdkReadRole` and downloads the Pippa SDK wheel at the version and hash of the SDK parameter (`fetch-sdk.sh`). It hands `sdk.txt` (the hash-locked requirement) and the wheel to the check job as a cache entry of this run (`pippa-hack-sdk-<run id>-<attempt>`: an entry cannot be replaced, and "Re-run all jobs" seals with a new key, so each attempt of the job saves its own; the check job restores the entry the SDK job names in its output `handover_entry`, which "Re-run failed jobs" keeps together with the key), never as an artifact: artifacts of a public repository can be downloaded by anyone signed in to GitHub. A pull request's run (also one from a fork, once its workflow is approved) and a branch's run can restore the caches of `main`, and run ids are public, so the entry is **sealed**: `sdk.txt` and the wheel are packed into a tar archive and encrypted (`openssl enc`, AES-256-CBC with PBKDF2) with a random 256-bit key made for this run. The cache entry holds only `sdk.tar.enc`; the key leaves the job only as its output `handover_key`, which reaches this run's check job and no other run. The job is not in an environment, because the role trusts the `ref:refs/heads/main` subject.

**Check** (runs the app's code, without credentials and without `id-token`)

6. Restores the hand-over, opens it with `needs.sdk.outputs.handover_key` (passed to this one step only, never to the step that runs the app's code) and installs the SDK from it offline (`install-sdk.sh`: `--no-index`, `--require-hashes`, one hash-locked SDK requirement or nothing), then deletes the hand-over and everything opened from it. A hand-over in the clear, a missing or wrong key, or an archive holding anything but `sdk.txt` and one SDK wheel is refused before pip runs (a platform error). The SDK is never stored in the app repository.
7. Installs `requirements-dev.txt` from PyPI and runs the app's `scripts/check.py` (tests included).
8. After a passed check, runs the app's `scripts/routing_check.py`. Its findings, the warnings of `scripts/check.py` and the commit-email warnings of a private repository never stop the publish; they are added to the commit comment of a published app.

**Publish** (runs no app code): packages the app (3 MB limit; what is only for building it stays out: the folders `.git`, `.github`, `.claude` (Claude Code's settings, which the templates ship), `.idea`, `scripts`, `docs`, `vendor`, virtualenvs and caches at any depth, tests, `README.md`, `CLAUDE.md` and `requirements-dev.txt`) and sends it to Pippa with a GitHub OIDC token for the publisher. Links are never packaged or followed: `manifest.yaml` must be a regular file of the repository (a link, also one to a file of the repository, or a folder of that name is a code error), and it is opened without following links. Every invoke requests its own token, also when a throttled invoke is retried: Pippa accepts a token once. Before deploying anything, Pippa checks that the repository is linked to a builder's Pippa account, that the push was made by that builder, and that `manifest.yaml` has nothing it refuses.

## Re-runs of runs made before the split (until 2026-12-20)

Remove this section, hack-check's modes `sdk` and `clear` (`refuse-old-workflow.sh`, `clear-credentials.sh`), hack-publish's input `old_workflow` and `publish.py --old-workflow` after 2026-12-20.

Before the check was split into the `prepare`, `sdk` and `check` jobs, one `check` job ran hack-check with modes `prepare`, `sdk` (install the SDK with the SDK read role), `clear` (blank the AWS credentials) and `check`. GitHub replays that workflow when someone re-runs single jobs of one of its runs, for 30 days after the run, but takes the actions from `@main`, so they must still answer those modes:

- `clear` does what it did: writes `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN` and `AWS_SECURITY_TOKEN` as empty to `GITHUB_ENV`, deletes `$RUNNER_TEMP/hack-sdk` and any pip config that names CodeArtifact. Without it the mode would do nothing and the SDK role's credentials would stay in the job.
- `sdk` no longer installs anything: it fails with `::error title=Push a new commit to publish::Not published: this run was made by an older version of the Pippa publish workflow, which can no longer publish, so re-running it will not help. Push a new commit to main to publish your app.` That job could request an OIDC token and held the SDK role, so the app's code must never run there; its `Check the app` step is skipped after the failure.
- The publish job of that workflow then sees a check that failed without a verdict. hack-publish takes a missing `old_workflow` input as that workflow (the current workflow passes `old_workflow: "false"`), so the result is `REFUSED` with the same next step and `(Reason: old_workflow)`, not a platform error: no `InfraErrors` is counted. A refusal by the personal data, key or dependency checks (a `failed` verdict) is still a code error, and re-running only the publish job of an old run that had passed its check publishes as before, if Pippa accepts the re-run.

## Result on the commit

| Outcome | Commit status `pippa/publish` | Commit comment |
|---------|-------------------------------|----------------|
| Published | `success` | `Your app <name> (<app_id> v<version>) is live. ...`, plus a `Warnings` section when Pippa, the personal data check, the check or the routing check found something (in that order; Pippa's own under `Pippa`, commit emails under `Personal data`) |
| Published, API keys missing | `success` (`Live: <app_id> v<version>; add its API keys in Pippa`) | `Your app ... is published, but it can't run until you add its API keys. ...` with Pippa's sentences (`... can't run until you add <NAME> ...`) in a block of their own; they are not among the warnings, because adding the key in Pippa (Created by me) fixes them without a push |
| Code error | `failure` | starts with `CODE_ERROR`, names what to fix, ends with `Fix the code and push again.` Personal data and committed keys found by the prepare job are reported this way too |
| Refused | `failure` (`Refused: <sentence>`) | starts with `REFUSED: the app was not published:`, then Pippa's sentence. Nothing was deployed, and the cause is not the app's code: the repository is not linked to a Pippa account (a fork or a copy), its owner has no account yet, or `manifest.yaml` has something Pippa does not accept. The publisher marks it `error_kind: "refused"` |
| Refused: the run itself | `failure` (`Refused: <sentence>`) | as above, with what to do and the reason code, for a refusal whose `reason` (or `error_reason`) is `actor_not_builder` (pushed by somebody other than the repository's builder), `token_replayed` (the token was already used), `not_a_push`, `builder_identity_unknown` (Pippa does not know the builder's GitHub account yet), `stale_run` (a re-run of a run older than the one that published the live version: push a new commit), `rerun_untagged` (a re-run Pippa cannot date against the live version, as every app published before Pippa recorded the run meets until its next push: push a new commit), `visibility_not_allowed` (a repository that is not private calling the private pipeline: restore `publish.yml` from the public template) or `concurrent_publish` (another publish of the app ran at the same time: wait for it, then push again if it did not publish the latest commit). Recognised by Pippa's sentence too, for a publisher that sends the sentence without the reason (`Only the builder this repository belongs to can publish it`, `Only a push to main publishes`, `This publish used a GitHub sign-in token that had already been used`, `Pippa doesn't know which GitHub account this repository belongs to`, `This workflow run is older than the one that published the live version of this app`, `This is a re-run of an earlier workflow run, and Pippa cannot tell whether it is older`, `this repository is not private, so it cannot publish through the private pipeline`, `Another publish of this app is running at the same time`); a built-in sentence stands in when Pippa sends none |
| Refused: a re-run of the workflow before the split (until 2026-12-20) | `failure` (`Refused: This run was made by an older version of the Pippa publish workflow, ...`) | `REFUSED`, push a new commit, `(Reason: old_workflow)`; see [Re-runs of runs made before the split](#re-runs-of-runs-made-before-the-split-until-2026-12-20). No platform error is counted |
| Platform error | `failure` | starts with `PLATFORM_ERROR` and a reference; the app is not at fault |
| Re-run by another account | unchanged | none; the jobs are skipped, or the publish job fails before any token (`Not published: only the account that pushed can re-run a publish; ...` in its log) |

## Tests

```bash
python -m pip install --require-hashes -r .github/scripts/hack/requirements.txt
python -m pip install pytest
python -m pytest .github/scripts/hack/tests
```

The scripts are bash and the tests stub `gh`, `aws`, `pip` and `gitleaks` (`openssl` and GNU `tar` are used as they are), so run them on Linux (or WSL) with LF line endings. The workflow's `if` conditions are evaluated as GitHub does by `tests/workflow_expressions.py`, a small parser of the expression language (strings compare without case, `&&`/`||` return an operand, an `if` without a status function gets `success() &&`), so the tests can play a run or a re-run of single jobs through the real conditions. The tests of `secret_scan.py` against the real gitleaks run when `HACK_TEST_GITLEAKS` names a gitleaks 8.30.1 binary (or `gitleaks` 8.30.1 is on `PATH`) and are skipped otherwise. `HACK_GITLEAKS_BIN` makes `prepare.sh` use a given gitleaks instead of the pinned download; it is ignored inside GitHub Actions.

`publish.py`, `secret_scan.py`, `gitleaks.toml`, `nothing_to_publish.py`, `personal_data.py` and their tests, and `tests/workflow_expressions.py` with its tests, are the same files as in `Pippa-Labs/pippa-common-pipeline` (the private pipeline); change both together.
