# pippa-builders-pipeline

The build that publishes a Pippa hackathon app. Every app repository created from the hackathon template calls it on each push to `main`: it checks the app, publishes it to Pippa and reports the result on the pushed commit.

## Calling it

`.github/workflows/publish.yml` in the app repository:

```yaml
name: Publish to Pippa

on:
  push:
    branches: [main]
  workflow_dispatch:

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

## What it does

**Check** (runs the app's code)

1. Looks for personal data, on every push and before anything else (`.github/scripts/hack/personal_data.py`). It cannot be skipped: it runs before any app code, and the run's identity is pinned to this workflow.
   - **Files**: every tracked text file (binary files and files over 1 MB are skipped). An email address is refused as a code error, unless it is a GitHub no-reply address (`@users.noreply.github.com`) or at a domain reserved for examples (`example.com`, `example.org`, `example.net` and their subdomains, `*.example`, `*.test`, `*.invalid`, `localhost`).
   - **Commits being pushed**: an author or committer email that is not a GitHub no-reply address (`...@users.noreply.github.com`, or `noreply@github.com` for edits made on github.com) is refused in a public repository, where everybody can read it for good, and is a warning in a private one. The repository's visibility comes from the push event. Which commits: from the event's `before` to the pushed commit; for a force push whose old tip the clone does not have, the commits the event lists (else just the pushed commit); for the first push of the branch, its whole history (at most 500 commits); for `workflow_dispatch`, the commit being published. Commits that were already on `main` are not checked again.
   - An address is never printed in full: only `file:line` (or the commit) and `a***@domain`.
   - It runs even when there is nothing to publish yet: if it refuses something, the run reports it on the commit like any code error.
2. Skips the run ("Nothing to publish yet" in the run summary; no commit status, no comment) when `manifest.yaml` still has the placeholder app id `hack.your_app_name_here`, or has an `app_author_name` key whose value is blank (the template ships `app_author_name: ""`; Claude asks the builder for their public author name). A manifest without the key is not held back.
3. Refuses, as a code error, an app that:
   - names the Pippa SDK in a requirements file (the build installs it);
   - sets a package index or download location (`-i`, `--index-url`, `--extra-index-url`, `-f`, `--find-links`);
   - installs from a wheel, a local file, a folder, a URL or a VCS link, or includes a requirements file from outside the repository;
   - has a `.whl` file anywhere in the repository.
4. Installs the Pippa SDK at the version the apps run with. The SDK is never stored in the app repository, and no AWS credentials are left in the environment once it is installed. The job can still request a token for the read-only SDK role; that exposure is accepted.
5. Installs `requirements-dev.txt` from PyPI and runs the app's `scripts/check.py` (tests included).
6. After a passed check, runs the app's `scripts/routing_check.py`. Its findings, the warnings of `scripts/check.py` and the commit-email warnings of a private repository never stop the publish; they are added to the commit comment of a published app.

The check job checks out the whole history (`fetch-depth: 0`), so the personal data check can read the pushed commits.

**Publish** (runs no app code): packages the app (3 MB limit) and sends it to Pippa. Before deploying anything, Pippa checks that the repository is linked to a builder's Pippa account and that `manifest.yaml` has nothing it refuses.

## Result on the commit

| Outcome | Commit status `pippa/publish` | Commit comment |
|---------|-------------------------------|----------------|
| Published | `success` | `Your app <name> (<app_id> v<version>) is live. ...`, plus a `Warnings` section when Pippa, the personal data check, the check or the routing check found something (in that order; Pippa's own under `Pippa`, commit emails under `Personal data`) |
| Code error | `failure` | starts with `CODE_ERROR`, names what to fix, ends with `Fix the code and push again.` Personal data found by the check is reported this way too |
| Refused | `failure` (`Refused: <sentence>`) | starts with `REFUSED: the app was not published:`, then Pippa's sentence. Nothing was deployed, and the cause is not the app's code: the repository is not linked to a Pippa account (a fork or a copy), its owner has no account yet, or `manifest.yaml` has something Pippa does not accept. The publisher marks it `error_kind: "refused"` |
| Platform error | `failure` | starts with `PLATFORM_ERROR` and a reference; the app is not at fault |

## Tests

```bash
python -m pip install --require-hashes -r .github/scripts/hack/requirements.txt
python -m pip install pytest
python -m pytest .github/scripts/hack/tests
```
