# run_waitress.py

from waitress import serve
from app import app  # This assumes your Flask app is declared as `app = Flask(__name__)` in app.py

if __name__ == "__main__":
    serve(app, host="0.0.0.0", port=8000)
