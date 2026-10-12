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
WEB_DIR = os.path.join(RESOURCES, "web")
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
            return self.send(200, web_page("index.html"), "text/html")
        if self.path == "/mini":
            return self.send(200, web_page("mini.html"), "text/html")
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





def web_page(name):
    """Page de l'interface (src/web/*.html) avec le jeton de session."""
    with open(os.path.join(WEB_DIR, name), encoding="utf-8") as f:
        return f.read().replace("__TOKEN__", TOKEN)


def watch_parent():
    """Mode intégré : s'arrête si l'app parente disparaît."""
    while True:
        if os.getppid() == 1:
            os._exit(0)
        time.sleep(2)





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
