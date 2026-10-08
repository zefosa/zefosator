"""Host side of `zefosator verify`: install the build output in a clean vendor container."""

import shlex
from pathlib import Path

from jinja2 import Environment, PackageLoader, StrictUndefined

from zefosator.config import Plan
from zefosator.errors import BuildError
from zefosator.podman import Executor, execute

_ENV = Environment(
    loader=PackageLoader("zefosator", "templates"),
    undefined=StrictUndefined,
    autoescape=False,  # noqa: S701 -- renders a shell script, values are quoted with the sh filter
    keep_trailing_newline=True,
    trim_blocks=True,
    lstrip_blocks=True,
)
_ENV.filters["sh"] = shlex.quote


def render_script(plan: Plan) -> str:
    return _ENV.get_template("verify.sh.j2").render(plan=plan)


def verify_argv(plan: Plan, root: Path, script: Path) -> list[str]:
    return [
        "podman",
        "run",
        "--rm",
        "--security-opt=label=disable",
        f"--volume={root / 'output' / plan.target_id}:/output:ro",
        f"--volume={script}:/verify.sh:ro",
        plan.image.base,
        "sh",
        "/verify.sh",
    ]


def verify(plan: Plan, root: Path, *, execute: Executor = execute) -> None:
    output = root / "output" / plan.target_id
    if not (output / "kernel").is_dir():
        msg = f"{output}/kernel not found; run `zefosator build` for this target first"
        raise BuildError(msg)
    script = root / "cache" / "verify" / f"{plan.target_id}.sh"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(render_script(plan))
    code = execute(verify_argv(plan, root, script))
    if code != 0:
        msg = f"verification failed for {plan.target_id} (exit code {code}); see FAIL lines above"
        raise BuildError(msg)
