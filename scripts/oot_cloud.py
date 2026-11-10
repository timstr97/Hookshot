"""Keep an Ocarina of Time save in a GitHub repository of your own, so two PCs can play the same save.

    python -I scripts/oot_cloud.py login                 (once per PC: sign in to GitHub)
    python -I scripts/oot_cloud.py status
    python -I scripts/oot_cloud.py push  [--dry-run] [--force]
    python -I scripts/oot_cloud.py pull  [--dry-run] [--force]
    python -I scripts/oot_cloud.py sync  [--dry-run]     (pull or push, whichever side is behind)
    python -I scripts/oot_cloud.py play -- ./Hookshot.AppImage   (pull, run the game, push when it quits)

Nothing is stored by this project: the save goes into a private repository of your own GitHub account (by default
hookshot-saves, made on first use), and the only credentials are yours. Only the game's own save files are ever sent --
account.sav, option.sav and each slot's progress.sav and caption.sav -- never game files, keys or anything made from a
dump. Every send is one commit, so the repository's history is the backup, and a pull backs up the save it overwrites
first, the way oot_saves.py does. Two PCs cannot overwrite each other: a push that is not a fast-forward is refused,
and so is a pull over local changes that were never pushed (--force decides, and says what it keeps).

Sign-in is GitHub's device flow and needs an OAuth app's client id (--client-id, HOOKSHOT_GITHUB_CLIENT_ID, or
client_id in the config file); `login --help-setup` prints how to make one in two minutes. A fine-grained token works
instead, with no app to register: `login --token` reads it from the terminal or HOOKSHOT_GITHUB_TOKEN.

Settings and the token live beside each other, outside the save folder: $XDG_CONFIG_HOME/hookshot (default
~/.config/hookshot) on Linux, %LOCALAPPDATA%\\Hookshot on Windows. Standard library only; the guide is docs/cloud-saves.md.
"""
import argparse
import base64
import datetime
import getpass
import hashlib
import importlib.util
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("oot_saves", os.path.join(HERE, "oot_saves.py"))
saves = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(saves)   # (python -I has no sibling imports: the tests load this file the same way)

Fail = saves.Fail
WINDOWS = saves.WINDOWS

GITHUB = "https://github.com"
API = "https://api.github.com"
AGENT = "hookshot-oot-cloud/1"
DEFAULT_REPO_NAME = "hookshot-saves"
DEFAULT_BRANCH = "main"
SCOPE = "repo"            # (a private repository is not in public_repo, and the saves repository is private)
MANIFEST = "cloud.json"
FORMAT = 1
LOOSE = ("account.sav", "option.sav")
IN_SLOT = ("progress.sav", "caption.sav")
MAX_FILE = 8 * 1024 * 1024     # a save file is a few KB: anything near this is not one
MAX_TOTAL = 64 * 1024 * 1024
POLL_CAP = 15 * 60

SETUP = """To sign in with the device flow, make a GitHub OAuth app -- it is free, instant and needs no review:

  1. Open https://github.com/settings/developers -> OAuth Apps -> New OAuth App
  2. Any name ("Hookshot saves on my PCs"), any homepage URL, no callback URL needed
  3. Create it, then on its page turn on "Enable Device Flow"
  4. Copy the Client ID (it is not a secret) and either pass it once

       python -I scripts/oot_cloud.py login --client-id <id>

     or put it in the config file as client_id (%s)

The app is yours, so nobody else can revoke it for you. If you would rather register nothing, use a fine-grained
token instead -- Settings -> Developer settings -> Personal access tokens -> Fine-grained tokens, with Repository
permissions -> Contents: Read and write (and Administration: Read and write if this script should create the
repository for you) -- and pass it with

       python -I scripts/oot_cloud.py login --token
"""


def config_dir():
    if WINDOWS:
        base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
        return os.path.join(base, "Hookshot")
    return os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.join(os.path.expanduser("~"), ".config"),
                        "hookshot")


def config_path():
    return os.path.join(config_dir(), "cloud.json")


def token_path():
    return os.path.join(config_dir(), "cloud-token")


