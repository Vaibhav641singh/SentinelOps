"""Terminal client for the DevOps AI Copilot.

    python copilot.py error.log
    tail -50 /var/log/postgresql/postgresql.log | python copilot.py -
"""

import json
import os
import sys

import requests
import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel

load_dotenv()

app = typer.Typer(
    name="DevOps Copilot CLI",
    help="Diagnose server error logs and suggest safe recovery commands.",
    add_completion=False,
)
console = Console()

DEFAULT_URL = os.getenv("COPILOT_URL", "http://localhost:8000/diagnose")

# Connect fast, read slow: the web-patched path runs two Gemini calls plus web
# searches and has been measured at 65s.
TIMEOUTS = (10, 300)

STAGE_LABELS = {
    "retrieving": "Searching runbooks",
    "judging": "Auditing runbook for staleness",
    "web_search": "Searching the web",
    "synthesizing": "Writing recovery steps",
}


def read_log(logfile: str) -> str:
    if logfile == "-":
        return sys.stdin.read().strip()
    try:
        with open(logfile, "r", encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError as exc:
        console.print(f"[bold red]Could not read log file:[/bold red] {exc}")
        raise typer.Exit(code=1)


def iter_events(response: requests.Response):
    """Yield (event, data) pairs from a text/event-stream response.

    Frames are blank-line separated; a frame carries an `event:` name and a
    `data:` JSON payload.
    """
    buffer = ""
    for chunk in response.iter_content(chunk_size=None, decode_unicode=True):
        if not chunk:
            continue
        # decode_unicode yields str once the charset is known, bytes before that.
        buffer += chunk.decode("utf-8", "replace") if isinstance(chunk, bytes) else chunk
        while "\n\n" in buffer:
            frame, buffer = buffer.split("\n\n", 1)
            name, payload = "message", None
            for line in frame.splitlines():
                if line.startswith("event: "):
                    name = line[7:].strip()
                elif line.startswith("data: "):
                    payload = line[6:]
            if payload:
                yield name, json.loads(payload)


@app.command(no_args_is_help=True)
def diagnose(
    logfile: str = typer.Argument(..., help="Path to the log file, or '-' for stdin"),
    url: str = typer.Option(DEFAULT_URL, "--url", "-u", help="Backend Copilot API URL"),
    key: str = typer.Option("", "--key", "-k", help="API key (defaults to $APP_API_KEY)"),
    raw: bool = typer.Option(False, "--raw", help="Print plain text instead of rendered Markdown"),
):
    """Read a log, query the Copilot backend, and stream back the recovery plan."""
    log_content = read_log(logfile)
    if not log_content:
        console.print("[bold yellow]Warning:[/bold yellow] the log is empty.")
        raise typer.Exit(code=1)

    api_key = key or os.getenv("APP_API_KEY", "")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["X-API-Key"] = api_key

    source = "stdin" if logfile == "-" else logfile
    console.print(
        Panel(f"[bold blue]DevOps AI Copilot[/bold blue]\n[dim]Analyzing: {source}[/dim]")
    )

    try:
        response = requests.post(
            url,
            json={"error_log": log_content},
            headers=headers,
            stream=True,
            timeout=TIMEOUTS,
        )
    except requests.exceptions.ConnectionError:
        console.print(f"[bold red]Connection failed:[/bold red] could not reach {url}")
        console.print(
            "[dim]Start the backend with:  uvicorn copilot.api:app --port 8000[/dim]"
        )
        raise typer.Exit(code=1)
    except requests.exceptions.Timeout:
        console.print("[bold red]Timed out[/bold red] waiting for the backend to respond.")
        raise typer.Exit(code=1)

    if response.status_code == 401:
        console.print("[bold red]Unauthorized.[/bold red] This server requires an API key.")
        console.print("[dim]Pass --key, or set APP_API_KEY in your environment or .env[/dim]")
        raise typer.Exit(code=1)
    if response.status_code != 200:
        console.print(f"[bold red]API error ({response.status_code}):[/bold red] {response.text}")
        raise typer.Exit(code=1)

    answer = ""
    path = None
    sources: list[str] = []
    streaming = False

    try:
        for name, data in iter_events(response):
            if name == "stage":
                stage = data.get("stage")
                if stage == "retrieved":
                    weak = " [dim](too low)[/dim]" if data.get("weak_match") else ""
                    console.print(
                        f"  [green]|[/green] Matched [bold]{data['source']}[/bold] "
                        f"[dim]score {data['score']}[/dim]{weak}"
                    )
                elif stage == "skipped_judge":
                    console.print("  [green]|[/green] Audit skipped — no close match")
                elif stage == "web_results":
                    hits = sum(g["hits"] for g in data.get("per_finding", []))
                    console.print(f"  [green]|[/green] Web results in [dim]{hits} result(s)[/dim]")
                elif stage in STAGE_LABELS:
                    console.print(f"  [green]|[/green] {STAGE_LABELS[stage]}")

            elif name == "verdict":
                if data["current"]:
                    console.print(f"  [green]|[/green] [green]Runbook is current[/green] — {data['reason']}")
                else:
                    console.print(f"  [green]|[/green] [yellow]Runbook is outdated[/yellow] — {data['reason']}")
                    for finding in data.get("findings", []):
                        console.print(
                            f"      [yellow]-[/yellow] [bold]{finding['item']}[/bold]: {finding['problem']}"
                        )

            elif name == "token":
                if not streaming:
                    console.print("\n[bold green]=== Recovery Suggestions ===[/bold green]\n")
                    streaming = True
                answer += data["text"]
                if raw:
                    sys.stdout.write(data["text"])
                    sys.stdout.flush()

            elif name == "done":
                path = data.get("path")
                sources = data.get("sources", [])

            elif name == "error":
                console.print(f"\n[bold red]Backend error:[/bold red] {data['message']}")
                raise typer.Exit(code=1)

    except requests.exceptions.Timeout:
        console.print("\n[bold red]Timed out[/bold red] mid-response.")
        raise typer.Exit(code=1)

    # Re-render as Markdown once complete; --raw already printed it live.
    if answer and not raw:
        console.print(Markdown(answer))

    if sources:
        console.print("\n[dim]Web sources consulted:[/dim]")
        for src in sources:
            console.print(f"  [dim]- {src}[/dim]")

    if path:
        label = {
            "runbook": "[green]runbook was current[/green]",
            "web_patched": "[yellow]runbook was outdated, patched from the web[/yellow]",
            "no_match": "[dim]no matching runbook[/dim]",
        }.get(path, path)
        console.print(f"\n[dim]Path:[/dim] {label}")

    console.print(
        "\n[bold yellow]Review every command before running it on a production server.[/bold yellow]"
    )


if __name__ == "__main__":
    app()
