"""CLI: python -m darkgen <comando>"""

from __future__ import annotations

import argparse
import json
import logging
import sys


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="darkgen", description="Gerenciador de canais dark para YouTube")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("serve", help="painel web + API + MCP HTTP (/mcp) + agendador")
    sub.add_parser("mcp", help="servidor MCP via stdio (Claude Desktop / Claude Code)")
    sub.add_parser("channels", help="lista os canais e a saúde de cada um")
    p_new = sub.add_parser("new-channel", help="cria um canal")
    p_new.add_argument("name")
    p_new.add_argument("--preset", default=None, help="chave do modelo de nicho (veja 'presets')")
    sub.add_parser("presets", help="lista os modelos de nicho")
    p_plan = sub.add_parser("plan", help="preenche a agenda com vídeos automáticos")
    p_plan.add_argument("--channel", type=int, default=None)
    p_plan.add_argument("--days", type=int, default=None)
    p_work = sub.add_parser("work", help="processa a fila até esvaziar (sem agendador)")
    p_work.add_argument("--max", type=int, default=50)
    p_stats = sub.add_parser("stats", help="atualiza estatísticas do YouTube")
    p_stats.add_argument("--channel", type=int, default=None)
    args = parser.parse_args(argv)

    if args.cmd == "mcp":
        logging.basicConfig(level=logging.WARNING, stream=sys.stderr)  # stdout é do protocolo
        from .mcp_server import run_stdio

        run_stdio()
        return

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    from . import pipeline, service

    if args.cmd == "serve":
        import uvicorn

        from .config import env
        from .web.app import create_app

        uvicorn.run(create_app(), host=env().host, port=env().port)
    elif args.cmd == "channels":
        for r in service.portfolio()["channels"]:
            flag = {"ok": "OK ", "warning": "!! ", "critical": "XX "}[r["health"]]
            print(f"{flag}#{r['id']} {r['name']} [{r['language']}] ativo={r['active']} "
                  f"agendados={r['scheduled']} falhas={r['failed']}")
            for i in r["issues"]:
                print(f"     - {i['severity']}: {i['message']}")
    elif args.cmd == "new-channel":
        ch = service.create_channel(args.name, preset=args.preset)
        print(f"Canal #{ch['id']} criado. Conecte o YouTube: {service.connect_url(ch['id'])}")
    elif args.cmd == "presets":
        for p in service.list_presets():
            print(f"{p['key']:24} {p['label']}  —  {p['description']}")
    elif args.cmd == "plan":
        for v in pipeline.plan(args.channel, args.days):
            print(f"#{v['id']} canal={v['channel_id']} {v['kind']} {v['scheduled_at']}")
    elif args.cmd == "work":
        pipeline.recover_interrupted()
        for _ in range(args.max):
            if not pipeline.work_tick():
                break
    elif args.cmd == "stats":
        print(json.dumps(pipeline.refresh_stats(args.channel), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