def read_config():
    try:
        with open(config_path(), encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        return {}
    return cfg if isinstance(cfg, dict) else {}


def write_config(cfg):
    os.makedirs(config_dir(), exist_ok=True)
    tmp = config_path() + ".new"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, config_path())


def read_token():
    env = os.environ.get("HOOKSHOT_GITHUB_TOKEN")
    if env:
        return env.strip()
    try:
        with open(token_path(), encoding="utf-8") as f:
            return f.read().strip() or None
    except OSError:
        return None


def write_token(token):
    os.makedirs(config_dir(), exist_ok=True)
    path = token_path()
    tmp = path + ".new"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(token.strip() + "\n")
    os.replace(tmp, path)
    if not WINDOWS:
        os.chmod(path, 0o600)


def device_id(cfg):
    """This PC, as the manifest names it: a name to read and an id that stays the same after a rename."""
    if not cfg.get("device_id"):
        cfg["device_id"] = uuid.uuid4().hex[:16]
        cfg.setdefault("device_name", socket.gethostname() or "this PC")
        write_config(cfg)
    return cfg["device_id"], cfg.get("device_name") or "this PC"


def request(method, url, token=None, data=None, form=None, accept="application/vnd.github+json"):
    """One HTTP call to GitHub, as (status, parsed body). Every call in this script goes through here, which is what
    the tests replace."""
    headers = {"Accept": accept, "User-Agent": AGENT}
    body = None
    if form is not None:
        body = urllib.parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    elif data is not None:
        body = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token
        headers["X-GitHub-Api-Version"] = "2022-11-28"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            status = r.status
    except urllib.error.HTTPError as e:
        raw, status = e.read(), e.code
    except urllib.error.URLError as e:
        raise Fail("cannot reach %s (%s)" % (urllib.parse.urlsplit(url).netloc, e.reason))
    try:
        return status, json.loads(raw.decode("utf-8")) if raw else {}
    except ValueError:
        return status, {"raw": raw.decode("utf-8", "replace")}


def explain(status, body, what):
    """GitHub's answer as something to read. Raises; never returns."""
    message = body.get("message") if isinstance(body, dict) else None
    if status == 401:
        raise Fail("GitHub refused the token (401 %s): sign in again with `login`" % (message or ""))
    if status == 403 and "rate limit" in (message or "").lower():
        raise Fail("GitHub's rate limit is reached: wait a few minutes")
    if status == 403:
        raise Fail("the token may not do that (403 %s): a fine-grained token needs Contents: Read and write on the "
                   "saves repository" % (message or ""))
    raise Fail("GitHub said %d %s (%s)" % (status, message or "", what))


def api(method, path, token, data=None, ok=(200, 201)):
    status, body = request(method, API + path, token=token, data=data)
    if status in ok:
        return body
    explain(status, body, path)


# ---------------------------------------------------------------- signing in

