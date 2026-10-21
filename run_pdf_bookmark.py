"""PyInstaller 打包入口。"""

import multiprocessing
import sys

from pdf_bookmark.cli import entry

if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(entry())
