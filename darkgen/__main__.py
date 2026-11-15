"""CLI: python -m darkgen <comando>"""

from __future__ import annotations

import argparse
import logging
import sys


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="darkgen", description="Gerador de conteúdo dark para YouTube")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="painel web + API + MCP HTTP (/mcp) + agendador")
    sub.add_parser("mcp", help="servidor MCP via stdio (Claude Desktop / Claude Code)")
    p_plan = sub.add_parser("plan", help="preenche a agenda com vídeos automáticos")
    p_plan.add_argument("--days", type=int, default=None)
    p_work = sub.add_parser("work", help="processa a fila até esvaziar (sem agendador)")
    p_work.add_argument("--max", type=int, default=50)
    sub.add_parser("stats", help="atualiza estatísticas do YouTube")
    args = parser.parse_args(argv)

    if args.cmd == "mcp":
        logging.basicConfig(level=logging.WARNING, stream=sys.stderr)  # stdout é do protocolo
        from .mcp_server import run_stdio

        run_stdio()
        return

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.cmd == "serve":
        import uvicorn

        from .config import env
        from .web.app import create_app

        uvicorn.run(create_app(), host=env().host, port=env().port)
    elif args.cmd == "plan":
        from . import pipeline

        for v in pipeline.plan(args.days):
            print(f"#{v['id']} {v['kind']} {v['scheduled_at']}")
    elif args.cmd == "work":
        from . import pipeline

        pipeline.recover_interrupted()
        for _ in range(args.max):
            if not pipeline.work_tick():
                break
    elif args.cmd == "stats":
        from . import pipeline

        print(pipeline.refresh_stats())


if __name__ == "__main__":
    main()
