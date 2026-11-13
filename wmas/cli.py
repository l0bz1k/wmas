"""Terminal front end for the WMAS engine."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys

from . import __version__, core

OUTPUT_JSON = False


class Quit(Exception):
    """The user asked to leave."""


def say(text=""):
    print(text, file=sys.stderr if OUTPUT_JSON else sys.stdout)


def note(text):
    print("  · " + text, file=sys.stderr, flush=True)


def probe_progress(row):
    if row.get("from_cache"):
        return
    if row.get("error"):
        note("version %s did not answer" % row["ext_id"])
    else:
        note("probed %s — %s" % (row["display_version"], os_label(row["min_os"])))


def os_label(min_os):
    return "macOS %s+" % min_os if min_os else "min. macOS not stated"


def price_label(app):
    price = app.get("price")
    if price is None:
        return ""
    return "free" if not price else "%s" % price


def ask(prompt, default=""):
    """Read one line. "q" quits from any prompt."""
    try:
        answer = input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        raise Quit()
    if answer.lower() == "q":
        raise Quit()
    return answer or default


def who(info):
    return "%s <%s>" % (info["name"], info["email"]) if info else "not signed in"


# ---- accounts -----------------------------------------------------------

def sign_in(engine, profile, email):
    command, env = engine.login_command(email, profile)
    say("Signing in to the App Store as profile \"%s\". The password and 2FA code go straight to ipatool." % profile)
    subprocess.call(command, env=env)
    info = engine.account(profile)
    if info:
        engine.use(profile)
        say("Signed in: %s" % who(info))
    else:
        say("Sign-in failed.")
    return info


def add_account(engine, profile, email):
    engine.add_profile(profile)
    if not sign_in(engine, profile, email):
        engine.forget(profile)
        return False
    return True


def print_accounts(engine):
    names = list(engine.profiles())
    for number, name in enumerate(names, 1):
        mark = "*" if name == engine.profile else " "
        say(" %s %d) %-14s %s" % (mark, number, name, who(engine.account(name))))
    return names


def pick(names, text):
    return names[int(text) - 1] if text.isdigit() and 1 <= int(text) <= len(names) else None


def account_menu(engine):
    while True:
        say()
        say("Apple ID profiles (* = current):")
        names = print_accounts(engine)
        choice = ask("number — use, n — new, r <number> — rename, d <number> — remove, Enter — back, q — quit: ")
        if not choice:
            return
        command, _, argument = choice.partition(" ")
        command, argument = command.lower(), argument.strip()
        try:
            if command == "n":
                email = ask("Apple ID (e-mail): ")
                if not email:
                    continue
                suggested = core.safe_name(email.split("@")[0]).lower()
                profile = ask("Profile name — any label you like, e.g. work or us-store [%s]: " % suggested, suggested)
                add_account(engine, profile, email)
            elif command == "r" and pick(names, argument):
                old = pick(names, argument)
                new = ask("New name for \"%s\": " % old)
                if new:
                    engine.rename_profile(old, new)
            elif command == "d" and pick(names, argument):
                name = pick(names, argument)
                if ask("Sign out and remove \"%s\"? [y/N]: " % name).lower() == "y":
                    removed = engine.forget(name)
                    say("Removed." if removed else "Signed out (this profile is kept as the base one).")
            elif pick(names, choice):
                name = pick(names, choice)
                if engine.account(name):
                    engine.use(name)
                    return
                known = engine.profiles()[name].get("email", "")
                email = ask("Apple ID for \"%s\"%s: " % (name, " [%s]" % known if known else ""), known)
                if email:
                    sign_in(engine, name, email)
            else:
                say("Not understood.")
        except core.WmasError as failure:
            say("Error: %s" % failure)


def account_step(engine):
    """Shown first on every interactive start: confirm the account or switch."""
    info = engine.account()
    if not info:
        say("Profile \"%s\" is not signed in to the App Store." % engine.profile)
        account_menu(engine)
        return engine.account() is not None
    say("Account: %s  [profile %s]" % (who(info), engine.profile))
    if ask("Enter — continue, s — switch account, q — quit: ").lower() == "s":
        account_menu(engine)
        info = engine.account()
        if info:
            say("Account: %s  [profile %s]" % (who(info), engine.profile))
    return info is not None


# ---- listing ------------------------------------------------------------

def print_apps(apps):
    for number, app in enumerate(apps, 1):
        price = price_label(app)
        say(" %2d) %s — %s — id %s%s" % (
            number, app.get("name", "?"), app.get("bundleID", "?"), app["id"], " — " + price if price else ""))


def print_versions(engine, app):
    """Newest first. Probed builds are numbered; runs of unprobed builds collapse."""
    rows = engine.table(app["id"])
    say("%s — %d version%s offered by the Store" % (app["name"], len(rows), "" if len(rows) == 1 else "s"))
    say("  #  %-14s %-11s %-16s %-16s %s" % ("Version", "Released", "Minimum system", "Architectures", "Version ID"))
    numbered, pending = [], []

    def flush():
        if not pending:
            return
        inferred = {row.get("min_os_inferred") for row in pending}
        failed = sum(1 for row in pending if row["state"] == "failed")
        hint = "(%s, inferred)" % os_label(inferred.pop()) if len(inferred) == 1 and None not in inferred \
            else "(minimum system unknown)"
        say("   ·  %d not probed %s%s" % (len(pending), hint, ", %d did not answer" % failed if failed else ""))
        del pending[:]

    for row in rows:
        if row["state"] != "probed":
            pending.append(row)
            continue
        flush()
        numbered.append(row)
        say(" %3d  %-14s %-11s %-16s %-16s %s" % (
            len(numbered), row["display_version"], row["release_date"] or "", "(%s)" % os_label(row["min_os"]),
            row["arch"] or "", row["ext_id"]))
    flush()
    missing = engine.unoffered(app["id"])
    if missing:
        say()
        say("Listed on the App Store page but NOT offered for download (%d); the Store no longer serves them:"
            % len(missing))
        say("  " + ", ".join("%s (%s)" % (entry["version"], entry["date"]) for entry in missing))
    return numbered


def ensure_owned(engine, app, acquire, interactive):
    """Probe the newest build; if the account lacks a licence, offer to get one."""
    try:
        engine.probe(app["id"], engine.version_ids(app["id"])[-1])
        return True
    except core.LicenseRequired:
        if app.get("price"):
            say("This is a paid app that this account has not bought, so it cannot be downloaded.")
            return False
        if not acquire and not (interactive and ask(
                "This account does not own the app yet. Get the free licence? [y/N]: ").lower() == "y"):
            say("A licence is needed. Repeat with --acquire to get it (the app is free).")
            return False
        engine.purchase(app["id"])
        say("Licence obtained.")
        return True


def resolve_app(engine, text, interactive):
    app_id = core.parse_app_id(text)
    if app_id:
        return engine.app(app_id)
    apps = engine.search(text)
    exact = [app for app in apps if app.get("name", "").lower() == text.lower()]
    if len(exact) == 1:
        return exact[0]
    if len(apps) == 1:
        return apps[0]
    if not apps:
        raise core.WmasError("nothing found for \"%s\"; try: search --owned" % text)
    print_apps(apps)
    if not interactive:
        raise core.WmasError("several apps match — pass an App ID")
    choice = pick(apps, ask("App number: "))
    if not choice:
        raise core.WmasError("no app chosen")
    return choice


def report_download(result):
    say("Done: %s" % result["path"])
    say("  version %s, %s, %d bytes" % (result["version"], os_label(result["min_os"]), result["size"]))
    say("  SHA-256: %s" % result["sha256"])
    say("  signature: %s%s" % (
        result["signature"], "" if result["signed_by_apple"] else "  <- WARNING: not an Apple signature"))


# ---- interactive --------------------------------------------------------

def version_menu(engine, app):
    if not ensure_owned(engine, app, False, True):
        return
    say("Locating minimum-system boundaries…")
    engine.boundaries(app["id"], probe_progress)
    while True:
        say()
        numbered = print_versions(engine, app)
        say()
        choice = ask("# or version — download, f — probe all, m <macOS> — newest for that system, "
                     "Enter — back, q — quit: ")
        if not choice:
            return
        try:
            if choice.lower() == "f":
                todo = len(engine.version_ids(app["id"])) - len(engine.cached(app["id"]))
                say("Probing the remaining %d versions (about %d min). Ctrl-C stops; results so far are kept."
                    % (todo, max(1, todo * 7 // 60)))
                try:
                    engine.fill(app["id"], probe_progress)
                except KeyboardInterrupt:
                    say("Stopped; everything probed so far is cached.")
                continue
            if choice.lower() == "m" or choice.lower().startswith("m "):
                macos = choice[1:].strip() or ask("macOS version (for example 11): ")
                row = engine.newest_for(app["id"], macos, probe_progress)
                say("Newest version for macOS %s: %s" % (macos, "%s (%s, %s)" % (
                    row["display_version"], row["release_date"], os_label(row["min_os"])) if row else "none"))
                if not row or ask("Download it? [y/N]: ").lower() != "y":
                    continue
            elif pick(numbered, choice):
                row = pick(numbered, choice)
            else:
                row = engine.find_version(app["id"], choice, probe_progress)
                if not row or row.get("error"):
                    say("Version \"%s\" is not offered by the Store." % choice)
                    continue
            say("Downloading %s %s…" % (app["name"], row["display_version"]))
            report_download(engine.download(app["id"], row["ext_id"]))
        except core.WmasError as failure:
            say("Error: %s" % failure)


def interactive(engine):
    say("WMAS — older versions of Mac App Store apps")
    if not account_step(engine):
        return 1
    while True:
        say()
        text = ask("App name, App ID or App Store link  (o <text> — search purchases, a — accounts, q — quit): ")
        if not text:
            continue
        try:
            app_id = core.parse_app_id(text)
            if text.lower() == "a":
                account_menu(engine)
                continue
            if text.lower() == "o" or text.lower().startswith("o "):
                apps = engine.owned(text[1:].strip() or None)
            elif app_id:
                apps = [engine.app(app_id)]
            else:
                apps = engine.search(text)
            if not apps:
                say("Nothing found. Apps pulled from sale can still be found with: o <text>")
                continue
            app = apps[0]
            if len(apps) > 1:
                print_apps(apps)
                app = pick(apps, ask("App number (Enter — back): "))
                if not app:
                    continue
            version_menu(engine, app)
        except core.NotSignedIn:
            say("The App Store session has expired — sign in again.")
            account_menu(engine)
        except core.WmasError as failure:
            say("Error: %s" % failure)


# ---- commands -----------------------------------------------------------

def main(argv=None):
    global OUTPUT_JSON
    parser = argparse.ArgumentParser(
        prog="wmas", description="Find and download older versions of Mac App Store apps. "
        "With no command it starts an interactive session.")
    parser.add_argument("--profile", metavar="NAME", help="Apple ID profile to use (default: the last one selected)")
    parser.add_argument("--json", action="store_true", help="print JSON; messages go to stderr")
    parser.add_argument("--version", action="version", version="wmas %s" % __version__)
    sub = parser.add_subparsers(dest="command")
    p = sub.add_parser("search", help="find an app")
    p.add_argument("term")
    p.add_argument("--owned", action="store_true", help="search the account's purchases, including apps pulled from sale")
    p.add_argument("--limit", type=int, default=10)
    p = sub.add_parser("versions", help="list an app's versions with their minimum macOS")
    p.add_argument("app", help="App ID (1476245357 or id1476245357), App Store link, or name")
    p.add_argument("--full", action="store_true", help="probe every version (slow the first time, cached after)")
    p.add_argument("--cached", action="store_true", help="show only what is already cached")
    p.add_argument("--for-macos", metavar="X", help="find the newest version that runs on macOS X")
    p.add_argument("--acquire", action="store_true", help="get the licence if a free app is not owned yet")
    p = sub.add_parser("download", help="download one version")
    p.add_argument("app", help="App ID, App Store link, or name")
    p.add_argument("version", help="version (8.2.11) or version ID")
    p.add_argument("-o", "--output", help="destination directory")
    p.add_argument("--acquire", action="store_true")
    p = sub.add_parser(
        "accounts", help="manage Apple ID profiles",
        description="A profile is a named Apple ID session. The name is any label you choose, "
                    "for example 'work' or 'us-store'.",
        epilog="examples:  wmas accounts add us-store --email me@example.com   |   "
               "wmas accounts rename default ru   |   wmas accounts use us-store")
    p.add_argument("action", nargs="?", default="list", choices=["list", "add", "use", "rename", "remove"])
    p.add_argument("names", nargs="*", metavar="NAME", help="profile name; rename takes OLD NEW")
    p.add_argument("--email", help="Apple ID for 'add'")
    args = parser.parse_args(argv)
    OUTPUT_JSON = args.json

    try:
        engine = core.Engine(profile=args.profile, notify=note)
        if not args.command:
            return interactive(engine)

        if args.command == "accounts":
            names = args.names
            if args.action == "list":
                if args.json:
                    print(json.dumps({"current": engine.profile, "profiles": {
                        name: engine.account(name) for name in engine.profiles()}}, ensure_ascii=False, indent=2))
                else:
                    print_accounts(engine)
            elif args.action == "use" and len(names) == 1:
                engine.use(names[0])
                say("Current profile: %s" % engine.profile)
            elif args.action == "add" and len(names) == 1 and args.email:
                return 0 if add_account(engine, names[0], args.email) else 5
            elif args.action == "rename" and len(names) == 2:
                engine.rename_profile(names[0], names[1])
                say("Profile \"%s\" is now \"%s\"." % (names[0], names[1]))
            elif args.action == "remove" and len(names) == 1:
                removed = engine.forget(names[0])
                say("Profile \"%s\" %s." % (names[0], "removed" if removed else "signed out"))
            else:
                parser.error("usage: accounts [list | add NAME --email APPLE_ID | use NAME | rename OLD NEW | remove NAME]")
            return 0

        if args.command == "search":
            apps = engine.owned(args.term) if args.owned else engine.search(args.term, args.limit)
            if args.json:
                print(json.dumps(apps, ensure_ascii=False, indent=2))
            elif apps:
                print_apps(apps)
            else:
                say("Nothing found.")
            return 0

        app = resolve_app(engine, args.app, sys.stdin.isatty() and not args.json)
        if not (args.command == "versions" and args.cached):
            if not ensure_owned(engine, app, args.acquire, False):
                return 2

        if args.command == "versions":
            if args.for_macos:
                row = engine.newest_for(app["id"], args.for_macos, probe_progress)
                if args.json:
                    print(json.dumps(row, ensure_ascii=False, indent=2))
                elif row:
                    say("%s: newest version for macOS %s is %s (%s, %s), version ID %s" % (
                        app["name"], args.for_macos, row["display_version"], row["release_date"],
                        os_label(row["min_os"]), row["ext_id"]))
                else:
                    say("No offered version runs on macOS %s." % args.for_macos)
                return 0 if row else 3
            if args.full:
                engine.fill(app["id"], probe_progress)
            elif not args.cached:
                engine.boundaries(app["id"], probe_progress)
            if args.json:
                print(json.dumps({"app": app, "versions": engine.table(app["id"]),
                                  "unoffered": engine.unoffered(app["id"])}, ensure_ascii=False, indent=2))
            else:
                print_versions(engine, app)
            return 0

        if args.command == "download":
            row = engine.find_version(app["id"], args.version, probe_progress)
            if not row or row.get("error"):
                raise core.WmasError("version \"%s\" is not offered by the Store" % args.version)
            result = engine.download(app["id"], row["ext_id"], args.output)
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                report_download(result)
            return 0 if result["signed_by_apple"] else 4
    except Quit:
        return 0
    except core.NotSignedIn:
        say("Not signed in to the App Store. Run wmas with no arguments, or: "
            "wmas accounts add NAME --email APPLE_ID")
        return 5
    except core.WmasError as failure:
        say("Error: %s" % failure)
        return 1
    except KeyboardInterrupt:
        say("\nInterrupted.")
        return 130
    return 0
