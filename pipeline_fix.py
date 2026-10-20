import subprocess
import sys
from pathlib import Path

# 1. Fix the inline loop in test_gui.py
tgui = Path('tests/test_gui.py')
if tgui.exists():
    text = tgui.read_text('utf-8')
    text = text.replace('for _ in range(20): pump(frame, g)', 'for _ in range(20):\n        pump(frame, g)')
    tgui.write_text(text, 'utf-8')

# 2. Fix the line length in pyproject.toml
pyc = Path('pyproject.toml')
if pyc.exists():
    text = pyc.read_text('utf-8')
    text = text.replace('filterwarnings = ["ignore::ResourceWarning", "ignore::pytest.PytestUnraisableExceptionWarning"]', 'filterwarnings = [\n    "ignore::ResourceWarning",\n    "ignore::pytest.PytestUnraisableExceptionWarning"\n]')
    pyc.write_text(text, 'utf-8')

# 3. Use python -m ruff to automatically fix import sorting and unused imports
print("Running ruff to automatically fix import orders and unused imports...")
subprocess.run([sys.executable, "-m", "ruff", "check", ".", "--fix"], check=False)
subprocess.run([sys.executable, "-m", "ruff", "format", "."], check=False)

print("\nDone. Ruff has formatted the codebase.")