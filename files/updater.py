"""Автообновление Криптобота.

  py -3.12 updater.py           # один раз проверить и поставить обновление
  py -3.12 updater.py --run     # запустить ботов и раз в 6 часов проверять обновления (это делает ЗАПУСК.bat)

Как это работает:
  * Адрес обновлений лежит в update_url.txt. Там только публичный код, без ключей.
  * Сначала скачивается manifest.json и подпись manifest.sig. Подпись проверяется
    вшитым ниже открытым ключом Криптобота (Ed25519). Чужой или битый манифест не ставится.
  * Каждый файл скачивается во временный файл, сверяется sha256 из манифеста и только потом
    заменяет старый. Старая версия кладётся в папку _backup.
  * НИКОГДА не трогаются: keys.json, state/, journal/, data/, results/, отчёт*.csv, *_state.json.
    config.yaml заменяется, только если вы его не меняли (его sha256 есть в списке выпущенных версий).
  * Если интернет или сервер недоступны, боты просто работают на текущей версии.
"""
import argparse, base64, hashlib, json, os, subprocess, sys, time, urllib.parse, urllib.request
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
URL_FILE = HERE / "update_url.txt"
VERSION_FILE = HERE / "version.txt"
BACKUP_DIR = HERE / "_backup"
LOG_FILE = HERE / "updater.log"
CHECK_EVERY_H = 6

# Открытый ключ подписи Криптобота (секретная половина есть только у Криптобота).
PUBKEY_HEX = "0fcbc2620a7f1895d1d604efb5e0e59bf118569e96866d28d6c35c9428b3decd"

# Эти файлы и папки обновление не трогает никогда.
NEVER = {"keys.json", "update_url.txt", "version.txt", "updater.log"}
NEVER_DIRS = {"state", "journal", "data", "results", "_backup", ".venv", "__pycache__", ".git"}
ALLOWED_EXT = {".py", ".bat", ".txt", ".md", ".yaml"}

BOTS = [
    ("Криптобот: спот BTC", ["bot.py", "testnet", "--loop"]),
    ("Криптобот: фьючерсы", ["bybit_futures_bot.py", "--loop"]),
]
WIN = os.name == "nt"


