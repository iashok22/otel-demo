import logging
import os
import random
import time

from fastapi import Depends, FastAPI, HTTPException
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.requests import RequestsInstrumentor
from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
from opentelemetry.trace import Status, StatusCode
from sqlalchemy.orm import Session

from db import Item, engine, get_db
from schemas import ItemCreate, ItemOut
from telemetry import init_telemetry

telemetry = init_telemetry()
tracer = telemetry.tracer
meter = telemetry.meter

logger = logging.getLogger("otel_demo")

CHAOS_ERROR_RATE = float(os.getenv("CHAOS_ERROR_RATE", "0.1"))

app = FastAPI(title="OTel Demo API")

# --- Instrumentation: wires FastAPI request spans, SQLAlchemy query spans,
# and outbound `requests` calls into the same trace context automatically.
FastAPIInstrumentor.instrument_app(app)
SQLAlchemyInstrumentor().instrument(engine=engine)
RequestsInstrumentor().instrument()

# --- Custom metrics ---------------------------------------------------------
items_created_counter = meter.create_counter(
    name="items.created",
    unit="1",
    description="Number of items created via the API",
)
items_deleted_counter = meter.create_counter(
    name="items.deleted",
    unit="1",
    description="Number of items deleted via the API",
)
request_duration_histogram = meter.create_histogram(
    name="demo.request.duration",
    unit="ms",
    description="Hand-instrumented request duration for endpoints doing extra work",
)
chaos_error_counter = meter.create_counter(
    name="chaos.errors",
    unit="1",
    description="Number of synthetic errors raised by /chaos",
)
active_requests_gauge = meter.create_up_down_counter(
    name="demo.active_requests",
    unit="1",
    description="In-flight requests, tracked manually to demo gauges",
)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/items", response_model=ItemOut, status_code=201)
def create_item(item: ItemCreate, db: Session = Depends(get_db)):
    active_requests_gauge.add(1)
    start = time.perf_counter()
    try:
        with tracer.start_as_current_span("create_item.validate") as span:
            span.set_attribute("item.name", item.name)
            span.set_attribute("item.price", item.price)
            if item.price < 0:
                span.set_status(Status(StatusCode.ERROR, "negative price"))
                raise HTTPException(status_code=400, detail="price must be >= 0")

        db_item = Item(name=item.name, description=item.description, price=item.price)
        db.add(db_item)
        db.commit()
        db.refresh(db_item)

        items_created_counter.add(1, {"result": "success"})
        logger.info("created item id=%s name=%s", db_item.id, db_item.name)
        return db_item
    except HTTPException:
        items_created_counter.add(1, {"result": "rejected"})
        raise
    finally:
        active_requests_gauge.add(-1)
        request_duration_histogram.record(
            (time.perf_counter() - start) * 1000, {"endpoint": "create_item"}
        )


@app.get("/items", response_model=list[ItemOut])
def list_items(db: Session = Depends(get_db)):
    with tracer.start_as_current_span("list_items.query") as span:
        items = db.query(Item).order_by(Item.id).all()
        span.set_attribute("items.count", len(items))
    logger.info("listed %d items", len(items))
    return items


@app.get("/items/{item_id}", response_model=ItemOut)
def get_item(item_id: int, db: Session = Depends(get_db)):
    item = db.query(Item).filter(Item.id == item_id).first()
    if item is None:
        logger.warning("item id=%s not found", item_id)
        raise HTTPException(status_code=404, detail="item not found")
    return item


@app.delete("/items/{item_id}", status_code=204)
def delete_item(item_id: int, db: Session = Depends(get_db)):
    item = db.query(Item).filter(Item.id == item_id).first()
    if item is None:
        raise HTTPException(status_code=404, detail="item not found")
    db.delete(item)
    db.commit()
    items_deleted_counter.add(1)
    logger.info("deleted item id=%s", item_id)
    return None


@app.get("/chaos")
def chaos():
    """
    Simulates variable latency and a configurable failure rate (env var
    CHAOS_ERROR_RATE, default 10%) purely to generate interesting
    traces/metrics/logs for the demo (slow spans, error spans, error logs
    correlated by trace_id).
    """
    active_requests_gauge.add(1)
    start = time.perf_counter()
    with tracer.start_as_current_span("chaos.simulated_work") as span:
        delay = random.uniform(0.05, 1.2)
        span.set_attribute("chaos.delay_seconds", delay)
        time.sleep(delay)

        if random.random() < CHAOS_ERROR_RATE:
            span.set_status(Status(StatusCode.ERROR, "simulated downstream failure"))
            span.set_attribute("chaos.outcome", "error")
            chaos_error_counter.add(1)
            logger.error("chaos endpoint hit a simulated downstream failure")
            active_requests_gauge.add(-1)
            request_duration_histogram.record(
                (time.perf_counter() - start) * 1000, {"endpoint": "chaos"}
            )
            raise HTTPException(status_code=503, detail="simulated downstream failure")

        span.set_attribute("chaos.outcome", "success")

    logger.info("chaos endpoint succeeded after %.2fs", delay)
    active_requests_gauge.add(-1)
    request_duration_histogram.record(
        (time.perf_counter() - start) * 1000, {"endpoint": "chaos"}
    )
    return {"status": "ok", "delay_seconds": round(delay, 3)}
