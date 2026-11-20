"""Run the coverage analysis: python main.py --data data --out output"""

import sys

from coverage.cli import main

if __name__ == "__main__":
    sys.exit(main())
