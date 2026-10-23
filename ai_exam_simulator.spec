# PyInstaller build spec:  pyinstaller ai_exam_simulator.spec
# Produces dist\AIExamSimulator\AIExamSimulator.exe (one-folder build, fastest startup).
from PyInstaller.utils.hooks import collect_data_files

block_cipher = None

a = Analysis(
    ["main.py"],
    pathex=["."],
    datas=[("ai_exam_simulator/data", "ai_exam_simulator/data")] + collect_data_files("reportlab"),
    hiddenimports=["keyring.backends.Windows", "PySide6.QtCharts", "PySide6.QtPdf"],
    excludes=["tkinter", "matplotlib", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets"],
    cipher=block_cipher,
)
pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="AIExamSimulator", console=False, upx=False)
coll = COLLECT(exe, a.binaries, a.zipfiles, a.datas, name="AIExamSimulator", upx=False)
