# Active implementation contract

Implement the 47 requirements of the SOLID specification, version 1.0, 2026-10-30.
Baseline: `0abb85c0a343b84248edfffaa8d8b844804bccb9`.
Source: [specification.txt](specification.txt), read from the authoritative Library DOCX.

Preserve process isolation, the single active run, transaction/fencing boundaries,
durable command identities and hashes, approvals, READY guards and stored formats.
Permitted behavior deltas: B01/B02 invalid input, B03 heartbeat and declared errors.
Other previously reported functional defects remain tracked separately.

Implementation and review in a separate branch; no merge or deployment.
Human acceptance T14 cannot be represented as executed by an AI.
