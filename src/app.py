#!/usr/bin/env python3
"""RsyncGUI - petite interface locale pour rsync (reprise auto, remontage SMB).

Python 3.9+ sans dépendance. Lancer : python3 app.py
"""
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.parse
import uuid
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DATA_DIR = os.path.expanduser("~/.rsync-gui")
JOBS_FILE = os.path.join(DATA_DIR, "jobs.json")
TOKEN = secrets.token_urlsafe(24)
RETRY_DELAY = 10
APP_SUPPORT = os.path.expanduser("~/Library/Application Support/Rsync GUI")
UPDATED_RSYNC = os.path.join(APP_SUPPORT, "bin", "rsync")
STATE_FILE = os.path.join(DATA_DIR, "state.json")
FAV_FILE = os.path.join(DATA_DIR, "favorites.json")
HISTORY_FILE = os.path.join(DATA_DIR, "history.json")
HISTORY_MAX = 50
ACTIVE = ("starting", "running", "retrying", "paused")
PERSIST_FIELDS = ("status", "current", "percent", "attempt", "files", "total_bytes", "total_files",
                  "total_dirs", "files_done", "dirs_done", "completed_bytes", "cur_size", "cur_bytes",
                  "active_time")
RESOURCES = os.environ.get("RSYNCGUI_RESOURCES") or os.path.dirname(os.path.abspath(__file__))
BUNDLED_RSYNC = os.path.join(RESOURCES, "bin", "rsync")
RSYNC_LISTING = "https://download.samba.org/pub/rsync/"
CHECK_EVERY = 24 * 3600
EMBEDDED = bool(os.environ.get("RSYNCGUI_EMBEDDED"))
CONFIGURE_FLAGS = ["--disable-xxhash", "--disable-zstd", "--disable-lz4", "--disable-openssl",
                   "--disable-md2man", "--disable-idn", "--with-included-popt", "--with-included-zlib"]

jobs_lock = threading.Lock()
history_lock = threading.Lock()
runs = {}  # job id -> Run


def load_jobs():
    try:
        with open(JOBS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def save_jobs(jobs):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = JOBS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(jobs, f, ensure_ascii=False, indent=2)
    os.replace(tmp, JOBS_FILE)


_ver_cache = {}


def rsync_version(path):
    """(3, 5, 1) ou None. Mis en cache par chemin + date de modification."""
    try:
        key = (path, os.path.getmtime(path))
    except OSError:
        return None
    if key not in _ver_cache:
        m = re.search(r"version\s+(\d+)\.(\d+)\.(\d+)", sh([path, "--version"], timeout=10))
        _ver_cache[key] = tuple(int(x) for x in m.groups()) if m else None
    return _ver_cache[key]


def rsync_info():
    """Meilleur rsync disponible : mis à jour > intégré > Homebrew. Retourne (chemin, version, origine)."""
    best = None
    for path, origin in ((UPDATED_RSYNC, "mis à jour"), (BUNDLED_RSYNC, "intégré"),
                         ("/opt/homebrew/bin/rsync", "Homebrew"), ("/usr/local/bin/rsync", "Homebrew")):
        v = rsync_version(path) if os.access(path, os.X_OK) else None
        if v and (best is None or v > best[1]):
            best = (path, v, origin)
    return best


def rsync_path():
    info = rsync_info()
    return info[0] if info else None


rsync_update = {"state": "idle", "message": ""}
update_lock = threading.Lock()


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(st):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f)


def load_favorites():
    try:
        with open(FAV_FILE, encoding="utf-8") as f:
            favs = json.load(f)
        return [p for p in favs if isinstance(p, str)]
    except (OSError, ValueError):
        return []


def set_favorite(path, add):
    path = os.path.normpath(os.path.expanduser(path))
    with jobs_lock:
        favs = [p for p in load_favorites() if p != path]
        if add:
            favs.append(path)
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(FAV_FILE, "w", encoding="utf-8") as f:
            json.dump(favs, f, ensure_ascii=False, indent=2)
    return favs


def run_checked(cmd, cwd=None, timeout=900):
    r = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        tail = "\n".join((r.stdout + r.stderr).strip().splitlines()[-6:])
        raise RuntimeError("%s a échoué :\n%s" % (cmd[0], tail))
    return r.stdout


def latest_rsync_version():
    html = run_checked(["curl", "-fsSL", "-m", "20", RSYNC_LISTING], timeout=40)
    found = {tuple(int(x) for x in m.groups()) for m in re.finditer(r"rsync-(\d+)\.(\d+)\.(\d+)\.tar\.gz", html)}
    if not found:
        raise RuntimeError("Aucune version trouvée sur %s" % RSYNC_LISTING)
    return max(found)


def build_rsync(version, dest):
    """Télécharge le code source officiel de rsync, le compile (dépendances système seulement) vers dest."""
    v = ".".join(map(str, version))
    work = tempfile.mkdtemp(prefix="rsync-build-")
    try:
        tgz = os.path.join(work, "src.tar.gz")
        run_checked(["curl", "-fsSL", "-m", "300", "-o", tgz, "%srsync-%s.tar.gz" % (RSYNC_LISTING, v)], timeout=330)
        with tarfile.open(tgz) as t:
            for m in t.getmembers():
                if m.name.startswith("/") or ".." in m.name.split("/"):
                    raise RuntimeError("Archive suspecte : %s" % m.name)
            t.extractall(work)
        src = os.path.join(work, "rsync-" + v)
        run_checked(["./configure"] + CONFIGURE_FLAGS, cwd=src)
        run_checked(["make", "-j%d" % (os.cpu_count() or 4), "rsync"], cwd=src)
        built = os.path.join(src, "rsync")
        if rsync_version(built) != version:
            raise RuntimeError("Le binaire compilé ne répond pas comme prévu.")
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        tmp = dest + ".new"
        shutil.copy2(built, tmp)
        os.chmod(tmp, 0o755)
        os.replace(tmp, dest)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def update_rsync():
    if not update_lock.acquire(blocking=False):
        return
    try:
        rsync_update.update(state="checking", message="Recherche de mises à jour…")
        latest = latest_rsync_version()
        info = rsync_info()
        current = info[1] if info else None
        st = load_state()
        st["last_check"] = time.time()
        save_state(st)
        lv = ".".join(map(str, latest))
        if current and latest <= current:
            rsync_update.update(state="uptodate", message="rsync %s est à jour." % lv)
            return
        rsync_update.update(state="building", message="Compilation de rsync %s…" % lv)
        build_rsync(latest, UPDATED_RSYNC)
        rsync_update.update(state="updated", message="rsync mis à jour en %s." % lv)
    except Exception as exc:  # noqa: BLE001 - affiché tel quel dans l'interface
        msg = str(exc)
        if "xcode" in msg.lower() or "No developer tools" in msg:
            msg = "Les outils de développement Xcode sont nécessaires pour compiler rsync."
        rsync_update.update(state="error", message=msg)
    finally:
        update_lock.release()


def update_loop():
    while True:
        if time.time() - load_state().get("last_check", 0) > CHECK_EVERY:
            update_rsync()
        time.sleep(3600)


def sh(cmd, timeout=30):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout
    except (subprocess.TimeoutExpired, OSError):
        return ""


_mounts_cache = [0.0, {}]


def smb_mounts(fresh=False):
    """{'/Volumes/x': 'smb://host/share'} pour les partages SMB montés (mis en cache 3 s)."""
    if not fresh and time.time() - _mounts_cache[0] < 3:
        return _mounts_cache[1]
    result = {}
    for line in sh(["mount"]).splitlines():
        m = re.match(r"//(?:[^@]*@)?([^/]+)/(.+?) on (/Volumes/.+?) \(smbfs", line)
        if m:
            result[m.group(3)] = "smb://%s/%s" % (m.group(1), m.group(2))
    _mounts_cache[0], _mounts_cache[1] = time.time(), result
    return result


def volume_of(path):
    m = re.match(r"(/Volumes/[^/]+)", path or "")
    return m.group(1) if m else None


UNITS = {"": 1, "k": 1024, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4}
PROGRESS_RE = re.compile(r"^([\d,]+)\s+(\d+)%\s+([\d.]+)([kKMGT]?)B/s\s+\d+:\d+:\d+")


def fr_bytes(n):
    n = float(n)
    for unit, div in (("Go", 1024 ** 3), ("Mo", 1024 ** 2), ("Ko", 1024)):
        if n >= div:
            return (("%.2f %s" if unit == "Go" else "%.1f %s") % (n / div, unit)).replace(".", ",")
    return "%d o" % n


