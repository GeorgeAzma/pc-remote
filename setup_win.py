"""Windows integration for the installed app (run by the installer, elevated):

- a scheduled task that starts PC Remote at sign-in with the highest
  privileges, in the signed-in user's session (screen capture and input
  need the desktop; a service would run in session 0, which has none)
- firewall rules: allowed on private (home) networks and from Tailscale
  (100.64.0.0/10, fd7a:115c:a1e0::/48) on any network; never on public
  networks otherwise"""
import os
import subprocess

TASK = os.environ.get("PC_TASK_NAME", "PC Remote")  # (the override is for testing)
RULE, RULE_TS = TASK, f"{TASK} (Tailscale)"
TAILSCALE = "100.64.0.0/10,fd7a:115c:a1e0::/48"
_NO_WINDOW = 0x08000000


def _run(args, check=False):
    r = subprocess.run(args, capture_output=True, text=True, creationflags=_NO_WINDOW)
    if check and r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip() or f"{args[0]} failed")
    return r


def _ps(script, check=True):
    return _run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script], check)


def register_task(exe: str):
    """At every sign-in (any user; each gets their own copy running in their
    session), highest privileges, no time limit, also on battery, one copy."""
    exe_ps = exe.replace("'", "''")
    _ps(f"""
$a = New-ScheduledTaskAction -Execute '{exe_ps}' -Argument '--background'
$t = New-ScheduledTaskTrigger -AtLogOn
$p = New-ScheduledTaskPrincipal -GroupId 'S-1-5-32-545' -RunLevel Highest
$s = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
       -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName '{TASK}' -Action $a -Trigger $t -Principal $p -Settings $s `
  -Description 'Starts PC Remote at sign-in (remote control from your phone).' -Force | Out-Null
""")


def unregister_task():
    _ps(f"Unregister-ScheduledTask -TaskName '{TASK}' -Confirm:$false -ErrorAction SilentlyContinue", check=False)


def task_exists() -> bool:
    return _run(["schtasks", "/Query", "/TN", TASK]).returncode == 0


def run_task() -> bool:
    """Start the installed copy through its task: elevated, without a UAC prompt."""
    return _run(["schtasks", "/Run", "/TN", TASK]).returncode == 0


def add_firewall(exe: str):
    remove_firewall()
    base = ["netsh", "advfirewall", "firewall", "add", "rule", "dir=in", "action=allow", f"program={exe}", "enable=yes"]
    _run(base + [f"name={RULE}", "profile=private,domain"], check=True)
    _run(base + [f"name={RULE_TS}", "profile=any", f"remoteip={TAILSCALE}"], check=True)


def remove_firewall():
    for name in (RULE, RULE_TS):
        _run(["netsh", "advfirewall", "firewall", "delete", "rule", f"name={name}"])


def install(exe: str, startup: bool = True):
    add_firewall(exe)
    if startup:
        register_task(exe)
    else:
        unregister_task()


def uninstall():
    unregister_task()
    remove_firewall()
