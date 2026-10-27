# Released identity and serialization fixture

`bea8639.json` was captured by `probe.py` against an immutable `git archive`
of `bea8639471537fb9c181089cdb027d69e31a041b`. The independent reviewer
authored the scenario from the preservation requirements, before comparing
the candidate. All data is synthetic.

The fixture preserves command request JSON and its content hash, distinct
absent/null JSON hashes, and the three runner/provider JSON Schemas.
Model schemas, stable operation identifiers and deterministic executable ZIP
contents have separate baseline vectors in the orchestration contract tests.

Do not replace this fixture with candidate output to silence a compatibility
failure. An intentional protocol change needs an explicit contract delta and
a separate versioned fixture. This check does not exercise live services.