def fr_eta(seconds):
    s = int(seconds)
    if s < 60:
        return "%d s" % s
    if s < 3600:
        return "%d min" % round(s / 60)
    return "%dh%02d" % (s // 3600, s % 3600 // 60)


class Run:
    def __init__(self, job):
        self.job = job
        self.status = "starting"
        self.log = deque(maxlen=400)
        self.current = ""
        self.percent = 0
        self.attempt = 0
        self.files = 0
        self.stop_flag = False
        self.proc = None
        self.started = time.time()
        self.finished = None
        # progression détaillée
        self.planning = False
        self.planned = False
        self.total_bytes = None
        self.total_files = None
        self.total_dirs = None
        self.files_done = 0
        self.dirs_done = 0
        self.completed_bytes = 0
        self.cur_size = 0
        self.cur_bytes = 0
        self.cur_speed = 0.0
        self.cur_started = None
        self.cur_counted = True
        self.active_time = 0.0
        self.attempt_start = None
        self.caff = None
        self.paused = False
        self.plan_proc = None
        self.cur_live = False   # taille suivie en direct sur la destination
        self.cur_dest = ""
        self.samples = deque(maxlen=14)
        self.done_files = {}   # nom -> taille des fichiers terminés par la copie
        self.done_dirs = set()

    def say(self, text):
        self.log.append("%s  %s" % (time.strftime("%H:%M:%S"), text))

    @classmethod
    def restore(cls, entry):
        """Recrée un transfert depuis l'historique ; un transfert qui était actif devient « interrompu »."""
        run = cls(entry["job"])
        for f in PERSIST_FIELDS:
            if f in entry:
                setattr(run, f, entry[f])
        run.started = entry.get("started", time.time())
        run.finished = entry.get("finished") or entry.get("saved_at") or time.time()
        run.log = deque(entry.get("log", []), maxlen=400)
        run.cur_counted = True
        run.planned = True
        if run.status in ACTIVE:
            run.status = "interrupted"
            run.say("Interrompu : l'app a été fermée pendant le transfert.")
        return run

    def metrics(self):
        now = time.time()
        active = self.active_time + (now - self.attempt_start if self.attempt_start else 0)
        done = self.completed_bytes + (0 if self.cur_counted else self.cur_bytes)
        avg = done / active if active > 2 and done > 0 else 0.0
        total = self.total_bytes
        overall = -1
        if total:
            overall = min(100, int(done * 100 / total))
        if self.status == "done":
            overall = 100 if total else overall
        total_eta = (total - done) / avg if total and avg > 0 and done < total else None
        cur_eta = None
        if not self.cur_counted and self.cur_speed > 0 and self.cur_size > self.cur_bytes:
            cur_eta = (self.cur_size - self.cur_bytes) / self.cur_speed
        cur_elapsed = int(now - self.cur_started) if self.cur_started else 0
        return {
            "planning": self.planning,
            "total_bytes": total, "done_bytes": done, "overall": overall,
            "total_files": self.total_files, "files_done": self.files_done,
            "total_dirs": self.total_dirs, "dirs_done": self.dirs_done,
            "avg_speed": avg, "total_eta": total_eta, "active": int(active),
            "cur_size": self.cur_size, "cur_bytes": self.cur_bytes, "cur_speed": self.cur_speed,
            "cur_elapsed": cur_elapsed, "cur_eta": cur_eta,
        }

    def summary(self, m):
        parts = []
        if m["total_files"]:
            parts.append("%d/%d fichiers" % (m["files_done"], m["total_files"]))
        if m["total_bytes"]:
            parts.append("%s/%s" % (fr_bytes(m["done_bytes"]), fr_bytes(m["total_bytes"])))
        if m["avg_speed"]:
            parts.append("%s/s" % fr_bytes(m["avg_speed"]))
        if m["total_eta"] is not None:
            parts.append("~%s restant" % fr_eta(m["total_eta"]))
        return " · ".join(parts)

    def snapshot(self):
        m = self.metrics()
        snap = {
            "status": self.status,
            "percent": self.percent,
            "current": self.current,
            "attempt": self.attempt,
            "files": self.files,
            "log": list(self.log)[-120:],
            "elapsed": int((self.finished or time.time()) - self.started),
            "name": self.job.get("name") or os.path.basename(self.job["dest"].rstrip("/")),
            "source": self.job.get("source") or "%d élément(s)" % len(self.job.get("items", [])),
            "dest": self.job["dest"],
            "ephemeral": bool(self.job.get("ephemeral")),
            "summary": self.summary(m),
            "started_at": self.started,
        }
        snap.update(m)
        return snap

    def ensure_mounted(self):
        vol = volume_of(self.job["dest"])
        if not vol:
            os.makedirs(self.job["dest"], exist_ok=True)
            return True
        if vol in smb_mounts(fresh=True):
            return True
        url = self.job.get("remount_url", "").strip()
        if not url:
            self.say("Volume %s absent et aucune adresse de remontage définie." % vol)
            return False
        self.say("Volume absent, remontage de %s ..." % url)
        sh(["osascript", "-e", 'mount volume "%s"' % url.replace('"', "")], timeout=60)
        return vol in smb_mounts(fresh=True)

    def command(self, plan=False):
        j = self.job
        rs = rsync_path()
        if plan:
            cmd = [rs or "rsync", "-rtn", "--stats", "--out-format=%l|%n", "--timeout=60"]
        elif rs:
            # rsync 3.x : --inplace reprend un gros fichier après une coupure SMB
            cmd = [rs, "-rtv", "--inplace", "--partial", "--progress", "--timeout=60"]
        else:
            cmd = ["rsync", "-rtv", "--partial", "--progress"]
        if j.get("exclude_ds_store", True):
            cmd += ["--exclude", ".DS_Store"]
        if j.get("mirror"):
            cmd.append("--delete")
        if j.get("bwlimit") and not plan:
            cmd.append("--bwlimit=%d" % int(j["bwlimit"]))  # Ko/s
        if j.get("_dry") and not plan:
            cmd.append("-n")
        dst = j["dest"].rstrip("/") + "/"
        if j.get("items"):
            # sans "/" final : un dossier est copié en tant que dossier
            srcs = [p.rstrip("/") or "/" for p in j["items"]]
        else:
            srcs = [j["source"].rstrip("/") + "/"]
        return cmd + srcs + [dst]

    def plan(self):
        """Calcul du total en arrière-plan (rsync -n --stats), sans retarder la copie."""
        self.planning = True
        self.say("Calcul du total à copier en arrière-plan…")
        try:
            proc = subprocess.Popen(self.command(plan=True) + [], stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True,
                                    preexec_fn=lambda: os.nice(10))  # priorité basse
        except OSError:
            self.planning = False
            return
        self.plan_proc = proc
        out = []
        reader = threading.Thread(target=lambda: out.append(proc.stdout.read()), daemon=True)
        reader.start()
        while proc.poll() is None:
            if self.stop_flag:
                proc.terminate()
                break
            time.sleep(0.3)
        reader.join(timeout=5)
        text = out[0] if out else ""
        self.planning = False
        self.plan_proc = None
        if self.stop_flag or proc.returncode not in (0, 23, 24):
            return

        plan_files, plan_dirs = {}, set()
        for line in text.splitlines():
            m = re.match(r"^(\d+)\|(.+)$", line)
            if not m:
                continue
            name = m.group(2)
            if name.endswith("/"):
                plan_dirs.add(name)
            else:
                plan_files[name] = int(m.group(1))
        if not plan_files and not plan_dirs and "Number of files" not in text:
            return
        # exact : fichiers à copier d'après l'analyse + ceux que la copie a déjà terminés entre-temps
        union = dict(plan_files)
        union.update(self.done_files)
        self.total_files = len(union)
        self.total_bytes = sum(union.values())
        self.total_dirs = len(plan_dirs | self.done_dirs)
        self.say("Total à copier : %d fichier(s), %s." % (self.total_files, fr_bytes(self.total_bytes)))

    def resolve(self, name):
        items = self.job.get("items")
        base = os.path.dirname(items[0].rstrip("/")) if items else self.job.get("source", "")
        return os.path.join(base, name)

    def file_done(self):
        self.done_files[self.current] = self.cur_size or self.cur_bytes
        self.completed_bytes += self.cur_size or self.cur_bytes
        self.files_done += 1
        self.cur_counted = True
        self.percent = 100

    def parse(self, line):
        line = line.strip()
        if not line:
            return
        m = PROGRESS_RE.match(line)
        if m:
            b = int(m.group(1).replace(",", ""))
            pct = int(m.group(2))
            if not self.cur_live:
                self.percent = pct
                self.cur_speed = float(m.group(3)) * UNITS[m.group(4)]
            if not self.cur_counted:
                self.cur_bytes = max(self.cur_bytes, b)
                if not self.cur_size and pct > 0:
                    self.cur_size = int(b * 100 / pct)
            if "(xfr#" in line and not self.cur_counted:
                self.cur_bytes = self.cur_size or b
                self.file_done()
            return
        if re.match(r"^[\d,]+\s+\d+%", line):  # openrsync : pas de débit détaillé
            self.percent = int(re.match(r"^[\d,]+\s+(\d+)%", line).group(1))
            return
        if line in ("./", "."):
            return
        if line.startswith(("sending incremental", "sent ", "total size", "building file", "created directory")):
            self.say(line)
            return
        if line.endswith("/"):
            self.done_dirs.add(line)
            self.dirs_done += 1
            self.files += 1
            self.log.append(line)
            return
        self.current = line
        self.percent = 0
        self.files += 1
        self.cur_bytes = 0
        self.cur_speed = 0.0
        self.cur_started = time.time()
        self.cur_dest = os.path.join(self.job["dest"], line)
        # nouveau fichier : sa taille sur la destination suit la progression (rsync --inplace)
        self.cur_live = not os.path.exists(self.cur_dest)
        self.samples.clear()
        self.cur_counted = False
        try:
            self.cur_size = os.path.getsize(self.resolve(line))
        except OSError:
            self.cur_size = 0
        self.log.append(line)

    def go(self):
        max_retries = int(self.job.get("max_retries", 20))
        while not self.stop_flag:
            self.attempt += 1
            self.status = "running"
            if not self.ensure_mounted():
                if self.attempt > max_retries:
                    break
                self.status = "retrying"
                self.say("Nouvelle tentative dans %ds ..." % RETRY_DELAY)
                self.wait(RETRY_DELAY)
                continue
            try:
                os.makedirs(self.job["dest"], exist_ok=True)
            except OSError as e:
                self.say("Création du dossier impossible : %s" % e)
            if not self.planned and not self.job.get("_dry"):
                self.planned = True
                threading.Thread(target=self.plan, daemon=True).start()
                threading.Thread(target=self.sample, daemon=True).start()
            cmd = self.command()
            self.say("Tentative %d : %s" % (self.attempt, " ".join(cmd)))
            try:
                self.proc = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, bufsize=0,
                    start_new_session=True,
                )
            except OSError as e:
                self.say("Impossible de lancer rsync : %s" % e)
                break
            self.attempt_start = time.time()
            self.keep_awake()
            buf = b""
            while True:
                chunk = self.proc.stdout.read1(4096) if hasattr(self.proc.stdout, "read1") else os.read(self.proc.stdout.fileno(), 4096)
                if not chunk:
                    break
                buf += chunk
                parts = re.split(rb"[\r\n]", buf)
                buf = parts.pop()
                for p in parts:
                    self.parse(p.decode("utf-8", "replace"))
            rc = self.proc.wait()
            self.release_awake()
            if self.attempt_start:
                self.active_time += time.time() - self.attempt_start
            self.attempt_start = None
            self.paused = False
            self.cur_counted = True  # un fichier interrompu sera repris et recompté
            self.cur_speed = 0.0
            if self.stop_flag:
                break
            if rc == 0:
                self.status = "done"
                self.percent = 100
                self.say("Terminé avec succès.")
                self.finished = time.time()
                return
            self.say("rsync a échoué (code %d)." % rc)
            if self.attempt > max_retries:
                break
            self.status = "retrying"
            self.say("Reprise dans %ds ..." % RETRY_DELAY)
            self.wait(RETRY_DELAY)
        self.status = "stopped" if self.stop_flag else "error"
        if self.status == "error":
            self.say("Abandon après %d tentatives." % self.attempt)
        self.finished = time.time()

    def wait(self, seconds):
        for _ in range(seconds * 2):
            if self.stop_flag:
                return
            time.sleep(0.5)

    def sample(self):
        """Mesure la progression du fichier en cours 4 fois par seconde (rsync n'affiche qu'environ 1 fois/s)."""
        while self.finished is None:
            time.sleep(0.25)
            if self.paused or self.cur_counted or not self.cur_live or not self.cur_dest:
                continue
            try:
                size = os.stat(self.cur_dest).st_size
            except OSError:
                continue
            if self.cur_size and size > self.cur_size:
                size = self.cur_size
            if size < self.cur_bytes:
                continue
            now = time.time()
            self.cur_bytes = size
            self.samples.append((now, size))
            if self.cur_size:
                self.percent = min(99, int(size * 100 / self.cur_size))
            if len(self.samples) >= 2:
                (t0, b0), (t1, b1) = self.samples[0], self.samples[-1]
                if t1 - t0 > 0.4:
                    self.cur_speed = (b1 - b0) / (t1 - t0)

    def keep_awake(self):
        """Empêche la veille tant que rsync tourne (caffeinate suit le pid de rsync)."""
        self.release_awake()
        if self.proc and self.proc.poll() is None:
            try:
                self.caff = subprocess.Popen(["caffeinate", "-i", "-w", str(self.proc.pid)])
            except OSError:
                self.caff = None

    def release_awake(self):
        if self.caff and self.caff.poll() is None:
            self.caff.terminate()
        self.caff = None

    def signal_group(self, sig):
        try:
            os.killpg(os.getpgid(self.proc.pid), sig)
            return True
        except (OSError, AttributeError):
            return False

    def pause(self):
        """Fige rsync (SIGSTOP) : rien n'est perdu, la copie reprendra exactement au même endroit."""
        if self.paused or self.status != "running" or not self.proc or self.proc.poll() is not None:
            return False
        if not self.signal_group(signal.SIGSTOP):
            return False
        self.paused = True
        self.status = "paused"
        self.cur_speed = 0.0
        if self.attempt_start:
            self.active_time += time.time() - self.attempt_start
            self.attempt_start = None
        self.release_awake()
        self.say("Transfert mis en pause.")
        return True

    def resume(self):
        if not self.paused or not self.proc or self.proc.poll() is not None:
            return False
        self.signal_group(signal.SIGCONT)
        self.paused = False
        self.status = "running"
        self.attempt_start = time.time()
        self.keep_awake()
        self.say("Transfert repris.")
        return True

    def stop(self):
        self.stop_flag = True
        if self.plan_proc and self.plan_proc.poll() is None:
            self.plan_proc.terminate()
        if self.proc and self.proc.poll() is None:
            if self.paused:
                self.signal_group(signal.SIGCONT)  # un processus figé ne reçoit pas SIGTERM
            self.proc.terminate()


