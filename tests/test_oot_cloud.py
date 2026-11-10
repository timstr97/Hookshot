"""scripts/oot_cloud.py, network-free: a fake GitHub in memory stands in for the one HTTP function, so the push, the
pull, the conflicts, the device-flow sign-in and the guards are all checked on temp folders. No account, no token and
no socket are needed.

python -I -B tests/test_oot_cloud.py
"""
import base64
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import urllib.parse

RUN_ALL_SKIP = {
    "windows": "the save folder discovery it builds on is the Linux one (on Windows the saves are in the game"
               " folder: --hookshot)",
}

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("oot_cloud", ROOT / "scripts" / "oot_cloud.py")
cloud = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cloud)

OWNER = "player"
REPO = "%s/hookshot-saves" % OWNER
UID = "0123456789abcdef0123456789abcdef"
OTHER_UID = "fedcba9876543210fedcba9876543210"


def sha_of(kind, payload):
    return hashlib.sha1(("%s\0" % kind).encode() + payload).hexdigest()


class FakeGitHub:
    """Enough of GitHub's git data API to commit to a branch and read it back: blobs, trees, commits and one ref per
    branch, with the fast-forward check that stops two PCs overwriting each other."""

    def __init__(self, login=OWNER):
        self.login = login
        self.repos = {}
        self.blobs = {}
        self.trees = {}
        self.commits = {}
        self.calls = []
        self.device = None
        self.authorized = False
        self.token = "gho_faketoken"

    # -- helpers -------------------------------------------------------
    def make_repo(self, full, private=True, empty=True):
        self.repos[full] = {"full_name": full, "private": private, "default_branch": "main", "branches": {}}
        if not empty:
            tree = self._put_tree({})
            self.repos[full]["branches"]["main"] = self._put_commit(tree, [])
        return self.repos[full]

    def _put_tree(self, mapping):
        sha = sha_of("tree", json.dumps(mapping, sort_keys=True).encode())
        self.trees[sha] = dict(mapping)
        return sha

    def _put_commit(self, tree, parents):
        sha = sha_of("commit", json.dumps([tree, parents], sort_keys=True).encode())
        self.commits[sha] = {"tree": tree, "parents": list(parents)}
        return sha

    def files_on(self, full, branch="main"):
        """{path: bytes} of the branch's tip."""
        head = self.repos[full]["branches"].get(branch)
        if head is None:
            return {}
        return {p: self.blobs[s] for p, s in self.trees[self.commits[head]["tree"]].items()}

    def manifest_on(self, full, prefix, branch="main"):
        raw = self.files_on(full, branch).get("%s/%s" % (prefix, cloud.MANIFEST))
        return json.loads(raw.decode()) if raw else None

    def history(self, full, branch="main"):
        out, head = [], self.repos[full]["branches"].get(branch)
        while head:
            out.append(head)
            head = (self.commits[head]["parents"] or [None])[0]
        return out

    # -- the one entry point oot_cloud uses ----------------------------
    def request(self, method, url, token=None, data=None, form=None, accept=None):
        parts = urllib.parse.urlsplit(url)
        path = parts.path
        self.calls.append((method, path))
        if parts.netloc == "github.com":
            return self._oauth(path, form)
        if token != self.token:
            return 401, {"message": "Bad credentials"}
        if path == "/user" and method == "GET":
            return 200, {"login": self.login}
        if path == "/user/repos" and method == "POST":
            if ("%s/%s" % (self.login, data["name"])) in self.repos:
                return 422, {"message": "name already exists on this account"}
            repo = self.make_repo("%s/%s" % (self.login, data["name"]), private=data.get("private", False),
                                  empty=not data.get("auto_init"))
            return 201, repo
        bits = path.strip("/").split("/")
        if bits[0] != "repos" or len(bits) < 3:
            return 404, {"message": "Not Found"}
        full = "%s/%s" % (bits[1], bits[2])
        rest = bits[3:]
        if full not in self.repos:
            return 404, {"message": "Not Found"}
        repo = self.repos[full]
        if not rest:
            return 200, repo
        if rest[:1] != ["git"]:
            return 404, {"message": "Not Found"}
        return self._git(method, repo, rest[1:], data, parts.query)

    def _oauth(self, path, form):
        if path == "/login/device/code":
            self.device = {"device_code": "dc-1", "user_code": "ABCD-1234",
                           "verification_uri": "https://github.com/login/device", "interval": 0, "expires_in": 900}
            return 200, dict(self.device)
        if path == "/login/oauth/access_token":
            if form.get("device_code") != (self.device or {}).get("device_code"):
                return 200, {"error": "incorrect_device_code"}
            if not self.authorized:
                return 200, {"error": "authorization_pending"}
            return 200, {"access_token": self.token, "token_type": "bearer", "scope": cloud.SCOPE}
        return 404, {"message": "Not Found"}

    def _git(self, method, repo, rest, data, query):
        branches = repo["branches"]
        if rest[:2] == ["ref", "heads"] and method == "GET":
            name = "/".join(rest[2:])
            if name not in branches:
                return 404 if branches else 409, {"message": "Not Found"}
            return 200, {"ref": "refs/heads/" + name, "object": {"sha": branches[name], "type": "commit"}}
        if rest[:1] == ["commits"] and method == "GET":
            sha = rest[1]
            if sha not in self.commits:
                return 404, {"message": "Not Found"}
            return 200, {"sha": sha, "tree": {"sha": self.commits[sha]["tree"]}}
        if rest[:1] == ["trees"] and method == "GET":
            sha = rest[1]
            if sha not in self.trees:
                return 404, {"message": "Not Found"}
            entries = [{"path": p, "type": "blob", "mode": "100644", "sha": s}
                       for p, s in sorted(self.trees[sha].items())]
            return 200, {"sha": sha, "tree": entries, "truncated": False}
        if rest[:1] == ["blobs"] and method == "GET":
            sha = rest[1]
            if sha not in self.blobs:
                return 404, {"message": "Not Found"}
            return 200, {"sha": sha, "encoding": "base64",
                         "content": base64.b64encode(self.blobs[sha]).decode("ascii")}
        if rest == ["blobs"] and method == "POST":
            raw = base64.b64decode(data["content"])
            sha = sha_of("blob", raw)
            self.blobs[sha] = raw
            return 201, {"sha": sha}
        if rest == ["trees"] and method == "POST":
            mapping = dict(self.trees.get(data.get("base_tree"), {}))
            for entry in data["tree"]:
                if entry.get("sha") is None:
                    mapping.pop(entry["path"], None)
                else:
                    mapping[entry["path"]] = entry["sha"]
            return 201, {"sha": self._put_tree(mapping)}
        if rest == ["commits"] and method == "POST":
            return 201, {"sha": self._put_commit(data["tree"], data.get("parents") or [])}
        if rest == ["refs"] and method == "POST":
            name = data["ref"].split("refs/heads/", 1)[-1]
            if name in branches:
                return 422, {"message": "Reference already exists"}
            branches[name] = data["sha"]
            return 201, {"ref": data["ref"], "object": {"sha": data["sha"]}}
        if rest[:2] == ["refs", "heads"] and method == "PATCH":
            name = "/".join(rest[2:])
            if name not in branches:
                return 422, {"message": "Reference does not exist"}
            new = data["sha"]
            if not data.get("force") and branches[name] not in self.commits[new]["parents"]:
                return 422, {"message": "Update is not a fast forward"}
            branches[name] = new
            return 200, {"object": {"sha": new}}
        return 404, {"message": "Not Found"}


