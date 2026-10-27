# M10: independent human maintenance acceptance

Status: **PREPARED — NOT EXECUTED**. This protocol is ready for a Python
developer who did not author the refactor. No AI review can close this gate.

Freeze the candidate SHA after CI and technical review. Give the developer
only the repository, this protocol and normal library documentation. Do not
provide the implementation conversation or author coaching. For the first
measurement do not use an AI assistant; an assisted measurement is a separate
optional follow-up and must not replace it.

## Tasks and proposed thresholds from the accepted specification

| Task | Pass criterion |
|---|---|
| Clean installation | API and fast tests run within 60 minutes of active work; network download waiting logged separately |
| Find behavior ownership | Within 30 minutes identify retry, fencing, approval and READY owners with relevant tests |
| Unseen small change | Within 2 hours implement one isolated validation or additive diagnostic field+display with a distinguishing test |
| Diagnose a safe injected failure | Within 30 minutes identify stage, operation and supported next recovery action; no manual DB state rewrite |

The assessor writes the exact change request and hidden expected checks **after
candidate freeze**. The change must fit the stated class and current supported
behavior, not require cloud spend or new architecture. Do not disclose it to
the refactor author beforehand. Preserve the original thresholds after seeing
results; a failure is useful evidence for the next correction.

## Evidence record

Fill this in as an actual receipt; blanks are not a pass:

```text
Candidate commit:
Assessor / independence:
Developer experience:
OS / Python / Node:
Task prepared after freeze:
Exact task digest / private location of held-out assertions:
Start / end / active minutes for each task:
Network wait minutes:
All questions and answers / hints:
Changed files / patch commit:
Checks executed and raw outcomes:
Fault evidence and recovery decision:
PASS / FAIL / BLOCKED per task:
Remaining causes and follow-up:
```

Retain the tested code and results at the same SHA. If the exercise finds a
problem, fix the underlying documentation/interface/test gap and use a new
comparable task for another attempt. Report "technically verified, human
handoff unconfirmed" until this evidence exists.
