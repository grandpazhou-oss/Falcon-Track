"""Cross-platform project bootstrap. Never requires global pip packages or Cloud login."""
import argparse
import json
import os
import platform
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
import venv
import webbrowser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VENV = ROOT / ".venv"
PYTHON = VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")

def project_env():
    sys.path.insert(0, str(ROOT))
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env", override=False)
    data = Path(os.environ.get("LEAD_DATA_DIR", ROOT / "data")).expanduser()
    if not data.is_absolute():
        data = ROOT / data
    data = data.resolve()
    data.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    try:prefect_port=int(os.environ.get('LEAD_PREFECT_PORT','4200'))
    except ValueError:raise SystemExit('LEAD_PREFECT_PORT 必须是有效端口。')
    if not 1<=prefect_port<=65535:raise SystemExit('LEAD_PREFECT_PORT 必须是有效端口。')
    env.update({"LEAD_DATA_DIR": str(data), "DJANGO_SETTINGS_MODULE": "config.settings", "PYTHONUTF8": "1",
        "PREFECT_HOME": str(data / "prefect"), "PREFECT_API_URL": f"http://127.0.0.1:{prefect_port}/api",
        "PREFECT_SERVER_API_HOST": "127.0.0.1", "PREFECT_SERVER_ANALYTICS_ENABLED": "false",
        "PREFECT_SERVER_UI_API_URL": f"http://127.0.0.1:{prefect_port}/api", "LEAD_PREFECT_PORT":str(prefect_port)})
    # This project is deliberately local. Do not inherit credentials for a user's Cloud workspace.
    env.pop("PREFECT_API_KEY", None)
    env.pop("PREFECT_SERVER_DATABASE_CONNECTION_URL", None)
    env["PREFECT_SERVER_DATABASE_CONNECTION_URL"] = "sqlite+aiosqlite:///" + (data / "prefect" / "prefect.db").as_posix()
    (data / "prefect").mkdir(parents=True, exist_ok=True)
    return env, data

def setup():
    if not (3, 11) <= sys.version_info[:2] < (3, 15):
        raise SystemExit("请先安装 Python 3.11–3.14（建议 3.12）。不会修改系统 Python。")
    if not PYTHON.exists():
        print("正在创建项目专用 Python 环境……", flush=True)
        venv.create(VENV, with_pip=True)
    subprocess.run([str(PYTHON), "-m", "pip", "install", "-r", str(ROOT / "requirements.txt")], cwd=ROOT, check=True)
    if not (ROOT / ".env").exists():
        shutil.copyfile(ROOT / ".env.example", ROOT / ".env")
    subprocess.run([str(PYTHON), str(Path(__file__).resolve()), "init"], cwd=ROOT, check=True)
    print("安装完成。模型和自动采集均默认关闭；现在可以启动试用。")

def port_free(port):
    with socket.socket() as connection:
        try:
            connection.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False

def initialize(env):
    for args in [["migrate", "--noinput"], ["seed_sources"]]:
        subprocess.run([str(PYTHON), "manage.py", *args], cwd=ROOT, env=env, check=True)

def start(env, data, open_browser=True):
    from filelock import FileLock, Timeout
    lock = FileLock(str(data / "launcher.lock"), timeout=0)
    try:
        lock.acquire()
    except Timeout:
        raise SystemExit("本项目已经运行。请打开 http://127.0.0.1:8765 或先停止原实例。")
    children = []
    log_handles = []
    stop_flag = data / "stop.request"
    try:
        web_port = int(env.get("LEAD_WEB_PORT", "8765"))
        prefect_port=int(env.get('LEAD_PREFECT_PORT','4200'))
        if prefect_port==web_port or not port_free(prefect_port) or not port_free(web_port):
            raise SystemExit("工作流或网页端口已被占用。请关闭冲突的项目实例，不会自动终止其他程序。")
        initialize(env)
        stop_flag.unlink(missing_ok=True)
        logs = data / "logs"
        logs.mkdir(exist_ok=True)
        def launch(name, args):
            path = logs / (name + ".log")
            if path.exists() and path.stat().st_size > 5 * 1024 * 1024:
                path.replace(logs / (name + ".previous.log"))
            handle = path.open("a", encoding="utf-8")
            log_handles.append(handle)
            kwargs = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
            proc = subprocess.Popen(args, cwd=ROOT, env=env, stdout=handle, stderr=subprocess.STDOUT, **kwargs)
            children.append((name, proc))
            return proc
        server = launch("prefect", [str(PYTHON), "-m", "prefect", "server", "start", "--host", "127.0.0.1", "--port", str(prefect_port)])
        print("正在启动本机工作流服务……", flush=True)
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            if server.poll() is not None:
                raise RuntimeError("工作流服务未启动，详情见 data/logs/prefect.log。")
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{prefect_port}/api/health", timeout=2) as response:
                    if response.status == 200:
                        break
            except Exception:
                time.sleep(1)
        else:
            raise RuntimeError("工作流服务启动超时，详情见 data/logs/prefect.log。")
        web = launch("web", [str(PYTHON), "scripts/web.py"])
        launch("worker", [str(PYTHON), "flows.py"])
        web_deadline = time.monotonic() + 45
        while time.monotonic() < web_deadline:
            if web.poll() is not None:
                raise RuntimeError("营业网页未启动，详情见 data/logs/web.log。")
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{web_port}/", timeout=2) as response:
                    if response.status == 200:
                        break
            except Exception:
                time.sleep(1)
        else:
            raise RuntimeError("营业网页启动超时，详情见 data/logs/web.log。")
        (data / "runtime.json").write_text(json.dumps({"launcher": os.getpid(), "children": {n:p.pid for n,p in children}, "url": f"http://127.0.0.1:{web_port}"}), encoding="utf-8")
        print(f"营业页面：http://127.0.0.1:{web_port}\n任务后台：http://127.0.0.1:{prefect_port}\n保留此窗口。按 Ctrl+C 或运行停止入口即可结束。", flush=True)
        if open_browser:
            webbrowser.open(f"http://127.0.0.1:{web_port}")
        while not stop_flag.exists():
            for name, process in children:
                if process.poll() is not None:
                    raise RuntimeError(f"{name} 进程退出，请检查 data/logs/{name}.log。")
            time.sleep(1)
    except KeyboardInterrupt:
        print("正在保存并停止本机服务……", flush=True)
    finally:
        for name, process in reversed(children):
            if process.poll() is None:
                try:
                    if os.name == "nt":
                        process.send_signal(signal.CTRL_BREAK_EVENT)
                    else:
                        os.killpg(process.pid, signal.SIGTERM)
                    process.wait(timeout=15)
                except (OSError, subprocess.TimeoutExpired):
                    if os.name == "nt":
                        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    else:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
        for handle in log_handles:
            handle.close()
        (data / "runtime.json").unlink(missing_ok=True)
        stop_flag.unlink(missing_ok=True)
        lock.release()

