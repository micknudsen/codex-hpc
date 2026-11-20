"""SSH defaults shared by session and callback connections."""


def ssh_options(jump_host=None):
    options = ["ssh", "-o", "ForwardAgent=no", "-o", "ForwardX11=no"]
    if jump_host:
        options.extend(["-J", jump_host])
    return options


def ssh_arguments(host, command, jump_host=None, terminal=True):
    arguments = [*ssh_options(jump_host), "-tt" if jump_host and terminal else "-T"]
    if jump_host:
        arguments.extend(["-o", "ControlMaster=no", "-o", "ControlPath=none"])
    return [*arguments, "--", host, command]
