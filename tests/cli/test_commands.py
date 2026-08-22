import asyncio
import json
import socket
import subprocess
import sys
from pathlib import Path
from textwrap import dedent
from types import SimpleNamespace

import pytest
import typer
from typer.testing import CliRunner

import pyxle.cli as cli
from pyxle import __version__
from pyxle.cli import app, version_callback
from pyxle.cli.assets import default_favicon_bytes
from pyxle.config import PyxleConfig

runner = CliRunner()


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_init_scaffolds_project_structure() -> None:
    with runner.isolated_filesystem():
        result = runner.invoke(app, ["init", "My App"], catch_exceptions=False)
        assert result.exit_code == 0, result.stdout

        project_dir = Path("my-app")
        assert project_dir.is_dir()
        assert (project_dir / "pages" / "layout.pyxl").exists()
        assert (project_dir / "pages" / "index.pyxl").exists()
        assert (project_dir / "pages" / "api" / "pulse.py").exists()
        # Default scaffold: plain CSS + CSS Modules, no Tailwind.
        assert (project_dir / "pages" / "styles" / "app.css").exists()
        assert (project_dir / "pages" / "components" / "Badge.jsx").exists()
        assert (project_dir / "pages" / "components" / "Badge.module.css").exists()
        assert (project_dir / "jsconfig.json").exists()
        assert (project_dir / "vite.config.js").exists()
        # Legacy Tailwind-v3 side-channel is gone from the default scaffold.
        assert not (project_dir / "tailwind.config.cjs").exists()
        assert not (project_dir / "postcss.config.cjs").exists()
        assert not (project_dir / "public" / "styles" / "tailwind.css").exists()
        # No Tailwind by default: no Tailwind CSS entry, no shadcn files.
        assert not (project_dir / "components.json").exists()
        assert not (project_dir / "lib" / "utils.js").exists()
        branding_dir = project_dir / "public" / "branding"
        assert (branding_dir / "pyxle-mark.svg").exists()
        assert (project_dir / "public" / "favicon.ico").read_bytes() == default_favicon_bytes()

        package_json = read_json(project_dir / "package.json")
        assert package_json["name"] == "my-app"

        config_payload = json.loads((project_dir / "pyxle.config.json").read_text(encoding="utf-8"))
        assert config_payload["middleware"] == []

        next_steps = result.stdout.splitlines()
        assert any("Next steps" in line for line in next_steps)
        assert "pyxle install" in result.stdout


def test_init_requires_force_for_existing_directory() -> None:
    with runner.isolated_filesystem():
        project_dir = Path("demo")
        project_dir.mkdir()
        result = runner.invoke(app, ["init", "demo"], catch_exceptions=False)
        assert result.exit_code == 1
        assert "Target directory already exists" in result.stdout

        (project_dir / "old.txt").write_text("legacy")
        result_force = runner.invoke(app, ["init", "demo", "--force"], catch_exceptions=False)
        assert result_force.exit_code == 0, result_force.stdout
        assert not (project_dir / "old.txt").exists()


def test_init_rejects_unknown_template() -> None:
    with runner.isolated_filesystem():
        result = runner.invoke(app, ["init", "demo", "--template", "fancy"], catch_exceptions=False)
        assert result.exit_code == 1
        assert "Unsupported template" in result.stdout


def test_init_rejects_invalid_name() -> None:
    with runner.isolated_filesystem():
        result = runner.invoke(app, ["init", "!!!"], catch_exceptions=False)
        assert result.exit_code == 1
        assert "Project name" in result.stdout


def test_install_invokes_dependency_helper(monkeypatch) -> None:
    with runner.isolated_filesystem():
        Path("demo").mkdir()

        called: dict[str, object] = {}

        def fake_install(
            project_root,
            *,
            logger,
            install_python=True,
            install_node=True,
            break_system_packages=False,
        ):
            called["root"] = project_root.resolve()
            called["python"] = install_python
            called["node"] = install_node

        monkeypatch.setattr(cli, "_install_dependencies", fake_install)

        result = runner.invoke(
            app,
            ["install", "demo", "--no-node"],
            catch_exceptions=False,
        )
        assert result.exit_code == 0
        assert called["root"] == Path("demo").resolve()
        assert called["python"] is True
        assert called["node"] is False


def test_install_fails_when_directory_missing() -> None:
    with runner.isolated_filesystem():
        result = runner.invoke(app, ["install", "missing"], catch_exceptions=False)
        assert result.exit_code == 1
        assert "does not exist" in result.stdout


def test_install_rejects_file_path() -> None:
    with runner.isolated_filesystem():
        file_path = Path("demo.txt")
        file_path.write_text("demo", encoding="utf-8")

        result = runner.invoke(app, ["install", "demo.txt"], catch_exceptions=False)
        assert result.exit_code == 1
        assert "not a directory" in result.stdout


def test_install_dependencies_executes_commands(monkeypatch, tmp_path) -> None:
    # Python deps are only installed when there is something to install;
    # these tests assert on the pip command, so give them a reason to run one.
    (tmp_path / "requirements.txt").write_text("pyxle-framework\n", encoding="utf-8")

    calls: list[tuple[list[str], Path]] = []

    def fake_run(command, *, cwd, check, stdout=None, stderr=None, text=False):
        calls.append((command, cwd))

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    logger = cli.ConsoleLogger()

    cli._install_dependencies(tmp_path, logger=logger)

    assert calls[0][0][0] == sys.executable
    assert calls[0][0][-2:] == ["-r", "requirements.txt"]
    assert calls[1][0] == ["npm", "install"]
    assert calls[0][1] == tmp_path
    assert calls[1][1] == tmp_path


def test_scaffold_requirements_pins_running_framework_version(tmp_path, monkeypatch) -> None:
    """`pyxle init` must render the pyxle-framework requirement from the RUNNING
    framework version — `>=<current>,<<next-minor>` — so `pyxle install` can never
    silently downgrade the framework a scaffold was generated with.

    The expectation is derived from the live ``pyxle.__version__`` so this test
    can never go stale on a version bump.
    """
    import re

    from pyxle.cli.init import run_init

    monkeypatch.chdir(tmp_path)
    run_init("demo", force=False, template="default", logger=cli.ConsoleLogger(), log_steps=False)

    content = (tmp_path / "demo" / "requirements.txt").read_text(encoding="utf-8")
    line = next(
        ln for ln in content.splitlines() if ln.startswith("pyxle-framework")
    )

    match = re.match(r"^(\d+)\.(\d+)", __version__)
    assert match is not None, (
        "pyxle-framework distribution metadata is unavailable in this environment"
    )
    major, minor = int(match.group(1)), int(match.group(2))
    assert line == f"pyxle-framework>={__version__},<{major}.{minor + 1}"
    # No leftover template placeholder anywhere in the rendered file.
    assert "$" not in content


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("0.7.0", "pyxle-framework>=0.7.0,<0.8"),
        ("0.6.1", "pyxle-framework>=0.6.1,<0.7"),
        ("1.9.3", "pyxle-framework>=1.9.3,<1.10"),
        ("0.7.0rc1", "pyxle-framework>=0.7.0rc1,<0.8"),
        ("2.0.0.dev4", "pyxle-framework>=2.0.0.dev4,<2.1"),
    ],
)
def test_framework_requirement_spans_current_to_next_minor(version, expected) -> None:
    from pyxle.cli.init import framework_requirement

    assert framework_requirement(version) == expected


def test_framework_requirement_unparseable_version_left_unpinned() -> None:
    """An uninstalled source checkout reports version "unknown" — the scaffold
    must emit a satisfiable (unpinned) requirement, not a broken specifier."""
    from pyxle.cli.init import framework_requirement

    assert framework_requirement("unknown") == "pyxle-framework"


def test_scaffold_package_json_modern_stack(tmp_path, monkeypatch) -> None:
    """The scaffold must pin the modern, audit-clean stack: React 19, Vite 7,
    and the Vite-7-compatible @vitejs/plugin-react 5. Node 18 is EOL, so the
    engines floor must be >= 20.19 (Vite 7's minimum)."""
    from pyxle.cli.init import run_init

    monkeypatch.chdir(tmp_path)
    run_init("demo", force=False, template="default", logger=cli.ConsoleLogger(), log_steps=False)

    manifest = read_json(tmp_path / "demo" / "package.json")
    deps = manifest["dependencies"]
    dev_deps = manifest["devDependencies"]

    assert int(deps["react"].lstrip("^~").split(".", 1)[0]) >= 19
    assert int(deps["react-dom"].lstrip("^~").split(".", 1)[0]) >= 19
    assert int(dev_deps["vite"].lstrip("^~").split(".", 1)[0]) >= 7
    assert int(dev_deps["@vitejs/plugin-react"].lstrip("^~").split(".", 1)[0]) >= 5

    engines = manifest["engines"]["node"]
    assert engines == ">=20.19"

    # The default scaffold declines Tailwind, so no Tailwind dependencies leak in.
    assert "tailwindcss" not in dev_deps
    assert "@tailwindcss/vite" not in dev_deps


def test_scaffold_tailwind_adds_vite_plugin_deps(tmp_path, monkeypatch) -> None:
    """Choosing Tailwind adds the v4 `@tailwindcss/vite` plugin + `tailwindcss`
    (and nothing PostCSS-related — the legacy side-channel is gone)."""
    from pyxle.cli.init import run_init

    monkeypatch.chdir(tmp_path)
    run_init(
        "demo",
        force=False,
        template="default",
        logger=cli.ConsoleLogger(),
        tailwind=True,
        log_steps=False,
    )

    manifest = read_json(tmp_path / "demo" / "package.json")
    dev_deps = manifest["devDependencies"]
    assert int(dev_deps["@tailwindcss/vite"].lstrip("^~").split(".", 1)[0]) >= 4
    assert int(dev_deps["tailwindcss"].lstrip("^~").split(".", 1)[0]) >= 4
    assert "autoprefixer" not in dev_deps
    assert "postcss" not in dev_deps


def test_scaffold_agents_md_documents_auto_injected_runtime_names(tmp_path, monkeypatch) -> None:
    """AGENTS.md must not claim `LoaderError` needs an import — the compiler
    auto-injects it (alongside ActionError/ValidationActionError/invalidate_routes),
    and the rest of the file says so."""
    from pyxle.cli.init import run_init

    monkeypatch.chdir(tmp_path)
    run_init("demo", force=False, template="default", logger=cli.ConsoleLogger(), log_steps=False)

    agents = (tmp_path / "demo" / "AGENTS.md").read_text(encoding="utf-8")
    assert "*not* injected" not in agents
    assert "import `LoaderError` (`from pyxle.runtime import LoaderError`) before" not in agents


def test_scaffold_includes_human_readme(tmp_path, monkeypatch) -> None:
    """A fresh project gets a human-facing README.md (not only AGENTS.md)."""
    from pyxle.cli.init import run_init

    monkeypatch.chdir(tmp_path)
    run_init("demo", force=False, template="default", logger=cli.ConsoleLogger(), log_steps=False)

    readme_path = tmp_path / "demo" / "README.md"
    assert readme_path.exists()
    readme = readme_path.read_text(encoding="utf-8")
    assert "# demo" in readme  # project name interpolated
    assert "pyxle dev" in readme
    assert "Node.js 20.19+" in readme


def test_in_virtualenv_detects_environments(monkeypatch) -> None:
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    monkeypatch.delenv("CONDA_PREFIX", raising=False)
    # Diverging prefixes → venv / virtualenv.
    monkeypatch.setattr(cli.sys, "prefix", "/proj/.venv")
    monkeypatch.setattr(cli.sys, "base_prefix", "/usr")
    assert cli._in_virtualenv() is True
    # Same prefix, no env markers → system Python.
    monkeypatch.setattr(cli.sys, "base_prefix", "/proj/.venv")
    assert cli._in_virtualenv() is False
    # VIRTUAL_ENV set → venv even when prefixes match.
    monkeypatch.setenv("VIRTUAL_ENV", "/proj/.venv")
    assert cli._in_virtualenv() is True


def test_install_dependencies_warns_outside_virtualenv(monkeypatch, tmp_path) -> None:
    """Outside a venv, install warns about PEP 668 with venv guidance instead of
    letting pip throw its raw 'externally-managed-environment' wall."""
    # Python deps are only installed when there is something to install;
    # these tests assert on the pip command, so give them a reason to run one.
    (tmp_path / "requirements.txt").write_text("pyxle-framework\n", encoding="utf-8")

    monkeypatch.setattr(cli, "_in_virtualenv", lambda: False)
    monkeypatch.setattr(cli.subprocess, "run", lambda *a, **k: None)
    warnings: list[str] = []

    class _Logger:
        verbosity = cli.Verbosity.NORMAL

        def step(self, *a, **k) -> None: ...
        def warning(self, msg) -> None:
            warnings.append(msg)
        def success(self, *a, **k) -> None: ...

    cli._install_dependencies(tmp_path, logger=_Logger(), install_node=False)
    assert any("virtual environment" in w.lower() for w in warnings)
    assert any("PEP 668" in w for w in warnings)


def test_init_generates_dev_secret_key(tmp_path, monkeypatch) -> None:
    """`pyxle init` writes a gitignored .env.local with a random PYXLE_SECRET_KEY
    so CSRF HMAC is enabled out of the box (no 'secret unset' warning)."""
    from pyxle.cli.init import run_init

    monkeypatch.chdir(tmp_path)
    run_init("demo", force=False, template="default", logger=cli.ConsoleLogger(), log_steps=False)

    env_local = tmp_path / "demo" / ".env.local"
    assert env_local.exists()
    content = env_local.read_text(encoding="utf-8")
    assert "PYXLE_SECRET_KEY=" in content
    # The key is a real random hex value, not a left-in placeholder.
    key = next(
        ln.split("=", 1)[1] for ln in content.splitlines() if ln.startswith("PYXLE_SECRET_KEY=")
    )
    assert len(key) >= 32 and all(c in "0123456789abcdef" for c in key)

    # And it's gitignored, so the secret is never committed.
    gitignore = (tmp_path / "demo" / ".gitignore").read_text(encoding="utf-8")
    assert ".env.local" in gitignore


def test_scaffold_gitignore_commits_env_ignores_only_local(tmp_path, monkeypatch) -> None:
    """`pyxle init` must NOT gitignore `.env` (the env doc says to commit it).

    Only machine-local secret overrides (`.env.local`, `.env.*.local`) are
    ignored; the shared `.env` / `.env.development` / `.env.production` files
    stay committable, matching docs/guides/environment-variables.md.
    """
    from pyxle.cli.init import run_init

    monkeypatch.chdir(tmp_path)
    run_init("demo", force=False, template="default", logger=cli.ConsoleLogger(), log_steps=False)

    lines = (tmp_path / "demo" / ".gitignore").read_text(encoding="utf-8").splitlines()
    # Local secret overrides remain ignored.
    assert ".env.local" in lines
    assert ".env.*.local" in lines
    # The committable env files must NOT appear as ignore patterns.
    assert ".env" not in lines
    assert ".env.development" not in lines
    assert ".env.production" not in lines


