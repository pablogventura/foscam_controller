#!/usr/bin/env python3
"""
CLI unificado para gestionar cámaras Foscam.
Comandos: discover, config, apply, view, console.
"""

import argparse
import sys

from foscam.client import FoscamClient
from foscam.discover import FoscamDiscoverer
from foscam.config_io import FoscamConfigReader, FoscamConfigWriter


def _client_from_args(args) -> FoscamClient:
    return FoscamClient(
        host=args.ip,
        user=args.user,
        password=args.password,
        port=getattr(args, "port", 88),
    )


def cmd_discover(args) -> None:
    discoverer = FoscamDiscoverer(network_range=args.network)
    discoverer.discover(max_workers=args.workers, show_progress=True)
    discoverer.print_results()


def cmd_config(args) -> None:
    client = _client_from_args(args)
    reader = FoscamConfigReader(client)
    config = reader.get_all_config(show_progress=True)
    reader.print_config(config)
    if args.save:
        reader.save_to_file(args.save, config)


def cmd_apply(args) -> None:
    client = _client_from_args(args)
    writer = FoscamConfigWriter(client)
    writer.apply_from_file(args.file, dry_run=args.dry_run, show_progress=True)


def cmd_view(args) -> None:
    from foscam.viewer import main as viewer_main
    sys.argv = [
        "foscam-viewer",
        "--ip", args.ip,
        "--user", args.user,
        "--password", args.password,
        "--port", str(getattr(args, "port", 88)),
    ]
    if getattr(args, "sub", False):
        sys.argv.append("--sub")
    if getattr(args, "nvidia", False):
        sys.argv.append("--nvidia")
    if getattr(args, "audio_gate_db", None) is not None:
        sys.argv.extend(["--audio-gate-db", str(args.audio_gate_db)])
    viewer_main()


def cmd_console(args) -> None:
    from foscam.cgi_console import main as console_main
    sys.argv = [
        "foscam-console",
        "--ip", args.ip,
        "--user", args.user,
        "--password", args.password,
        "--port", str(getattr(args, "port", 88)),
    ]
    console_main()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Gestionar cámaras Foscam: descubrir, configurar, ver stream, consola CGI.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Ejemplos:
  %(prog)s discover
  %(prog)s discover --network 192.168.1.0/24 --workers 100
  %(prog)s config --ip 192.168.1.6 --user admin --password xxx --save cam.json
  %(prog)s apply --ip 192.168.1.7 --user admin --password xxx --file cam.json --dry-run
  %(prog)s view --ip 192.168.1.6 --user admin --password xxx
  %(prog)s console --ip 192.168.1.6 --user admin --password xxx
        """,
    )
    subparsers = parser.add_subparsers(dest="command", help="Comando")

    # discover
    p_discover = subparsers.add_parser("discover", help="Descubrir cámaras en la red")
    p_discover.add_argument("--network", type=str, help="Rango (ej: 192.168.1.0/24). Por defecto auto.")
    p_discover.add_argument("--workers", type=int, default=50, help="Hilos de escaneo (default: 50)")
    p_discover.set_defaults(func=cmd_discover)

    # config
    p_config = subparsers.add_parser("config", help="Obtener configuración de una cámara")
    p_config.add_argument("--ip", required=True, help="IP de la cámara")
    p_config.add_argument("--user", required=True, help="Usuario")
    p_config.add_argument("--password", required=True, help="Contraseña")
    p_config.add_argument("--port", type=int, default=88, help="Puerto (default: 88)")
    p_config.add_argument("--save", type=str, help="Guardar en archivo JSON")
    p_config.set_defaults(func=cmd_config)

    # apply
    p_apply = subparsers.add_parser("apply", help="Aplicar configuración desde JSON")
    p_apply.add_argument("--ip", required=True, help="IP de la cámara")
    p_apply.add_argument("--user", required=True, help="Usuario")
    p_apply.add_argument("--password", required=True, help="Contraseña")
    p_apply.add_argument("--port", type=int, default=88, help="Puerto (default: 88)")
    p_apply.add_argument("--file", required=True, help="Archivo JSON con la configuración")
    p_apply.add_argument("--dry-run", action="store_true", help="Solo mostrar qué se aplicaría")
    p_apply.set_defaults(func=cmd_apply)

    # view
    p_view = subparsers.add_parser("view", help="Abrir visor de video en vivo (GUI)")
    p_view.add_argument("--ip", required=True, help="IP de la cámara")
    p_view.add_argument("--user", required=True, help="Usuario")
    p_view.add_argument("--password", required=True, help="Contraseña")
    p_view.add_argument("--port", type=int, default=88, help="Puerto (default: 88)")
    p_view.add_argument("--sub", action="store_true", help="Usar sub stream (menor resolución)")
    p_view.add_argument("--nvidia", action="store_true", help="Decodificación GPU NVIDIA")
    p_view.add_argument(
        "--audio-gate-db",
        type=float,
        default=None,
        metavar="dB",
        help="Puerta de ruido en dB (por defecto: valor guardado o -38)",
    )
    p_view.set_defaults(func=cmd_view)

    # console
    p_console = subparsers.add_parser("console", help="Consola interactiva para comandos CGI")
    p_console.add_argument("--ip", required=True, help="IP de la cámara")
    p_console.add_argument("--user", required=True, help="Usuario")
    p_console.add_argument("--password", required=True, help="Contraseña")
    p_console.add_argument("--port", type=int, default=88, help="Puerto (default: 88)")
    p_console.set_defaults(func=cmd_console)

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        sys.exit(0)
    args.func(args)


if __name__ == "__main__":
    main()
