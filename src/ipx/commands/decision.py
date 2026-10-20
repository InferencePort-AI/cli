"""Typed System One decisions on the unified Pay-2-Go API."""
import json
import sys

import click

from ipx import ui
from ipx.app import App, pass_app
from ipx.commands.modalities import write_atomic
from ipx.errors import IpxError


def add_decision(group):
    @group.command("decision", short_help="Classify state with System One questions")
    @click.argument("state", required=False)
    @click.option("-m", "--model", default="microsoft/decision-1", show_default=True)
    @click.option("-q", "--questions", type=click.Path(exists=True, dir_okay=False), help="JSON file containing the named questions object.")
    @click.option("-f", "--file", "request_file", type=click.Path(dir_okay=False), help="Full System One request JSON. Use - for stdin.")
    @click.option("-o", "--output", help="Save the full response as JSON.")
    @pass_app
    def decision(app: App, state, model, questions, request_file, output):
        """Evaluate typed choice/noul/score questions. Pass STATE and --questions, or --file with a full request.

        Without STATE, reads the state from stdin. --file - reads a full request from stdin.
        Provider authentication stays on the server; this command uses your InferencePort credentials.
        """
        app.require_login()
        try:
            if request_file:
                if state or questions:
                    raise IpxError("Use either --file or STATE with --questions.")
                if request_file == "-":
                    body = json.load(sys.stdin)
                else:
                    with open(request_file, encoding="utf-8") as f:
                        body = json.load(f)
                if not isinstance(body, dict):
                    raise IpxError("Request JSON must be an object.")
                body.setdefault("model", model)
                # An explicit -m always overrides the model in a request file.
                if click.get_current_context().get_parameter_source("model") == click.core.ParameterSource.COMMANDLINE:
                    body["model"] = model
            else:
                if not questions:
                    raise IpxError("Pass --questions FILE or --file REQUEST.json.")
                with open(questions, encoding="utf-8") as f:
                    named = json.load(f)
                if not isinstance(named, dict) or not named:
                    raise IpxError("Questions JSON must be a non-empty object.")
                if state is None or state == "-":
                    state = sys.stdin.read() if not sys.stdin.isatty() else ""
                body = {"model": model, "state": state, "questions": named}
            if not isinstance(body.get("state"), (str, dict, list)) or not body["state"]:
                raise IpxError("Provide a non-empty state.")
        except (OSError, ValueError) as e:
            raise IpxError("Could not read decision JSON.", str(e))
        result = app.api.request("POST", "/v1/systemone", json=body, timeout=120).json()
        if output:
            write_atomic(output, (json.dumps(result, indent=2) + "\n").encode("utf-8"))
        app.out(result, lambda data: ui.console.print_json(data=data))