def save_history():
    """Enregistre les transferts (hors simulations) pour les retrouver après un redémarrage."""
    with history_lock:
        entries = []
        for rid, run in list(runs.items()):
            if run.job.get("_dry"):
                continue
            e = {
                "id": rid,
                "job": {k: v for k, v in run.job.items() if not k.startswith("_")},
                "started": run.started, "finished": run.finished, "saved_at": time.time(),
                "log": list(run.log)[-200:],
            }
            for f in PERSIST_FIELDS:
                e[f] = getattr(run, f)
            entries.append(e)
        entries = sorted(entries, key=lambda e: e["started"])[-HISTORY_MAX:]
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = HISTORY_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(entries, f, ensure_ascii=False)
        os.replace(tmp, HISTORY_FILE)


def load_history():
    try:
        with open(HISTORY_FILE, encoding="utf-8") as f:
            entries = json.load(f)
    except (OSError, ValueError):
        return
    for e in entries:
        try:
            runs[e["id"]] = Run.restore(e)
        except (KeyError, TypeError):
            continue


def history_loop():
    last = None
    while True:
        time.sleep(2)
        sig = tuple((rid, r.status, r.files_done, r.percent) for rid, r in list(runs.items()))
        if sig != last:
            last = sig
            try:
                save_history()
            except OSError:
                pass


def start_run(job, dry=False, keep_log=False):
    old = runs.get(job["id"])
    if old and old.status in ACTIVE:
        return
    job = dict(job, _dry=dry)
    run = Run(job)
    if keep_log and old:
        run.log.extend(old.log)
        run.say("--- Reprise du transfert ---")
    if dry:
        run.say("Mode simulation : aucun fichier ne sera copié.")
    if not rsync_path():
        run.say("rsync 3 introuvable : reprise des gros fichiers limitée (utilisez « Mettre à jour rsync »).")
    runs[job["id"]] = run
    threading.Thread(target=run.go, daemon=True).start()
    save_history()


def list_dir(path):
    path = os.path.expanduser(path or "~")
    path = os.path.normpath(path)
    out = {"path": path, "parent": os.path.dirname(path), "entries": []}
    try:
        with os.scandir(path) as it:
            for e in it:
                if e.name.startswith("."):
                    continue
                try:
                    st = e.stat()
                    is_dir = e.is_dir()
                except OSError:
                    continue
                out["entries"].append({"n": e.name, "d": is_dir, "s": 0 if is_dir else st.st_size, "m": int(st.st_mtime)})
    except OSError as exc:
        out["error"] = exc.strerror or str(exc)
    out["entries"].sort(key=lambda x: (not x["d"], x["n"].lower()))
    return out


def dir_size(path, limit=300):
    """Taille totale d'un dossier. Retourne (octets, nb fichiers, partiel) ; partiel si le délai est dépassé."""
    total = count = 0
    deadline = time.time() + limit
    stack = [path]
    while stack:
        if time.time() > deadline:
            return total, count, True
        try:
            with os.scandir(stack.pop()) as it:
                for e in it:
                    try:
                        if e.is_symlink():
                            continue
                        if e.is_dir(follow_symlinks=False):
                            stack.append(e.path)
                        else:
                            total += e.stat(follow_symlinks=False).st_size
                            count += 1
                    except OSError:
                        continue
        except OSError:
            continue
    return total, count, False


def places():
    home = os.path.expanduser("~")
    result = []
    for label, p in (("Dossier personnel", home), ("Bureau", home + "/Desktop"),
                     ("Documents", home + "/Documents"), ("Téléchargements", home + "/Downloads")):
        if os.path.isdir(p):
            result.append({"label": label, "path": p})
    cloud = home + "/Library/CloudStorage"
    try:
        for n in sorted(os.listdir(cloud)):
            if n.startswith("GoogleDrive-") and " (" not in n:
                shared = os.path.join(cloud, n, "Drive partagés")
                result.append({"label": "Google Drive", "path": shared if os.path.isdir(shared) else os.path.join(cloud, n)})
    except OSError:
        pass
    try:
        for n in sorted(os.listdir("/Volumes")):
            if n != "Macintosh HD":
                result.append({"label": "Volume : " + n, "path": "/Volumes/" + n})
    except OSError:
        pass
    return result


def rsync_status():
    info = rsync_info()
    return {
        "version": ".".join(map(str, info[1])) if info else None,
        "origin": info[2] if info else None,
        "update": dict(rsync_update),
    }


def start_ephemeral(job, dry=False):
    job = dict(job, id=uuid.uuid4().hex[:8], ephemeral=True)
    vol = volume_of(job["dest"])
    job["remount_url"] = smb_mounts().get(vol, "") if vol else ""
    start_run(job, dry=dry)
    return job["id"]


