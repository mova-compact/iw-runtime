# Release and rollback procedure

## Release

1. Update the version in `pyproject.toml` and describe contract/schema changes.
2. Regenerate both cross-platform locks with
   `uv pip compile --python-version 3.11 --universal --generate-hashes requirements.in -o requirements.txt`
   and `uv pip compile --python-version 3.11 --universal --generate-hashes requirements-dev.in -o requirements-dev.txt`.
3. Run `python scripts/check_release.py`, Ruff, Bandit, pip-audit, and
   `python -m scripts.docker_e2e`.
4. Merge only after all required CI jobs pass.
5. Create and push an annotated `vX.Y.Z` tag from the reviewed commit. The tag
   workflow rebuilds tests, creates a deterministic source archive and SBOM,
   signs each artifact keylessly with Sigstore, and publishes the release.
6. Verify the GitHub OIDC identity in each Sigstore bundle and compare
   `SHA256SUMS` before deployment. Deploy by immutable tag and record the tag,
   commit SHA, SBOM digest, and operator.

## Rollback

1. Stop new runs and preserve audit/run-state files for incident analysis.
2. Select the most recent previously verified release; never rebuild an old
   tag because that produces a different provenance event.
3. Verify its Sigstore identity and SHA256SUMS again, then deploy that exact
   archive. Restore only compatible persistent run records; leave incompatible
   `running` records stopped for explicit operator review.
4. Run `/health/ready`, the Docker E2E smoke test, and one no-side-effect sample
   workflow. Re-enable traffic only after all checks pass.
5. Record rollback reason, old/new tag and SHA, timestamps, and audit-chain
   verification result. Fix forward through a new reviewed tag; never move or
   overwrite an existing release tag.
