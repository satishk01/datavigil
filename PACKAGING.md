# Packaging DataVigil for testers

Two options, from lightest to most self-contained.

## Option A — Zip + Python (simplest)

Ship the project folder zipped. The tester needs Python 3.10+ installed, then
double-clicks `Start-DataVigil.bat`. The first run creates a virtual environment and
installs dependencies automatically; subsequent runs start in a couple of seconds.

```
# build the zip (from the project root)
#   excludes caches, local state, and screenshots
zip -r datavigil.zip . -x "*/__pycache__/*" ".dqtest/*" "*.png" ".venv/*"
```

## Option B — Single-file Windows executable (no Python required)

Use PyInstaller to bundle the server, dashboard, and a launcher into one `.exe`.

```
pip install pyinstaller
pyinstaller --name DataVigil --onefile ^
  --add-data "datavigil/web;datavigil/web" ^
  --collect-all sklearn --collect-all scipy --collect-all pandas ^
  launcher.py
```

`launcher.py` (provided) opens the browser and starts uvicorn. The result is
`dist/DataVigil.exe` — hand that single file (plus the `samples/` folder and the
test guide) to testers. For database support, build with the relevant driver
installed in the build environment (`pip install datavigil[aurora]`). To include
AWS features (Bedrock AI, Aurora IAM, MCP Data API), install
`boto3`/`mcp`/`awslabs.postgres-mcp-server` in the build environment too —
they are optional at runtime.

> Note: scientific wheels (numpy/scipy/scikit-learn) make the onefile exe large
> (~150–250 MB). For an internal tester audience, Option A is usually the better
> trade-off; use Option B when testers can't install Python at all.

## What to send a tester

- `datavigil.zip` **or** `DataVigil.exe`
- `samples/parts_inventory.csv`
- `BLACK_BOX_TEST_GUIDE.md`