def log(msg):
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}"
    print("[обновление] " + msg, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
        if LOG_FILE.stat().st_size > 200_000:          # не даём логу разрастись
            LOG_FILE.write_text(LOG_FILE.read_text(encoding="utf-8")[-100_000:], encoding="utf-8")
    except OSError:
        pass


def sha256(p):
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def base_url():
    if os.environ.get("CRYPTOBOT_TEST_URL"):            # только для проверки на стенде
        return os.environ["CRYPTOBOT_TEST_URL"]
    if not URL_FILE.exists():
        return ""
    u = URL_FILE.read_text(encoding="utf-8").strip()
    return u if u.startswith("https://") else ""


def fetch(url, timeout=30):
    sep = "&" if "?" in url else "?"
    req = urllib.request.Request(f"{url}{sep}t={int(time.time())}",   # мимо кэша
                                 headers={"User-Agent": "cryptobot-updater", "Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def verify_sig(data, sig_b64):
    try:
        from Crypto.PublicKey import ECC
        from Crypto.Signature import eddsa
    except ImportError:
        log("нет библиотеки pycryptodome для проверки подписи, обновление пропущено")
        return False
    try:
        key = ECC.import_key(bytes.fromhex("302a300506032b6570032100") + bytes.fromhex(PUBKEY_HEX))
        eddsa.new(key, "rfc8032").verify(data, base64.b64decode(sig_b64.strip()))
        return True
    except Exception as e:
        log(f"подпись манифеста не сошлась ({type(e).__name__}), обновление пропущено")
        return False


def safe_name(name):
    p = Path(name)
    if p.is_absolute() or ".." in p.parts or "\\" in name or ":" in name:
        return False
    if p.name in NEVER or p.name.startswith("отчёт") or p.name.endswith("_state.json"):
        return False
    if p.parts and p.parts[0] in NEVER_DIRS:
        return False
    return p.suffix.lower() in ALLOWED_EXT


def check_and_apply():
    """Возвращает (обновлено: bool, изменённые файлы: list). Никогда не бросает исключений."""
    try:
        url = base_url()
        if not url:
            log("адрес обновлений не задан (update_url.txt), работаю на текущей версии")
            return False, []
        url = url.rstrip("/") + "/"
        raw = fetch(url + "manifest.json")
        sig = fetch(url + "manifest.sig").decode("ascii")
        if not verify_sig(raw, sig):
            return False, []
        man = json.loads(raw.decode("utf-8"))
        ver = str(man.get("version", "?"))
        cur = VERSION_FILE.read_text(encoding="utf-8").strip() if VERSION_FILE.exists() else ""

        todo = []
        for name, info in man.get("files", {}).items():
            if not safe_name(name):
                log(f"пропускаю запрещённый файл в манифесте: {name}")
                continue
            dst = HERE / name
            local = sha256(dst) if dst.exists() else None
            if local == info["sha256"]:
                continue
            if info.get("mode") == "if_unmodified" and local is not None and local not in info.get("known", []):
                log(f"{name}: вы меняли этот файл, оставляю ваш вариант")
                continue
            todo.append((name, info))

        if not todo:
            if cur != ver:
                VERSION_FILE.write_text(ver, encoding="utf-8")
            log(f"версия {ver}, обновлений нет")
            return False, []

        # 1) скачиваем всё во временные файлы и проверяем
        staged = []
        for name, info in todo:
            data = fetch(url + "files/" + urllib.parse.quote(name))
            if hashlib.sha256(data).hexdigest() != info["sha256"]:
                log(f"{name}: контрольная сумма не совпала, обновление отменено")
                for _, t in staged:
                    t.unlink(missing_ok=True)
                return False, []
            tmp = HERE / (name + ".new")
            tmp.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_bytes(data)
            staged.append((name, tmp))

        # 2) бэкап и замена
        bdir = BACKUP_DIR / datetime.now().strftime("%Y%m%d-%H%M%S")
        for name, tmp in staged:
            dst = HERE / name
            if dst.exists():
                (bdir / name).parent.mkdir(parents=True, exist_ok=True)
                (bdir / name).write_bytes(dst.read_bytes())
            os.replace(tmp, dst)
        VERSION_FILE.write_text(ver, encoding="utf-8")
        names = [n for n, _ in staged]
        log(f"поставлена версия {ver}: {', '.join(names)}. Старые файлы в {bdir.name}")
        _prune_backups()
        return True, names
    except Exception as e:
        log(f"не удалось проверить обновление ({type(e).__name__}: {str(e)[:120]}), работаю на текущей версии")
        return False, []


def _prune_backups(keep=5):
    try:
        dirs = sorted(d for d in BACKUP_DIR.iterdir() if d.is_dir())
        import shutil
        for d in dirs[:-keep]:
            shutil.rmtree(d, ignore_errors=True)
    except OSError:
        pass


# ---------------- запуск и перезапуск ботов ----------------

def start_bot(title, args):
    if WIN:
        cmd = f'title {title} && "{sys.executable}" {" ".join(args)} || (echo. & echo Бот остановился с ошибкой, перезапуск через минуту... & timeout /t 60 >nul)'
        # строкой, а не списком: cmd не понимает экранирование \" из списка аргументов
        return subprocess.Popen(f'cmd /c "{cmd}"', cwd=HERE, creationflags=subprocess.CREATE_NEW_CONSOLE)
    return subprocess.Popen([sys.executable] + args, cwd=HERE)


def stop_bot(p):
    if p.poll() is not None:
        return
    if WIN:
        subprocess.call(["taskkill", "/PID", str(p.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    else:
        p.terminate()
    try:
        p.wait(20)
    except subprocess.TimeoutExpired:
        p.kill()


def pip_install():
    log("обновляю библиотеки (requirements.txt)...")
    subprocess.call([sys.executable, "-m", "pip", "install", "-q", "-r", str(HERE / "requirements.txt")], cwd=HERE)


def quiet_minute():
    if os.environ.get("CRYPTOBOT_TEST_ANYTIME"):
        return True
    # фьючерсный бот работает в ~:05 каждого часа, поэтому перезапускаем между :20 и :55
    return 20 <= datetime.now().minute <= 55


PIDS_FILE = HERE / "_running.pids"


def _is_our_process(pid):
    """Проверяем, что под этим номером всё ещё наш бот, а не чужая программа (номера процессов повторяются)."""
    try:
        if WIN:
            out = subprocess.run(["powershell", "-NoProfile", "-Command",
                                  f"(Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}').CommandLine"],
                                 capture_output=True, text=True, timeout=30).stdout
        else:
            out = Path(f"/proc/{int(pid)}/cmdline").read_bytes().decode(errors="ignore")
    except Exception:
        return False
    return any(n in out for n in ("bot.py", "bybit_futures_bot.py", "updater.py"))


def kill_old_copies():
    """Если ботов уже запускали (например, ЗАПУСК.bat нажали второй раз), закрываем старые копии,
    чтобы два одинаковых бота не торговали одновременно."""
    try:
        pids = [int(x) for x in PIDS_FILE.read_text().split()] if PIDS_FILE.exists() else []
    except (OSError, ValueError):
        pids = []
    for pid in pids:
        if pid in (os.getpid(), os.getppid()) or not _is_our_process(pid):
            continue
        if WIN:
            subprocess.call(["taskkill", "/PID", str(pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            try:
                os.kill(pid, 15)
            except OSError:
                pass


def save_pids(procs):
    try:
        PIDS_FILE.write_text(" ".join(str(p.pid) for p in procs.values()) + f" {os.getpid()}")
    except OSError:
        pass


def run(check_hours):
    kill_old_copies()
    procs = {t: start_bot(t, a) for t, a in BOTS}
    save_pids(procs)
    log("боты запущены. Обновления проверяю раз в %g ч. Это окно не закрывайте." % check_hours)
    next_check = time.time() + check_hours * 3600
    pending = False
    while True:
        time.sleep(30)
        for t, a in BOTS:                      # бот упал или окно закрыли — поднимаем снова
            if procs[t].poll() is not None:
                log(f"«{t}» не работает, запускаю снова")
                procs[t] = start_bot(t, a)
                save_pids(procs)
        if time.time() >= next_check:
            next_check = time.time() + check_hours * 3600
            r = subprocess.call([sys.executable, str(HERE / "updater.py"), "--check-only"], cwd=HERE)
            pending = pending or r == 10
        if pending and quiet_minute():
            pending = False
            log("перезапускаю ботов на новой версии")
            for p in procs.values():
                stop_bot(p)
            if (HERE / ".need_pip").exists():
                (HERE / ".need_pip").unlink(missing_ok=True)
                pip_install()
            # перезапускаем и себя — вдруг обновился сам updater.py
            sys.exit(subprocess.call([sys.executable, str(HERE / "updater.py"), "--run", "--no-first-check",
                                      "--hours", str(check_hours)], cwd=HERE))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true")
    ap.add_argument("--check-only", action="store_true")
    ap.add_argument("--no-first-check", action="store_true")
    ap.add_argument("--hours", type=float, default=CHECK_EVERY_H)
    a = ap.parse_args()
    if a.run:
        if not a.no_first_check:
            changed, names = check_and_apply()
            if "requirements.txt" in names:
                pip_install()
            if "updater.py" in names:          # новая версия самого обновлятора — запускаем её
                sys.exit(subprocess.call([sys.executable, str(HERE / "updater.py"), "--run", "--no-first-check",
                                          "--hours", str(a.hours)], cwd=HERE))
        run(a.hours)
    else:
        changed, names = check_and_apply()
        if changed and "requirements.txt" in names:
            if a.check_only:
                (HERE / ".need_pip").write_text("1")   # поставит --run перед перезапуском
            else:
                pip_install()
        sys.exit(10 if changed else 0)


if __name__ == "__main__":
    main()
