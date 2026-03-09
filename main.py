#!/usr/bin/env python3
"""DevOps Agent — entry point."""
from __future__ import annotations

import uvicorn
import typer
from rich.console import Console

app = typer.Typer(help="DevOps Agent — manage deployments via AI chat")
console = Console()


@app.command()
def serve(
    host: str = typer.Option("0.0.0.0", help="Bind host"),
    port: int = typer.Option(8000, envvar="PORT", help="Bind port"),
    reload: bool = typer.Option(False, help="Auto-reload on code changes (dev mode)"),
):
    """Start the web server (chat UI + webhook receiver)."""
    console.print(f"[bold green]🚀 DevOps Agent[/] starting on http://{host}:{port}")
    uvicorn.run(
        "devops_agent.web.app:create_app",
        host=host,
        port=port,
        reload=reload,
        factory=True,
        log_level="info",
    )


if __name__ == "__main__":
    app()
