"""Project CLI contract shared by fakes: flags supported by gh 2.96 and 2.102."""

FLAGS = {
    "item-list": {"--owner", "--format", "--limit", "--jq", "--query", "--template"},
    "item-edit": {"--id", "--project-id", "--field-id", "--single-select-option-id",
                  "--clear", "--body", "--date", "--format", "--iteration-id", "--jq",
                  "--number", "--template", "--text", "--title"},
    "field-list": {"--owner", "--format", "--limit", "--jq", "--template"},
    "view": {"--owner", "--format", "--jq", "--template", "--web"},
    "item-add": {"--owner", "--url", "--format", "--jq", "--template"},
}


def validate(args):
    if args[0] != "project":
        return
    allowed = FLAGS[args[1]]
    for arg in args[2:]:
        if arg.startswith("--") and arg not in allowed:
            raise AssertionError(f"unknown flag: {arg}")
    if args[1] == "item-edit":
        assert args[2].startswith("--"), "item-edit takes node IDs, not a project number"
        for flag in ("--id", "--project-id", "--field-id"):
            assert flag in args, f"missing required flag: {flag}"
        assert "--clear" in args or "--single-select-option-id" in args
