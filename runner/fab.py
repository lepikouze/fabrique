#!/usr/bin/env python3
"""Commande `fab` donnée à l'agent pour parler à la Fabrique (via le relais local du runner, jeton propre au job).
  fab etape <triage|investigating|implementing> "résumé"
  fab question "la question, avec des options A / B"     (puis terminer)
  fab decision --confiant oui|non --verdict "…" --interpretation "…" [--hypothese "…"]… [--notes "…"]
  fab note "message"
  fab echec "la raison"                                  (puis terminer)"""
import argparse, json, os, sys, urllib.error, urllib.request


def send(body):
    req = urllib.request.Request(os.environ.get("FAB_RELAY", "http://127.0.0.1:7071") + "/fab", data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "X-Fab-Token": os.environ.get("FAB_TOKEN", "")})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            print(json.load(r).get("message", "ok"))
    except urllib.error.HTTPError as e:
        print("refusé : " + e.read().decode(errors="replace")[:300], file=sys.stderr)
        sys.exit(1)


def main(argv=None):
    p = argparse.ArgumentParser(prog="fab")
    sub = p.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("etape"); e.add_argument("etape", choices=["triage", "investigating", "implementing"]); e.add_argument("resume")
    q = sub.add_parser("question"); q.add_argument("texte")
    d = sub.add_parser("decision")
    d.add_argument("--confiant", choices=["oui", "non"], required=True); d.add_argument("--verdict", required=True)
    d.add_argument("--interpretation", required=True); d.add_argument("--hypothese", action="append", default=[])
    d.add_argument("--notes", default=""); d.add_argument("--recommandation", default="")
    n = sub.add_parser("note"); n.add_argument("texte")
    f = sub.add_parser("echec"); f.add_argument("raison")
    a = p.parse_args(argv)
    if a.cmd == "etape":
        send({"cmd": "stage", "stage": a.etape, "summary": a.resume})
    elif a.cmd == "question":
        send({"cmd": "question", "question": a.texte})
    elif a.cmd == "decision":
        send({"cmd": "decision", "confident": a.confiant == "oui", "verdict": a.verdict, "interpretation": a.interpretation,
              "assumptions": a.hypothese, "notes": a.notes,
              "recommendation": a.recommandation or ("L'agent recommande de fusionner." if a.confiant == "oui" else "L'agent préfère une relecture humaine.")})
    elif a.cmd == "echec":
        send({"cmd": "failed", "reason": a.raison})
    else:
        send({"cmd": "note", "message": a.texte})


if __name__ == "__main__":
    main()
