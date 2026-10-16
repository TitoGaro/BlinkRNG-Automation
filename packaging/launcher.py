"""Punto de entrada del ejecutable de Windows (lo usa ``rngauto.spec``)."""
import sys

from rngauto.gui.main import main

if __name__ == "__main__":
    sys.exit(main())
