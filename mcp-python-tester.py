#!/usr/bin/env python3
"""
mcp_stdio_tester.py — minimal interactive client for testing an MCP server
that speaks the stdio transport (newline-delimited JSON-RPC 2.0).

Usage:
    python mcp_stdio_tester.py -- java -jar your-server.jar [server-args...]
    python mcp_stdio_tester.py --cwd ./server -- java -jar server.jar

Everything after "--" is the command used to launch the server.

Once running, you get a REPL with these commands:

    init                          send initialize + initialized handshake
    tools                         list available tools
    call <name> <json-args>       call a tool, e.g. call add {"a":1,"b":2}
    resources                     list resources
    read <uri>                    read a resource
    prompts                       list prompts
    prompt <name> <json-args>     get a prompt
    raw <json>                    send a raw JSON-RPC request object
    notify <method> <json-params> send a raw JSON-RPC notification
    quit / exit                   stop the server and exit

Server stderr is echoed live as "[server stderr] ..." so you can see logs.
Unsolicited messages (notifications) from the server print as "[notification]".
"""

import argparse
import json
import shlex
import subprocess
import sys
import threading
import time


class MCPClient:
    def __init__(self, command, cwd=None):
        print(f"[launching] {' '.join(command)}")
        self.proc = subprocess.Popen(
            command,
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            encoding="utf-8",
        )
        self._id = 0
        self._lock = threading.Lock()
        self._events = {}
        self._results = {}

        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read_stdout(self):
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                print(f"\n[non-JSON stdout] {line}")
                continue
            msg_id = msg.get("id")
            if msg_id is not None and msg_id in self._events:
                self._results[msg_id] = msg
                self._events[msg_id].set()
            else:
                print(f"\n[notification] {json.dumps(msg, indent=2)}")
        print("\n[server stdout closed]")

    def _read_stderr(self):
        for line in self.proc.stderr:
            print(f"[server stderr] {line.rstrip()}")

    def request(self, method, params=None, timeout=15):
        with self._lock:
            self._id += 1
            req_id = self._id
        req = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            req["params"] = params

        ev = threading.Event()
        self._events[req_id] = ev

        self._write(req)

        if ev.wait(timeout):
            return self._results.pop(req_id)
        return {"error": f"timeout waiting for response to {method} (id={req_id})"}

    def notify(self, method, params=None):
        n = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            n["params"] = params
        self._write(n)

    def _write(self, obj):
        line = json.dumps(obj)
        try:
            self.proc.stdin.write(line + "\n")
            self.proc.stdin.flush()
        except BrokenPipeError:
            print("[error] server process is no longer accepting input (did it crash?)")

    def alive(self):
        return self.proc.poll() is None

    def close(self):
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.terminate()
            self.proc.wait(timeout=3)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass


def pretty(result):
    print(json.dumps(result, indent=2))


def do_init(client):
    result = client.request(
        "initialize",
        {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "mcp-stdio-tester", "version": "0.1"},
        },
    )
    pretty(result)
    if "error" not in result.get("result", {}) and "result" in result:
        client.notify("notifications/initialized")
        print("[sent initialized notification]")


def parse_json_arg(s, default):
    s = s.strip()
    if not s:
        return default
    try:
        return json.loads(s)
    except json.JSONDecodeError as e:
        print(f"[error] couldn't parse JSON args: {e}")
        return None


def repl(client):
    print("\nType 'help' for commands, 'quit' to exit.\n")
    while True:
        if not client.alive():
            print("[server process has exited]")
            break
        try:
            line = input("mcp> ").strip()
        except EOFError:
            break
        if not line:
            continue

        if line in ("quit", "exit"):
            break
        if line == "help":
            print(__doc__)
            continue
        if line == "init":
            do_init(client)
            continue
        if line == "tools":
            pretty(client.request("tools/list"))
            continue
        if line == "resources":
            pretty(client.request("resources/list"))
            continue
        if line == "prompts":
            pretty(client.request("prompts/list"))
            continue

        if line.startswith("call "):
            rest = line[len("call "):].strip()
            parts = rest.split(None, 1)
            name = parts[0]
            args_str = parts[1] if len(parts) > 1 else "{}"
            args = parse_json_arg(args_str, {})
            if args is None:
                continue
            pretty(client.request("tools/call", {"name": name, "arguments": args}))
            continue

        if line.startswith("read "):
            uri = line[len("read "):].strip()
            pretty(client.request("resources/read", {"uri": uri}))
            continue

        if line.startswith("prompt "):
            rest = line[len("prompt "):].strip()
            parts = rest.split(None, 1)
            name = parts[0]
            args_str = parts[1] if len(parts) > 1 else "{}"
            args = parse_json_arg(args_str, {})
            if args is None:
                continue
            pretty(client.request("prompts/get", {"name": name, "arguments": args}))
            continue

        if line.startswith("raw "):
            obj = parse_json_arg(line[len("raw "):], None)
            if obj is None:
                continue
            method = obj.get("method")
            params = obj.get("params")
            pretty(client.request(method, params))
            continue

        if line.startswith("notify "):
            rest = line[len("notify "):].strip()
            parts = rest.split(None, 1)
            method = parts[0]
            params_str = parts[1] if len(parts) > 1 else "{}"
            params = parse_json_arg(params_str, {})
            if params is None:
                continue
            client.notify(method, params)
            print("[notification sent]")
            continue

        print("unknown command — type 'help'")


def main():
    parser = argparse.ArgumentParser(
        description="Interactive tester for MCP servers using the stdio transport."
    )
    parser.add_argument("--cwd", default=None, help="working directory to launch the server in")
    parser.add_argument("command", nargs=argparse.REMAINDER,
                         help="command to launch the server, after --")
    args = parser.parse_args()

    cmd = args.command
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        print("Usage: python mcp_stdio_tester.py -- java -jar your-server.jar [args...]")
        sys.exit(1)

    client = MCPClient(cmd, cwd=args.cwd)
    time.sleep(0.3)  # give the JVM a moment to boot before we start typing
    try:
        repl(client)
    finally:
        client.close()


if __name__ == "__main__":
    main()