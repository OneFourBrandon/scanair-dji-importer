from pathlib import Path
import sys

if __package__:
    from .app import main
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scanair_dji_importer.app import main

if __name__ == "__main__":
    main()
