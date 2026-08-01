# Security policy

Report vulnerabilities privately to the repository owner. Do not open a public
issue containing credentials, exploit details, or affected production data.

Supported releases are the latest signed `v*` release only. Every release must
include a source archive, CycloneDX SBOM, SHA256SUMS, and Sigstore bundles. Do
not deploy an artifact whose signature identity, tag, checksum, or SBOM cannot
be verified.

Secrets must be held in the OS keyring locally or protected CI secret storage.
Never commit `.env`, audit logs, run records, signing keys, or generated
credentials.
