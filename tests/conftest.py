import sys
import pathlib

# Додаємо теку generate_symbol 2.5 у sys.path, щоб `import rollback_Gdocs` працював
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