def browse_folder():
    out = sh(["osascript", "-e", 'POSIX path of (choose folder with prompt "Choisir un dossier")'], timeout=300)
    return out.strip().rstrip("/")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def host_ok(self):
        return self.headers.get("Host", "").split(":")[0] in ("127.0.0.1", "localhost")

    def authorized(self):
        return self.host_ok() and self.headers.get("X-Token") == TOKEN

    def do_GET(self):
        if not self.host_ok():
            return self.send(403, "{}")
        if self.path == "/":
            return self.send(200, PAGE.replace("__TOKEN__", TOKEN), "text/html")
        if self.path == "/mini":
            return self.send(200, MINI.replace("__TOKEN__", TOKEN), "text/html")
        if not self.authorized():
            return self.send(403, "{}")
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/ls":
            q = urllib.parse.parse_qs(parsed.query)
            return self.send(200, json.dumps(list_dir((q.get("path") or ["~"])[0])))
        if self.path == "/api/state":
            with jobs_lock:
                jobs = load_jobs()
            return self.send(200, json.dumps({
                "jobs": jobs,
                "runs": {k: v.snapshot() for k, v in runs.items()},
                "mounts": smb_mounts(),
                "places": places(),
                "rsync": rsync_status(),
                "favorites": load_favorites(),
            }))
        self.send(404, "{}")

    def do_POST(self):
        if not self.authorized():
            return self.send(403, "{}")
        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return self.send(400, "{}")

        if self.path == "/api/jobs":
            job = body
            job["id"] = job.get("id") or uuid.uuid4().hex[:8]
            with jobs_lock:
                jobs = [j for j in load_jobs() if j["id"] != job["id"]]
                jobs.append(job)
                save_jobs(jobs)
            return self.send(200, json.dumps({"id": job["id"]}))
        if self.path == "/api/delete":
            with jobs_lock:
                save_jobs([j for j in load_jobs() if j["id"] != body.get("id")])
            return self.send(200, "{}")
        if self.path in ("/api/run", "/api/dry"):
            job = next((j for j in load_jobs() if j["id"] == body.get("id")), None)
            if not job:
                return self.send(404, "{}")
            start_run(job, dry=self.path == "/api/dry")
            return self.send(200, "{}")
        if self.path in ("/api/pause", "/api/resume"):
            run = runs.get(body.get("id"))
            ok = bool(run) and (run.pause() if self.path == "/api/pause" else run.resume())
            save_history()
            return self.send(200, json.dumps({"ok": ok}))
        if self.path == "/api/prioritize":
            keep = body.get("id")
            for rid, run in list(runs.items()):
                if rid == keep:
                    run.resume()
                else:
                    run.pause()
            return self.send(200, "{}")
        if self.path == "/api/stop":
            run = runs.get(body.get("id"))
            if run:
                run.stop()
            return self.send(200, "{}")
        if self.path == "/api/transfer":
            items, dest = body.get("items") or [], body.get("dest") or ""
            if not items or not os.path.isdir(dest):
                return self.send(400, json.dumps({"error": "Sélection ou destination invalide."}))
            d = os.path.realpath(dest)
            for it in items:
                r = os.path.realpath(it)
                if os.path.isdir(r) and (d == r or d.startswith(r + "/")):
                    return self.send(400, json.dumps({"error": "Impossible de copier un dossier dans lui-même."}))
            name = "Copie de %d élément(s)" % len(items) if len(items) > 1 else "Copie de " + os.path.basename(items[0])
            return self.send(200, json.dumps({"id": start_ephemeral({"name": name, "items": items, "dest": dest})}))
        if self.path in ("/api/sync", "/api/syncdry"):
            src, dest = body.get("source") or "", body.get("dest") or ""
            if not os.path.isdir(src) or not os.path.isdir(dest) or os.path.realpath(src) == os.path.realpath(dest):
                return self.send(400, json.dumps({"error": "Source ou destination invalide."}))
            name = "Synchro de " + os.path.basename(src.rstrip("/"))
            return self.send(200, json.dumps({"id": start_ephemeral({"name": name, "source": src, "dest": dest}, dry=self.path == "/api/syncdry")}))
        if self.path == "/api/size":
            path = body.get("path") or ""
            if not os.path.isabs(path) or not os.path.isdir(path):
                return self.send(400, json.dumps({"error": "Dossier invalide."}))
            size, count, partial = dir_size(path)
            return self.send(200, json.dumps({"size": size, "files": count, "partial": partial}))
        if self.path == "/api/fav":
            path = body.get("path") or ""
            if not os.path.isabs(path):
                return self.send(400, json.dumps({"error": "Chemin invalide."}))
            return self.send(200, json.dumps({"favorites": set_favorite(path, bool(body.get("add")))}))
        if self.path == "/api/rsync-update":
            threading.Thread(target=update_rsync, daemon=True).start()
            return self.send(200, "{}")
        if self.path == "/api/mkdir":
            try:
                os.mkdir(body.get("path", ""))
            except OSError as exc:
                return self.send(400, json.dumps({"error": exc.strerror or str(exc)}))
            return self.send(200, "{}")
        if self.path == "/api/clear":
            run = runs.get(body.get("id"))
            if run and run.status not in ACTIVE:
                runs.pop(body["id"], None)
                save_history()
            return self.send(200, "{}")
        if self.path == "/api/clear-finished":
            for rid in [k for k, r in runs.items() if r.status not in ACTIVE]:
                runs.pop(rid, None)
            save_history()
            return self.send(200, "{}")
        if self.path == "/api/restart":
            run = runs.get(body.get("id"))
            if not run or run.status in ACTIVE:
                return self.send(400, json.dumps({"error": "Transfert introuvable ou déjà en cours."}))
            start_run(dict(run.job), keep_log=True)
            return self.send(200, "{}")
        if self.path == "/api/browse":
            return self.send(200, json.dumps({"path": browse_folder()}))
        self.send(404, "{}")


