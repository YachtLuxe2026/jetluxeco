web: gunicorn app:app --bind 0.0.0.0:$PORT --workers 2 --threads 4 --timeout 60
release: python -c "import db; db.init_db()"
