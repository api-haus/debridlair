#!/usr/bin/env python3
"""Fully configure a fresh Emby server via its HTTP API.

- completes the startup wizard
- creates the admin user
- registers the Premiere key
- creates libraries (TV / Movies / Anime / Torbox raw)
- enables intro marker detection (Detect Episode Intros) on TV libraries
- kicks off a library scan

Reads credentials from ../.env. Safe to re-run.
"""
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE_URL = "http://localhost:8096/emby"
ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
DEVICE_ID = "debrid-setup-001"
CLIENT = ("MediaBrowser Client=\"debrid-setup\", Device=\"setup-script\", "
          f"DeviceId=\"{DEVICE_ID}\", Version=\"1.0.0\"")


def load_env():
    env = {}
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def req(method, path, body=None, token=None, api_key=None, timeout=60):
    url = BASE_URL + path
    if api_key:
        sep = "&" if "?" in url else "?"
        url += f"{sep}api_key={urllib.parse.quote(api_key)}"
    headers = {"X-Emby-Authorization": CLIENT, "Accept": "application/json",
               "User-Agent": "debrid-emby-stack/1.0"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    if token:
        headers["X-Emby-Token"] = token
    r = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:500]
        raise RuntimeError(f"{method} {path} -> {e.code}: {detail}")


def wait_up():
    for i in range(60):
        try:
            return req("GET", "/System/Info/Public")
        except Exception:
            time.sleep(2)
    sys.exit("emby did not come up in 120s")


def run_wizard(env):
    info = wait_up()
    if info.get("StartupWizardCompleted"):
        print("wizard already completed")
        return
    print("running startup wizard...")
    steps = [
        ("/Startup/Configuration", {
            "UICulture": "en-US", "MetadataCountryCode": "US",
            "PreferredMetadataLanguage": "en"}),
        ("/Startup/User", {
            "Name": env["EMBY_ADMIN_USER"],
            "Password": env["EMBY_ADMIN_PASSWORD"]}),
        ("/Startup/RemoteAccess", {
            "EnableRemoteAccess": True, "EnableAutomaticPortMapping": False}),
        ("/Startup/Complete", None),
    ]
    for path, body in steps:
        try:
            req("POST", path, body)
        except RuntimeError as e:
            # startup routes may disappear mid-wizard once the server
            # considers a step satisfied (observed on 4.8.x)
            print(f"[warn] {path}: {e}", file=sys.stderr)
    print("wizard done")


def authenticate(env):
    auth = req("POST", "/Users/AuthenticateByName", {
        "Username": env["EMBY_ADMIN_USER"],
        "Pw": env["EMBY_ADMIN_PASSWORD"]})
    return auth["AccessToken"], auth["User"]["Id"]


def ensure_api_key(token):
    keys = req("GET", "/Auth/Keys", token=token)
    for k in keys.get("Items", []):
        if k.get("AppName") == "debrid-setup":
            return k["AccessToken"]
    req("POST", "/Auth/Keys?app=debrid-setup", {}, token=token)
    keys = req("GET", "/Auth/Keys", token=token)
    for k in keys.get("Items", []):
        if k.get("AppName") == "debrid-setup":
            return k["AccessToken"]
    sys.exit("could not create api key")


def register_premiere(token, key):
    if not key:
        return
    info = req("GET", "/Plugins/SecurityInfo", token=token)
    if info.get("IsMBSupporter") and info.get("SupporterKey") == key:
        print("premiere already registered")
        return
    req("POST", "/Plugins/SecurityInfo",
        {"SupporterKey": key, "IsMBSupporter": False}, token=token)
    info = req("GET", "/Plugins/SecurityInfo", token=token)
    if info.get("IsMBSupporter"):
        print("premiere key registered")
    else:
        print("[warn] premiere key not accepted; enter it in "
              "Dashboard -> Emby Premiere", file=sys.stderr)


def get_libraries(token):
    return req("GET", "/Library/VirtualFolders", token=token)


def add_library(token, name, coll_type, paths):
    existing = {l["Name"]: l for l in get_libraries(token)}
    if name in existing:
        print(f"library '{name}' exists")
        return existing[name]
    q = urllib.parse.urlencode({"name": name, "collectionType": coll_type,
                                "refreshLibrary": "false"})
    req("POST", f"/Library/VirtualFolders?{q}",
        {"Paths": paths, "LibraryOptions": {}}, token=token)
    print(f"library '{name}' created")
    return {l["Name"]: l for l in get_libraries(token)}[name]


