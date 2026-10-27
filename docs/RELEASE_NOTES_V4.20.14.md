# Local Agent 4.20.14

Fixes macOS repository registration failing on maintenance-menu exit with
`project ledger missing`. Registration previously saved the repository
before its private claims and result-spool directories existed; the strict
restart gate then correctly rolled back the registry.

New registrations now durably prepare the complete private ledger before
publishing the registry entry. Explicit retries reuse the existing canonical
binding and Git control branch. Failed preparation cannot publish a partial
project ledger. Existing history is preserved, and missing history in an
already registered project remains an error. Read-only diagnostics and the
pre-restart gate do not fabricate or repair state.

Regression coverage includes real local Git registration through maintenance
exit, interrupted publication and explicit retry, symlink rejection, missing
history and preservation of the owner's branch, index and dirty files.
Native service update and a fresh Git roundtrip require separate acceptance;
source tests alone are not deployment evidence.

Linux, Raspberry Pi and Android runtimes are unchanged by this macOS fix.
