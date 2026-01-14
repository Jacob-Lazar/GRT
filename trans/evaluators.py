"""Online Evaluators for Trans Processor.

Evaluators run on the stream of incoming traces to compute quality
metrics in real-time. Results are attached to spans before storage.
"""

from __future__ import annotations

import json
import logging
import re
from abc import ABC, abstractmethod
from typing import Any

logger = logging.getLogger("trans.evaluators")


class Evaluator(ABC):
    """Base class for online evaluators.
    
    Evaluators analyze span data and return evaluation results that
    get attached to the span before storage.
    """
    
    name: str = "base"
    
    @abstractmethod
    async def evaluate(self, span: dict[str, Any]) -> dict[str, Any]:
        """Evaluate a span and return results.
        
        Args:
            span: The span dictionary to evaluate.
            
        Returns:
            Dictionary of evaluation results.
        """
        ...


class LatencyEvaluator(Evaluator):
    """Evaluates span latency against thresholds.
    
    Flags spans that exceed configured latency thresholds.
    
    Attributes:
        warn_threshold_ms: Latency above this triggers a warning.
        error_threshold_ms: Latency above this triggers an error flag.
    """
    
    name = "latency"
    
    def __init__(
        self,
        warn_threshold_ms: int = 3000,
        error_threshold_ms: int = 10000,
    ) -> None:
        self.warn_threshold_ms = warn_threshold_ms
        self.error_threshold_ms = error_threshold_ms
    
    async def evaluate(self, span: dict[str, Any]) -> dict[str, Any]:
        duration_ns = span.get("duration_ns", 0)
        duration_ms = duration_ns / 1_000_000
        
        result = {
            "duration_ms": duration_ms,
            "status": "ok",
        }
        
        if duration_ms >= self.error_threshold_ms:
            result["status"] = "error"
            result["message"] = f"Latency {duration_ms:.0f}ms exceeds {self.error_threshold_ms}ms"
        elif duration_ms >= self.warn_threshold_ms:
            result["status"] = "warn"
            result["message"] = f"Latency {duration_ms:.0f}ms exceeds {self.warn_threshold_ms}ms"
        
        return result


class GuardBreachEvaluator(Evaluator):
    """Detects guard breaches in span attributes.
    
    Looks for `guard.breach=true` or `guard.type` attributes that
    indicate safety limits were exceeded.
    """
    
    name = "guard_breach"
    
    async def evaluate(self, span: dict[str, Any]) -> dict[str, Any]:
        attrs_str = span.get("attributes_json", "{}")
        
        # Parse attributes safely using JSON
        try:
            if isinstance(attrs_str, str):
                attrs = json.loads(attrs_str) if attrs_str else {}
            else:
                attrs = attrs_str
        except Exception:
            attrs = {}
        
        breach_detected = (
            attrs.get("guard.breach") is True or
            attrs.get("guard.type") is not None
        )
        
        if breach_detected:
            return {
                "breach": True,
                "type": attrs.get("guard.type", "unknown"),
                "threshold": attrs.get("guard.threshold"),
                "value": attrs.get("guard.value"),
            }
        
        return {"breach": False}


class PIIDetector(Evaluator):
    """Detects potential PII in span content.
    
    Uses regex patterns to identify emails, phone numbers, SSNs,
    and API keys in input/output fields.
    
    Note: This is detection only. Scrubbing happens in a separate step.
    """
    
    name = "pii_detection"
    
    PATTERNS = {
        "email": re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}"),
        "phone": re.compile(r"\b\d{3}[-.]?\d{3}[-.]?\d{4}\b"),
        "ssn": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
        "api_key": re.compile(r"(sk-[a-zA-Z0-9]{20,}|api[_-]?key[=:]\s*['\"]?[a-zA-Z0-9]{20,})", re.I),
    }
    
    async def evaluate(self, span: dict[str, Any]) -> dict[str, Any]:
        attrs_str = span.get("attributes_json", "{}")
        
        # Parse attributes safely using JSON
        try:
            if isinstance(attrs_str, str):
                attrs = json.loads(attrs_str) if attrs_str else {}
            else:
                attrs = attrs_str
        except json.JSONDecodeError:
            attrs = {}
        
        # Fields to scan
        text_to_scan = " ".join([
            str(attrs.get("input", "")),
            str(attrs.get("output", "")),
            str(attrs.get("thought.reasoning", "")),
        ])
        
        detections = {}
        for pii_type, pattern in self.PATTERNS.items():
            matches = pattern.findall(text_to_scan)
            if matches:
                detections[pii_type] = len(matches)
        
        return {
            "pii_detected": bool(detections),
            "detections": detections,
        }


class EvaluatorPipeline:
    """Pipeline that runs multiple evaluators on each span.
    
    Aggregates results from all evaluators into a single dictionary
    that gets attached to the span.
    """
    
    def __init__(self, evaluators: list[Evaluator] | None = None) -> None:
        """Initialize the pipeline with evaluators.
        
        Args:
            evaluators: List of evaluator instances. If None, uses defaults.
        """
        self.evaluators = evaluators or [
            LatencyEvaluator(),
            GuardBreachEvaluator(),
            PIIDetector(),
        ]
        logger.info(f"Evaluator pipeline initialized: {[e.name for e in self.evaluators]}")
    
    async def evaluate(self, span: dict[str, Any]) -> dict[str, Any]:
        """Run all evaluators on a span.
        
        Args:
            span: The span dictionary to evaluate.
            
        Returns:
            Dictionary with results from each evaluator, keyed by evaluator name.
        """
        results = {}
        
        for evaluator in self.evaluators:
            try:
                result = await evaluator.evaluate(span)
                results[evaluator.name] = result
            except Exception as e:
                logger.warning(f"Evaluator {evaluator.name} failed: {e}")
                results[evaluator.name] = {"error": str(e)}
        
        return results
