import argparse
import os
import socket
import subprocess
import sys
import urllib.request
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[1]


def _parse_args():
    parser = argparse.ArgumentParser(description="Launch AVOCarbon Choke Costing Orchestrator UI")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true", help="Enable uvicorn reload mode for development")
    return parser.parse_args()


def _port_available(host, port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1)
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def _choke_page_running(host, port):
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/choke-costing", timeout=2) as response:
            return response.status == 200
    except Exception:
        return False


def main():
    args = _parse_args()
    os.chdir(BASE_DIR)
    if str(BASE_DIR) not in sys.path:
        sys.path.insert(0, str(BASE_DIR))

    url = f"http://{args.host}:{args.port}/choke-costing"
    mcp_sse_url = f"http://{args.host}:{args.port}/sse"
    mcp_http_url = f"http://{args.host}:{args.port}/mcp"
    if not _port_available(args.host, args.port):
        if _choke_page_running(args.host, args.port):
            print(f"AVOCarbon Choke Costing Orchestrator is already running: {url}")
            print(f"MCP SSE endpoint: {mcp_sse_url}")
            print(f"MCP streamable HTTP endpoint: {mcp_http_url}")
            return 0
        print(f"Port {args.port} is already in use by another process.")
        print(f"Try a different port:")
        print(f"  .\\.venv\\Scripts\\python.exe scripts\\launch_choke_costing_ui.py --port {args.port + 1}")
        print("Or stop the process currently using the port:")
        print(f"  Get-NetTCPConnection -LocalPort {args.port} | Select-Object OwningProcess")
        return 1

    print(f"Open {url}")
    print(f"MCP SSE endpoint: {mcp_sse_url}")
    print(f"MCP streamable HTTP endpoint: {mcp_http_url}")
    try:
        import uvicorn

        uvicorn.run("app.main:app", host=args.host, port=args.port, reload=args.reload)
    except Exception as exc:
        print(f"uvicorn Python launch failed: {exc}")
        print("Trying command-line uvicorn...")
        command = [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            args.host,
            "--port",
            str(args.port),
        ]
        if args.reload:
            command.append("--reload")
        return subprocess.call(command, cwd=BASE_DIR)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
