# Offline tests

`sh tests/run.sh` — runs every `t_*.py` in the service image with `app/` mounted. No model, network or cluster:
each model answer is a stand-in that makes the mistakes the real model made on the cluster.

| file | what it replays |
|---|---|
| `t_labels.py` | re-prompts refused because the model copied `[role: …]` into a name; the one retry after a refusal |
| `t_answer.py` | a question answered from the MoM, nothing changed; a rewrite that keeps the history for undo |
| `t_pronoun.py` | "his/her/their" with no name = the person changed last; asked when unclear |
| `t_spelling.py` | spelling corrections checked (never names, owners, numbers); replies in plain sentences |
| `t_group.py` | ITEM grouping: plain-line replies, separate calls, decisions and figures placed by matching |
| `t_limits.py` | no size limits: long text read in parts with nothing lost, every scanned page read, big MoMs re-promptable |
| `t_speed.py` | faster, same minutes: 1 vs 8 calls at a time give identical minutes; the gate; parallel OCR; "Try again" joins |
| `fake_model.py` | the stand-in model for `t_speed.py`: same question → same answer, with a delay like the real one |

`t_limits.py` and `t_speed.py` also use the scanned PDFs and long texts in the repo folder (`scanned-transcripts/`,
`t5.txt`, not in git) when they are there, and skip those checks when not.

They used to live only in a session's scratch folder and were lost with it (2026-09-22).