def login_device(client_id, print_=print):
    status, body = request("POST", GITHUB + "/login/device/code", accept="application/json",
                           form={"client_id": client_id, "scope": SCOPE})
    if status != 200 or "device_code" not in body:
        hint = body.get("error_description") or body.get("error") or ""
        if "device" in str(hint).lower() or status == 422:
            raise Fail("the OAuth app has no device flow turned on: open its page on GitHub and tick "
                       "\"Enable Device Flow\" (%s)" % hint)
        raise Fail("GitHub would not start the sign-in (%d %s)" % (status, hint))
    interval = max(int(body.get("interval") or 5), 1)
    print_("Open %s and enter this code:\n\n    %s\n" % (body.get("verification_uri") or GITHUB + "/login/device",
                                                         body["user_code"]))
    print_("Waiting for GitHub...")
    deadline = time.monotonic() + min(int(body.get("expires_in") or 900), POLL_CAP)
    while time.monotonic() < deadline:
        time.sleep(interval)
        status, got = request("POST", GITHUB + "/login/oauth/access_token", accept="application/json",
                              form={"client_id": client_id, "device_code": body["device_code"],
                                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code"})
        error = got.get("error")
        if got.get("access_token"):
            return got["access_token"]
        if error == "authorization_pending":
            continue
        if error == "slow_down":
            interval = max(int(got.get("interval") or interval + 5), interval + 5)
            continue
        if error == "expired_token":
            raise Fail("the code expired: run `login` again")
        if error == "access_denied":
            raise Fail("the sign-in was turned down on GitHub")
        raise Fail("GitHub would not finish the sign-in (%s)" % (got.get("error_description") or error or status))
    raise Fail("GitHub did not answer in time: run `login` again")


def cmd_login(args):
    cfg = read_config()
    if args.help_setup:
        print(SETUP % config_path())
        return
    if args.token:
        token = os.environ.get("HOOKSHOT_GITHUB_TOKEN") or getpass.getpass("Paste the token (it is not shown): ")
        if not token.strip():
            raise Fail("no token given")
    else:
        client_id = args.client_id or os.environ.get("HOOKSHOT_GITHUB_CLIENT_ID") or cfg.get("client_id")
        if not client_id:
            raise Fail("no client id for the device flow. `login --help-setup` prints how to make an OAuth app, or "
                       "use `login --token` with a fine-grained token and register nothing")
        token = login_device(client_id)
        cfg["client_id"] = client_id
    who = api("GET", "/user", token)
    cfg["login"] = who.get("login")
    if args.repo:
        cfg["repo"] = args.repo
    write_config(cfg)
    write_token(token)
    print("Signed in as %s. The token is in %s (only you can read it)." % (who.get("login"), token_path()))
    print("Next: `python -I scripts/oot_cloud.py push` sends this PC's save to %s." % repo_name(cfg, None))


def cmd_logout(args):
    gone = []
    for path in (token_path(),):
        if os.path.exists(path):
            os.remove(path)
            gone.append(path)
    print("Removed %s." % ", ".join(gone) if gone else "No token was stored.")
    print("The save in your repository is untouched; `login` signs in again.")


# ---------------------------------------------------------------- the repository

def repo_name(cfg, override):
    name = override or cfg.get("repo") or DEFAULT_REPO_NAME
    if "/" in name:
        return name
    owner = cfg.get("login")
    return "%s/%s" % (owner, name) if owner else name


def split_repo(full):
    if "/" not in full:
        raise Fail("the repository is %s: give it as owner/name with --repo, or sign in with `login` so the owner is "
                   "known" % full)
    owner, _, name = full.partition("/")
    return owner, name


def ensure_repo(full, token, branch, dry_run):
    """The repository, made private on first use if it is not there yet; returns its default branch's name."""
    owner, name = split_repo(full)
    status, body = request("GET", "%s/repos/%s/%s" % (API, owner, name), token=token)
    if status == 200:
        return body.get("default_branch") or branch
    if status != 404:
        return api("GET", "/repos/%s/%s" % (owner, name), token)  # (raises with GitHub's own message)
    if dry_run:
        print("Would make the private repository %s" % full)
        return branch
    who = api("GET", "/user", token)
    if (who.get("login") or "").lower() != owner.lower():
        raise Fail("there is no repository %s, and it is not yours to make (you are %s): make it on GitHub as a "
                   "private repository, or point --repo at one you own" % (full, who.get("login")))
    made = api("POST", "/user/repos", token, data={"name": name, "private": True, "auto_init": True,
                                                   "description": "Ocarina of Time saves (Hookshot)"})
    print("Made the private repository %s" % made.get("full_name", full))
    return made.get("default_branch") or branch


def head_commit(full, token, branch):
    """(commit sha, tree sha) of the branch, or (None, None) when the branch has nothing yet."""
    owner, name = split_repo(full)
    status, body = request("GET", "%s/repos/%s/%s/git/ref/heads/%s" % (API, owner, name, branch), token=token)
    if status == 404 or status == 409:          # (409: an empty repository has no branch at all)
        return None, None
    if status != 200:
        explain(status, body, "%s on %s" % (branch, full))
    sha = body["object"]["sha"]
    commit = api("GET", "/repos/%s/%s/git/commits/%s" % (owner, name, sha), token)
    return sha, commit["tree"]["sha"]


def remote_tree(full, token, tree_sha, prefix):
    """{path under prefix: blob sha} for the save folder in the commit's tree."""
    if tree_sha is None:
        return {}
    owner, name = split_repo(full)
    body = api("GET", "/repos/%s/%s/git/trees/%s?recursive=1" % (owner, name, tree_sha), token)
    out = {}
    for entry in body.get("tree") or []:
        path = entry.get("path") or ""
        if entry.get("type") == "blob" and path.startswith(prefix + "/"):
            out[path[len(prefix) + 1:]] = entry["sha"]
    if body.get("truncated"):
        raise Fail("the repository's tree is too big to read in one go: it holds more than the saves")
    return out


def read_blob(full, token, sha):
    owner, name = split_repo(full)
    body = api("GET", "/repos/%s/%s/git/blobs/%s" % (owner, name, sha), token)
    if body.get("encoding") != "base64":
        raise Fail("unexpected blob encoding %s" % body.get("encoding"))
    return base64.b64decode(body.get("content") or "")


def write_commit(full, token, branch, base_commit, base_tree, changes, message):
    """One commit holding changes ({path: bytes or None to delete}); the branch moves only if nobody else pushed."""
    owner, name = split_repo(full)
    entries = []
    for path, content in sorted(changes.items()):
        if content is None:
            entries.append({"path": path, "mode": "100644", "type": "blob", "sha": None})
            continue
        blob = api("POST", "/repos/%s/%s/git/blobs" % (owner, name), token,
                   data={"content": base64.b64encode(content).decode("ascii"), "encoding": "base64"})
        entries.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
    tree_body = {"tree": entries}
    if base_tree:
        tree_body["base_tree"] = base_tree
    tree = api("POST", "/repos/%s/%s/git/trees" % (owner, name), token, data=tree_body)
    commit_body = {"message": message, "tree": tree["sha"]}
    if base_commit:
        commit_body["parents"] = [base_commit]
    commit = api("POST", "/repos/%s/%s/git/commits" % (owner, name), token, data=commit_body)
    ref = "/repos/%s/%s/git/refs/heads/%s" % (owner, name, branch)
    status, body = request("PATCH", API + ref, token=token, data={"sha": commit["sha"], "force": False})
    if status == 422 and base_commit is None:
        api("POST", "/repos/%s/%s/git/refs" % (owner, name), token,
            data={"ref": "refs/heads/" + branch, "sha": commit["sha"]})
        return commit["sha"]
    if status == 422:
        raise Fail("another PC pushed while this one was sending: run `pull` (or `sync`) and try again")
    if status != 200:
        explain(status, body, "moving %s on %s" % (branch, full))
    return commit["sha"]


# ---------------------------------------------------------------- the save folder

def save_files(folder):
    """{relative path: bytes} for the game's own save files in folder, and nothing else that happens to be there."""
    saves.refuse_links(folder)
    out = {}
    for n in LOOSE:
        p = os.path.join(folder, n)
        if os.path.isfile(p):
            out[n] = p
    for s in sorted(saves.slots(folder)):
        for n in IN_SLOT:
            p = os.path.join(folder, s, n)
            if os.path.isfile(p):
                out["%s/%s" % (s, n)] = p
    files, total = {}, 0
    for rel, path in out.items():
        size = os.path.getsize(path)
        if size > MAX_FILE:
            raise Fail("%s is %d bytes, far more than a save file: nothing was sent" % (path, size))
        total += size
        if total > MAX_TOTAL:
            raise Fail("the save folder holds more than %d MB of save files: nothing was sent" % (MAX_TOTAL >> 20))
        with open(path, "rb") as f:
            files[rel] = f.read()
    return files


def digests(files):
    return {rel: hashlib.sha256(data).hexdigest() for rel, data in sorted(files.items())}


def manifest_of(files, generation, dev_id, dev_name):
    return {"format": FORMAT, "generation": generation, "device": dev_id, "device_name": dev_name,
            "saved": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "files": {rel: {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                      for rel, data in sorted(files.items())}}


def state_key(full, prefix):
    return "%s:%s" % (full, prefix)


def last_synced(cfg, full, prefix):
    """What this PC pushed or pulled last: (generation, {path: sha256}). Zero when it never synced."""
    entry = (cfg.get("synced") or {}).get(state_key(full, prefix)) or {}
    return int(entry.get("generation") or 0), dict(entry.get("files") or {})


def remember_sync(cfg, full, prefix, generation, files):
    cfg.setdefault("synced", {})[state_key(full, prefix)] = {"generation": generation, "files": digests(files)}
    write_config(cfg)


def local_save(args, make=False):
    """(the save folder, the id of the profile it belongs to). The folder is Hookshot's own, found the way
    oot_saves.py finds it; a PC that has never saved has none, and only `--user <id>` says which to make."""
    try:
        _, found = saves.locate(saves.HOOKSHOT, args.hookshot, "--hookshot")
    except Fail:
        if not (make and args.user):
            raise
        base = args.hookshot or (saves.folders(saves.HOOKSHOT) or [None])[0]
        if not base:
            raise Fail("name the game folder with --hookshot: Hookshot keeps its saves in it on Windows")
        path = os.path.join(base, "save", args.user)
        if not args.dry_run:
            os.makedirs(path, exist_ok=True)
        return path, args.user
    uid, path = saves.pick(found, "Hookshot", "--user <the user id folder>", args.user)
    return path, uid


def has_progress(folder):
    """Whether this save holds anything that could be lost: a save with no slots has nothing played in it yet."""
    return bool(saves.slots(folder))


def backup_local(folder, args, what):
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    base = os.path.join(args.backup_dir or saves.default_backup_dir(), "cloud-%s-%s" % (what, stamp))
    target, n = base, 1
    while os.path.exists(target):
        n += 1
        target = "%s-%d" % (base, n)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    shutil.copytree(folder, target)
    return target


def refuse_running(args):
    if args.force or args.dry_run:
        return
    busy = saves.running_games()
    if busy:
        raise Fail("close %s first (or pass --force): a running game writes its save again on exit"
                   % " and ".join(busy))


# ---------------------------------------------------------------- what the two sides hold

class Side:
    def __init__(self, full, token, branch, prefix):
        self.full, self.token, self.branch, self.prefix = full, token, branch, prefix
        self.commit, self.tree = head_commit(full, token, branch)
        self.blobs = remote_tree(full, token, self.tree, prefix)
        self.manifest = {}
        if MANIFEST in self.blobs:
            try:
                self.manifest = json.loads(read_blob(full, token, self.blobs[MANIFEST]).decode("utf-8"))
            except ValueError:
                raise Fail("%s/%s in %s is not readable: fix or delete it on GitHub" % (prefix, MANIFEST, full))

    @property
    def generation(self):
        return int(self.manifest.get("generation") or 0)

    @property
    def files(self):
        return {rel: info.get("sha256") for rel, info in (self.manifest.get("files") or {}).items()}

    def exists(self):
        return bool(self.manifest)

    def describe(self):
        if not self.exists():
            return "nothing yet"
        return "generation %d, saved %s by %s" % (self.generation, self.manifest.get("saved", "?"),
                                                  self.manifest.get("device_name") or self.manifest.get("device", "?"))

    def download(self):
        out = {}
        for rel, sha in sorted(self.blobs.items()):
            if rel != MANIFEST:
                out[rel] = read_blob(self.full, self.token, sha)
        return out


def open_side(args, cfg, token):
    full = repo_name(cfg, args.repo)
    branch = args.branch or cfg.get("branch") or DEFAULT_BRANCH
    folder, uid = local_save(args, make=args.command in ("pull", "sync", "play"))
    prefix = "saves/" + (args.remote_name or cfg.get("remote_name") or uid)
    branch = ensure_repo(full, token, branch, args.dry_run) if args.command != "status" else branch
    return folder, uid, full, branch, prefix, Side(full, token, branch, prefix)


def need_token():
    token = read_token()
    if not token:
        raise Fail("not signed in: run `login` (or `login --help-setup` to see what it needs)")
    return token


# ---------------------------------------------------------------- the commands

def cmd_status(args):
    cfg = read_config()
    token = need_token()
    folder, uid, full, branch, prefix, side = open_side(args, cfg, token)
    local = save_files(folder)
    gen, synced = last_synced(cfg, full, prefix)
    print("This PC:     %s (profile %s), %d save file(s)" % (folder, uid, len(local)))
    print("Repository:  %s, branch %s, folder %s" % (full, branch, prefix))
    print("In the cloud: %s" % side.describe())
    print("Last synced here: %s" % ("generation %d" % gen if gen else "never"))
    changed = digests(local) != synced
    ahead = side.generation > gen
    if not side.exists():
        print("\nNothing is in the cloud yet: `push` sends this save.")
    elif changed and ahead:
        print("\nBoth sides moved since this PC last synced: `pull --force` keeps the cloud's save (this one is backed "
              "up first), `push --force` keeps this PC's.")
    elif changed:
        print("\nThis PC's save is newer: `push`.")
    elif ahead:
        print("\nThe cloud's save is newer: `pull`.")
    else:
        print("\nBoth sides agree.")


def cmd_push(args):
    cfg = read_config()
    token = need_token()
    refuse_running(args)
    folder, uid, full, branch, prefix, side = open_side(args, cfg, token)
    local = save_files(folder)
    if not local:
        raise Fail("%s holds no save files yet: start the game there and save once" % folder)
    gen, synced = last_synced(cfg, full, prefix)
    if side.exists() and side.generation > gen and not args.force:
        raise Fail("the cloud has generation %d and this PC last synced %s: that save was made on another PC. Run "
                   "`pull` to take it, or `push --force` to replace it (its commit stays in the repository's history)"
                   % (side.generation, ("generation %d" % gen) if gen else "nothing"))
    if digests(local) == side.files and side.exists():
        print("The cloud already has this save (generation %d): nothing to send." % side.generation)
        remember_sync(cfg, full, prefix, side.generation, local)
        return
    generation = max(side.generation, gen) + 1
    dev_id, dev_name = device_id(cfg)
    changes = {"%s/%s" % (prefix, rel): data for rel, data in local.items()}
    for rel in side.blobs:
        if rel != MANIFEST and rel not in local:
            changes["%s/%s" % (prefix, rel)] = None
    changes["%s/%s" % (prefix, MANIFEST)] = (json.dumps(manifest_of(local, generation, dev_id, dev_name),
                                                        indent=2, sort_keys=True) + "\n").encode()
    print("%s %d save file(s) from %s to %s (%s, generation %d)"
          % ("Would send" if args.dry_run else "Sending", len(local), folder, full, prefix, generation))
    for rel in sorted(local):
        print("  %s" % rel)
    gone = [p for p, c in changes.items() if c is None]
    for p in gone:
        print("  (removing %s)" % p[len(prefix) + 1:])
    if args.dry_run:
        print("Dry run: nothing was changed")
        return
    sha = write_commit(full, token, branch, side.commit, side.tree, changes,
                       "saves: %s from %s (generation %d)" % (prefix.split("/", 1)[-1], dev_name, generation))
    remember_sync(cfg, full, prefix, generation, local)
    print("Done: commit %s on %s." % (sha[:8], branch))


def cmd_pull(args):
    cfg = read_config()
    token = need_token()
    refuse_running(args)
    folder, uid, full, branch, prefix, side = open_side(args, cfg, token)
    if not side.exists():
        raise Fail("nothing is in %s/%s yet: `push` from the PC that has the save" % (full, prefix))
    local = save_files(folder)
    gen, synced = last_synced(cfg, full, prefix)
    if (local and has_progress(folder) and digests(local) != synced and digests(local) != side.files
            and not args.force):
        raise Fail("this PC's save was changed since it last synced and those changes are in no repository: `push` "
                   "them first, or `pull --force` to take the cloud's save (this one is backed up first)")
    if digests(local) == side.files:
        print("This PC already has the cloud's save (generation %d): nothing to take." % side.generation)
        remember_sync(cfg, full, prefix, side.generation, local)
        return
    remote = side.download()
    for rel in remote:
        if rel not in LOOSE and not (rel.count("/") == 1 and rel.startswith("slot_")
                                     and rel.split("/", 1)[1] in IN_SLOT):
            raise Fail("%s/%s in the repository is not a save file: nothing was written" % (prefix, rel))
    print("%s %d save file(s) from %s (%s, generation %d) to %s"
          % ("Would take" if args.dry_run else "Taking", len(remote), full, prefix, side.generation, folder))
    for rel in sorted(remote):
        print("  %s" % rel)
    if args.dry_run:
        print("Dry run: nothing was changed")
        return
    kept = backup_local(folder, args, "pull") if local else None
    for s in saves.slots(folder):
        shutil.rmtree(os.path.join(folder, s))
    for rel, data in sorted(remote.items()):
        path = os.path.join(folder, *rel.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
    remember_sync(cfg, full, prefix, side.generation, remote)
    print("Done." + (" The save that was here is in %s" % kept if kept else ""))


def cmd_sync(args):
    cfg = read_config()
    token = need_token()
    refuse_running(args)
    folder, uid, full, branch, prefix, side = open_side(args, cfg, token)
    local = save_files(folder)
    gen, synced = last_synced(cfg, full, prefix)
    changed = bool(local) and has_progress(folder) and digests(local) != synced
    ahead = side.generation > gen
    if not side.exists():
        return cmd_push(args)
    if digests(local) == side.files:
        print("Both sides agree (generation %d)." % side.generation)
        remember_sync(cfg, full, prefix, side.generation, local)
        return
    if changed and ahead:
        raise Fail("both sides moved since this PC last synced: `push --force` keeps this PC's save, `pull --force` "
                   "keeps the cloud's (and backs this one up). Nothing was changed")
    if changed:
        return cmd_push(args)
    return cmd_pull(args)


def cmd_play(args):
    if not args.game:
        raise Fail("name the game after --, for example: play -- ./Hookshot.AppImage")
    args.force = False
    try:
        cmd_sync(args)
    except Fail as e:
        print("error: %s" % e, file=sys.stderr)
        raise Fail("the save was not synced, so the game was not started")
    print("\nStarting %s\n" % " ".join(args.game))
    code = subprocess.call(args.game)
    print("\nThe game stopped (exit code %d). Sending the save." % code)
    cmd_sync(args)
    return code


COMMANDS = {"login": cmd_login, "logout": cmd_logout, "status": cmd_status, "push": cmd_push, "pull": cmd_pull,
            "sync": cmd_sync, "play": cmd_play}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    game = []
    if "--" in argv:                      # (everything after -- is the game's own command line, not ours)
        cut = argv.index("--")
        argv, game = argv[:cut], argv[cut + 1:]
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], prog="oot_cloud.py",
                                epilog="play takes the game after --: play -- ./Hookshot.AppImage")
    p.add_argument("command", choices=sorted(COMMANDS), help="login, status, push, pull, sync, play or logout")
    p.add_argument("--repo", metavar="OWNER/NAME", help="the repository to keep the save in (default %s of your "
                                                        "account)" % DEFAULT_REPO_NAME)
    p.add_argument("--branch", metavar="NAME", help="the branch in it (default %s)" % DEFAULT_BRANCH)
    p.add_argument("--remote-name", metavar="NAME",
                   help="the folder in the repository (default this PC's user id, so pin it when two PCs differ)")
    p.add_argument("--hookshot", metavar="DIR", help="Hookshot's data folder (Windows: the game folder)")
    p.add_argument("--user", metavar="ID", help="which user id folder to sync, when there are several")
    p.add_argument("--backup-dir", metavar="DIR", help="where a pull puts the save it replaces")
    p.add_argument("--client-id", metavar="ID", help="for login: the OAuth app's client id")
    p.add_argument("--token", action="store_true", help="for login: paste a fine-grained token instead")
    p.add_argument("--help-setup", action="store_true", help="for login: print how to set GitHub up")
    p.add_argument("--dry-run", action="store_true", help="say what would happen and change nothing")
    p.add_argument("--force", action="store_true", help="push over, or pull over, a save made somewhere else")
    args = p.parse_args(argv)
    args.game = game
    if game and args.command != "play":
        p.error("%s takes nothing after --" % args.command)
    try:
        out = COMMANDS[args.command](args)
    except Fail as e:
        print("error: %s" % e, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
        return 130
    return out if isinstance(out, int) else 0


if __name__ == "__main__":
    sys.exit(main())