PAGE = r"""<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Rsync GUI</title>
<style>
:root{--bg:#f5f5f7;--card:#fff;--fg:#1d1d1f;--mut:#6e6e73;--line:#e3e3e8;--acc:#0a66ff;--sel:#dbe8ff;--ok:#1a8f4c;--err:#d6322e;--warn:#b7791f}
@media(prefers-color-scheme:dark){:root{--bg:#1c1c1e;--card:#2a2a2d;--fg:#f2f2f5;--mut:#9a9aa2;--line:#3a3a3e;--acc:#4c8dff;--sel:#27406b;--ok:#43c077;--err:#ff6b66;--warn:#e0b050}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:13.5px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
main{max-width:1280px;margin:0 auto;padding:18px 16px 60px}
header{display:flex;align-items:center;justify-content:space-between;margin-bottom:10px}
h1{font-size:20px;margin:0}h2{font-size:15px;margin:22px 0 8px;display:flex;justify-content:space-between;align-items:center}
button,select{font:inherit;border:1px solid var(--line);background:var(--card);color:var(--fg);border-radius:8px;padding:5px 10px;cursor:pointer}
button:hover{border-color:var(--acc)}button.p{background:var(--acc);border-color:var(--acc);color:#fff}
button.d{color:var(--err)}button:disabled{opacity:.45;cursor:default}
.mounts{font-size:12px;color:var(--mut)}.mounts button{font-size:11.5px;padding:1px 8px;margin-left:6px}
.panes{display:grid;grid-template-columns:1fr 130px 1fr;gap:10px;align-items:stretch}
@media(max-width:860px){.panes{grid-template-columns:1fr}.mid{flex-direction:row!important}}
.pane{background:var(--card);border:1px solid var(--line);border-radius:12px;display:flex;flex-direction:column;min-width:0;height:460px;overflow:hidden}
.pane.over{outline:2px solid var(--acc)}
.bar{display:flex;gap:5px;padding:8px;border-bottom:1px solid var(--line);flex-wrap:wrap}
.bar input{flex:1;min-width:120px;font:inherit;padding:5px 8px;border-radius:8px;border:1px solid var(--line);background:var(--bg);color:var(--fg)}
.bar select{max-width:150px}
.head,.row{display:grid;grid-template-columns:1fr 72px 100px;gap:8px;padding:3px 10px;align-items:center}
.head{color:var(--mut);font-size:12px;border-bottom:1px solid var(--line)}
.list{overflow:auto;flex:1;user-select:none}
.row{cursor:default}.row:hover{background:var(--bg)}.row.sel{background:var(--sel)}.row.dropt{outline:2px solid var(--acc);outline-offset:-2px}
.row .n{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.row .s,.row .m{color:var(--mut);font-size:12px;text-align:right;white-space:nowrap}
.foot{padding:5px 10px;border-top:1px solid var(--line);font-size:12px;color:var(--mut);display:flex;justify-content:space-between}
.err{color:var(--err);padding:12px}
.mid{display:flex;flex-direction:column;gap:8px;justify-content:center}.mid button{width:100%}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:12px 14px;margin-bottom:10px}
.top{display:flex;justify-content:space-between;gap:12px;align-items:flex-start}
.name{font-weight:600}.path{color:var(--mut);font-size:12.5px;word-break:break-all}
.badge{font-size:12px;padding:2px 9px;border-radius:99px;border:1px solid var(--line);white-space:nowrap}
.running,.starting{color:var(--acc)}.retrying,.paused,.interrupted{color:var(--warn)}.done{color:var(--ok)}.error{color:var(--err)}
.card.isp .pb>i{background:var(--warn)}
.line{display:flex;gap:10px;justify-content:space-between;align-items:baseline;margin-top:10px}
.line .cur{flex:1;min-width:0}.line b{font-variant-numeric:tabular-nums}
.tot{border-top:1px solid var(--line);margin-top:10px;padding-top:2px}
.pb{height:8px;background:var(--line);border-radius:6px;overflow:hidden;margin:10px 0 4px}
.pb>i{display:block;height:100%;background:var(--acc);width:0;transition:width .4s}
.cur{font-size:12.5px;color:var(--mut);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.btns{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}
pre{background:var(--bg);border-radius:8px;padding:8px 10px;max-height:220px;overflow:auto;font-size:12px;margin:10px 0 0;white-space:pre-wrap;word-break:break-all}
dialog{border:1px solid var(--line);border-radius:14px;background:var(--card);color:var(--fg);width:min(600px,92vw);padding:20px}
dialog::backdrop{background:#0006}
label{display:block;font-size:12.5px;color:var(--mut);margin:12px 0 4px}
dialog input[type=text],dialog input[type=number]{width:100%;font:inherit;padding:7px 9px;border-radius:8px;border:1px solid var(--line);background:var(--bg);color:var(--fg)}
.chk{display:flex;gap:8px;align-items:center;margin-top:10px;font-size:13.5px}
.hint{font-size:12px;color:var(--mut);margin-top:4px}.empty{color:var(--mut);padding:10px 0}
.favs{display:flex;flex-wrap:wrap;gap:6px;align-items:center;margin:0 0 10px;min-height:28px}
.favs .lbl{font-size:12px;color:var(--mut);margin-right:2px}
.chip{display:inline-flex;align-items:center;gap:2px;border:1px solid var(--line);background:var(--card);border-radius:99px;padding:2px 4px 2px 11px;max-width:260px}
.chip:hover{border-color:var(--acc)}.chip .t{cursor:pointer;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;padding:1px 2px}
.chip .x{border:0;background:transparent;color:var(--mut);padding:0 6px;border-radius:99px;font-size:15px;line-height:1.3}
.chip .x:hover{background:var(--err);color:#fff}
#dd{position:fixed;z-index:60;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:4px;min-width:270px;max-width:440px;max-height:62vh;overflow:auto;box-shadow:0 8px 28px #0004;display:none}
#dd .g{font-size:11.5px;color:var(--mut);padding:6px 10px 3px}
#dd .it{display:flex;justify-content:space-between;align-items:center;gap:8px;padding:5px 10px;border-radius:6px;cursor:default}
#dd .it .tx{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#dd .it:hover{background:var(--acc);color:#fff}
#dd .it .x{border:0;background:transparent;color:inherit;opacity:.55;padding:0 7px;border-radius:99px;font-size:15px;line-height:1.3}
#dd .it .x:hover{opacity:1;background:var(--err);color:#fff}
.ddbtn{max-width:150px;white-space:nowrap}
#ctx{position:fixed;z-index:50;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:4px;min-width:210px;box-shadow:0 8px 28px #0004;display:none}
#ctx div{padding:6px 12px;border-radius:6px;cursor:default;white-space:nowrap}#ctx div:hover{background:var(--acc);color:#fff}
#ctx hr{border:0;border-top:1px solid var(--line);margin:4px 2px}
.bar .fav{padding:5px 8px}.bar .fav.on{color:#e0a800;border-color:#e0a800}
#toast{position:fixed;left:50%;bottom:22px;transform:translateX(-50%);background:var(--fg);color:var(--bg);padding:8px 14px;border-radius:10px;opacity:0;transition:opacity .25s;pointer-events:none}
#toast.on{opacity:.95}
</style></head><body><main>
<header><h1>Rsync GUI</h1><div style="text-align:right"><div class="mounts" id="mounts"></div>
<div class="mounts" id="rs"></div>
<div class="mounts"><button id="miniBtn" style="display:none" onclick="window.webkit.messageHandlers.app.postMessage('mini')">Mode miniature</button></div></div></header>

<div class="panes">
 <section class="pane" id="paneL"></section>
 <div class="mid">
  <button class="p" onclick="copySel('L','R')">Copier →</button>
  <button class="p" onclick="copySel('R','L')">← Copier</button>
  <button onclick="syncDir('L','R',false)" title="Copie tout le contenu du dossier de gauche vers celui de droite">Synchroniser →</button>
  <button onclick="syncDir('L','R',true)" title="Affiche ce qui serait copié, sans rien copier">Simuler →</button>
  <button onclick="memorize()" title="Enregistrer gauche → droite comme tâche réutilisable">☆ Mémoriser</button>
 </div>
 <section class="pane" id="paneR"></section>
</div>
<div class="hint" style="margin-top:6px">Clic = sélectionner (⌘ ajouter, ⇧ plage) · double-clic = ouvrir un dossier · glisser les éléments d'un panneau vers l'autre pour les copier · clic droit = favoris et options.</div>

<h2>Transferts <button onclick="clearFinished()">Effacer l'historique</button></h2><div id="transfers"></div>
<h2>Tâches enregistrées <button class="p" onclick="openForm()">+ Nouvelle tâche</button></h2><div id="jobs"></div>

<dialog id="dlg"><form method="dialog" onsubmit="return false">
<h2 style="margin:0 0 4px" id="dtitle">Tâche</h2>
<label>Nom</label><input type="text" id="f_name" placeholder="Vidéos de l événement">
<label>Source (dossier : son contenu est copié)</label><input type="text" id="f_source">
<label>Destination</label><input type="text" id="f_dest" oninput="detect()">
<label>Adresse de remontage du NAS (optionnel)</label>
<input type="text" id="f_url" placeholder="smb://nas.local/archives">
<div class="hint">Remonte le partage tout seul après une coupure (identifiants du Trousseau).</div>
<label>Tentatives maximum</label><input type="number" id="f_retries" min="1" value="20">
<div class="chk"><input type="checkbox" id="f_ds" checked><span>Ignorer les .DS_Store</span></div>
<div class="chk"><input type="checkbox" id="f_mirror"><span>Mode miroir : supprime à la destination ce qui n'est plus dans la source</span></div>
<div class="btns" style="justify-content:flex-end;margin-top:18px">
<button type="button" onclick="dlg.close()">Annuler</button><button type="button" class="p" onclick="saveJob()">Enregistrer</button></div>
</form></dialog>
<div id="ctx"></div>
<div id="dd"></div>
<div id="toast"></div>

<script>
const T="__TOKEN__";const $=id=>document.getElementById(id);const dlg=$("dlg");
let activeSide="L";
let state={jobs:[],runs:{},mounts:{},places:[],favorites:[]},editId=null,openLogs={},sig="",placesSig="";
const P={L:{path:"",entries:[],sel:new Set(),last:-1,error:"",sizes:{},gen:0},R:{path:"",entries:[],sel:new Set(),last:-1,error:"",sizes:{},gen:0}};

async function api(p,b){const r=await fetch(p,{method:b===undefined?"GET":"POST",headers:{"X-Token":T,"Content-Type":"application/json"},body:b===undefined?undefined:JSON.stringify(b)});
 const j=await r.json();if(!r.ok&&j.error)throw new Error(j.error);return j}
function toast(m){const t=$("toast");t.textContent=m;t.classList.add("on");clearTimeout(toast.h);toast.h=setTimeout(()=>t.classList.remove("on"),2600)}
const esc=s=>String(s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const fmtT=s=>s>=3600?Math.floor(s/3600)+"h"+String(Math.floor(s%3600/60)).padStart(2,"0"):Math.floor(s/60)+"m"+String(s%60).padStart(2,"0")+"s";
const fmtDur=s=>{s=Math.max(0,Math.round(s));const h=Math.floor(s/3600),m=Math.floor(s%3600/60),r=s%60;return h?h+":"+String(m).padStart(2,"0")+":"+String(r).padStart(2,"0"):m+":"+String(r).padStart(2,"0")};
const fmtEta=s=>s<60?Math.max(1,Math.round(s))+" s":s<3600?"~"+Math.round(s/60)+" min":"~"+Math.floor(s/3600)+" h "+String(Math.round(s%3600/60)).padStart(2,"0");
const fmtV=v=>fmtS(v)+"/s";
const fmtS=b=>(b<1024?Math.round(b)+" o":b<1048576?(b/1024).toFixed(0)+" Ko":b<1073741824?(b/1048576).toFixed(1)+" Mo":(b/1073741824).toFixed(2)+" Go").replace(".",",");
const fmtD=t=>{const d=new Date(t*1000);return d.toLocaleDateString("fr-FR")+" "+d.toLocaleTimeString("fr-FR",{hour:"2-digit",minute:"2-digit"})};
const other=s=>s==="L"?"R":"L";
const join=(a,b)=>a.replace(/\/$/,"")+"/"+b;

/* ---------- panneaux ---------- */
function buildPane(side){
 const el=$("pane"+side);el.innerHTML="";
 const bar=document.createElement("div");bar.className="bar";
 const sel=document.createElement("button");sel.id="pl"+side;sel.className="ddbtn";sel.textContent="Raccourcis ▾";
 sel.onclick=e=>{e.stopPropagation();ddSide===side?closeDD():openDD(side,sel)};
 const up=document.createElement("button");up.textContent="↑";up.title="Dossier parent";up.onclick=()=>go(side,P[side].parent);
 const inp=document.createElement("input");inp.id="pi"+side;inp.spellcheck=false;inp.onkeydown=e=>{if(e.key==="Enter")go(side,inp.value)};
 const rf=document.createElement("button");rf.textContent="⟳";rf.title="Actualiser";rf.onclick=()=>go(side,P[side].path);
 const fv=document.createElement("button");fv.className="fav";fv.id="fv"+side;fv.textContent="☆";fv.title="Ajouter / retirer ce dossier des favoris";fv.onclick=()=>toggleFav(P[side].path);
 const sg=document.createElement("button");sg.textContent="Σ";sg.title="Calculer la taille de tous les dossiers affichés";sg.onclick=()=>calcAll(side);
 const mk=document.createElement("button");mk.textContent="+ Dossier";mk.onclick=()=>mkdir(side);
 bar.append(sel,up,inp,fv,sg,rf,mk);
 const head=document.createElement("div");head.className="head";head.innerHTML="<span>Nom</span><span style='text-align:right'>Taille</span><span style='text-align:right'>Modifié</span>";
 const list=document.createElement("div");list.className="list";list.id="ls"+side;
 const foot=document.createElement("div");foot.className="foot";foot.id="ft"+side;
 el.append(bar,head,list,foot);
 list.oncontextmenu=ev=>{if(ev.target!==list)return;const path=P[side].path,isFav=(state.favorites||[]).includes(path);
  showCtx(ev,[[isFav?"★ Retirer ce dossier des favoris":"☆ Ajouter ce dossier aux favoris",()=>toggleFav(path)],
   ["Nouveau dossier…",()=>mkdir(side)],["Actualiser",()=>go(side,path)]])};
 el.onmousedown=()=>{activeSide=side};
 el.ondragover=e=>{e.preventDefault();el.classList.add("over")};
 el.ondragleave=()=>el.classList.remove("over");
 el.ondrop=e=>{e.preventDefault();el.classList.remove("over");onDrop(e,side,P[side].path)};
 fillPlaces(side);
}
function fillPlaces(side){refreshStar(side);refreshDD()}
/* menu déroulant des raccourcis : favoris (avec ×) puis emplacements */
let ddSide=null;
function openDD(side,btn){ddSide=side;drawDD();const d=$("dd");d.style.display="block";
 const r=btn.getBoundingClientRect(),w=d.offsetWidth;
 d.style.left=Math.max(8,Math.min(r.left,innerWidth-w-8))+"px";d.style.top=(r.bottom+4)+"px"}
function closeDD(){$("dd").style.display="none";ddSide=null}
function refreshDD(){if(ddSide)drawDD()}
function drawDD(){const d=$("dd");d.innerHTML="";const favs=state.favorites||[];
 const group=t=>{const g=document.createElement("div");g.className="g";g.textContent=t;d.append(g)};
 const row=(label,path,removable)=>{const r=document.createElement("div");r.className="it";r.title=path;
  const t=document.createElement("span");t.className="tx";t.textContent=label;r.append(t);
  if(removable){const x=document.createElement("button");x.className="x";x.textContent="×";x.title="Retirer des favoris";
   x.onclick=e=>{e.stopPropagation();toggleFav(path)};r.append(x)}
  r.onclick=()=>{const s=ddSide;closeDD();go(s,path)};d.append(r)};
 if(favs.length){group("★ Favoris");favs.forEach(f=>{const parts=f.split("/").filter(Boolean);
  row(parts.slice(-1)[0]+(parts.length>1?"  —  "+parts.slice(-2,-1)[0]:""),f,true)})}
 group("Emplacements");state.places.forEach(p=>row(p.label,p.path,false))}
document.addEventListener("click",e=>{if(!e.target.closest("#dd"))closeDD()});
document.addEventListener("keydown",e=>{if(e.key==="Escape")closeDD()});
function refreshStar(side){const b=$("fv"+side);if(!b)return;const on=(state.favorites||[]).includes(P[side].path);
 b.textContent=on?"★":"☆";b.classList.toggle("on",on)}
async function toggleFav(path){if(!path)return;const add=!(state.favorites||[]).includes(path);
 try{const r=await api("/api/fav",{path,add});state.favorites=r.favorites;fillPlaces("L");fillPlaces("R");
  toast(add?"Ajouté aux favoris":"Retiré des favoris")}catch(e){alert(e.message)}}

/* menu contextuel */
function hideCtx(){$("ctx").style.display="none"}
document.addEventListener("click",hideCtx);document.addEventListener("scroll",hideCtx,true);window.addEventListener("blur",hideCtx);
document.addEventListener("keydown",e=>{if(e.key==="Escape")hideCtx()});
function showCtx(ev,items){ev.preventDefault();const c=$("ctx");c.innerHTML="";
 items.forEach(it=>{if(it==="-"){c.append(document.createElement("hr"));return}
  const d=document.createElement("div");d.textContent=it[0];d.onclick=e=>{e.stopPropagation();hideCtx();it[1]()};c.append(d)});
 c.style.display="block";const w=c.offsetWidth,h=c.offsetHeight;
 c.style.left=Math.min(ev.clientX,innerWidth-w-8)+"px";c.style.top=Math.min(ev.clientY,innerHeight-h-8)+"px"}
async function go(side,path){
 try{const r=await api("/api/ls?path="+encodeURIComponent(path));
  Object.assign(P[side],{path:r.path,parent:r.parent,entries:r.entries,error:r.error||"",sel:new Set(),last:-1,sizes:{},gen:P[side].gen+1});
  try{localStorage.setItem("rg_"+side,r.path)}catch(e){}
 }catch(e){P[side].error=String(e.message||e)}
 drawList(side)}
function drawList(side){
 const p=P[side];$("pi"+side).value=p.path;refreshStar(side);const list=$("ls"+side);list.innerHTML="";
 if(p.error){const d=document.createElement("div");d.className="err";d.textContent="Erreur : "+p.error;list.append(d)}
 p.entries.forEach((e,i)=>{
  const r=document.createElement("div");r.className="row"+(p.sel.has(i)?" sel":"");r.draggable=true;
  const n=document.createElement("span");n.className="n";n.textContent=(e.d?"📁 ":"📄 ")+e.n;
  const s=document.createElement("span");s.className="s";s.textContent=e.d?sizeLabel(p.sizes[e.n]):fmtS(e.s);if(e.d&&p.sizes[e.n]&&p.sizes[e.n].title)s.title=p.sizes[e.n].title;
  const m=document.createElement("span");m.className="m";m.textContent=fmtD(e.m);
  r.append(n,s,m);
  r.onclick=ev=>{
   if(ev.shiftKey&&p.last>=0){const [a,b]=[Math.min(p.last,i),Math.max(p.last,i)];if(!(ev.metaKey||ev.ctrlKey))p.sel.clear();for(let k=a;k<=b;k++)p.sel.add(k)}
   else if(ev.metaKey||ev.ctrlKey){p.sel.has(i)?p.sel.delete(i):p.sel.add(i);p.last=i}
   else{p.sel=new Set([i]);p.last=i}
   drawList(side)};
  r.ondblclick=()=>{if(e.d)go(side,join(p.path,e.n))};
  r.oncontextmenu=ev=>{
   if(!p.sel.has(i)){p.sel=new Set([i]);p.last=i;drawList(side)}
   const full=join(p.path,e.n),o=other(side),isFav=(state.favorites||[]).includes(full),items=[];
   if(e.d){items.push(["Ouvrir",()=>go(side,full)]);
    items.push(["Calculer la taille",()=>{[...p.sel].forEach(k=>{if(p.entries[k].d)calcSize(side,p.entries[k].n)})}]);
    items.push([isFav?"★ Retirer des favoris":"☆ Ajouter aux favoris",()=>toggleFav(full)]);items.push("-")}
   items.push(["Copier vers l'autre panneau →",()=>copySel(side,o)]);
   showCtx(ev,items)};
  r.ondragstart=ev=>{if(!p.sel.has(i)){p.sel=new Set([i]);p.last=i;drawList(side)}ev.dataTransfer.setData("text/plain","rg:"+side);ev.dataTransfer.effectAllowed="copy"};
  if(e.d){r.ondragover=ev=>{ev.preventDefault();ev.stopPropagation();r.classList.add("dropt")};
   r.ondragleave=()=>r.classList.remove("dropt");
   r.ondrop=ev=>{ev.preventDefault();ev.stopPropagation();r.classList.remove("dropt");$("pane"+side).classList.remove("over");onDrop(ev,side,join(p.path,e.n))}}
  list.append(r)});
 const n=p.sel.size;let tot=0,unk=0;p.sel.forEach(i=>{const e=p.entries[i];if(!e.d)tot+=e.s;else{const z=p.sizes[e.n];if(z&&typeof z.bytes==="number")tot+=z.bytes;else unk++}});
 $("ft"+side).innerHTML="<span>"+p.entries.length+" élément(s)</span><span>"+(n?n+" sélectionné(s) · "+fmtS(tot)+(unk?" + "+unk+" dossier(s) non calculé(s)":""):"")+"</span>";
}
function sizeLabel(z){if(!z)return"";if(z.pending)return"calcul…";if(z.error)return"—";return(z.partial?"≥ ":"")+fmtS(z.bytes)}
async function calcSize(side,name){const p=P[side],gen=p.gen;if(p.sizes[name]&&(p.sizes[name].pending||typeof p.sizes[name].bytes==="number"))return;
 p.sizes[name]={pending:true};drawList(side);
 try{const r=await api("/api/size",{path:join(p.path,name)});if(P[side].gen!==gen)return;
  p.sizes[name]={bytes:r.size,partial:r.partial,title:r.files+" fichier(s)"+(r.partial?" — calcul interrompu (trop long)":"")}}
 catch(e){if(P[side].gen!==gen)return;p.sizes[name]={error:true}}
 drawList(side)}
async function calcAll(side){const p=P[side],gen=p.gen,names=p.entries.filter(e=>e.d).map(e=>e.n);
 if(!names.length){toast("Aucun dossier dans cette liste.");return}
 let k=0;const worker=async()=>{while(k<names.length&&P[side].gen===gen){await calcSize(side,names[k++])}};
 await Promise.all([worker(),worker()])}
function onDrop(ev,toSide,destPath){const d=ev.dataTransfer.getData("text/plain");
 if(!d.startsWith("rg:"))return;const from=d.slice(3);if(from===toSide&&destPath===P[from].path)return;
 copySel(from,toSide,destPath)}
async function copySel(from,to,destPath){
 const p=P[from];const items=[...p.sel].map(i=>join(p.path,p.entries[i].n));
 if(!items.length){toast("Sélectionnez d'abord des fichiers ou dossiers.");return}
 try{await api("/api/transfer",{items,dest:destPath||P[to].path});toast("Transfert lancé");sig="";refresh()}catch(e){alert(e.message)}}
async function syncDir(from,to,dry){
 const src=P[from].path,dest=P[to].path;
 if(!dry&&!confirm("Synchroniser le contenu de\n"+src+"\nvers\n"+dest+" ?\n\nRien n'est supprimé à la destination."))return;
 try{await api(dry?"/api/syncdry":"/api/sync",{source:src,dest});toast(dry?"Simulation lancée":"Synchronisation lancée");sig="";refresh()}catch(e){alert(e.message)}}
async function mkdir(side){const n=prompt("Nom du nouveau dossier :");if(!n)return;
 try{await api("/api/mkdir",{path:join(P[side].path,n)});go(side,P[side].path)}catch(e){alert(e.message)}}
function memorize(){openForm(null,{source:P.L.path,dest:P.R.path,name:"Synchro "+P.L.path.split("/").pop()})}

/* ---------- transferts & tâches ---------- */
const LAB={starting:"Démarrage",running:"En cours",retrying:"Reprise…",paused:"En pause",interrupted:"Interrompu",done:"Terminé",error:"Échec",stopped:"Arrêté"};
const fmtDate=t=>{const d=new Date(t*1000);return d.toLocaleDateString("fr-FR",{day:"2-digit",month:"2-digit"})+" "+d.toLocaleTimeString("fr-FR",{hour:"2-digit",minute:"2-digit"})};
function ctl(id,st){
 if(st!=="running"&&st!=="paused")return"";
 let h=st==="running"?`<button data-act="pause" data-id="${id}">⏸ Pause</button>`:`<button class="p" data-act="resume" data-id="${id}">▶ Reprendre</button>`;
 const others=Object.entries(state.runs).some(([i,x])=>i!==id&&["starting","running","retrying"].includes(x.status));
 if(others)h+=`<button data-act="prioritize" data-id="${id}" title="Met en pause tous les autres transferts">Prioriser</button>`;
 return h}
function progressHTML(r){
 const st=r.status,done=st==="done";
 if(r.planning&&!r.current&&!r.files&&!done)return '<div class="cur" style="margin-top:8px">Démarrage de la copie… (le total est calculé en arrière-plan)</div><div class="pb"><i style="width:0"></i></div>';
 if(done&&r.total_files===0)return '<div class="cur" style="margin-top:8px">Rien à copier : tout est déjà à jour.</div>';
 let h="";
 if(r.current||done){
  const sz=r.cur_size?fmtS(r.cur_bytes)+" / "+fmtS(r.cur_size):"";
  const bits=[sz,r.cur_speed?fmtV(r.cur_speed):"",r.cur_elapsed?"écoulé "+fmtDur(r.cur_elapsed):"",r.cur_eta!=null?"restant "+fmtDur(r.cur_eta):""].filter(Boolean).join(" · ");
  h+=`<div class="line"><span class="cur">${esc(r.current||"")}</span><b>${done?100:r.percent}%</b></div><div class="pb"><i style="width:${done?100:r.percent}%"></i></div><div class="cur">${bits}</div>`}
 if(r.total_files>1){
  const pct=r.overall>=0?r.overall:0;
  const cnt=`${r.files_done} / ${r.total_files} fichiers`+(r.total_dirs>0?` · ${r.dirs_done} / ${r.total_dirs} dossiers`:"");
  const bits=[r.total_bytes?fmtS(r.done_bytes)+" / "+fmtS(r.total_bytes):"",r.avg_speed?"vitesse moyenne "+fmtV(r.avg_speed):"",
   r.total_eta!=null&&!done?"restant "+fmtEta(r.total_eta):"","écoulé "+fmtDur(r.active)].filter(Boolean).join(" · ");
  h+=`<div class="tot"><div class="line"><b>${cnt}</b><b>${pct}%</b></div><div class="pb"><i style="width:${done?100:pct}%"></i></div><div class="cur">${bits}</div></div>`}
 else if(r.planning&&!done)h+=`<div class="tot"><div class="cur" style="margin-top:8px">${r.files_done} fichier(s) copié(s) · calcul du total en arrière-plan…</div></div>`;
 else if(!r.current&&!done)h+=`<div class="cur" style="margin-top:8px">${r.files} fichiers · ${fmtDur(r.elapsed)}</div>`;
 return h}
function card(id,name,sub,r,extra){
 const st=r?r.status:"",busy=["starting","running","retrying","paused"].includes(st);
 return `<div class="card${st==="paused"?" isp":""}"><div class="top"><div><div class="name">${esc(name||"Sans nom")}</div><div class="path">${sub}</div></div>
 <span class="badge ${st}">${r?LAB[st]+(r.attempt>1?" · essai "+r.attempt:""):"Prêt"}</span></div>
 ${r?progressHTML(r):""}
 <div class="btns">${extra(busy,st)}<button data-log="${id}">${openLogs[id]?"Masquer le journal":"Journal"}</button></div>
 ${openLogs[id]&&r?`<pre>${esc(r.log.join("\n"))}</pre>`:""}</div>`}
function renderLists(){
 const tr=Object.entries(state.runs).filter(([k,r])=>r.ephemeral);
 $("transfers").innerHTML=tr.length?tr.reverse().map(([k,r])=>card(k,r.name,esc(r.source)+"<br>→ "+esc(r.dest)+"<br>"+fmtDate(r.started_at),r,(busy,st)=>
  busy?ctl(k,st)+`<button class="d" data-act="stop" data-id="${k}">Arrêter</button>`:
  (["interrupted","stopped","error"].includes(st)?`<button class="p" data-act="restart" data-id="${k}">↻ Reprendre</button>`:"")+`<button data-act="clear" data-id="${k}">Effacer</button>`)).join(""):'<div class="empty">Aucun transfert en cours.</div>';
 $("jobs").innerHTML=state.jobs.length?state.jobs.map(j=>card(j.id,j.name,esc(j.source)+"<br>→ "+esc(j.dest),state.runs[j.id],(busy)=>
  (busy?ctl(j.id,st)+`<button class="d" data-act="stop" data-id="${j.id}">Arrêter</button>`:`<button class="p" data-act="run" data-id="${j.id}">Lancer</button><button data-act="dry" data-id="${j.id}">Simuler</button>`)+
  `<button ${busy?"disabled":""} data-act="edit" data-id="${j.id}">Modifier</button><button class="d" ${busy?"disabled":""} data-act="del" data-id="${j.id}">Supprimer</button>`)).join(""):'<div class="empty">Aucune tâche enregistrée.</div>';
 const rs=state.rsync||{},u=rs.update||{},busyU=["checking","building"].includes(u.state);
 $("rs").innerHTML="rsync "+(rs.version?esc(rs.version)+" ("+esc(rs.origin)+")":"introuvable")+(u.message?" · "+esc(u.message):"")+
  `<button ${busyU?"disabled":""} onclick="updRsync()">${busyU?"Mise à jour…":"Mettre à jour rsync"}</button>`;
 const m=Object.entries(state.mounts);$("mounts").textContent=m.length?"Partages SMB montés : "+m.map(x=>x[0].replace("/Volumes/","")).join(" · "):"Aucun partage SMB monté";
}
document.addEventListener("click",async e=>{
 const b=e.target.closest("button");if(!b)return;
 if(b.dataset.log){openLogs[b.dataset.log]=!openLogs[b.dataset.log];sig="";renderLists();return}
 const a=b.dataset.act,id=b.dataset.id;if(!a)return;
 if(a==="edit")return openForm(id);
 if(a==="del"){if(confirm("Supprimer cette tâche ? (les fichiers ne sont pas touchés)")){await api("/api/delete",{id});sig="";refresh()}return}
 await api("/api/"+a,{id});sig="";refresh()});
async function refresh(){try{state=await api("/api/state");
 const ps=JSON.stringify([state.places,state.favorites]);if(ps!==placesSig){placesSig=ps;fillPlaces("L");fillPlaces("R")}
 const s=JSON.stringify([state.jobs,state.runs,state.mounts,state.rsync,openLogs]);if(s!==sig&&!dlg.open){sig=s;renderLists()}}catch(e){}}

async function clearFinished(){if(!Object.values(state.runs).some(r=>!["starting","running","retrying","paused"].includes(r.status)))return;
 if(confirm("Effacer l'historique des transferts terminés ou interrompus ?")){await api("/api/clear-finished",{});sig="";refresh()}}
async function updRsync(){await api("/api/rsync-update",{});sig="";refresh()}

/* ---------- formulaire de tâche ---------- */
function openForm(id,pre){editId=id||null;const j=state.jobs.find(x=>x.id===id)||pre||{};
 $("dtitle").textContent=id?"Modifier la tâche":"Nouvelle tâche";
 $("f_name").value=j.name||"";$("f_source").value=j.source||"";$("f_dest").value=j.dest||"";$("f_url").value=j.remount_url||"";
 $("f_retries").value=j.max_retries||20;$("f_ds").checked=j.exclude_ds_store!==false;$("f_mirror").checked=!!j.mirror;detect();dlg.showModal()}
function detect(){const d=$("f_dest").value,m=d.match(/^(\/Volumes\/[^\/]+)/);if(m&&state.mounts[m[1]]&&!$("f_url").value)$("f_url").value=state.mounts[m[1]]}
async function saveJob(){const src=$("f_source").value.trim(),dst=$("f_dest").value.trim();
 if(!src||!dst){alert("Source et destination sont obligatoires.");return}
 if(src.replace(/\/$/,"")===dst.replace(/\/$/,"")){alert("Source et destination sont identiques.");return}
 await api("/api/jobs",{id:editId,name:$("f_name").value.trim(),source:src,dest:dst,remount_url:$("f_url").value.trim(),
 max_retries:parseInt($("f_retries").value)||20,exclude_ds_store:$("f_ds").checked,mirror:$("f_mirror").checked});
 dlg.close();sig="";refresh()}

if(window.webkit&&window.webkit.messageHandlers&&window.webkit.messageHandlers.app)$("miniBtn").style.display="";

/* ---------- démarrage ---------- */
buildPane("L");buildPane("R");
(async()=>{await refresh();
 const get=k=>{try{return localStorage.getItem("rg_"+k)}catch(e){return null}};
 const vol=Object.keys(state.mounts)[0];
 go("L",get("L")||"~");go("R",get("R")||vol||"/Volumes")})();
setInterval(refresh,500);
</script></main></body></html>"""


