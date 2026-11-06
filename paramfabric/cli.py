"""Typer command-line interface"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode

import typer
import yaml
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.tree import Tree

from paramfabric.auditors import audit_sources
from paramfabric.core.graph import ParameterGraph
from paramfabric.core.schema import Fabric, Parameter, load_fabric
from paramfabric.generators.cpp import generate_cpp
from paramfabric.generators.docs import generate_latex, generate_markdown
from paramfabric.generators.python import generate_python
from paramfabric.generators.rust import generate_rust
from paramfabric.integrations.cad_nx import generate_nx_journal
from paramfabric.integrations.cad_solidworks import generate_solidworks_macro
from paramfabric.integrations.openscad import generate_openscad
from paramfabric.integrations.simulation import export_solver
from paramfabric.security.auth import issue_access_token
from paramfabric.utils.git_tools import fabric_at_revision, parameter_diff, parameter_history

sync_app = typer.Typer(help="Synchronize a local fabric with the enterprise server.")
baseline_app = typer.Typer(help="Manage immutable enterprise baselines.")
app = typer.Typer(
    name="paramfabric",
    help="A unified source of truth for engineering parameters.",
    no_args_is_help=True,
)
app.add_typer(sync_app, name="sync")
app.add_typer(baseline_app, name="baseline")
console = Console()


def fabric_path() -> Path:
    """Select the conventional YAML or JSON source file"""
    if Path("params.yaml").exists():
        return Path("params.yaml")
    if Path("params.yml").exists():
        return Path("params.yml")
    return Path("params.json")


def load_current() -> Fabric:
    return load_fabric(fabric_path())


def parse_value(value: str) -> float | int | bool | str:
    if value.lower() in {"true", "false"}:
        return value.lower() == "true"
    if re.fullmatch(r"[+-]?\d+", value):
        return int(value)
    if re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?", value):
        return float(value)
    return value


@app.command()
def init() -> None:
    """Create a starter params.yaml in the current directory"""
    destination=Path("params.yaml")
    if destination.exists():
        raise typer.BadParameter(f"{destination} already exists")
    template = {
        "version": 1,
        "parameters": [
            {
                "name": "root_chord",
                "value": 2.0,
                "unit": "m",
                "type": "float",
                "description": "Wing root chord.",
                "rationale": "Initial reference geometry.",
                "tags": ["aerodynamics", "geometry"],
                "bindings": {
                    "cpp_header": "generated/params.hpp",
                    "py_module": "generated/params.py",
                    "markdown": "docs/parameters.md",
                },
            },
            {
                "name": "wing_span",
                "value": "5.0 * root_chord",
                "unit": "m",
                "type": "float",
                "description": "Full wing span.",
                "rationale": "Aspect ratio target of 5.",
                "tags": ["aerodynamics", "geometry"],
                "bindings": {},
            },
        ],
    }
    destination.write_text(yaml.safe_dump(template, sort_keys=False), encoding="utf-8")
    console.print(Panel.fit(f"Created [bold green]{destination}[/bold green]", title="Paramfabric"))


@app.command()
def add(
    name: str = typer.Argument(..., help="Unique parameter name."),
    value: str = typer.Option(..., "--value", help="Number, boolean, or expression."),
    unit: str = typer.Option(..., "--unit", help="Pint-compatible physical unit."),
    rationale: str = typer.Option("", "--rationale", help="Why this value was selected."),
    description: str = typer.Option("", "--description", help="Parameter description."),
    parameter_type: str = typer.Option("float", "--type", help="float, int, str, or bool."),
) -> None:
    """Add a parameter to the current fabric"""
    path = fabric_path()
    if not path.exists():
        raise typer.BadParameter("no fabric found; run `paramfabric init` first")
    fabric = load_fabric(path)
    if name in fabric.by_name():
        raise typer.BadParameter(f"parameter {name!r} already exists")
    parameter=Parameter(
        name=name,
        value=parse_value(value),
        unit=unit,
        rationale=rationale,
        description=description,
        type=parameter_type,  # validated by Pydantic
    )
    fabric.parameters.append(parameter)
    path.write_text(yaml.safe_dump(fabric.model_dump(mode="json"), sort_keys=False), encoding="utf-8")
    console.print(f"[green]Added[/green] [bold]{name}[/bold] = {value} {unit}")


@app.command()
def check() -> None:
    """Evaluate formulas, check units, and detect dependency cycles"""
    try:
        graph = ParameterGraph(load_current())
        values = graph.evaluate()
    except Exception as exc:
        console.print(Panel(str(exc), title="[red]Check failed[/red]", border_style="red"))
        raise typer.Exit(code=1) from exc
    table = Table(title="Parameter check")
    table.add_column("Parameter")
    table.add_column("Resolved value", justify="right")
    table.add_column("Unit")
    table.add_column("Dependencies")
    for parameter in graph.fabric.parameters:
        value = values[parameter.name]
        shown = str(parameter.value) if parameter.type == "str" else (
            str(bool(parameter.value)) if parameter.type == "bool" else f"{float(value.magnitude):.12g}"
        )
        table.add_row(parameter.name, shown, parameter.unit, ", ".join(graph.dependencies(parameter.name)) or "-")
    console.print(table)
    console.print("[bold green]PASS: Fabric is consistent[/bold green]")


@app.command()
def build() -> None:
    """Generate all output files declared in parameter bindings"""
    graph = ParameterGraph(load_current())
    graph.evaluate()
    outputs = {
        "cpp_header": generate_cpp(graph),
        "py_module": generate_python(graph),
        "rust": generate_rust(graph),
        "latex": generate_latex(graph),
        "markdown": generate_markdown(graph),
        "solidworks": generate_solidworks_macro(graph),
        "nx": generate_nx_journal(graph),
        "ansys": export_solver(graph, "ansys"),
        "openfoam": export_solver(graph, "openfoam"),
        "comsol": export_solver(graph, "comsol"),
        "openscad": generate_openscad(graph),
    }
    bindings: dict[tuple[str, str], None] = {}
    for parameter in graph.fabric.parameters:
        for kind, path in parameter.bindings.items():
            bindings[(kind, path)] = None
    if not bindings:
        console.print("[yellow]No bindings configured; nothing to build.[/yellow]")
        return
    for (kind, relative_path) in sorted(bindings):
        destination = Path(relative_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(outputs[kind], encoding="utf-8")
        console.print(f"[green]OK[/green] {kind}: {destination}")
    console.print(Panel.fit(f"Generated {len(bindings)} file(s)", title="Build complete"))


@app.command()
def audit(
    path: Path = typer.Option(Path("."), "--path", help="Project directory to scan."),
) -> None:
    """Scan Python and C/C++ sources for parameter drift"""
    if not path.is_dir():
        raise typer.BadParameter(f"not a directory: {path}")
    graph = ParameterGraph(load_current())
    graph.evaluate()
    findings=audit_sources(path, graph)
    table = Table(title="Parameter audit")
    table.add_column("Status")
    table.add_column("Parameter")
    table.add_column("File")
    table.add_column("Finding")
    styles={
        "drift": "red",
        "unit-mismatch": "red",
        "undeclared": "red",
        "missing-unit": "yellow",
        "missing": "yellow",
    }
    for finding in findings:
        table.add_row(
            f"[{styles[finding.status]}]{finding.status}[/{styles[finding.status]}]",
            finding.parameter,
            str(finding.file) if finding.file else "-",
            finding.message,
        )
    if findings:
        console.print(table)
        console.print(f"[red]Audit found {len(findings)} issue(s).[/red]")
        raise typer.Exit(code=1)
    console.print(Panel.fit("No parameter drift found.", title="[green]Audit passed[/green]"))


@app.command()
def diff(
    ref1: str = typer.Argument(..., help="Base Git revision."),
    ref2: str = typer.Argument("HEAD", help="Comparison revision (default: HEAD)."),
) -> None:
    """Compare fabric values and units across Git revisions"""
    path = fabric_path().resolve()
    cwd = Path.cwd()
    if ref2 == "HEAD" and "..." in ref1:
        ref1, ref2 = ref1.split("...", maxsplit=1)
    elif ref2 == "HEAD" and ".." in ref1:
        ref1, ref2 = ref1.split("..", maxsplit=1)
    try:
        before = fabric_at_revision(ref1, path, cwd)
        after = fabric_at_revision(ref2, path, cwd)
        changes = parameter_diff(before, after)
    except (RuntimeError, ValueError, OSError) as exc:
        console.print(Panel(str(exc), title="[red]Diff failed[/red]", border_style="red"))
        raise typer.Exit(code=1) from exc
    table = Table(title=f"Parameter changes: {ref1} to {ref2}")
    table.add_column("Parameter")
    table.add_column("Change")
    table.add_column("Before (raw -> resolved)")
    table.add_column("After (raw -> resolved)")
    table.add_column("Resolved delta")
    for change in changes:
        old=change.get("before", {})
        new = change.get("after", {})
        old_text = "-" if not old else f"{old.get('value')} -> {change.get('before_resolved', old.get('value'))} {old.get('unit')}"
        new_text = "-" if not new else f"{new.get('value')} -> {change.get('after_resolved', new.get('value'))} {new.get('unit')}"
        delta = change.get("resolved_delta")
        delta_text = "-" if delta is None else f"{delta:.12g} {old.get('unit', '')}"
        table.add_row(str(change["name"]), str(change["change"]), old_text, new_text, delta_text)
    if changes:
        console.print(table)
        history_table=Table(title="Parameter mutation provenance")
        history_table.add_column("Parameter")
        history_table.add_column("Commit")
        history_table.add_column("Author")
        history_table.add_column("Timestamp")
        try:
            for change in changes:
                for entry in parameter_history(str(change["name"]), path, cwd, ref2):
                    history_table.add_row(
                        str(change["name"]),
                        entry["commit"][:12],
                        entry["author"],
                        entry["timestamp"],
                    )
        except RuntimeError as exc:
            console.print(Panel(str(exc), title="[red]Provenance lookup failed[/red]", border_style="red"))
            raise typer.Exit(code=1) from exc
        if history_table.row_count:
            console.print(history_table)
    else:
        console.print("[green]No parameter changes.[/green]")


@app.command()
def tree() -> None:
    """Display parameter dependencies as a Rich tree"""
    graph = ParameterGraph(load_current())
    graph.evaluate()
    tree_root = Tree("[bold]Parameter fabric[/bold]")
    roots = [name for name in graph.graph.nodes if graph.graph.in_degree(name) == 0]

    def add_branch(parent: Tree, name: str, ancestors: frozenset[str]) -> None:
        parameter = graph.parameters[name]
        value = graph.value(name).magnitude if parameter.type not in {"str", "bool"} else parameter.value
        branch = parent.add(f"[cyan]{name}[/cyan] = {value} [dim]{parameter.unit}[/dim]")
        if name not in ancestors:
            for child in sorted(graph.graph.successors(name)):
                add_branch(branch, child, ancestors | {name})

    for root in roots:
        add_branch(tree_root, root, frozenset())
    console.print(tree_root)


@app.command()
def serve(port: int = typer.Option(8000, "--port", min=1, max=65535)) -> None:
    """Serve a local interactive parameter lineage graph"""
    import uvicorn

    from paramfabric.viz.server import create_app

    uvicorn.run(create_app(fabric_path()), host="127.0.0.1", port=port)


@app.command("ci-check")
def ci_check(
    base: str = typer.Option(..., "--base", help="Base revision (e.g. origin/main)."),
    head: str = typer.Option("WORKTREE", "--head", help="Head revision, or WORKTREE for the checked-out files."),
    fabric: Path = typer.Option(Path("params.yaml"), "--fabric", help="Fabric file path inside the repository."),
    approvals: str = typer.Option("required", "--approvals", help="required | warn | off"),
    drift: str = typer.Option("error", "--drift", help="Source-code drift: error | warn | off"),
    lock: Path = typer.Option(None, "--lock", help="Approval lock file (default paramfabric.lock)."),
    public_key: str = typer.Option(None, "--public-key", envvar="PARAMFABRIC_APPROVAL_PUBLIC_KEY",
                                   help="Organization public key(s) that sign approvals (PEM file or bundle)."),
    summary: Path = typer.Option(None, "--summary", help="Write the Markdown PR summary to this file."),
    json_output: Path = typer.Option(None, "--json", help="Write the machine-readable result to this file."),
) -> None:
    """Pull-request quality gate: units, cycles, approvals, source drift, and blast radius (exit 1 on blocking errors)"""
    import json
    import os

    from paramfabric.ci import run_ci_check

    for option, value, allowed in (("--approvals", approvals, {"required", "warn", "off"}), ("--drift", drift, {"error", "warn", "off"})):
        if value not in allowed:
            raise typer.BadParameter(f"{option} must be one of {', '.join(sorted(allowed))}")
    report = run_ci_check(Path.cwd(), fabric, base, head, approvals=approvals, drift=drift, lock_path=lock, public_keys=public_key)
    markdown = report.markdown()
    if summary:
        summary.write_text(markdown, encoding="utf-8")
    if json_output:
        json_output.write_text(json.dumps(report.to_dict(), indent=2, default=str), encoding="utf-8")
    step_summary = os.getenv("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a", encoding="utf-8") as handle:
            handle.write(markdown)
    typer.echo(markdown)
    raise typer.Exit(0 if report.passed else 1)


@app.command("approval-lock")
def approval_lock(
    request_id: str = typer.Argument(..., help="Merged change request id."),
    server: str = typer.Option(..., "--server", envvar="PARAMFABRIC_SERVER_URL"),
    token: str = typer.Option(..., "--token", envvar="PARAMFABRIC_TOKEN"),
    project: str = typer.Option("default", "--project", envvar="PARAMFABRIC_PROJECT"),
    output: Path = typer.Option(Path("paramfabric.lock"), "--output"),
    public_key: str = typer.Option(None, "--public-key", envvar="PARAMFABRIC_APPROVAL_PUBLIC_KEY", help="Verify before writing (PEM file)."),
) -> None:
    """Fetch the Ed25519-signed approval of a merged change request into paramfabric.lock (commit it with params.yaml)"""
    import json
    import urllib.request

    from paramfabric.governance.approvals import verify_approval, write_lock
    from paramfabric.security.signing import load_public_keys

    request = urllib.request.Request(
        f"{server.rstrip('/')}/api/change-requests/{quote(request_id)}/approval",
        headers={"Authorization": f"Bearer {token}", "X-Paramfabric-Project": project, "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310 - operator-provided server URL
        record = json.loads(response.read().decode("utf-8"))
    if public_key and not verify_approval(record, load_public_keys(public_key)):
        console.print("[red]The approval signature does not verify with the given public key.[/red]")
        raise typer.Exit(1)
    write_lock(output, record)
    console.print(f"[green]Wrote {output}[/green] for fabric {record['fabric_hash'][:12]} "
                  f"(key {record.get('key_id')}, approved by {', '.join(record.get('approvers') or []) or 'n/a'}).")


def _audit_key(root: Path) -> bytes | None:
    import json
    import os

    value = os.getenv("PARAMFABRIC_AUDIT_KEY")
    if not value:
        secrets = root / ".paramfabric" / "local-secrets.json"
        value = json.loads(secrets.read_text(encoding="utf-8")).get("audit") if secrets.is_file() else None
    return value.encode("utf-8") if value else None


@app.command("report")
def report(
    output: Path = typer.Option(Path("audit_report.html"), "--output", "-o", help="Destination (.html or .pdf)."),
    fabric: Path = typer.Option(None, "--fabric", help="Fabric file (default: PARAMFABRIC_CONFIG_PATH or params.yaml)."),
    title: str = typer.Option("Engineering Parameter Compliance Report", "--title"),
    prepared_by: str = typer.Option(None, "--prepared-by", help="Name recorded in the report and seal."),
    database: str = typer.Option(None, "--database", help="Include governance history from this database URL."),
    organization: str = typer.Option("default", "--organization"),
    project: str = typer.Option("default", "--project"),
) -> None:
    """Export an Ed25519-signed ISO 9001 / AS9100 compliance report (HTML or PDF)"""
    import getpass

    from paramfabric.governance.tenancy import Tenant
    from paramfabric.reporting.pdf_exporter import ReportError, collect_report_data, export_report, governance_summary, report_signer
    from paramfabric.security.audit import AuditLedger

    root=Path.cwd()
    try:
        signing_key = report_signer(root, organization)
    except ReportError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    ledger_path = root / ".paramfabric" / "audit-ledger.jsonl"
    audit_key = _audit_key(root)
    ledger = AuditLedger(ledger_path, audit_key) if ledger_path.is_file() and audit_key else None
    governance = None
    if database:
        from paramfabric.governance.store import create_database

        engine, factory = create_database(database, create_schema=False)
        try:
            with factory() as session:
                governance = governance_summary(session, Tenant(organization, project))
        finally:
            engine.dispose()
    signer = prepared_by or getpass.getuser()
    data = collect_report_data(root, load_fabric(fabric or fabric_path()), title=title, organization=organization,
                               project=project, governance=governance, ledger=ledger, prepared_by=signer)
    seal = export_report(output, data, signing_key, signer=signer)
    console.print(f"[green]Wrote {output}[/green] · {len(data['parameters'])} parameters · signed by key {seal['key_id']} "
                  f"(public key: {signing_key.source.replace('.pem', '.pub.pem') if signing_key.source.endswith('.pem') else 'paramfabric keys public'})")


@app.command("report-verify")
def report_verify(
    path: Path = typer.Argument(..., exists=True, dir_okay=False),
    public_key: str = typer.Option(None, "--public-key", help="Trusted public key(s): PEM file or bundle."),
    keys_url: str = typer.Option(None, "--keys-url", help="Fetch the organization's public keys, e.g. "
                                 "https://host/api/public/organizations/acme/signing-keys?format=pem"),
    legacy_key: str = typer.Option(None, "--legacy-key", envvar="PARAMFABRIC_REPORT_KEY", help="HMAC secret for v0.5 reports."),
) -> None:
    """Verify a report's Ed25519 seal with the organization's PUBLIC key (no secrets, no server account)"""
    from paramfabric.reporting.pdf_exporter import ReportError, read_report, verify_report
    from paramfabric.security.signing import SigningError, load_public_keys, resolve_signer

    keys = []
    try:
        if public_key:
            keys += load_public_keys(public_key)
        if keys_url:
            import urllib.request

            with urllib.request.urlopen(keys_url, timeout=30) as response:  # noqa: S310 - auditor-provided URL
                keys += load_public_keys(response.read())
        if not keys and not legacy_key:
            try:  # on the signing workstation itself, the local key's public half is trusted
                keys.append(resolve_signer(Path.cwd(), read_report(path)[0].get("organization", "default"), create_local=False).public_key)
            except SigningError:
                console.print("[red]Give the organization's public key: --public-key org.pub.pem (or --keys-url).[/red]")
                raise typer.Exit(2) from None
        result = verify_report(path, keys, legacy_key=legacy_key.encode("utf-8") if legacy_key else None)
    except (ReportError, SigningError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    if result["valid"]:
        console.print(f"[green]Signature valid[/green] · {result['algorithm']} key {result['verified_key_id'] or 'legacy'} · "
                      f"fabric {result['fabric_hash'][:12]} · generated {result['generated_at']} · signed by {result['seal']['signer']}")
        return
    reason = ("content was modified after signing" if not result["payload_intact"]
              else "the document pages or metadata were edited after signing" if result.get("document_intact") is False
              else "not signed by any of the trusted keys")
    console.print(f"[red]Signature INVALID[/red]: {reason}.")
    raise typer.Exit(1)


keys_app = typer.Typer(help="Ed25519 signing keys for reports and approval locks.")
app.add_typer(keys_app, name="keys")


@keys_app.command("generate")
def keys_generate(
    output: Path = typer.Option(Path(".paramfabric/signing-key.pem"), "--output", "-o", help="Private key path (public key: *.pub.pem)."),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing key."),
) -> None:
    """Create an Ed25519 key pair (passphrase from PARAMFABRIC_SIGNING_KEY_PASSPHRASE)"""
    import os

    from paramfabric.security.signing import Signer

    if output.exists() and not force:
        console.print(f"[red]{output} exists; use --force to replace it (old reports then need the old public key).[/red]")
        raise typer.Exit(1)
    signer = Signer.generate()
    signer.save(output, os.getenv("PARAMFABRIC_SIGNING_KEY_PASSPHRASE") or None)
    console.print(f"[green]Private key[/green] {output} (keep it secret)\n[green]Public key[/green]  {output.with_suffix('.pub.pem')} "
                  f"(share with auditors and CI) · key id {signer.key_id}")


@keys_app.command("public")
def keys_public(organization: str = typer.Option("default", "--organization")) -> None:
    """Print the public key of the signing key this workspace uses"""
    from paramfabric.security.signing import SigningError, resolve_signer

    try:
        signer = resolve_signer(Path.cwd(), organization, create_local=False)
    except SigningError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from exc
    typer.echo(signer.public_pem, nl=False)


@app.command("worker")
def worker(
    name: str = typer.Option("", "--name", help="Worker name shown on tasks (default: host name)."),
    once: bool = typer.Option(False, "--once", help="Run at most one task, then exit."),
) -> None:
    """Drain the Redis task queue (meshing and solver runs) on a compute node.

    Requires PARAMFABRIC_REDIS_URL and a PARAMFABRIC_ARTIFACT_DIR / PARAMFABRIC_DATA_DIR
    shared with the API servers.
    """
    import os
    import socket

    import paramfabric.solvers.manager  # noqa: F401 - registers task handlers
    from paramfabric.server.tasks import RedisTaskQueue, queue_from_environment

    if not os.getenv("PARAMFABRIC_REDIS_URL"):
        raise typer.BadParameter("set PARAMFABRIC_REDIS_URL to the queue's Redis instance")
    data_dir = Path(os.getenv("PARAMFABRIC_DATA_DIR", "var/workbench")).resolve()
    queue = queue_from_environment(data_dir / ".paramfabric" / "tasks")
    assert isinstance(queue, RedisTaskQueue)
    label = name or socket.gethostname()
    console.print(f"[green]Worker {label} waiting for tasks…[/green]")
    executed = queue.work(label, once=once)
    console.print(f"Executed {executed} task(s).")


@app.command("server")
def enterprise_server(
    host: str = typer.Option("127.0.0.1", "--host"),
    port: int = typer.Option(8000, "--port", min=1, max=65535),
    fabric: Path = typer.Option(Path("params.yaml"), "--fabric"),
    enterprise_auth: bool = typer.Option(
        False,
        "--enterprise-auth",
        help="Require signed bearer-token authentication.",
    ),
) -> None:
    """Run the local Studio or explicitly authenticated enterprise API"""
    import os
    import uvicorn

    from paramfabric.server.app import create_app

    auth_required = enterprise_auth or os.getenv("PARAMFABRIC_AUTH_REQUIRED", "false").lower() == "true"
    if not auth_required and host not in {"127.0.0.1", "localhost", "::1"}:
        raise typer.BadParameter("local mode can bind only to loopback; use --enterprise-auth for network access")
    uvicorn.run(create_app(fabric, auth_required=auth_required), host=host, port=port)


@app.command("db-upgrade")
def database_upgrade() -> None:
    """Apply versioned database migrations before production startup"""
    import os
    from alembic import command
    from alembic.config import Config

    config = Config()
    migrations_path = Path(__file__).parent / "migrations"
    config.set_main_option("script_location", str(migrations_path).replace("%", "%%"))
    config.set_main_option(
        "sqlalchemy.url",
        os.getenv("PARAMFABRIC_DATABASE_URL", "sqlite:///paramfabric-enterprise.db").replace("%", "%%"),
    )
    command.upgrade(config, "head")
    console.print("[green]Database schema is up to date.[/green]")


@app.command("token")
def token(
    subject: str = typer.Option(..., "--subject", help="Authenticated user identifier."),
    role: list[str] = typer.Option(..., "--role", help="Role claim; may be repeated."),
    lifetime_minutes: int = typer.Option(30, "--lifetime-minutes", min=1, max=1440),
    organization: str = typer.Option("default", "--organization", help="Organization (tenant) claim."),
) -> None:
    """Mint an API token using a configured trusted signing secret"""
    import os
    from datetime import timedelta

    secret = os.getenv("PARAMFABRIC_JWT_SECRET")
    if not secret:
        raise typer.BadParameter("set PARAMFABRIC_JWT_SECRET; use a trusted identity provider in production")
    access_token = issue_access_token(
        subject,
        role,
        secret,
        lifetime=timedelta(minutes=lifetime_minutes),
        organization=organization,
    )
    console.print(access_token, markup=False)


def _sync_request(
    url: str,
    token_value: str,
    method: str,
    body: dict[str, object] | None = None,
) -> Any:
    import json
    from urllib.error import HTTPError, URLError
    from urllib.request import Request, urlopen

    payload = None if body is None else json.dumps(body).encode("utf-8")
    request=Request(
        url,
        data=payload,
        method=method,
        headers={
            "Authorization": f"Bearer {token_value}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"server returned HTTP {exc.code}: {details}") from exc
    except URLError as exc:
        raise RuntimeError(f"could not reach Paramfabric server: {exc.reason}") from exc


@sync_app.command("push")
def sync_push(
    url: str = typer.Option(..., "--url", help="Enterprise API base URL."),
    rationale: str = typer.Option(..., "--rationale", help="Change request justification."),
    token_value: str = typer.Option("", "--token", envvar="PARAMFABRIC_TOKEN"),
    branch: str = typer.Option("main", "--branch"),
) -> None:
    """Submit the local fabric as a reviewed change request"""
    if not token_value:
        raise typer.BadParameter("provide --token or set PARAMFABRIC_TOKEN")
    fabric = load_current()
    try:
        result = _sync_request(
            f"{url.rstrip('/')}/api/change-requests",
            token_value,
            "POST",
            {
                "proposed_fabric": fabric.model_dump(mode="json"),
                "rationale": rationale,
                "target_branch": branch,
            },
        )
    except RuntimeError as exc:
        console.print(Panel(str(exc), title="[red]Sync failed[/red]", border_style="red"))
        raise typer.Exit(code=1) from exc
    console.print(Panel.fit(f"Created change request [bold]{result['id']}[/bold]", title="Sync submitted"))


@sync_app.command("pull")
def sync_pull(
    url: str = typer.Option(..., "--url", help="Enterprise API base URL."),
    token_value: str = typer.Option("", "--token", envvar="PARAMFABRIC_TOKEN"),
    branch: str = typer.Option("main", "--branch"),
) -> None:
    """Pull the approved server fabric to the local source file"""
    if not token_value:
        raise typer.BadParameter("provide --token or set PARAMFABRIC_TOKEN")
    try:
        result = _sync_request(
            f"{url.rstrip('/')}/api/parameters?{urlencode({'branch': branch})}",
            token_value,
            "GET",
        )
        fabric = Fabric.model_validate({"version": 1, "parameters": result["parameters"]})
    except (RuntimeError, KeyError, ValueError) as exc:
        console.print(Panel(str(exc), title="[red]Sync failed[/red]", border_style="red"))
        raise typer.Exit(code=1) from exc
    destination=fabric_path()
    destination.write_text(yaml.safe_dump(fabric.model_dump(mode="json"), sort_keys=False), encoding="utf-8")
    console.print(f"[green]Pulled {len(fabric.parameters)} parameter(s) into {destination}[/green]")


@sync_app.command("requests")
def sync_requests(
    url: str = typer.Option(..., "--url", help="Enterprise API base URL."),
    token_value: str = typer.Option("", "--token", envvar="PARAMFABRIC_TOKEN"),
) -> None:
    """List change requests awaiting review or merge"""
    if not token_value:
        raise typer.BadParameter("provide --token or set PARAMFABRIC_TOKEN")
    try:
        items=_sync_request(f"{url.rstrip('/')}/api/change-requests", token_value, "GET")
    except RuntimeError as exc:
        console.print(Panel(str(exc), title="[red]Sync failed[/red]", border_style="red"))
        raise typer.Exit(code=1) from exc
    table = Table(title="Enterprise change requests")
    table.add_column("ID")
    table.add_column("Status")
    table.add_column("Author")
    table.add_column("Rationale")
    for item in items:
        table.add_row(
            str(item["id"]),
            str(item["status"]),
            str(item["author"]),
            str(item["rationale"]),
        )
    console.print(table)


@sync_app.command("review")
def sync_review(
    request_id: str = typer.Argument(..., help="Change request identifier."),
    decision: str = typer.Option(..., "--decision", help="approve or reject."),
    url: str = typer.Option(..., "--url", help="Enterprise API base URL."),
    comment: str = typer.Option("", "--comment"),
    token_value: str = typer.Option("", "--token", envvar="PARAMFABRIC_TOKEN"),
) -> None:
    """Approve or reject a pending parameter change request"""
    if decision not in {"approve", "reject"}:
        raise typer.BadParameter("decision must be approve or reject")
    if not token_value:
        raise typer.BadParameter("provide --token or set PARAMFABRIC_TOKEN")
    try:
        result = _sync_request(
            f"{url.rstrip('/')}/api/change-requests/{request_id}/review",
            token_value,
            "POST",
            {"decision": decision, "comment": comment},
        )
    except RuntimeError as exc:
        console.print(Panel(str(exc), title="[red]Review failed[/red]", border_style="red"))
        raise typer.Exit(code=1) from exc
    console.print(f"Request [bold]{request_id}[/bold] is now [green]{result['status']}[/green]")


@sync_app.command("merge")
def sync_merge(
    request_id: str = typer.Argument(..., help="Approved change request identifier."),
    url: str = typer.Option(..., "--url", help="Enterprise API base URL."),
    token_value: str = typer.Option("", "--token", envvar="PARAMFABRIC_TOKEN"),
) -> None:
    """Merge an approved request into its target branch"""
    if not token_value:
        raise typer.BadParameter("provide --token or set PARAMFABRIC_TOKEN")
    try:
        result = _sync_request(
            f"{url.rstrip('/')}/api/change-requests/{request_id}/merge",
            token_value,
            "POST",
        )
    except RuntimeError as exc:
        console.print(Panel(str(exc), title="[red]Merge failed[/red]", border_style="red"))
        raise typer.Exit(code=1) from exc
    console.print(f"[green]Merged request {request_id} to {result['branch']}[/green]")


@baseline_app.command("create")
def baseline_create(
    tag: str = typer.Argument(..., help="Immutable baseline tag."),
    url: str = typer.Option(..., "--url", help="Enterprise API base URL."),
    branch: str = typer.Option("main", "--branch"),
    token_value: str = typer.Option("", "--token", envvar="PARAMFABRIC_TOKEN"),
) -> None:
    """Freeze the current server branch under a new baseline tag"""
    if not token_value:
        raise typer.BadParameter("provide --token or set PARAMFABRIC_TOKEN")
    query = urlencode({"tag": tag, "branch": branch})
    try:
        result = _sync_request(f"{url.rstrip('/')}/api/baselines?{query}", token_value, "POST")
    except RuntimeError as exc:
        console.print(Panel(str(exc), title="[red]Baseline failed[/red]", border_style="red"))
        raise typer.Exit(code=1) from exc
    console.print(f"[green]Created immutable baseline {result['tag']} ({result['hash']})[/green]")


@baseline_app.command("list")
def baseline_list(
    url: str = typer.Option(..., "--url", help="Enterprise API base URL."),
    token_value: str = typer.Option("", "--token", envvar="PARAMFABRIC_TOKEN"),
) -> None:
    """List immutable server baselines"""
    if not token_value:
        raise typer.BadParameter("provide --token or set PARAMFABRIC_TOKEN")
    try:
        items = _sync_request(f"{url.rstrip('/')}/api/baselines", token_value, "GET")
    except RuntimeError as exc:
        console.print(Panel(str(exc), title="[red]Baseline lookup failed[/red]", border_style="red"))
        raise typer.Exit(code=1) from exc
    table=Table(title="Immutable parameter baselines")
    table.add_column("Tag")
    table.add_column("Hash")
    table.add_column("Created by")
    table.add_column("Created at")
    for item in items:
        table.add_row(str(item["tag"]), str(item["hash"]), str(item["created_by"]), str(item["created_at"]))
    console.print(table)


@baseline_app.command("diff")
def baseline_compare(
    first: str = typer.Argument(...),
    second: str = typer.Argument(...),
    url: str = typer.Option(..., "--url", help="Enterprise API base URL."),
    token_value: str = typer.Option("", "--token", envvar="PARAMFABRIC_TOKEN"),
) -> None:
    """Compare two immutable baseline tags"""
    if not token_value:
        raise typer.BadParameter("provide --token or set PARAMFABRIC_TOKEN")
    try:
        changes = _sync_request(
            f"{url.rstrip('/')}/api/baselines/{quote(first, safe='')}/diff/{quote(second, safe='')}",
            token_value,
            "GET",
        )
    except RuntimeError as exc:
        console.print(Panel(str(exc), title="[red]Baseline diff failed[/red]", border_style="red"))
        raise typer.Exit(code=1) from exc
    table = Table(title=f"Baseline changes: {first} to {second}")
    table.add_column("Parameter")
    table.add_column("Change")
    table.add_column("Before")
    table.add_column("After")
    for change in changes:
        before, after = change.get("before", {}), change.get("after", {})
        table.add_row(
            str(change["name"]),
            str(change["change"]),
            "-" if not before else f"{before.get('value')} {before.get('unit')}",
            "-" if not after else f"{after.get('value')} {after.get('unit')}",
        )
    console.print(table)


if __name__ == "__main__":
    app()
