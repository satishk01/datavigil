"""PyInstaller entrypoint: open the browser and run the DataVigil server."""
import threading
import time
import webbrowser

HOST, PORT = "127.0.0.1", 8000


def _open():
    time.sleep(1.5)
    webbrowser.open(f"http://{HOST}:{PORT}")


if __name__ == "__main__":
    import uvicorn
    from datavigil.api.app import app
    threading.Thread(target=_open, daemon=True).start()
    uvicorn.run(app, host=HOST, port=PORT)
