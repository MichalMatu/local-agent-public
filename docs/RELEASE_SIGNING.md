# Signed release tags

New public releases should be bound to a reviewed exact source commit by a
cryptographically signed, annotated tag. Signing is a maintainer operation on
a trusted machine, using the maintainer's own GPG or SSH signing key.
**Never upload a private signing key or sign on behalf of another maintainer.**

## Before publishing a new release

1. In a clean, complete (non-shallow) checkout, fetch the intended upstream
   refs and tags. Confirm the remote and exact `HEAD` independently.
2. Run clean-source and affected native release checks. In particular run
   `python scripts/quality/security_audit.py --mode all` with Git and Gitleaks
   installed. The command must report PASS for both the current tree and
   **all locally reachable Git history**; UNVERIFIED or FAIL blocks release.
3. Review the maintainer's public signing key identity and fingerprint from
   an independent, trusted source. Set up Git signing *locally*. This repository
   does not contain signing secrets and does not assume any particular signer.
4. Create a new version tag against the exact reviewed source commit:

   ```sh
   SOURCE_SHA="$(git rev-parse HEAD)"
   git tag -s vNEXT_VERSION "$SOURCE_SHA" -m "Local Agent NEXT_VERSION"
   python scripts/release/verify_signed_tag.py \
     --tag vNEXT_VERSION --expected-sha "$SOURCE_SHA"
   ```

   Replace `vNEXT_VERSION` with a valid version, such as `v4.20.15`.
   If the signing backend is SSH, configure Git's local `gpg.format=ssh`
   and trusted allowed signers before verification. A passing cryptographic
   check **does not independently establish the signer's identity**.
5. Verify the signature signer against the out-of-band trusted identity,
   inspect the tag target, and only then publish the new tag and release:

   ```sh
   git push origin refs/tags/vNEXT_VERSION
   ```

   Do not force-push or retag a released version. A release must reference
   that signed tag, not an independently moving branch. Attach checksums,
   source identity and verification evidence without exposing private logs.

## Validation and rollback

The verifier returns `PASS` (0), `FAIL` (1), or `UNVERIFIED` (2).
Treat both nonzero outcomes as release blockers; never silently downgrade to
an unsigned release. On a signing error **before publishing**, delete only
the unpublished local candidate tag and recreate it after correcting the
configuration. Never rewrite a tag already visible to customers.

`v4.20.14` is a historical **unsigned** annotated tag. It cannot be made
historically signed without replacing the published tag. The signing gate
applies to new releases. For existing installations keep the pinned source
SHA, immutable release artifacts and independently checked checksums.

[Security policy](../SECURITY.md) · [quality](QUALITY_TOOLING.md) ·
[installation](../INSTALL_WITH_CODEX.md).
