"""
OTLP Data Models for Ingestion
"""
from typing import List, Dict, Any, Optional, Union
from pydantic import BaseModel, Field

class AttributeValue(BaseModel):
    stringValue: Optional[str] = None
    boolValue: Optional[bool] = None
    intValue: Optional[str] = None  # OTLP ints are strings (int64)
    doubleValue: Optional[float] = None
    arrayValue: Optional[Dict[str, List["AttributeValue"]]] = None

class KeyValue(BaseModel):
    key: str
    value: AttributeValue

class Status(BaseModel):
    code: int
    message: Optional[str] = None

class SpanLink(BaseModel):
    traceId: str
    spanId: str
    traceState: Optional[str] = None
    attributes: List[KeyValue] = Field(default_factory=list)

class SpanEvent(BaseModel):
    timeUnixNano: str
    name: str
    attributes: List[KeyValue] = Field(default_factory=list)

class Span(BaseModel):
    traceId: str
    spanId: str
    parentSpanId: Optional[str] = None
    name: str
    kind: int
    startTimeUnixNano: str
    endTimeUnixNano: str
    attributes: List[KeyValue] = Field(default_factory=list)
    events: List[SpanEvent] = Field(default_factory=list)
    links: List[SpanLink] = Field(default_factory=list)
    status: Optional[Status] = None

class Scope(BaseModel):
    name: str
    version: Optional[str] = None

class ScopeSpans(BaseModel):
    scope: Scope
    spans: List[Span]

class Resource(BaseModel):
    attributes: List[KeyValue] = Field(default_factory=list)

class ResourceSpans(BaseModel):
    resource: Resource
    scopeSpans: List[ScopeSpans]

class TracePayload(BaseModel):
    resourceSpans: List[ResourceSpans]
