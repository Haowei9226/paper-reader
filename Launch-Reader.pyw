"""Windows entry point for the shortcut; keep diagnostics in the local data folder."""
import ctypes
import os
import sys
import traceback
from pathlib import Path


def show_error(message):
    ctypes.windll.user32.MessageBoxW(None, message, "文献阅读器", 0x10)


def launch():
    root = Path(__file__).resolve().parent
    os.chdir(root)
    if not (root / "web" / "dist" / "index.html").is_file():
        show_error("阅读器尚未安装完成，请先运行项目目录中的 Setup.cmd。")
        return 1
    from reader import DATA

    log_path = DATA / "reader.log"
    try:
        DATA.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8", buffering=1) as log:
            previous = sys.stdout, sys.stderr
            sys.stdout = sys.stderr = log
            try:
                from reader.__main__ import main
                main()
                return 0
            except SystemExit as exc:
                if not exc.code:
                    return 0
                traceback.print_exc()
                show_error(f"未能启动文献阅读器。请检查启动日志：\n{log_path}")
            except Exception as exc:
                traceback.print_exc()
                explanation = str(exc) if isinstance(exc, RuntimeError) and str(exc).startswith("旧版文献阅读器") else "未能启动文献阅读器。"
                show_error(f"{explanation}\n\n启动日志：{log_path}")
            finally:
                sys.stdout, sys.stderr = previous
    except OSError:
        show_error(f"无法写入文献阅读器的启动日志：\n{log_path}")
    return 1


if __name__ == "__main__":
    raise SystemExit(launch())
