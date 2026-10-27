# Released operator wire fixture

`bea8639.json` was captured by `probe.py` against an immutable `git archive`
of `bea8639471537fb9c181089cdb027d69e31a041b`, before using candidate responses.
The independent reviewer authored this scenario from the preservation
requirements, without reusing assertions from existing tests.

The 29 calls preserve status, JSON including explicit nulls and nested public
redaction, plus selected response headers. Binary download bytes and headers
are also compared. All data and credentials are synthetic. Time and UUID
generation are fixed inside a separate process; no patch leaks into other tests.

Do not regenerate this fixture from a candidate merely to make a failing check
pass. An intentional wire change requires an explicit contract delta and a new
versioned fixture, with the previous behavior's compatibility decision recorded.

The fixture does not prove live infrastructure readiness or human handover.
The probe uses seeded persistent records for the acceptance endpoint rather
than executing an actual model or VM.
