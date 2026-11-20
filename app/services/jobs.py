"""Background work that must survive restarts of a free hosting instance."""
import logging
import threading
from datetime import datetime, timezone

from app.database import connect, upload_path

log = logging.getLogger(__name__)


def start_thread(target, *args):
    thread = threading.Thread(target=target, args=args, daemon=True)
    thread.start()
    return thread


def recover():
    """Resume generation, text extraction and topic search interrupted by a restart."""
    from app.main import process_document
    from app.services.generation import run_generation, update_job
    from app.services.topics import find_topics
    with connect() as db:
        jobs = [dict(r) for r in db.execute("SELECT id,status,resume_at FROM jobs WHERE status IN ('queued','waiting')")]
        documents = [dict(r) for r in db.execute("SELECT id,format,status,topic_status FROM documents WHERE deleted_at IS NULL AND (status='processing' OR topic_status='processing')")]
    for doc in documents:
        if doc['status'] == 'processing':
            path = upload_path(doc)
            if path and path.is_file():
                start_thread(process_document, doc['id'], path, doc['format'])
            else:
                with connect() as db:
                    db.execute("UPDATE documents SET status='error', error='Обработка прервана перезапуском, а исходный файл не сохранился. Загрузите файл повторно.', topic_status='error' WHERE id=?", (doc['id'],))
        elif doc['topic_status'] == 'processing':
            start_thread(find_topics, doc['id'])
    for job in jobs:
        if job['status'] == 'waiting':
            update_job(job['id'], status='queued')
        start_thread(run_generation, job['id'])


def parse_time(value):
    if not value:
        return None
    try:
        return datetime.strptime(value, '%Y-%m-%d %H:%M:%S').replace(tzinfo=timezone.utc)
    except ValueError:
        return None
