# The same save on two PCs

`scripts/oot_cloud.py` keeps an Ocarina of Time save in a **GitHub repository of your own**, so a desktop and a laptop
(or a Steam Deck) can carry on from the same spot. This project runs no server and holds no save: the repository is
yours, private, in your account, and the only credentials involved are yours.

It is the same idea as [using a save from an emulator](saves.md) — the files are copied, nothing is converted — with
GitHub standing in for the folder you would otherwise copy by hand.

You need your own copy of the game. Nothing here comes with game files.

## What is sent, and what is not

Only the files the game itself writes as your progress:

```
account.sav
option.sav            (the game's own settings)
slot_00/ ... slot_NN/
  progress.sav
  caption.sav
```

Everything else in the save folder stays on the PC. **No game files, keys, builds or anything made from a dump are
ever sent** — the script sends the four names above and nothing else, refuses a file that is far larger than a save
file could be, and refuses a folder with links in it, so a save cannot smuggle in whatever a link points at. A pull
is just as strict in the other direction: a repository holding anything that is not one of those names is refused
before a single file is written.

## Signing in, once per PC

Pick either way. The device flow is nicer to live with; the token needs nothing registered.

**GitHub's device flow.** It needs an OAuth app's client id, and the app has to be yours, so that nobody else can
revoke it for you:

```sh
python -I scripts/oot_cloud.py login --help-setup     # prints these steps
```

1. <https://github.com/settings/developers> → OAuth Apps → New OAuth App. Any name, any homepage URL, no callback URL.
2. On the app's page, turn on **Enable Device Flow**.
3. Copy the Client ID (it is not a secret) and sign in with it:

```sh
python -I scripts/oot_cloud.py login --client-id <id>
```

The script prints a short code and the URL to type it into, waits for you, and stores the token it gets back. The
client id is remembered, so later PCs only need `login`.

**A fine-grained token**, with no app to register: Settings → Developer settings → Personal access tokens →
Fine-grained tokens, with **Contents: Read and write** on the saves repository (add **Administration: Read and write**
if the script should create that repository for you). Then:

```sh
python -I scripts/oot_cloud.py login --token      # reads it from the terminal, or $HOOKSHOT_GITHUB_TOKEN
```

The token is written to `cloud-token` with owner-only permissions, beside `cloud.json` (the settings):
`$XDG_CONFIG_HOME/hookshot` (default `~/.config/hookshot`) on Linux, `%LOCALAPPDATA%\Hookshot` on Windows. Both sit
outside the save folder, so neither is ever sent. `logout` deletes the token and leaves the save in the repository
alone.

## Day to day

```sh
python -I scripts/oot_cloud.py status                     # what each side has
python -I scripts/oot_cloud.py push                       # this PC's save -> the repository
python -I scripts/oot_cloud.py pull                        # the repository -> this PC
python -I scripts/oot_cloud.py sync                        # whichever side is behind
python -I scripts/oot_cloud.py play -- ./Hookshot.AppImage  # sync, run the game, sync when it quits
```

The repository is made on first use, private, named `hookshot-saves` unless `--repo owner/name` says otherwise. The
save goes in `saves/<user id>/`, the user id being the folder Hookshot already keeps the save in. **Start the game
once on a new PC and save**, so that folder exists, the way the emulator copies need it to; `pull --user <id>` makes
it instead if you know the id. When two PCs use different ids, pin one with `--remote-name <id>` so both read the same
folder (it is remembered afterwards).

`play` is the one to put in a shortcut: it refuses to start the game if the save is in conflict, rather than letting
you play an hour into a save that cannot be sent afterwards.

| Option | What it does |
|---|---|
| `--repo OWNER/NAME` | The repository to use (default `hookshot-saves` in your account) |
| `--branch NAME` | The branch in it (default `main`) |
| `--remote-name NAME` | The folder in the repository, when two PCs' user ids differ |
| `--hookshot DIR` | Hookshot's data folder (Windows: the game folder) |
| `--user ID` | Which user id folder to sync, when there are several (or the one to make on a first pull) |
| `--backup-dir DIR` | Where a pull puts the save it replaces |
| `--dry-run` | Says what would happen and changes nothing |
| `--force` | Pushes over, or pulls over, a save made somewhere else |

## Two PCs cannot overwrite each other

Every push is **one commit** holding the whole save plus a small `cloud.json`: a counter that goes up by one each
time, which PC sent it and when, and a SHA-256 of every file. Each PC also remembers the counter it last synced.

- A **push** when the repository has moved on since this PC last synced is refused: that save was made somewhere else.
  `pull` takes it, or `push --force` replaces it — and the replaced save stays in the repository's history either way,
  because nothing is ever rewritten. The branch is moved only as a fast-forward, so two PCs pushing at the same second
  end with one refused, never with a mixed save.
- A **pull** over local progress that was never pushed is refused the same way. `pull --force` takes the cloud's save
  and copies the local one to `oot-save-backups/cloud-pull-<date>-<time>/` first (`$XDG_DATA_HOME` on Linux,
  `%LOCALAPPDATA%\Hookshot` on Windows), exactly where `oot_saves.py` puts its backups. A save with no `slot_*`
  folders has nothing played in it, so a first pull on a new PC goes ahead without asking.
- Saves are never merged. Two PCs that both played from the same spot are two saves, and you pick one; the other is
  in the backup folder or in the repository's history.
- Like `oot_saves.py`, every command refuses to run while Hookshot or an emulator is running, since a game writes its
  save again on exit. `--force` skips that check.

## Good to know

- A save is a few KB, so a year of play is a repository of a few MB. Nothing needs cleaning up.
- Make the repository **private**. The script does when it creates it; if you make it yourself, do the same. A public
  one gives your progress away, nothing more, but there is no reason to.
- Two PCs playing at once is still two saves. Push when you stop, pull when you start — `play` does both.
- The repository's history is the backup worth having: `git log` on it, or GitHub's own view of `cloud.json`, shows
  every save sent, from which PC and when, and any old one can be fetched out of it.
- Nothing here talks to GitHub while the game runs. The game itself gained no network code: it is a script beside it.

## What was tested

- `tests/test_oot_cloud.py`, part of `python -I -B tests/run_all.py`: a fake GitHub in memory stands in for the one
  HTTP function, so the push, the pull, `sync`, `play`, both conflict directions, the forced cases, the backups, the
  device-flow sign-in and the guards are checked on temporary folders — with no account, no token and no network.
- Linux. Not tried on Windows: the paths come from the save documentation's table.
- Against GitHub itself, a real account's repository is yours to try; the API calls are the ordinary git data
  endpoints (blobs, trees, commits, refs).
