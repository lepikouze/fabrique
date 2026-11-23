#!/usr/bin/env python3
"""`fab` command given to the agent to talk to Fabrique (through the runner's local relay, token specific to the job).
  fab step <triage|investigating|implementing> "summary"
  fab question "the question, with options A / B"        (then finish)
  fab decision --confident yes|no --verdict "…" --interpretation "…" [--assumption "…"]… [--notes "…"]
  fab note "message"
  fab fail "the reason"                                  (then finish)"""
import argparse, json, os, sys, urllib.error, urllib.request


def send(body):
    req = urllib.request.Request(os.environ.get("FAB_RELAY", "http://127.0.0.1:7071") + "/fab", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "X-Fab-Token": os.environ.get("FAB_TOKEN", "")})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            print(json.load(r).get("message", "ok"))
    except urllib.error.HTTPError as e:
        print("refused: " + e.read().decode(errors="replace")[:300], file=sys.stderr)
        sys.exit(1)


def main(argv=None):
    p = argparse.ArgumentParser(prog="fab")
    sub = p.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("step"); e.add_argument("stage", choices=["triage", "investigating", "implementing"]); e.add_argument("summary")
    q = sub.add_parser("question"); q.add_argument("text")
    d = sub.add_parser("decision")
    d.add_argument("--confident", choices=["yes", "no"], required=True); d.add_argument("--verdict", required=True)
    d.add_argument("--interpretation", required=True); d.add_argument("--assumption", dest="assumptions", action="append", default=[])
    d.add_argument("--notes", default=""); d.add_argument("--recommendation", default="")
    n = sub.add_parser("note"); n.add_argument("text")
    f = sub.add_parser("fail"); f.add_argument("reason")
    a = p.parse_args(argv)
    if a.cmd == "step":
        send({"cmd": "stage", "stage": a.stage, "summary": a.summary})
    elif a.cmd == "question":
        send({"cmd": "question", "question": a.text})
    elif a.cmd == "decision":
        # No default recommendation here: the dashboard words it from "confident", in each reader's language.
        send({"cmd": "decision", "confident": a.confident == "yes", "verdict": a.verdict, "interpretation": a.interpretation,
              "assumptions": a.assumptions, "notes": a.notes, "recommendation": a.recommendation})
    elif a.cmd == "fail":
        send({"cmd": "failed", "reason": a.reason})
    else:
        send({"cmd": "note", "message": a.text})


if __name__ == "__main__":
    main()
