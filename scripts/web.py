import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
from config.wsgi import application
from django.contrib.staticfiles.handlers import StaticFilesHandler
from waitress import serve

if __name__ == "__main__":
    # This is a local pilot only. Deliberately not configurable to 0.0.0.0.
    serve(StaticFilesHandler(application), host="127.0.0.1", port=int(os.environ.get("LEAD_WEB_PORT", "8765")), threads=4)

