from celery import Celery

celery_app = Celery(
    "tinylink",
    broker="redis://localhost:6379/0",
    backend="redis://localhost:6379/0"
)

@celery_app.task
def record_click(link_id: int, ip_address: str):
    from app.database import SessionLocal
    from app import models
    from datetime import datetime, timezone
    
    db = SessionLocal()
    try:
        click = models.Click(
            link_id=link_id,
            ip_address=ip_address,
            clicked_at=datetime.now(timezone.utc)
        )
        db.add(click)
        db.commit()
    finally:
        db.close()