def doctor(env, data):
    import importlib.metadata
    output = {"system": platform.platform(), "python": platform.python_version(),
        "data_directory": str(data), "free_disk_gib": round(shutil.disk_usage(data).free / 1024**3, 1),
        "model_enabled": env.get("LEAD_MODEL_ENABLED", "false"), "model_key_configured": bool(env.get("LEAD_MODEL_API_KEY")),
        "database": "PostgreSQL" if env.get("DATABASE_URL") else "SQLite · 单人试用", "versions": {}}
    for package in ["Django", "prefect", "Scrapy", "pydantic", "waitress", "psycopg"]:
        try:
            output["versions"][package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            output["versions"][package] = "missing"
    print(json.dumps(output, ensure_ascii=False, indent=2))
    subprocess.run([str(PYTHON), "manage.py", "check"], cwd=ROOT, env=env, check=True)

def in_project_venv(expected, prefix, base_prefix):
    return Path(prefix).resolve()==Path(expected).resolve() and Path(prefix).resolve()!=Path(base_prefix).resolve()


def main():
    parser = argparse.ArgumentParser(description="物流商机单机试用管理")
    parser.add_argument("command", choices=["setup", "init", "start", "stop", "doctor", "backup", "restore"])
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--file")
    args = parser.parse_args()
    if args.command == "setup":
        setup()
        return
    if not PYTHON.exists():
        raise SystemExit("请先运行安装入口，创建项目环境。")
    if not in_project_venv(VENV,sys.prefix,sys.base_prefix):
        if os.environ.get('LEAD_LOCAL_REEXECUTED')=='1':
            raise SystemExit('项目虚拟环境无法验证；请重新运行安装入口。')
        env=os.environ.copy();env['LEAD_LOCAL_REEXECUTED']='1'
        raise SystemExit(subprocess.call([str(PYTHON), str(Path(__file__).resolve()), *sys.argv[1:]], cwd=ROOT,env=env))
    env, data = project_env()
    if args.command == "init":
        initialize(env)
    elif args.command == "start":
        start(env, data, not args.no_browser)
    elif args.command == "stop":
        (data / "stop.request").touch()
        print("已请求停止；正在执行的任务会保留检查点。")
    elif args.command == "doctor":
        doctor(env, data)
    elif args.command == "backup":
        compact = [] if env.get("DATABASE_URL", "").strip() else ["--compact"]
        subprocess.run([str(PYTHON), "manage.py", "backup_data", *compact, *(["--output", args.file] if args.file else [])], cwd=ROOT, env=env, check=True)
    elif args.command == "restore":
        if not args.file:
            raise SystemExit("恢复需要 --file 指定备份文件，并使用空的数据目录。")
        # A newly selected data directory has no schema yet. Do not seed sources:
        # the restore command checks that the business database is entirely empty.
        subprocess.run([str(PYTHON), "manage.py", "migrate", "--noinput"], cwd=ROOT, env=env, check=True)
        subprocess.run([str(PYTHON), "manage.py", "restore_data", args.file], cwd=ROOT, env=env, check=True)

if __name__ == "__main__":
    main()

