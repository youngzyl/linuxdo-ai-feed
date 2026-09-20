#!/usr/bin/env python3
"""linuxdo-ai-feed CLI.

  python3 run.py serve                 # scheduler + HTTP server (normal mode)
  python3 run.py cycle [--pages N]     # one fetch+filter cycle, prints JSON summary
  python3 run.py status                # health + counts (what the watchdog reads)
  python3 run.py keycheck [--probe]    # key presence / optional live probe
  python3 run.py fixture [--out path]  # regenerate fixtures/state.sample.json from real state
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from pathlib import Path

import config
from config import DATA_DIR, LOG_DIR, resolve_api_key
from pipeline import Pipeline, Scheduler
from server import App, build_server
from store import Store


def setup_logging(verbose: bool = False) -> logging.Logger:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("linuxdo_ai")
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    if not logger.handlers:
        fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%dT%H:%M:%SZ")
        fmt.converter = __import__("time").gmtime
        fh = logging.FileHandler(LOG_DIR / "server.log", encoding="utf-8")
        fh.setFormatter(fmt)
        sh = logging.StreamHandler(sys.stderr)
        sh.setFormatter(fmt)
        logger.addHandler(fh)
        logger.addHandler(sh)
    return logger


def build(cfg: dict, verbose: bool = False):
    logger = setup_logging(verbose)
    log = logger.info
    cfg = dict(cfg)
    cfg["_data_dir"] = str(DATA_DIR)
    store = Store(DATA_DIR / "state.json", LOG_DIR / "failures.jsonl")
    pipeline = Pipeline(cfg, store, log)
    return cfg, store, pipeline, log


def cmd_serve(args) -> int:
    cfg = config.load_config()
    cfg, store, pipeline, log = build(cfg, args.verbose)
    app = App(cfg, store, pipeline, log)
    httpd = build_server(cfg, app)
    scheduler = Scheduler(pipeline, log) if not args.no_scheduler else None
    log(f"serving on http://{cfg['host']}:{cfg['port']} (public={config.PUBLIC_DIR})")
    if scheduler:
        scheduler.start()
        log(f"scheduler started (interval {cfg['interval_minutes']} min)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log("shutdown requested")
    finally:
        if scheduler:
            scheduler.stop()
        httpd.server_close()
    return 0


def cmd_cycle(args) -> int:
    cfg = config.load_config()
    cfg, store, pipeline, log = build(cfg, args.verbose)
    result = pipeline.run_cycle(pages=args.pages, do_detail=not args.no_detail, do_filter=not args.no_filter)
    print(json.dumps(result, ensure_ascii=False, indent=1))
    return 0 if result.get("ok") else 1


def cmd_status(args) -> int:
    cfg = config.load_config()
    cfg, store, pipeline, log = build(cfg, args.verbose)
    payload = store.health_payload(cfg=cfg, uptime_s=0)
    payload["counts"] = store.counts()
    print(json.dumps(payload, ensure_ascii=False, indent=1))
    return 1 if payload["attention"]["needed"] else 0


def cmd_keycheck(args) -> int:
    cfg = config.load_config()
    key, source = resolve_api_key()
    out = {"key_present": bool(key), "key_source": source, "model": cfg["filter"]["model"]}
    if not key:
        out["hint"] = "set env commandcode_apikey, or put commandcode_apikey=... in .env"
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return 1
    out["key_len"] = len(key)
    if args.probe:
        import http_util

        cfg, store, pipeline, log = build(cfg, args.verbose)
        try:
            text = pipeline.filter._post_chat(
                key,
                [
                    {"role": "system", "content": "Reply with exactly: OK"},
                    {"role": "user", "content": "ping"},
                ],
                use_json_mode=False,
            )
            out.update(probe_ok=True, probe_reply=text.strip()[:60])
        except Exception as exc:
            out.update(probe_ok=False, probe_error=str(exc)[:300])
            print(json.dumps(out, ensure_ascii=False, indent=1))
            return 1
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


def cmd_fixture(args) -> int:
    cfg = config.load_config()
    cfg, store, pipeline, log = build(cfg, args.verbose)
    payload = store.api_payload(cfg=cfg, uptime_s=0)
    topics = payload["topics"]
    picked = [t for t in topics if t["state"] == "picked"]
    rejected = [t for t in topics if t["state"] == "rejected"]
    pending = [t for t in topics if t["state"] == "pending"]
    sample = picked[:20] + rejected[:25] + pending[:15]
    payload["topics"] = sample
    payload["fixture"] = {"generated_from_real_state": True, "topics": len(sample)}
    target = Path(args.out) if args.out else config.FIXTURE_DIR / "state.sample.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    public_copy = config.PUBLIC_DIR / "fixtures" / "state.sample.json"
    public_copy.parent.mkdir(parents=True, exist_ok=True)
    public_copy.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({"ok": True, "written": [str(target), str(public_copy)], "topics": len(sample)}, ensure_ascii=False))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="linux.do 人工智能 feed with an LLM filter")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("serve", help="run scheduler + HTTP server")
    p.add_argument("--no-scheduler", action="store_true")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("cycle", help="run one cycle now")
    p.add_argument("--pages", type=int, default=None)
    p.add_argument("--no-detail", action="store_true")
    p.add_argument("--no-filter", action="store_true")
    p.set_defaults(func=cmd_cycle)

    p = sub.add_parser("status", help="print health + counts")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("keycheck", help="check CommandCode key presence")
    p.add_argument("--probe", action="store_true")
    p.set_defaults(func=cmd_keycheck)

    p = sub.add_parser("fixture", help="regenerate the frontend fixture from real state")
    p.add_argument("--out", default=None)
    p.set_defaults(func=cmd_fixture)

    args = parser.parse_args(argv)
    os.chdir(config.ROOT)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