def enable_intro_detection(token):
    """Set marker/intro detection options on every TV-type library."""
    libs = get_libraries(token)
    for lib in libs:
        if lib.get("CollectionType") not in ("tvshows", None):
            continue
        opts = lib.get("LibraryOptions", {})
        marker_keys = [k for k in opts if "marker" in k.lower()
                       or "intro" in k.lower()]
        if not marker_keys:
            continue
        changed = False
        for k in marker_keys:
            v = opts[k]
            if isinstance(v, bool) and not v:
                opts[k] = True
                changed = True
            elif isinstance(v, (int, float)) and v == 0:
                opts[k] = 1  # e.g. marker detection mode: scheduled
                changed = True
        if changed:
            q = urllib.parse.urlencode({"refreshLibrary": "false"})
            req("POST", f"/Library/VirtualFolders/LibraryOptions?{q}",
                {"Id": lib["ItemId"], "LibraryOptions": opts}, token=token)
            print(f"intro detection enabled on '{lib['Name']}': "
                  f"{[(k, opts[k]) for k in marker_keys]}")
        else:
            print(f"intro detection already on for '{lib['Name']}' "
                  f"{[(k, opts[k]) for k in marker_keys]}")


def install_strmassistant():
    """Install the StrmAssistant plugin (STRM intro-skip unlock) + config."""
    import os
    dll_url = ("https://github.com/sjtuross/StrmAssistant/releases/download/"
               "v2.0.0.30/StrmAssistant.dll")
    plugins_dir = ENV_FILE.parent / "emby" / "plugins"
    dll_path = plugins_dir / "StrmAssistant.dll"
    if not dll_path.exists():
        print("downloading StrmAssistant...")
        with urllib.request.urlopen(dll_url, timeout=120) as r, \
                open(dll_path, "wb") as f:
            f.write(r.read())
        os.chmod(dll_path, 0o644)
        print("StrmAssistant installed (requires emby restart)")
    cfg_dir = plugins_dir / "configurations"
    cfg_dir.mkdir(exist_ok=True)
    intro_cfg = cfg_dir / "Strm Assistant_IntroSkipOptions.json"
    if not intro_cfg.exists():
        intro_cfg.write_text(json.dumps({
            "UnlockIntroSkip": True,
            "IntroDetectionFingerprintMinutes": 10,
            "EnableIntroSkip": True,
            "MaxIntroDurationSeconds": 150,
            "MaxCreditsDurationSeconds": 360,
            "MinOpeningPlotDurationSeconds": 60,
            "LibraryScope": "",
            "MarkerEnabledLibraryScope": ""}, indent=2))
    main_cfg = cfg_dir / "Strm Assistant.json"
    if not main_cfg.exists():
        main_cfg.write_text(json.dumps({
            "GeneralOptions": {
                "CatchupMode": False,
                "CatchupTaskScope": "MediaInfo,Fingerprint",
                "MaxConcurrentCount": 2,
                "CooldownDurationSeconds": 0,
                "Tier2MaxConcurrentCount": 1}}, indent=2))
    print("StrmAssistant config in place")


def main():
    install_strmassistant()
    env = load_env()
    run_wizard(env)
    token, _uid = authenticate(env)
    register_premiere(token, env.get("EMBY_PREMIERE_KEY", ""))

    api_key = ensure_api_key(token)
    state_dir = ENV_FILE.parent / "sync-state"
    state_dir.mkdir(exist_ok=True)
    (state_dir / "emby_api_key").write_text(api_key)
    print("api key stored in sync-state/emby_api_key")

    add_library(token, "TV Shows", "tvshows", ["/media/library/tv"])
    add_library(token, "Movies", "movies", ["/media/library/movies"])
    add_library(token, "Anime", "tvshows", ["/media/anime"])
    add_library(token, "Torbox (raw)", "mixed", ["/media/torbox"])

    enable_intro_detection(token)

    # general perf/analysis friendly settings
    req("POST", "/System/Configuration", {
        **req("GET", "/System/Configuration", token=token),
        "EnableCaseSensitiveItemIds": True,
        "MetadataRefreshDays": 30,
    }, token=token)

    # kick off a full library scan
    req("POST", "/Library/Refresh", {}, token=token)
    print("library scan started")
    print("done")


if __name__ == "__main__":
    main()
