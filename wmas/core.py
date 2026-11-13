"""WMAS engine.

Everything a front end needs, with no printing and no prompts: accounts, search,
version enumeration with minimum-OS probing, a permanent cache and downloads. The
terminal UI in cli.py and any later GUI are thin layers over this module.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import time
import urllib.parse
import urllib.request
import plistlib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IPATOOL = Path(os.environ.get("WMAS_IPATOOL", ROOT / "bin" / "ipatool"))
STATE = ROOT / "state"
ARTIFACTS = ROOT / "artifacts"
DEFAULT_PROFILE = "default"
PROFILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
# ipatool platform name -> how WMAS presents and looks it up.
PLATFORMS = {
    "macos": {"label": "Mac", "os": "macOS", "entity": "macSoftware", "page": "mac", "extension": ".pkg"},
    "iphone": {"label": "iPhone", "os": "iOS", "entity": "software", "page": "iphone", "extension": ".ipa"},
    "ipad": {"label": "iPad", "os": "iPadOS", "entity": "iPadSoftware", "page": "ipad", "extension": ".ipa"},
}
PLATFORM_ALIASES = {"mac": "macos", "osx": "macos", "ios": "iphone", "ipados": "ipad"}

# Apple answers request volume with 429, with 403 on the commerce endpoint, with
# empty bodies and with dropped connections. All of them mean "slow down".
THROTTLE_RE = re.compile(
    r"HTTP 429|HTTP 403|HTTP 5\d\d|\bEOF\b|connection reset|timeout|non-plist|invalid response", re.I)
AUTH_RE = re.compile(r"not be found in the keyring|failed to get account|password token|sign.?in", re.I)
LICENSE_RE = re.compile(r"license", re.I)

VERSION_LIST_TTL = 6 * 3600
FAILED_PROBE_TTL = 24 * 3600
HISTORY_TTL = 6 * 3600
AUTO_FILL_LIMIT = 8
HISTORY_COUNTRIES = ("us", "ru", "gb", "de")
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
              "(KHTML, like Gecko) Version/17.0 Safari/605.1.15")


class WmasError(Exception):
    """A request failed for a reason retrying will not fix."""


class Throttled(WmasError):
    """Apple kept throttling after every backoff step."""


class NotSignedIn(WmasError):
    """The selected profile has no usable App Store session."""


class LicenseRequired(WmasError):
    """The account does not own the app."""


def version_key(text):
    """Sort key for dotted versions. Trailing zeros are dropped so that "11" and
    "11.0" compare equal; an unknown version sorts lowest."""
    parts = [int(part) for part in re.findall(r"\d+", text or "")]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts) or (-1,)


def parse_app_id(text):
    """App ID from "1476245357", "id1476245357" or an App Store URL, else None."""
    text = (text or "").strip()
    match = re.fullmatch(r"(?:id)?(\d+)", text, re.I) or re.search(r"/id(\d+)", text)
    return int(match.group(1)) if match else None


def normalize_platform(text):
    name = (text or "macos").strip().lower()
    name = PLATFORM_ALIASES.get(name, name)
    if name not in PLATFORMS:
        raise WmasError("unknown platform %r: use mac, iphone or ipad" % text)
    return name


def safe_name(text):
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text or "").strip("-") or "app"


class Engine:
    def __init__(self, profile=None, pace=2.5, notify=None, platform=None):
        STATE.mkdir(parents=True, exist_ok=True)
        self._config_path = STATE / "config.json"
        self.config = {"current": DEFAULT_PROFILE, "profiles": {}}
        if self._config_path.exists():
            self.config.update(json.loads(self._config_path.read_text()))
        # A profile is a label the user chooses, mapped to the storage name ipatool
        # keys the session by ("" is ipatool's stock single-account storage). Keeping
        # the two apart is what makes renaming a profile a pure relabel.
        profiles = self.config.setdefault("profiles", {})
        for label, entry in profiles.items():
            entry.setdefault("storage", "" if label == DEFAULT_PROFILE else label)
        if not profiles:
            profiles[DEFAULT_PROFILE] = {"storage": ""}
        if self.config.get("current") not in profiles:
            self.config["current"] = sorted(profiles)[0]
        self.profile = profile or self.config["current"]
        if self.profile not in profiles:
            raise WmasError("unknown profile %r; run 'wmas accounts' to list profiles" % self.profile)
        self.platform = normalize_platform(platform or self.config.get("platform"))
        self.pace = pace
        self.notify = notify or (lambda message: None)
        self._last_call = 0.0
        self.db = sqlite3.connect(str(STATE / "cache.sqlite3"))
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS apps (
                app_id INTEGER PRIMARY KEY, bundle_id TEXT, name TEXT, price REAL, seen_at REAL);
            CREATE TABLE IF NOT EXISTS version_lists (
                app_id INTEGER PRIMARY KEY, ids TEXT NOT NULL, fetched_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS versions (
                app_id INTEGER NOT NULL, ext_id TEXT NOT NULL, display_version TEXT,
                bundle_version TEXT, release_date TEXT, min_os TEXT, arch TEXT,
                package_size INTEGER, error TEXT, probed_at REAL NOT NULL,
                PRIMARY KEY (app_id, ext_id));
            CREATE TABLE IF NOT EXISTS histories (
                app_id INTEGER PRIMARY KEY, entries TEXT NOT NULL, fetched_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS version_lists_v2 (
                app_id INTEGER NOT NULL, platform TEXT NOT NULL, ids TEXT NOT NULL, fetched_at REAL NOT NULL,
                PRIMARY KEY (app_id, platform));
            CREATE TABLE IF NOT EXISTS histories_v2 (
                app_id INTEGER NOT NULL, platform TEXT NOT NULL, entries TEXT NOT NULL, fetched_at REAL NOT NULL,
                PRIMARY KEY (app_id, platform));
            INSERT OR IGNORE INTO version_lists_v2 SELECT app_id, 'macos', ids, fetched_at FROM version_lists;
            CREATE TABLE IF NOT EXISTS downloads (
                app_id INTEGER, ext_id TEXT, path TEXT, sha256 TEXT, signature TEXT, at REAL);
        """)

    # ---- plumbing -------------------------------------------------------

    @staticmethod
    def _check_label(label):
        if not PROFILE_RE.match(label or ""):
            raise WmasError("invalid profile name %r: use letters, digits, dot, underscore or hyphen" % label)

    def _entry(self, label=None):
        label = label or self.profile
        try:
            return self.config["profiles"][label]
        except KeyError:
            raise WmasError("unknown profile %r" % label)

    def _save_config(self):
        self._config_path.write_text(json.dumps(self.config, indent=2, ensure_ascii=False) + "\n")

    def _env(self, profile=None):
        env = dict(os.environ)
        storage = self._entry(profile)["storage"]
        if storage:
            env["IPATOOL_PROFILE"] = storage
        else:
            env.pop("IPATOOL_PROFILE", None)
        return env

    def _wait(self):
        delay = self._last_call + self.pace - time.monotonic()
        if delay > 0:
            time.sleep(delay)

    def _ipatool(self, args, paced=False, profile=None, show_progress=False, retries=4):
        """Run ipatool and return its JSON result, backing off while Apple throttles."""
        command = [str(IPATOOL), *args, "--format", "json", "--non-interactive"]
        soft_retry_used = False
        attempt = 0
        while True:
            if paced:
                self._wait()
            proc = subprocess.run(
                command, env=self._env(profile), text=True, stdout=subprocess.PIPE,
                stderr=None if show_progress else subprocess.PIPE)
            self._last_call = time.monotonic()
            data = None
            for line in reversed(proc.stdout.splitlines()):
                line = line.strip()
                if line.startswith("{"):
                    try:
                        data = json.loads(line)
                        break
                    except ValueError:
                        continue
            # Not every command reports "success"; an error always has level "error".
            if data and proc.returncode == 0 and data.get("level") != "error" and "error" not in data:
                return data
            error = (data or {}).get("error") or (proc.stderr or "").strip()[-400:] or "ipatool returned no reply"
            if AUTH_RE.search(error):
                raise NotSignedIn(error)
            if LICENSE_RE.search(error):
                raise LicenseRequired(error)
            if THROTTLE_RE.search(error):
                if attempt >= retries:
                    raise Throttled(error)
                delay = 20 * 2 ** attempt
                attempt += 1
                self.notify("Apple is throttling requests; waiting %d s (retry %d of %d)" % (delay, attempt, retries))
                time.sleep(delay)
                continue
            if not soft_retry_used:
                # An unrecognised failure gets one more try before it counts.
                soft_retry_used = True
                time.sleep(8)
                continue
            raise WmasError(error)

    # ---- platform -------------------------------------------------------

    def set_platform(self, platform):
        self.platform = normalize_platform(platform)
        self.config["platform"] = self.platform
        self._save_config()

    @property
    def platform_label(self):
        return PLATFORMS[self.platform]["label"]

    @property
    def os_name(self):
        return PLATFORMS[self.platform]["os"]

    # ---- accounts -------------------------------------------------------

    def profiles(self):
        return {label: dict(entry) for label, entry in self.config["profiles"].items()}

    def account(self, profile=None):
        """Return {'name', 'email'} for a signed-in profile, else None."""
        label = profile or self.profile
        try:
            data = self._ipatool(["auth", "info"], profile=label)
        except NotSignedIn:
            return None
        info = {"name": data.get("name", ""), "email": data.get("email", "")}
        self._entry(label).update(info)
        self._save_config()
        return info

    def use(self, profile):
        self._entry(profile)
        self.profile = profile
        self.config["current"] = profile
        self._save_config()

    def add_profile(self, label):
        """Register a new, signed-out profile with its own isolated storage."""
        self._check_label(label)
        profiles = self.config["profiles"]
        if label.lower() in {name.lower() for name in profiles}:
            raise WmasError("profile %r already exists" % label)
        used = {entry["storage"] for entry in profiles.values()}
        base = safe_name(label).lower()
        storage, suffix = base, 2
        while storage in used:
            storage, suffix = "%s-%d" % (base, suffix), suffix + 1
        profiles[label] = {"storage": storage}
        self._save_config()
        return label

    def rename_profile(self, old, new):
        """Relabel a profile. The stored session is untouched."""
        entry = self._entry(old)
        self._check_label(new)
        profiles = self.config["profiles"]
        if new.lower() in {name.lower() for name in profiles if name != old}:
            raise WmasError("profile %r already exists" % new)
        self.config["profiles"] = {(new if name == old else name): value for name, value in profiles.items()}
        if self.config.get("current") == old:
            self.config["current"] = new
        if self.profile == old:
            self.profile = new
        self._save_config()
        return entry

    def login_command(self, email, profile=None):
        """Command and environment for an interactive sign-in. The caller runs it
        attached to the terminal so the password and 2FA code never pass through here."""
        return [str(IPATOOL), "auth", "login", "-e", email], self._env(profile)

    def forget(self, profile):
        """Sign a profile out and remove it. The profile on ipatool's stock storage
        is only signed out, so there is always somewhere to sign in."""
        entry = self._entry(profile)
        try:
            self._ipatool(["auth", "revoke"], profile=profile)
        except WmasError:
            pass
        profiles = self.config["profiles"]
        if entry["storage"] == "" or len(profiles) == 1:
            profiles[profile] = {"storage": entry["storage"]}
            removed = False
        else:
            del profiles[profile]
            removed = True
            if self.config.get("current") == profile:
                self.config["current"] = sorted(profiles)[0]
            if self.profile == profile:
                self.profile = self.config["current"]
        self._save_config()
        return removed

    # ---- catalogue ------------------------------------------------------

    def _remember_app(self, app):
        self.db.execute(
            "INSERT INTO apps (app_id, bundle_id, name, price, seen_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(app_id) DO UPDATE SET bundle_id=excluded.bundle_id, name=excluded.name, "
            "price=excluded.price, seen_at=excluded.seen_at",
            (app["id"], app.get("bundleID", ""), app.get("name", ""), app.get("price", 0), time.time()))
        self.db.commit()

    def search(self, term, limit=10):
        """Search the account's storefront catalogue."""
        data = self._ipatool(["search", term, "--platform", self.platform, "--limit", str(limit)], paced=True)
        apps = [app for app in data.get("apps", []) if self.platform in app.get("platforms", [self.platform])]
        for app in apps:
            self._remember_app(app)
        return apps

    def owned(self, term=None):
        """Apps the account owns, including ones no longer listed in the store."""
        found, page = [], 1
        while True:
            data = self._ipatool(
                ["list-purchases", "--platform", self.platform, "-l", "100", "-p", str(page)], paced=True)
            apps = data.get("apps", [])
            for app in apps:
                self._remember_app(app)
            found.extend(apps)
            if len(found) >= data.get("totalCount", 0) or not apps:
                break
            page += 1
        if term:
            needle = term.lower()
            found = [a for a in found if needle in (a.get("name", "") + " " + a.get("bundleID", "")).lower()]
        return found

    def app(self, app_id):
        """Cached app record, falling back to the public lookup API for the name."""
        row = self.db.execute("SELECT * FROM apps WHERE app_id=?", (app_id,)).fetchone()
        if row:
            return {"id": row["app_id"], "bundleID": row["bundle_id"], "name": row["name"], "price": row["price"]}
        app = {"id": int(app_id), "bundleID": "", "name": "app-%s" % app_id, "price": None}
        try:
            url = "https://itunes.apple.com/lookup?" + urllib.parse.urlencode({"id": app_id, "entity": PLATFORMS[self.platform]["entity"]})
            with urllib.request.urlopen(url, timeout=15) as reply:
                results = json.load(reply).get("results", [])
            if results:
                app.update(bundleID=results[0].get("bundleId", ""), name=results[0].get("trackName", app["name"]),
                           price=results[0].get("price"))
                self._remember_app(app)
        except Exception:
            pass
        return app

    # ---- versions -------------------------------------------------------

    def version_ids(self, app_id, refresh=False):
        """External version identifiers, oldest first."""
        row = self.db.execute("SELECT * FROM version_lists_v2 WHERE app_id=? AND platform=?",
                              (app_id, self.platform)).fetchone()
        if row and not refresh and time.time() - row["fetched_at"] < VERSION_LIST_TTL:
            return json.loads(row["ids"])
        data = self._ipatool(["list-versions", "-i", str(app_id), "--platform", self.platform], paced=True)
        ids = [str(i) for i in data.get("externalVersionIdentifiers", [])]
        if not ids:
            raise WmasError("the Store returned no version list")
        self.db.execute("INSERT OR REPLACE INTO version_lists_v2 VALUES (?,?,?,?)",
                        (app_id, self.platform, json.dumps(ids), time.time()))
        self.db.commit()
        return ids

    def cached(self, app_id):
        """Probed versions of an app on the current platform, keyed by version ID."""
        rows = self.db.execute("SELECT * FROM versions WHERE app_id=?", (app_id,)).fetchall()
        listed = self.db.execute("SELECT ids FROM version_lists_v2 WHERE app_id=? AND platform=?",
                                 (app_id, self.platform)).fetchone()
        wanted = set(json.loads(listed["ids"])) if listed else None
        return {row["ext_id"]: dict(row) for row in rows if wanted is None or row["ext_id"] in wanted}

    def probe(self, app_id, ext_id, force=False):
        """Version, release date, minimum OS and architectures of one build.

        Successful results never change and are cached for good. A failure is kept
        for a day so one dead build is not retried on every listing, and is never
        treated as proof the build does not exist.
        """
        row = self.db.execute(
            "SELECT * FROM versions WHERE app_id=? AND ext_id=?", (app_id, str(ext_id))).fetchone()
        if row and not force:
            if not row["error"] or time.time() - row["probed_at"] < FAILED_PROBE_TTL:
                return dict(row, from_cache=True)
        record = {"app_id": int(app_id), "ext_id": str(ext_id), "display_version": None, "bundle_version": None,
                  "release_date": None, "min_os": None, "arch": None, "package_size": None, "error": None,
                  "probed_at": time.time()}
        try:
            data = self._ipatool(
                ["get-version-metadata", "-i", str(app_id), "--platform", self.platform,
                 "--external-version-id", str(ext_id)] + (["--probe-only"] if self.platform == "macos" else []),
                paced=True)
            record.update(
                display_version=data.get("displayVersion"), bundle_version=data.get("bundleVersion"),
                release_date=(data.get("releaseDate") or "")[:10], min_os=data.get("minimumOS") or "",
                arch=data.get("architectures") or data.get("deviceFamilies") or "", package_size=data.get("packageSize"))
        except (Throttled, NotSignedIn, LicenseRequired):
            raise
        except WmasError as failure:
            record["error"] = str(failure)
        self.db.execute(
            "INSERT OR REPLACE INTO versions VALUES (:app_id,:ext_id,:display_version,:bundle_version,"
            ":release_date,:min_os,:arch,:package_size,:error,:probed_at)", record)
        self.db.commit()
        return record

    def _usable(self, app_id, ids, index, low, high, progress):
        """Probe ids[index]; if that build is dead, try its neighbours inside (low, high)."""
        for candidate in (index, index + 1, index - 1, index + 2, index - 2, index + 3, index - 3):
            if not low < candidate < high:
                continue
            row = self.probe(app_id, ids[candidate])
            if progress:
                progress(row)
            if not row["error"]:
                return candidate
        return None

    def boundaries(self, app_id, progress=None):
        """Probe just enough builds to locate every minimum-OS change.

        Minimum OS almost never goes down between releases, so two builds with the
        same requirement are taken to bracket builds that share it. That turns a
        full sweep into a few probes per boundary. Builds in between stay unprobed
        until fill() or an explicit probe confirms them.
        """
        ids = self.version_ids(app_id)
        last = len(ids) - 1
        top = self._usable(app_id, ids, last, -1, len(ids), progress)
        if top is None:
            raise WmasError("none of the newest versions could be probed")
        bottom = self._usable(app_id, ids, 0, -1, top, progress) if top > 0 else top
        if bottom is None:
            # The oldest builds are dead: bisect for the oldest one that still answers.
            dead, alive = 0, top
            while alive - dead > 1:
                middle = (dead + alive) // 2
                row = self.probe(app_id, ids[middle])
                if progress:
                    progress(row)
                if row["error"]:
                    dead = middle
                else:
                    alive = middle
            bottom = alive

        def split(low, high):
            if high - low <= 1:
                return
            known = self.cached(app_id)
            if known[ids[low]]["min_os"] == known[ids[high]]["min_os"]:
                return
            middle = self._usable(app_id, ids, (low + high) // 2, low, high, progress)
            if middle is None:
                return
            split(low, middle)
            split(middle, high)

        if bottom < top:
            split(bottom, top)
        # A handful of leftovers is cheaper to probe than to explain.
        if 0 < len(ids) - len(self.cached(app_id)) <= AUTO_FILL_LIMIT:
            self.fill(app_id, progress)
        return ids

    def fill(self, app_id, progress=None, limit=None):
        """Probe every build not yet in the cache, newest first."""
        ids = self.version_ids(app_id)
        known = self.cached(app_id)
        todo = [i for i in reversed(ids) if i not in known or (known[i]["error"] and
                time.time() - known[i]["probed_at"] >= FAILED_PROBE_TTL)]
        for ext_id in todo[:limit]:
            row = self.probe(app_id, ext_id)
            if progress:
                progress(row)
        return ids

    def table(self, app_id):
        """Rows for display, newest first. Unprobed builds carry the requirement
        inferred from their probed neighbours, flagged as such."""
        ids = self.version_ids(app_id)
        known = self.cached(app_id)
        rows, below = [], None
        for ext_id in ids:
            row = known.get(ext_id)
            if row and not row["error"]:
                below = row["min_os"]
                rows.append(dict(row, state="probed"))
            else:
                rows.append({"ext_id": ext_id, "state": "failed" if row else "unprobed",
                             "error": row["error"] if row else None, "_below": below})
        above = None
        for row in reversed(rows):
            if row["state"] == "probed":
                above = row["min_os"]
            else:
                below = row.pop("_below")
                row["min_os_inferred"] = below if below is not None and below == above else None
        rows.reverse()
        return rows

    def newest_for(self, app_id, macos, progress=None):
        """Newest build whose minimum OS is at most `macos`, by bisection."""
        ids = self.version_ids(app_id)
        target = version_key(macos)

        def fits(index):
            row = self.probe(app_id, ids[index])
            if progress:
                progress(row)
            if row["error"]:
                return None
            return version_key(row["min_os"]) <= target

        high = len(ids) - 1
        while high >= 0 and fits(high) is None:
            high -= 1
        if high < 0:
            return None
        if fits(high):
            return self.probe(app_id, ids[high])
        low = 0
        while low < high and fits(low) is None:
            low += 1
        if low >= high or not fits(low):
            return None
        while high - low > 1:
            middle = (low + high) // 2
            verdict = fits(middle)
            step = 1
            while verdict is None and middle + step < high:
                middle += step
                verdict = fits(middle)
            if verdict is None:
                break
            if verdict:
                low = middle
            else:
                high = middle
        return self.probe(app_id, ids[low])

    def history(self, app_id, refresh=False):
        """Recent versions from the public App Store page, newest first.

        This is what the store page advertises, which can be more than the Store
        will actually serve: the commerce API lists only the builds still offered.
        """
        row = self.db.execute("SELECT * FROM histories_v2 WHERE app_id=? AND platform=?",
                              (app_id, self.platform)).fetchone()
        if row and not refresh and time.time() - row["fetched_at"] < HISTORY_TTL:
            return json.loads(row["entries"])
        entries = []
        for country in HISTORY_COUNTRIES:
            try:
                request = urllib.request.Request(
                    "https://apps.apple.com/%s/app/id%d?platform=%s" % (
                        country, int(app_id), PLATFORMS[self.platform]["page"]),
                    headers={"User-Agent": BROWSER_UA})
                with urllib.request.urlopen(request, timeout=20) as reply:
                    page = reply.read().decode("utf-8", "ignore")
            except Exception:
                continue
            seen = set()
            for version, date in re.findall(
                    r'<span[^>]*>\s*([0-9][0-9A-Za-z.\-]*)\s*</span>\s*<time datetime="(\d{4}-\d{2}-\d{2})"', page):
                if version not in seen:
                    seen.add(version)
                    entries.append({"version": version, "date": date})
            if entries:
                break
        if entries:
            entries.sort(key=lambda entry: version_key(entry["version"]), reverse=True)
            self.db.execute("INSERT OR REPLACE INTO histories_v2 VALUES (?,?,?,?)",
                            (app_id, self.platform, json.dumps(entries), time.time()))
            self.db.commit()
        return entries

    def unoffered(self, app_id):
        """Versions the public page lists that the Store does not offer for download.

        Certain for anything older than the oldest offered build; for the rest it
        can only be said once every offered build has been probed.
        """
        ids = self.version_ids(app_id)
        probed = [row for row in self.cached(app_id).values() if not row["error"]]
        if not probed:
            return []
        offered = {version_key(row["display_version"]) for row in probed}
        oldest = min(offered)
        complete = len(probed) == len(ids)
        return [entry for entry in self.history(app_id)
                if version_key(entry["version"]) not in offered
                and (version_key(entry["version"]) < oldest or complete)]

    def find_version(self, app_id, text, progress=None):
        """Resolve an external version identifier or a display version such as
        "8.2.11" to a probed row. Display versions are located by bisection."""
        ids = self.version_ids(app_id)
        text = str(text).strip()
        if text in ids:
            return self.probe(app_id, text)
        for row in self.cached(app_id).values():
            if not row["error"] and version_key(row["display_version"]) == version_key(text):
                return row
        wanted = version_key(text)
        low, high = 0, len(ids) - 1
        while low <= high:
            middle = (low + high) // 2
            usable = self._usable(app_id, ids, middle, low - 1, high + 1, progress)
            if usable is None:
                break
            row = self.probe(app_id, ids[usable])
            if version_key(row["display_version"]) == wanted:
                return row
            if version_key(row["display_version"]) < wanted:
                low = usable + 1
            else:
                high = usable - 1
        return None

    # ---- downloads ------------------------------------------------------

    def purchase(self, app_id):
        """Obtain a licence for a free app."""
        return self._ipatool(["purchase", "-i", str(app_id), "--platform", self.platform], paced=True)

    def download(self, app_id, ext_id, directory=None, show_progress=True):
        """Download one build, then check Apple's signature and record the hash."""
        app = self.app(app_id)
        row = self.probe(app_id, ext_id)
        if row["error"]:
            raise WmasError("version unavailable: %s" % row["error"])
        folder = Path(directory) if directory else ARTIFACTS / safe_name(app["name"]).lower()
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / ("%s-%s%s" % (safe_name(app["name"]), safe_name(row["display_version"]),
                                        PLATFORMS[self.platform]["extension"]))
        self._ipatool(
            ["download", "-i", str(app_id), "--platform", self.platform, "--external-version-id", str(ext_id),
             "-o", str(target)], paced=True, show_progress=show_progress)
        if not target.exists():
            raise WmasError("ipatool reported success but the file is missing: %s" % target)
        digest = hashlib.sha256()
        with open(target, "rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)
        if self.platform == "macos":
            check = subprocess.run(["pkgutil", "--check-signature", str(target)], text=True, capture_output=True)
            status = next((line.split("Status:", 1)[1].strip() for line in check.stdout.splitlines()
                           if "Status:" in line), "signature status unknown")
            verified = "signed by Apple" in status
        else:
            # An IPA has no installer signature to check; confirm it is the build asked for.
            try:
                with zipfile.ZipFile(target) as archive:
                    name = next(n for n in archive.namelist() if re.fullmatch(r"Payload/[^/]+\.app/Info\.plist", n))
                    found = plistlib.loads(archive.read(name)).get("CFBundleShortVersionString")
                verified = version_key(found) == version_key(row["display_version"])
                status = "IPA contains version %s%s" % (found, "" if verified else " — NOT the requested one")
            except Exception as failure:
                verified, status = False, "IPA could not be read: %s" % failure
        result = {"path": str(target), "sha256": digest.hexdigest(), "signature": status, "verified": verified,
                  "signed_by_apple": verified and self.platform == "macos", "size": target.stat().st_size,
                  "version": row["display_version"], "min_os": row["min_os"], "platform": self.platform}
        self.db.execute("INSERT INTO downloads VALUES (?,?,?,?,?,?)",
                        (app_id, str(ext_id), str(target), result["sha256"], status, time.time()))
        self.db.commit()
        sums = {}
        sums_path = folder / "SHA256SUMS"
        if sums_path.exists():
            for line in sums_path.read_text().splitlines():
                parts = line.split(None, 1)
                if len(parts) == 2:
                    sums[parts[1].strip()] = parts[0]
        sums[target.name] = result["sha256"]
        sums_path.write_text("".join("%s  %s\n" % (sums[name], name) for name in sorted(sums)))
        return result
