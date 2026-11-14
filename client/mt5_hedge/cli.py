"""入口命令 mt5-hedge:
    mt5-hedge            启动页面 + 两条腿(常用, 桌面快捷方式就是它)
    mt5-hedge setup      装两份 MT5 到 Program Files\\MT5-A / MT5-B, 写好 hedge.json
    mt5-hedge health|status|open|close|modify   命令行等价操作(见 hedge.py 文档)
"""
import sys


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        from .app import main as run
        run()
    elif argv[0] == "setup":
        from .setup_mt5 import main as run
        run(argv[1:])
    elif argv[0] in ("-h", "--help", "help"):
        print(__doc__)
    else:
        from .hedge import main as run
        try:
            run(argv)
        except RuntimeError as e:   # 腿没起来等: 一句话, 不要堆栈
            raise SystemExit(f"!! {e}")


if __name__ == "__main__":
    main()