def make_save(folder, tag, slots=("slot_00", "slot_01")):
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "account.sav").write_bytes(b"account-" + tag)
    (folder / "option.sav").write_bytes(b"option-" + tag)
    for s in slots:
        (folder / s).mkdir(exist_ok=True)
        (folder / s / "progress.sav").write_bytes(b"progress-" + tag + s.encode())
        (folder / s / "caption.sav").write_bytes(b"caption-" + tag + s.encode())


def tree_of(folder):
    return {str(p.relative_to(folder)): p.read_bytes() for p in sorted(Path(folder).rglob("*")) if p.is_file()}


class PC:
    """One PC: its own HOME, so its own save folder, config and token."""

    def __init__(self, root, name, uid=UID):
        self.root = Path(root)
        self.name = name
        self.uid = uid
        self.data = self.root / "data"
        self.config = self.root / "config"
        self.save = self.data / "hookshot" / "save" / uid
        self.env = {"HOME": str(self.root), "XDG_DATA_HOME": str(self.data), "XDG_CONFIG_HOME": str(self.config),
                    "HOOKSHOT_GITHUB_TOKEN": "", "HOOKSHOT_GITHUB_CLIENT_ID": ""}

    @contextlib.contextmanager
    def at(self):
        with mock.patch.dict(os.environ, self.env, clear=False):
            for empty in ("HOOKSHOT_GITHUB_TOKEN", "HOOKSHOT_GITHUB_CLIENT_ID"):
                os.environ.pop(empty, None)
            yield self

    def sign_in(self, hub):
        with self.at():
            cloud.write_token(hub.token)
            cfg = cloud.read_config()
            cfg.update({"login": hub.login, "device_name": self.name})
            cloud.write_config(cfg)

    def run(self, *argv):
        with self.at():
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
                code = cloud.main(list(argv))
            return code, out.getvalue()


class CloudTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.hub = FakeGitHub()
        self.patches = [mock.patch.object(cloud, "request", self.hub.request),
                        mock.patch.object(cloud.saves, "running_games", lambda: []),
                        mock.patch.object(cloud.time, "sleep", lambda _s: None)]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        self.a = PC(Path(self.tmp.name) / "pc-a", "desktop")
        self.b = PC(Path(self.tmp.name) / "pc-b", "laptop")
        make_save(self.a.save, b"a")
        make_save(self.b.save, b"fresh", slots=())
        self.a.sign_in(self.hub)
        self.b.sign_in(self.hub)

    def prefix(self, uid=UID):
        return "saves/" + uid

    # -- pushing -------------------------------------------------------
    def test_push_makes_a_private_repo_and_sends_only_save_files(self):
        (self.a.save / "notes.txt").write_bytes(b"mine")
        (self.a.save / "image.bin").write_bytes(b"not a save")
        code, out = self.a.run("push")
        self.assertEqual(code, 0, out)
        self.assertTrue(self.hub.repos[REPO]["private"])
        sent = self.hub.files_on(REPO)
        self.assertEqual(sorted(sent), sorted(["%s/%s" % (self.prefix(), n) for n in
                                               ("account.sav", cloud.MANIFEST, "option.sav",
                                                "slot_00/caption.sav", "slot_00/progress.sav",
                                                "slot_01/caption.sav", "slot_01/progress.sav")]))
        self.assertEqual(sent["%s/account.sav" % self.prefix()], b"account-a")
        manifest = self.hub.manifest_on(REPO, self.prefix())
        self.assertEqual(manifest["generation"], 1)
        self.assertEqual(manifest["device_name"], "desktop")
        self.assertEqual(manifest["files"]["account.sav"]["sha256"], hashlib.sha256(b"account-a").hexdigest())

    def test_push_twice_without_a_change_sends_nothing_new(self):
        self.a.run("push")
        before = self.hub.history(REPO)
        code, out = self.a.run("push")
        self.assertEqual(code, 0, out)
        self.assertIn("already has this save", out)
        self.assertEqual(self.hub.history(REPO), before)

    def test_push_after_a_change_bumps_the_generation_and_keeps_the_old_commit(self):
        self.a.run("push")
        make_save(self.a.save, b"a2")
        code, out = self.a.run("push")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.hub.manifest_on(REPO, self.prefix())["generation"], 2)
        self.assertEqual(len(self.hub.history(REPO)), 3)   # auto_init, generation 1, generation 2

    def test_push_removes_a_slot_that_is_gone_locally(self):
        self.a.run("push")
        import shutil
        shutil.rmtree(self.a.save / "slot_01")
        code, out = self.a.run("push")
        self.assertEqual(code, 0, out)
        self.assertNotIn("%s/slot_01/progress.sav" % self.prefix(), self.hub.files_on(REPO))

    def test_dry_run_push_changes_nothing(self):
        code, out = self.a.run("push", "--dry-run")
        self.assertEqual(code, 0, out)
        self.assertIn("Would send", out)
        self.assertIn("Would make the private repository", out)
        self.assertNotIn(REPO, self.hub.repos)      # a dry run does not even make the repository

    def test_push_refuses_an_oversized_file(self):
        (self.a.save / "slot_00" / "progress.sav").write_bytes(b"x" * (cloud.MAX_FILE + 1))
        code, out = self.a.run("push")
        self.assertEqual(code, 1)
        self.assertIn("far more than a save file", out)
        self.assertEqual(self.hub.files_on(REPO), {})

    def test_push_refuses_while_a_game_is_running(self):
        with mock.patch.object(cloud.saves, "running_games", lambda: ["Hookshot"]):
            code, out = self.a.run("push")
        self.assertEqual(code, 1)
        self.assertIn("close Hookshot first", out)

    def test_push_needs_a_token(self):
        with self.a.at():
            os.remove(cloud.token_path())
        code, out = self.a.run("push")
        self.assertEqual(code, 1)
        self.assertIn("not signed in", out)

    # -- pulling -------------------------------------------------------
    def test_pull_on_a_second_pc_gets_the_same_save(self):
        self.a.run("push")
        code, out = self.b.run("pull")
        self.assertEqual(code, 0, out)
        self.assertEqual(tree_of(self.b.save), tree_of(self.a.save))

    def test_pull_backs_up_what_it_replaces(self):
        self.a.run("push")
        make_save(self.b.save, b"b")
        code, out = self.b.run("pull", "--force")
        self.assertEqual(code, 0, out)
        self.assertEqual((self.b.save / "account.sav").read_bytes(), b"account-a")
        backups = list((self.b.data / "oot-save-backups").glob("cloud-pull-*"))
        self.assertEqual(len(backups), 1, out)
        self.assertEqual((backups[0] / "account.sav").read_bytes(), b"account-b")

    def test_pull_refuses_unpushed_local_changes(self):
        self.a.run("push")
        self.b.run("pull")
        make_save(self.b.save, b"b-played")
        code, out = self.b.run("pull")
        self.assertEqual(code, 1)
        self.assertIn("changed since it last synced", out)
        self.assertEqual((self.b.save / "account.sav").read_bytes(), b"account-b-played")

    def test_pull_with_nothing_in_the_cloud_says_so(self):
        self.hub.make_repo(REPO, empty=False)
        code, out = self.b.run("pull")
        self.assertEqual(code, 1)
        self.assertIn("nothing is in", out)

    def test_pull_drops_a_slot_the_cloud_no_longer_has(self):
        self.a.run("push")
        self.b.run("pull")
        import shutil
        shutil.rmtree(self.a.save / "slot_01")
        self.a.run("push")
        code, out = self.b.run("pull")
        self.assertEqual(code, 0, out)
        self.assertFalse((self.b.save / "slot_01").exists())

    def test_pull_on_a_pc_that_never_saved_says_to_start_the_game(self):
        import shutil
        shutil.rmtree(self.b.save)
        self.a.run("push")
        code, out = self.b.run("pull")
        self.assertEqual(code, 1)
        self.assertIn("start the game", out)
        code, out = self.b.run("pull", "--user", UID)        # (or name the folder to make)
        self.assertEqual(code, 0, out)
        self.assertEqual(tree_of(self.b.save), tree_of(self.a.save))

    def test_a_first_pull_keeps_a_save_with_nothing_played_in_it(self):
        self.a.run("push")
        code, out = self.b.run("pull")                        # no --force: a slotless save has nothing to lose
        self.assertEqual(code, 0, out)
        self.assertEqual(tree_of(self.b.save), tree_of(self.a.save))
        backups = list((self.b.data / "oot-save-backups").glob("cloud-pull-*"))
        self.assertEqual((backups[0] / "account.sav").read_bytes(), b"account-fresh")

    def test_pull_refuses_a_repository_holding_something_that_is_not_a_save(self):
        self.a.run("push")
        head = self.hub.repos[REPO]["branches"]["main"]
        tree = dict(self.hub.trees[self.hub.commits[head]["tree"]])
        blob = sha_of("blob", b"#!/bin/sh\n")
        self.hub.blobs[blob] = b"#!/bin/sh\n"
        tree["%s/run.sh" % self.prefix()] = blob
        self.hub.repos[REPO]["branches"]["main"] = self.hub._put_commit(self.hub._put_tree(tree), [head])
        code, out = self.b.run("pull")
        self.assertEqual(code, 1)
        self.assertIn("is not a save file", out)
        self.assertEqual((self.b.save / "account.sav").read_bytes(), b"account-fresh")

    # -- two PCs at once -----------------------------------------------
    def test_push_refuses_when_the_other_pc_pushed_first(self):
        self.a.run("push")
        self.b.run("pull")
        make_save(self.b.save, b"b2")
        self.b.run("push")
        make_save(self.a.save, b"a2")
        code, out = self.a.run("push")
        self.assertEqual(code, 1)
        self.assertIn("made on another PC", out)
        self.assertEqual(self.hub.manifest_on(REPO, self.prefix())["device_name"], "laptop")

    def test_forced_push_wins_and_keeps_the_other_save_in_the_history(self):
        self.a.run("push")
        self.b.run("pull")
        make_save(self.b.save, b"b2")
        self.b.run("push")
        laptop_commit = self.hub.repos[REPO]["branches"]["main"]
        make_save(self.a.save, b"a2")
        code, out = self.a.run("push", "--force")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.hub.files_on(REPO)["%s/account.sav" % self.prefix()], b"account-a2")
        self.assertIn(laptop_commit, self.hub.history(REPO))

    def test_sync_pushes_pulls_or_stops(self):
        code, out = self.a.run("sync")             # nothing in the cloud: push
        self.assertEqual(code, 0, out)
        code, out = self.b.run("sync")             # nothing local: pull
        self.assertEqual(code, 0, out)
        self.assertEqual(tree_of(self.b.save), tree_of(self.a.save))
        code, out = self.b.run("sync")             # both agree
        self.assertIn("Both sides agree", out)
        make_save(self.b.save, b"b3")              # only this PC moved: push
        code, out = self.b.run("sync")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.hub.manifest_on(REPO, self.prefix())["generation"], 2)
        make_save(self.a.save, b"a3")              # both moved: stop and say so
        code, out = self.a.run("sync")
        self.assertEqual(code, 1)
        self.assertIn("both sides moved", out)

    def test_two_pcs_with_different_user_ids_share_one_folder_when_pinned(self):
        other = PC(Path(self.tmp.name) / "pc-c", "steamdeck", uid=OTHER_UID)
        make_save(other.save, b"fresh", slots=())
        other.sign_in(self.hub)
        self.a.run("push")
        code, out = other.run("pull", "--remote-name", UID)
        self.assertEqual(code, 0, out)
        self.assertEqual(tree_of(other.save), tree_of(self.a.save))

    # -- status, sign-in -----------------------------------------------
    def test_status_reads_both_sides_without_changing_anything(self):
        self.a.run("push")
        make_save(self.a.save, b"a-later")
        code, out = self.a.run("status")
        self.assertEqual(code, 0, out)
        self.assertIn("generation 1", out)
        self.assertIn("This PC's save is newer", out)
        self.assertEqual(self.hub.manifest_on(REPO, self.prefix())["generation"], 1)

    def test_status_names_a_conflict(self):
        self.a.run("push")
        self.b.run("pull")
        make_save(self.b.save, b"b4")
        self.b.run("push")
        make_save(self.a.save, b"a4")
        code, out = self.a.run("status")
        self.assertEqual(code, 0, out)
        self.assertIn("Both sides moved", out)

    def test_device_flow_login_stores_a_private_token(self):
        with self.a.at():
            os.remove(cloud.token_path())
        self.hub.authorized = True
        code, out = self.a.run("login", "--client-id", "Iv1.test")
        self.assertEqual(code, 0, out)
        self.assertIn("ABCD-1234", out)
        self.assertIn("Signed in as player", out)
        with self.a.at():
            self.assertEqual(cloud.read_token(), self.hub.token)
            self.assertEqual(cloud.read_config()["client_id"], "Iv1.test")
            if os.name != "nt":
                self.assertEqual(os.stat(cloud.token_path()).st_mode & 0o777, 0o600)

    def test_login_waits_for_the_code_to_be_entered(self):
        with self.a.at():
            os.remove(cloud.token_path())
        pending = {"n": 0}

        def authorize(_s):
            pending["n"] += 1
            if pending["n"] >= 2:
                self.hub.authorized = True
        with mock.patch.object(cloud.time, "sleep", authorize):
            code, out = self.a.run("login", "--client-id", "Iv1.test")
        self.assertEqual(code, 0, out)
        self.assertGreaterEqual(pending["n"], 2)

    def test_login_without_a_client_id_explains_the_two_ways(self):
        code, out = self.a.run("login")
        self.assertEqual(code, 1)
        self.assertIn("--help-setup", out)
        code, out = self.a.run("login", "--help-setup")
        self.assertEqual(code, 0, out)
        self.assertIn("Enable Device Flow", out)

    def test_token_login_takes_a_pasted_token(self):
        with self.a.at():
            os.remove(cloud.token_path())
        with mock.patch.object(cloud.getpass, "getpass", lambda _p: self.hub.token):
            code, out = self.a.run("login", "--token")
        self.assertEqual(code, 0, out)
        with self.a.at():
            self.assertEqual(cloud.read_token(), self.hub.token)

    def test_logout_removes_the_token_and_leaves_the_cloud_alone(self):
        self.a.run("push")
        code, out = self.a.run("logout")
        self.assertEqual(code, 0, out)
        with self.a.at():
            self.assertIsNone(cloud.read_token())
        self.assertEqual(self.hub.manifest_on(REPO, self.prefix())["generation"], 1)

    def test_a_bad_token_says_to_sign_in_again(self):
        with self.a.at():
            cloud.write_token("gho_wrong")
        code, out = self.a.run("status")
        self.assertEqual(code, 1)
        self.assertIn("sign in again", out)

    def test_a_repository_of_someone_else_is_not_made(self):
        code, out = self.a.run("push", "--repo", "someone-else/saves")
        self.assertEqual(code, 1)
        self.assertIn("not yours to make", out)

    # -- play ----------------------------------------------------------
    def test_play_syncs_runs_and_syncs_again(self):
        self.a.run("push")
        self.b.run("pull")
        make_save(self.a.save, b"a-played")
        self.a.run("push")

        def start(argv):
            self.assertEqual(argv, ["./Hookshot.AppImage"])
            make_save(self.b.save, b"b-played-after")
            return 0
        with mock.patch.object(cloud.subprocess, "call", start):
            code, out = self.b.run("play", "--", "./Hookshot.AppImage")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.hub.files_on(REPO)["%s/account.sav" % self.prefix()], b"account-b-played-after")
        self.assertEqual(self.hub.manifest_on(REPO, self.prefix())["generation"], 3)

    def test_play_does_not_start_the_game_when_the_save_is_in_conflict(self):
        self.a.run("push")
        self.b.run("pull")
        make_save(self.b.save, b"b5")
        self.b.run("push")
        make_save(self.a.save, b"a5")
        started = []
        with mock.patch.object(cloud.subprocess, "call", lambda argv: started.append(argv) or 0):
            code, out = self.a.run("play", "--", "./Hookshot.AppImage")
        self.assertEqual(code, 1)
        self.assertEqual(started, [])
        self.assertIn("was not started", out)

    def test_play_needs_a_command(self):
        code, out = self.a.run("play")
        self.assertEqual(code, 1)
        self.assertIn("name the game after --", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
