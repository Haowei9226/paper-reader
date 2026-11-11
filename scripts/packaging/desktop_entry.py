"""Entry point shared by the Windows executable and the macOS app bundle."""
import ctypes
import os
from pathlib import Path
import subprocess
import sys
import traceback

os.environ["READER_SHARED_BUILD"] = "1"


def show_error(message):
    if sys.platform == "win32":
        ctypes.windll.user32.MessageBoxW(None, message, "文献阅读器", 0x10)
    elif sys.platform == "darwin":
        # Pass the message as argv, rather than interpolating AppleScript.
        subprocess.run(["/usr/bin/osascript", "-e", 'on run argv\ndisplay alert "文献阅读器" message (item 1 of argv) as critical\nend run', message], check=False)
    else:
        print(message, file=sys.stderr)


def launch():
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    os.chdir(root)
    sys.path.insert(0, str(root))
    from reader import DATA

    worker = len(sys.argv) == 3 and sys.argv[1] == "--reader-worker"
    log_path = DATA / ("jobs.log" if worker else "reader.log")
    try:
        DATA.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8", buffering=1) as log:
            previous = sys.stdout, sys.stderr
            sys.stdout = sys.stderr = log
            try:
                if worker:
                    from reader.store import Store
                    from reader.worker import run
                    run(Store(), sys.argv[2])
                else:
                    if not (root / "web" / "dist" / "index.html").is_file():
                        raise RuntimeError("阅读器文件不完整，请重新解压完整分享包。")
                    from reader.__main__ import main
                    main()
                return 0
            except SystemExit as exc:
                if not exc.code:
                    return 0
                traceback.print_exc()
                if not worker:
                    show_error(f"未能启动文献阅读器。\n启动日志：{log_path}")
            except Exception as exc:
                traceback.print_exc()
                if not worker:
                    show_error(f"未能启动文献阅读器：{exc}\n\n启动日志：{log_path}")
            finally:
                sys.stdout, sys.stderr = previous
    except OSError:
        if not worker:
            show_error(f"无法写入文献阅读器的启动日志：\n{log_path}")
    return 1


if __name__ == "__main__":
    raise SystemExit(launch())
