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

## What it does

| Job | Permissions | Runs app code | What it does |
|-----|-------------|---------------|--------------|
| `prepare` | `contents: read` | no | Personal data, committed keys, nothing to publish yet, dependencies (steps 1 to 4) |
| `sdk` | `id-token: write` | no, and checks nothing out | Fetches the Pippa SDK with the read-only SDK role (step 5) |
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
   - **What is scanned**: the pushed commits (`before..after` of the push event); the files of the checkout when the push created the branch or its `before` is not in the clone.
   - **Rules**: gitleaks' own, plus the allowlists in `.github/scripts/hack/gitleaks.toml`: `.env.example` (key names only), provider demo keys such as NASA's `DEMO_KEY`, obvious placeholders (`your-key-here`, `changeme`, `xxx`, `<...>`, `${NAME}`) and lines marked `pragma: allowlist secret` (the marker the template's own secret check accepts).
   - **The app's exceptions**: the allowlists (`[allowlist]` / `[[allowlists]]`) of a `.gitleaks.toml` at the top of the app repository are added; its rules and `[extend]` are ignored, so an app can skip a false positive but cannot switch the check off. Fingerprints in `.gitleaksignore` are skipped, and so is a line ending in a `gitleaks:allow` comment. An allowlist gitleaks cannot use is dropped (with a note) and the scan runs again without it.
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

5. Assumes `PippaHackSdkReadRole` and downloads the Pippa SDK wheel at the version and hash of the SDK parameter (`fetch-sdk.sh`). It hands `sdk.txt` (the hash-locked requirement) and the wheel to the check job as a cache entry of this run (`pippa-hack-sdk-<run id>`), never as an artifact: artifacts of a public repository can be downloaded by anyone signed in to GitHub. The job is not in an environment, because the role trusts the `ref:refs/heads/main` subject.

**Check** (runs the app's code, without credentials and without `id-token`)

6. Restores the hand-over and installs the SDK from it offline (`install-sdk.sh`: `--no-index`, `--require-hashes`, one hash-locked SDK requirement or nothing), then deletes it. The SDK is never stored in the app repository.
7. Installs `requirements-dev.txt` from PyPI and runs the app's `scripts/check.py` (tests included).
8. After a passed check, runs the app's `scripts/routing_check.py`. Its findings, the warnings of `scripts/check.py` and the commit-email warnings of a private repository never stop the publish; they are added to the commit comment of a published app.

**Publish** (runs no app code): packages the app (3 MB limit) and sends it to Pippa with a GitHub OIDC token for the publisher. Every invoke requests its own token, also when a throttled invoke is retried: Pippa accepts a token once. Before deploying anything, Pippa checks that the repository is linked to a builder's Pippa account, that the push was made by that builder, and that `manifest.yaml` has nothing it refuses.

## Result on the commit

| Outcome | Commit status `pippa/publish` | Commit comment |
|---------|-------------------------------|----------------|
| Published | `success` | `Your app <name> (<app_id> v<version>) is live. ...`, plus a `Warnings` section when Pippa, the personal data check, the check or the routing check found something (in that order; Pippa's own under `Pippa`, commit emails under `Personal data`) |
| Published, API keys missing | `success` (`Live: <app_id> v<version>; add its API keys in Pippa`) | `Your app ... is published, but it can't run until you add its API keys. ...` with Pippa's sentences (`... can't run until you add <NAME> ...`) in a block of their own; they are not among the warnings, because adding the key in Pippa (Created by me) fixes them without a push |
| Code error | `failure` | starts with `CODE_ERROR`, names what to fix, ends with `Fix the code and push again.` Personal data and committed keys found by the prepare job are reported this way too |
| Refused | `failure` (`Refused: <sentence>`) | starts with `REFUSED: the app was not published:`, then Pippa's sentence. Nothing was deployed, and the cause is not the app's code: the repository is not linked to a Pippa account (a fork or a copy), its owner has no account yet, or `manifest.yaml` has something Pippa does not accept. The publisher marks it `error_kind: "refused"` |
| Refused: the run itself | `failure` (`Refused: <sentence>`) | as above, with what to do and the reason code, for a refusal whose `reason` (or `error_reason`) is `actor_not_builder` (pushed by somebody other than the repository's builder), `token_replayed` (the token was already used), `not_a_push` or `builder_identity_unknown` (Pippa does not know the builder's GitHub account yet). Recognised by the sentence too (`Only the builder this repository belongs to can publish it`, `Only a push to main publishes`); a built-in sentence stands in when Pippa sends none |
| Platform error | `failure` | starts with `PLATFORM_ERROR` and a reference; the app is not at fault |

## Tests

```bash
python -m pip install --require-hashes -r .github/scripts/hack/requirements.txt
python -m pip install pytest
python -m pytest .github/scripts/hack/tests
```

The scripts are bash and the tests stub `gh`, `aws`, `pip` and `gitleaks`, so run them on Linux (or WSL) with LF line endings. The tests of `secret_scan.py` against the real gitleaks run when `HACK_TEST_GITLEAKS` names a gitleaks 8.30.1 binary (or `gitleaks` 8.30.1 is on `PATH`) and are skipped otherwise. `HACK_GITLEAKS_BIN` makes `prepare.sh` use a given gitleaks instead of the pinned download; it is ignored inside GitHub Actions.

`publish.py`, `secret_scan.py`, `gitleaks.toml`, `nothing_to_publish.py`, `personal_data.py` and their tests are the same files as in `Pippa-Labs/pippa-common-pipeline` (the private pipeline); change both together.