def watch_parent():
    """Mode intégré : s'arrête si l'app parente disparaît."""
    while True:
        if os.getppid() == 1:
            os._exit(0)
        time.sleep(2)


MINI = r"""<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Rsync GUI</title>
<style>
:root{--bg:#f5f5f7;--card:#fff;--fg:#1d1d1f;--mut:#6e6e73;--line:#e3e3e8;--acc:#0a66ff;--ok:#1a8f4c;--err:#d6322e;--warn:#b7791f}
@media(prefers-color-scheme:dark){:root{--bg:#1c1c1e;--card:#2a2a2d;--fg:#f2f2f5;--mut:#9a9aa2;--line:#3a3a3e;--acc:#4c8dff;--ok:#43c077;--err:#ff6b66;--warn:#e0b050}}
*{box-sizing:border-box}html,body{margin:0;height:100%}
body{background:var(--bg);color:var(--fg);font:12.5px/1.35 -apple-system,BlinkMacSystemFont,sans-serif;padding:10px;overflow:auto}
.top{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}
.top b{font-size:13px}button{font:inherit;font-size:11.5px;border:1px solid var(--line);background:var(--card);color:var(--fg);border-radius:7px;padding:2px 9px;cursor:pointer}
button:hover{border-color:var(--acc)}button.d{color:var(--err)}
.t{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:8px 10px;margin-bottom:8px}
.r{display:flex;justify-content:space-between;gap:8px;align-items:center}.n{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.s{font-size:11.5px;white-space:nowrap}.running,.starting{color:var(--acc)}.retrying,.paused,.interrupted{color:var(--warn)}.done{color:var(--ok)}.error{color:var(--err)}
.t.isp .pb>i{background:var(--warn)}.b{display:flex;gap:6px}
.pb{height:6px;background:var(--line);border-radius:4px;overflow:hidden;margin:5px 0 3px}
.tot{border-top:1px solid var(--line);margin-top:6px;padding-top:6px}.pb>i{display:block;height:100%;background:var(--acc);width:0;transition:width .4s}
.c{color:var(--mut);font-size:11.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.empty{color:var(--mut);text-align:center;padding:18px 0}
</style></head><body>
<div class="top"><b>Rsync GUI</b><button onclick="window.webkit.messageHandlers.app.postMessage('expand')">Agrandir</button></div>
<div id="list"></div>
<script>
const T="__TOKEN__";const $=id=>document.getElementById(id);
const LAB={starting:"Démarrage",running:"En cours",retrying:"Reprise…",paused:"En pause",interrupted:"Interrompu",done:"Terminé",error:"Échec",stopped:"Arrêté"};
const esc=s=>String(s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const fmt=s=>s>=3600?Math.floor(s/3600)+"h"+String(Math.floor(s%3600/60)).padStart(2,"0"):Math.floor(s/60)+"m"+String(s%60).padStart(2,"0")+"s";
const act=st=>["starting","running","retrying","paused"].includes(st);
const fmtS=b=>(b<1024?Math.round(b)+" o":b<1048576?(b/1024).toFixed(0)+" Ko":b<1073741824?(b/1048576).toFixed(1)+" Mo":(b/1073741824).toFixed(2)+" Go").replace(".",",");
const fmtDur=s=>{s=Math.max(0,Math.round(s));const h=Math.floor(s/3600),m=Math.floor(s%3600/60),r=s%60;return h?h+":"+String(m).padStart(2,"0")+":"+String(r).padStart(2,"0"):m+":"+String(r).padStart(2,"0")};
const fmtEta=s=>s<60?Math.max(1,Math.round(s))+" s":s<3600?"~"+Math.round(s/60)+" min":"~"+Math.floor(s/3600)+" h "+String(Math.round(s%3600/60)).padStart(2,"0");
function prog(x){const done=x.status==="done";
 if(x.planning&&!x.current&&!x.files&&!done)return '<div class="c">Démarrage… (total calculé en arrière-plan)</div>';
 if(done&&x.total_files===0)return '<div class="c">Rien à copier : déjà à jour.</div>';
 let h="";
 if(x.current||done){const bits=[x.cur_size?fmtS(x.cur_bytes)+" / "+fmtS(x.cur_size):"",x.cur_speed?fmtS(x.cur_speed)+"/s":"",x.cur_elapsed?"écoulé "+fmtDur(x.cur_elapsed):"",x.cur_eta!=null?"restant "+fmtDur(x.cur_eta):""].filter(Boolean).join(" · ");
  h+=`<div class="pb"><i style="width:${done?100:x.percent}%"></i></div><div class="c">${esc(x.current||"")}</div><div class="c">${bits}</div>`}
 if(x.total_files>1){const pct=x.overall>=0?x.overall:0;
  const bits=[x.files_done+"/"+x.total_files+" fichiers"+(x.total_dirs>0?" · "+x.dirs_done+"/"+x.total_dirs+" dossiers":""),
   x.total_bytes?fmtS(x.done_bytes)+"/"+fmtS(x.total_bytes):"",x.avg_speed?"moy. "+fmtS(x.avg_speed)+"/s":"",x.total_eta!=null&&!done?"restant "+fmtEta(x.total_eta):""].filter(Boolean).join(" · ");
  h+=`<div class="tot"><div class="r"><b>Total</b><span class="s">${done?100:pct}%</span></div><div class="pb"><i style="width:${done?100:pct}%"></i></div><div class="c">${bits}</div></div>`}
 if(x.planning&&!(x.total_files>1)&&!done)h+='<div class="c">'+x.files_done+" fichier(s) copié(s) · calcul du total…</div>";
 return h}
async function call(a,id){await fetch("/api/"+a,{method:"POST",headers:{"X-Token":T,"Content-Type":"application/json"},body:JSON.stringify({id})});tick()}
async function tick(){try{const r=await fetch("/api/state",{headers:{"X-Token":T}});const d=await r.json();
 const runs=Object.entries(d.runs).sort((a,b)=>(act(b[1].status)-act(a[1].status))).slice(0,5);
 $("list").innerHTML=runs.length?runs.map(([id,x])=>`<div class="t${x.status==="paused"?" isp":""}"><div class="r"><span class="n">${esc(x.name)}</span>
 <span class="s ${x.status}">${LAB[x.status]}${x.attempt>1&&act(x.status)?" · essai "+x.attempt:""}</span></div>
 ${prog(x)}<div class="r"><span class="c">${fmt(x.elapsed)} au total</span>
 <span class="b">${x.status==="running"?`<button onclick="call('pause','${id}')">⏸ Pause</button>`:x.status==="paused"?`<button onclick="call('resume','${id}')">▶ Reprendre</button>`:""}${act(x.status)?`<button class="d" onclick="call('stop','${id}')">Arrêter</button>`:""}</span></div></div>`).join(""):'<div class="empty">Aucun transfert.</div>'}catch(e){}}
tick();setInterval(tick,500);
</script></body></html>"""


def main():
    if len(sys.argv) >= 3 and sys.argv[1] == "--build-rsync":
        version = latest_rsync_version()
        print("Compilation de rsync %s ..." % ".".join(map(str, version)), flush=True)
        build_rsync(version, sys.argv[2])
        print("OK : %s" % sys.argv[2])
        return
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    url = "http://127.0.0.1:%d/" % server.server_address[1]
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    load_history()
    threading.Thread(target=history_loop, daemon=True).start()
    threading.Thread(target=update_loop, daemon=True).start()
    if EMBEDDED:
        threading.Thread(target=watch_parent, daemon=True).start()
        print("RSYNCGUI_URL %s %s" % (url, TOKEN), flush=True)
    else:
        print("Rsync GUI : %s  (Ctrl+C pour quitter)" % url)
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    try:
        save_history()  # avant l'arrêt : les transferts actifs seront retrouvés comme « interrompus »
    except OSError:
        pass
    for r in runs.values():
        r.stop()
    time.sleep(0.3)


if __name__ == "__main__":
    main()