def test_scaffold_gitignore_excludes_build_dirs(tmp_path, monkeypatch) -> None:
    """`pyxle init` must gitignore BOTH build outputs — the dev cache
    (.pyxle-build/) and the production build dir (dist/) — so generated,
    regenerable artifacts never get committed."""
    from pyxle.cli.init import run_init

    monkeypatch.chdir(tmp_path)
    run_init("demo", force=False, template="default", logger=cli.ConsoleLogger(), log_steps=False)

    gitignore = (tmp_path / "demo" / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert ".pyxle-build/" in gitignore  # dev incremental-build cache
    assert "dist/" in gitignore  # `pyxle build` production output


def test_run_subprocess_handles_missing_binary(monkeypatch, tmp_path) -> None:
    def fake_run(*_, **__):
        raise FileNotFoundError("missing binary")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    logger = cli.ConsoleLogger()

    with pytest.raises(typer.Exit):
        cli._run_subprocess(["npm", "install"], cwd=tmp_path, label="Node", logger=logger)


def test_serve_refuses_an_occupied_port_before_building_anything(monkeypatch) -> None:
    """The check has to come *before* the build, or it costs a build to learn.

    ``pyxle serve`` rebuilds the project on the way up. Discovering the port is
    taken on the far side of that made the developer pay a full build for a
    failure that was knowable in milliseconds — and printed
    ``Serving Pyxle build on http://host:port``, a success line with a clickable
    URL, for a server that then failed to bind.
    """
    built: list[str] = []
    monkeypatch.setattr(cli, "run_build", lambda *a, **k: built.append("built"))

    with runner.isolated_filesystem():
        project = Path("demo")
        (project / "pages").mkdir(parents=True)

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(("127.0.0.1", 0))
            sock.listen(1)
            port = sock.getsockname()[1]

            result = runner.invoke(
                app,
                ["serve", "demo", "--host", "127.0.0.1", "--port", str(port)],
            )
        finally:
            sock.close()

    assert result.exit_code == 1
    assert "already in use" in result.stdout
    assert str(port) in result.stdout
    # The whole point: nothing was built, and no URL was advertised.
    assert built == []
    assert "Serving Pyxle build on" not in result.stdout


def test_dev_refuses_an_occupied_port_before_starting_anything(monkeypatch) -> None:
    """Same, one step earlier: before Vite, npm install and the first build.

    With Vite already spawned, the *last* line on screen was
    ``[vite] process exited with code 143`` — the SIGTERM of an innocent child
    during teardown — so the most prominent message named the one component
    that had done nothing wrong.
    """
    started: list[str] = []

    class ExplodingDevServer:
        def __init__(self, *a, **k):
            started.append("constructed")

    monkeypatch.setattr(cli, "DevServer", ExplodingDevServer)

    with runner.isolated_filesystem():
        project = Path("demo")
        (project / "pages").mkdir(parents=True)

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(("127.0.0.1", 0))
            sock.listen(1)
            port = sock.getsockname()[1]

            result = runner.invoke(
                app,
                ["dev", "demo", "--host", "127.0.0.1", "--port", str(port)],
            )
        finally:
            sock.close()

    assert result.exit_code == 1
    assert "already in use" in result.stdout
    assert started == []
    # uvicorn's raw errno is what this replaced; it must not survive alongside.
    assert "Errno 98" not in result.stdout


def test_serve_command_runs_build_and_uvicorn(monkeypatch) -> None:
    with runner.isolated_filesystem():
        project = Path("demo")
        (project / "pages").mkdir(parents=True)
        (project / "public").mkdir(parents=True)

        dist_root = project / "dist"
        # Vite's bundle directory — the only part of dist/client/ that is served.
        client_dir = dist_root / "client" / "dist"
        public_dir = dist_root / "public"
        client_dir.mkdir(parents=True, exist_ok=True)
        public_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = dist_root / "page-manifest.json"
        manifest_path.parent.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text('{"/": {"client": {"file": "client/bundle.js"}}}', encoding="utf-8")

        captured: dict[str, object] = {}

        def fake_run_build(settings, *, logger, dist_dir=None, force_rebuild=True):
            captured["build_settings"] = settings
            captured["dist_dir"] = dist_dir
            captured["force_rebuild"] = force_rebuild

        monkeypatch.setattr(cli, "run_build", fake_run_build)

        registry_sentinel = object()
        route_table_sentinel = object()

        monkeypatch.setattr("pyxle.build.production.build_metadata_registry",
            lambda settings, metadata=None: registry_sentinel)
        monkeypatch.setattr("pyxle.build.production.build_route_table",lambda registry: route_table_sentinel)

        app_instance = SimpleNamespace(state=SimpleNamespace(pyxle_ready=False))

        def fake_create_app(settings, routes, **kwargs):
            captured["create_settings"] = settings
            captured["routes"] = routes
            captured["create_kwargs"] = kwargs
            return app_instance

        monkeypatch.setattr("pyxle.build.production.create_starlette_app", fake_create_app)

        class StubServer:
            def __init__(self, config):
                captured["uvicorn_config"] = config

            async def serve(self):
                captured["served"] = True

        monkeypatch.setattr(cli.uvicorn, "Server", StubServer)

        def fake_asyncio_run(coro):
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(coro)
            finally:
                loop.close()

        monkeypatch.setattr(cli.asyncio, "run", fake_asyncio_run)

        result = runner.invoke(
            app,
            [
                "serve",
                "demo",
                "--host",
                "0.0.0.0",
                "--port",
                "8200",
            ],
            catch_exceptions=False,
        )

        assert result.exit_code == 0, result.stdout
        assert captured["dist_dir"] == (project / "dist").resolve()
        assert captured["force_rebuild"] is True
        assert captured["routes"] is route_table_sentinel
        assert captured["create_kwargs"]["public_static_dir"] == public_dir.resolve()
        assert captured["create_kwargs"]["client_static_dir"] == client_dir.resolve()
        assert app_instance.state.pyxle_ready is True
        assert captured.get("served") is True


def test_serve_command_can_disable_static_mounts(monkeypatch) -> None:
    with runner.isolated_filesystem():
        project = Path("demo")
        (project / "pages").mkdir(parents=True)
        (project / "public").mkdir(parents=True)

        dist_root = project / "dist"
        dist_root.mkdir(parents=True, exist_ok=True)
        (dist_root / "page-manifest.json").write_text('{}', encoding="utf-8")

        monkeypatch.setattr(cli, "run_build", lambda *_, **__: None)
        monkeypatch.setattr("pyxle.build.production.build_metadata_registry",
            lambda settings, metadata=None: object())
        monkeypatch.setattr("pyxle.build.production.build_route_table",lambda registry: object())

        captured: dict[str, object] = {}

        def fake_create_app(*_, **kwargs):
            captured.update(kwargs)
            app_instance = SimpleNamespace(state=SimpleNamespace(pyxle_ready=False))
            return app_instance

        monkeypatch.setattr("pyxle.build.production.create_starlette_app", fake_create_app)

        class StubServer:
            def __init__(self, config):
                pass

            async def serve(self):
                return None

        monkeypatch.setattr(cli.uvicorn, "Server", StubServer)
        def fake_asyncio_run(coro):
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(coro)
            finally:
                loop.close()

        monkeypatch.setattr(cli.asyncio, "run", fake_asyncio_run)

        result = runner.invoke(app, ["serve", "demo", "--no-serve-static"], catch_exceptions=False)

        assert result.exit_code == 0
        assert captured["public_static_dir"] is None
        assert captured["client_static_dir"] is None
        assert captured["serve_static"] is False


def test_serve_command_requires_manifest_when_skipping_build(monkeypatch) -> None:
    with runner.isolated_filesystem():
        project = Path("demo")
        (project / "pages").mkdir(parents=True)
        (project / "public").mkdir(parents=True)

        # Ensure run_build is not invoked when skipping
        def fake_run_build(*_, **__):  # pragma: no cover - should not be called
            raise AssertionError("run_build should not run when --skip-build is set")

        monkeypatch.setattr(cli, "run_build", fake_run_build)

        result = runner.invoke(
            app,
            ["serve", "demo", "--skip-build"],
            catch_exceptions=False,
        )

        assert result.exit_code == 1
        assert "page-manifest" in result.stdout


def test_install_dependencies_flag_skips(monkeypatch, tmp_path) -> None:
    # Python deps are only installed when there is something to install;
    # these tests assert on the pip command, so give them a reason to run one.
    (tmp_path / "requirements.txt").write_text("pyxle-framework\n", encoding="utf-8")

    calls: list[list[str]] = []

    def fake_run(command, *, cwd, check, stdout=None, stderr=None, text=False):
        calls.append(command)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    logger = cli.ConsoleLogger()

    cli._install_dependencies(tmp_path, logger=logger, install_python=False, install_node=True)
    assert calls == [["npm", "install"]]

    calls.clear()
    cli._install_dependencies(tmp_path, logger=logger, install_python=True, install_node=False)
    assert calls[0][0] == sys.executable


def test_install_dependencies_warns_when_disabled(monkeypatch, tmp_path) -> None:
    def fake_run(*_, **__):  # pragma: no cover - should not be called
        raise AssertionError("Should not run installers when both disabled")

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    logger = cli.ConsoleLogger()
    warnings: list[str] = []
    monkeypatch.setattr(logger, "warning", lambda message: warnings.append(message))

    cli._install_dependencies(tmp_path, logger=logger, install_python=False, install_node=False)
    assert warnings and "Skipping dependency installation" in warnings[0]


def test_run_subprocess_handles_failed_exit(monkeypatch, tmp_path) -> None:
    def fake_run(command, *, cwd, check, stdout=None, stderr=None, text=False):
        raise subprocess.CalledProcessError(returncode=2, cmd=command)

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    logger = cli.ConsoleLogger()

    with pytest.raises(typer.Exit):
        cli._run_subprocess(["npm", "install"], cwd=tmp_path, label="Node", logger=logger)


_NOISY_CHILD = (
    "import sys\n"
    "print('Requirement already satisfied: certifi in /some/site-packages')\n"
    "print('[notice] A new release of pip is available: 26.1.2 -> 26.2.1')\n"
    "sys.stderr.write('npm warn deprecated something@1.0.0\\n')\n"
    "sys.exit(int(sys.argv[1]))\n"
)


def _noisy_installer(tmp_path, exit_code: int) -> list[str]:
    """A stand-in installer that chatters like pip/npm, then exits *exit_code*.

    Written to a file (rather than passed with ``-c``) so the echoed step line
    can't accidentally satisfy assertions about the child's own output.
    """
    script = tmp_path / "noisy_installer.py"
    script.write_text(_NOISY_CHILD, encoding="utf-8")
    return [sys.executable, str(script), str(exit_code)]


def test_run_subprocess_hides_tool_chatter_on_success(tmp_path, capfd, uninstrumented_child) -> None:
    """A successful installer prints the step line and nothing of pip/npm's own noise.

    Runs a REAL child process (not a mock) so this test fails if the output is
    merely redirected rather than captured.
    """
    cli._run_subprocess(
        _noisy_installer(tmp_path, 0),
        cwd=tmp_path,
        label="Python dependencies",
        logger=cli.ConsoleLogger(),
    )

    output = capfd.readouterr()
    combined = output.out + output.err
    assert "Python dependencies" in combined  # the step line still shows
    assert "Requirement already satisfied" not in combined
    assert "A new release of pip is available" not in combined
    assert "npm warn deprecated" not in combined


def test_run_subprocess_shows_tool_output_on_failure(tmp_path, capfd, uninstrumented_child) -> None:
    """A failing installer must still show the user everything the tool said."""
    with pytest.raises(typer.Exit):
        cli._run_subprocess(
            _noisy_installer(tmp_path, 1),
            cwd=tmp_path,
            label="Python dependencies",
            logger=cli.ConsoleLogger(),
        )

    output = capfd.readouterr()
    combined = output.out + output.err
    assert "Requirement already satisfied" in combined
    assert "npm warn deprecated" in combined
    assert "Python dependencies failed with exit code 1." in combined


def test_run_subprocess_streams_live_when_verbose(tmp_path, capfd, uninstrumented_child) -> None:
    """`pyxle -v install` streams the child's output instead of capturing it."""
    logger = cli.ConsoleLogger()
    logger.set_verbosity(cli.Verbosity.VERBOSE)

    cli._run_subprocess(
        _noisy_installer(tmp_path, 0),
        cwd=tmp_path,
        label="Python dependencies",
        logger=logger,
    )

    output = capfd.readouterr()
    combined = output.out + output.err
    assert "Requirement already satisfied" in combined
    assert "npm warn deprecated" in combined


def test_resolve_run_build_prefers_overridden_callable(monkeypatch) -> None:
    def fake_run_build(*_, **__):
        return "ok"

    monkeypatch.setattr(cli, "run_build", fake_run_build)

    resolved = cli._resolve_run_build()
    assert resolved is fake_run_build


def test_init_optionally_installs_dependencies(monkeypatch) -> None:
    with runner.isolated_filesystem():
        called: dict[str, Path] = {}

        def fake_install(
            project_root,
            *,
            logger,
            install_python=True,
            install_node=True,
            break_system_packages=False,
        ):
            called["root"] = project_root.resolve()

        monkeypatch.setattr(cli, "_install_dependencies", fake_install)

        result = runner.invoke(app, ["init", "demo", "--install"], catch_exceptions=False)
        assert result.exit_code == 0, result.stdout
        assert called["root"] == Path("demo").resolve()
        assert "Next steps" in result.stdout
        assert "pyxle install" not in result.stdout
        assert "pyxle dev" in result.stdout


def test_dev_command_requires_existing_directory() -> None:
    with runner.isolated_filesystem():
        result = runner.invoke(app, ["dev", "missing"], catch_exceptions=False)
        assert result.exit_code == 1
        assert "does not exist" in result.stdout


def test_dev_command_rejects_file_path() -> None:
    with runner.isolated_filesystem():
        file_path = Path("not-a-dir.txt")
        file_path.write_text("demo", encoding="utf-8")

        result = runner.invoke(app, ["dev", "not-a-dir.txt"], catch_exceptions=False)
        assert result.exit_code == 1
        assert "not a directory" in result.stdout


def test_dev_command_invokes_devserver(monkeypatch) -> None:
    with runner.isolated_filesystem():
        project = Path("demo")
        (project / "pages").mkdir(parents=True)
        (project / "public").mkdir(parents=True)

        captured: dict[str, object] = {}

        class StubDevServer:
            def __init__(self, settings, logger, **kwargs):
                captured["settings"] = settings
                captured["logger"] = logger

            async def start(self) -> None:
                captured["started"] = True

        def fake_run(coro):
            captured["run_invoked"] = True
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(coro)
            finally:
                loop.close()

        monkeypatch.setattr("pyxle.cli.DevServer", StubDevServer)
        monkeypatch.setattr("pyxle.cli.asyncio.run", fake_run)

        result = runner.invoke(
            app,
            [
                "dev",
                "demo",
                "--host",
                "0.0.0.0",
                "--port",
                "9000",
                "--vite-host",
                "localhost",
                "--vite-port",
                "1234",
                "--no-debug",
            ],
            catch_exceptions=False,
        )

        assert result.exit_code == 0, result.stdout
        settings = captured["settings"]
        assert settings.project_root == project.resolve()
        assert settings.starlette_host == "0.0.0.0"
        assert settings.starlette_port == 9000
        assert settings.vite_host == "localhost"
        assert settings.vite_port == 1234
        assert settings.debug is False
        assert captured.get("started") is True
        assert captured.get("run_invoked") is True
        assert captured.get("logger").__class__.__name__ == "ConsoleLogger"


@pytest.mark.parametrize("argv", [["dev", "demo", "--verbose"], ["-v", "dev", "demo"]])
def test_dev_command_verbose_sets_verbose_verbosity(monkeypatch, argv) -> None:
    """Both `pyxle dev --verbose` and `pyxle -v dev` raise the shared logger."""
    from pyxle.cli.logger import Verbosity

    with runner.isolated_filesystem():
        project = Path("demo")
        (project / "pages").mkdir(parents=True)
        (project / "public").mkdir(parents=True)

        captured: dict[str, object] = {}

        class StubDevServer:
            def __init__(self, settings, logger, **kwargs):
                captured["verbosity"] = logger.verbosity

            async def start(self) -> None:  # pragma: no cover - not awaited
                captured["started"] = True

        from pyxle.devserver import DevServerSettings as _RealSettings

        monkeypatch.setattr("pyxle.cli.DevServer", StubDevServer)
        monkeypatch.setattr("pyxle.cli.DevServerSettings", _RealSettings)
        monkeypatch.setattr("pyxle.cli.asyncio.run", lambda coro: coro.close())

        result = runner.invoke(app, argv, catch_exceptions=False)

        assert result.exit_code == 0, result.stdout
        assert captured["verbosity"] == Verbosity.VERBOSE


def test_dev_command_default_verbosity_is_normal(monkeypatch) -> None:
    from pyxle.cli.logger import Verbosity

    with runner.isolated_filesystem():
        project = Path("demo")
        (project / "pages").mkdir(parents=True)
        (project / "public").mkdir(parents=True)

        captured: dict[str, object] = {}

        class StubDevServer:
            def __init__(self, settings, logger, **kwargs):
                captured["verbosity"] = logger.verbosity

            async def start(self) -> None:  # pragma: no cover - not awaited
                captured["started"] = True

        from pyxle.devserver import DevServerSettings as _RealSettings

        monkeypatch.setattr("pyxle.cli.DevServer", StubDevServer)
        monkeypatch.setattr("pyxle.cli.DevServerSettings", _RealSettings)
        monkeypatch.setattr("pyxle.cli.asyncio.run", lambda coro: coro.close())

        result = runner.invoke(app, ["dev", "demo"], catch_exceptions=False)

        assert result.exit_code == 0, result.stdout
        assert captured["verbosity"] == Verbosity.NORMAL


def test_dev_command_dashboard_flag(monkeypatch) -> None:
    with runner.isolated_filesystem():
        project = Path("demo")
        (project / "pages").mkdir(parents=True)
        (project / "public").mkdir(parents=True)

        captured: dict[str, object] = {}

        class StubDevServer:
            def __init__(self, settings, logger, **kwargs):
                captured.update(kwargs)

            async def start(self) -> None:
                pass

        def fake_run(coro):
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(coro)
            finally:
                loop.close()

        # dev() lazily initialises its DevServer / DevServerSettings globals and
        # re-imports them when either is None, which would clobber the stub.
        # Pin DevServerSettings to the real class so that block is skipped.
        from pyxle.devserver import DevServerSettings as _RealSettings

        monkeypatch.setattr("pyxle.cli.DevServerSettings", _RealSettings)
        monkeypatch.setattr("pyxle.cli.DevServer", StubDevServer)
        monkeypatch.setattr("pyxle.cli.asyncio.run", fake_run)

        result = runner.invoke(
            app, ["dev", "demo", "--dashboard"], catch_exceptions=False
        )
        assert result.exit_code == 0, result.stdout
        assert captured.get("dashboard") is True


def _invoke_studio_command(monkeypatch, argv, config_payload=None) -> dict[str, object]:
    """Run ``pyxle studio`` against a stub DevServer and return its capture."""
    with runner.isolated_filesystem():
        project = Path("demo")
        (project / "pages").mkdir(parents=True)
        (project / "public").mkdir(parents=True)
        if config_payload is not None:
            (project / "pyxle.config.json").write_text(
                json.dumps(config_payload), encoding="utf-8"
            )

        captured: dict[str, object] = {}

        class StubDevServer:
            def __init__(self, settings, logger, **kwargs):
                captured["settings"] = settings
                captured.update(kwargs)

            async def start(self) -> None:  # pragma: no cover - not awaited
                pass

        from pyxle.devserver import DevServerSettings as _RealSettings

        monkeypatch.setattr("pyxle.cli.DevServerSettings", _RealSettings)
        monkeypatch.setattr("pyxle.cli.DevServer", StubDevServer)
        monkeypatch.setattr("pyxle.cli.asyncio.run", lambda coro: coro.close())

        result = runner.invoke(app, argv, catch_exceptions=False)
        assert result.exit_code == 0, result.stdout
        return captured


def test_studio_command_opens_browser_at_the_dashboard(monkeypatch) -> None:
    captured = _invoke_studio_command(monkeypatch, ["studio", "demo"])
    assert captured["open_browser_path"] == "/__pyxle/studio"
    settings = captured["settings"]
    assert settings.debug is True  # Studio is dev-only; debug is forced on
    # Default-on: no config block means the dashboard is enabled.
    assert settings.studio.enabled is True


def test_studio_command_no_open_flag_skips_the_browser(monkeypatch) -> None:
    captured = _invoke_studio_command(monkeypatch, ["studio", "demo", "--no-open"])
    assert captured["open_browser_path"] is None


def test_studio_command_wins_over_config_opt_out(monkeypatch) -> None:
    captured = _invoke_studio_command(
        monkeypatch,
        ["studio", "demo"],
        config_payload={
            "debug": False,
            "studio": {"enabled": False, "allowedHosts": ["mybox.local"]},
        },
    )
    settings = captured["settings"]
    # `pyxle studio` explicitly asked for the dashboard: the run is debug with
    # Studio enabled, but the rest of the studio block is preserved.
    assert settings.debug is True
    assert settings.studio.enabled is True
    assert settings.studio.allowed_hosts == ("mybox.local",)


def test_dev_command_respects_studio_config_opt_out(monkeypatch) -> None:
    # Plain `pyxle dev` never forces the dashboard back on.
    captured = _invoke_studio_command(
        monkeypatch, ["dev", "demo"], config_payload={"studio": False}
    )
    assert captured["settings"].studio.enabled is False
    assert captured["open_browser_path"] is None


def test_studio_command_requires_existing_directory() -> None:
    with runner.isolated_filesystem():
        result = runner.invoke(app, ["studio", "missing"], catch_exceptions=False)
        assert result.exit_code == 1
        assert "does not exist" in result.stdout


def test_dev_command_respects_config_file(monkeypatch) -> None:
    with runner.isolated_filesystem():
        project = Path("demo")
        (project / "src" / "pages").mkdir(parents=True)
        (project / "static").mkdir(parents=True)

        config_payload = {
            "pagesDir": "src/pages",
            "publicDir": "static",
            "buildDir": ".pyxle-dist",
            "starlette": {"host": "0.0.0.0", "port": 9100},
            "vite": {"host": "localhost", "port": 6200},
            "debug": False,
        }
        (project / "pyxle.config.json").write_text(json.dumps(config_payload), encoding="utf-8")

        captured: dict[str, object] = {}

        class StubDevServer:
            def __init__(self, settings, logger, **kwargs):
                captured["settings"] = settings
                captured["logger"] = logger

            async def start(self) -> None:
                captured["started"] = True

        def fake_run(coro):
            captured["run_invoked"] = True
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(coro)
            finally:
                loop.close()

        monkeypatch.setattr("pyxle.cli.DevServer", StubDevServer)
        monkeypatch.setattr("pyxle.cli.asyncio.run", fake_run)

        result = runner.invoke(app, ["dev", "demo"], catch_exceptions=False)

        assert result.exit_code == 0, result.stdout
        settings = captured["settings"]
        assert settings.project_root == project.resolve()
        assert settings.pages_dir == (project / "src" / "pages").resolve()
        assert settings.public_dir == (project / "static").resolve()
        assert settings.build_root == (project / ".pyxle-dist").resolve()
        assert settings.starlette_host == "0.0.0.0"
        assert settings.starlette_port == 9100
        assert settings.vite_host == "localhost"
        assert settings.vite_port == 6200
        assert settings.debug is False
        assert captured.get("started") is True
        assert captured.get("run_invoked") is True


def test_build_command_invokes_pipeline(monkeypatch) -> None:
    with runner.isolated_filesystem():
        project = Path("demo")
        (project / "pages").mkdir(parents=True)
        (project / "public").mkdir(parents=True)

        captured: dict[str, object] = {}

        def fake_run_build(settings, *, logger, dist_dir=None, force_rebuild=True):
            captured["settings"] = settings
            captured["logger"] = logger
            captured["dist_dir"] = dist_dir
            captured["force_rebuild"] = force_rebuild
            from pyxle.build.pipeline import BuildResult
            from pyxle.devserver.builder import BuildSummary
            from pyxle.devserver.registry import MetadataRegistry

            summary = BuildSummary()
            result_dist = dist_dir or settings.project_root / "dist"
            (result_dist / "client").mkdir(parents=True, exist_ok=True)
            (result_dist / "server").mkdir(parents=True, exist_ok=True)
            (result_dist / "metadata").mkdir(parents=True, exist_ok=True)
            (result_dist / "public").mkdir(parents=True, exist_ok=True)
            client_manifest_path = result_dist / "client" / "manifest.json"
            client_manifest_path.write_text("{}", encoding="utf-8")
            page_manifest_path = result_dist / "page-manifest.json"
            page_manifest_path.write_text("{}", encoding="utf-8")
            return BuildResult(
                dist_dir=result_dist,
                client_dir=result_dist / "client",
                server_dir=result_dist / "server",
                metadata_dir=result_dist / "metadata",
                public_dir=result_dist / "public",
                client_manifest_path=client_manifest_path,
                page_manifest={"/": {"client": {"file": "client/index.js", "imports": []}}},
                page_manifest_path=page_manifest_path,
                summary=summary,
                registry=MetadataRegistry(pages=[], apis=[]),
            )

        monkeypatch.setattr("pyxle.cli.run_build", fake_run_build)

        result = runner.invoke(
            app,
            [
                "build",
                "demo",
                "--out-dir",
                "dist-prod",
            ],
            catch_exceptions=False,
        )

        assert result.exit_code == 0, result.stdout
        settings = captured["settings"]
        assert settings.project_root == project.resolve()
        expected_out_dir = (project / "dist-prod").resolve()
        assert captured["dist_dir"] == expected_out_dir
        assert captured["force_rebuild"] is True
        assert "Build completed" in result.stdout
        assert "Artifacts" in result.stdout
        assert "Client manifest" in result.stdout
        assert "Page manifest" in result.stdout
        assert "Server modules" in result.stdout
        assert "Metadata" in result.stdout
        assert "Public assets" in result.stdout


def test_build_command_static_flag_prerenders(monkeypatch) -> None:
    with runner.isolated_filesystem():
        project = Path("demo")
        (project / "pages").mkdir(parents=True)
        (project / "public").mkdir(parents=True)

        def fake_run_build(settings, *, logger, dist_dir=None, force_rebuild=True):
            from pyxle.build.pipeline import BuildResult
            from pyxle.devserver.builder import BuildSummary
            from pyxle.devserver.registry import MetadataRegistry

            result_dist = dist_dir or settings.project_root / "dist"
            for sub in ("client", "server", "metadata", "public"):
                (result_dist / sub).mkdir(parents=True, exist_ok=True)
            page_manifest_path = result_dist / "page-manifest.json"
            page_manifest_path.write_text("{}", encoding="utf-8")
            return BuildResult(
                dist_dir=result_dist,
                client_dir=result_dist / "client",
                server_dir=result_dist / "server",
                metadata_dir=result_dist / "metadata",
                public_dir=result_dist / "public",
                client_manifest_path=None,
                page_manifest={},
                page_manifest_path=page_manifest_path,
                summary=BuildSummary(),
                registry=MetadataRegistry(pages=[], apis=[]),
            )

        captured: dict[str, object] = {}

        def fake_generate(settings, dist_dir, *, logger=None):
            captured["dist_dir"] = dist_dir
            return ["/", "/about"]

        monkeypatch.setattr("pyxle.cli.run_build", fake_run_build)
        monkeypatch.setattr("pyxle.build.static_gen.generate_static_site", fake_generate)

        result = runner.invoke(app, ["build", "demo", "--static"], catch_exceptions=False)

        assert result.exit_code == 0, result.stdout
        assert captured["dist_dir"] == (project / "dist").resolve()
        assert "Static pages" in result.stdout
        assert "2 pre-rendered" in result.stdout


def test_build_command_supports_incremental_flag(monkeypatch) -> None:
    with runner.isolated_filesystem():
        project = Path("demo")
        (project / "pages").mkdir(parents=True)
        (project / "public").mkdir(parents=True)

        captured: dict[str, object] = {}

        def fake_run_build(settings, *, logger, dist_dir=None, force_rebuild=True):
            captured["force_rebuild"] = force_rebuild
            from pyxle.build.pipeline import BuildResult
            from pyxle.devserver.builder import BuildSummary
            from pyxle.devserver.registry import MetadataRegistry

            summary = BuildSummary()
            result_dist = settings.project_root / "dist"
            (result_dist / "client").mkdir(parents=True, exist_ok=True)
            (result_dist / "server").mkdir(parents=True, exist_ok=True)
            (result_dist / "metadata").mkdir(parents=True, exist_ok=True)
            (result_dist / "public").mkdir(parents=True, exist_ok=True)
            client_manifest_path = result_dist / "client" / "manifest.json"
            client_manifest_path.write_text("{}", encoding="utf-8")
            page_manifest_path = result_dist / "page-manifest.json"
            page_manifest_path.write_text("{}", encoding="utf-8")
            return BuildResult(
                dist_dir=result_dist,
                client_dir=result_dist / "client",
                server_dir=result_dist / "server",
                metadata_dir=result_dist / "metadata",
                public_dir=result_dist / "public",
                client_manifest_path=client_manifest_path,
                page_manifest={},
                page_manifest_path=page_manifest_path,
                summary=summary,
                registry=MetadataRegistry(pages=[], apis=[]),
            )

        monkeypatch.setattr("pyxle.cli.run_build", fake_run_build)

        result = runner.invoke(app, ["build", "demo", "--incremental"], catch_exceptions=False)

        assert result.exit_code == 0, result.stdout
        assert captured.get("force_rebuild") is False


def test_build_command_analyze_counts_only_the_bundle(monkeypatch) -> None:
    """``--analyze`` must report what the browser downloads, nothing else.

    Walking ``dist/client/`` instead of ``dist/client/dist/`` swept in the
    generated ``vite.config.js``, ``client-entry.js`` and the CSS sources Vite
    consumed — build inputs no browser fetches — and inflated the total.
    """
    with runner.isolated_filesystem():
        project = Path("demo")
        (project / "pages").mkdir(parents=True)
        (project / "public").mkdir(parents=True)

        def fake_run_build(settings, *, logger, dist_dir=None, force_rebuild=True):
            from pyxle.build.pipeline import BuildResult
            from pyxle.devserver.builder import BuildSummary
            from pyxle.devserver.registry import MetadataRegistry

            result_dist = settings.project_root / "dist"
            client = result_dist / "client"
            (client / "dist" / "assets").mkdir(parents=True, exist_ok=True)
            (client / "dist" / "assets" / "index-a1b2c3d4.js").write_bytes(b"a" * 1000)
            # Build inputs beside the bundle.
            (client / "vite.config.js").write_bytes(b"b" * 6000)
            (client / "client-entry.js").write_bytes(b"c" * 46000)
            for name in ("server", "metadata", "public"):
                (result_dist / name).mkdir(parents=True, exist_ok=True)
            page_manifest_path = result_dist / "page-manifest.json"
            page_manifest_path.write_text("{}", encoding="utf-8")
            return BuildResult(
                dist_dir=result_dist,
                client_dir=client,
                server_dir=result_dist / "server",
                metadata_dir=result_dist / "metadata",
                public_dir=result_dist / "public",
                client_manifest_path=None,
                page_manifest={},
                page_manifest_path=page_manifest_path,
                summary=BuildSummary(),
                registry=MetadataRegistry(pages=[], apis=[]),
            )

        monkeypatch.setattr("pyxle.cli.run_build", fake_run_build)

        result = runner.invoke(app, ["build", "demo", "--analyze"], catch_exceptions=False)

        assert result.exit_code == 0, result.stdout
        assert "assets/index-a1b2c3d4.js" in result.stdout
        assert "vite.config.js" not in result.stdout
        assert "client-entry.js" not in result.stdout
        assert "(1 file(s))" in result.stdout


def test_build_command_logs_missing_public_assets(monkeypatch) -> None:
    with runner.isolated_filesystem():
        project = Path("demo")
        (project / "pages").mkdir(parents=True)
        (project / "public").mkdir(parents=True)

        def fake_run_build(settings, *, logger, dist_dir=None, force_rebuild=True):
            from pyxle.build.pipeline import BuildResult
            from pyxle.devserver.builder import BuildSummary
            from pyxle.devserver.registry import MetadataRegistry

            summary = BuildSummary()
            result_dist = settings.project_root / "dist"
            (result_dist / "client").mkdir(parents=True, exist_ok=True)
            (result_dist / "server").mkdir(parents=True, exist_ok=True)
            (result_dist / "metadata").mkdir(parents=True, exist_ok=True)
            client_manifest_path = result_dist / "client" / "manifest.json"
            client_manifest_path.write_text("{}", encoding="utf-8")
            page_manifest_path = result_dist / "page-manifest.json"
            page_manifest_path.write_text("{}", encoding="utf-8")
            missing_public = result_dist / "public-missing"
            return BuildResult(
                dist_dir=result_dist,
                client_dir=result_dist / "client",
                server_dir=result_dist / "server",
                metadata_dir=result_dist / "metadata",
                public_dir=missing_public,
                client_manifest_path=client_manifest_path,
                page_manifest={"/": {"client": {"file": "client/index.js", "imports": []}}},
                page_manifest_path=page_manifest_path,
                summary=summary,
                registry=MetadataRegistry(pages=[], apis=[]),
            )

        monkeypatch.setattr("pyxle.cli.run_build", fake_run_build)

        result = runner.invoke(app, ["build", "demo"], catch_exceptions=False)

        assert result.exit_code == 0, result.stdout
        assert "Public assets" in result.stdout
        assert "(not generated)" in result.stdout


def test_dev_command_prints_effective_config(monkeypatch) -> None:
    with runner.isolated_filesystem():
        project = Path("demo")
        (project / "pages").mkdir(parents=True)
        (project / "public").mkdir(parents=True)

        (project / "pyxle.config.json").write_text(
            json.dumps(
                {
                    "starlette": {"host": "127.0.0.1", "port": 8300},
                    "vite": {"host": "127.0.0.1", "port": 5400},
                }
            ),
            encoding="utf-8",
        )

        captured: dict[str, object] = {}

        class StubDevServer:
            def __init__(self, settings, logger, **kwargs):
                captured["settings"] = settings
                captured["logger"] = logger

            async def start(self) -> None:
                captured["started"] = True

        monkeypatch.setattr("pyxle.cli.DevServer", StubDevServer)

        def fake_run(coro):
            captured["run_invoked"] = True
            loop = asyncio.new_event_loop()
            try:
                return loop.run_until_complete(coro)
            finally:
                loop.close()

        monkeypatch.setattr("pyxle.cli.asyncio.run", fake_run)

        result = runner.invoke(
            app,
            [
                "dev",
                "demo",
                "--print-config",
                "--vite-port",
                "6000",
            ],
            catch_exceptions=False,
        )

        assert result.exit_code == 0, result.stdout
        assert "Effective configuration" in result.stdout
        assert "\"vite\": {" in result.stdout
        assert "6000" in result.stdout
        assert captured.get("run_invoked") is True


def test_dev_command_fails_with_invalid_config() -> None:
    with runner.isolated_filesystem():
        project = Path("demo")
        project.mkdir()
        (project / "pages").mkdir()
        (project / "public").mkdir()
        (project / "pyxle.config.json").write_text("[]", encoding="utf-8")

        result = runner.invoke(app, ["dev", "demo"], catch_exceptions=False)

        assert result.exit_code == 1
        assert "Configuration file" in result.stdout


def test_build_command_requires_existing_directory() -> None:
    with runner.isolated_filesystem():
        result = runner.invoke(app, ["build", "missing"], catch_exceptions=False)
        assert result.exit_code == 1
        assert "does not exist" in result.stdout


def test_build_command_rejects_file_path() -> None:
    with runner.isolated_filesystem():
        file_path = Path("not-a-dir.txt")
        file_path.write_text("demo", encoding="utf-8")

        result = runner.invoke(app, ["build", "not-a-dir.txt"], catch_exceptions=False)
        assert result.exit_code == 1
        assert "not a directory" in result.stdout


class _StubLogger:
    def __init__(self) -> None:
        self.infos: list[str] = []
        self.errors: list[str] = []
        self.steps: list[tuple[str, str | None]] = []

    def info(self, message: str) -> None:
        self.infos.append(message)

    def success(self, message: str) -> None:
        self.infos.append(message)

    def warning(self, message: str) -> None:
        self.infos.append(message)

    def error(self, message: str) -> None:
        self.errors.append(message)

    def step(self, label: str, detail: str | None = None) -> None:
        self.steps.append((label, detail))


def test_build_function_errors_when_directory_missing(monkeypatch, tmp_path: Path) -> None:
    from pyxle.cli import build

    logger = _StubLogger()
    monkeypatch.setattr("pyxle.cli.get_logger", lambda: logger)

    with pytest.raises(typer.Exit):
        build(directory=tmp_path / "missing")

    assert logger.errors and "does not exist" in logger.errors[0]


def test_build_function_errors_when_path_not_directory(monkeypatch, tmp_path: Path) -> None:
    from pyxle.cli import build

    logger = _StubLogger()
    monkeypatch.setattr("pyxle.cli.get_logger", lambda: logger)

    file_path = tmp_path / "file.txt"
    file_path.write_text("demo", encoding="utf-8")

    with pytest.raises(typer.Exit):
        build(directory=file_path)

    assert logger.errors and "not a directory" in logger.errors[0]


def test_compile_hidden_command_invokes_compiler() -> None:
    with runner.isolated_filesystem():
        source_dir = Path("pages/posts")
        source_dir.mkdir(parents=True)
        source_file = source_dir / "[id].pyxl"
        source_file.write_text(
            dedent(
                """
                
                @server
                async def loader(request):
                    return {"id": request.params.get("id")}

                # --- JavaScript/PSX ---
                import React from 'react';

                export default function Page({ data }) {
                    return <div>{data.id}</div>;
                }
                """
            ),
            encoding="utf-8",
        )

        result = runner.invoke(app, ["compile", str(source_file)], catch_exceptions=False)
        assert result.exit_code == 0, result.stdout
        assert "Compiled" in result.stdout

        build_root = Path(".pyxle-build")
        server_artifact = build_root / "server/pages/posts/[id].py"
        client_artifact = build_root / "client/pages/posts/[id].jsx"
        metadata_artifact = build_root / "metadata/pages/posts/[id].json"

        assert server_artifact.exists()
        assert client_artifact.exists()
        metadata = read_json(metadata_artifact)
        assert metadata["route_path"] == "/posts/{id}"
        assert metadata["loader_name"] == "loader"
    assert metadata["alternate_route_paths"] == []
    assert metadata["head"] == []


def test_resolve_run_build_returns_existing_callable(monkeypatch):
    sentinel = object()

    def fake_run_build(*args, **kwargs):  # pragma: no cover - function body unused
        return sentinel

    monkeypatch.setattr(cli, "run_build", fake_run_build)

    resolved = cli._resolve_run_build()

    assert resolved is fake_run_build


def test_resolve_run_build_imports_when_placeholder(monkeypatch):
    def stub_run_build(*args, **kwargs):  # pragma: no cover - function body unused
        raise AssertionError("Should not be invoked during resolution")

    monkeypatch.setattr(cli, "run_build", None)
    monkeypatch.setattr("pyxle.build.run_build", stub_run_build)

    resolved = cli._resolve_run_build()

    assert resolved is stub_run_build


    def test_cli_version_option_displays_version() -> None:
        result = runner.invoke(app, ["--version"], catch_exceptions=False)
        assert result.exit_code == 0
        assert __version__ in result.stdout


    def test_version_callback_handles_flag_values(capsys) -> None:
        with pytest.raises(typer.Exit):
            version_callback(True)

        captured = capsys.readouterr()
        assert __version__ in captured.out

        assert version_callback(False) is None


def test_compile_command_errors_when_source_missing() -> None:
    with runner.isolated_filesystem():
        result = runner.invoke(app, ["compile", "pages/missing.pyxl"], catch_exceptions=False)
        assert result.exit_code == 1
        assert "was not found" in result.stdout


def test_compile_command_surfaces_compiler_failure() -> None:
    with runner.isolated_filesystem():
        source_dir = Path("pages")
        source_dir.mkdir()
        source_file = source_dir / "bad.pyxl"
        source_file.write_text(
            dedent(
                """
                @server
                def loader(request):
                    return {}

                export default function Demo() {
                    return <div />;
                }
                """
            ),
            encoding="utf-8",
        )

        result = runner.invoke(app, ["compile", str(source_file)], catch_exceptions=False)
        assert result.exit_code == 1
        assert "Compilation failed" in result.stdout


def test_resolve_global_script_entries_deduplicates(tmp_path: Path) -> None:
    config = PyxleConfig(
        global_scripts=(
            " scripts/track.js ",
            "",
            "scripts/track.js",
            "scripts/analytics.js",
        )
    )

    result = cli._resolve_global_script_entries(tmp_path, config)

    assert result == ("scripts/track.js", "scripts/analytics.js")


def test_dev_command_env_file_error_exits(monkeypatch) -> None:
    """An unreadable .env file causes dev to exit with code 1."""
    with runner.isolated_filesystem():
        project = Path("demo")
        (project / "pages").mkdir(parents=True)
        (project / "public").mkdir(parents=True)

        from pyxle.env import EnvFileError

        monkeypatch.setattr(
            "pyxle.cli.load_env_files",
            lambda *a, **kw: (_ for _ in ()).throw(EnvFileError("cannot read")),
        )

        result = runner.invoke(app, ["dev", "demo"], catch_exceptions=False)

        assert result.exit_code == 1
        assert "cannot read" in result.stdout


def test_dev_command_ssr_workers_flag(monkeypatch) -> None:
    """--ssr-workers is passed through to DevServerSettings."""
    with runner.isolated_filesystem():
        project = Path("demo")
        (project / "pages").mkdir(parents=True)
        (project / "public").mkdir(parents=True)

        captured: dict[str, object] = {}

        class StubDevServer:
            def __init__(self, settings, **kwargs):
                captured["settings"] = settings

            async def start(self) -> None:
                pass

        # Ensure DevServerSettings is populated so the lazy-import guard
        # doesn't overwrite the monkeypatched DevServer.
        from pyxle.devserver import DevServerSettings as _Real
        monkeypatch.setattr("pyxle.cli.DevServerSettings", _Real)
        monkeypatch.setattr("pyxle.cli.DevServer", StubDevServer)
        monkeypatch.setattr("pyxle.cli.asyncio.run", lambda coro: coro.close())

        result = runner.invoke(
            app, ["dev", "demo", "--ssr-workers", "4"], catch_exceptions=False
        )

        assert result.exit_code == 0, result.stdout
        assert captured["settings"].ssr_workers == 4  # type: ignore[union-attr]


def test_build_command_env_file_error_exits(monkeypatch) -> None:
    """An unreadable .env file causes build to exit with code 1."""
    with runner.isolated_filesystem():
        project = Path("demo")
        (project / "pages").mkdir(parents=True)
        (project / "public").mkdir(parents=True)

        from pyxle.env import EnvFileError

        monkeypatch.setattr(
            "pyxle.cli.load_env_files",
            lambda *a, **kw: (_ for _ in ()).throw(EnvFileError("perm denied")),
        )

        result = runner.invoke(app, ["build", "demo"], catch_exceptions=False)

        assert result.exit_code == 1
        assert "perm denied" in result.stdout


def test_serve_command_env_file_error_exits(monkeypatch) -> None:
    """An unreadable .env file causes serve to exit with code 1."""
    with runner.isolated_filesystem():
        project = Path("demo")
        project.mkdir(parents=True)

        from pyxle.env import EnvFileError

        monkeypatch.setattr(
            "pyxle.cli.load_env_files",
            lambda *a, **kw: (_ for _ in ()).throw(EnvFileError("bad file")),
        )

        result = runner.invoke(app, ["serve", "demo"], catch_exceptions=False)

        assert result.exit_code == 1
        assert "bad file" in result.stdout


def test_serve_command_ssr_workers_flag(monkeypatch) -> None:
    """--ssr-workers is passed through to DevServerSettings in pyxle serve."""
    with runner.isolated_filesystem():
        project = Path("demo")
        project.mkdir(parents=True)
        dist = project / "dist"
        dist.mkdir()
        manifest = dist / "page-manifest.json"
        manifest.write_text('{"pages": {}, "generated_at": "2024-01-01"}')
        (dist / "public").mkdir()
        (dist / "client").mkdir()

        captured: dict[str, object] = {}

        def fake_create_app(settings, *a, **kw):
            captured["settings"] = settings
            from starlette.applications import Starlette
            app_obj = Starlette()
            app_obj.state.pyxle_ready = False
            return app_obj

        monkeypatch.setattr("pyxle.build.production.create_starlette_app", fake_create_app)
        monkeypatch.setattr("pyxle.build.production.load_manifest", lambda p: {"pages": {}, "generated_at": "2024-01-01"})
        monkeypatch.setattr("pyxle.build.production.build_metadata_registry", lambda s, metadata=None: {})
        monkeypatch.setattr("pyxle.build.production.build_route_table", lambda r: [])
        monkeypatch.setattr("pyxle.cli.asyncio.run", lambda coro: coro.close())

        async def _noop_serve(self):
            pass

        import uvicorn
        monkeypatch.setattr(uvicorn, "Server", lambda cfg: type("S", (), {"serve": _noop_serve})())

        result = runner.invoke(
            app, ["serve", "demo", "--skip-build", "--ssr-workers", "3"], catch_exceptions=False
        )

        assert result.exit_code == 0, result.stdout
        assert captured["settings"].ssr_workers == 3  # type: ignore[union-attr]


def test_serve_command_defaults_ssr_workers_to_auto(monkeypatch) -> None:
    """Without --ssr-workers, `pyxle serve` defaults to auto-size (0).

    Auto (0) resolves to min(cpu_count, 4) so production renders concurrently by
    default instead of one stream at a time. `pyxle dev` keeps its single worker.
    """
    with runner.isolated_filesystem():
        project = Path("demo")
        project.mkdir(parents=True)
        dist = project / "dist"
        dist.mkdir()
        manifest = dist / "page-manifest.json"
        manifest.write_text('{"pages": {}, "generated_at": "2024-01-01"}')
        (dist / "public").mkdir()
        (dist / "client").mkdir()

        captured: dict[str, object] = {}

        def fake_create_app(settings, *a, **kw):
            captured["settings"] = settings
            from starlette.applications import Starlette
            app_obj = Starlette()
            app_obj.state.pyxle_ready = False
            return app_obj

        monkeypatch.setattr("pyxle.build.production.create_starlette_app", fake_create_app)
        monkeypatch.setattr("pyxle.build.production.load_manifest", lambda p: {"pages": {}, "generated_at": "2024-01-01"})
        monkeypatch.setattr("pyxle.build.production.build_metadata_registry", lambda s, metadata=None: {})
        monkeypatch.setattr("pyxle.build.production.build_route_table", lambda r: [])
        monkeypatch.setattr("pyxle.cli.asyncio.run", lambda coro: coro.close())

        async def _noop_serve(self):
            pass

        import uvicorn
        monkeypatch.setattr(uvicorn, "Server", lambda cfg: type("S", (), {"serve": _noop_serve})())

        result = runner.invoke(
            app, ["serve", "demo", "--skip-build"], catch_exceptions=False
        )

        assert result.exit_code == 0, result.stdout
        assert captured["settings"].ssr_workers == 0  # type: ignore[union-attr]


def test_serve_command_workers_runs_multiprocess_factory(monkeypatch) -> None:
    """`serve --workers N` (N>1) hands uvicorn the importable app factory in
    multi-worker mode and exports the PYXLE_SERVE_* env, instead of building the
    app in-process."""
    with runner.isolated_filesystem():
        project = Path("demo")
        project.mkdir(parents=True)
        dist = project / "dist"
        dist.mkdir()
        (dist / "page-manifest.json").write_text(
            '{"pages": {}, "generated_at": "2024-01-01"}'
        )

        # Isolate os.environ so the worker env we export doesn't leak to other tests.
        monkeypatch.setattr(cli.os, "environ", dict(cli.os.environ))

        def _no_single_process(*_a, **_k):  # pragma: no cover - must not run for N>1
            raise AssertionError("single-process build_production_app should not run")

        monkeypatch.setattr(
            "pyxle.build.production.build_production_app", _no_single_process
        )

        captured: dict = {}

        def fake_run(import_string, **kwargs):
            captured["import_string"] = import_string
            captured.update(kwargs)

        monkeypatch.setattr(cli.uvicorn, "run", fake_run)

        result = runner.invoke(
            app, ["serve", "demo", "--skip-build", "--workers", "4"], catch_exceptions=False
        )

        assert result.exit_code == 0, result.output
        assert captured["import_string"] == "pyxle.build.production:create_app"
        assert captured["factory"] is True
        assert captured["workers"] == 4
        # The event loop must stay at uvicorn's default ("auto" → uvloop):
        # forcing loop="asyncio" causes a ~40-50ms per-request stall on Linux
        # multi-worker (shared listening socket + epoll wakeup behaviour).
        assert captured.get("loop") is None
        # Worker subprocesses rebuild the app from these exported variables.
        assert cli.os.environ.get("PYXLE_SERVE_PROJECT_ROOT", "").endswith("demo")
        assert cli.os.environ.get("PYXLE_SERVE_SERVE_STATIC") == "1"


def test_resolve_global_style_entries_auto_detects_global_css(tmp_path: Path) -> None:
    """styles/global.css is auto-included when no explicit styles are configured."""
    styles_dir = tmp_path / "styles"
    styles_dir.mkdir()
    (styles_dir / "global.css").write_text("body { margin: 0; }")

    config = PyxleConfig()  # no global_styles configured
    result = cli._resolve_global_style_entries(tmp_path, config)

    assert result == ("styles/global.css",)


def test_resolve_global_style_entries_deduplicates(tmp_path: Path) -> None:
    """Duplicate entries in global_styles are removed."""
    config = PyxleConfig(global_styles=(" styles/a.css ", "", "styles/a.css", "styles/b.css"))

    result = cli._resolve_global_style_entries(tmp_path, config)

    assert result == ("styles/a.css", "styles/b.css")


# ---------------------------------------------------------------------------
# pyxle check
# ---------------------------------------------------------------------------


def test_check_command_succeeds_on_valid_project() -> None:
    with runner.isolated_filesystem():
        project = Path("demo")
        (project / "pages").mkdir(parents=True)
        (project / "public").mkdir()
        (project / "pages" / "index.pyxl").write_text(
            "import React from 'react';\n\n"
            "export default function Page() {\n"
            "    return <div>Hello</div>;\n"
            "}\n",
            encoding="utf-8",
        )

        result = runner.invoke(app, ["check", "demo"], catch_exceptions=False)

        assert result.exit_code == 0
        assert "1 .pyxl" in result.stdout
        assert "passed" in result.stdout


def test_check_passes_with_an_unused_import() -> None:
    """An unused import is a warning, not an error.

    `docs/guides/deployment.md` gates the deploy checklist on "`pyxle check`
    passes with no errors" — a leftover `import json` must not block a release.
    """
    with runner.isolated_filesystem():
        project = Path("demo")
        (project / "pages").mkdir(parents=True)
        (project / "public").mkdir()
        (project / "pages" / "index.pyxl").write_text(
            "import json\n\n"
            "import React from 'react';\n\n"
            "export default function Page() {\n"
            "    return <div>Hello</div>;\n"
            "}\n",
            encoding="utf-8",
        )

        result = runner.invoke(app, ["check", "demo"], catch_exceptions=False)

        assert result.exit_code == 0
        assert "'json' imported but unused" in result.stdout
        assert "warning: [python]" in result.stdout
        assert "All checks passed" in result.stdout
        assert "Check failed" not in result.stdout


def test_check_fails_on_an_undefined_name() -> None:
    """Genuine breakage — an unresolved reference — still fails the command."""
    with runner.isolated_filesystem():
        project = Path("demo")
        (project / "pages").mkdir(parents=True)
        (project / "public").mkdir()
        (project / "pages" / "index.pyxl").write_text(
            "@server\n"
            "async def load(request):\n"
            "    return {'x': compute_total(request)}\n\n"
            "import React from 'react';\n\n"
            "export default function Page() {\n"
            "    return <div>Hello</div>;\n"
            "}\n",
            encoding="utf-8",
        )

        result = runner.invoke(app, ["check", "demo"], catch_exceptions=False)

        assert result.exit_code == 1
        assert "undefined name 'compute_total'" in result.stdout
        assert "Check failed with 1 error(s)" in result.stdout


def test_check_command_reports_compilation_error() -> None:
    with runner.isolated_filesystem():
        project = Path("demo")
        (project / "pages").mkdir(parents=True)
        (project / "public").mkdir()
        (project / "pages" / "broken.pyxl").write_text(
            "@server\n"
            "def bad_loader(request):\n"
            "    return {}\n",
            encoding="utf-8",
        )

        result = runner.invoke(app, ["check", "demo"], catch_exceptions=False)

        assert result.exit_code == 1
        assert "must be declared as async" in result.stdout


def test_check_command_warns_missing_node_modules() -> None:
    with runner.isolated_filesystem():
        project = Path("demo")
        (project / "pages").mkdir(parents=True)
        (project / "public").mkdir()

        result = runner.invoke(app, ["check", "demo"], catch_exceptions=False)

        assert result.exit_code == 0
        assert "node_modules" in result.stdout


def test_check_command_fails_on_missing_pages_dir() -> None:
    with runner.isolated_filesystem():
        project = Path("demo")
        project.mkdir()

        result = runner.invoke(app, ["check", "demo"], catch_exceptions=False)

        assert result.exit_code == 1
        assert "pages" in result.stdout


def test_check_command_fails_on_missing_project() -> None:
    result = runner.invoke(app, ["check", "nonexistent_dir_xyz"], catch_exceptions=False)
    assert result.exit_code == 1


def test_check_command_reports_multiple_diagnostics_per_file() -> None:
    """``pyxle check`` runs in tolerant mode so a single page with
    multiple errors reports all of them in one pass."""
    with runner.isolated_filesystem():
        project = Path("demo")
        (project / "pages").mkdir(parents=True)
        (project / "public").mkdir()
        # This file has TWO distinct semantic errors:
        #   1. @server function is not async
        #   2. @action function is not async
        (project / "pages" / "broken.pyxl").write_text(
            "@server\n"
            "def bad_loader(request):\n"
            "    return {}\n"
            "\n"
            "@action\n"
            "def bad_action(request):\n"
            "    return {}\n"
            "\n"
            "export default function P() { return <div />; }\n",
            encoding="utf-8",
        )

        result = runner.invoke(app, ["check", "demo"], catch_exceptions=False)

        assert result.exit_code == 1
        # Both errors are reported.
        out = result.stdout
        assert "must be declared as async" in out
        # Verify we got at least 2 distinct error lines (one per error,
        # not just one per file). The check command writes one
        # diagnostic line per error.
        async_count = out.count("async")
        assert async_count >= 2


def test_check_command_reports_diagnostic_section_and_line() -> None:
    """``pyxle check`` output annotates each diagnostic with its section
    and source line."""
    with runner.isolated_filesystem():
        project = Path("demo")
        (project / "pages").mkdir(parents=True)
        (project / "public").mkdir()
        (project / "pages" / "broken.pyxl").write_text(
            "@server\n"
            "def bad_loader(request):\n"
            "    return {}\n"
            "\n"
            "export default function P() { return <div />; }\n",
            encoding="utf-8",
        )

        result = runner.invoke(app, ["check", "demo"], catch_exceptions=False)

        assert result.exit_code == 1
        # Section name is included in the output.
        assert "[python]" in result.stdout
        # Line number is included.
        assert "line 2" in result.stdout


def test_check_command_reports_jsx_syntax_errors() -> None:
    """``pyxle check`` passes ``validate_jsx=True`` so JSX syntax
    problems (e.g. unclosed tags, mismatched braces) surface as
    ``[jsx]`` diagnostics alongside Python errors. Previously JSX
    errors were silently passed through to the build step.
    """
    with runner.isolated_filesystem():
        project = Path("demo")
        (project / "pages").mkdir(parents=True)
        (project / "public").mkdir()
        # Invalid JSX: const declaration inside a JSX expression is
        # a parse error. Babel rejects this immediately.
        (project / "pages" / "bad-jsx.pyxl").write_text(
            "import React from 'react';\n"
            "\n"
            "export default function Page() {\n"
            "    return <div>{const x = 1}</div>;\n"
            "}\n",
            encoding="utf-8",
        )

        result = runner.invoke(app, ["check", "demo"], catch_exceptions=False)

        # The check should fail with a [jsx] diagnostic. Babel must be
        # available for this path to produce an error; if Node is
        # missing the test environment won't catch the issue and the
        # assertion would fail — that's intentional, it signals that
        # validate_jsx needs Node to work.
        assert result.exit_code == 1
        assert "[jsx]" in result.stdout


def test_check_command_survives_per_file_parser_crash(monkeypatch) -> None:
    """A single file that crashes the parser must NOT abort the
    entire ``pyxle check`` run. The CLI defensively wraps each
    per-file parse so a crash on file A still lets files B and C
    report their diagnostics. The crash is itself reported as a
    ``[python] parser crashed`` diagnostic.
    """
    from pyxle.compiler import parser as parser_module

    real_parse = parser_module.PyxParser.parse
    crash_target = "crashy.pyxl"

    def fake_parse(
        self, source_path, *, tolerant=False, validate_jsx=False, validate_semantics=False
    ):
        if source_path.name == crash_target:
            raise RuntimeError("simulated parser crash")
        return real_parse(
            self,
            source_path,
            tolerant=tolerant,
            validate_jsx=validate_jsx,
            validate_semantics=validate_semantics,
        )

    monkeypatch.setattr(parser_module.PyxParser, "parse", fake_parse)

    with runner.isolated_filesystem():
        project = Path("demo")
        (project / "pages").mkdir(parents=True)
        (project / "public").mkdir()
        # Two files. The first one will crash the parser; the second
        # is valid and must still be checked.
        (project / "pages" / crash_target).write_text(
            "import React from 'react';\n"
            "export default function P() { return <div />; }\n",
            encoding="utf-8",
        )
        (project / "pages" / "ok.pyxl").write_text(
            "import React from 'react';\n"
            "export default function Q() { return <div />; }\n",
            encoding="utf-8",
        )

        result = runner.invoke(app, ["check", "demo"], catch_exceptions=False)

        # Crash is reported as a diagnostic, not propagated.
        assert "parser crashed" in result.stdout
        assert "RuntimeError" in result.stdout
        # The CLI says it checked BOTH files (crash didn't abort
        # iteration).
        assert "Checked 2 .pyxl file(s)" in result.stdout


def test_check_command_warns_missing_package_json() -> None:
    """``pyxle check`` warns when package.json is missing."""
    with runner.isolated_filesystem():
        project = Path("demo")
        (project / "pages").mkdir(parents=True)
        (project / "public").mkdir()
        (project / "node_modules").mkdir()
        (project / "pages" / "index.pyxl").write_text(
            "import React from 'react';\n"
            "export default function P() { return <div />; }\n",
            encoding="utf-8",
        )

        result = runner.invoke(app, ["check", "demo"], catch_exceptions=False)

        assert result.exit_code == 0
        assert "package.json" in result.stdout


def test_check_command_handles_diagnostic_without_line() -> None:
    """``pyxle check`` output handles diagnostics that have no line info."""
    # Some diagnostics carry no source line (e.g. structural errors at
    # the file level). We patch the parser to emit one such diagnostic
    # and verify the CLI formats it without crashing on the missing line.
    from pyxle.compiler.parser import PyxDiagnostic, PyxParseResult

    def fake_parse(
        self, path, *, tolerant=False, validate_jsx=False, validate_semantics=False
    ):
        return PyxParseResult(
            python_code="",
            jsx_code="",
            loader=None,
            python_line_numbers=(),
            jsx_line_numbers=(),
            head_elements=(),
            head_is_dynamic=False,
            diagnostics=(
                PyxDiagnostic(
                    section="structural",
                    severity="error",
                    message="file-level diagnostic without a line",
                    line=None,
                ),
            ),
        )

    with runner.isolated_filesystem():
        project = Path("demo")
        (project / "pages").mkdir(parents=True)
        (project / "public").mkdir()
        (project / "pages" / "page.pyxl").write_text(
            "import React from 'react';\n"
            "export default function P() { return <div />; }\n",
            encoding="utf-8",
        )

        from pyxle.compiler.parser import PyxParser

        original = PyxParser.parse
        PyxParser.parse = fake_parse  # type: ignore[assignment]
        try:
            result = runner.invoke(app, ["check", "demo"], catch_exceptions=False)
        finally:
            PyxParser.parse = original  # type: ignore[assignment]

        assert result.exit_code == 1
        assert "[structural]" in result.stdout
        assert "file-level diagnostic without a line" in result.stdout


# ---------------------------------------------------------------------------
# pyxle routes
# ---------------------------------------------------------------------------


def test_routes_command_shows_page_routes() -> None:
    with runner.isolated_filesystem():
        project = Path("demo")
        (project / "pages").mkdir(parents=True)
        (project / "public").mkdir()
        (project / "pages" / "index.pyxl").write_text(
            "import React from 'react';\n\n"
            "export default function Page() {\n"
            "    return <div>Hello</div>;\n"
            "}\n",
            encoding="utf-8",
        )
        (project / "pages" / "about.pyxl").write_text(
            "import React from 'react';\n\n"
            "export default function About() {\n"
            "    return <div>About</div>;\n"
            "}\n",
            encoding="utf-8",
        )

        result = runner.invoke(app, ["routes", "demo"], catch_exceptions=False)

        assert result.exit_code == 0
        assert "route(s) found" in result.stdout
        # Pin the shape the docs print verbatim. Both `quick-start.md` and
        # `reference/cli.md` reproduce this output as a sample, and both had
        # drifted from it: they showed the section headers unprefixed and
        # hand-indented, and `cli.md` additionally wrote page paths as
        # `pages/index.pyxl` while the command prints them relative to
        # `pages/` — which quick-start's own prose already said. Nothing
        # compared the samples to reality, so asserting only "route(s) found"
        # let every one of those drift silently.
        assert "\u2139\ufe0f    Pages:" in result.stdout
        assert "\u25b6\ufe0f  /about \u2014 about.pyxl" in result.stdout
        assert "pages/about.pyxl" not in result.stdout


def test_routes_command_json_output() -> None:
    with runner.isolated_filesystem():
        project = Path("demo")
        (project / "pages").mkdir(parents=True)
        (project / "public").mkdir()
        (project / "pages" / "index.pyxl").write_text(
            "import React from 'react';\n\n"
            "export default function Page() {\n"
            "    return <div>Hello</div>;\n"
            "}\n",
            encoding="utf-8",
        )

        result = runner.invoke(app, ["routes", "demo", "--json"], catch_exceptions=False)

        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert isinstance(data, list)
        assert any(r["path"] == "/" for r in data)


def test_routes_command_shows_loader_and_api() -> None:
    """Routes command shows pages with loaders and API routes."""
    with runner.isolated_filesystem():
        project = Path("demo")
        (project / "pages").mkdir(parents=True)
        (project / "pages" / "api").mkdir()
        (project / "public").mkdir()
        (project / "pages" / "index.pyxl").write_text(
            "from pyxle.runtime import server\n\n"
            "@server\n"
            "async def loader(request):\n"
            "    return {}\n\n"
            "import React from 'react';\n\n"
            "export default function Page() {\n"
            "    return <div>Home</div>;\n"
            "}\n",
            encoding="utf-8",
        )
        (project / "pages" / "api" / "hello.py").write_text(
            "async def endpoint(request):\n"
            "    return {'ok': True}\n",
            encoding="utf-8",
        )

        result = runner.invoke(app, ["routes", "demo"], catch_exceptions=False)

        assert result.exit_code == 0
        assert "route(s) found" in result.stdout

    # Also test JSON output with loader
    with runner.isolated_filesystem():
        project = Path("demo2")
        (project / "pages").mkdir(parents=True)
        (project / "public").mkdir()
        (project / "pages" / "index.pyxl").write_text(
            "from pyxle.runtime import server\n\n"
            "@server\n"
            "async def loader(request):\n"
            "    return {}\n\n"
            "import React from 'react';\n\n"
            "export default function Page() {\n"
            "    return <div>Home</div>;\n"
            "}\n",
            encoding="utf-8",
        )

        result = runner.invoke(app, ["routes", "demo2", "--json"], catch_exceptions=False)

        assert result.exit_code == 0
        data = json.loads(result.stdout)
        assert data[0]["hasLoader"] is True


def test_check_command_config_error() -> None:
    """Check command reports config errors."""
    with runner.isolated_filesystem():
        project = Path("demo")
        (project / "pages").mkdir(parents=True)
        (project / "public").mkdir()
        (project / "pyxle.config.json").write_text(
            '{"badKey": true}',
            encoding="utf-8",
        )

        result = runner.invoke(app, ["check", "demo"], catch_exceptions=False)

        assert result.exit_code == 1
        assert "Config error" in result.stdout or "badKey" in result.stdout


def test_routes_command_with_actions_and_error_boundary() -> None:
    """Routes command shows actions, dynamic-head, API routes, and error boundaries."""
    with runner.isolated_filesystem():
        project = Path("demo")
        (project / "pages").mkdir(parents=True)
        (project / "pages" / "api").mkdir()
        (project / "public").mkdir()
        # Page with action and dynamic head
        (project / "pages" / "index.pyxl").write_text(
            "from pyxle.runtime import server, action\n\n"
            "HEAD = lambda data: f'<title>{data.get(\"title\", \"Home\")}</title>'\n\n"
            "@server\n"
            "async def loader(request):\n"
            "    return {'title': 'Home'}\n\n"
            "@action\n"
            "async def save(request):\n"
            "    return {'ok': True}\n\n"
            "import React from 'react';\n"
            "export default function Page() { return <div />; }\n",
            encoding="utf-8",
        )
        # Error boundary page
        (project / "pages" / "error.pyxl").write_text(
            "import React from 'react';\n"
            "export default function ErrorPage() { return <div>Error</div>; }\n",
            encoding="utf-8",
        )
        # API route
        (project / "pages" / "api" / "greet.py").write_text(
            "async def endpoint(request):\n    return {'hello': 'world'}\n",
            encoding="utf-8",
        )

        result = runner.invoke(app, ["routes", "demo"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "route(s) found" in result.stdout

        # Also test JSON with all features
        result = runner.invoke(app, ["routes", "demo", "--json"], catch_exceptions=False)
        assert result.exit_code == 0
        data = json.loads(result.stdout)
        page_routes = [r for r in data if r.get("type") != "api"]
        api_routes = [r for r in data if r.get("type") == "api"]
        assert len(page_routes) >= 1
        assert len(api_routes) >= 1


def test_routes_labels_special_files_not_as_paths() -> None:
    """`error.pyxl` / `not-found.pyxl` / `loading.pyxl` serve no URL, so the
    routes table must not print them under a path-shaped label like `/error` —
    that invites the reader to go and visit an address that always 404s."""
    with runner.isolated_filesystem():
        project = Path("demo")
        (project / "pages" / "dashboard").mkdir(parents=True)
        (project / "public").mkdir()
        (project / "pages" / "index.pyxl").write_text(
            "import React from 'react';\nexport default function P() { return <div />; }\n",
            encoding="utf-8",
        )
        for rel, component in (
            ("error.pyxl", "ErrorPage"),
            ("not-found.pyxl", "NotFound"),
            ("loading.pyxl", "Loading"),
            ("dashboard/error.pyxl", "DashError"),
        ):
            (project / "pages" / rel).write_text(
                f"import React from 'react';\n"
                f"export default function {component}() {{ return <div />; }}\n",
                encoding="utf-8",
            )

        result = runner.invoke(app, ["routes", "demo"], catch_exceptions=False)
        assert result.exit_code == 0

        # Named for what they are...
        assert "error boundary" in result.stdout
        assert "404 page" in result.stdout
        assert "loading fallback" in result.stdout
        # ...with the subtree each one covers.
        assert "[covers /]" in result.stdout
        assert "[covers /dashboard/*]" in result.stdout
        # ...and never as a visitable URL.
        for fake_path in ("▶️  /error", "▶️  /not-found", "▶️  /loading"):
            assert fake_path not in result.stdout
        # The routable pages are still counted; the special files are not.
        assert "1 route(s) found" in result.stdout


def test_routes_command_config_error() -> None:
    """Routes command reports config errors."""
    with runner.isolated_filesystem():
        project = Path("demo")
        (project / "pages").mkdir(parents=True)
        (project / "public").mkdir()
        (project / "pyxle.config.json").write_text('{"unknown": 1}', encoding="utf-8")

        result = runner.invoke(app, ["routes", "demo"], catch_exceptions=False)
        assert result.exit_code == 1


def test_resolve_dist_directory_absolute(tmp_path: Path) -> None:
    """_resolve_dist_directory handles absolute and relative paths."""
    result = cli._resolve_dist_directory(tmp_path, None)
    assert result == (tmp_path / "dist").resolve()

    relative = Path("output")
    result = cli._resolve_dist_directory(tmp_path, relative)
    assert result == (tmp_path / "output").resolve()

    absolute = tmp_path / "abs_output"
    result = cli._resolve_dist_directory(tmp_path, absolute)
    assert result == absolute.resolve()


def test_check_command_quiet_suppresses_info() -> None:
    """--quiet flag suppresses info messages but still shows errors."""
    with runner.isolated_filesystem():
        project = Path("demo")
        (project / "pages").mkdir(parents=True)
        (project / "public").mkdir()
        (project / "pages" / "index.pyxl").write_text(
            "import React from 'react';\n"
            "export default function Page() { return <div />; }\n",
            encoding="utf-8",
        )

        result = runner.invoke(app, ["--quiet", "check", "demo"], catch_exceptions=False)

        assert result.exit_code == 0
        # In quiet mode, "Checked N .pyxl" info line should be suppressed
        assert "Checked" not in result.stdout


def test_routes_command_fails_on_missing_project() -> None:
    result = runner.invoke(app, ["routes", "nonexistent_dir_xyz"], catch_exceptions=False)
    assert result.exit_code == 1


# --- typecheck command tests ---


def test_typecheck_command_fails_on_missing_project() -> None:
    result = runner.invoke(app, ["typecheck", "nonexistent_dir_xyz"], catch_exceptions=False)
    assert result.exit_code == 1


def test_typecheck_command_fails_when_tsc_not_found(monkeypatch) -> None:
    with runner.isolated_filesystem():
        project = Path("demo")
        (project / "pages").mkdir(parents=True)
        (project / "public").mkdir()
        (project / "pages" / "index.pyxl").write_text(
            "import React from 'react';\n"
            "export default function Page() { return <div />; }\n",
            encoding="utf-8",
        )

        monkeypatch.setattr("pyxle.cli._find_tsc", lambda root: None)

        result = runner.invoke(app, ["typecheck", "demo"], catch_exceptions=False)
        assert result.exit_code == 1
        assert "tsc" in result.stdout.lower() or "TypeScript" in result.stdout


def test_typecheck_missing_typescript_errors_without_invoking_tsc(monkeypatch) -> None:
    """Without typescript (no node_modules/typescript, not in package.json),
    typecheck must emit ONE actionable error and never spawn a compiler —
    previously the `npx --yes tsc` fallback relayed npm's placeholder banner."""
    with runner.isolated_filesystem():
        project = Path("demo")
        (project / "pages").mkdir(parents=True)
        (project / "public").mkdir()
        (project / "pages" / "index.pyxl").write_text(
            "import React from 'react';\n"
            "export default function Page() { return <div />; }\n",
            encoding="utf-8",
        )
        (project / "package.json").write_text(
            json.dumps({"name": "demo", "devDependencies": {"vite": "^6.3.5"}}),
            encoding="utf-8",
        )

        # No local node_modules and nothing on PATH → tsc genuinely absent.
        monkeypatch.setattr("shutil.which", lambda cmd: None)

        def forbid_run(*args, **kwargs):
            raise AssertionError("typecheck must not spawn a compiler when typescript is missing")

        monkeypatch.setattr("pyxle.cli.subprocess.run", forbid_run)

        result = runner.invoke(app, ["typecheck", "demo"], catch_exceptions=False)
        assert result.exit_code == 1
        assert "TypeScript is required for 'pyxle typecheck'" in result.stdout
        assert "npm install --save-dev typescript" in result.stdout
        assert "0 error(s)" not in result.stdout


def test_typecheck_typescript_declared_but_not_installed(monkeypatch) -> None:
    """typescript listed in package.json but node_modules missing → point at
    `npm install`, not at adding the dependency again."""
    with runner.isolated_filesystem():
        project = Path("demo")
        (project / "pages").mkdir(parents=True)
        (project / "public").mkdir()
        (project / "pages" / "index.pyxl").write_text(
            "import React from 'react';\n"
            "export default function Page() { return <div />; }\n",
            encoding="utf-8",
        )
        (project / "package.json").write_text(
            json.dumps({"name": "demo", "devDependencies": {"typescript": "^5.5.0"}}),
            encoding="utf-8",
        )

        monkeypatch.setattr("shutil.which", lambda cmd: None)

        result = runner.invoke(app, ["typecheck", "demo"], catch_exceptions=False)
        assert result.exit_code == 1
        assert "declared in package.json but not installed" in result.stdout
        assert "npm install" in result.stdout
        assert "--save-dev" not in result.stdout


def test_typecheck_failed_run_never_reports_zero_errors(monkeypatch) -> None:
    """A non-zero tsc exit with no `error TS` diagnostics (crash, wrong binary)
    must not produce the self-contradictory 'failed with 0 error(s)' summary."""
    with runner.isolated_filesystem():
        project = Path("demo")
        (project / "pages").mkdir(parents=True)
        (project / "public").mkdir()
        (project / "pages" / "index.pyxl").write_text(
            "import React from 'react';\n"
            "export default function Page() { return <div />; }\n",
            encoding="utf-8",
        )

        monkeypatch.setattr("pyxle.cli._find_tsc", lambda root: ["tsc"])

        fake_result = subprocess.CompletedProcess(
            args=["tsc", "--noEmit"],
            returncode=1,
            stdout="This is not the tsc command you are looking for\n",
            stderr="",
        )
        monkeypatch.setattr("pyxle.cli.subprocess.run", lambda *a, **kw: fake_result)

        result = runner.invoke(app, ["typecheck", "demo"], catch_exceptions=False)
        assert result.exit_code == 1
        assert "0 error(s)" not in result.stdout
        assert "exited with code 1" in result.stdout


def test_find_tsc_has_no_npx_fallback(tmp_path: Path, monkeypatch) -> None:
    """`npx --yes tsc` resolves npm's placeholder `tsc` package, never the real
    compiler — _find_tsc must not fall back to it."""
    from pyxle.cli import _find_tsc

    monkeypatch.setattr(
        "shutil.which", lambda cmd: "/usr/bin/npx" if cmd == "npx" else None
    )

    assert _find_tsc(tmp_path) is None


def test_typescript_declared_reads_package_json(tmp_path: Path) -> None:
    from pyxle.cli import _typescript_declared

    # No package.json at all.
    assert _typescript_declared(tmp_path) is False

    package_json = tmp_path / "package.json"
    package_json.write_text(json.dumps({"devDependencies": {"vite": "^6.3.5"}}))
    assert _typescript_declared(tmp_path) is False

    package_json.write_text(json.dumps({"devDependencies": {"typescript": "^5.5.0"}}))
    assert _typescript_declared(tmp_path) is True

    package_json.write_text(json.dumps({"dependencies": {"typescript": "^5.5.0"}}))
    assert _typescript_declared(tmp_path) is True

    package_json.write_text("{not json")
    assert _typescript_declared(tmp_path) is False

    package_json.write_text(json.dumps(["not", "a", "dict"]))
    assert _typescript_declared(tmp_path) is False


def test_typecheck_command_succeeds_on_clean_output(monkeypatch) -> None:
    with runner.isolated_filesystem():
        project = Path("demo")
        (project / "pages").mkdir(parents=True)
        (project / "public").mkdir()
        (project / "pages" / "index.pyxl").write_text(
            "import React from 'react';\n"
            "export default function Page() { return <div />; }\n",
            encoding="utf-8",
        )

        monkeypatch.setattr("pyxle.cli._find_tsc", lambda root: ["tsc"])

        fake_result = subprocess.CompletedProcess(
            args=["tsc", "--noEmit"],
            returncode=0,
            stdout="",
            stderr="",
        )
        monkeypatch.setattr("pyxle.cli.subprocess.run", lambda *a, **kw: fake_result)

        result = runner.invoke(app, ["typecheck", "demo"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "passed" in result.stdout.lower()


def test_typecheck_command_reports_errors(monkeypatch) -> None:
    with runner.isolated_filesystem():
        project = Path("demo")
        (project / "pages").mkdir(parents=True)
        (project / "public").mkdir()
        (project / "pages" / "index.pyxl").write_text(
            "import React from 'react';\n"
            "export default function Page() { return <div />; }\n",
            encoding="utf-8",
        )

        monkeypatch.setattr("pyxle.cli._find_tsc", lambda root: ["tsc"])

        fake_result = subprocess.CompletedProcess(
            args=["tsc", "--noEmit"],
            returncode=2,
            stdout="pages/index.jsx(3,10): error TS2304: Cannot find name 'foo'.\n"
                   "pages/about.jsx(7,1): error TS2304: Cannot find name 'bar'.\n",
            stderr="",
        )
        monkeypatch.setattr("pyxle.cli.subprocess.run", lambda *a, **kw: fake_result)

        result = runner.invoke(app, ["typecheck", "demo"], catch_exceptions=False)
        assert result.exit_code == 1
        assert "2 error(s)" in result.stdout


def test_typecheck_command_config_error() -> None:
    with runner.isolated_filesystem():
        project = Path("demo")
        (project / "pages").mkdir(parents=True)
        (project / "public").mkdir()
        (project / "pyxle.config.json").write_text("{bad json", encoding="utf-8")

        result = runner.invoke(app, ["typecheck", "demo"], catch_exceptions=False)
        assert result.exit_code == 1


def test_typecheck_command_handles_subprocess_file_not_found(monkeypatch) -> None:
    with runner.isolated_filesystem():
        project = Path("demo")
        (project / "pages").mkdir(parents=True)
        (project / "public").mkdir()
        (project / "pages" / "index.pyxl").write_text(
            "import React from 'react';\n"
            "export default function Page() { return <div />; }\n",
            encoding="utf-8",
        )

        monkeypatch.setattr("pyxle.cli._find_tsc", lambda root: ["fake-tsc"])

        def raise_fnf(*a, **kw):
            raise FileNotFoundError("fake-tsc not found")

        monkeypatch.setattr("pyxle.cli.subprocess.run", raise_fnf)

        result = runner.invoke(app, ["typecheck", "demo"], catch_exceptions=False)
        assert result.exit_code == 1
        assert "execute" in result.stdout.lower() or "TypeScript" in result.stdout


def test_typecheck_command_handles_timeout(monkeypatch) -> None:
    with runner.isolated_filesystem():
        project = Path("demo")
        (project / "pages").mkdir(parents=True)
        (project / "public").mkdir()
        (project / "pages" / "index.pyxl").write_text(
            "import React from 'react';\n"
            "export default function Page() { return <div />; }\n",
            encoding="utf-8",
        )

        monkeypatch.setattr("pyxle.cli._find_tsc", lambda root: ["tsc"])

        def raise_timeout(*a, **kw):
            raise subprocess.TimeoutExpired("tsc", 120)

        monkeypatch.setattr("pyxle.cli.subprocess.run", raise_timeout)

        result = runner.invoke(app, ["typecheck", "demo"], catch_exceptions=False)
        assert result.exit_code == 1
        assert "timed out" in result.stdout.lower()


def test_typecheck_command_shows_stderr_warnings(monkeypatch) -> None:
    with runner.isolated_filesystem():
        project = Path("demo")
        (project / "pages").mkdir(parents=True)
        (project / "public").mkdir()
        (project / "pages" / "index.pyxl").write_text(
            "import React from 'react';\n"
            "export default function Page() { return <div />; }\n",
            encoding="utf-8",
        )

        monkeypatch.setattr("pyxle.cli._find_tsc", lambda root: ["tsc"])

        fake_result = subprocess.CompletedProcess(
            args=["tsc", "--noEmit"],
            returncode=0,
            stdout="",
            stderr="Warning: some deprecation notice\n",
        )
        monkeypatch.setattr("pyxle.cli.subprocess.run", lambda *a, **kw: fake_result)

        result = runner.invoke(app, ["typecheck", "demo"], catch_exceptions=False)
        assert result.exit_code == 0
        assert "deprecation" in result.stdout.lower()


def test_find_tsc_local_bin(tmp_path: Path) -> None:
    from pyxle.cli import _find_tsc

    bin_dir = tmp_path / "node_modules" / ".bin"
    bin_dir.mkdir(parents=True)
    tsc_bin = bin_dir / "tsc"
    tsc_bin.write_text("#!/bin/sh")
    tsc_bin.chmod(0o755)

    result = _find_tsc(tmp_path)
    assert result is not None
    assert result == [str(tsc_bin)]


def test_find_tsc_returns_none_when_nothing_available(tmp_path: Path, monkeypatch) -> None:
    from pyxle.cli import _find_tsc

    monkeypatch.setattr("shutil.which", lambda cmd: None)

    result = _find_tsc(tmp_path)
    assert result is None


def test_emit_tsc_diagnostic_parses_structured_error() -> None:
    from pyxle.cli import _emit_tsc_diagnostic

    captured: list[str] = []

    def capture(message: str, *, fg: str | None = None, bold: bool = False) -> None:
        captured.append(message)

    from pyxle.cli.logger import ConsoleLogger

    logger = ConsoleLogger(secho=capture)
    _emit_tsc_diagnostic(logger, "pages/index.jsx(3,10): error TS2304: Cannot find name 'foo'.")

    assert len(captured) == 2
    assert "TS2304" in captured[0]
    assert "--> pages/index.jsx:3:10" in captured[1]


def test_emit_tsc_diagnostic_falls_back_for_unparseable_line() -> None:
    from pyxle.cli import _emit_tsc_diagnostic

    captured: list[str] = []

    def capture(message: str, *, fg: str | None = None, bold: bool = False) -> None:
        captured.append(message)

    from pyxle.cli.logger import ConsoleLogger

    logger = ConsoleLogger(secho=capture)
    _emit_tsc_diagnostic(logger, "Some random output line")

    assert len(captured) == 1
    assert "Some random output line" in captured[0]


# ---------------------------------------------------------------------------
# version callback / --version (lines 52-53)
# ---------------------------------------------------------------------------


def test_version_callback_true_echoes_and_exits(capsys) -> None:
    """version_callback(True) prints the version then raises typer.Exit."""
    with pytest.raises(typer.Exit):
        version_callback(True)

    captured = capsys.readouterr()
    assert __version__ in captured.out


def test_version_callback_false_returns_none() -> None:
    """version_callback(False) is a no-op and returns None."""
    assert version_callback(False) is None


def test_cli_version_flag_displays_version_and_exits() -> None:
    """The global --version flag prints the version and exits 0."""
    result = runner.invoke(app, ["--version"], catch_exceptions=False)
    assert result.exit_code == 0
    assert __version__ in result.stdout


# ---------------------------------------------------------------------------
# dev: lazy import of create_starlette_app (lines 324-328)
# ---------------------------------------------------------------------------


def test_dev_command_lazy_imports_create_starlette_app(monkeypatch) -> None:
    """When create_starlette_app is unset, ``dev`` lazily imports it."""
    with runner.isolated_filesystem():
        project = Path("demo")
        (project / "pages").mkdir(parents=True)
        (project / "public").mkdir(parents=True)

        # Force the lazy-import guard at lines 322-328 to execute by
        # resetting the cached module-level reference to None.
        monkeypatch.setattr("pyxle.cli.create_starlette_app", None)

        captured: dict[str, object] = {}

        class StubDevServer:
            def __init__(self, settings, logger, **kwargs):
                captured["settings"] = settings

            async def start(self) -> None:
                captured["started"] = True

        monkeypatch.setattr("pyxle.cli.DevServer", StubDevServer)
        monkeypatch.setattr("pyxle.cli.asyncio.run", lambda coro: coro.close())

        result = runner.invoke(app, ["dev", "demo"], catch_exceptions=False)

        assert result.exit_code == 0, result.stdout
        # The guard imported the real symbol back into the module global.
        assert cli.create_starlette_app is not None


# ---------------------------------------------------------------------------
# dev: global style/script config error (lines 379-381)
# ---------------------------------------------------------------------------


def test_dev_command_global_style_config_error_exits(monkeypatch) -> None:
    """A GlobalStyleConfigError from settings construction exits with 1."""
    with runner.isolated_filesystem():
        project = Path("demo")
        (project / "pages").mkdir(parents=True)
        (project / "public").mkdir(parents=True)

        from pyxle.devserver.styles import GlobalStyleConfigError

        def boom(*_a, **_kw):
            raise GlobalStyleConfigError("global stylesheet 'missing.css' not found")

        monkeypatch.setattr("pyxle.cli.DevServerSettings.from_project_root", boom)

        result = runner.invoke(app, ["dev", "demo"], catch_exceptions=False)

        assert result.exit_code == 1
        assert "global stylesheet 'missing.css' not found" in result.stdout


def test_dev_command_global_script_config_error_exits(monkeypatch) -> None:
    """A GlobalScriptConfigError from settings construction exits with 1."""
    with runner.isolated_filesystem():
        project = Path("demo")
        (project / "pages").mkdir(parents=True)
        (project / "public").mkdir(parents=True)

        from pyxle.devserver.scripts import GlobalScriptConfigError

        def boom(*_a, **_kw):
            raise GlobalScriptConfigError("global script 'track.js' not found")

        monkeypatch.setattr("pyxle.cli.DevServerSettings.from_project_root", boom)

        result = runner.invoke(app, ["dev", "demo"], catch_exceptions=False)

        assert result.exit_code == 1
        assert "global script 'track.js' not found" in result.stdout


# ---------------------------------------------------------------------------
# build: lazy import of DevServerSettings (lines 431-433)
# ---------------------------------------------------------------------------


def test_build_command_lazy_imports_devserver_settings(monkeypatch) -> None:
    """When DevServerSettings is unset, ``build`` lazily imports it."""
    with runner.isolated_filesystem():
        project = Path("demo")
        (project / "pages").mkdir(parents=True)
        (project / "public").mkdir(parents=True)

        # Reset the cached reference so the guard at 429-433 runs.
        monkeypatch.setattr("pyxle.cli.DevServerSettings", None)

        def fake_run_build(settings, *, logger, dist_dir=None, force_rebuild=True):
            from pyxle.build.pipeline import BuildResult
            from pyxle.devserver.builder import BuildSummary
            from pyxle.devserver.registry import MetadataRegistry

            result_dist = settings.project_root / "dist"
            (result_dist / "client").mkdir(parents=True, exist_ok=True)
            (result_dist / "server").mkdir(parents=True, exist_ok=True)
            (result_dist / "metadata").mkdir(parents=True, exist_ok=True)
            (result_dist / "public").mkdir(parents=True, exist_ok=True)
            client_manifest_path = result_dist / "client" / "manifest.json"
            client_manifest_path.write_text("{}", encoding="utf-8")
            page_manifest_path = result_dist / "page-manifest.json"
            page_manifest_path.write_text("{}", encoding="utf-8")
            return BuildResult(
                dist_dir=result_dist,
                client_dir=result_dist / "client",
                server_dir=result_dist / "server",
                metadata_dir=result_dist / "metadata",
                public_dir=result_dist / "public",
                client_manifest_path=client_manifest_path,
                page_manifest={},
                page_manifest_path=page_manifest_path,
                summary=BuildSummary(),
                registry=MetadataRegistry(pages=[], apis=[]),
            )

        monkeypatch.setattr("pyxle.cli.run_build", fake_run_build)

        result = runner.invoke(app, ["build", "demo"], catch_exceptions=False)

        assert result.exit_code == 0, result.stdout
        assert cli.DevServerSettings is not None


# ---------------------------------------------------------------------------
# build: config error (lines 451-453)
# ---------------------------------------------------------------------------


def test_build_command_reports_config_error() -> None:
    """An invalid config makes ``build`` exit with code 1."""
    with runner.isolated_filesystem():
        project = Path("demo")
        (project / "pages").mkdir(parents=True)
        (project / "public").mkdir(parents=True)
        (project / "pyxle.config.json").write_text("[]", encoding="utf-8")

        result = runner.invoke(app, ["build", "demo"], catch_exceptions=False)

        assert result.exit_code == 1
        assert "Configuration file" in result.stdout


# ---------------------------------------------------------------------------
# build: global style/script config error (lines 466-468)
# ---------------------------------------------------------------------------


def test_build_command_global_style_config_error_exits(monkeypatch) -> None:
    """A GlobalStyleConfigError during settings construction exits build."""
    with runner.isolated_filesystem():
        project = Path("demo")
        (project / "pages").mkdir(parents=True)
        (project / "public").mkdir(parents=True)

        from pyxle.devserver.styles import GlobalStyleConfigError

        def boom(*_a, **_kw):
            raise GlobalStyleConfigError("bad stylesheet reference")

        monkeypatch.setattr("pyxle.cli.DevServerSettings.from_project_root", boom)

        result = runner.invoke(app, ["build", "demo"], catch_exceptions=False)

        assert result.exit_code == 1
        assert "bad stylesheet reference" in result.stdout


# ---------------------------------------------------------------------------
# build: absolute --out-dir (line 476)
# ---------------------------------------------------------------------------


def test_build_command_accepts_absolute_out_dir(monkeypatch, tmp_path: Path) -> None:
    """An absolute --out-dir is resolved as-is (not joined to project root)."""
    with runner.isolated_filesystem():
        project = Path("demo")
        (project / "pages").mkdir(parents=True)
        (project / "public").mkdir(parents=True)

        absolute_out = tmp_path / "abs-dist"
        captured: dict[str, object] = {}

        def fake_run_build(settings, *, logger, dist_dir=None, force_rebuild=True):
            captured["dist_dir"] = dist_dir
            from pyxle.build.pipeline import BuildResult
            from pyxle.devserver.builder import BuildSummary
            from pyxle.devserver.registry import MetadataRegistry

            result_dist = dist_dir
            (result_dist / "client").mkdir(parents=True, exist_ok=True)
            (result_dist / "server").mkdir(parents=True, exist_ok=True)
            (result_dist / "metadata").mkdir(parents=True, exist_ok=True)
            (result_dist / "public").mkdir(parents=True, exist_ok=True)
            client_manifest_path = result_dist / "client" / "manifest.json"
            client_manifest_path.write_text("{}", encoding="utf-8")
            page_manifest_path = result_dist / "page-manifest.json"
            page_manifest_path.write_text("{}", encoding="utf-8")
            return BuildResult(
                dist_dir=result_dist,
                client_dir=result_dist / "client",
                server_dir=result_dist / "server",
                metadata_dir=result_dist / "metadata",
                public_dir=result_dist / "public",
                client_manifest_path=client_manifest_path,
                page_manifest={},
                page_manifest_path=page_manifest_path,
                summary=BuildSummary(),
                registry=MetadataRegistry(pages=[], apis=[]),
            )

        monkeypatch.setattr("pyxle.cli.run_build", fake_run_build)

        result = runner.invoke(
            app,
            ["build", "demo", "--out-dir", str(absolute_out)],
            catch_exceptions=False,
        )

        assert result.exit_code == 0, result.stdout
        assert captured["dist_dir"] == absolute_out.resolve()


# ---------------------------------------------------------------------------
# build: client/page manifest paths absent (branches 504->506, 506->508)
# ---------------------------------------------------------------------------


def test_build_command_skips_manifest_steps_when_paths_absent(monkeypatch) -> None:
    """When the build result has no client/page manifest paths, the
    corresponding step lines are skipped (branches 504->506 and 506->508)."""
    with runner.isolated_filesystem():
        project = Path("demo")
        (project / "pages").mkdir(parents=True)
        (project / "public").mkdir(parents=True)

        def fake_run_build(settings, *, logger, dist_dir=None, force_rebuild=True):
            from pyxle.build.pipeline import BuildResult
            from pyxle.devserver.builder import BuildSummary
            from pyxle.devserver.registry import MetadataRegistry

            result_dist = settings.project_root / "dist"
            (result_dist / "client").mkdir(parents=True, exist_ok=True)
            (result_dist / "server").mkdir(parents=True, exist_ok=True)
            (result_dist / "metadata").mkdir(parents=True, exist_ok=True)
            (result_dist / "public").mkdir(parents=True, exist_ok=True)
            return BuildResult(
                dist_dir=result_dist,
                client_dir=result_dist / "client",
                server_dir=result_dist / "server",
                metadata_dir=result_dist / "metadata",
                public_dir=result_dist / "public",
                client_manifest_path=None,
                page_manifest={},
                page_manifest_path=None,
                summary=BuildSummary(),
                registry=MetadataRegistry(pages=[], apis=[]),
            )

        monkeypatch.setattr("pyxle.cli.run_build", fake_run_build)

        result = runner.invoke(app, ["build", "demo"], catch_exceptions=False)

        assert result.exit_code == 0, result.stdout
        assert "Build completed" in result.stdout
        # Neither manifest step is emitted when its path is None.
        assert "Client manifest" not in result.stdout
        assert "Page manifest" not in result.stdout
        # The unconditional steps are still emitted.
        assert "Server modules" in result.stdout
        assert "Artifacts" in result.stdout


# ---------------------------------------------------------------------------
# serve: config error (lines 594-596)
# ---------------------------------------------------------------------------


def test_serve_command_reports_config_error() -> None:
    """An invalid config makes ``serve`` exit with code 1."""
    with runner.isolated_filesystem():
        project = Path("demo")
        project.mkdir()
        (project / "pyxle.config.json").write_text("[]", encoding="utf-8")

        result = runner.invoke(app, ["serve", "demo"], catch_exceptions=False)

        assert result.exit_code == 1
        assert "Configuration file" in result.stdout


# ---------------------------------------------------------------------------
# serve: global style/script config error (lines 618-620)
# ---------------------------------------------------------------------------


def test_serve_command_global_script_config_error_exits(monkeypatch) -> None:
    """A GlobalScriptConfigError during settings construction exits serve."""
    with runner.isolated_filesystem():
        project = Path("demo")
        project.mkdir()

        from pyxle.devserver.scripts import GlobalScriptConfigError

        def boom(*_a, **_kw):
            raise GlobalScriptConfigError("missing global script entry")

        monkeypatch.setattr("pyxle.cli.DevServerSettings.from_project_root", boom)

        result = runner.invoke(app, ["serve", "demo"], catch_exceptions=False)

        assert result.exit_code == 1
        assert "missing global script entry" in result.stdout


# ---------------------------------------------------------------------------
# serve: static fallback warnings + negative ssr_workers
# (lines 663-667, 673-676, 689-691, branch 692->701)
# ---------------------------------------------------------------------------


def _serve_with_stubbed_runtime(monkeypatch, captured: dict) -> None:
    """Stub create_starlette_app + uvicorn so ``serve`` doesn't really run."""

    def fake_create_app(settings, route_table, **kwargs):
        captured["settings"] = settings
        captured["create_kwargs"] = kwargs
        from starlette.applications import Starlette

        app_obj = Starlette()
        app_obj.state.pyxle_ready = False
        return app_obj

    monkeypatch.setattr("pyxle.build.production.create_starlette_app", fake_create_app)
    monkeypatch.setattr("pyxle.build.production.build_metadata_registry", lambda s, metadata=None: {})
    monkeypatch.setattr("pyxle.build.production.build_route_table", lambda r: [])
    monkeypatch.setattr("pyxle.build.production.load_manifest", lambda p: {"pages": {}})

    async def _noop_serve(self):
        return None

    monkeypatch.setattr(
        cli.uvicorn,
        "Server",
        lambda cfg: type("S", (), {"serve": _noop_serve})(),
    )


def test_serve_command_falls_back_when_dist_public_and_client_missing(monkeypatch) -> None:
    """With --skip-build and a dist that lacks public/ and client/, serve
    warns and falls back to the source public dir while disabling /client."""
    with runner.isolated_filesystem():
        project = Path("demo")
        (project / "pages").mkdir(parents=True)
        (project / "public").mkdir(parents=True)
        dist = project / "dist"
        dist.mkdir()
        (dist / "page-manifest.json").write_text('{"pages": {}}', encoding="utf-8")
        # Intentionally no dist/public and no dist/client.

        captured: dict[str, object] = {}
        _serve_with_stubbed_runtime(monkeypatch, captured)
        monkeypatch.setattr("pyxle.cli.asyncio.run", lambda coro: coro.close())

        result = runner.invoke(
            app, ["serve", "demo", "--skip-build"], catch_exceptions=False
        )

        assert result.exit_code == 0, result.stdout
        assert "Public assets directory" in result.stdout
        assert "does not exist" in result.stdout
        assert "Client bundle directory" in result.stdout
        assert "/client requests will 404" in result.stdout
        # Fallback: public served from source dir, client disabled.
        kwargs = captured["create_kwargs"]
        assert kwargs["public_static_dir"] == (project / "public").resolve()
        assert kwargs["client_static_dir"] is None


def test_serve_command_zero_ssr_workers_uses_cpu_count(monkeypatch) -> None:
    """--ssr-workers 0 derives the pool size from the CPU count and builds
    a worker pool (lines 689-691)."""
    with runner.isolated_filesystem():
        project = Path("demo")
        (project / "pages").mkdir(parents=True)
        (project / "public").mkdir(parents=True)
        dist = project / "dist"
        dist.mkdir()
        (dist / "page-manifest.json").write_text('{"pages": {}}', encoding="utf-8")
        (dist / "public").mkdir()
        (dist / "client").mkdir()

        pool_args: dict[str, object] = {}

        class StubPool:
            def __init__(self, *, size, project_root, client_root, **kwargs):
                pool_args["size"] = size
                pool_args.update(kwargs)

        monkeypatch.setattr("pyxle.ssr.worker_pool.SsrWorkerPool", StubPool)

        captured: dict[str, object] = {}
        _serve_with_stubbed_runtime(monkeypatch, captured)
        monkeypatch.setattr("pyxle.cli.asyncio.run", lambda coro: coro.close())

        result = runner.invoke(
            app,
            ["serve", "demo", "--skip-build", "--ssr-workers", "0"],
            catch_exceptions=False,
        )

        assert result.exit_code == 0, result.stdout
        # cpu_count fallback yields min(cpu_count or 2, 4) -> between 2 and 4.
        assert 2 <= pool_args["size"] <= 4
        assert captured["create_kwargs"]["pool"] is not None
        # The derived pool size is echoed in the serving banner.
        assert f"ssr_workers: {pool_args['size']}" in result.stdout


def test_serve_command_non_positive_worker_count_skips_pool(monkeypatch) -> None:
    """A non-positive ssr_workers count on the resolved settings means no
    SSR pool is created (branch 692->701) and pool=None is passed through.

    ``DevServerSettings.from_project_root`` clamps negatives to 0 (which
    would then trigger the cpu_count fallback), so we instead force the
    value on the settings object that ``serve`` produces via its internal
    ``dataclasses.replace`` call at the manifest-merge step. This models a
    defensive guard: if the effective worker count is ever non-positive,
    no SSR pool is spun up.
    """
    from dataclasses import replace as _real_replace

    with runner.isolated_filesystem():
        project = Path("demo")
        (project / "pages").mkdir(parents=True)
        (project / "public").mkdir(parents=True)
        dist = project / "dist"
        dist.mkdir()
        (dist / "page-manifest.json").write_text('{"pages": {}}', encoding="utf-8")
        (dist / "public").mkdir()
        (dist / "client").mkdir()

        def replace_with_negative_workers(obj, **changes):
            changes.setdefault("ssr_workers", -1)
            return _real_replace(obj, **changes)

        # serve() rebuilds settings via replace() right after loading the
        # manifest; inject a non-positive worker count there.
        monkeypatch.setattr("pyxle.build.production.replace", replace_with_negative_workers)

        # A non-positive size must never reach the pool constructor.
        def explode(*_a, **_kw):
            raise AssertionError("SsrWorkerPool must not be built for ssr_workers<=0")

        monkeypatch.setattr("pyxle.ssr.worker_pool.SsrWorkerPool", explode)

        captured: dict[str, object] = {}
        _serve_with_stubbed_runtime(monkeypatch, captured)
        monkeypatch.setattr("pyxle.cli.asyncio.run", lambda coro: coro.close())

        result = runner.invoke(
            app, ["serve", "demo", "--skip-build"], catch_exceptions=False
        )

        assert result.exit_code == 0, result.stdout
        assert captured["create_kwargs"]["pool"] is None
        assert "ssr_workers: -1" in result.stdout


# ---------------------------------------------------------------------------
# typecheck: lazy import + settings/build failures + missing tsconfig
# (lines 868-870, 883-885, 892-894, 899-900)
# ---------------------------------------------------------------------------


def test_typecheck_command_lazy_imports_devserver_settings(monkeypatch) -> None:
    """``typecheck`` lazily imports DevServerSettings when it is unset."""
    with runner.isolated_filesystem():
        project = Path("demo")
        (project / "pages").mkdir(parents=True)
        (project / "public").mkdir(parents=True)
        (project / "pages" / "index.pyxl").write_text(
            "import React from 'react';\n"
            "export default function P() { return <div />; }\n",
            encoding="utf-8",
        )

        monkeypatch.setattr("pyxle.cli.DevServerSettings", None)
        # Get past the up-front tsc resolution, then stop at the missing
        # tsconfig after the lazy import + build — enough to exercise the
        # lazy-import branch.
        monkeypatch.setattr("pyxle.cli._find_tsc", lambda root: ["tsc"])
        monkeypatch.setattr("pyxle.devserver.builder.build_once", lambda s: None)

        result = runner.invoke(app, ["typecheck", "demo"], catch_exceptions=False)

        assert result.exit_code == 1
        assert cli.DevServerSettings is not None
        assert "tsconfig.json not found" in result.stdout


def test_typecheck_command_settings_failure_exits(monkeypatch) -> None:
    """A failure constructing settings makes ``typecheck`` exit with 1."""
    with runner.isolated_filesystem():
        project = Path("demo")
        (project / "pages").mkdir(parents=True)
        (project / "public").mkdir(parents=True)

        def boom(*_a, **_kw):
            raise ValueError("settings exploded")

        monkeypatch.setattr("pyxle.cli.DevServerSettings.from_project_root", boom)

        result = runner.invoke(app, ["typecheck", "demo"], catch_exceptions=False)

        assert result.exit_code == 1
        assert "Failed to create settings" in result.stdout
        assert "settings exploded" in result.stdout


def test_typecheck_command_build_failure_exits(monkeypatch) -> None:
    """A failure during the pre-typecheck build exits with code 1."""
    with runner.isolated_filesystem():
        project = Path("demo")
        (project / "pages").mkdir(parents=True)
        (project / "public").mkdir(parents=True)

        def boom(_settings):
            raise RuntimeError("build blew up")

        monkeypatch.setattr("pyxle.cli._find_tsc", lambda root: ["tsc"])
        monkeypatch.setattr("pyxle.devserver.builder.build_once", boom)

        result = runner.invoke(app, ["typecheck", "demo"], catch_exceptions=False)

        assert result.exit_code == 1
        assert "Build failed" in result.stdout
        assert "build blew up" in result.stdout


def test_typecheck_command_errors_when_tsconfig_missing(monkeypatch) -> None:
    """``typecheck`` aborts when the generated tsconfig.json is absent."""
    with runner.isolated_filesystem():
        project = Path("demo")
        (project / "pages").mkdir(parents=True)
        (project / "public").mkdir(parents=True)

        # tsc resolves up front; build_once is stubbed so no real
        # tsconfig.json is produced and the missing-tsconfig branch fires.
        monkeypatch.setattr("pyxle.cli._find_tsc", lambda root: ["tsc"])
        monkeypatch.setattr("pyxle.devserver.builder.build_once", lambda s: None)

        result = runner.invoke(app, ["typecheck", "demo"], catch_exceptions=False)

        assert result.exit_code == 1
        assert "tsconfig.json not found" in result.stdout


# ---------------------------------------------------------------------------
# _find_tsc: local tsc.cmd and global tsc (lines 974, 979)
# ---------------------------------------------------------------------------


def test_find_tsc_prefers_local_tsc_cmd(tmp_path: Path) -> None:
    """On Windows-style installs, node_modules/.bin/tsc.cmd is used."""
    from pyxle.cli import _find_tsc

    bin_dir = tmp_path / "node_modules" / ".bin"
    bin_dir.mkdir(parents=True)
    tsc_cmd = bin_dir / "tsc.cmd"
    tsc_cmd.write_text("@echo off")

    result = _find_tsc(tmp_path)
    assert result == [str(tsc_cmd)]


def test_find_tsc_uses_global_tsc(tmp_path: Path, monkeypatch) -> None:
    """When no local binary exists, a global tsc on PATH is used."""
    from pyxle.cli import _find_tsc

    monkeypatch.setattr(
        "shutil.which",
        lambda cmd: "/usr/local/bin/tsc" if cmd == "tsc" else None,
    )

    result = _find_tsc(tmp_path)
    assert result == ["/usr/local/bin/tsc"]


# ---------------------------------------------------------------------------
# routes: lazy import + settings/build failures (lines 1014-1016, 1033-1035, 1041-1043)
# ---------------------------------------------------------------------------


def test_routes_command_lazy_imports_devserver_settings(monkeypatch) -> None:
    """``routes`` lazily imports DevServerSettings when it is unset."""
    with runner.isolated_filesystem():
        project = Path("demo")
        (project / "pages").mkdir(parents=True)
        (project / "public").mkdir(parents=True)
        (project / "pages" / "index.pyxl").write_text(
            "import React from 'react';\n"
            "export default function P() { return <div />; }\n",
            encoding="utf-8",
        )

        monkeypatch.setattr("pyxle.cli.DevServerSettings", None)

        result = runner.invoke(app, ["routes", "demo"], catch_exceptions=False)

        assert result.exit_code == 0, result.stdout
        assert cli.DevServerSettings is not None
        assert "route(s) found" in result.stdout


def test_routes_command_settings_failure_exits(monkeypatch) -> None:
    """A failure constructing settings makes ``routes`` exit with 1."""
    with runner.isolated_filesystem():
        project = Path("demo")
        (project / "pages").mkdir(parents=True)
        (project / "public").mkdir(parents=True)

        def boom(*_a, **_kw):
            raise ValueError("routes settings exploded")

        monkeypatch.setattr("pyxle.cli.DevServerSettings.from_project_root", boom)

        result = runner.invoke(app, ["routes", "demo"], catch_exceptions=False)

        assert result.exit_code == 1
        assert "Failed to create settings" in result.stdout
        assert "routes settings exploded" in result.stdout


def test_routes_command_build_failure_exits(monkeypatch) -> None:
    """A failure during the route-discovery build exits with code 1."""
    with runner.isolated_filesystem():
        project = Path("demo")
        (project / "pages").mkdir(parents=True)
        (project / "public").mkdir(parents=True)

        def boom(_settings):
            raise RuntimeError("routes build blew up")

        monkeypatch.setattr("pyxle.devserver.builder.build_once", boom)

        result = runner.invoke(app, ["routes", "demo"], catch_exceptions=False)

        assert result.exit_code == 1
        assert "Build failed" in result.stdout
        assert "routes build blew up" in result.stdout


def test_routes_text_output_with_no_pages_only_apis() -> None:
    """An API-only project produces text output that skips the Pages block
    (branch 1074->1088) but still lists API routes."""
    with runner.isolated_filesystem():
        project = Path("demo")
        (project / "pages" / "api").mkdir(parents=True)
        (project / "public").mkdir()
        # No .pyxl page files at all — only an API endpoint.
        (project / "pages" / "api" / "ping.py").write_text(
            "async def endpoint(request):\n    return {'ok': True}\n",
            encoding="utf-8",
        )

        result = runner.invoke(app, ["routes", "demo"], catch_exceptions=False)

        assert result.exit_code == 0, result.stdout
        assert "API Routes:" in result.stdout
        # No page rows were emitted because there are no pages.
        assert "Pages:" not in result.stdout
        assert "route(s) found" in result.stdout


# ---------------------------------------------------------------------------
# check: package.json present skips the "No package.json" warning
# (branch 776->787)
# ---------------------------------------------------------------------------


def test_check_command_with_package_json_skips_warning() -> None:
    """When package.json exists, ``check`` does not warn about it
    (branch 776->787 is taken)."""
    with runner.isolated_filesystem():
        project = Path("demo")
        (project / "pages").mkdir(parents=True)
        (project / "public").mkdir()
        (project / "node_modules").mkdir()
        (project / "package.json").write_text('{"name": "demo"}', encoding="utf-8")
        (project / "pages" / "index.pyxl").write_text(
            "import React from 'react';\n"
            "export default function P() { return <div />; }\n",
            encoding="utf-8",
        )

        result = runner.invoke(app, ["check", "demo"], catch_exceptions=False)

        assert result.exit_code == 0, result.stdout
        assert "No package.json found" not in result.stdout
        assert "passed" in result.stdout


def test_dist_has_websocket_pages(tmp_path: Path) -> None:
    """The multi-worker WS warning trigger: detects a websocket page in the
    build's page-manifest.json (and is safe when the manifest is absent)."""
    dist = tmp_path / "dist"
    dist.mkdir()
    manifest = dist / "page-manifest.json"

    # No manifest yet → no websocket pages.
    assert cli._dist_has_websocket_pages(dist) is False

    # Manifest with only ordinary pages → False.
    manifest.write_text(
        json.dumps({"/": {"client": {"file": "x"}}, "/about": {"server": {}}}),
        encoding="utf-8",
    )
    assert cli._dist_has_websocket_pages(dist) is False

    # Manifest with a websocket page → True.
    manifest.write_text(
        json.dumps(
            {
                "/": {"client": {"file": "x"}},
                "/chat/{room}": {"websocket": {"name": "websocket", "line": 3}},
            }
        ),
        encoding="utf-8",
    )
    assert cli._dist_has_websocket_pages(dist) is True

    # Malformed manifest → False (never raises).
    manifest.write_text("not json", encoding="utf-8")
    assert cli._dist_has_websocket_pages(dist) is False


def test_install_dependencies_break_system_packages(monkeypatch, tmp_path) -> None:
    """--break-system-packages threads through to the pip command for PEP-668."""
    # Python deps are only installed when there is something to install;
    # these tests assert on the pip command, so give them a reason to run one.
    (tmp_path / "requirements.txt").write_text("pyxle-framework\n", encoding="utf-8")

    calls: list[list[str]] = []
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda command, *, cwd, check, stdout=None, stderr=None, text=False: calls.append(
            command
        ),
    )
    monkeypatch.setattr(cli, "_in_virtualenv", lambda: False)
    cli._install_dependencies(
        tmp_path,
        logger=cli.ConsoleLogger(),
        install_node=False,
        break_system_packages=True,
    )
    assert "--break-system-packages" in calls[0]


def test_install_no_break_flag_by_default(monkeypatch, tmp_path) -> None:
    (tmp_path / "requirements.txt").write_text("pyxle-framework\n", encoding="utf-8")

    calls: list[list[str]] = []
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda command, *, cwd, check, stdout=None, stderr=None, text=False: calls.append(
            command
        ),
    )
    monkeypatch.setattr(cli, "_in_virtualenv", lambda: True)
    cli._install_dependencies(tmp_path, logger=cli.ConsoleLogger(), install_node=False)
    assert "--break-system-packages" not in calls[0]


def test_sigterm_helper_raises_keyboard_interrupt(monkeypatch) -> None:
    """The dev command's SIGTERM handler reuses the Ctrl-C cleanup path."""
    import signal as signal_module

    import pyxle.cli as cli_module

    installed: dict[str, object] = {}

    def fake_signal(signum, handler):
        installed["signum"] = signum
        installed["handler"] = handler
        return "previous-handler"

    monkeypatch.setattr(cli_module.signal, "signal", fake_signal)
    previous = cli_module._install_sigterm_keyboard_interrupt(logger=None)
    assert previous == "previous-handler"
    assert installed["signum"] == signal_module.SIGTERM
    with pytest.raises(KeyboardInterrupt):
        installed["handler"](signal_module.SIGTERM, None)

    restored: dict[str, object] = {}
    monkeypatch.setattr(
        cli_module.signal, "signal", lambda s, h: restored.update(signum=s, handler=h)
    )
    cli_module._restore_sigterm_handler("previous-handler")
    assert restored["handler"] == "previous-handler"
    # None (installation failed) restores nothing.
    restored.clear()
    cli_module._restore_sigterm_handler(None)
    assert restored == {}


# ---------------------------------------------------------------------------
# `pyxle dev --inspect` — the debugpy debug server
# ---------------------------------------------------------------------------


def _invoke_dev_with_inspect(monkeypatch, argv):
    """Run a dev/studio command with `_start_debug_server` stubbed out.

    Returns ``(debug_server_calls, devserver_kwargs)`` so tests can assert the
    flag wiring without touching debugpy.
    """
    with runner.isolated_filesystem():
        project = Path("demo")
        (project / "pages").mkdir(parents=True)
        (project / "public").mkdir(parents=True)

        debug_server_calls: list[dict[str, object]] = []
        devserver_kwargs: dict[str, object] = {}

        def fake_start_debug_server(logger, *, port):
            debug_server_calls.append({"port": port})
            return ("127.0.0.1", port)

        class StubDevServer:
            def __init__(self, settings, logger, **kwargs):
                devserver_kwargs.update(kwargs)

            async def start(self) -> None:  # pragma: no cover - not awaited
                pass

        from pyxle.devserver import DevServerSettings as _RealSettings

        monkeypatch.setattr("pyxle.cli.DevServerSettings", _RealSettings)
        monkeypatch.setattr("pyxle.cli.DevServer", StubDevServer)
        monkeypatch.setattr("pyxle.cli._start_debug_server", fake_start_debug_server)
        monkeypatch.setattr("pyxle.cli.asyncio.run", lambda coro: coro.close())

        result = runner.invoke(app, argv, catch_exceptions=False)
        assert result.exit_code == 0, result.stdout
        return debug_server_calls, devserver_kwargs


def test_dev_command_inspect_flag_hosts_debug_server(monkeypatch) -> None:
    calls, kwargs = _invoke_dev_with_inspect(monkeypatch, ["dev", "demo", "--inspect"])
    # _start_debug_server no longer waits — it just hosts and returns the endpoint.
    assert calls == [{"port": 5678}]
    # The endpoint flows into the DevServer so the discovery file can
    # advertise it to editor tooling.
    assert kwargs["inspect_endpoint"] == ("127.0.0.1", 5678)
    # No --inspect-wait: the DevServer must not block for an attach.
    assert kwargs["inspect_wait"] is False


def test_dev_command_inspect_port_flag(monkeypatch) -> None:
    calls, kwargs = _invoke_dev_with_inspect(
        monkeypatch,
        ["dev", "demo", "--inspect", "--inspect-port", "6000"],
    )
    assert calls == [{"port": 6000}]
    assert kwargs["inspect_endpoint"] == ("127.0.0.1", 6000)


def test_dev_command_inspect_wait_flows_to_devserver(monkeypatch) -> None:
    # The wait-for-attach moved out of _start_debug_server into DevServer.start,
    # so --inspect-wait must surface as an ``inspect_wait=True`` on the DevServer.
    calls, kwargs = _invoke_dev_with_inspect(
        monkeypatch, ["dev", "demo", "--inspect", "--inspect-wait"]
    )
    assert calls == [{"port": 5678}]
    assert kwargs["inspect_wait"] is True


def test_dev_command_inspect_wait_without_inspect_never_waits(monkeypatch) -> None:
    # --inspect-wait alone (no --inspect) hosts no debug server and never blocks:
    # ``inspect_wait and inspect`` collapses to False.
    calls, kwargs = _invoke_dev_with_inspect(monkeypatch, ["dev", "demo", "--inspect-wait"])
    assert calls == []
    assert kwargs["inspect_endpoint"] is None
    assert kwargs["inspect_wait"] is False


def test_dev_command_without_inspect_skips_debug_server(monkeypatch) -> None:
    calls, kwargs = _invoke_dev_with_inspect(monkeypatch, ["dev", "demo"])
    assert calls == []
    assert kwargs["inspect_endpoint"] is None


def test_studio_command_supports_inspect(monkeypatch) -> None:
    calls, kwargs = _invoke_dev_with_inspect(
        monkeypatch, ["studio", "demo", "--inspect", "--inspect-port", "6001"]
    )
    # Studio never waits for an attach — it has no --inspect-wait flag.
    assert calls == [{"port": 6001}]
    assert kwargs["inspect_endpoint"] == ("127.0.0.1", 6001)
    assert kwargs["inspect_wait"] is False


def test_studio_command_has_no_inspect_wait_flag() -> None:
    with runner.isolated_filesystem():
        project = Path("demo")
        (project / "pages").mkdir(parents=True)
        (project / "public").mkdir(parents=True)
        result = runner.invoke(app, ["studio", "demo", "--inspect-wait"])
        assert result.exit_code != 0


class _FakeDebugpy:
    """Stand-in for the optional ``debugpy`` dependency.

    CLI tests must never import real debugpy — it spawns an adapter
    subprocess and binds sockets.
    """

    def __init__(self, *, fail_ports: tuple[int, ...] = ()) -> None:
        self.configure_calls: list[dict[str, object]] = []
        self.listen_calls: list[tuple[str, int]] = []
        self.wait_calls = 0
        self._fail_ports = fail_ports

    def configure(self, **kwargs) -> None:
        self.configure_calls.append(kwargs)

    def listen(self, endpoint: tuple[str, int]) -> tuple[str, int]:
        self.listen_calls.append(endpoint)
        host, port = endpoint
        if port in self._fail_ports:
            raise RuntimeError("Address already in use")
        return (host, 49152 if port == 0 else port)

    def wait_for_client(self) -> None:
        self.wait_calls += 1


@pytest.fixture
def _no_pydevd_env(monkeypatch):
    """Ensure PYDEVD_DISABLE_FILE_VALIDATION is unset and restored afterwards."""
    monkeypatch.setenv("PYDEVD_DISABLE_FILE_VALIDATION", "primed-for-restore")
    monkeypatch.delenv("PYDEVD_DISABLE_FILE_VALIDATION")


def _capture_logger():
    messages: list[str] = []

    def secho(message: str, fg=None, bold: bool = False) -> None:
        messages.append(message)

    return cli.ConsoleLogger(secho=secho), messages


def test_start_debug_server_configures_and_listens(monkeypatch, _no_pydevd_env) -> None:
    import os

    fake = _FakeDebugpy()
    monkeypatch.setitem(sys.modules, "debugpy", fake)
    logger, messages = _capture_logger()

    endpoint = cli._start_debug_server(logger, port=5678)

    assert endpoint == ("127.0.0.1", 5678)
    # pydevd's noisy file validation is silenced before debugpy loads.
    assert os.environ["PYDEVD_DISABLE_FILE_VALIDATION"] == "1"
    # Subprocesses (Vite, SSR workers) must never inherit the debugger.
    assert fake.configure_calls == [{"subProcess": False}]
    assert fake.listen_calls == [("127.0.0.1", 5678)]
    # The wait-for-attach lives in DevServer.start now, never here.
    assert fake.wait_calls == 0
    assert any("Debugger listening on 127.0.0.1:5678" in m for m in messages)


def test_start_debug_server_respects_existing_pydevd_env(monkeypatch) -> None:
    import os

    fake = _FakeDebugpy()
    monkeypatch.setitem(sys.modules, "debugpy", fake)
    monkeypatch.setenv("PYDEVD_DISABLE_FILE_VALIDATION", "0")
    logger, _ = _capture_logger()

    cli._start_debug_server(logger, port=5678)

    # setdefault semantics: a user's explicit opt-out is preserved.
    assert os.environ["PYDEVD_DISABLE_FILE_VALIDATION"] == "0"


def test_start_debug_server_falls_back_to_ephemeral_port(
    monkeypatch, _no_pydevd_env
) -> None:
    fake = _FakeDebugpy(fail_ports=(5678,))
    monkeypatch.setitem(sys.modules, "debugpy", fake)
    logger, messages = _capture_logger()

    endpoint = cli._start_debug_server(logger, port=5678)

    assert fake.listen_calls == [("127.0.0.1", 5678), ("127.0.0.1", 0)]
    assert endpoint == ("127.0.0.1", 49152)
    assert any("ephemeral port" in m for m in messages)


def test_start_debug_server_does_not_wait_for_client(monkeypatch, _no_pydevd_env) -> None:
    # The wait moved into DevServer.start (after the discovery file is written),
    # so _start_debug_server itself must never call wait_for_client.
    fake = _FakeDebugpy()
    monkeypatch.setitem(sys.modules, "debugpy", fake)
    logger, messages = _capture_logger()

    cli._start_debug_server(logger, port=5678)

    assert fake.wait_calls == 0
    assert not any("Waiting for a debugger to attach" in m for m in messages)


def test_start_debug_server_handles_missing_debugpy(monkeypatch, _no_pydevd_env) -> None:
    # debugpy ships with the framework, so its absence means a broken install.
    # sys.modules[name] = None makes `import debugpy` raise ImportError without
    # touching the real package; the command must fail clearly, not crash later.
    monkeypatch.setitem(sys.modules, "debugpy", None)
    logger, messages = _capture_logger()

    with pytest.raises(typer.Exit) as excinfo:
        cli._start_debug_server(logger, port=5678)

    assert excinfo.value.exit_code == 1
    # The error points at reinstalling the framework, not an extra.
    assert any("reinstall" in m.lower() for m in messages)


def test_build_command_prints_client_build_error_verbatim(monkeypatch) -> None:
    """A missing Node toolchain must reach the user as *its own* message.

    The build error already names the prerequisite, the consequence, and the
    fix; wrapping it in a generic "Build failed:" prefix would bury the one line
    the user needs. Exit status must be non-zero so CI and Docker builds stop.
    """
    from pyxle.build.pipeline import ClientBuildError

    with runner.isolated_filesystem():
        project = Path("demo")
        (project / "pages").mkdir(parents=True)

        def fake_run_build(settings, *, logger, dist_dir=None, force_rebuild=True):
            raise ClientBuildError(
                "npx was not found on your PATH — cannot build the client bundle.\n"
                "  install Node.js 20.19+ from https://nodejs.org"
            )

        monkeypatch.setattr("pyxle.cli.run_build", fake_run_build)

        result = runner.invoke(app, ["build", "demo"], catch_exceptions=False)

    assert result.exit_code == 1, result.stdout
    assert "npx was not found on your PATH" in result.stdout
    assert "https://nodejs.org" in result.stdout
    assert "Build failed:" not in result.stdout
    assert "Build completed" not in result.stdout


def test_serve_command_stops_when_the_client_build_cannot_run(monkeypatch) -> None:
    """``pyxle serve`` builds before serving unless ``--skip-build``; that build
    failing must abort the serve rather than boot on a stale/absent dist."""
    from pyxle.build.pipeline import ClientBuildError

    with runner.isolated_filesystem():
        project = Path("demo")
        (project / "pages").mkdir(parents=True)

        def fake_run_build(settings, *, logger, dist_dir=None, force_rebuild=True):
            raise ClientBuildError("npx was not found on your PATH — cannot build.")

        monkeypatch.setattr("pyxle.cli.run_build", fake_run_build)

        result = runner.invoke(app, ["serve", "demo"], catch_exceptions=False)

    assert result.exit_code == 1, result.stdout
    assert "npx was not found on your PATH" in result.stdout


def test_install_dependencies_skips_python_without_requirements(monkeypatch, tmp_path) -> None:
    """No requirements.txt is a normal shape — skip pip, still install node.

    `pip install -r` exits 1 on a missing target, which used to abort the whole
    command before `npm install` ever ran. Our own charts example has no
    requirements file, so its documented first command failed.
    """
    calls: list[list[str]] = []
    monkeypatch.setattr(
        cli.subprocess,
        "run",
        lambda command, *, cwd, check, stdout=None, stderr=None, text=False: calls.append(
            command
        ),
    )

    cli._install_dependencies(tmp_path, logger=cli.ConsoleLogger())

    assert not any("pip" in part for command in calls for part in command)
    assert ["npm", "install"] in calls
